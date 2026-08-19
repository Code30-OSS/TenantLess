"""``generate --force`` (``writer.truncate_synthetic``) must clear
the ARM overlay while PRESERVING the monotonic revision sequence.

This is the Linux-PG16-gate regression for the THIRD full-wipe path (the reset and
snapshot-restore legs are proven natively in Rust: ``integration.rs``
``run_reset_clears_arm_overlay_and_ledger`` and ``control.rs``
``restore_clears_preexisting_arm_overlay``). ``writer.truncate_synthetic`` is the
``generate --force`` wipe: it ``TRUNCATE … RESTART IDENTITY CASCADE``s the STATIC
``_SYNTHETIC_TABLES`` allowlist. On the pre-fix tree ``synthetic.arm_overlay`` is
ABSENT from that allowlist, so a re-seeded tenant would resolve the PREVIOUS tenant's
drift overlay ids — a stale ``present=true`` phantom resource.

Behaviour proven (seed → wipe → assert, a before/after regression, not a grep):

  1. OVERLAY CLEARED    — after ``writer.truncate_synthetic`` the ``synthetic.arm_overlay``
     row count is ZERO (RED-BY-INSPECTION until the fix adds ``synthetic.arm_overlay``
     to ``_SYNTHETIC_TABLES``; the overlay row survives the truncate on the pre-fix tree).
  2. SEQUENCE PRESERVED — ``synthetic.arm_overlay_revision_seq`` ``last_value`` is
     NON-DECREASING across the wipe: the sequence is standalone/UNOWNED (sql/009:52-56)
     so ``TRUNCATE … RESTART IDENTITY`` cannot rewind it — revision/ETag monotonicity
     holds for ``generate --force`` with NO ``ALTER SEQUENCE … RESTART`` needed.

DB-backed. The ``pg_conn`` fixture is the ``autocommit=True`` connection reused
VERBATIM from ``tests/test_reset.py`` — a non-autocommit connection keeps a txn open
after every SELECT and DEADLOCKS the idempotent ensure-DDL preflight (ACCESS SHARE vs
ACCESS EXCLUSIVE; project memory ``drift-tests-autocommit-and-gate-recipe``).

EXECUTION CONTRACT: the authoritative Linux PG16 container
gate runs this against an ISOLATED database/container — NEVER the shared :5433 dev
tenant (native Windows ``uv run pytest`` HANGS + pytest TRUNCATES :5433). A SKIP at the
authoritative gate is a BLOCKER: when ``TENANTLESS_RESET_GATE`` is set, the ran-and-passed
sentinel below FAILS unless >= 1 DB-backed truncate test actually EXECUTED and PASSED, so
the gate can never pass vacuously by skipping. Local Windows dev MAY still skip.
"""

from __future__ import annotations

import os
import uuid

import pytest

from tenantless.generator import writer

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgres://tenantless:tenantless_dev@localhost:5433/tenantless",
)

# A present overlay row id that is NOT shaped like a baseline resource under test — a
# self-contained drift-authored copy-on-write snapshot row.
_OVERLAY_ID = (
    f"/subscriptions/{uuid.UUID(int=0x11)}/resourceGroups/rg-truncate-test/"
    "providers/Microsoft.Storage/storageAccounts/ov-truncate"
)

# Non-vacuity sentinel: incremented ONLY after a DB-backed truncate test passes every
# assertion. The authoritative gate asserts this is >= 1 so a SKIPPED test at close is a
# BLOCKER.
_TRUNCATE_TESTS_RAN_AND_PASSED = 0


@pytest.fixture
def pg_conn():
    """Yield a live ``autocommit=True`` psycopg connection, or skip if Postgres is
    unavailable.

    ``autocommit=True`` is REQUIRED (reused verbatim from ``tests/test_reset.py``): a
    non-autocommit connection keeps a txn open after every SELECT, so our reads would sit
    idle-in-transaction holding ACCESS SHARE and DEADLOCK ``truncate_synthetic``'s
    idempotent schema-ensure preflight (which needs ACCESS EXCLUSIVE — the server-startup
    ALTER-lock fragility). Autocommit → our reads hold no lock, so the preflight never blocks.
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


def _count(conn, relation: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {relation}")  # STATIC relation literal
        return int(cur.fetchone()[0])


def _seq_last_value(conn) -> int:
    """The overlay revision sequence's current ``last_value`` (0 if never advanced).

    ``pg_sequence_last_value`` returns NULL for a sequence whose ``nextval`` has never been
    called, so coalesce to 0 to get a comparable integer floor.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE("
            "pg_sequence_last_value('synthetic.arm_overlay_revision_seq'::regclass), 0)"
        )
        return int(cur.fetchone()[0])


def _seed_present_overlay_row(conn) -> None:
    """INSERT one VALID ``present=true`` overlay row (the trigger assigns its revision).

    The body is built server-side with ``jsonb_build_object`` so it satisfies the sql/009
    row-model CHECKs (present ⇒ body present + ``body->>'id' = id`` + string envelope +
    object ``tags``/``properties``); ``revision`` is a placeholder overwritten by the BEFORE
    trigger's ``nextval``. Only ``id`` binds as ``%s`` — no string splice.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO synthetic.arm_overlay "
            "(id_lower, id, target_kind, source, present, body, revision) "
            "VALUES (lower(%s), %s, 'resource', 'drift', true, "
            "        jsonb_build_object("
            "            'id', %s::text, 'name', 'ov-truncate', "
            "            'type', 'Microsoft.Storage/storageAccounts', "
            "            'location', 'eastus', 'tags', '{}'::jsonb, "
            "            'properties', '{}'::jsonb), "
            "        1)",
            (_OVERLAY_ID, _OVERLAY_ID, _OVERLAY_ID),
        )


def test_truncate_synthetic_clears_arm_overlay_and_preserves_sequence(pg_conn):
    """Seed a present overlay row → ``writer.truncate_synthetic`` → assert the overlay is
    empty AND the revision sequence is non-decreasing (a before/after regression, not a grep).

    RED-BY-INSPECTION on the pre-fix tree: ``synthetic.arm_overlay`` is absent from
    ``writer._SYNTHETIC_TABLES``, so ``truncate_synthetic`` never truncates it and the
    seeded overlay row SURVIVES the wipe. Goes GREEN once the allowlist entry lands.
    """
    global _TRUNCATE_TESTS_RAN_AND_PASSED

    # Provision the drift + overlay + resolver substrate (idempotent), then seed one overlay row.
    writer.ensure_drift_schema(pg_conn)
    writer.ensure_arm_overlay_schema(pg_conn)
    writer.ensure_arm_resolver_schema(pg_conn)
    # Start from a clean overlay for isolation, then seed the row under test.
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM synthetic.arm_overlay")
    _seed_present_overlay_row(pg_conn)

    assert _count(pg_conn, "synthetic.arm_overlay") > 0, "precondition: a present overlay row is seeded"
    seq_before = _seq_last_value(pg_conn)

    # The generate --force wipe (the REAL writer path, not a re-implemented TRUNCATE).
    writer.truncate_synthetic(pg_conn)

    # (1) OVERLAY CLEARED — no stale present=true overlay id can leak into a re-seeded tenant.
    assert _count(pg_conn, "synthetic.arm_overlay") == 0, (
        "truncate_synthetic did not clear synthetic.arm_overlay — arm_overlay is "
        "missing from writer._SYNTHETIC_TABLES"
    )

    # (2) SEQUENCE PRESERVED — the UNOWNED revision seq is not rewound by RESTART IDENTITY.
    seq_after = _seq_last_value(pg_conn)
    assert seq_after >= seq_before, (
        f"arm_overlay_revision_seq regressed across truncate_synthetic "
        f"({seq_after} < {seq_before}) — revision/ETag monotonicity broken"
    )

    _TRUNCATE_TESTS_RAN_AND_PASSED += 1


def test_truncate_overlay_ran_and_passed_sentinel():
    """Non-vacuity floor: at the authoritative Linux PG16
    gate (``TENANTLESS_RESET_GATE`` set) at least one DB-backed truncate test MUST have
    executed and passed — a SKIP at close is a BLOCKER, so the gate can never pass vacuously.

    Local Windows dev (gate env unset) MAY skip when Postgres is unavailable — the escape
    stays for the dev host only. Ordered after the DB-backed test so the module-level counter
    reflects its outcome.
    """
    gate_active = bool(os.environ.get("TENANTLESS_RESET_GATE"))
    if _TRUNCATE_TESTS_RAN_AND_PASSED >= 1:
        return  # a DB-backed truncate test executed and passed — non-vacuous.
    if gate_active:
        pytest.fail(
            "BLOCKER: no DB-backed truncate_synthetic overlay test executed at the "
            "authoritative gate (TENANTLESS_RESET_GATE set) — the overlay-clear invariant "
            "would pass vacuously. The gate requires an isolated PG16 tenant so the proof "
            "runs non-skipped."
        )
    pytest.skip(
        "truncate overlay test skipped (Postgres unavailable and TENANTLESS_RESET_GATE "
        "unset) — local dev escape; the authoritative gate sets TENANTLESS_RESET_GATE."
    )
