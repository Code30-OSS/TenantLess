"""Unit tests for the per-type property/sku shape extractor against the
synthetic CI fixture.

NONE of these tests touch the external real DB.
"""

from __future__ import annotations

import inspect

import pytest

from tenantless.analyzer import privacy
from tenantless.analyzer.extractors import resource_types, type_shapes
from tenantless.analyzer.reader import open_duckdb

from fixtures.build_fixture_duckdb import MIN_BUCKET_SIZE


def _build_rtd_with_shapes(fixture_duckdb, min_bucket_size=MIN_BUCKET_SIZE):
    with open_duckdb(str(fixture_duckdb)) as reader:
        counts = reader.resource_type_counts()
        surviving = privacy.merge_min_buckets(counts, min_bucket_size)
        rtd = resource_types.extract(surviving)
        type_shapes.extract_into(
            rtd,
            property_frame_for=reader.type_property_value_counts,
            sku_frame_for=reader.type_sku_value_counts,
            min_bucket_size=min_bucket_size,
        )
    return rtd


def test_at_most_15_types_carry_shapes(fixture_duckdb):
    """No more than TOP_N (15) types receive non-empty property/sku shapes."""
    rtd = _build_rtd_with_shapes(fixture_duckdb)
    with_shapes = [
        k
        for k, v in rtd.items()
        if v.get("property_distributions") or v.get("sku_distributions")
    ]
    assert len(with_shapes) <= type_shapes.TOP_N


def test_property_value_maps_normalized(fixture_duckdb):
    rtd = _build_rtd_with_shapes(fixture_duckdb)
    found_any = False
    for entry in rtd.values():
        for field, dist in entry.get("property_distributions", {}).items():
            found_any = True
            assert dist
            assert sum(dist.values()) == pytest.approx(1.0, abs=1e-6)
        for field, dist in entry.get("sku_distributions", {}).items():
            assert dist
            assert sum(dist.values()) == pytest.approx(1.0, abs=1e-6)
    assert found_any, "fixture VM/SA resources must yield property shapes"


def test_vm_property_shapes_present_and_enum_only(fixture_duckdb):
    """The dominant VM type carries known enum property fields (vmSize/osType)."""
    rtd = _build_rtd_with_shapes(fixture_duckdb)
    vm_key = resource_types.normalize_type_key(
        "microsoft.compute/virtualmachines"
    )
    assert vm_key in rtd
    props = rtd[vm_key]["property_distributions"]
    # Only allow-listed enum fields appear.
    allowed = type_shapes.PROPERTY_FIELD_ALLOWLIST[vm_key]
    assert set(props.keys()) <= allowed
    assert "vmSize" in props
    sku = rtd[vm_key].get("sku_distributions", {})
    assert set(sku.keys()) <= type_shapes.SKU_FIELD_ALLOWLIST_DEFAULT


def test_shapes_have_no_stray_schema_keys(fixture_duckdb):
    """Each type entry has only schema-permitted keys."""
    rtd = _build_rtd_with_shapes(fixture_duckdb)
    allowed_keys = {"frequency", "property_distributions", "sku_distributions"}
    for entry in rtd.values():
        assert set(entry.keys()) <= allowed_keys
        assert "frequency" in entry
        assert "property_distributions" in entry


def test_no_duckdb_import_in_type_shapes():
    assert "import duckdb" not in inspect.getsource(type_shapes)
