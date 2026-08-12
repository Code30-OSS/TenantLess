"""revert-drift CLI tests (overlay reconcile).

``tenantless revert-drift --batch-id <uuid> [--dry-run]`` is a single-transaction
recompute-from-ledger. The overlay migration retired in-place ``synthetic.resources`` restore:
revert rebuilds each affected id from the IMMUTABLE baseline by replaying every
still-active overlay batch except the target, then DELETE-if-baseline (no zombie)
else UPSERT a fresh ``source='drift'`` overlay snapshot/tombstone, and marks the
batch ``reverted_at`` WITHOUT deleting history. The strict-LIFO overlap
guard is GONE — ANY batch is independently revertable. Disappear
revert removes the overlay tombstone; @appear revert DELETEs the overlay row.
``synthetic.resources`` is NEVER mutated. ``--dry-run`` mutates nothing.

This file ALSO pins the apply-side temporal lifecycle wiring carried forward from
the apply-drift tests: ``apply-drift --type temporal`` must PRODUCE the disappear/appear
``drift_records`` (and overlay rows) revert consumes.

DB-backed tests use the project ``pg_conn`` skip fixture so DB-less CI skips
clean. The canonical NEW-behaviour coverage lives in
``tests/test_drift_overlay_revert.py``.
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

    ``autocommit=True`` is REQUIRED (mirrors
    ``tests/test_drift_overlay_revert.py``): a non-autocommit psycopg3 connection
    keeps a transaction open after every ``SELECT``, so the overlay/baseline reads
    below would leave this connection idle-in-transaction holding ACCESS SHARE on
    ``synthetic.resources``. ``apply-drift`` / ``revert-drift`` (invoked in-process)
    run their idempotent schema-ensure preflight, whose DDL needs ACCESS EXCLUSIVE —
    they would DEADLOCK behind that stray read lock (the server-startup ALTER-lock
    fragility). Autocommit means our reads hold no lock, so the preflight never blocks.
    """
    psycopg = pytest.importorskip("psycopg")
    try:
        conn = psycopg.connect(DATABASE_URL, connect_timeout=3, autocommit=True)
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
    writer.ensure_arm_overlay_schema(conn)
    writer.ensure_arm_resolver_schema(conn)
    writer.truncate_synthetic(conn)
    with conn.cursor() as cur:
        # arm_overlay is mutable overlay state, EXCLUDED from truncate_synthetic
        # (_SYNTHETIC_TABLES), so clear it explicitly — else overlay rows from a
        # prior test leak into this one's assertions.
        cur.execute("DELETE FROM synthetic.arm_overlay")
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


def _overlay_by_id(conn):
    """The arm_overlay resource rows keyed by id (mirrors test_drift_overlay_revert.py)."""
    conn.commit()  # fresh snapshot (no-op under autocommit)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, present, body, source, revision FROM synthetic.arm_overlay "
            "WHERE target_kind = 'resource'"
        )
        cols = ("id", "present", "body", "source", "revision")
        return {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}


# --------------------------------------------------------------------------- #
# apply-side temporal lifecycle wiring.
# revert's unhide/delete needs a real PRODUCER: apply-drift --type
# temporal must compute compute_lifecycle and persist disappear (drift_deleted_at
# set + record) / appear (new leaf row + @appear record).
# --------------------------------------------------------------------------- #


def test_temporal_lifecycle_records(pg_conn):
    """apply-drift --type temporal produces appear/disappear drift_records AND writes
    the lifecycle to arm_overlay: disappear → present=false
    tombstones (source='drift'), @appear → present=true overlay rows for the minted
    leaves. synthetic.resources is NEVER mutated in place — no soft-delete, no minted
    baseline row — so revert's overlay recompute has a real producer."""
    _seed_storage(pg_conn, count=3)

    res = _apply("--type", "temporal", "--intensity", "1.0")
    assert res.exit_code == 0, (res.output, res.exception)

    with pg_conn.cursor() as cur:
        # 3 eligible leaves at intensity 1.0 -> 3 disappear + 3 appear records.
        cur.execute(
            "SELECT count(*) FROM synthetic.drift_records "
            "WHERE field_path = 'drift_deleted_at'"
        )
        assert cur.fetchone()[0] == 3
        cur.execute(
            "SELECT count(*) FROM synthetic.drift_records WHERE field_path = '@appear'"
        )
        assert cur.fetchone()[0] == 3
        cur.execute(
            "SELECT resource_id FROM synthetic.drift_records WHERE field_path='@appear'"
        )
        appeared = {r[0] for r in cur.fetchall()}

    # Overlay carries the lifecycle: 3 tombstones (disappeared originals) + a present
    # source='drift' row per minted appear leaf.
    ov = _overlay_by_id(pg_conn)
    tombstones = {rid for rid, r in ov.items() if r["present"] is False}
    present_rows = {rid for rid, r in ov.items() if r["present"] is True}
    assert len(tombstones) == 3
    assert appeared <= present_rows
    for r in ov.values():
        assert r["source"] == "drift"

    # Baseline never mutated in place: no soft-delete, no minted row, and
    # each minted @appear leaf lives ONLY in the overlay (absent from synthetic.resources).
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM synthetic.resources "
            "WHERE drift_deleted_at IS NOT NULL"
        )
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM synthetic.resources")
        assert cur.fetchone()[0] == 3
        for aid in appeared:
            cur.execute("SELECT count(*) FROM synthetic.resources WHERE id = %s", (aid,))
            assert cur.fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# The strict-LIFO overlap guard is REMOVED.
# Recompute-from-ledger rebuilds each affected id's overlay from the immutable
# baseline by replaying whatever active batches remain, so ANY batch — including
# a middle batch under a newer active overlapping batch — is independently
# revertable. The four legacy LIFO tests that asserted the removed guard (reject
# / same-instant-deadlock / non-overlap-permit / latest-permit) were removed:
# their reject cases contradict the new model, and their permit cases are
# now vacuous ("revert succeeds") and are covered by the harder OVERLAPPING case
# in tests/test_drift_overlay_revert.py::test_middle_batch_revert_permitted.
# --------------------------------------------------------------------------- #

# Retained: used by test_revert_serialized_by_advisory_lock (below) to stamp a
# deterministic applied_at on a hand-seeded batch.
_T1 = _dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)


# --------------------------------------------------------------------------- #
# Concurrent drift commands lose updates. apply-drift and revert-drift
# do read-modify-write over JSONB columns; without serialization two concurrent
# commands read the same parent state and overwrite with stale snapshots. Both
# take a transaction-level Postgres advisory lock on a FIXED application-wide key
# at the very start of the mutation transaction. These tests prove the lock is
# real: while a separate session holds DRIFT_LOCK_KEY, the command BLOCKS, then
# completes once the lock is released.
# --------------------------------------------------------------------------- #


def _assert_serialized_by_drift_lock(invoke):
    """A drift mutation command must BLOCK while DRIFT_LOCK_KEY is held by another
    session, then complete (exit 0) once it is released."""
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
# single-transaction recompute-from-ledger onto arm_overlay:
# overlay DELETE-if-baseline / tombstone-remove / @appear-delete +
# mark reverted_at (never delete history) + dry-run; synthetic.resources untouched.
# --------------------------------------------------------------------------- #


def test_restore_from_before(pg_conn):
    """After revert, the drift is gone: recompute-from-ledger rebuilds each affected
    id from the immutable baseline; with no other active batch the replayed result
    EQUALS baseline so the overlay row is DELETED (no zombie). The
    synthetic.resources baseline was never mutated (pristine {}), and the batch is
    marked reverted_at."""
    _seed_storage(pg_conn, count=3)
    res = _apply("--type", "chaos", "--intensity", "1.0")
    assert res.exit_code == 0, (res.output, res.exception)
    bid = _only_batch_id(pg_conn)

    # Pre-revert: the drift is live in the OVERLAY body (baseline untouched).
    ov = _overlay_by_id(pg_conn)
    assert ov[_res_id(0)]["body"]["properties"]["allowBlobPublicAccess"] is True

    r = _revert("--batch-id", bid)
    assert r.exit_code == 0, (r.output, r.exception)

    # Post-revert: the overlay rows are DELETED back to baseline (no zombie).
    assert _overlay_by_id(pg_conn) == {}
    with pg_conn.cursor() as cur:
        # The baseline was never mutated in place — still empty properties.
        cur.execute("SELECT properties FROM synthetic.resources ORDER BY id")
        for (props,) in cur.fetchall():
            assert props == {}
        # The batch is marked reverted (history preserved).
        cur.execute("SELECT reverted_at FROM synthetic.drift_batches WHERE batch_id=%s", (bid,))
        assert cur.fetchone()[0] is not None


def test_unhide_disappeared(pg_conn):
    """Revert REMOVES the overlay tombstone for each disappeared id — the id is live
    again via the baseline. It does NOT clear synthetic.resources.drift_deleted_at
    (never set in place under the overlay model)."""
    _seed_storage(pg_conn, count=3)
    res = _apply("--type", "temporal", "--intensity", "1.0")
    assert res.exit_code == 0, (res.output, res.exception)
    bid = _only_batch_id(pg_conn)

    # The temporal apply hid all 3 leaves as overlay tombstones (NOT in-place).
    ov = _overlay_by_id(pg_conn)
    tombstoned = [rid for rid, r in ov.items() if r["present"] is False]
    assert len(tombstoned) == 3
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM synthetic.resources WHERE drift_deleted_at IS NOT NULL"
        )
        assert cur.fetchone()[0] == 0  # baseline never soft-deleted in place

    r = _revert("--batch-id", bid)
    assert r.exit_code == 0, (r.output, r.exception)

    # Every disappear tombstone is removed → the id resolves live via baseline again.
    after = _overlay_by_id(pg_conn)
    for rid in tombstoned:
        assert rid not in after


def test_delete_appeared(pg_conn):
    """Revert DELETEs the overlay row a batch added via @appear; the baseline
    never held the minted leaf (count stays 3) and the appear id no longer resolves
    via the overlay."""
    _seed_storage(pg_conn, count=3)
    res = _apply("--type", "temporal", "--intensity", "1.0")
    assert res.exit_code == 0, (res.output, res.exception)
    bid = _only_batch_id(pg_conn)

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT resource_id FROM synthetic.drift_records WHERE field_path='@appear'"
        )
        appeared = [r[0] for r in cur.fetchall()]
        assert appeared
        # The minted leaves were written to the overlay only — NOT to the baseline.
        cur.execute("SELECT count(*) FROM synthetic.resources")
        assert cur.fetchone()[0] == 3
    ov = _overlay_by_id(pg_conn)
    for aid in appeared:
        assert ov[aid]["present"] is True

    r = _revert("--batch-id", bid)
    assert r.exit_code == 0, (r.output, r.exception)

    # Each @appear overlay row is DELETED; baseline still never gains the leaf.
    after = _overlay_by_id(pg_conn)
    for aid in appeared:
        assert aid not in after
        with pg_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM synthetic.resources WHERE id=%s", (aid,))
            assert cur.fetchone()[0] == 0
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM synthetic.resources")
        assert cur.fetchone()[0] == 3


def test_mark_not_delete(pg_conn):
    """Revert marks reverted_at non-NULL AND preserves all drift_records — the
    ledger history is never deleted. synthetic.resources is untouched throughout:
    recompute writes only the overlay + the reverted_at mark."""
    _seed_storage(pg_conn, count=3)
    res = _apply("--type", "chaos", "--intensity", "1.0")
    assert res.exit_code == 0, (res.output, res.exception)
    bid = _only_batch_id(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM synthetic.drift_records WHERE batch_id=%s", (bid,))
        before_count = cur.fetchone()[0]
    assert before_count == 9
    # Snapshot the baseline to prove revert never mutates synthetic.resources.
    with pg_conn.cursor() as cur:
        cur.execute("SELECT id, tags, sku, kind, properties FROM synthetic.resources ORDER BY id")
        baseline_before = cur.fetchall()

    r = _revert("--batch-id", bid)
    assert r.exit_code == 0, (r.output, r.exception)

    with pg_conn.cursor() as cur:
        cur.execute("SELECT reverted_at FROM synthetic.drift_batches WHERE batch_id=%s", (bid,))
        assert cur.fetchone()[0] is not None
        # History preserved — every drift_record row still present.
        cur.execute("SELECT count(*) FROM synthetic.drift_records WHERE batch_id=%s", (bid,))
        assert cur.fetchone()[0] == before_count
        # Baseline byte-identical before/after revert (never mutated in place).
        cur.execute("SELECT id, tags, sku, kind, properties FROM synthetic.resources ORDER BY id")
        assert cur.fetchall() == baseline_before


def test_revert_dry_run(pg_conn):
    """--dry-run reports the would-revert count and writes NO overlay change:
    reverted_at stays NULL and the overlay snapshot the apply wrote is byte-identical
    afterwards."""
    _seed_storage(pg_conn, count=3)
    res = _apply("--type", "chaos", "--intensity", "1.0")
    assert res.exit_code == 0, (res.output, res.exception)
    bid = _only_batch_id(pg_conn)

    before_overlay = _overlay_by_id(pg_conn)
    assert before_overlay  # apply wrote overlay rows

    r = _revert("--batch-id", bid, "--dry-run")
    assert r.exit_code == 0, (r.output, r.exception)
    assert "would revert 9" in r.output  # the count is reported (not silent)

    with pg_conn.cursor() as cur:
        # Nothing marked, nothing recomputed.
        cur.execute("SELECT reverted_at FROM synthetic.drift_batches WHERE batch_id=%s", (bid,))
        assert cur.fetchone()[0] is None
    # The overlay is byte-identical (dry-run mutated nothing).
    assert _overlay_by_id(pg_conn) == before_overlay
