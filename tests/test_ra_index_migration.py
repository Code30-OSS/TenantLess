"""DB-backed upgrade test for the composite role-assignment keyset index (v1.1.11).

``idx_ra_sub_assignment (subscription_id, assignment_id)`` backs the keyset-paginated
``Microsoft.Authorization/roleAssignments`` read
(``WHERE subscription_id = $1 AND assignment_id > $2 ORDER BY assignment_id LIMIT $3``).
It ships in ``sql/005_identity.sql`` alongside the table's existing ``idx_ra_sub``, and
``sql/005`` is applied UNCONDITIONALLY and idempotently by ``generate``/``init-db`` via
``writer.ensure_identity_schema`` — so a database provisioned before v1.1.11 must gain the
composite index automatically on the next run, without touching existing data and without a
re-provision. This test proves that upgrade path end-to-end against a live Postgres:

    pre-v1.1.11 schema (composite index absent) -> ensure_identity_schema
    -> index present -> existing rows unchanged -> a second application is an idempotent no-op.

The whole test runs inside one transaction that is rolled back, so the shared :5433 dev DB
(or the CI Postgres) is left exactly as it was found. Skips cleanly when no Postgres is
reachable (mirrors the ``pg_conn`` fixture used across the generator suite, and the twin
``test_rg_index_migration.py`` for the sql/008 index).
"""

from __future__ import annotations

import uuid

from tenantless.generator import writer


def _index_names(conn) -> list[str]:
    rows = conn.execute(
        "SELECT indexname FROM pg_indexes "
        "WHERE schemaname = 'synthetic' AND tablename = 'role_assignments' "
        "  AND indexname = 'idx_ra_sub_assignment'"
    ).fetchall()
    return [r[0] for r in rows]


def _assignment_rows(conn) -> list[tuple]:
    return conn.execute(
        "SELECT assignment_id, subscription_id, principal_oid, scope "
        "FROM synthetic.role_assignments ORDER BY assignment_id"
    ).fetchall()


def test_ra_index_migration_upgrades_existing_db(pg_conn):
    """A pre-v1.1.11 schema (composite index dropped) gains ``idx_ra_sub_assignment`` when
    the identity migration is re-applied, leaves existing rows untouched, and a re-apply is
    a no-op."""
    conn = pg_conn
    try:
        # Simulate a database provisioned BEFORE v1.1.11: the identity schema exists (the CI
        # `init-db` step / dev volume applied sql/005) but the additive composite index does
        # not. `idx_ra_sub` (the single-key index) is left in place, as on a real old DB.
        conn.execute("DROP INDEX IF EXISTS synthetic.idx_ra_sub_assignment")
        assert _index_names(conn) == [], (
            "precondition: a pre-v1.1.11 schema has no idx_ra_sub_assignment"
        )

        # Seed a principal -> role_assignment so 'existing rows unchanged' is a meaningful
        # assertion (the FK fk_ra_principal requires the principal; role_assignments carries
        # no FK on subscription_id/scope, so an arbitrary id/text is valid).
        principal_oid = uuid.uuid4()
        conn.execute(
            "INSERT INTO synthetic.principals (oid, principal_type, display_name, app_id) "
            "VALUES (%s, 'ServicePrincipal', NULL, NULL)",
            (principal_oid,),
        )
        assignment_id = uuid.uuid4()
        sub_id = uuid.uuid4()
        conn.execute(
            "INSERT INTO synthetic.role_assignments "
            "(assignment_id, subscription_id, principal_oid, principal_type, "
            " role_definition_id, scope) "
            "VALUES (%s, %s, %s, 'ServicePrincipal', "
            "'/providers/Microsoft.Authorization/roleDefinitions/"
            "8e3af657-bb00-4899-acbc-f0f7f5db61aa', %s)",
            (assignment_id, sub_id, principal_oid, f"/subscriptions/{sub_id}"),
        )
        before = _assignment_rows(conn)
        assert before, "the seed role_assignment row must be present before the migration"

        # Run provisioning: re-applying sql/005 (idempotent) must CREATE the composite index.
        applied = writer.ensure_identity_schema(conn)
        assert applied is True, "the bundled sql/005 migration must be found and applied"
        assert _index_names(conn) == ["idx_ra_sub_assignment"], (
            "provisioning must create idx_ra_sub_assignment on an existing DB"
        )
        assert _assignment_rows(conn) == before, (
            "the index migration must not change any existing rows"
        )

        # A SECOND application is an idempotent no-op (CREATE INDEX IF NOT EXISTS): it
        # succeeds, does not raise, and creates no duplicate index.
        applied_again = writer.ensure_identity_schema(conn)
        assert applied_again is True, "re-applying the migration must succeed"
        assert _index_names(conn) == ["idx_ra_sub_assignment"], (
            "re-apply must not create a duplicate index"
        )
        assert _assignment_rows(conn) == before, "rows still unchanged after the re-apply"
    finally:
        # Isolate: undo the DROP INDEX, the seed rows, and the CREATE INDEX so the shared
        # database is left exactly as it was found.
        conn.rollback()
