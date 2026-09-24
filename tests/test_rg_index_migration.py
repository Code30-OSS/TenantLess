"""DB-backed proof that the resource-group index seam no longer resurrects the legacy
``lower(resource_group_name)`` index.

``writer.ensure_rg_index_schema`` used to (re)apply sql/008, creating ``idx_res_rg_lower``.
The RG-scoped predicates now compare the canonical identity-component fold
``synthetic.ascii_fold(resource_group_name)``, served by ``idx_res_rg_ascii_fold`` — built
CONCURRENTLY by ``writer.build_arm_id_key_indexes_concurrently`` (which also drops the legacy
index once the identity audit passes). So the seam that ``generate`` / ``init-db`` run inside
their writer transaction must apply NO DDL: re-creating the legacy index there would be a
plain (non-concurrent) build only for the cutover to drop it again. This test proves that
against a live Postgres:

    legacy index present -> seam -> index set unchanged, rows unchanged
    legacy index absent  -> seam -> still absent (never resurrected), rows unchanged

The whole test runs inside one transaction that is rolled back, so the shared database (or
the CI Postgres) is left exactly as it was found. Skips cleanly when no Postgres is reachable.
"""

from __future__ import annotations

import uuid

from tenantless.generator import writer


def _resource_index_names(conn) -> list[str]:
    rows = conn.execute(
        "SELECT indexname FROM pg_indexes "
        "WHERE schemaname = 'synthetic' AND tablename = 'resources' ORDER BY indexname"
    ).fetchall()
    return [r[0] for r in rows]


def _resource_rows(conn) -> list[tuple]:
    return conn.execute(
        "SELECT id, subscription_id, resource_group_name, name "
        "FROM synthetic.resources ORDER BY id"
    ).fetchall()


def test_rg_index_seam_applies_no_ddl_and_never_resurrects_the_lower_index(pg_conn):
    conn = pg_conn
    try:
        # Seed a tenant -> subscription -> resource so 'existing rows unchanged' is a
        # meaningful assertion (the FK fk_resources_subscription requires the parents).
        tenant_id = uuid.uuid4()
        sub_id = uuid.uuid4()
        conn.execute(
            "INSERT INTO synthetic.tenant "
            "(tenant_id, display_name, profile_version, scale_params) "
            "VALUES (%s, 'upgrade-test', '1.0', '{}')",
            (tenant_id,),
        )
        conn.execute(
            "INSERT INTO synthetic.subscriptions "
            "(subscription_id, tenant_id, display_name, archetype) "
            "VALUES (%s, %s, 'sub', 'general')",
            (sub_id, tenant_id),
        )
        res_id = (
            f"/subscriptions/{sub_id}/resourceGroups/Rg-UpgradeTest"
            "/providers/Microsoft.Storage/storageAccounts/res-upgrade"
        )
        conn.execute(
            "INSERT INTO synthetic.resources "
            "(id, subscription_id, resource_group_name, name, type, location) "
            "VALUES (%s, %s, 'Rg-UpgradeTest', 'res-upgrade', "
            "'Microsoft.Storage/storageAccounts', 'eastus')",
            (res_id, sub_id),
        )
        before_rows = _resource_rows(conn)

        # Given whatever index set the volume has, the seam changes NOTHING.
        before_idx = _resource_index_names(conn)
        assert writer.ensure_rg_index_schema(conn) is True, "bundled sql/008 is present"
        assert _resource_index_names(conn) == before_idx, "the seam must apply no DDL"

        # Given a cut-over volume (legacy lower() RG index gone), the seam never recreates it.
        conn.execute("DROP INDEX IF EXISTS synthetic.idx_res_rg_lower")
        assert writer.ensure_rg_index_schema(conn) is True
        assert "idx_res_rg_lower" not in _resource_index_names(conn), (
            "the legacy lower() RG index must never be resurrected"
        )
        assert _resource_rows(conn) == before_rows, "no existing row may change"
    finally:
        # Isolate: undo the DROP INDEX and the seed rows.
        conn.rollback()
