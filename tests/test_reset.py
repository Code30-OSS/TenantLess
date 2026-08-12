"""Reset bracket — ``reset`` returns the served tenant to the immutable
pre-drift baseline (overlay + drift ledger cleared, baseline untouched, revision
sequence preserved).

This is the AUTHORITATIVE Python-reset proof: it drives the REAL
``reset`` CLI command via the in-process ``CliRunner`` (never a hand-rolled DELETE)
and asserts the four reset invariants against a live tenant:

  1. HASH-INVARIANCE   — H_after_reset == H0 (byte-identical): the estate hash
     (``mock-server/tests/common/immutability_hash.sql``, read VERBATIM — the ONE
     canonical digest shared with the immutability bracket, never
     re-implemented) is unchanged pre-drift vs post-reset. The overlay is EXCLUDED
     from the hash, so a write can never leak into the baseline digest.
  2. EMPTY LEDGER      — synthetic.arm_overlay / drift_records / drift_batches are
     all empty post-reset.
  3. SEQUENCE PRESERVED — synthetic.arm_overlay_revision_seq ``last_value`` is
     NON-DECREASING across the reset (never RESTART'd to 1) — ETag/revision
     monotonicity.
  4. BASELINE UNCHANGED — synthetic.resources / resource_groups / subscriptions /
     tenant row counts are identical to their pre-drift values: the
     full-wipe TRUNCATE + signer-refresh path is explicitly NOT taken.

DB-backed. The ``pg_conn`` fixture is the ``autocommit=True`` connection reused
VERBATIM from ``tests/test_immutability_bracket.py`` — a non-autocommit connection
keeps a txn open after every SELECT and DEADLOCKS reset's ensure-DDL preflight
(ACCESS SHARE vs ACCESS EXCLUSIVE — the server-startup ALTER-lock fragility;
project memory ``drift-tests-autocommit-and-gate-recipe``).

EXECUTION CONTRACT: the authoritative Linux PG16 container gate
runs this suite against an ISOLATED database/container — NEVER the shared :5433 dev
tenant (native Windows ``uv run pytest`` HANGS + pytest TRUNCATES :5433). A SKIP at
the authoritative gate is a BLOCKER: when ``TENANTLESS_RESET_GATE`` is set, the
ran-and-passed sentinel below FAILS unless >= 1 DB-backed reset test actually
EXECUTED and PASSED, so the gate can never pass vacuously by skipping. Local Windows
dev MAY still skip (the fixture escape).
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from tenantless.cli import main
from tenantless.generator import resources

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgres://tenantless:tenantless_dev@localhost:5433/tenantless",
)

_TENANT = str(uuid.UUID(int=0x1))
_SUB = str(uuid.UUID(int=0x11))
_RG = "rg-reset-test"

# The single canonical digest, shared with the immutability bracket — read
# VERBATIM, NEVER re-implemented (no inline hash SQL). Path:
# <repo-root>/mock-server/tests/common/immutability_hash.sql.
_HASH_SQL_PATH = (
    Path(__file__).resolve().parent.parent
    / "mock-server"
    / "tests"
    / "common"
    / "immutability_hash.sql"
)

# Non-vacuity sentinel: incremented ONLY after a DB-backed reset test passes every
# assertion. The authoritative gate asserts this is >= 1 so a SKIPPED reset test at
# close is a BLOCKER.
_RESET_TESTS_RAN_AND_PASSED = 0


@pytest.fixture
def pg_conn():
    """Yield a live ``autocommit=True`` psycopg connection, or skip if Postgres is
    unavailable.

    ``autocommit=True`` is REQUIRED (reused verbatim from
    ``tests/test_immutability_bracket.py``): a non-autocommit connection keeps a txn
    open after every SELECT, so our hash/baseline reads would sit idle-in-transaction
    holding ACCESS SHARE on ``synthetic.resources`` and DEADLOCK reset's idempotent
    schema-ensure preflight (which needs ACCESS EXCLUSIVE — the server-startup
    ALTER-lock fragility). Autocommit → our reads hold no lock, so the ensure preflight
    never blocks.
    """
    psycopg = pytest.importorskip("psycopg")
    try:
        conn = psycopg.connect(DATABASE_URL, connect_timeout=3, autocommit=True)
    except Exception as exc:  # noqa: BLE001 - any connection failure → skip
        pytest.skip(f"Postgres on 5433 unavailable: {exc}")
    try:
        yield conn
    finally:
        conn.close()


def _rid(name: str, type_key: str) -> str:
    return f"/subscriptions/{_SUB}/resourceGroups/{_RG}/providers/{type_key}/{name}"


def _seed(conn, specs):
    """Truncate + reseed a baseline tenant (tenant, subscription, ONE resource_group,
    and ``specs`` resources), clearing the overlay for isolation.

    Both relations the hash covers are NON-empty: ``synthetic.resources`` (the specs)
    and ``synthetic.resource_groups`` (one RG). Mirrors the bracket seed so the reset
    proof exercises the SAME baseline shape.
    """
    from psycopg.types.json import Jsonb

    from tenantless.generator import writer

    writer.ensure_drift_schema(conn)
    writer.ensure_arm_overlay_schema(conn)
    writer.ensure_arm_resolver_schema(conn)
    writer.truncate_synthetic(conn)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM synthetic.arm_overlay")
        cur.execute(
            "INSERT INTO synthetic.tenant "
            "(tenant_id, display_name, generated_at, profile_version, scale_params) "
            "VALUES (%s, %s, now(), %s, %s)",
            (_TENANT, "reset-test", "1.0", Jsonb({})),
        )
        cur.execute(
            "INSERT INTO synthetic.subscriptions "
            "(subscription_id, tenant_id, display_name, state, archetype, tags, "
            "authorization_source, spending_limit) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (_SUB, _TENANT, "sub", "Enabled", "test", Jsonb({}), "RoleBased", "On"),
        )
        cur.execute(
            "INSERT INTO synthetic.resource_groups "
            "(id, subscription_id, name, location, template_type, tags, provisioning_state) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                f"/subscriptions/{_SUB}/resourceGroups/{_RG}",
                _SUB,
                _RG,
                "eastus",
                "network",
                Jsonb({"environment": "prod"}),
                "Succeeded",
            ),
        )
        for s in specs:
            cur.execute(
                "INSERT INTO synthetic.resources "
                "(id, subscription_id, resource_group_name, name, type, location, "
                "tags, sku, kind, properties, provisioning_state, managed_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    _rid(s["name"], s["type"]),
                    _SUB,
                    _RG,
                    s["name"],
                    s["type"],
                    "eastus",
                    Jsonb(s.get("tags") or {}),
                    Jsonb(s["sku"]) if s.get("sku") is not None else None,
                    s.get("kind"),
                    Jsonb(s.get("properties") or {}),
                    "Succeeded",
                    None,
                ),
            )


def _estate_hash(conn) -> str:
    """Compute the canonical baseline digest by executing the SHARED hash SQL verbatim."""
    sql_text = _HASH_SQL_PATH.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql_text)
        row = cur.fetchone()
    assert row is not None and row[0] is not None, "hash SQL returned no estate_hash"
    return row[0]


def _seq_last_value(conn) -> int:
    """The overlay revision sequence's current ``last_value`` (0 if never advanced).

    ``pg_sequence_last_value`` returns NULL for a sequence whose ``nextval`` has never
    been called, so coalesce to 0 to get a comparable integer floor.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE("
            "pg_sequence_last_value('synthetic.arm_overlay_revision_seq'::regclass), 0)"
        )
        return int(cur.fetchone()[0])


def _count(conn, relation: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {relation}")
        return int(cur.fetchone()[0])


def _baseline_counts(conn) -> dict[str, int]:
    return {
        rel: _count(conn, rel)
        for rel in (
            "synthetic.resources",
            "synthetic.resource_groups",
            "synthetic.subscriptions",
            "synthetic.tenant",
        )
    }


def _apply(*args) -> None:
    """Invoke ``apply-drift`` in-process; assert it succeeded."""
    from click.testing import CliRunner

    runner = CliRunner()
    res = runner.invoke(main, ["apply-drift", "--database-url", DATABASE_URL, *args])
    assert res.exit_code == 0, (res.output, res.exception)


def _reset(*args) -> "object":
    """Invoke the NEW ``reset`` command in-process; return the CliRunner result."""
    from click.testing import CliRunner

    runner = CliRunner()
    return runner.invoke(main, ["reset", "--database-url", DATABASE_URL, *args])


def _seed_full_drift_mix(conn) -> None:
    """Seed a baseline + apply the whole drift surface (properties + tags + sku +
    @appear + disappear) so overlay AND the drift ledger are non-empty before reset.

    ORDER MATTERS: appear MUST precede disappear (a prior disappear at intensity 1.0
    tombstones every eligible leaf, emptying the population a following appear draws
    from). Mirrors the immutability bracket mix.
    """
    _seed(
        conn,
        [
            {
                "name": f"streset{i:03d}",
                "type": resources.T_STORAGE,
                "tags": {"environment": "prod"},
                "sku": {"name": "Standard_LRS", "tier": "Standard"},
                "properties": {"minimumTlsVersion": "TLS1_2"},
            }
            for i in range(5)
        ],
    )
    _apply("--type", "chaos", "--intensity", "1.0")
    _apply("--type", "temporal", "--codes", "DRIFT_SKU_TIER_SHIFT", "--intensity", "1.0")
    _apply("--type", "temporal", "--codes", "DRIFT_APPEAR", "--intensity", "1.0")
    _apply("--type", "temporal", "--codes", "DRIFT_DISAPPEAR", "--intensity", "1.0")


def test_reset_returns_tenant_to_pre_drift_baseline(pg_conn):
    """H0 (fresh) → apply drift mix → ``reset`` → assert the four reset invariants:
    hash-invariance, empty overlay+ledger, sequence non-decreasing,
    baseline row counts unchanged.

    RED until the ``reset`` command lands (the CliRunner invoke exits non-zero
    for an unknown command).
    """
    global _RESET_TESTS_RAN_AND_PASSED

    # --- Capture the pristine pre-drift baseline. ---
    _seed(
        pg_conn,
        [
            {
                "name": f"streset{i:03d}",
                "type": resources.T_STORAGE,
                "tags": {"environment": "prod"},
                "sku": {"name": "Standard_LRS", "tier": "Standard"},
                "properties": {"minimumTlsVersion": "TLS1_2"},
            }
            for i in range(5)
        ],
    )
    h0 = _estate_hash(pg_conn)
    seq_before = _seq_last_value(pg_conn)
    baseline_before = _baseline_counts(pg_conn)

    # --- Apply the full drift surface: overlay + ledger become non-empty. ---
    _apply("--type", "chaos", "--intensity", "1.0")
    _apply("--type", "temporal", "--codes", "DRIFT_SKU_TIER_SHIFT", "--intensity", "1.0")
    _apply("--type", "temporal", "--codes", "DRIFT_APPEAR", "--intensity", "1.0")
    _apply("--type", "temporal", "--codes", "DRIFT_DISAPPEAR", "--intensity", "1.0")

    assert _count(pg_conn, "synthetic.arm_overlay") > 0, "drift mix must write overlay rows"
    assert _count(pg_conn, "synthetic.drift_batches") > 0, "drift mix must write batches"
    seq_after_drift = _seq_last_value(pg_conn)
    assert seq_after_drift >= seq_before, "the revision sequence advanced across apply"

    # --- Reset returns the tenant to the pre-drift baseline. ---
    res = _reset()
    assert res.exit_code == 0, (res.output, res.exception)

    # (1) HASH-INVARIANCE — overlay excluded from the hash; baseline byte-identical.
    h1 = _estate_hash(pg_conn)
    assert h1 == h0, "estate hash changed across reset — baseline is not pristine"

    # (2) EMPTY LEDGER — overlay + drift records + drift batches all cleared.
    assert _count(pg_conn, "synthetic.arm_overlay") == 0, "arm_overlay not cleared by reset"
    assert _count(pg_conn, "synthetic.drift_records") == 0, "drift_records not cleared by reset"
    assert _count(pg_conn, "synthetic.drift_batches") == 0, "drift_batches not cleared by reset"

    # (3) SEQUENCE PRESERVED — never RESTART'd; last_value is non-decreasing.
    seq_after_reset = _seq_last_value(pg_conn)
    assert seq_after_reset >= seq_after_drift, (
        "arm_overlay_revision_seq regressed across reset — monotonicity broken"
    )

    # (4) BASELINE UNCHANGED — synthetic.* row counts identical to pre-drift.
    baseline_after = _baseline_counts(pg_conn)
    assert baseline_after == baseline_before, (
        f"synthetic.* baseline row counts changed across reset "
        f"(before={baseline_before}, after={baseline_after}) — full-wipe path taken"
    )

    _RESET_TESTS_RAN_AND_PASSED += 1


def test_reset_is_idempotent_on_a_clean_tenant(pg_conn):
    """Reset on an already-clean tenant is a no-op success (empty ledger stays empty,
    baseline hash unchanged) — reset carries no precondition that drift exist.

    RED until the ``reset`` command lands.
    """
    global _RESET_TESTS_RAN_AND_PASSED

    _seed(
        pg_conn,
        [
            {
                "name": f"stclean{i:03d}",
                "type": resources.T_STORAGE,
                "tags": {"environment": "prod"},
                "sku": {"name": "Standard_LRS", "tier": "Standard"},
                "properties": {"minimumTlsVersion": "TLS1_2"},
            }
            for i in range(3)
        ],
    )
    h0 = _estate_hash(pg_conn)

    res = _reset()
    assert res.exit_code == 0, (res.output, res.exception)

    assert _count(pg_conn, "synthetic.arm_overlay") == 0
    assert _count(pg_conn, "synthetic.drift_batches") == 0
    assert _estate_hash(pg_conn) == h0

    _RESET_TESTS_RAN_AND_PASSED += 1


def test_reset_ran_and_passed_sentinel():
    """Non-vacuity floor: at the authoritative Linux PG16 gate
    (``TENANTLESS_RESET_GATE`` set) at least one DB-backed reset test MUST have executed
    and passed — a SKIP at close is a BLOCKER, so the gate can never pass vacuously.

    Local Windows dev (gate env unset) MAY skip when Postgres is unavailable — the
    escape stays for the dev host only. Ordered after the DB-backed tests so the
    module-level counter reflects their outcome.
    """
    gate_active = bool(os.environ.get("TENANTLESS_RESET_GATE"))
    if _RESET_TESTS_RAN_AND_PASSED >= 1:
        return  # a DB-backed reset test executed and passed — non-vacuous.
    if gate_active:
        pytest.fail(
            "BLOCKER: no DB-backed reset test executed at the authoritative gate "
            "(TENANTLESS_RESET_GATE set) — the reset invariant would pass vacuously. "
            "The gate requires an isolated PG16 tenant so the reset proof runs non-skipped."
        )
    pytest.skip(
        "reset tests skipped (Postgres unavailable and TENANTLESS_RESET_GATE unset) — "
        "local dev escape; the authoritative gate sets TENANTLESS_RESET_GATE."
    )
