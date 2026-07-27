"""Unit tests for the k-means subscription-archetype extractor and the
location-affinity extractor against the synthetic CI fixture.

NONE of these tests touch the external real DB -- they use the
``fixture_duckdb`` conftest fixture exclusively.
"""

from __future__ import annotations

import pytest

from tenantless.analyzer import privacy
from tenantless.analyzer.extractors import archetypes, locations
from tenantless.analyzer.profile import build_profile
from tenantless.analyzer.reader import open_duckdb

from fixtures.build_fixture_duckdb import (
    MIN_BUCKET_SIZE,
    N_SUBSCRIPTIONS,
    RARE_LOCATION,
)


# --------------------------------------------------------------------------- #
# Archetype extractor
# --------------------------------------------------------------------------- #


def _build_archetypes(fixture_duckdb, k, min_bucket_size=MIN_BUCKET_SIZE):
    with open_duckdb(str(fixture_duckdb)) as reader:
        features = reader.subscription_features()
    return archetypes.extract(features, k=k, min_bucket_size=min_bucket_size)


@pytest.mark.parametrize("k", [3, 6])
def test_kmeans_produces_exactly_k_archetypes(fixture_duckdb, k):
    """KMeans(n_clusters=k) yields exactly k archetypes when k <= #subscriptions."""
    assert k <= N_SUBSCRIPTIONS
    arche = _build_archetypes(fixture_duckdb, k)
    assert len(arche) == k


def test_archetype_weights_sum_to_one(fixture_duckdb):
    arche = _build_archetypes(fixture_duckdb, k=3)
    total = sum(a["weight"] for a in arche)
    assert total == pytest.approx(1.0, abs=1e-6)
    assert all(0.0 <= a["weight"] <= 1.0 for a in arche)


def test_archetype_distribution_stats_shape(fixture_duckdb):
    arche = _build_archetypes(fixture_duckdb, k=3)
    for a in arche:
        assert set(a.keys()) == {
            "id",
            "weight",
            "resource_group_count",
            "resource_count",
            "location_distribution",
            "tag_density",
        }
        for field in ("resource_group_count", "resource_count"):
            assert set(a[field].keys()) == {"mean", "std", "min", "max"}
            assert a[field]["std"] >= 0.0
            assert a[field]["min"] <= a[field]["max"]
        assert set(a["tag_density"].keys()) == {"mean", "std"}
        assert a["tag_density"]["std"] >= 0.0


def test_archetype_location_distribution_sums_to_one(fixture_duckdb):
    arche = _build_archetypes(fixture_duckdb, k=3)
    for a in arche:
        ld = a["location_distribution"]
        assert ld, "each archetype must have a non-empty location_distribution"
        assert all(0.0 <= v <= 1.0 for v in ld.values())
        assert sum(ld.values()) == pytest.approx(1.0, abs=1e-6)


def test_archetype_ids_are_synthetic(fixture_duckdb):
    """ids must be synthetic ('archetype-N'), never derived from real names."""
    arche = _build_archetypes(fixture_duckdb, k=4)
    assert {a["id"] for a in arche} == {f"archetype-{i}" for i in range(4)}


def test_archetypes_are_reproducible(fixture_duckdb):
    """Two independent runs produce identical archetypes.

    KMeans has a fixed random_state, but KMeans++ init also depends on the
    feature-matrix ROW ORDER. reader.subscription_features sorts by
    subscription_id so DuckDB's thread-dependent GROUP BY/join order cannot
    re-roll the clustering. Regression guard for that determinism fix.
    """
    first = _build_archetypes(fixture_duckdb, k=4)
    second = _build_archetypes(fixture_duckdb, k=4)
    assert first == second


def test_subscription_features_row_order_is_stable(fixture_duckdb):
    """The feature frame is deterministically ordered by subscription_id."""
    with open_duckdb(str(fixture_duckdb)) as reader:
        ids_a = reader.subscription_features()["subscription_id"].to_list()
        ids_b = reader.subscription_features()["subscription_id"].to_list()
    assert ids_a == ids_b
    assert ids_a == sorted(ids_a)


# --------------------------------------------------------------------------- #
# Location extractor / min-bucket merge
# --------------------------------------------------------------------------- #


def test_rare_location_merged_into_other(fixture_duckdb):
    """A location below min_bucket_size folds into '__other__' and the
    distribution still sums to ~1.0."""
    with open_duckdb(str(fixture_duckdb)) as reader:
        loc_counts = reader.location_counts()

    dist = locations.extract(loc_counts, min_bucket_size=MIN_BUCKET_SIZE)
    # The rare location itself never appears as its own key.
    assert RARE_LOCATION not in dist
    assert "__other__" in dist
    assert sum(dist.values()) == pytest.approx(1.0, abs=1e-6)


def test_location_extract_no_merge_when_all_above_threshold(fixture_duckdb):
    """With min_bucket_size=1 nothing is merged; sum stays ~1.0."""
    with open_duckdb(str(fixture_duckdb)) as reader:
        loc_counts = reader.location_counts()
    dist = locations.extract(loc_counts, min_bucket_size=1)
    assert RARE_LOCATION in dist
    assert sum(dist.values()) == pytest.approx(1.0, abs=1e-6)


# --------------------------------------------------------------------------- #
# --k wired end-to-end through build_profile
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("k", [3, 6])
def test_build_profile_k_is_honored_exactly(fixture_duckdb, tmp_path, k):
    """build_profile(k=N) over the fixture produces EXACTLY N archetypes."""
    profile = build_profile(
        source=f"duckdb:{fixture_duckdb}",
        out=tmp_path / f"out-{k}.json",
        min_bucket_size=MIN_BUCKET_SIZE,
        k=k,
        allow_no_denylist=True,
    )
    assert len(profile["subscription_archetypes"]) == k


def test_no_duckdb_import_in_archetypes_or_locations():
    """Extractors are source-agnostic: they must not import duckdb."""
    import inspect

    for mod in (archetypes, locations):
        src = inspect.getsource(mod)
        assert "import duckdb" not in src
