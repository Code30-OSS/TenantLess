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


# --------------------------------------------------------------------------------------- #
# Live conformance harness (real Rust server + real Python drift CLI + one live PG16).
#
# The conformance machine (tests/test_arm_conformance.py) resets the served tenant before
# EVERY example, so it must NEVER run against the shared :5433 dev tenant: it uses a
# dedicated, env-configurable conformance database, provisioned once per session, and a
# server launched once per session against THAT database.
#
# Non-skip gate: under TENANTLESS_REQUIRE_LIVE=1 (the CI conformance job) an unavailable
# Postgres / server binary FAILS the run; without the flag (local dev) it skips cleanly.
# --------------------------------------------------------------------------------------- #

REQUIRE_LIVE_ENV = "TENANTLESS_REQUIRE_LIVE"
CONFORMANCE_DB_ENV = "TENANTLESS_CONFORMANCE_DATABASE_URL"
SERVER_BIN_ENV = "TENANTLESS_SERVER_BIN"

# A DEDICATED database (not the `tenantless` dev tenant). 127.0.0.1 rather than
# `localhost`: on Windows `localhost` may resolve to ::1 first, and a psycopg connect with
# no timeout can then hang against a container bound to IPv4 only.
_CONFORMANCE_DB_DEFAULT = (
    "postgres://tenantless:tenantless_dev@127.0.0.1:5433/tenantless_conformance"
)

# The dev tenant database name: the one database the harness refuses outright.
_DEV_TENANT_DB = "tenantless"

# The small, clean, deterministic baseline every example is restored to. No violations,
# cross-sub dependencies or identity, so leaf resources carry no inbound references and
# stay eligible for temporal disappear; small enough that temporal drift stays cheap.
CONFORMANCE_BASELINE = {"seed": 2424, "subscriptions": 3, "resources": 60}

_CLI_RUNNER = "from tenantless.cli import main; main()"
_REPO_ROOT = Path(__file__).resolve().parents[1]


def require_live(reason: str) -> None:
    """Fail (gate on) or skip (gate off) when live infrastructure is unavailable.

    Only the exact value ``TENANTLESS_REQUIRE_LIVE=1`` arms the gate, so a CI job that
    sets it can never be turned into a green no-op by absent infrastructure.
    """
    if os.environ.get(REQUIRE_LIVE_ENV) == "1":
        pytest.fail(f"live infra required but unavailable: {reason}", pytrace=False)
    pytest.skip(f"live infra unavailable: {reason}")


def conformance_database_url() -> str:
    """The dedicated conformance database URL (env override, else the default)."""
    return os.environ.get(CONFORMANCE_DB_ENV) or _CONFORMANCE_DB_DEFAULT


def _safe_target(url: str) -> str:
    """host:port/db of a DSN, never the credentials."""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    return f"{parts.hostname}:{parts.port}/{parts.path.lstrip('/')}"


def refuse_dev_tenant(url: str) -> None:
    """Hard-fail if ``url`` targets the shared dev tenant.

    The machine clears the overlay + drift ledger before every example and the session
    provisioning regenerates the baseline, so pointing it at the dev tenant would wipe it.
    Refuses the dev tenant database name and any URL equal to the suite-wide DATABASE_URL.
    """
    from urllib.parse import urlsplit

    db_name = urlsplit(url).path.lstrip("/")
    if db_name == _DEV_TENANT_DB or url == os.environ.get("DATABASE_URL"):
        pytest.fail(
            "refusing to run the conformance harness against the dev tenant "
            f"({_safe_target(url)}); set {CONFORMANCE_DB_ENV} to a dedicated database",
            pytrace=False,
        )


def _run_cli_subprocess(url: str, *args: str) -> str:
    """Run the real ``tenantless`` CLI in a child interpreter against ``url``."""
    import subprocess

    env = {**os.environ, "DATABASE_URL": url}
    proc = subprocess.run(  # noqa: S603 - argv list, trusted interpreter
        [sys.executable, "-c", _CLI_RUNNER, *args],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        timeout=600,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"conformance provisioning `tenantless {args[0]}` failed "
            f"(exit {proc.returncode}):\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}",
            pytrace=False,
        )
    return proc.stdout


def run_cli_inprocess(*args: str) -> tuple[int, str]:
    """Invoke the real Click CLI in-process; return ``(exit_code, output)``.

    Same command code as the console script, without a ~1-2s interpreter start per call,
    which is what makes one live op per state-machine step affordable.
    """
    from click.testing import CliRunner

    from tenantless.cli import main as cli_main

    result = CliRunner().invoke(cli_main, list(args), catch_exceptions=True)
    output = result.output
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        output += f"\n{type(result.exception).__name__}: {result.exception}"
    return result.exit_code, output


def reset_conformance_baseline(url: str) -> str:
    """Restore the known baseline: clear overlay + drift ledger, keep the revision seq.

    Uses the real ``reset`` command, which leaves the four baseline relations untouched
    and never rewinds ``arm_overlay_revision_seq`` (revisions stay monotonic across
    examples).
    """
    code, out = run_cli_inprocess("reset", "--database-url", url)
    if code != 0:
        pytest.fail(f"baseline reset failed (exit {code}):\n{out}", pytrace=False)
    return out


@pytest.fixture(scope="session")
def conformance_db() -> str:
    """Provision the isolated conformance database ONCE per session; return its URL.

    ``init-db`` + a seeded ``generate`` of the small clean baseline. An unreachable
    Postgres goes through :func:`require_live` (fail under the gate, skip otherwise).
    """
    url = conformance_database_url()
    refuse_dev_tenant(url)
    import psycopg

    try:
        probe = psycopg.connect(url, connect_timeout=5)
    except Exception as exc:  # noqa: BLE001 - any connection failure is "unavailable"
        require_live(
            f"conformance Postgres at {_safe_target(url)} unreachable "
            f"({type(exc).__name__})"
        )
    probe.close()

    _run_cli_subprocess(url, "init-db", "--database-url", url)
    b = CONFORMANCE_BASELINE
    _run_cli_subprocess(
        url,
        "generate",
        "--profile", "small",
        "--seed", str(b["seed"]),
        "--subscriptions", str(b["subscriptions"]),
        "--resources", str(b["resources"]),
        "--force",
        "--no-violations",
        "--no-cross-sub",
        "--no-identity",
        "--jobs", "1",
    )
    return url


@pytest.fixture(scope="session")
def conformance_pg(conformance_db):
    """An AUTOCOMMIT psycopg connection to the conformance DB (the read-only oracle).

    autocommit is required: a read left open in a transaction would hold ACCESS SHARE on
    the resolved view and deadlock the idempotent schema-ensure DDL the drift commands run.
    """
    import psycopg

    conn = psycopg.connect(conformance_db, autocommit=True, connect_timeout=5)
    try:
        yield conn
    finally:
        conn.close()


def _server_binary() -> str | None:
    """Locate a BUILT tenantless-server binary (never falls back to ``cargo run``)."""
    import shutil

    override = os.environ.get(SERVER_BIN_ENV)
    if override:
        return override if Path(override).is_file() else None
    exe = "tenantless-server" + (".exe" if os.name == "nt" else "")
    roots = []
    if os.environ.get("CARGO_TARGET_DIR"):
        roots.append(Path(os.environ["CARGO_TARGET_DIR"]))
    roots.append(_REPO_ROOT / "target")
    for root in roots:
        for profile in ("release", "debug"):
            candidate = root / profile / exe
            if candidate.is_file():
                return str(candidate)
    return shutil.which("tenantless-server")


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def conformance_server(conformance_db, tmp_path_factory):
    """Launch the REAL server ONCE per session against the conformance DB; yield base URL.

    Writes are armed with ``--enable-arm-writes`` on a loopback bind without enforced auth
    (the documented local/test posture; any Bearer is accepted). Readiness-polls a GET
    before yielding and terminates the child at session end.
    """
    import subprocess
    import time
    import urllib.error
    import urllib.request

    binary = _server_binary()
    if binary is None:
        require_live(
            "no built tenantless-server binary (cargo build --release -p "
            f"tenantless-server, or set {SERVER_BIN_ENV})"
        )

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = tmp_path_factory.mktemp("conformance-server") / "server.log"
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("ENFORCE_AUTH", "TLS", "ENABLE_CONTROL_PLANE", "ALLOW_INSECURE_WRITES")
    }
    env["DATABASE_URL"] = conformance_db
    env.setdefault("RUST_LOG", "warn")
    cmd = [
        binary,
        "--host", "127.0.0.1",
        "--port", str(port),
        "--base-url", base_url,
        "--database-url", conformance_db,
        "--enable-arm-writes",
    ]
    log = open(log_path, "wb")  # noqa: SIM115 - closed in the finally below
    proc = subprocess.Popen(  # noqa: S603 - argv list, trusted binary
        cmd, env=env, stdout=log, stderr=subprocess.STDOUT
    )
    try:
        deadline = time.monotonic() + 60
        ready = False
        while time.monotonic() < deadline and proc.poll() is None:
            req = urllib.request.Request(f"{base_url}/subscriptions", method="GET")
            req.add_header("Authorization", "Bearer conformance")
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    if resp.status == 200:
                        ready = True
                        break
            except (urllib.error.URLError, ConnectionError, OSError):
                pass
            time.sleep(0.25)
        if not ready:
            log.flush()
            tail = log_path.read_bytes()[-3000:].decode("utf-8", "replace")
            pytest.fail(
                f"conformance server did not become ready (exit={proc.poll()}):\n{tail}",
                pytrace=False,
            )
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        log.close()
