"""Canonical ARM-ID identity cutover — CHECK re-derivation, boot order, legacy-index drop.

The cutover moves every stateful identity comparison onto ``synthetic.arm_id_key`` /
``synthetic.ascii_fold`` (sql/011). These proofs pin its migration half:

* ``sql/012`` re-derives ``ck_arm_overlay_id_lower`` from ``lower(id)`` to
  ``synthetic.arm_id_key(id)`` by INSPECTING ``pg_constraint`` — a re-run on an already
  cut-over volume is a NO-OP (the constraint oid is unchanged, never dropped + re-added);
* the cutover is AUDIT-GATED: a divergent overlay row (a stored ``id_lower`` derived by
  locale ``lower()`` that differs from the ASCII-only key) makes both the Python seam and
  the raw migration file (the docker-initdb path) refuse, leaving the CHECK and the stored
  key untouched — identity is never silently rewritten;
* the retained legacy ``lower()`` indexes (``idx_res_lower_id``, ``idx_res_rg_lower``) are
  dropped CONCURRENTLY only after the fold indexes are valid and the audit re-passes, and
  a healthy cut-over volume is still reported as a COMPLETE base schema;
* the boot preflight order is ``011 -> audit -> 012 -> 010`` (source-order proof).

DB-backed tests are marked ``integration`` and each runs in its OWN throwaway database
(``tenantless_test_<hex>``) created on the ``DATABASE_URL`` server — the CHECK conversion is
a one-way migration, so sharing a database between proofs would make them order-dependent.
"""

from __future__ import annotations

import os
import re
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

from tenantless.generator import writer

REPO = Path(__file__).resolve().parents[1]

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgres://tenantless:tenantless_dev@localhost:5433/tenantless",
)

_TENANT = str(uuid.UUID(int=0xC07))
_SUB = str(uuid.UUID(int=0xC0701))
_RG = "rg-cutover"
# A non-ASCII uppercase letter: locale lower() folds it, the ASCII-only key does not.
_DIVERGENT_ID = (
    f"/subscriptions/{_SUB}/resourceGroups/{_RG}/providers/"
    "Microsoft.Storage/storageAccounts/ÀccountZ"
)


def _dsn_for(dbname: str) -> str:
    parts = urlsplit(DATABASE_URL)
    return urlunsplit(parts._replace(path=f"/{dbname}"))


@contextmanager
def _throwaway_database():
    """Create a fresh ``tenantless_test_<hex>`` database, yield its DSN, drop it after."""
    psycopg = pytest.importorskip("psycopg")
    try:
        admin = psycopg.connect(DATABASE_URL, connect_timeout=3, autocommit=True)
    except Exception as exc:  # noqa: BLE001 - any connection failure -> skip
        pytest.skip(f"Postgres unavailable: {exc}")
    name = f"tenantless_test_{uuid.uuid4().hex[:12]}"
    assert name.startswith("tenantless_test_")  # fail-closed: never touch a real DB
    try:
        admin.execute(f'CREATE DATABASE "{name}"')
        try:
            yield _dsn_for(name)
        finally:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
    finally:
        admin.close()


@pytest.fixture
def fresh_dsn():
    with _throwaway_database() as dsn:
        yield dsn


def _connect(dsn: str):
    import psycopg

    return psycopg.connect(dsn, connect_timeout=3, autocommit=True)


def _pre_cutover_volume(conn) -> None:
    """An upgraded volume BEFORE the cutover: base + overlay (lower() CHECK) + fold fns,
    plus the retained sql/008 RG-name ``lower()`` index an older release provisioned."""
    writer.ensure_base_schema(conn)
    writer.ensure_drift_schema(conn)
    conn.execute((REPO / "sql" / "008_rg_lower_index.sql").read_text(encoding="utf-8"))
    writer.ensure_arm_overlay_schema(conn)
    writer.ensure_arm_id_key_schema(conn)


def _id_check(conn) -> tuple[int, str]:
    row = conn.execute(
        "SELECT oid::int8, pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = 'synthetic.arm_overlay'::regclass "
        "AND contype = 'c' AND conname = 'ck_arm_overlay_id_lower'"
    ).fetchone()
    assert row is not None, "ck_arm_overlay_id_lower must exist"
    return int(row[0]), row[1]


def _index_exists(conn, name: str) -> bool:
    return conn.execute("SELECT to_regclass(%s)", (f"synthetic.{name}",)).fetchone()[0] is not None


def _seed_parents(conn) -> None:
    conn.execute(
        "INSERT INTO synthetic.tenant (tenant_id, display_name, profile_version, scale_params) "
        "VALUES (%s, 't', 'v0', '{}'::jsonb)",
        (_TENANT,),
    )
    conn.execute(
        "INSERT INTO synthetic.subscriptions (subscription_id, tenant_id, display_name, archetype) "
        "VALUES (%s, %s, 'sub', 'test')",
        (_SUB, _TENANT),
    )


def _insert_divergent_overlay_row(conn) -> None:
    # Valid under the OLD CHECK (id_lower = lower(id)), divergent from arm_id_key(id).
    conn.execute(
        "INSERT INTO synthetic.arm_overlay (id_lower, id, target_kind, source, present, body) "
        "VALUES (lower(%s), %s, 'resource', 'user', true, jsonb_build_object("
        "'id', %s::text, 'name', 'n', 'type', 'Microsoft.Storage/storageAccounts', "
        "'location', 'eastus', 'tags', '{}'::jsonb, 'properties', '{}'::jsonb))",
        (_DIVERGENT_ID, _DIVERGENT_ID, _DIVERGENT_ID),
    )


# --------------------------------------------------------------------------- #
# CHECK re-derivation (sql/012 via the Python seam)
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_cutover_rederives_check_and_rerun_is_noop(fresh_dsn):
    with _connect(fresh_dsn) as conn:
        # Given a pre-cutover volume
        _pre_cutover_volume(conn)
        _, before = _id_check(conn)
        assert "lower(id)" in before
        # When the cutover seam runs
        assert writer.ensure_arm_id_identity_cutover_schema(conn) is True
        # Then the CHECK derives from arm_id_key
        oid1, after = _id_check(conn)
        assert "arm_id_key(id)" in after, after
        # And a re-run is a NO-OP (same constraint oid, same definition)
        assert writer.ensure_arm_id_identity_cutover_schema(conn) is True
        oid2, again = _id_check(conn)
        assert (oid1, after) == (oid2, again)
        # And the resolver still provisions on top of the cut-over overlay
        assert writer.ensure_arm_resolver_schema(conn) is True


@pytest.mark.integration
def test_cutover_refuses_divergent_overlay_and_never_rewrites_identity(fresh_dsn):
    with _connect(fresh_dsn) as conn:
        _pre_cutover_volume(conn)
        _insert_divergent_overlay_row(conn)
        # When the cutover seam runs, the audit trips first, naming only the id
        with pytest.raises(writer.ArmIdIdentityAuditError) as exc:
            writer.ensure_arm_id_identity_cutover_schema(conn)
        assert _DIVERGENT_ID in str(exc.value)
        # Then the CHECK and the stored key are untouched
        _, def_ = _id_check(conn)
        assert "lower(id)" in def_
        stored = conn.execute("SELECT id_lower FROM synthetic.arm_overlay").fetchone()[0]
        assert stored.endswith("/àccountz"), stored


@pytest.mark.integration
def test_raw_migration_file_refuses_divergent_overlay(fresh_dsn):
    """The docker-initdb path runs sql/012 with no Python audit in front of it — the file
    itself must refuse a divergent overlay rather than rewrite or merge identity."""
    import psycopg

    with _connect(fresh_dsn) as conn:
        _pre_cutover_volume(conn)
        _insert_divergent_overlay_row(conn)
        sql = (REPO / "sql" / "012_arm_id_identity_cutover.sql").read_text(encoding="utf-8")
        with pytest.raises(psycopg.Error):
            conn.execute(sql)
        _, def_ = _id_check(conn)
        assert "lower(id)" in def_


@pytest.mark.integration
def test_raw_migration_file_is_noop_without_overlay(fresh_dsn):
    """A volume without the overlay table (a bare generate) is left alone by sql/012."""
    with _connect(fresh_dsn) as conn:
        writer.ensure_base_schema(conn)
        writer.ensure_arm_id_key_schema(conn)
        conn.execute((REPO / "sql" / "012_arm_id_identity_cutover.sql").read_text(encoding="utf-8"))
        assert conn.execute("SELECT to_regclass('synthetic.arm_overlay')").fetchone()[0] is None


@pytest.mark.integration
def test_cutover_then_non_ascii_key_is_accepted_and_locale_key_rejected(fresh_dsn):
    import psycopg

    with _connect(fresh_dsn) as conn:
        _pre_cutover_volume(conn)
        writer.ensure_arm_id_identity_cutover_schema(conn)
        # The ASCII-only key is now the ONLY valid derivation...
        conn.execute(
            "INSERT INTO synthetic.arm_overlay (id_lower, id, target_kind, source, present, body) "
            "VALUES (synthetic.arm_id_key(%s), %s, 'resource', 'user', false, NULL)",
            (_DIVERGENT_ID, _DIVERGENT_ID),
        )
        # ...and a locale-lower() key for a non-ASCII id is rejected by the CHECK.
        other = _DIVERGENT_ID.replace("ccountZ", "ccountQ")
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO synthetic.arm_overlay "
                "(id_lower, id, target_kind, source, present, body) "
                "VALUES (lower(%s), %s, 'resource', 'user', false, NULL)",
                (other, other),
            )


# --------------------------------------------------------------------------- #
# Legacy lower() index drop (audit-gated, CONCURRENTLY, outside any boot tx)
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_index_cutover_drops_legacy_lower_indexes(fresh_dsn):
    with _connect(fresh_dsn) as conn:
        _pre_cutover_volume(conn)
        assert _index_exists(conn, "idx_res_lower_id")
        assert _index_exists(conn, "idx_res_rg_lower")
    # When the provisioning seam builds the fold indexes
    assert writer.build_arm_id_key_indexes_concurrently(fresh_dsn) is True
    with _connect(fresh_dsn) as conn:
        # Then the fold indexes exist and the legacy lower() indexes are gone
        assert _index_exists(conn, "idx_res_arm_id_key")
        assert _index_exists(conn, "idx_res_rg_ascii_fold")
        assert not _index_exists(conn, "idx_res_lower_id")
        assert not _index_exists(conn, "idx_res_rg_lower")
        # And the cut-over volume is still a COMPLETE base schema (no partial-schema error)
        assert writer.ensure_base_schema(conn) is False
        # And the RG-index twin no longer recreates the legacy lower() index
        writer.ensure_rg_index_schema(conn)
        assert not _index_exists(conn, "idx_res_rg_lower")
    # And a re-run is idempotent
    assert writer.build_arm_id_key_indexes_concurrently(fresh_dsn) is True


@pytest.mark.integration
def test_index_cutover_refuses_to_drop_when_audit_fails(fresh_dsn):
    with _connect(fresh_dsn) as conn:
        _pre_cutover_volume(conn)
        _seed_parents(conn)
        conn.execute(
            "INSERT INTO synthetic.resources "
            "(id, subscription_id, resource_group_name, name, type, location) "
            "VALUES (%s, %s, %s, 'n', 'x', 'eastus')",
            (_DIVERGENT_ID, _SUB, _RG),
        )
    with pytest.raises(writer.ArmIdIdentityAuditError) as exc:
        writer.build_arm_id_key_indexes_concurrently(fresh_dsn)
    assert _DIVERGENT_ID in str(exc.value)
    with _connect(fresh_dsn) as conn:
        assert _index_exists(conn, "idx_res_lower_id"), "legacy index retained on audit failure"
        assert _index_exists(conn, "idx_res_rg_lower"), "legacy index retained on audit failure"


# --------------------------------------------------------------------------- #
# DB-free structure proofs
# --------------------------------------------------------------------------- #


def test_migration_file_is_pg_constraint_conditional_and_never_rewrites_identity():
    sql = (REPO / "sql" / "012_arm_id_identity_cutover.sql").read_text(encoding="utf-8")
    code = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    assert "pg_constraint" in code and "pg_get_constraintdef" in code
    assert "duplicate_object" not in code, "no EXCEPTION WHEN duplicate_object guard"
    assert not re.search(r"\bUPDATE\b", code, re.IGNORECASE), "identity is never rewritten"
    assert "synthetic.arm_id_key(id)" in code
    # sql/009's live CHECK stays as shipped (the derivation change lives ONLY in 012).
    sql009 = (REPO / "sql" / "009_arm_overlay.sql").read_text(encoding="utf-8")
    assert "CHECK (id_lower = lower(id))" in sql009


def test_migration_chain_orders_cutover_between_fold_functions_and_resolver():
    names = [p.name for p in writer._all_migration_sql_files()]
    assert names.index("011_arm_id_key.sql") < names.index(
        "012_arm_id_identity_cutover.sql"
    ) < names.index("010_arm_resolver.sql"), names


def test_boot_preflight_order_is_fold_audit_cutover_resolver():
    src = (REPO / "mock-server" / "src" / "main.rs").read_text(encoding="utf-8")
    calls = [
        "ensure_arm_overlay_schema(&pool)",
        "ensure_arm_id_key_schema(&pool)",
        "audit_arm_id_identity(&pool)",
        "ensure_arm_id_identity_cutover_schema(&pool)",
        "ensure_arm_resolver_schema(&pool)",
    ]
    positions = [src.find(c) for c in calls]
    assert all(p >= 0 for p in positions), dict(zip(calls, positions))
    assert positions == sorted(positions), dict(zip(calls, positions))


def test_base_inventory_no_longer_requires_the_legacy_lower_id_index():
    assert ("index", "idx_res_lower_id") not in writer._BASE_SCHEMA_INVENTORY
