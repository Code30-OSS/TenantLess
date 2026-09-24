"""Concurrent fold (re)definitions serialize instead of failing.

``CREATE OR REPLACE FUNCTION`` rewrites the ``pg_proc`` row even for an identical body, so
two sessions redefining ``synthetic.ascii_fold`` / ``synthetic.arm_id_key`` at once used
to fail the second with ``tuple concurrently updated``. Every provisioning path now takes
one shared transaction-scoped advisory lock before any fold (re)definition — sql/011 and
the sql/010 prelude, in both the Rust boot and these Python twins — so the second session
waits and then succeeds.

Runs in its own scratch database (created and dropped here) on the server
``DATABASE_URL`` points at. DB-backed + marked ``integration`` (live PG16).
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

import pytest
from click.testing import CliRunner

from tenantless.cli import main
from tenantless.generator import writer
from tenantless._resources import resource_path

pytestmark = pytest.mark.integration

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgres://tenantless:tenantless_dev@localhost:5433/tenantless",
)


@pytest.fixture
def scratch_url():
    psycopg = pytest.importorskip("psycopg")
    try:
        admin = psycopg.connect(DATABASE_URL, connect_timeout=3, autocommit=True)
    except Exception as exc:  # noqa: BLE001 - any connection failure -> skip
        pytest.skip(f"Postgres unavailable: {exc}")
    name = f"tl_foldlock_{uuid.uuid4().hex[:10]}"
    try:
        admin.execute(f'CREATE DATABASE "{name}"')
        url = urlunsplit(urlsplit(DATABASE_URL)._replace(path=f"/{name}"))
        result = CliRunner().invoke(main, ["init-db", "--database-url", url])
        assert result.exit_code == 0, result.output
        yield url
    finally:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


def _run(url: str, ensure, errors: list) -> None:
    import psycopg

    try:
        with psycopg.connect(url) as conn:
            ensure(conn)
            conn.commit()
    except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
        errors.append(f"{ensure.__name__}: {type(exc).__name__}: {exc}")


def test_concurrent_fold_redefinitions_serialize(scratch_url):
    import psycopg

    # Given a session mid-way through redefining the fold functions (lock, then redefine)
    holder = psycopg.connect(scratch_url)
    holder.execute("SELECT pg_advisory_xact_lock(hashtext('synthetic.arm_id_fold'))")
    holder.execute(resource_path("sql", "011_arm_id_key.sql").read_text(encoding="utf-8"))

    # When the 011 twin and the 010 twin (whose prelude redefines them) race it
    errors: list[str] = []
    threads = [
        threading.Thread(target=_run, args=(scratch_url, ensure, errors))
        for ensure in (writer.ensure_arm_id_key_schema, writer.ensure_arm_resolver_schema)
    ]
    for t in threads:
        t.start()
    time.sleep(0.8)
    holder.commit()
    holder.close()
    for t in threads:
        t.join(timeout=60)

    # Then both waited for it and succeeded
    assert not any(t.is_alive() for t in threads), "a redefinition never finished"
    assert errors == []
