"""DB-backed upgrade test for the sql/008 resource-group functional-index migration.

The v1.1.10 index (``idx_res_rg_lower``) is a TWIN migration applied unconditionally by
``generate``/``init-db`` (writer.ensure_rg_index_schema), NOT a base-schema object — so a
database provisioned before v1.1.10 must gain the index automatically on the next run,
without touching existing data and without a re-provision. This test proves that upgrade
path end-to-end against a live Postgres:

    pre-v1.1.10 schema (index absent) -> ensure_rg_index_schema -> index present
    -> existing rows unchanged -> a second application is an idempotent no-op.

The whole test runs inside one transaction that is rolled back, so the shared :5433 dev
DB (or the CI Postgres) is left exactly as it was found. Skips cleanly when no Postgres is
reachable (mirrors the ``pg_conn`` fixture used across the generator suite).
"""

from __future__ import annotations

import uuid

from tenantless.generator import writer


def _index_names(conn) -> list[str]:
    rows = conn.execute(
        "SELECT indexname FROM pg_indexes "
        "WHERE schemaname = 'synthetic' AND tablename = 'resources' "
        "  AND indexname = 'idx_res_rg_lower'"
    ).fetchall()
    return [r[0] for r in rows]


def _resource_rows(conn) -> list[tuple]:
    return conn.execute(
        "SELECT id, subscription_id, resource_group_name, name "
        "FROM synthetic.resources ORDER BY id"
    ).fetchall()


def test_rg_index_migration_upgrades_existing_db(pg_conn):
    """A pre-v1.1.10 schema (index dropped) gains ``idx_res_rg_lower`` when the twin
    migration is applied, leaves existing rows untouched, and a re-apply is a no-op."""
    conn = pg_conn
    try:
        # Simulate a database provisioned BEFORE v1.1.10: the base schema exists (the CI
        # `init-db` step / dev volume provisioned it) but the additive index does not.
        conn.execute("DROP INDEX IF EXISTS synthetic.idx_res_rg_lower")
        assert _index_names(conn) == [], (
            "precondition: a pre-v1.1.10 schema has no idx_res_rg_lower"
        )

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
        before = _resource_rows(conn)
        assert before, "the seed resource row must be present before the migration"

        # Run provisioning: the twin migration must CREATE the index.
        applied = writer.ensure_rg_index_schema(conn)
        assert applied is True, "the bundled sql/008 migration must be found and applied"
        assert _index_names(conn) == ["idx_res_rg_lower"], (
            "provisioning must create idx_res_rg_lower on an existing DB"
        )
        assert _resource_rows(conn) == before, (
            "the index migration must not change any existing rows"
        )

        # A SECOND application is an idempotent no-op (CREATE INDEX IF NOT EXISTS): it
        # succeeds, does not raise, and creates no duplicate index.
        applied_again = writer.ensure_rg_index_schema(conn)
        assert applied_again is True, "re-applying the migration must succeed"
        assert _index_names(conn) == ["idx_res_rg_lower"], (
            "re-apply must not create a duplicate index"
        )
        assert _resource_rows(conn) == before, "rows still unchanged after the re-apply"
    finally:
        # Isolate: undo the DROP INDEX, the seed rows, and the CREATE INDEX so the shared
        # database is left exactly as it was found.
        conn.rollback()
