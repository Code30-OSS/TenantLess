"""reader.rg_type_sets() must surface TRUE-EMPTY resource groups (RGs that exist
but hold zero resources) by FULL OUTER JOINing the source's resource_groups
table — a resources-only frame can never see an empty RG, so the generator would
model 0% empties for a tenant that has some. Falls back to a resources-only frame
when the source has no resource_groups table. DB-free apart from a tiny in-tmp
DuckDB built here.
"""

from __future__ import annotations

import duckdb

from tenantless.analyzer.reader import open_duckdb

_RES_DDL = """
CREATE TABLE resources (
    resource_id VARCHAR, name VARCHAR, type VARCHAR, location VARCHAR,
    resource_group VARCHAR, subscription_id VARCHAR,
    properties VARCHAR, sku VARCHAR, tags VARCHAR, kind VARCHAR
)
"""
_RG_DDL = """
CREATE TABLE resource_groups (
    scan_id VARCHAR, resource_group_id VARCHAR, name VARCHAR,
    location VARCHAR, subscription_id VARCHAR, tags VARCHAR
)
"""
_RES_ROWS = [
    ("id1", "n1", "Microsoft.Storage/storageAccounts", "eastus", "rg-a", "sub-1", "{}", "{}", "{}", None),
    ("id2", "n2", "Microsoft.KeyVault/vaults", "eastus", "rg-a", "sub-1", "{}", "{}", "{}", None),
]


def _make_db(path, *, with_rg_table: bool, empty_rg: bool) -> None:
    con = duckdb.connect(str(path))
    con.execute(_RES_DDL)
    con.executemany("INSERT INTO resources VALUES (?,?,?,?,?,?,?,?,?,?)", _RES_ROWS)
    if with_rg_table:
        con.execute(_RG_DDL)
        rows = [("s", "rgid-a", "rg-a", "eastus", "sub-1", "{}")]
        if empty_rg:
            rows.append(("s", "rgid-b", "rg-empty", "eastus", "sub-1", "{}"))
        con.executemany("INSERT INTO resource_groups VALUES (?,?,?,?,?,?)", rows)
    con.close()


def test_rg_type_sets_surfaces_true_empty_rgs(tmp_path):
    db = tmp_path / "with_empty.duckdb"
    _make_db(db, with_rg_table=True, empty_rg=True)
    with open_duckdb(str(db)) as reader:
        frame = reader.rg_type_sets()
    assert frame.height == 2  # rg-a (2 resources) + rg-empty (0)
    by_count = {row["resource_count"]: list(row["type_set"]) for row in frame.iter_rows(named=True)}
    assert 0 in by_count and by_count[0] == []  # the empty RG: empty composition
    assert 2 in by_count and len(by_count[2]) == 2  # rg-a: two distinct types


def test_rg_type_sets_falls_back_without_resource_groups_table(tmp_path):
    db = tmp_path / "no_rg_table.duckdb"
    _make_db(db, with_rg_table=False, empty_rg=False)
    with open_duckdb(str(db)) as reader:
        frame = reader.rg_type_sets()
    # Resources-only fallback: only the one RG that has resources, no empty rows.
    assert frame.height == 1
    assert frame["resource_count"].to_list() == [2]
