"""Isolated-DB test for ``writer.ensure_base_schema`` (Docker-optional / BYO-Postgres).

``ensure_base_schema`` self-provisions the BASE synthetic schema (sql/001 -> 002 ->
003) on a bare PostgreSQL that never saw the docker ``initdb`` mount. sql/001 and
sql/002 use BARE ``CREATE TABLE`` (not ``IF NOT EXISTS``), so the applier is guarded
at the FUNCTION level by ``to_regclass('synthetic.tenant')`` — apply on a bare DB,
harmless no-op on an already-provisioned Docker volume.

CRITICAL: this test provisions its OWN dedicated throwaway database and NEVER
touches the shared :5433 dev tenant (which ``uv run pytest`` truncates — project
memory "pytest truncates the dev tenant"). It mirrors ``conftest.py::pg_conn``'s
skip-on-unavailable idiom, extended with a ``CREATE DATABASE`` / ``DROP DATABASE``
lifecycle so the shared ``tenantless`` DB's ``synthetic.*`` data is untouched.
"""

from __future__ import annotations

import re
import uuid
from urllib.parse import urlsplit, urlunsplit

import click
import pytest

from tenantless.generator import writer as writer_mod

DATABASE_URL = writer_mod.DATABASE_URL


def _swap_database(dsn: str, dbname: str) -> str:
    """Return ``dsn`` with its database path replaced by ``dbname`` (same server)."""
    parts = urlsplit(dsn)
    return urlunsplit(
        (parts.scheme, parts.netloc, "/" + dbname, parts.query, parts.fragment)
    )


@pytest.fixture
def fresh_db_conn():
    """Yield a psycopg connection to a DEDICATED throwaway database, or skip if
    Postgres is unavailable.

    Creates ``tenantless_basetest_<uuid>`` on the same server as ``DATABASE_URL``,
    yields a connection to it, then terminates lingering backends and drops it.
    NEVER touches the shared ``tenantless`` dev tenant (which pytest truncates).
    """
    psycopg = pytest.importorskip("psycopg")
    # dbname is generated here (uuid hex) — never user input — so quoting is safe.
    dbname = f"tenantless_basetest_{uuid.uuid4().hex[:12]}"
    try:
        admin = psycopg.connect(DATABASE_URL, connect_timeout=3, autocommit=True)
    except Exception as exc:  # noqa: BLE001 — any connection failure -> skip
        pytest.skip(f"Postgres on 5433 unavailable: {exc}")
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{dbname}"')
    except Exception as exc:  # noqa: BLE001 — no CREATEDB / server issue -> skip
        admin.close()
        pytest.skip(f"cannot CREATE DATABASE for isolated test: {exc}")

    conn = psycopg.connect(_swap_database(DATABASE_URL, dbname), connect_timeout=3)
    try:
        yield conn
    finally:
        conn.close()
        with admin.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (dbname,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
        admin.close()


def test_ensure_base_schema_applies_on_bare_db(fresh_db_conn):
    """Against a fresh (bare) DB, ensure_base_schema returns True and materialises
    the full base schema: the six sql/001+002 tables plus the sql/003
    fk_resources_subscription constraint."""
    conn = fresh_db_conn
    assert writer_mod.ensure_base_schema(conn) is True
    conn.commit()
    with conn.cursor() as cur:
        for rel in (
            "synthetic.tenant",
            "synthetic.subscriptions",
            "synthetic.resource_groups",
            "synthetic.resources",
            "synthetic.dependencies",
            "synthetic.violations",
        ):
            cur.execute("SELECT to_regclass(%s)", (rel,))
            assert cur.fetchone()[0] is not None, f"{rel} was not created"
        cur.execute(
            "SELECT 1 FROM pg_constraint WHERE conname = 'fk_resources_subscription'"
        )
        assert cur.fetchone() is not None, "fk_resources_subscription missing (sql/003)"


def test_ensure_base_schema_is_idempotent_noop(fresh_db_conn):
    """A SECOND call on the now-provisioned DB returns False (the to_regclass guard
    short-circuits) and raises no 'relation already exists' error."""
    conn = fresh_db_conn
    assert writer_mod.ensure_base_schema(conn) is True
    conn.commit()
    # sql/001+002 are bare CREATE TABLE; a blind re-apply would raise. The guard
    # short-circuits instead -> False, no error.
    assert writer_mod.ensure_base_schema(conn) is False
    conn.commit()


def test_ensure_base_schema_missing_sql_returns_false(fresh_db_conn, monkeypatch, tmp_path):
    """When the bundled sql/ files are absent (installed-package case, no checkout),
    ensure_base_schema returns False without applying anything — mirrors the
    ensure_cost/identity/drift twins' file-not-found behaviour."""
    conn = fresh_db_conn
    monkeypatch.setattr(
        writer_mod,
        "_base_schema_sql_files",
        lambda: [
            tmp_path / "001_synthetic_tenant.sql",
            tmp_path / "002_cross_sub_dependencies.sql",
            tmp_path / "003_integrity_and_index.sql",
        ],
    )
    assert writer_mod.ensure_base_schema(conn) is False
    conn.commit()
    # Nothing was applied: the guard passed (bare DB) but the files were absent.
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('synthetic.tenant')")
        assert cur.fetchone()[0] is None


# --------------------------------------------------------------------------- #
# EXHAUSTIVE base-object inventory + PartialBaseSchemaError
# --------------------------------------------------------------------------- #
def _scan_base_objects() -> set[str]:
    """Derive the object names sql/001+002+003 actually declare, by scanning the
    three base-schema files for every CREATE TABLE / CREATE INDEX / named
    CONSTRAINT. Used to guard the inventory against SQL drift."""
    names: set[str] = set()
    for p in writer_mod._base_schema_sql_files():
        text = p.read_text(encoding="utf-8")
        names |= set(re.findall(r"CREATE TABLE synthetic\.(\w+)", text))
        names |= set(re.findall(r"CREATE INDEX (?:IF NOT EXISTS )?(\w+)", text))
        names |= set(re.findall(r"ADD CONSTRAINT (\w+)", text))
    return names


def test_base_inventory_is_exhaustive():
    """``_BASE_SCHEMA_INVENTORY`` must be the exhaustive 19 objects sql/001-003
    declare — 6 relations + 2 FK constraints + 11 indexes — and its name set must
    equal the set scanned from the SQL, so a future 001-003 object addition fails
    this test loudly instead of silently escaping the completeness check."""
    inv = writer_mod._BASE_SCHEMA_INVENTORY
    assert len(inv) == 19, f"expected 19 base objects, got {len(inv)}: {inv}"
    kinds = [k for k, _ in inv]
    assert kinds.count("relation") == 6, f"expected 6 relations, got {kinds.count('relation')}"
    assert kinds.count("constraint") == 2, f"expected 2 constraints, got {kinds.count('constraint')}"
    assert kinds.count("index") == 11, f"expected 11 indexes, got {kinds.count('index')}"

    inv_names = {n for _, n in inv}
    scanned = _scan_base_objects()
    assert inv_names == scanned, (
        "inventory drift vs sql/001-003:\n"
        f"  only in inventory: {inv_names - scanned}\n"
        f"  only in sql:       {scanned - inv_names}"
    )


class _RecordingConn:
    """DB-free connection double: records every ``execute()`` (the base-file apply)
    so the branch tests can assert whether the 001->003 chain ran."""

    def __init__(self):
        self.executed: list[str] = []

    def execute(self, sql):  # noqa: D401 — mirrors psycopg Connection.execute
        self.executed.append(sql)


def test_ensure_base_schema_complete_is_noop(monkeypatch):
    """All 19 objects present -> no-op (returns False), applies nothing."""
    monkeypatch.setattr(
        writer_mod, "_base_object_present", lambda conn, kind, name: True
    )
    conn = _RecordingConn()
    assert writer_mod.ensure_base_schema(conn) is False
    assert conn.executed == []


def test_ensure_base_schema_bare_applies_chain(monkeypatch):
    """No object present + all base files on disk -> applies 001->002->003 (one
    execute per base file) and returns True."""
    monkeypatch.setattr(
        writer_mod, "_base_object_present", lambda conn, kind, name: False
    )
    conn = _RecordingConn()
    assert writer_mod.ensure_base_schema(conn) is True
    assert len(conn.executed) == 3, (
        f"expected one execute per base file (001,002,003); got {len(conn.executed)}"
    )


def test_ensure_base_schema_bare_missing_files_returns_false(monkeypatch, tmp_path):
    """No object present + bundled sql/ absent (installed-package / docker-initdb
    path) -> returns False, applies nothing."""
    monkeypatch.setattr(
        writer_mod, "_base_object_present", lambda conn, kind, name: False
    )
    monkeypatch.setattr(
        writer_mod,
        "_base_schema_sql_files",
        lambda: [
            tmp_path / "001_synthetic_tenant.sql",
            tmp_path / "002_cross_sub_dependencies.sql",
            tmp_path / "003_integrity_and_index.sql",
        ],
    )
    conn = _RecordingConn()
    assert writer_mod.ensure_base_schema(conn) is False
    assert conn.executed == []


def test_ensure_base_schema_partial_missing_index_raises(monkeypatch):
    """PARTIAL base — everything present EXCEPT one INDEX (idx_viol_type) — raises
    PartialBaseSchemaError naming the missing index, and applies nothing. Exercising
    a missing INDEX (not just a table/constraint) pins that indexes are covered."""

    def present(conn, kind, name):
        return not (kind == "index" and name == "idx_viol_type")

    monkeypatch.setattr(writer_mod, "_base_object_present", present)
    conn = _RecordingConn()
    with pytest.raises(writer_mod.PartialBaseSchemaError) as exc:
        writer_mod.ensure_base_schema(conn)
    assert "idx_viol_type" in str(exc.value), (
        f"the missing index must be named: {exc.value}"
    )
    assert conn.executed == [], "a partial base must apply nothing"


def test_partial_base_schema_error_is_clickexception():
    """PartialBaseSchemaError is a ClickException so BOTH callers (generate,
    init-db) surface it cleanly with zero per-caller code."""
    assert issubclass(writer_mod.PartialBaseSchemaError, click.ClickException)


def test_ensure_base_schema_partial_after_dropped_index_raises(fresh_db_conn):
    """ISOLATED-DB: apply the base once, DROP a real 002 index, then a re-run raises
    PartialBaseSchemaError naming idx_viol_type (the false-complete bug)."""
    conn = fresh_db_conn
    assert writer_mod.ensure_base_schema(conn) is True
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("DROP INDEX synthetic.idx_viol_type")
    conn.commit()
    with pytest.raises(writer_mod.PartialBaseSchemaError) as exc:
        writer_mod.ensure_base_schema(conn)
    assert "idx_viol_type" in str(exc.value)
    conn.rollback()


def test_all_migration_sql_files_lists_seven(monkeypatch):
    """``_all_migration_sql_files`` returns the 3 base + 4 twin migration paths (the
    pre-flight file gate init-db checks before opening any transaction)."""
    files = writer_mod._all_migration_sql_files()
    names = [p.name for p in files]
    assert names == [
        "001_synthetic_tenant.sql",
        "002_cross_sub_dependencies.sql",
        "003_integrity_and_index.sql",
        "004_cost.sql",
        "005_identity.sql",
        "006_drift.sql",
        "007_web_metadata.sql",
    ], names
