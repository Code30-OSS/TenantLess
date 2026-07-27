"""Unit tests for the reader, resource-type extractor, privacy min-aggregation,
schema validator, and profile assembler against the synthetic CI fixture.

NONE of these tests touch the external real DB -- they use the
``fixture_duckdb`` conftest fixture exclusively.
"""

from __future__ import annotations

import duckdb
import polars as pl
import pytest
from jsonschema.exceptions import ValidationError

from tenantless.analyzer import privacy, schema_validate
from tenantless.analyzer.extractors import resource_types
from tenantless.analyzer.profile import build_profile
from tenantless.analyzer.reader import DuckDBReader, open_duckdb

from fixtures.build_fixture_duckdb import (
    COMMON_TYPE,
    N_SUBSCRIPTIONS,
    RARE_TYPE,
    RARE_TYPE_COUNT,
)


def test_reader_returns_polars_frame_readonly(fixture_duckdb):
    """Reader opens read_only and returns a Polars (type, count) frame."""
    with open_duckdb(str(fixture_duckdb)) as reader:
        counts = reader.resource_type_counts()
        stats = reader.source_stats()

    assert isinstance(counts, pl.DataFrame)
    assert set(counts.columns) == {"type", "count"}
    assert counts.height >= 2
    # source_stats are positive integers.
    assert stats["total_subscriptions"] == N_SUBSCRIPTIONS
    assert stats["total_resources"] >= 1


def test_min_bucket_drops_low_count_type(fixture_duckdb):
    """A type seen RARE_TYPE_COUNT (4) times is ABSENT after min_bucket_size=5,
    while the dominant type survives with a normalized frequency in [0, 1]."""
    assert RARE_TYPE_COUNT < 5  # guard: fixture proves the drop at default 5

    with open_duckdb(str(fixture_duckdb)) as reader:
        counts = reader.resource_type_counts()

    surviving = privacy.merge_min_buckets(counts, min_bucket_size=5)
    rtd = resource_types.extract(surviving)

    # The rare type (lowercase microsoft.* -> canonical Microsoft.*) is dropped.
    rare_key = resource_types.normalize_type_key(RARE_TYPE)
    common_key = resource_types.normalize_type_key(COMMON_TYPE)
    assert rare_key not in rtd
    assert common_key in rtd

    freq = rtd[common_key]["frequency"]
    assert 0.0 < freq <= 1.0
    # frequencies over surviving buckets sum to ~1.0
    assert pytest.approx(sum(v["frequency"] for v in rtd.values()), rel=1e-9) == 1.0


def test_every_type_entry_has_empty_property_distributions(fixture_duckdb):
    """Each emitted resource_type_distributions entry carries property_distributions == {}."""
    with open_duckdb(str(fixture_duckdb)) as reader:
        counts = reader.resource_type_counts()
    surviving = privacy.merge_min_buckets(counts, min_bucket_size=5)
    rtd = resource_types.extract(surviving)

    assert rtd  # at least one surviving type
    assert all(entry["property_distributions"] == {} for entry in rtd.values())
    assert any(entry["property_distributions"] == {} for entry in rtd.values())


def test_normalize_type_key_canonicalizes_microsoft_namespace():
    assert (
        resource_types.normalize_type_key("microsoft.compute/virtualmachines")
        == "Microsoft.compute/virtualmachines"
    )
    # non-microsoft types pass through unchanged
    assert resource_types.normalize_type_key("custom/thing") == "custom/thing"


def test_schema_validate_rejects_extra_top_level_key(fixture_duckdb, tmp_path):
    """validate_profile rejects a dict with a stray top-level key
    (additionalProperties: false)."""
    profile = build_profile(
        source=f"duckdb:{fixture_duckdb}",
        out=tmp_path / "out.json",
        min_bucket_size=5,
        allow_no_denylist=True,
    )
    # The clean profile validates...
    schema_validate.validate_profile(profile)

    # ...but a stray key is rejected.
    bad = dict(profile)
    bad["unexpected_key"] = 123
    with pytest.raises(ValidationError):
        schema_validate.validate_profile(bad)


def test_build_profile_produces_schema_valid_dict(fixture_duckdb, tmp_path):
    """build_profile over the fixture yields a schema-valid dict, including a
    valid RFC3339 extracted_at and property_distributions on every type."""
    out = tmp_path / "derived.json"
    profile = build_profile(
        source=f"duckdb:{fixture_duckdb}",
        out=out,
        min_bucket_size=5,
        allow_no_denylist=True,
    )
    # Does not raise.
    schema_validate.validate_profile(profile)
    assert out.exists()

    # extracted_at is an RFC3339 date-time accepted by the format checker.
    assert profile["extracted_at"].endswith("Z")

    # Real source_stats from the fixture.
    assert profile["source_stats"]["total_subscriptions"] == N_SUBSCRIPTIONS

    # Every resource_type_distributions entry has property_distributions.
    rtd = profile["resource_type_distributions"]
    assert rtd
    assert all("property_distributions" in v for v in rtd.values())
    assert any(v["property_distributions"] == {} for v in rtd.values())


def test_build_profile_rejects_bad_datetime_format(monkeypatch, fixture_duckdb, tmp_path):
    """A malformed extracted_at is rejected by the schema's date-time format."""
    from tenantless.analyzer import profile as profile_mod

    monkeypatch.setattr(profile_mod, "_utc_now_rfc3339", lambda: "not-a-date")
    with pytest.raises(ValidationError):
        build_profile(
            source=f"duckdb:{fixture_duckdb}",
            out=tmp_path / "bad.json",
            min_bucket_size=5,
            allow_no_denylist=True,
        )


# --- Plan 09-01 Task 1: resource_cost_samples() DuckDB seam -------------------

def _cost_reader_with_costs() -> DuckDBReader:
    """Build an in-memory DuckDB carrying a resource_costs ⋈ resources fixture.

    Facts the cost-reader tests rely on:
      * ``res-AAA`` (type virtualmachines) costs are recorded under the
        LOWER-case id ``res-aaa`` -> proves the case-insensitive join matches.
      * That same resource has TWO meter rows for billing_month ``2025-01``
        (10.0 + 5.0) -> proves multi-meter rows SUM to one 15.0 sample, plus a
        single 7.0 row for ``2025-02``.
      * ``res-bbb`` (type storageaccounts) has one 3.0 cost row.
    So the expected (type, monthly_cost) frame is exactly 3 rows:
    vm -> {15.0, 7.0}, storage -> {3.0}. A case-SENSITIVE join would drop the
    vm rows entirely (height would be 1), and an un-summed join would yield 4.
    """
    conn = duckdb.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE resources (resource_id VARCHAR, type VARCHAR);
        CREATE TABLE resource_costs (
            resource_id VARCHAR,
            subscription_id VARCHAR,
            billing_month VARCHAR,
            amortized_cost_eur DOUBLE,
            cost_type VARCHAR
        );
        INSERT INTO resources VALUES
            ('res-AAA', 'microsoft.compute/virtualmachines'),
            ('res-bbb', 'microsoft.storage/storageaccounts');
        INSERT INTO resource_costs VALUES
            ('res-aaa', 'sub-1', '2025-01', 10.0, 'AmortizedCost'),
            ('res-aaa', 'sub-1', '2025-01',  5.0, 'AmortizedCost'),
            ('res-aaa', 'sub-1', '2025-02',  7.0, 'AmortizedCost'),
            ('res-bbb', 'sub-1', '2025-01',  3.0, 'AmortizedCost');
        """
    )
    return DuckDBReader(conn)


def test_resource_cost_samples_returns_type_and_cost_only():
    """The cost seam returns exactly (type, monthly_cost) -- no identifier cols."""
    reader = _cost_reader_with_costs()
    frame = reader.resource_cost_samples()

    assert isinstance(frame, pl.DataFrame)
    assert set(frame.columns) == {"type", "monthly_cost"}
    # No identifier column ever crosses the seam.
    assert "resource_id" not in frame.columns
    assert "subscription_id" not in frame.columns
    assert "billing_month" not in frame.columns


def test_resource_cost_samples_sums_multimeter_and_joins_case_insensitively():
    """Multi-meter (resource,month) rows SUM; the join is case-insensitive."""
    reader = _cost_reader_with_costs()
    frame = reader.resource_cost_samples()

    # 3 samples total: vm 2025-01 (summed), vm 2025-02, storage 2025-01.
    # A case-sensitive join would have produced 1 row; an un-summed one, 4.
    assert frame.height == 3

    vm_costs = sorted(
        frame.filter(pl.col("type") == "microsoft.compute/virtualmachines")[
            "monthly_cost"
        ].to_list()
    )
    # 10.0 + 5.0 collapsed into one 15.0 sample (not two rows, not max).
    assert vm_costs == [7.0, 15.0]

    storage_costs = frame.filter(
        pl.col("type") == "microsoft.storage/storageaccounts"
    )["monthly_cost"].to_list()
    assert storage_costs == [3.0]


def test_resource_cost_samples_empty_when_table_absent():
    """A source WITHOUT a resource_costs table degrades to an empty frame."""
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE resources (resource_id VARCHAR, type VARCHAR);")
    reader = DuckDBReader(conn)

    frame = reader.resource_cost_samples()  # must NOT raise
    assert isinstance(frame, pl.DataFrame)
    assert set(frame.columns) == {"type", "monthly_cost"}
    assert frame.is_empty()
