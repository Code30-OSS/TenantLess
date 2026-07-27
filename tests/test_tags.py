"""Unit tests for the tag-distribution extractor (key_frequencies +
denylist-safe, min-bucketed value_distributions) against the synthetic CI
fixture.

NONE of these tests touch the external real DB.
"""

from __future__ import annotations

import inspect

import pytest

from tenantless.analyzer.extractors import tags
from tenantless.analyzer.reader import open_duckdb

from fixtures.build_fixture_duckdb import (
    MIN_BUCKET_SIZE,
    RARE_TAG_KEY,
    RARE_TAG_VALUE,
)


def _build_tags(fixture_duckdb, min_bucket_size=MIN_BUCKET_SIZE):
    with open_duckdb(str(fixture_duckdb)) as reader:
        key_counts = reader.tag_key_counts()
        value_counts = reader.tag_value_counts()
        total = reader.total_resources()
    return tags.extract(key_counts, value_counts, total, min_bucket_size)


def test_key_frequencies_in_unit_interval(fixture_duckdb):
    td = _build_tags(fixture_duckdb)
    kf = td["key_frequencies"]
    assert kf, "fixture carries at least one tag key"
    assert all(0.0 <= v <= 1.0 for v in kf.values())
    # Environment is the most common key in the fixture.
    assert "Environment" in kf


def test_value_distributions_normalized(fixture_duckdb):
    td = _build_tags(fixture_duckdb)
    vd = td["value_distributions"]
    assert vd, "fixture carries at least one tag value distribution"
    for key, dist in vd.items():
        assert dist, f"value distribution for {key} is non-empty"
        assert all(0.0 <= v <= 1.0 for v in dist.values())
        assert sum(dist.values()) == pytest.approx(1.0, abs=1e-6)


def test_rare_tag_value_folded_into_other(fixture_duckdb):
    """A real-looking tag value seen once (< min_bucket_size) must be ABSENT
    from the output (folded into __other__), never leaking verbatim."""
    td = _build_tags(fixture_duckdb)
    vd = td["value_distributions"]

    # The Owner key exists but its only value is below threshold.
    owner_dist = vd.get(RARE_TAG_KEY, {})
    assert RARE_TAG_VALUE not in owner_dist
    # If the key survived at all, the rare value is collapsed into __other__.
    if owner_dist:
        assert "__other__" in owner_dist

    # Stronger: the rare value must not appear ANYWHERE in the tag fragment.
    import json

    assert RARE_TAG_VALUE not in json.dumps(td)


def test_common_tag_value_survives(fixture_duckdb):
    """An above-threshold value (Environment=prod) survives verbatim."""
    td = _build_tags(fixture_duckdb)
    env = td["value_distributions"].get("Environment", {})
    assert "prod" in env or "dev" in env


def test_no_duckdb_import_in_tags():
    assert "import duckdb" not in inspect.getsource(tags)


# --- ANLZ-07: tag key co-occurrence / value cardinality / untagged-rate -------

import polars as pl  # noqa: E402

from tenantless.analyzer.extractors import cooccurrence  # noqa: E402


def test_tag_key_cooccurrence_normalized():
    """Tag key co-occurrence: (key_a, key_b, count) -> keyA -> {keyB: prob}.

    Probabilities are normalized per source key and min-bucket gated. A pair seen
    below ``min_bucket_size`` is dropped (privacy min-aggregation, ANLZ-07)."""
    pair_counts = pl.DataFrame(
        {
            "key_a": ["Environment", "Environment", "Environment"],
            "key_b": ["BU", "CostCenter", "RareKey"],
            "count": [60, 40, 2],  # RareKey pair below threshold (5)
        }
    )
    out = cooccurrence.tag_key_cooccurrence(pair_counts, min_bucket_size=5)

    assert "Environment" in out
    env = out["Environment"]
    assert "BU" in env and "CostCenter" in env
    # Below-threshold pair dropped.
    assert "RareKey" not in env
    # Normalized over the SURVIVING pairs for the source key.
    assert sum(env.values()) == pytest.approx(1.0, abs=1e-6)


def test_tag_value_cardinality_bucketed():
    """Tag value cardinality: distinct value count per key (min-bucket gated)."""
    value_counts = pl.DataFrame(
        {
            "tag_key": ["Environment", "Environment", "Environment", "Owner"],
            "tag_value": ["prod", "dev", "uat", "rare-owner"],
            "count": [60, 40, 20, 1],  # Owner's only value below threshold
        }
    )
    out = cooccurrence.tag_value_cardinality(value_counts, min_bucket_size=5)

    # Environment has 3 distinct above-threshold values.
    assert out["Environment"] == 3
    # Owner's single value is below threshold -> cardinality 0 (or key absent).
    assert out.get("Owner", 0) == 0


def test_untagged_rate_by_type():
    """Untagged-rate-by-type: per type, share of resources with NO tags."""
    per_type = pl.DataFrame(
        {
            "type": [
                "microsoft.compute/virtualmachines",
                "microsoft.storage/storageaccounts",
            ],
            "total": [100, 50],
            "tagged": [80, 50],
        }
    )
    out = cooccurrence.untagged_rate_by_type(per_type)

    vm = "Microsoft.compute/virtualmachines"
    sa = "Microsoft.storage/storageaccounts"
    assert out[vm] == pytest.approx(0.2, abs=1e-6)  # 20/100 untagged
    assert out[sa] == pytest.approx(0.0, abs=1e-6)  # fully tagged
