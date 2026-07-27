"""revert-drift CLI tests (Plan 11-06, DRIFT-01/04 + D-01/02/03/06/13).

``tenantless revert-drift --batch-id <uuid> [--dry-run]`` is the LIFO-guarded,
single-transaction restore. It rejects an out-of-order revert when a newer ACTIVE
(``reverted_at IS NULL``) batch overlaps any resource (strict LIFO, D-06); else it
restores each affected resource from its ``drift_records`` per-field ``before``
value, unhides disappeared rows / DELETEs appear rows (D-13), and marks the batch
``reverted_at`` WITHOUT deleting history (D-03). ``--dry-run`` mutates nothing.

This file ALSO pins the apply-side temporal lifecycle wiring carried forward from
Plan 11-05: ``apply-drift --type temporal`` must PRODUCE the disappear/appear
``drift_records`` revert consumes (otherwise revert's unhide/delete path has no
producer and the 11-08 round-trip is untestable).

DB-backed tests use the project ``pg_conn`` skip fixture so DB-less CI skips
clean; the LIFO scenarios seed ``drift_batches`` / ``drift_records`` directly.
"""

from __future__ import annotations

import datetime as _dt
import os
import uuid

import pytest

from tenantless.cli import main
from tenantless.generator import resources

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgres://tenantless:tenantless_dev@localhost:5433/tenantless",
)

# A fixed synthetic tenant + subscription for the seeded test rows.
_TENANT = str(uuid.UUID(int=0x1))
_SUB = str(uuid.UUID(int=0x11))


@pytest.fixture
def pg_conn():
    """Yield a live psycopg connection, or skip if Postgres is unavailable.

    Verbatim mirror of ``tests/test_drift_apply.py::pg_conn`` so the suite skips
    clean in DB-less CI (STATE.md: "DB-less CI skips clean").
    """
    psycopg = pytest.importorskip("psycopg")
    try:
        conn = psycopg.connect(DATABASE_URL, connect_timeout=3)
    except Exception as exc:  # noqa: BLE001 - any connection failure -> skip
        pytest.skip(f"Postgres on 5433 unavailable: {exc}")
    try:
        yield conn
    finally:
        conn.close()


def _seed_storage(conn, *, count=3, sub=_SUB):
    """Truncate the synthetic schema and insert ``count`` empty storage accounts.

    Empty ``properties``/``tags`` + no refs make every storage account both
    chaos-eligible (allowBlobPublicAccess / supportsHttpsTrafficOnly /
    minimumTlsVersion) AND disappear-eligible (leaf, unreferenced) so the
    lifecycle apply produces deterministic disappear/appear records.
    """
    from psycopg.types.json import Jsonb

    from tenantless.generator import writer

    writer.ensure_drift_schema(conn)
    writer.truncate_synthetic(conn)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO synthetic.tenant "
            "(tenant_id, display_name, generated_at, profile_version, scale_params) "
            "VALUES (%s, %s, now(), %s, %s)",
            (sub_tenant := _TENANT, "drift-test", "1.0", Jsonb({})),
        )
        cur.execute(
            "INSERT INTO synthetic.subscriptions "
            "(subscription_id, tenant_id, display_name, state, archetype, tags, "
            "authorization_source, spending_limit) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (sub, sub_tenant, "sub", "Enabled", "test", Jsonb({}), "RoleBased", "On"),
        )
        for i in range(count):
            name = f"stdrift{i:03d}"
            rid = (
                f"/subscriptions/{sub}/resourceGroups/rg-drift-test/providers/"
                f"{resources.T_STORAGE}/{name}"
            )
            cur.execute(
                "INSERT INTO synthetic.resources "
                "(id, subscription_id, resource_group_name, name, type, location, "
                "tags, sku, kind, properties, provisioning_state, managed_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    rid, sub, "rg-drift-test", name, resources.T_STORAGE, "eastus",
                    Jsonb({}), None, None, Jsonb({}), "Succeeded", None,
                ),
            )
    conn.commit()


def _apply(*args):
    from click.testing import CliRunner

    runner = CliRunner()
    return runner.invoke(
        main, ["apply-drift", "--database-url", DATABASE_URL, *args]
    )


def _revert(*args):
    from click.testing import CliRunner

    runner = CliRunner()
    return runner.invoke(
        main, ["revert-drift", "--database-url", DATABASE_URL, *args]
    )


def _only_batch_id(conn):
    """The single drift batch's id (tests apply exactly one batch).

    Commits BEFORE and AFTER the read so the test connection holds no lock when
    the in-process CliRunner next invokes a command whose ensure_drift_schema
    ALTERs synthetic.resources (an open AccessShareLock here would self-deadlock).
    """
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT batch_id FROM synthetic.drift_batches")
        rows = cur.fetchall()
    conn.commit()
    assert len(rows) == 1, rows
    return str(rows[0][0])


def _seed_batch(conn, *, batch_id, applied_at, records, reverted_at=None):
    """Insert one ``drift_batches`` row + its ``drift_records`` directly.

    ``records`` is a list of ``(resource_id, field_path, before, after)`` tuples.
    Used by the LIFO scenarios to construct overlapping/non-overlapping batches
    without invoking apply.
    """
    from psycopg.types.json import Jsonb

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO synthetic.drift_batches "
            "(batch_id, drift_type, seed, options, parent_fingerprint, "
            "result_fingerprint, applied_at, reverted_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                batch_id, "chaos", 42, Jsonb({}), "p" * 8, "r" * 8,
                applied_at, reverted_at,
            ),
        )
        for rid, field_path, before, after in records:
            cur.execute(
                "INSERT INTO synthetic.drift_records "
                "(batch_id, resource_id, subscription_id, field_path, before, after) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (batch_id, rid, _SUB, field_path, Jsonb(before), Jsonb(after)),
            )
    conn.commit()


def _res_id(i):
    return (
        f"/subscriptions/{_SUB}/resourceGroups/rg-drift-test/providers/"
        f"{resources.T_STORAGE}/stdrift{i:03d}"
    )


# --------------------------------------------------------------------------- #
# Task 1 (carry-forward from 11-05) — apply-side temporal lifecycle wiring.
# revert's unhide/delete (D-13) needs a real PRODUCER: apply-drift --type
# temporal must compute compute_lifecycle and persist disappear (drift_deleted_at
# set + record) / appear (new leaf row + @appear record).
# --------------------------------------------------------------------------- #


def test_temporal_lifecycle_records(pg_conn):
    """apply-drift --type temporal produces appear/disappear drift_records and
    mutates the DB: disappeared leaves get drift_deleted_at set; appear mints new
    leaf rows (D-09/D-12) — the producer revert's unhide/delete consumes."""
    _seed_storage(pg_conn, count=3)

    res = _apply("--type", "temporal", "--intensity", "1.0")
    assert res.exit_code == 0, (res.output, res.exception)

    pg_conn.commit()
    with pg_conn.cursor() as cur:
        # 3 eligible leaves at intensity 1.0 -> 3 disappear + 3 appear.
        cur.execute(
            "SELECT count(*) FROM synthetic.drift_records "
            "WHERE field_path = 'drift_deleted_at'"
        )
        assert cur.fetchone()[0] == 3
        cur.execute(
            "SELECT count(*) FROM synthetic.drift_records WHERE field_path = '@appear'"
        )
        assert cur.fetchone()[0] == 3

        # The original 3 leaves are now soft-deleted in place (D-09).
        cur.execute(
            "SELECT count(*) FROM synthetic.resources "
            "WHERE drift_deleted_at IS NOT NULL"
        )
        assert cur.fetchone()[0] == 3

        # 3 original + 3 minted appear rows (D-12).
        cur.execute("SELECT count(*) FROM synthetic.resources")
        assert cur.fetchone()[0] == 6

        # Each @appear record's resource_id is a real, newly-inserted row.
        cur.execute(
            "SELECT resource_id FROM synthetic.drift_records WHERE field_path='@appear'"
        )
        appear_ids = [r[0] for r in cur.fetchall()]
        for aid in appear_ids:
            cur.execute("SELECT count(*) FROM synthetic.resources WHERE id = %s", (aid,))
            assert cur.fetchone()[0] == 1


# --------------------------------------------------------------------------- #
# Task 2 (plan Task 1) — strict-LIFO overlap guard (D-06, Pitfall 5: the check
# precedes any mutation). Reject reverting a batch when a newer ACTIVE batch
# shares any resource_id; allow when there is no newer active overlap.
# --------------------------------------------------------------------------- #

_T1 = _dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)
_T2 = _dt.datetime(2026, 1, 2, 12, 0, 0, tzinfo=_dt.timezone.utc)


def test_lifo_reject(pg_conn):
    """Reverting an older batch is REJECTED with zero mutation when a newer active
    batch overlaps any of its resources (D-06). The target's reverted_at stays
    NULL and no resource column changes (Pitfall 5 — guard precedes mutation)."""
    from psycopg.types.json import Jsonb

    _seed_storage(pg_conn, count=3)
    # R0 carries B1's drift in place (simulating an applied chaos batch).
    with pg_conn.cursor() as cur:
        cur.execute(
            "UPDATE synthetic.resources SET properties = %s WHERE id = %s",
            (Jsonb({"allowBlobPublicAccess": True}), _res_id(0)),
        )
    pg_conn.commit()

    b1, b2 = str(uuid.uuid4()), str(uuid.uuid4())
    _seed_batch(
        pg_conn, batch_id=b1, applied_at=_T1,
        records=[(_res_id(0), "properties.allowBlobPublicAccess", None, True)],
    )
    # B2 is NEWER and ACTIVE and overlaps R0 → reverting B1 is out-of-order.
    _seed_batch(
        pg_conn, batch_id=b2, applied_at=_T2,
        records=[(_res_id(0), "properties.minimumTlsVersion", None, "TLS1_0")],
    )

    res = _revert("--batch-id", b1)
    assert res.exit_code != 0, (res.output, res.exception)

    pg_conn.commit()
    with pg_conn.cursor() as cur:
        # B1 was NOT marked reverted (no mutation).
        cur.execute("SELECT reverted_at FROM synthetic.drift_batches WHERE batch_id=%s", (b1,))
        assert cur.fetchone()[0] is None
        # R0's served column is untouched (the guard ran before any restore).
        cur.execute("SELECT properties FROM synthetic.resources WHERE id=%s", (_res_id(0),))
        assert cur.fetchone()[0] == {"allowBlobPublicAccess": True}


def test_lifo_allows_non_overlap(pg_conn):
    """Reverting B1 is ALLOWED when the newer active batch B2 shares no
    resource_id with B1 (no out-of-order corruption risk)."""
    _seed_storage(pg_conn, count=3)
    b1, b2 = str(uuid.uuid4()), str(uuid.uuid4())
    _seed_batch(
        pg_conn, batch_id=b1, applied_at=_T1,
        records=[(_res_id(0), "properties.allowBlobPublicAccess", None, True)],
    )
    _seed_batch(  # newer + active but a DIFFERENT resource → no overlap
        pg_conn, batch_id=b2, applied_at=_T2,
        records=[(_res_id(1), "properties.allowBlobPublicAccess", None, True)],
    )

    res = _revert("--batch-id", b1)
    assert res.exit_code == 0, (res.output, res.exception)


def test_lifo_same_instant_no_deadlock(pg_conn):
    """Two batches sharing the EXACT same applied_at that overlap a resource_id
    must NOT deadlock (P1, 11-10). The monotonic ``seq`` total order breaks the
    applied_at tie: B2 (inserted second → higher seq) is the newer batch.

    - Reverting the OLDER (B1) FIRST is rejected (B2 is a newer active overlap).
    - Reverting the NEWER (B2) succeeds.
    - The OLDER (B1) then becomes revertable — no permanent mutual block.

    A pre-11-10 ``>=`` applied_at guard made the two same-instant batches block
    EACH OTHER forever (neither revertable); this test pins that the seq order
    resolves the deadlock."""
    from psycopg.types.json import Jsonb

    _seed_storage(pg_conn, count=3)
    with pg_conn.cursor() as cur:
        cur.execute(
            "UPDATE synthetic.resources SET properties = %s WHERE id = %s",
            (Jsonb({"allowBlobPublicAccess": True}), _res_id(0)),
        )
    pg_conn.commit()

    # B1 and B2 share the EXACT same microsecond applied_at and overlap on R0.
    # B1 is inserted first (lower seq); B2 second (higher seq → the newer batch).
    same_ts = _dt.datetime(2026, 3, 3, 9, 0, 0, 123456, tzinfo=_dt.timezone.utc)
    b1, b2 = str(uuid.uuid4()), str(uuid.uuid4())
    _seed_batch(
        pg_conn, batch_id=b1, applied_at=same_ts,
        records=[(_res_id(0), "properties.allowBlobPublicAccess", None, True)],
    )
    _seed_batch(  # same instant, ACTIVE, overlaps R0, HIGHER seq → newer
        pg_conn, batch_id=b2, applied_at=same_ts,
        records=[(_res_id(0), "properties.minimumTlsVersion", None, "TLS1_0")],
    )

    # 1) Reverting the older B1 FIRST is rejected (B2 is a newer active overlap).
    res = _revert("--batch-id", b1)
    assert res.exit_code != 0, (res.output, res.exception)
    with pg_conn.cursor() as cur:
        cur.execute("SELECT reverted_at FROM synthetic.drift_batches WHERE batch_id=%s", (b1,))
        assert cur.fetchone()[0] is None  # untouched (guard tripped before mutation)
    pg_conn.commit()  # release the AccessShareLock before the next in-process CLI call

    # 2) Reverting the NEWER B2 succeeds (no deadlock — the prior >= guard rejected
    #    this too because B1 shared the same applied_at).
    res = _revert("--batch-id", b2)
    assert res.exit_code == 0, (res.output, res.exception)

    # 3) The older B1 is now revertable — its only overlapping sibling is reverted.
    res = _revert("--batch-id", b1)
    assert res.exit_code == 0, (res.output, res.exception)
    with pg_conn.cursor() as cur:
        cur.execute("SELECT reverted_at FROM synthetic.drift_batches WHERE batch_id=%s", (b1,))
        assert cur.fetchone()[0] is not None
    pg_conn.commit()


def test_lifo_allows_latest(pg_conn):
    """Reverting the NEWEST batch is always allowed — there is no newer active
    batch that could be corrupted (D-06)."""
    _seed_storage(pg_conn, count=3)
    b1, b2 = str(uuid.uuid4()), str(uuid.uuid4())
    _seed_batch(
        pg_conn, batch_id=b1, applied_at=_T1,
        records=[(_res_id(0), "properties.allowBlobPublicAccess", None, True)],
    )
    _seed_batch(  # newest, overlaps R0 — but B2 itself has no newer active batch
        pg_conn, batch_id=b2, applied_at=_T2,
        records=[(_res_id(0), "properties.minimumTlsVersion", None, "TLS1_0")],
    )

    res = _revert("--batch-id", b2)
    assert res.exit_code == 0, (res.output, res.exception)


# --------------------------------------------------------------------------- #
# 11-10 P1 — concurrent drift commands lose updates. apply-drift and revert-drift
# do read-modify-write over JSONB columns; without serialization two concurrent
# commands read the same parent state and overwrite with stale snapshots. Both
# take a transaction-level Postgres advisory lock on a FIXED application-wide key
# at the very start of the mutation transaction. These tests prove the lock is
# real: while a separate session holds DRIFT_LOCK_KEY, the command BLOCKS, then
# completes once the lock is released.
# --------------------------------------------------------------------------- #


def _assert_serialized_by_drift_lock(invoke):
    """A drift mutation command must BLOCK while DRIFT_LOCK_KEY is held by another
    session, then complete (exit 0) once it is released (P1, 11-10)."""
    import threading

    import psycopg

    from tenantless.cli import DRIFT_LOCK_KEY

    blocker = psycopg.connect(DATABASE_URL, autocommit=True)
    try:
        with blocker.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (DRIFT_LOCK_KEY,))

        result: dict = {}

        def _run():
            result["res"] = invoke()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        # The command must still be waiting on the advisory lock (it cannot make
        # progress past pg_advisory_xact_lock while we hold the key).
        t.join(timeout=3.0)
        assert t.is_alive(), "command did not block on the drift advisory lock"

        # Release the key — the command acquires the xact lock and finishes.
        with blocker.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (DRIFT_LOCK_KEY,))
        t.join(timeout=30.0)
        assert not t.is_alive(), "command did not finish after the lock was released"
        assert result["res"].exit_code == 0, (
            result["res"].output,
            result["res"].exception,
        )
    finally:
        blocker.close()


def test_apply_serialized_by_advisory_lock(pg_conn):
    """apply-drift takes pg_advisory_xact_lock(DRIFT_LOCK_KEY) before its read."""
    _seed_storage(pg_conn, count=3)
    pg_conn.commit()  # release locks before the in-process CLI invocation
    _assert_serialized_by_drift_lock(
        lambda: _apply("--type", "chaos", "--intensity", "1.0")
    )


def test_revert_serialized_by_advisory_lock(pg_conn):
    """revert-drift takes pg_advisory_xact_lock(DRIFT_LOCK_KEY) before its read."""
    _seed_storage(pg_conn, count=3)
    b1 = str(uuid.uuid4())
    _seed_batch(
        pg_conn, batch_id=b1, applied_at=_T1,
        records=[(_res_id(0), "properties.allowBlobPublicAccess", None, True)],
    )
    pg_conn.commit()  # release locks before the in-process CLI invocation
    _assert_serialized_by_drift_lock(lambda: _revert("--batch-id", b1))


# --------------------------------------------------------------------------- #
# Task 3 (plan Task 2) — single-transaction restore + unhide/delete + mark
# reverted_at (never delete history) + dry-run (D-02/03/04/13).
# --------------------------------------------------------------------------- #


def test_restore_from_before(pg_conn):
    """After revert, every affected resource's served column is restored to its
    pre-drift value (the recorded per-field before); the served shape is back to
    the original (D-02/D-04)."""
    _seed_storage(pg_conn, count=3)
    res = _apply("--type", "chaos", "--intensity", "1.0")
    assert res.exit_code == 0, (res.output, res.exception)
    bid = _only_batch_id(pg_conn)

    # Pre-revert: the drift is live in the served column.
    with pg_conn.cursor() as cur:
        cur.execute("SELECT properties FROM synthetic.resources WHERE id=%s", (_res_id(0),))
        assert cur.fetchone()[0]["allowBlobPublicAccess"] is True
    pg_conn.commit()  # release locks before the in-process CLI invocation

    r = _revert("--batch-id", bid)
    assert r.exit_code == 0, (r.output, r.exception)

    pg_conn.commit()
    with pg_conn.cursor() as cur:
        # Every storage account is back to its empty pre-drift properties.
        cur.execute("SELECT properties FROM synthetic.resources ORDER BY id")
        for (props,) in cur.fetchall():
            assert props == {}
        # The batch is marked reverted.
        cur.execute("SELECT reverted_at FROM synthetic.drift_batches WHERE batch_id=%s", (bid,))
        assert cur.fetchone()[0] is not None


def test_unhide_disappeared(pg_conn):
    """Revert clears drift_deleted_at on disappeared resources (unhide, D-13)."""
    _seed_storage(pg_conn, count=3)
    res = _apply("--type", "temporal", "--intensity", "1.0")
    assert res.exit_code == 0, (res.output, res.exception)
    bid = _only_batch_id(pg_conn)

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM synthetic.resources WHERE drift_deleted_at IS NOT NULL"
        )
        assert cur.fetchone()[0] == 3  # all 3 leaves hidden by the temporal apply
    pg_conn.commit()  # release locks before the in-process CLI invocation

    r = _revert("--batch-id", bid)
    assert r.exit_code == 0, (r.output, r.exception)

    pg_conn.commit()
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM synthetic.resources WHERE drift_deleted_at IS NOT NULL"
        )
        assert cur.fetchone()[0] == 0  # all unhidden


def test_delete_appeared(pg_conn):
    """Revert DELETEs the rows a batch added via appear (D-13)."""
    _seed_storage(pg_conn, count=3)
    res = _apply("--type", "temporal", "--intensity", "1.0")
    assert res.exit_code == 0, (res.output, res.exception)
    bid = _only_batch_id(pg_conn)

    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM synthetic.resources")
        assert cur.fetchone()[0] == 6  # 3 original + 3 appeared
    pg_conn.commit()  # release locks before the in-process CLI invocation

    r = _revert("--batch-id", bid)
    assert r.exit_code == 0, (r.output, r.exception)

    pg_conn.commit()
    with pg_conn.cursor() as cur:
        # appear rows deleted; the 3 disappeared originals are unhidden -> back to 3.
        cur.execute("SELECT count(*) FROM synthetic.resources")
        assert cur.fetchone()[0] == 3
        # none of the @appear ids survive.
        cur.execute(
            "SELECT resource_id FROM synthetic.drift_records WHERE field_path='@appear'"
        )
        for (aid,) in cur.fetchall():
            cur.execute("SELECT count(*) FROM synthetic.resources WHERE id=%s", (aid,))
            assert cur.fetchone()[0] == 0


def test_mark_not_delete(pg_conn):
    """Revert marks reverted_at non-NULL AND preserves all drift_records (D-03)."""
    _seed_storage(pg_conn, count=3)
    res = _apply("--type", "chaos", "--intensity", "1.0")
    assert res.exit_code == 0, (res.output, res.exception)
    bid = _only_batch_id(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM synthetic.drift_records WHERE batch_id=%s", (bid,))
        before_count = cur.fetchone()[0]
    pg_conn.commit()  # release locks before the in-process CLI invocation
    assert before_count == 9

    r = _revert("--batch-id", bid)
    assert r.exit_code == 0, (r.output, r.exception)

    pg_conn.commit()
    with pg_conn.cursor() as cur:
        cur.execute("SELECT reverted_at FROM synthetic.drift_batches WHERE batch_id=%s", (bid,))
        assert cur.fetchone()[0] is not None
        # History preserved — every drift_record row still present.
        cur.execute("SELECT count(*) FROM synthetic.drift_records WHERE batch_id=%s", (bid,))
        assert cur.fetchone()[0] == before_count


def test_revert_dry_run(pg_conn):
    """--dry-run reports the would-revert count and mutates NOTHING: reverted_at
    stays NULL and the drifted columns are unchanged (D-04)."""
    _seed_storage(pg_conn, count=3)
    res = _apply("--type", "chaos", "--intensity", "1.0")
    assert res.exit_code == 0, (res.output, res.exception)
    bid = _only_batch_id(pg_conn)

    r = _revert("--batch-id", bid, "--dry-run")
    assert r.exit_code == 0, (r.output, r.exception)
    assert "would revert 9" in r.output  # the count is reported (not silent)

    pg_conn.commit()
    with pg_conn.cursor() as cur:
        # Nothing marked, nothing restored.
        cur.execute("SELECT reverted_at FROM synthetic.drift_batches WHERE batch_id=%s", (bid,))
        assert cur.fetchone()[0] is None
        cur.execute("SELECT properties FROM synthetic.resources WHERE id=%s", (_res_id(0),))
        assert cur.fetchone()[0]["allowBlobPublicAccess"] is True
