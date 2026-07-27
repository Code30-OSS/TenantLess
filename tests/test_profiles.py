"""Bundled-profile tests: privacy leak gate, schema validity, packaging (PLAT-04).

Two concerns split across the plan's tasks:

1. LEAK + VALIDATE (Task 1): the two bundled profiles shipped under the package
   (``enterprise``, ``small``) must be schema-valid AND carry ZERO real-tenant /
   denylist tokens — the genericized ``enterprise`` is derived from a dev-only
   real-source profile (D-14), and the privacy boundary forbids any real
   identifier crossing into a shipped artifact.
2. RESOLUTION (Task 2): ``resolve_profile`` resolves a bundled name, an existing
   file path (back-compat), errors on an unknown name, and never lets a
   path-shaped/traversal value join into the package (V5 security).

These run inside the default ``-m 'not integration'`` suite (no DB needed).
"""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

import click
import pytest

import scrub_tokens

from jsonschema.exceptions import ValidationError

from tenantless.analyzer import privacy
from tenantless.analyzer.schema_validate import validate_profile

# The bundled profiles live under the package so importlib.resources can find
# them after an install; the path here resolves the same files for direct reads.
BUNDLED_DIR = files("tenantless.profiles")
ENTERPRISE = BUNDLED_DIR.joinpath("enterprise.json")
SMALL = BUNDLED_DIR.joinpath("small.json")

# The dev-only hardcoded profile (profiles/test-small.json) -- a v1.0 cost-less
# profile that must keep validating under the additive v1.2 schema bump.
TEST_SMALL = Path(__file__).resolve().parents[1] / "profiles" / "test-small.json"

# Tokens that must never leak into a shipped profile, loaded from data.
#
# These used to be assembled from string fragments so this file would not trip
# the whole-tree scrub gate. That defeated the public/private split: deleting the
# `+` signs reconstructed the private word list from a public file. They now come
# from tests/scrub-tokens.json plus the gitignored private supplement, so a
# public checkout checks the generic sentinels and a maintainer's checkout also
# covers the real internal names.
PRODUCT_TOKENS = list(scrub_tokens.all_tokens())

# The gitignored real-identifier denylist (the privacy backstop). It carries the
# real-tenant tokens — company names, business units, and real people — that the
# genericized enterprise profile must never leak. It is NEVER committed, so its
# contents must NOT be hardcoded into this source file. Loaded at runtime; when
# absent (the OSS / CI case) the deep-real-token assertions skip.
_DENYLIST_PATH = (
    Path(__file__).resolve().parents[1] / "profiles" / ".scan-denylist.json"
)


def _real_tokens() -> list[str] | None:
    """Load the deep real-identifier tokens from the gitignored denylist.

    Returns ``None`` when the denylist is absent (OSS / CI): the caller skips.
    """
    if not _DENYLIST_PATH.is_file():
        return None
    data = json.loads(_DENYLIST_PATH.read_text(encoding="utf-8"))
    terms = data.get("terms", data) if isinstance(data, dict) else data
    return [t for t in terms if isinstance(t, str) and t]


def _load(traversable) -> dict:
    return json.loads(traversable.read_bytes())


# --- Task 1: privacy leak gate -------------------------------------------------


def test_bundled_profiles_exist():
    """Both named profiles ship under the package."""
    assert ENTERPRISE.is_file()
    assert SMALL.is_file()


def test_enterprise_is_product_token_clean():
    """The genericized enterprise profile leaks none of the product-name tokens.

    scan_denylist walks every key + string value and raises loudly on any whole-
    token match — invariant 1 (privacy boundary). It must NOT raise.
    """
    enterprise = _load(ENTERPRISE)
    privacy.scan_denylist(enterprise, PRODUCT_TOKENS)  # must not raise


def test_small_is_product_token_clean():
    """The small demo profile carries no product-name tokens either."""
    small = _load(SMALL)
    privacy.scan_denylist(small, PRODUCT_TOKENS)  # must not raise


def test_enterprise_is_real_denylist_clean():
    """When the gitignored denylist is present, enterprise leaks none of its
    deep real-identifier tokens (company names, BUs, real people)."""
    tokens = _real_tokens()
    if tokens is None:
        pytest.skip("gitignored denylist absent (OSS/CI): no deep real tokens to scan")
    privacy.scan_denylist(_load(ENTERPRISE), tokens)  # must not raise


def test_no_product_token_substring_anywhere_in_enterprise():
    """Belt-and-suspenders raw-substring scan of the serialized enterprise blob.

    scan_denylist is whole-token; this catches a product token even embedded
    inside a larger string (the OSS-scrub bar over the bundled dir must be empty).
    """
    blob = ENTERPRISE.read_text(encoding="utf-8")
    for token in PRODUCT_TOKENS:
        assert token not in blob, f"product token {token!r} leaked into enterprise.json"


def test_no_real_token_substring_anywhere_in_enterprise():
    """When the denylist is present, no deep real token appears as a raw substring
    anywhere in the serialized enterprise blob."""
    tokens = _real_tokens()
    if tokens is None:
        pytest.skip("gitignored denylist absent (OSS/CI): no deep real tokens to scan")
    blob = ENTERPRISE.read_text(encoding="utf-8")
    for token in tokens:
        assert token not in blob, f"real token {token!r} leaked into enterprise.json"


def test_no_forbidden_token_in_bundled_dir():
    """No forbidden token may appear in a bundled profile.

    Needles come from the scrub-token data files, never from literals in this
    source -- see the PRODUCT_TOKENS note above for why.
    """
    tokens = scrub_tokens.all_tokens()
    assert tokens, "no scrub tokens configured -- refusing a vacuous pass"
    for prof in (ENTERPRISE, SMALL):
        text = prof.read_text(encoding="utf-8").lower()
        for needle in tokens:
            assert needle not in text, f"{needle!r} leaked into {prof.name}"


def test_enterprise_validates_against_schema():
    """The genericized enterprise profile is still schema-valid."""
    validate_profile(_load(ENTERPRISE))


def test_enterprise_is_v1_2_with_cost_distributions():
    """The regenerated enterprise profile is v1.2 and carries cost_distributions.

    Plan 09-02 (COST-01/D-02): `--profile enterprise` must ship a fitted cost
    section so generated tenants have realistic per-resource cost. The section is
    privacy-clean by construction -- canonical Microsoft.* type keys + numeric
    lognormal params only (the denylist-clean assertions above cover the leak gate).
    """
    enterprise = _load(ENTERPRISE)
    assert enterprise["version"] == "1.2"

    cost = enterprise.get("cost_distributions")
    assert cost, "enterprise.json must carry a non-empty cost_distributions section"

    # Every entry is a fitted per-type distribution: canonical Microsoft.* key,
    # required `distribution`, numeric params only -- no identifier-shaped value.
    for type_key, entry in cost.items():
        assert type_key.lower().startswith("microsoft."), type_key
        assert entry["distribution"] in {"lognormal", "gamma"}
        assert isinstance(entry["mu"], (int, float))
        assert isinstance(entry["sigma"], (int, float)) and entry["sigma"] >= 0
        assert isinstance(entry["sample_count"], int) and entry["sample_count"] >= 0


def test_small_validates_against_schema():
    """The small demo profile is schema-valid."""
    validate_profile(_load(SMALL))


# --- v1.2 cost_distributions: additive-optional schema (COST-01) ---------------


def test_v1_cost_less_profiles_still_validate_under_v1_2():
    """v1.0 cost-less profiles (small.json, test-small.json) still validate.

    The v1.2 bump only ADDS the optional cost_distributions property; a profile
    with no cost section -- and an older `version` -- must remain valid (the
    generator zero-fills cost for these). This is the back-compat guarantee.
    """
    small = _load(SMALL)
    assert "cost_distributions" not in small  # cost-less by construction
    validate_profile(small)  # must not raise

    test_small = json.loads(TEST_SMALL.read_text(encoding="utf-8"))
    assert "cost_distributions" not in test_small
    validate_profile(test_small)  # must not raise


def test_cost_bearing_profile_validates():
    """A profile carrying a cost_distributions section validates under v1.2."""
    profile = _load(SMALL)
    profile["version"] = "1.2"
    profile["cost_distributions"] = {
        "Microsoft.compute/virtualmachines": {
            "distribution": "lognormal",
            "mu": 3.5,
            "sigma": 1.2,
            "sample_count": 5730,
        }
    }
    validate_profile(profile)  # must not raise


def test_malformed_cost_entry_is_rejected():
    """A cost entry missing the required `distribution` key fails validation."""
    profile = _load(SMALL)
    profile["version"] = "1.2"
    profile["cost_distributions"] = {
        "Microsoft.compute/virtualmachines": {
            # `distribution` is required by the schema -- omitting it is invalid.
            "mu": 3.5,
            "sigma": 1.2,
        }
    }
    with pytest.raises(ValidationError):
        validate_profile(profile)


def test_dev_only_sources_are_not_packaged():
    """The dev-only real-source artifacts must NOT ship under src/ (Pitfall 4).

    The real-derived profile and the local privacy denylist are gitignored
    dev-only files; neither may be packaged inside ``src/tenantless/profiles``.
    """
    pkg_profiles = Path(__file__).resolve().parents[1] / "src" / "tenantless" / "profiles"
    # No real-derived profile (``*-real.json``) and no local denylist (dotfile).
    assert not list(pkg_profiles.glob("*-real.json"))
    assert not list(pkg_profiles.glob(".*-denylist.json"))


# --- Task 2: resolve_profile resolution + traversal safety ---------------------


def test_resolve_bundled_name_enterprise():
    """Given a bundled name, resolve_profile returns the packaged enterprise.json.

    The returned Traversable/Path must read back as the bundled enterprise blob
    (resolvable via importlib.resources — works pre-install).
    """
    from tenantless.generator.profile_input import resolve_profile

    resolved = resolve_profile("enterprise")
    data = json.loads(Path(resolved).read_bytes()) if isinstance(resolved, Path) else json.loads(resolved.read_bytes())
    # It is the bundled enterprise profile (schema-valid, denylist-clean).
    validate_profile(data)
    assert data == _load(ENTERPRISE)


def test_resolve_bundled_name_small():
    """Given the bundled name 'small', resolve_profile returns the demo profile."""
    from tenantless.generator.profile_input import resolve_profile

    resolved = resolve_profile("small")
    raw = Path(resolved).read_bytes() if isinstance(resolved, Path) else resolved.read_bytes()
    assert json.loads(raw) == _load(SMALL)


def test_resolve_existing_path_returns_that_path(tmp_path):
    """Given an existing file path, resolve_profile returns that Path (D-12 back-compat)."""
    from tenantless.generator.profile_input import resolve_profile

    p = tmp_path / "my-profile.json"
    p.write_text("{}", encoding="utf-8")
    resolved = resolve_profile(str(p))
    assert Path(resolved) == p


def test_resolve_unknown_name_raises_usageerror():
    """Given an unknown name, resolve_profile raises click.UsageError naming available profiles."""
    from tenantless.generator.profile_input import resolve_profile

    with pytest.raises(click.UsageError) as exc:
        resolve_profile("bogus")
    msg = str(exc.value)
    assert "enterprise" in msg and "small" in msg


def test_resolve_traversal_value_does_not_read_inside_package():
    """Given a traversal-shaped value, resolution goes through Path.is_file() only (V5).

    A path-shaped/`..` value must NOT be joined into the package namespace. Since
    such a path does not exist, it falls through to the error branch — proving no
    implicit package join smuggled it in.
    """
    from tenantless.generator.profile_input import resolve_profile

    # A traversal stem that would resolve to enterprise.json IF naively joined
    # (`files(pkg).joinpath("../profiles/enterprise")`) must NOT resolve.
    with pytest.raises(click.UsageError):
        resolve_profile("../profiles/enterprise")
    # A bare `..`-style separator value likewise errors (not silently read).
    with pytest.raises(click.UsageError):
        resolve_profile("../secret")


def test_load_profile_accepts_bundled_traversable():
    """load_profile works on a Traversable returned by resolve_profile (path.read_bytes seam)."""
    from tenantless.generator.profile_input import load_profile, resolve_profile

    profile = load_profile(resolve_profile("small"))
    assert profile == _load(SMALL)
