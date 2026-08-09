"""Compose-volume integration proof for the Docker ``docker-entrypoint-initdb.d`` path.

The Rust/Python provisioning paths apply ``sql/009`` inside an explicit transaction; Docker's
initdb runs every ``*.sql`` file statement-by-statement under AUTOCOMMIT. These tests spin an
ISOLATED Compose stack (unique project name + throwaway named volume) to prove:

1. **Fresh initialization is COMPLETE** — a clean volume booted purely through the mounted
   ``sql/001..009`` chain yields the full ``synthetic.arm_overlay`` substrate (table, unowned
   revision sequence, ROW trigger, all twelve ``ck_arm_overlay_*`` CHECKs) and a valid insert
   gets a trigger-assigned revision. This only holds because ``sql/009`` is now pure,
   transaction-free DDL (a ``SET LOCAL`` there would have been a silent autocommit no-op).

2. **A failed migration is FAIL-CLOSED** — a deliberately broken migration ordered after 009
   aborts init (the official image runs files with ``ON_ERROR_STOP=1``); the container never
   becomes ready, so a half-provisioned volume is never served rather than silently accepted.

Skipped when ``docker compose`` is unavailable. Uses its OWN volume — never the dev ``:5433``.
"""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SQL_DIR = REPO / "sql"

COMPOSE_YAML = """services:
  pg:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: tenantless
      POSTGRES_PASSWORD: tenantless_dev
      POSTGRES_DB: tenantless
    volumes:
      - ./initdb:/docker-entrypoint-initdb.d
      - data:/var/lib/postgresql/data
volumes:
  data:
"""

# A complete, CHECK-valid resource snapshot for the end-to-end insert proof.
_OK_BODY = (
    "jsonb_build_object('id','/probe','name','n',"
    "'type','Microsoft.Storage/storageAccounts','location','eastus',"
    "'tags','{}'::jsonb,'properties','{}'::jsonb)"
)


def _docker_compose_available() -> bool:
    try:
        r = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            timeout=30,
        )
        return r.returncode == 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _docker_compose_available(),
    reason="docker compose not available",
)


def _compose(work: Path, proj: str, *args: str, timeout: int = 120):
    return subprocess.run(
        ["docker", "compose", "-p", proj, *args],
        cwd=work,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _make_stack(tmp_path: Path, *, bad_name: str | None = None) -> Path:
    work = tmp_path / "stack"
    initdb = work / "initdb"
    initdb.mkdir(parents=True)
    for f in sorted(SQL_DIR.glob("*.sql")):
        shutil.copy(f, initdb / f.name)
    if bad_name:
        # A syntax-error migration; its filename decides WHERE in the chain it aborts init.
        (initdb / bad_name).write_text(
            "SELECT this_is_not_valid_sql_boom(;\n", encoding="utf-8"
        )
    (work / "docker-compose.yml").write_text(COMPOSE_YAML, encoding="utf-8")
    return work


def _psql(work: Path, proj: str, sql: str) -> str:
    r = _compose(
        work, proj, "exec", "-T", "pg",
        "psql", "-U", "tenantless", "-d", "tenantless", "-tAc", sql,
        timeout=60,
    )
    assert r.returncode == 0, f"psql failed: {r.stderr}"
    return r.stdout.strip()


def _poll_ready(work: Path, proj: str, timeout: int) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _compose(
            work, proj, "exec", "-T", "pg", "pg_isready", "-U", "tenantless",
            timeout=20,
        )
        if r.returncode == 0:
            return True
        time.sleep(2)
    return False


def _wait_exited(work: Path, proj: str, timeout: int) -> bool:
    """Poll until the pg container has EXITED. This — not `pg_isready` — is the fail-closed
    signal: during init the image runs a TEMPORARY socket-only server (so `pg_isready` reports
    ready mid-init, a race), whereas a broken init file (ON_ERROR_STOP=1) makes the entrypoint
    exit and the container stop."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _compose(work, proj, "ps", "-a", "--status", "exited", "-q", timeout=20)
        if (r.stdout or "").strip():
            return True
        time.sleep(2)
    return False


def test_compose_initdb_fresh_provision_is_complete(tmp_path: Path):
    proj = "tlsim_init_" + uuid.uuid4().hex[:8]
    work = _make_stack(tmp_path)
    try:
        up = _compose(work, proj, "up", "-d", timeout=240)
        assert up.returncode == 0, f"compose up failed: {up.stderr}"
        assert _poll_ready(work, proj, timeout=90), "postgres did not become ready"

        assert _psql(
            work, proj,
            "SELECT count(*) FROM pg_class WHERE relname='arm_overlay' "
            "AND relnamespace='synthetic'::regnamespace",
        ) == "1"
        assert _psql(
            work, proj,
            "SELECT count(*) FROM pg_class WHERE relname='arm_overlay_revision_seq' "
            "AND relnamespace='synthetic'::regnamespace",
        ) == "1"
        assert _psql(
            work, proj,
            "SELECT count(*) FROM pg_trigger WHERE tgname='trg_arm_overlay_revision' "
            "AND tgrelid='synthetic.arm_overlay'::regclass AND NOT tgisinternal",
        ) == "1"
        assert _psql(
            work, proj,
            "SELECT count(*) FROM pg_constraint "
            "WHERE conrelid='synthetic.arm_overlay'::regclass AND contype='c' "
            "AND conname LIKE 'ck_arm_overlay_%'",
        ) == "12"

        # End-to-end: a valid insert is accepted and the ROW trigger assigns revision > 0,
        # proving initdb applied the pure-DDL 009 fully under autocommit.
        rev = _psql(
            work, proj,
            "INSERT INTO synthetic.arm_overlay "
            "(id_lower,id,target_kind,source,present,body) "
            f"VALUES ('/probe','/probe','resource','user',true, {_OK_BODY}) "
            "RETURNING revision",
        )
        # psql appends the "INSERT 0 1" status tag after the RETURNING value — take line 1.
        assert int(rev.splitlines()[0]) > 0
    finally:
        _compose(work, proj, "down", "-v", "--remove-orphans", timeout=120)


def test_compose_initdb_failed_migration_is_fail_closed(tmp_path: Path):
    proj = "tlsim_boom_" + uuid.uuid4().hex[:8]
    work = _make_stack(tmp_path, bad_name="zzz_boom.sql")  # sorts AFTER 009
    try:
        # `up -d` may still return 0 (the container starts); the broken init file then aborts
        # startup under ON_ERROR_STOP=1 and the container exits — so it must never become ready.
        _compose(work, proj, "up", "-d", timeout=240)
        assert _wait_exited(work, proj, timeout=90), (
            "a broken initdb migration must fail-closed (container exits), "
            "not serve a half-provisioned volume"
        )
    finally:
        _compose(work, proj, "down", "-v", "--remove-orphans", timeout=120)


def test_compose_initdb_partial_persists_across_restart_then_recovers(tmp_path: Path):
    """A broken migration ordered BETWEEN 008 and 009 aborts init AFTER 001..008 commit but
    BEFORE 009 — the official image runs init files individually with ON_ERROR_STOP=1, NOT one
    transaction, so `synthetic.arm_overlay` is never created. On a later restart the data
    directory already exists, so init scripts are SKIPPED and the PARTIAL schema persists and is
    served (the risk under review). This proves PURE-DDL idempotent recovery ONLY: re-applying the
    transaction-free `009_arm_overlay.sql` on the partial DB completes the substrate to the full
    12-constraint form. It deliberately does NOT invoke the Rust `ensure_arm_overlay_schema()`
    provisioning path (which additionally takes the advisory lock and runs the deep inventory) nor
    boot the mock server — it isolates the claim that `009` is safe to re-apply on a partial init.
    The boot path applies exactly this same DDL on every start; malformed-overlay REJECTION at boot
    is covered by the Rust `overlay_structural_inventory_deep` test."""
    proj = "tlsim_partial_" + uuid.uuid4().hex[:8]
    work = _make_stack(tmp_path, bad_name="008a_boom.sql")  # sorts after 008_*, before 009_*
    try:
        _compose(work, proj, "up", "-d", timeout=240)
        assert _wait_exited(work, proj, timeout=90), "first boot must fail-closed (container exits)"

        # Restart the SAME volume: PGDATA exists -> initdb SKIPPED -> the partial schema is served.
        _compose(work, proj, "restart", "pg", timeout=120)
        assert _poll_ready(work, proj, timeout=60), "postgres must start on restart (init skipped)"

        # The partial state persists: arm_overlay absent, but 001..008 present.
        assert _psql(
            work, proj,
            "SELECT count(*) FROM pg_class WHERE relname='arm_overlay' "
            "AND relnamespace='synthetic'::regnamespace",
        ) == "0", "partial schema must persist across restart (arm_overlay absent)"
        assert _psql(
            work, proj,
            "SELECT count(*) FROM pg_class WHERE relname='resources' "
            "AND relnamespace='synthetic'::regnamespace",
        ) == "1", "001..008 must be present (genuinely partial, not empty)"

        # Recovery: re-apply 009 (pure DDL, autocommit-safe) as the boot path does.
        r = _compose(
            work, proj, "exec", "-T", "pg",
            "psql", "-U", "tenantless", "-d", "tenantless", "-v", "ON_ERROR_STOP=1",
            "-f", "/docker-entrypoint-initdb.d/009_arm_overlay.sql",
            timeout=60,
        )
        assert r.returncode == 0, f"re-applying 009 must recover the partial DB: {r.stderr}"
        assert _psql(
            work, proj,
            "SELECT count(*) FROM pg_class WHERE relname='arm_overlay' "
            "AND relnamespace='synthetic'::regnamespace",
        ) == "1", "arm_overlay must exist after recovery"
        assert _psql(
            work, proj,
            "SELECT count(*) FROM pg_constraint WHERE conrelid='synthetic.arm_overlay'::regclass "
            "AND contype='c' AND conname LIKE 'ck_arm_overlay_%'",
        ) == "12", "recovered overlay must carry all 12 CHECK constraints"
    finally:
        _compose(work, proj, "down", "-v", "--remove-orphans", timeout=120)
