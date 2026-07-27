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

import uuid
from urllib.parse import urlsplit, urlunsplit

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
