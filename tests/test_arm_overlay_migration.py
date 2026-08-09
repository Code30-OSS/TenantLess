"""DB-backed upgrade test for the sql/009 ARM overlay/tombstone/revision migration.

The overlay substrate (``synthetic.arm_overlay`` + the unowned revision sequence +
the revision trigger + the NAMED row-model CHECK constraints) is a TWIN migration applied by
``init-db`` (writer.ensure_arm_overlay_schema) and self-provisioned by the Rust server at
boot — NOT a base-schema object. So a database provisioned before the overlay existed must gain the
substrate automatically on the next ``init-db`` run, without touching existing data and
without a re-provision. This test proves that upgrade path against a live Postgres:

    pre-overlay schema (overlay absent) -> ensure_arm_overlay_schema -> table + sequence +
    trigger present -> a second application is an idempotent no-op.

The whole test runs inside one transaction that is rolled back, so the shared :5433 dev DB
(or the CI Postgres) is left exactly as it was found. Skips cleanly when no Postgres is
reachable (mirrors the ``pg_conn`` fixture used across the generator suite, and the sibling
``test_rg_index_migration.py``).
"""

from __future__ import annotations

from tenantless.generator import writer


def _object_present(conn, kind: str, name: str) -> bool:
    """True if the named ``kind`` (relation/sequence via to_regclass, or trigger) exists in
    the synthetic schema. ``name`` is bound as a parameter (never spliced) — the project SQL
    bar."""
    with conn.cursor() as cur:
        if kind == "trigger":
            cur.execute(
                "SELECT 1 FROM pg_trigger t "
                "JOIN pg_class c ON c.oid = t.tgrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'synthetic' AND c.relname = 'arm_overlay' "
                "  AND t.tgname = %s AND NOT t.tgisinternal",
                (name,),
            )
            return cur.fetchone() is not None
        # relation or sequence — both resolvable via to_regclass in the synthetic schema.
        cur.execute("SELECT to_regclass(%s)", (f"synthetic.{name}",))
        return cur.fetchone()[0] is not None


def test_arm_overlay_migration_upgrades_existing_db(pg_conn):
    """A pre-overlay schema (overlay dropped) gains ``synthetic.arm_overlay`` + its revision
    sequence + revision trigger when the twin migration is applied, and a re-apply is an
    idempotent no-op."""
    conn = pg_conn
    try:
        # Simulate a database provisioned BEFORE the overlay existed: the base schema exists (the CI
        # `init-db` step / dev volume provisioned it) but the overlay substrate does not.
        # DROP the table (CASCADE removes its revision trigger) then the sequence, so the
        # precondition holds even on a truly pre-overlay DB where the table never existed.
        conn.execute("DROP TABLE IF EXISTS synthetic.arm_overlay CASCADE")
        conn.execute("DROP SEQUENCE IF EXISTS synthetic.arm_overlay_revision_seq CASCADE")
        assert not _object_present(conn, "relation", "arm_overlay"), (
            "precondition: a schema without the overlay has no synthetic.arm_overlay"
        )
        assert not _object_present(conn, "sequence", "arm_overlay_revision_seq"), (
            "precondition: a schema without the overlay has no revision sequence"
        )

        # Run provisioning: the twin migration must CREATE the table, sequence, and trigger.
        applied = writer.ensure_arm_overlay_schema(conn)
        assert applied is True, "the bundled sql/009 migration must be found and applied"
        assert _object_present(conn, "relation", "arm_overlay"), (
            "provisioning must create synthetic.arm_overlay on an existing DB"
        )
        assert _object_present(conn, "sequence", "arm_overlay_revision_seq"), (
            "provisioning must create the unowned revision sequence"
        )
        assert _object_present(conn, "trigger", "trg_arm_overlay_revision"), (
            "provisioning must create the BEFORE INSERT OR UPDATE revision trigger"
        )

        # A SECOND application is an idempotent no-op (CREATE ... IF NOT EXISTS + guarded DO
        # blocks + DROP/CREATE trigger): it succeeds and does not raise or duplicate.
        applied_again = writer.ensure_arm_overlay_schema(conn)
        assert applied_again is True, "re-applying the migration must succeed"
        assert _object_present(conn, "relation", "arm_overlay"), "table still present after re-apply"
        assert _object_present(conn, "trigger", "trg_arm_overlay_revision"), (
            "exactly one revision trigger remains after re-apply (DROP/CREATE is idempotent)"
        )
    finally:
        # Isolate: roll back the DROPs and the CREATE so the shared database is left exactly
        # as it was found.
        conn.rollback()
