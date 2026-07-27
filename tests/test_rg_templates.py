"""Unit tests for the RG type-set template extractor (top 30 + __misc__) and
the casing consistency between resource_group_templates[].type_set and
resource_type_distributions keys, against the synthetic CI fixture.

NONE of these tests touch the external real DB.
"""

from __future__ import annotations

import pytest

from tenantless.analyzer.extractors import resource_types, rg_templates
from tenantless.analyzer.profile import build_profile
from tenantless.analyzer.reader import open_duckdb

from fixtures.build_fixture_duckdb import MIN_BUCKET_SIZE, RARE_TYPE


def _build_templates(fixture_duckdb, min_bucket_size=MIN_BUCKET_SIZE):
    with open_duckdb(str(fixture_duckdb)) as reader:
        rg_sets = reader.rg_type_sets()
    return rg_templates.extract(rg_sets, min_bucket_size=min_bucket_size)


def test_at_most_31_templates(fixture_duckdb):
    """Top 30 + a single __misc__ => at most 31 templates."""
    templates = _build_templates(fixture_duckdb)
    assert len(templates) <= 31


def test_template_weights_sum_to_one(fixture_duckdb):
    templates = _build_templates(fixture_duckdb)
    total = sum(t["weight"] for t in templates)
    assert total == pytest.approx(1.0, abs=1e-6)
    assert all(0.0 <= t["weight"] <= 1.0 for t in templates)


def test_every_template_has_non_empty_type_set(fixture_duckdb):
    templates = _build_templates(fixture_duckdb)
    assert templates
    for t in templates:
        assert isinstance(t["type_set"], list)
        assert len(t["type_set"]) >= 1
        assert set(t["resource_count"].keys()) == {"mean", "std", "min", "max"}
        assert t["resource_count"]["std"] >= 0.0


def test_rare_composition_folds_into_misc(fixture_duckdb):
    """The fixture's single rare RG composition (below min_bucket_size) must land
    in the __misc__ template, not as its own template."""
    templates = _build_templates(fixture_duckdb)
    ids = {t["id"] for t in templates}
    assert "__misc__" in ids

    rare_key = resource_types.normalize_type_key(RARE_TYPE)
    # The rare type must NOT appear as a standalone (non-misc) template type_set.
    for t in templates:
        if t["id"] == "__misc__":
            continue
        assert t["type_set"] != [rare_key]


def test_type_set_entries_follow_casing_rule(fixture_duckdb):
    """Every type_set entry uses the documented normalize_type_key casing."""
    templates = _build_templates(fixture_duckdb)
    for t in templates:
        if t["id"] == "__misc__":
            continue
        for entry in t["type_set"]:
            assert entry == resource_types.normalize_type_key(entry)


def test_casing_matches_resource_type_distributions(fixture_duckdb, tmp_path):
    """For any type present in both sections, the key casing matches exactly."""
    profile = build_profile(
        source=f"duckdb:{fixture_duckdb}",
        out=tmp_path / "out.json",
        min_bucket_size=MIN_BUCKET_SIZE,
        allow_no_denylist=True,
    )
    rtd_keys = set(profile["resource_type_distributions"].keys())
    template_types = {
        entry
        for t in profile["resource_group_templates"]
        if t["id"] != "__misc__"
        for entry in t["type_set"]
    }
    # Every non-misc template type that also appears in rtd matches casing exactly.
    overlap = template_types & rtd_keys
    assert overlap, "fixture should share at least one type across both sections"
    for t_type in template_types:
        # Casing rule: each entry equals its own normalization (idempotent).
        assert t_type == resource_types.normalize_type_key(t_type)


def test_build_profile_assembles_real_rg_templates(fixture_duckdb, tmp_path):
    """build_profile output carries non-placeholder RG templates."""
    profile = build_profile(
        source=f"duckdb:{fixture_duckdb}",
        out=tmp_path / "out.json",
        min_bucket_size=MIN_BUCKET_SIZE,
        allow_no_denylist=True,
    )
    templates = profile["resource_group_templates"]
    assert templates
    assert all(t["id"] != "placeholder" for t in templates)
    # Real archetypes too (Task 1), each with a non-empty location_distribution.
    arche = profile["subscription_archetypes"]
    assert all(a["id"] != "placeholder" for a in arche)
    assert all(a["location_distribution"] for a in arche)


def test_misc_carries_type_weights_and_empty_share(fixture_duckdb):
    """The __misc__ template enriches with a normalized type_weights histogram
    (so the privacy bucket still carries resource-type mass) and an empty_share
    fraction — the fix for the ~55%-empty-RG generation artifact."""
    templates = _build_templates(fixture_duckdb)
    misc = next((t for t in templates if t["id"] == "__misc__"), None)
    assert misc is not None
    tw = misc.get("type_weights")
    assert isinstance(tw, dict) and tw, "misc must carry a non-empty type_weights histogram"
    assert sum(tw.values()) == pytest.approx(1.0, abs=1e-6)
    for key, val in tw.items():
        assert 0.0 <= val <= 1.0
        assert key != "__misc__", "sentinel must never appear in type_weights"
        assert key == resource_types.normalize_type_key(key)  # canonical casing
    # The fixture's folded rare type contributes mass to the misc histogram.
    assert resource_types.normalize_type_key(RARE_TYPE) in tw
    es = misc.get("empty_share")
    assert es is None or (0.0 <= es <= 1.0)


def test_misc_type_weights_mass_and_empty_share_pure():
    """Mass approximation (resource_count / |type_set| per RG) + empty_share,
    against a hand-built frame — no fixture DB."""
    import polars as pl

    a = resource_types.normalize_type_key("Microsoft.Foo/a")
    b = resource_types.normalize_type_key("Microsoft.Foo/b")
    c = resource_types.normalize_type_key("Microsoft.Foo/c")
    rows = (
        [{"type_set": ["Microsoft.Foo/a"], "resource_count": 4}]  # A += 4
        + [{"type_set": ["Microsoft.Foo/a", "Microsoft.Foo/b"], "resource_count": 6}]  # A+=3, B+=3
        + [{"type_set": [], "resource_count": 0}] * 2  # two truly-empty RGs
        + [{"type_set": ["Microsoft.Foo/c"], "resource_count": 2}] * 5  # frequent → standalone
    )
    templates = rg_templates.extract(pl.DataFrame(rows), min_bucket_size=5, top_n=30)
    misc = next(t for t in templates if t["id"] == "__misc__")
    tw = misc["type_weights"]
    # A mass = 4 + 6/2 = 7; B mass = 6/2 = 3; total 10.
    assert tw[a] == pytest.approx(0.7)
    assert tw[b] == pytest.approx(0.3)
    assert c not in tw  # C is a standalone template, not folded into misc
    # 4 folded RGs (1×[A], 1×[A,B], 2×empty); empty_share = 2/4.
    assert misc["empty_share"] == pytest.approx(0.5)
    assert any(t["type_set"] == [c] for t in templates)


def test_misc_type_weights_apply_identifier_shape_guard():
    """Shared data-boundary guard (analyzer-shared-privacy-guard): an
    identifier-shaped 'type' folded into misc must NOT leak into type_weights."""
    import polars as pl

    from tenantless.analyzer.extractors.tags import _is_identifier_shaped_key

    leaky = "/subscriptions/11111111-1111-4111-8111-111111111111/rg"
    safe = resource_types.normalize_type_key("Microsoft.Foo/a")
    rows = (
        [{"type_set": [leaky, "Microsoft.Foo/a"], "resource_count": 4}]  # rare → misc
        + [{"type_set": ["Microsoft.Foo/c"], "resource_count": 2}] * 5  # frequent → standalone
    )
    templates = rg_templates.extract(pl.DataFrame(rows), min_bucket_size=5, top_n=30)
    misc = next(t for t in templates if t["id"] == "__misc__")
    tw = misc.get("type_weights", {})
    assert leaky not in tw
    assert not any(_is_identifier_shaped_key(k) for k in tw)
    assert safe in tw  # the co-folded safe type survives


def test_no_duckdb_import_in_rg_templates():
    import inspect

    assert "import duckdb" not in inspect.getsource(rg_templates)
