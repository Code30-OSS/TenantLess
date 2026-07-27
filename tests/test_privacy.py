"""Centerpiece acceptance test: the privacy denylist gate (DEFINING GATE).

Two directions, both required:

1. CLEAN: given the fixture's embedded fake real identifiers written into a temp
   denylist file, build_profile (with --denylist set) produces output in which
   NONE of the denylist strings appear anywhere.
2. LEAK: a deliberately leaked identifier injected into the profile dict makes
   scan_denylist raise -- proving the gate fails LOUDLY on a leak.

Runs against the synthetic fixture only; never the real DB.
"""

from __future__ import annotations

import json

import pytest

from tenantless.analyzer import privacy
from tenantless.analyzer.privacy import DenylistLeakError
from tenantless.analyzer.profile import DenylistRequiredError, build_profile

from fixtures.build_fixture_duckdb import fake_identifiers


def _write_denylist(tmp_path, terms):
    path = tmp_path / ".scan-denylist.json"
    path.write_text(json.dumps(terms), encoding="utf-8")
    return path


def test_clean_output_contains_no_denylist_strings(fixture_duckdb, tmp_path):
    """Direction 1: aggregated output leaks none of the real identifiers."""
    terms = fake_identifiers()
    denylist_path = _write_denylist(tmp_path, terms)

    profile = build_profile(
        source=f"duckdb:{fixture_duckdb}",
        out=tmp_path / "derived.json",
        min_bucket_size=5,
        denylist=denylist_path,
    )

    # build_profile already runs scan_denylist; assert again on the full blob.
    blob = json.dumps(profile)
    for term in terms:
        assert term not in blob
    # And the scan itself is clean (does not raise).
    privacy.scan_denylist(profile, terms)


def test_injected_leak_makes_scan_raise(fixture_duckdb, tmp_path):
    """Direction 2: a deliberately leaked identifier trips the gate loudly."""
    terms = fake_identifiers()

    profile = build_profile(
        source=f"duckdb:{fixture_duckdb}",
        out=tmp_path / "derived.json",
        min_bucket_size=5,
        allow_no_denylist=True,
    )

    # Inject a real identifier somewhere in the profile dict.
    leaked = terms[0]
    profile["resource_type_distributions"][f"leaked/{leaked}"] = {
        "frequency": 0.0,
        "property_distributions": {},
    }

    with pytest.raises(DenylistLeakError):
        privacy.scan_denylist(profile, terms)


def test_scan_ignores_substring_of_larger_word():
    """Whole-token matching: a term that is only a SUBSTRING of a larger word
    must NOT trip the gate (the false-positive class that broke real runs)."""
    # 'subscriptions' inside the structural key 'total_subscriptions';
    # 'ers' inside 'providers' / 'eastus' inside 'eastus2'.
    profile = {
        "source_stats": {"total_subscriptions": 123},
        "resource_type_distributions": {"Microsoft.network/providers": {}},
        "tag_distributions": {"value_distributions": {"region": {"eastus2": 1.0}}},
    }
    # None of these denylist terms appear as a WHOLE token in the profile.
    privacy.scan_denylist(profile, ["subscriptions", "ers", "eastus"])


def test_scan_catches_whole_token_identifier():
    """Whole-token matching still catches a real identifier bounded by
    separators or string edges -- the protection must not regress."""
    # A synthetic GUID. This fixture previously hardcoded a REAL subscription id
    # from the source tenant -- in the very test suite whose job is to prove real
    # identifiers never escape. The test only needs an identifier-shaped string
    # that it then passes as the denylist, so nothing real is required.
    uid = "b0a1c2d3-4e5f-4a6b-8c9d-0e1f2a3b4c5d"
    embedded = {"id": f"/subscriptions/{uid}/resourceGroups/rg"}
    # Bounded by '/' on both sides -> a real leak.
    with pytest.raises(DenylistLeakError):
        privacy.scan_denylist(embedded, [uid])
    # Bounded by a string edge (whole value) -> a real leak.
    with pytest.raises(DenylistLeakError):
        privacy.scan_denylist({"owner": "acme-data-uat-rg"}, ["acme-data-uat-rg"])


def test_build_profile_fails_loudly_when_denylist_term_leaks(fixture_duckdb, tmp_path):
    """If a denylist term would appear in the assembled output, build_profile raises.

    We force a leak by adding the fixture's COMMON resource-type substring fragment
    to the denylist so it matches a real emitted key, proving the end-to-end gate
    (not just the standalone scan) fails loudly.
    """
    # 'virtualmachines' appears in the emitted Microsoft.compute/virtualmachines key.
    denylist_path = _write_denylist(tmp_path, ["virtualmachines"])

    with pytest.raises(DenylistLeakError):
        build_profile(
            source=f"duckdb:{fixture_duckdb}",
            out=tmp_path / "should-not-write.json",
            min_bucket_size=5,
            denylist=denylist_path,
        )


# --- SEC-HIGH-1: fail-closed denylist enforcement for real-derived sources -----


def test_real_source_without_denylist_aborts(fixture_duckdb, tmp_path):
    """A real-derived source with NO denylist aborts BEFORE writing any output.

    Fail-closed: without an explicit ``allow_no_denylist`` escape hatch the run
    must raise a dedicated error AND leave the output path nonexistent (nothing
    written) so a real scan can never silently produce an unscanned profile.
    """
    out_path = tmp_path / "derived.json"
    with pytest.raises(DenylistRequiredError):
        build_profile(
            source=f"duckdb:{fixture_duckdb}",
            out=out_path,
            min_bucket_size=5,
            denylist=None,
        )
    assert not out_path.exists(), "no profile may be written when the run aborts"


def test_real_source_with_missing_denylist_path_aborts(fixture_duckdb, tmp_path):
    """A --denylist pointing at a non-existent path aborts for a real source."""
    out_path = tmp_path / "derived.json"
    missing = tmp_path / "does-not-exist.json"
    with pytest.raises(DenylistRequiredError):
        build_profile(
            source=f"duckdb:{fixture_duckdb}",
            out=out_path,
            min_bucket_size=5,
            denylist=missing,
        )
    assert not out_path.exists()


def test_real_source_with_empty_denylist_aborts(fixture_duckdb, tmp_path):
    """A denylist file that yields zero non-empty terms aborts for a real source.

    Both an empty list ``[]`` and an empty ``{"terms": []}`` object must abort —
    an empty denylist provides no leak protection, so it is treated like none.
    """
    for empty in ([], {"terms": []}, ["", "   "]):
        out_path = tmp_path / "derived.json"
        denylist_path = tmp_path / ".scan-denylist.json"
        denylist_path.write_text(json.dumps(empty), encoding="utf-8")
        with pytest.raises(DenylistRequiredError):
            build_profile(
                source=f"duckdb:{fixture_duckdb}",
                out=out_path,
                min_bucket_size=5,
                denylist=denylist_path,
            )
        assert not out_path.exists()


def test_allow_no_denylist_escape_hatch_permits_sample(fixture_duckdb, tmp_path):
    """The explicit escape hatch permits profiling a sample/test source w/o denylist."""
    out_path = tmp_path / "derived.json"
    profile = build_profile(
        source=f"duckdb:{fixture_duckdb}",
        out=out_path,
        min_bucket_size=5,
        denylist=None,
        allow_no_denylist=True,
    )
    assert out_path.exists(), "escape hatch must write the profile"
    assert profile["version"]


# --- P2-c: inverted set-membership matcher (equivalence + scaling) ------------


def _ref_raises(s, terms):
    """True iff the REFERENCE matcher (_check_string) flags a leak in ``s``."""
    cleaned = [t for t in terms if t and t.strip()]
    try:
        privacy._check_string(s, cleaned)
        return False
    except DenylistLeakError:
        return True


def _inv_raises(s, terms):
    """True iff the INVERTED matcher (_scan_string) flags a leak in ``s``."""
    cleaned = {t for t in terms if t and t.strip()}
    if not cleaned:
        return False
    matcher = (cleaned, max(len(t) for t in cleaned))
    try:
        privacy._scan_string(s, matcher)
        return False
    except DenylistLeakError:
        return True


def test_inverted_matcher_matches_reference_differentially():
    """The inverted matcher agrees (raise-vs-not) with the reference matcher.

    Property/differential test over random strings + random term sets drawn from
    an alphabet mixing identifier chars, separators and spaces -- the boundary
    cases (token edges, overlap, substring-of-word, terms with internal
    separators, leading/trailing separators) all arise. Seeded for determinism.
    """
    import random

    rnd = random.Random(20260629)
    alphabet = "ab_/-. 1"  # identifier chars + separators + space + digit
    for _ in range(5000):
        n_terms = rnd.randint(1, 6)
        terms = [
            "".join(rnd.choice(alphabet) for _ in range(rnd.randint(1, 6)))
            for _ in range(n_terms)
        ]
        s = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 24)))
        assert _ref_raises(s, terms) == _inv_raises(s, terms), (
            f"mismatch: s={s!r} terms={terms!r}"
        )


def test_inverted_matcher_handles_overlap_and_internal_separators():
    """Overlapping terms + terms carrying internal separators match the reference."""
    assert _inv_raises("x ab y", ["ab", "b"]) == _ref_raises("x ab y", ["ab", "b"])
    assert _inv_raises("/abc/", ["abc", "bc"]) == _ref_raises("/abc/", ["abc", "bc"])
    # A term with an internal separator (uuid-ish) flanked by non-ident chars.
    s = "/subscriptions/a-b-c/x"
    assert _inv_raises(s, ["a-b-c"]) == _ref_raises(s, ["a-b-c"]) is True


def test_scan_denylist_large_realistic_term_set_bounded_memory_and_time():
    """P2-c: a 100K REALISTIC-UNIQUE-term denylist scans a profile-size output
    with BOUNDED MEMORY and quickly.

    The earlier benchmark used shared-prefix synthetic terms, which collapse a
    trie and masked memory behavior. Here the terms are realistic UNIQUE names
    (distinct prefixes + a hex suffix). The inverted set matcher allocates only
    the term SET (O(terms)); a per-term trie would have been ~550 MiB at this
    count -- so a generous sub-150-MiB ceiling fails loudly if a trie returns.
    """
    import time
    import tracemalloc

    # Realistic, mostly-unique resource-name-like terms (no shared prefix).
    terms = [
        f"vm-{i:06d}-{(i * 2654435761) & 0xFFFFFFFF:08x}-fake" for i in range(100_000)
    ]
    # Bounded, profile-shaped output (~2000 short aggregate strings, no leak).
    profile = {f"Microsoft.Type{i}/kind{i}": float(i) for i in range(2000)}

    tracemalloc.start()
    start = time.perf_counter()
    privacy.scan_denylist(profile, terms)  # builds a set, scans the small output
    elapsed = time.perf_counter() - start
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    peak_mib = peak / (1024 * 1024)

    assert elapsed < 10.0, f"scan_denylist too slow on 100K terms: {elapsed:.1f}s"
    # The matcher's footprint is the set table only (no per-term trie). Far below
    # the ~550 MiB an Aho-Corasick trie used at the same count.
    assert peak_mib < 150, f"scan_denylist memory too high: {peak_mib:.0f} MiB"


def test_scan_denylist_still_trips_on_leak_with_large_term_set():
    """The inverted matcher still fails loudly when a real term IS present."""
    terms = [
        f"vm-{i:06d}-{(i * 2654435761) & 0xFFFFFFFF:08x}-fake" for i in range(50_000)
    ]
    leaked = {"resource": f"/subscriptions/{terms[42]}/x"}
    with pytest.raises(DenylistLeakError):
        privacy.scan_denylist(leaked, terms)
