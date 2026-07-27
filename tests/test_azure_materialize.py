"""Unit tests for the pure ARG -> scan-schema materializer (Plan 12-02).

Proves the azure-free ``materialize.py`` faithfully adapts ARG ObjectArray rows
into the EXISTING DuckDB scan schema so the proven ``DuckDBReader`` SQL runs
verbatim (Task 1), and that ``open_azure`` single-passes every ARG page into one
in-memory DuckDB table while auto-deriving the in-memory denylist from the
tenant's own identifiers (Task 2) — with zero persistence and zero ``azure-*``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# tests/fixtures is on sys.path via conftest.py (mirrors how Waves 1-2 consume).
from fixtures.azure_rows import (  # noqa: E402
    PLANTED_NAME,
    PLANTED_RG,
    PLANTED_SUB,
    PLANTED_TAGVAL,
    _FakeExecutor,
    synthetic_arg_rows,
)

from tenantless.analyzer.azure import materialize as M  # noqa: E402
from tenantless.analyzer.reader import DuckDBReader  # noqa: E402

_ARG_SHAPE = Path(__file__).resolve().parent / "fixtures" / "arg_shape.json"


def _load_arg_shape_data() -> list[dict]:
    with _ARG_SHAPE.open(encoding="utf-8") as fh:
        return json.load(fh)["data"]


# --------------------------------------------------------------------------- #
# Task 1: normalize + materialize one page into the scan schema
# --------------------------------------------------------------------------- #


def test_normalize_returns_11_tuple_scan_positioned():
    """_normalize maps a camelCase ARG dict to the 11-col resources tuple."""
    row = _load_arg_shape_data()[0]
    t = M._normalize(row)
    assert isinstance(t, tuple)
    assert len(t) == 11
    # scan_id is the constant literal "azure" in position 0.
    assert t[0] == "azure"
    # id -> resource_id, name, type, location, resource_group, subscription_id.
    assert t[1] == row["id"]
    assert t[2] == row["name"]
    assert t[3] == row["type"]
    assert t[4] == row["location"]
    assert t[5] == row["resourceGroup"]
    assert t[6] == row["subscriptionId"]
    # properties / sku / tags are JSON *strings* (dict serialized), not dicts.
    assert isinstance(t[7], str) and json.loads(t[7]) == row["properties"]
    assert isinstance(t[8], str) and json.loads(t[8]) == row["sku"]
    assert isinstance(t[9], str) and json.loads(t[9]) == row["tags"]
    assert t[10] == row["kind"]  # kind is a top-level scalar (None here)


def test_json_or_null_coerces_empty_and_absent_to_none():
    """None and {} coerce to SQL NULL; a populated dict serializes to a string."""
    assert M._json_or_null(None) is None
    assert M._json_or_null({}) is None
    out = M._json_or_null({"a": 1})
    assert isinstance(out, str) and json.loads(out) == {"a": 1}


def test_materialized_page_is_read_verbatim_by_duckdb_reader():
    """arg_shape.json data materializes so DuckDBReader SQL runs unchanged."""
    rows = _load_arg_shape_data()
    conn = M._new_conn()
    try:
        M._insert_page(conn, rows)
        reader = DuckDBReader(conn)
        assert reader.source_stats()["total_resources"] == 1
        rtc = reader.resource_type_counts()
        assert rtc.columns == ["type", "count"]
        assert rtc["type"].to_list() == [rows[0]["type"]]
    finally:
        conn.close()


def test_empty_or_absent_json_columns_materialize_as_sql_null():
    """tags={} and absent properties store as SQL NULL (IS NULL predicates match)."""
    row = {
        "id": "/subscriptions/s/resourceGroups/g/providers/p/x/n",
        "name": "n",
        "type": "p/x",
        "location": "eastus",
        "resourceGroup": "g",
        "subscriptionId": "s",
        "tags": {},  # empty -> NULL
        # properties absent -> NULL
        "sku": {"name": "S"},
        "kind": None,
    }
    conn = M._new_conn()
    try:
        M._insert_page(conn, [row])
        null_tags = conn.execute(
            "SELECT COUNT(*) FROM resources WHERE tags IS NULL"
        ).fetchone()[0]
        null_props = conn.execute(
            "SELECT COUNT(*) FROM resources WHERE properties IS NULL"
        ).fetchone()[0]
        assert null_tags == 1
        assert null_props == 1
    finally:
        conn.close()


def test_resources_insert_binds_parameters_never_splices():
    """The INSERT statement uses bound ? placeholders, no f-string/format/%."""
    sql = M._INSERT_RESOURCES
    assert sql.count("?") == 11
    assert "%" not in sql
    assert "{" not in sql and "}" not in sql
    assert "format" not in sql.lower()


# --------------------------------------------------------------------------- #
# Task 2: single-pass open_azure — paging exhaustion + denylist derivation
# --------------------------------------------------------------------------- #


def test_open_azure_single_passes_all_pages_once_each():
    """A 2-page executor materializes sum(rows); run called exactly twice."""
    page1 = synthetic_arg_rows(3)
    page2 = synthetic_arg_rows(2)

    calls = {"n": 0}

    class CountingExecutor(_FakeExecutor):
        def run(self, query, subscriptions, skip_token):
            calls["n"] += 1
            return super().run(query, subscriptions, skip_token)

    # page1 has a non-None token (must NOT terminate); page2 closes the loop.
    # A trailing empty page answers the RG-enumeration pass (P2-a).
    executor = CountingExecutor([(page1, "t1"), (page2, None), ([], None)])
    with M.open_azure(executor, None) as (reader, derived):
        stats = reader.source_stats()
        assert stats["total_resources"] == len(page1) + len(page2)
    assert calls["n"] == 3  # 2 resource pages + 1 RG-enumeration page


def test_open_azure_derives_all_planted_identifiers():
    """derived_terms contains sub id, RG name, resource name, and tag value."""
    rows = synthetic_arg_rows(4)
    executor = _FakeExecutor([(rows, None), ([], None)])  # +RG-enum page (empty)
    with M.open_azure(executor, None) as (_reader, derived):
        assert {PLANTED_SUB, PLANTED_RG, PLANTED_NAME, PLANTED_TAGVAL} <= derived


def test_open_azure_derives_distinct_subscription_and_rg_counts():
    """source_stats sub/RG counts come from DISTINCT post-loop inserts."""
    rows = synthetic_arg_rows(5)  # all share one sub + one RG
    executor = _FakeExecutor([(rows, None), ([], None)])  # +RG-enum page (empty)
    with M.open_azure(executor, None) as (reader, _derived):
        stats = reader.source_stats()
        assert stats["total_subscriptions"] == 1
        assert stats["total_resource_groups"] == 1
        assert stats["total_resources"] == 5


def test_open_azure_writes_no_temp_file_and_closes_conn(tmp_path, monkeypatch):
    """open_azure persists nothing to disk and closes the connection on exit."""
    # Run with cwd at an empty tmp dir; snapshot before/after to catch any spill.
    monkeypatch.chdir(tmp_path)
    before = set(tmp_path.rglob("*"))

    rows = synthetic_arg_rows(2)
    executor = _FakeExecutor([(rows, None), ([], None)])  # +RG-enum page (empty)
    captured = {}
    with M.open_azure(executor, None) as (reader, _derived):
        captured["conn"] = reader._conn
        assert reader.source_stats()["total_resources"] == 2

    after = set(tmp_path.rglob("*"))
    assert before == after, f"open_azure wrote files: {after - before}"

    # Connection is closed in the finally (a query now raises).
    raised = False
    try:
        captured["conn"].execute("SELECT 1")
    except Exception:
        raised = True
    assert raised, "connection was not closed on context exit"


def test_open_azure_loop_does_not_stop_on_nonnull_first_token():
    """A first page with a non-None token does not truncate the scan."""
    page1 = synthetic_arg_rows(1)
    page2 = synthetic_arg_rows(1)
    page3 = synthetic_arg_rows(1)
    executor = _FakeExecutor(
        [(page1, "t1"), (page2, "t2"), (page3, None), ([], None)]
    )  # +RG-enum page (empty)
    with M.open_azure(executor, None) as (reader, _derived):
        assert reader.source_stats()["total_resources"] == 3


def test_materialize_imports_no_azure_module():
    """Importing materialize pulls no azure* into sys.modules (core install).

    In-process check; skip when the ``azure`` extra is installed (another test's
    importorskip may have loaded it first). The subprocess test in
    ``test_azure_import_isolation.py`` guarantees isolation regardless of venv."""
    import importlib.util

    if importlib.util.find_spec("azure") is not None:
        pytest.skip("azure extra installed; subprocess test covers isolation")
    leaked = [m for m in sys.modules if m == "azure" or m.startswith("azure.")]
    assert not leaked, f"azure-* leaked into sys.modules: {leaked}"


# --------------------------------------------------------------------------- #
# Regression: post-completion review findings (P1-c, P2-a, P2-b)
# --------------------------------------------------------------------------- #


def _row(sub, rg, name, tags):
    """One ARG-shape row with the given scope + tags (other fields generic)."""
    return {
        "id": f"/subscriptions/{sub}/resourceGroups/{rg}/providers/p/x/{name}",
        "name": name,
        "type": "microsoft.compute/virtualmachines",
        "location": "eastus2",
        "resourceGroup": rg,
        "subscriptionId": sub,
        "tags": tags,
        "properties": {"vmSize": "Standard_D2s_v3"},
        "sku": {"name": "Standard_D2s_v3"},
        "kind": None,
    }


def test_open_azure_subtracts_published_enum_value_from_denylist():
    """P1-c: a published governance enum value (Environment=prod) is NOT in the
    denylist even when the SAME token is also a real RG/resource name."""
    # RG literally named "prod" AND an Environment=prod tag -> collision source.
    rows = [_row("sub-a", "prod", f"vm-{i}", {"Environment": "prod"}) for i in range(3)]
    executor = _FakeExecutor([(rows, None), ([], None)])  # +RG-enum page (empty)
    with M.open_azure(executor, None) as (_reader, derived):
        # "prod" is deliberately published (Environment value), so it must be
        # subtracted from the auto-denylist to avoid a false backstop trip.
        assert "prod" not in derived
        # A genuine, non-published identifier is still derived.
        assert "sub-a" in derived


def test_open_azure_counts_resolved_subscription_with_no_resources():
    """P2-a: a resolved subscription holding zero resources is still counted."""
    # Two subs resolved, but only sub-a has any resources in the scan.
    rows = [_row("sub-a", "rg1", f"vm-{i}", {}) for i in range(3)]
    executor = _FakeExecutor([(rows, None), ([], None)])  # +RG-enum page (empty)
    with M.open_azure(executor, ["sub-a", "sub-b"]) as (reader, _derived):
        stats = reader.source_stats()
        # Pre-fix this reported 1 (counted from DISTINCT resources only).
        assert stats["total_subscriptions"] == 2
        assert stats["total_resources"] == 3


def test_open_azure_aborts_on_repeated_continuation_token():
    """P2-b: a repeated continuation token aborts instead of looping forever."""
    page = synthetic_arg_rows(1)
    # Same non-None token handed back twice -> no forward progress.
    executor = _FakeExecutor([(page, "loop-tok"), (page, "loop-tok")])
    with pytest.raises(RuntimeError, match="did not progress"):
        with M.open_azure(executor, None) as (_reader, _derived):
            pass


def test_custom_tag_key_dropped_from_output_and_denylisted():
    """P1-b: a tenant-specific custom tag key is dropped from key_frequencies and
    added (with its value) to the auto-denylist backstop."""
    rows = [
        _row("sub-a", "rg1", f"vm-{i}",
             {"Environment": "prod", "AcmeProjectKey": "acme-team-x"})
        for i in range(3)
    ]
    executor = _FakeExecutor([(rows, None), ([], None)])  # +RG-enum page (empty)
    with M.open_azure(executor, None) as (reader, derived):
        keys = reader.tag_key_counts()["tag_key"].to_list()
        assert "AcmeProjectKey" not in keys  # dropped from materialized tags
        assert "Environment" in keys  # generic key retained
        # The custom key AND its value are in the denylist backstop.
        assert "AcmeProjectKey" in derived
        assert "acme-team-x" in derived


def test_generic_key_allowlist_is_case_insensitive():
    """A generic key in non-canonical casing is still retained (allowlist is CI)."""
    rows = [_row("sub-a", "rg1", f"vm-{i}", {"environment": "prod"}) for i in range(3)]
    executor = _FakeExecutor([(rows, None), ([], None)])  # +RG-enum page (empty)
    with M.open_azure(executor, None) as (reader, _derived):
        keys = reader.tag_key_counts()["tag_key"].to_list()
        assert "environment" in keys  # retained despite lowercase


# --------------------------------------------------------------------------- #
# P2-a: ResourceContainers empty-RG enumeration (count parity with DuckDB)
# --------------------------------------------------------------------------- #


def _rg(sub, name):
    """One ResourceContainers RG-enumeration row."""
    return {"subscriptionId": sub, "name": name}


def test_empty_resource_group_is_counted_and_denylisted():
    """An RG holding ZERO resources is counted via ResourceContainers + denylisted."""
    resources = [_row("sub-a", "rg-busy", f"vm-{i}", {}) for i in range(3)]
    # Enumeration lists the resource-bearing RG AND an empty one.
    rg_page = [_rg("sub-a", "rg-busy"), _rg("sub-a", "rg-empty-fake")]
    executor = _FakeExecutor([(resources, None), (rg_page, None)])
    with M.open_azure(executor, ["sub-a"]) as (reader, derived):
        stats = reader.source_stats()
        # Both RGs counted (the empty one would be invisible without enumeration).
        assert stats["total_resource_groups"] == 2
        assert stats["total_resources"] == 3
        # The empty RG's name is a tenant identifier -> denylist backstop.
        assert "rg-empty-fake" in derived


def test_resource_bearing_rg_not_double_counted_against_enumeration():
    """An RG present in BOTH resources and the enumeration is counted ONCE."""
    resources = [_row("sub-a", "rg-busy", f"vm-{i}", {}) for i in range(4)]
    rg_page = [_rg("sub-a", "rg-busy")]  # same RG the resources live in
    executor = _FakeExecutor([(resources, None), (rg_page, None)])
    with M.open_azure(executor, ["sub-a"]) as (reader, _derived):
        assert reader.source_stats()["total_resource_groups"] == 1


def test_duplicate_enumerated_rgs_counted_once():
    """A repeated (sub, rg) across enumeration pages is deduped (counted once)."""
    resources = [_row("sub-a", "rg1", "vm-0", {})]
    # Two enumeration pages repeating the same RG (sub-a, rg-dup).
    p1 = [_rg("sub-a", "rg-dup")]
    p2 = [_rg("sub-a", "rg-dup")]
    executor = _FakeExecutor([(resources, None), (p1, "t1"), (p2, None)])
    with M.open_azure(executor, ["sub-a"]) as (reader, _derived):
        # rg1 (from resources) + rg-dup (enumerated once) == 2, not 3.
        assert reader.source_stats()["total_resource_groups"] == 2


def test_rg_enumeration_casing_does_not_double_count():
    """A resource RG whose casing differs from the enumerated container name is
    counted ONCE (ARG resourceGroup-vs-container casing quirk)."""
    resources = [_row("sub-a", "RG-Mixed", "vm-0", {})]  # upper-ish casing
    rg_page = [_rg("sub-a", "rg-mixed")]  # container reports lower-ish
    executor = _FakeExecutor([(resources, None), (rg_page, None)])
    with M.open_azure(executor, ["sub-a"]) as (reader, _derived):
        assert reader.source_stats()["total_resource_groups"] == 1


def test_rg_enumeration_aborts_on_repeated_continuation_token():
    """P2-b: the RG-enumeration loop aborts on a repeated continuation token."""
    resources = [_row("sub-a", "rg1", "vm-0", {})]
    rg_page = [_rg("sub-a", "rg1")]
    executor = _FakeExecutor(
        [(resources, None), (rg_page, "loop-tok"), (rg_page, "loop-tok")]
    )
    with pytest.raises(RuntimeError, match="did not progress"):
        with M.open_azure(executor, ["sub-a"]) as (_reader, _derived):
            pass
