"""Shared pytest fixtures for the analyzer test suite.

The ``fixture_duckdb`` fixture builds a tiny synthetic DuckDB (via
``tests/fixtures/build_fixture_duckdb.py``) in a tmp path with KNOWN
distributions, so every analyzer test runs in CI WITHOUT touching the external
real scanner.duckdb.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import orjson
import pytest

# Make tests/fixtures importable as a package regardless of rootdir.
sys.path.insert(0, str(Path(__file__).parent))

from fixtures.build_fixture_duckdb import build_fixture  # noqa: E402

# profiles/test-small.json relative to the repo root:
# tests/conftest.py -> parents[1] == repo root
_TEST_SMALL_PROFILE = (
    Path(__file__).resolve().parents[1] / "profiles" / "test-small.json"
)


@pytest.fixture
def fixture_duckdb(tmp_path) -> Path:
    """Build the synthetic CI fixture DuckDB in a tmp path; return its path."""
    db_path = tmp_path / "fixture.duckdb"
    build_fixture(db_path)
    return db_path


@pytest.fixture
def generator_profile() -> dict:
    """The hardcoded dev profile (profiles/test-small.json) as a dict.

    Fast, DB-free fixture for the generator unit tests (GEN-01..08, D-01).
    Mirrors how ``generator.profile_input.load_profile`` reads it
    (``orjson.loads`` of the raw bytes).
    """
    return orjson.loads(_TEST_SMALL_PROFILE.read_bytes())


# Postgres connection string for the analyzer test DB on :5433. Mirrors the
# generator's pg_conn fixture (tests/test_generator_copy.py) so DB-less CI skips.
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgres://tenantless:tenantless_dev@localhost:5433/tenantless",
)


@pytest.fixture
def pg_conn():
    """Yield a live psycopg connection, or skip if Postgres is unavailable.

    Verbatim mirror of ``tests/test_generator_copy.py::pg_conn`` so analyzer
    tests share the one Docker-skip pattern (STATE.md: "DB-less CI skips clean").
    """
    psycopg = pytest.importorskip("psycopg")
    try:
        conn = psycopg.connect(DATABASE_URL, connect_timeout=3)
    except Exception as exc:  # noqa: BLE001 - any connection failure → skip
        pytest.skip(f"Postgres on 5433 unavailable: {exc}")
    try:
        yield conn
    finally:
        conn.close()
