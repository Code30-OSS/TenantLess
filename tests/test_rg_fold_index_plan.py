"""EXPLAIN coverage proof: the RG-scoped resolved read is served by ``idx_res_rg_ascii_fold``.

The RG-name predicate of the RG-scoped resource list (``resources.rs``) and the RG-scoped
Cost Management query (``cost.rs``) compares ``synthetic.ascii_fold(resource_group_name) =
synthetic.ascii_fold($4)`` — the canonical identity-component fold. After the cutover the
legacy ``idx_res_rg_lower`` (``lower(resource_group_name)``) index is DROPPED, so the fold
predicate must be index-covered by ``idx_res_rg_ascii_fold``
``(subscription_id, synthetic.ascii_fold(resource_group_name), id)`` or the scoped listing
would degrade to scanning every resource in the subscription.

The live proof EXPLAINs the production query shape through ``synthetic.arm_resolved_resources``
with sequential scans disabled for the session: if the fold expression did NOT match the
index expression the planner could not use the index at all (it would still pick a seq scan),
so an ``Index Name == idx_res_rg_ascii_fold`` node proves the predicate is index-covered.
Runs in its own throwaway database on the ``DATABASE_URL`` server (marked ``integration``).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from tenantless.generator import writer

# tests/ is on sys.path (conftest) — reuse the throwaway-database helpers.
from test_arm_id_cutover import _connect, _throwaway_database  # noqa: E402

REPO = Path(__file__).resolve().parents[1]

# The production RG-scoped list shape (resources.rs list_rg_resources), values bound.
_RG_SCOPED_SQL = (
    "SELECT id, name, type, location, tags, sku, kind, properties "
    "FROM synthetic.arm_resolved_resources "
    "WHERE subscription_id = %s::uuid AND (%s::text IS NULL OR id > %s) "
    "AND synthetic.ascii_fold(resource_group_name) = synthetic.ascii_fold(%s) "
    "ORDER BY id LIMIT 50"
)

_TENANT = str(uuid.UUID(int=0xF01D))
_SUB = str(uuid.UUID(int=0xF01D0001))


def _index_names(plan) -> set[str]:
    found: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            if "Index Name" in node:
                found.add(node["Index Name"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(plan)
    return found


def test_production_rg_predicates_use_the_fold():
    """The EXPLAINed shape above IS the production predicate (source coupling)."""
    resources_rs = (REPO / "mock-server/src/handlers/resources.rs").read_text(encoding="utf-8")
    cost_rs = (REPO / "mock-server/src/handlers/cost.rs").read_text(encoding="utf-8")
    assert (
        "synthetic.ascii_fold(resource_group_name) = synthetic.ascii_fold($4)" in resources_rs
    )
    assert "synthetic.ascii_fold(r.resource_group_name) = synthetic.ascii_fold($4)" in cost_rs
    assert "lower(resource_group_name)" not in resources_rs.split("#[cfg(test)]")[0]
    assert "lower(r.resource_group_name)" not in cost_rs


@pytest.mark.integration
def test_rg_scoped_read_is_covered_by_the_fold_index():
    with _throwaway_database() as dsn:
        with _connect(dsn) as conn:
            # Given a provisioned volume (full migration chain) with resources in two RGs
            writer.ensure_base_schema(conn)
            writer.ensure_drift_schema(conn)
            writer.ensure_rg_index_schema(conn)
            writer.ensure_arm_overlay_schema(conn)
            writer.ensure_arm_id_key_schema(conn)
            writer.ensure_arm_id_identity_cutover_schema(conn)
            writer.ensure_arm_resolver_schema(conn)
            conn.execute(
                "INSERT INTO synthetic.tenant "
                "(tenant_id, display_name, profile_version, scale_params) "
                "VALUES (%s, 't', 'v0', '{}'::jsonb)",
                (_TENANT,),
            )
            conn.execute(
                "INSERT INTO synthetic.subscriptions "
                "(subscription_id, tenant_id, display_name, archetype) "
                "VALUES (%s, %s, 'sub', 'test')",
                (_SUB, _TENANT),
            )
            # Many RGs in ONE subscription, so the subscription-only index is unselective and
            # the (subscription_id, ascii_fold(rg), id) index is the planner's natural choice.
            rgs = ["Rg-Alpha"] + [f"rg-other-{n:02d}" for n in range(39)]
            with conn.cursor() as cur:
                for rg in rgs:
                    for i in range(50):
                        rid = (
                            f"/subscriptions/{_SUB}/resourceGroups/{rg}/providers/"
                            f"Microsoft.Storage/storageAccounts/acct{i:04d}"
                        )
                        cur.execute(
                            "INSERT INTO synthetic.resources "
                            "(id, subscription_id, resource_group_name, name, type, location) "
                            "VALUES (%s, %s, %s, %s, 'Microsoft.Storage/storageAccounts', "
                            "'eastus')",
                            (rid, _SUB, rg, f"acct{i:04d}"),
                        )
        # When the provisioning seam cuts the indexes over
        writer.build_arm_id_key_indexes_concurrently(dsn)
        with _connect(dsn) as conn:
            assert (
                conn.execute("SELECT to_regclass('synthetic.idx_res_rg_lower')").fetchone()[0]
                is None
            ), "the legacy lower() RG index must be gone, so it cannot mask the proof"
            conn.execute("ANALYZE synthetic.resources")
            conn.execute("ANALYZE synthetic.arm_overlay")
            conn.execute("SET enable_seqscan = off")
            row = conn.execute(
                "EXPLAIN (FORMAT JSON) " + _RG_SCOPED_SQL, (_SUB, None, None, "RG-ALPHA")
            ).fetchone()
            plan = row[0] if not isinstance(row[0], str) else json.loads(row[0])
            # Then the fold predicate is served by the fold index
            assert "idx_res_rg_ascii_fold" in _index_names(plan), json.dumps(plan)[:2000]
            # And the ASCII-case variant of the RG name resolves exactly that RG's 50 rows
            n = conn.execute(
                "SELECT count(*) FROM (" + _RG_SCOPED_SQL.replace("LIMIT 50", "") + ") s",
                (_SUB, None, None, "RG-ALPHA"),
            ).fetchone()[0]
            assert n == 50


# An earlier release's fold definitions (translate(), no PARALLEL marking) — the shape an
# already-migrated volume carries before this release's sql/011 re-defines them at boot.
_PREVIOUS_FOLD_SQL = """
CREATE OR REPLACE FUNCTION synthetic.ascii_fold(t text) RETURNS text
    LANGUAGE sql IMMUTABLE STRICT
    AS $$ SELECT translate($1, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz') $$;
CREATE OR REPLACE FUNCTION synthetic.arm_id_key(id text) RETURNS text
    LANGUAGE sql IMMUTABLE STRICT
    AS $$ SELECT synthetic.ascii_fold($1) $$;
"""

_DETAIL_SQL = (
    "SELECT id FROM synthetic.arm_resolved_resources "
    "WHERE synthetic.arm_id_key(id) = synthetic.arm_id_key(%s)"
)


def _plan(conn, sql: str, params=()):
    row = conn.execute("EXPLAIN (FORMAT JSON) " + sql, params).fetchone()
    return row[0] if not isinstance(row[0], str) else json.loads(row[0])


def _node_types(plan) -> set[str]:
    found: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            if "Node Type" in node:
                found.add(node["Node Type"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(plan)
    return found


@pytest.mark.integration
def test_fold_redefinition_on_an_indexed_volume_keeps_index_matching_and_goes_parallel():
    """Upgrade path: the fold indexes were built under the previous definitions; re-applying
    sql/011 (the boot / init-db ensure path) swaps the body in place. The existing expression
    indexes must still match the fold predicates, the values they hold must equal the new
    fold, and the resolved aggregate must now be eligible for a parallel plan."""
    rid_fmt = "/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/sites/Site{i:03d}"
    with _throwaway_database() as dsn:
        with _connect(dsn) as conn:
            # Given a volume provisioned under the previous fold definitions
            writer.ensure_base_schema(conn)
            writer.ensure_drift_schema(conn)
            writer.ensure_arm_overlay_schema(conn)
            writer.ensure_arm_id_key_schema(conn)
            conn.execute(_PREVIOUS_FOLD_SQL)
            writer.ensure_arm_id_identity_cutover_schema(conn)
            writer.ensure_arm_resolver_schema(conn)
            conn.execute(_PREVIOUS_FOLD_SQL)
            conn.execute(
                "INSERT INTO synthetic.tenant "
                "(tenant_id, display_name, profile_version, scale_params) "
                "VALUES (%s, 't', 'v0', '{}'::jsonb)",
                (_TENANT,),
            )
            conn.execute(
                "INSERT INTO synthetic.subscriptions "
                "(subscription_id, tenant_id, display_name, archetype) "
                "VALUES (%s, %s, 'sub', 'test')",
                (_SUB, _TENANT),
            )
            with conn.cursor() as cur:
                for n in range(40):
                    rg = f"Rg-{n:02d}"
                    for i in range(50):
                        cur.execute(
                            "INSERT INTO synthetic.resources "
                            "(id, subscription_id, resource_group_name, name, type, location) "
                            "VALUES (%s, %s, %s, %s, 'Microsoft.Web/sites', 'eastus')",
                            (rid_fmt.format(sub=_SUB, rg=rg, i=i), _SUB, rg, f"Site{i:03d}"),
                        )
        # And the fold indexes built under those previous definitions
        writer.build_arm_id_key_indexes_concurrently(dsn)

        # When the next boot re-applies this release's fold definitions
        with _connect(dsn) as conn:
            writer.ensure_arm_id_key_schema(conn)
            writer.ensure_arm_resolver_schema(conn)

        with _connect(dsn) as conn:
            conn.execute("ANALYZE synthetic.resources")
            conn.execute("ANALYZE synthetic.arm_overlay")
            # Then both functions are PARALLEL SAFE
            par = dict(
                conn.execute(
                    "SELECT p.proname, p.proparallel FROM pg_proc p "
                    "JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE n.nspname = 'synthetic' "
                    "AND p.proname IN ('ascii_fold', 'arm_id_key')"
                ).fetchall()
            )
            assert par == {"arm_id_key": "s", "ascii_fold": "s"}, par
            # And the stored index values equal the new fold for every row
            stale = conn.execute(
                "SELECT count(*) FROM synthetic.resources "
                "WHERE synthetic.arm_id_key(id) IS DISTINCT FROM "
                "translate(id, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz') "
                "OR synthetic.ascii_fold(resource_group_name) IS DISTINCT FROM "
                "translate(resource_group_name, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                "'abcdefghijklmnopqrstuvwxyz')"
            ).fetchone()[0]
            assert stale == 0
            # And the existing expression indexes still match the fold predicates
            conn.execute("SET enable_seqscan = off")
            probe = rid_fmt.format(sub=_SUB, rg="Rg-07", i=3).upper()
            detail = _plan(conn, _DETAIL_SQL, (probe,))
            assert "idx_res_arm_id_key" in _index_names(detail), json.dumps(detail)[:2000]
            rg_list = _plan(conn, _RG_SCOPED_SQL, (_SUB, None, None, "RG-07"))
            assert "idx_res_rg_ascii_fold" in _index_names(rg_list), json.dumps(rg_list)[:2000]
            hit = conn.execute(_DETAIL_SQL, (probe,)).fetchall()
            assert len(hit) == 1
            # And the unbounded resolved aggregate is now eligible for a parallel plan
            conn.execute("RESET enable_seqscan")
            for knob in (
                "parallel_setup_cost = 0",
                "parallel_tuple_cost = 0",
                "min_parallel_table_scan_size = 0",
                "min_parallel_index_scan_size = 0",
                "max_parallel_workers_per_gather = 2",
            ):
                conn.execute(f"SET {knob}")
            agg = _plan(conn, "SELECT count(*) FROM synthetic.arm_resolved_resources")
            assert _node_types(agg) & {"Gather", "Gather Merge"}, json.dumps(agg)[:2000]
