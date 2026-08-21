"""D-04 user-ownership latch: drift yields to source='user' overlay rows.

Phase 23 introduces the ARM *write* plane — an accepted PUT/PATCH/DELETE takes
OWNERSHIP of an overlay row by flipping ``source='user'`` and becomes
authoritative. Configuration drift (``apply-drift`` / ``revert-drift``) is the
overlay's OTHER writer, and STATE-03 / D-04 make user ownership a ONE-WAY latch:
once a row is ``source='user'`` neither drift path may overwrite or tombstone it.
Mental model: hand-editing a resource DETACHES it from drift simulation.

The guard is two clauses in ``src/tenantless/cli.py``:
  * ``WHERE synthetic.arm_overlay.source <> 'user'`` on the ON CONFLICT DO UPDATE
    of ``_OVERLAY_UPSERT_SQL`` (used by apply AND revert upserts) — a drift upsert
    onto a user-owned id is a NO-OP (row not rewritten, revision not bumped: the
    BEFORE UPDATE trigger does not fire when the conflict-update WHERE is false);
  * ``AND source <> 'user'`` on ``revert_drift``'s from-baseline recompute DELETE
    — a user PUT/DELETE survives revert untouched (never deleted/tombstoned).

These proofs seed a ``source='user'`` present row AND a ``source='user'``
tombstone DIRECTLY via SQL (no Rust write handler needed — the handlers are exercised separately),
then run the real ``apply-drift`` / ``revert-drift`` over the SAME ids and assert
the user rows are byte-unchanged and un-revisioned while drift-owned ids move
normally.

NOTE (platform): the DB-backed proofs run on the Linux PG16 container gate; the
``pg_conn`` fixture skips clean when Postgres on :5433 is unavailable (native
``uv run pytest`` on the Windows dev host cannot host the advisory-lock drift
path — memory ``drift-tests-autocommit-and-gate-recipe``).
"""

from __future__ import annotations

import os
import uuid

import pytest

from tenantless.cli import main
from tenantless.generator import resources

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgres://tenantless:tenantless_dev@localhost:5433/tenantless",
)

_TENANT = str(uuid.UUID(int=0x1))
_SUB = str(uuid.UUID(int=0x11))
_RG = "rg-latch-test"


@pytest.fixture
def pg_conn():
    """Yield a live psycopg connection, or skip if Postgres is unavailable.

    ``autocommit=True`` is REQUIRED (memory ``drift-tests-autocommit-and-gate-recipe``):
    a non-autocommit psycopg3 connection keeps a transaction open after every
    SELECT, leaving this connection idle-in-transaction holding ACCESS SHARE on
    ``synthetic.resources``; the in-process ``apply-drift`` / ``revert-drift`` then
    DEADLOCKS when its idempotent schema-ensure preflight needs ACCESS EXCLUSIVE.
    Autocommit means our reads hold no lock, so the ensure preflight never blocks.
    """
    psycopg = pytest.importorskip("psycopg")
    try:
        conn = psycopg.connect(DATABASE_URL, connect_timeout=3, autocommit=True)
    except Exception as exc:  # noqa: BLE001 - any connection failure → skip
        pytest.skip(f"Postgres on 5433 unavailable: {exc}")
    try:
        # Hardened container-gate recipe: bound any lock wait + idle-in-txn so a
        # stray hold can never wedge the drift advisory-lock path.
        with conn.cursor() as cur:
            cur.execute("SET lock_timeout = '5s'")
            cur.execute("SET idle_in_transaction_session_timeout = '10s'")
        yield conn
    finally:
        conn.close()


def _rid(name: str, type_key: str, *, sub: str = _SUB, rg: str = _RG) -> str:
    return f"/subscriptions/{sub}/resourceGroups/{rg}/providers/{type_key}/{name}"


def _seed_baseline(conn, specs, *, sub: str = _SUB):
    """Truncate the synthetic schema + overlay and insert the given resources."""
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
            (_TENANT, "latch-test", "1.0", Jsonb({})),
        )
        cur.execute(
            "INSERT INTO synthetic.subscriptions "
            "(subscription_id, tenant_id, display_name, state, archetype, tags, "
            "authorization_source, spending_limit) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (sub, _TENANT, "sub", "Enabled", "test", Jsonb({}), "RoleBased", "On"),
        )
        for s in specs:
            cur.execute(
                "INSERT INTO synthetic.resources "
                "(id, subscription_id, resource_group_name, name, type, location, "
                "tags, sku, kind, properties, provisioning_state, managed_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    _rid(s["name"], s["type"], sub=sub),
                    sub,
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
    conn.commit()


def _user_body(name: str, type_key: str) -> dict:
    """A CHECK-valid (sql/009) present overlay body with DISTINCTIVE user markers.

    ``tags.owner`` + ``properties.userWrite`` are values drift never produces, so a
    byte-equality assertion catches ANY drift overwrite. NO ``allowBlobPublicAccess``
    property — the DRIFT_STORAGE_PUBLIC_ACCESS code would inject exactly that key if
    the guard leaked."""
    return {
        "id": _rid(name, type_key),
        "name": name,
        "type": type_key,
        "location": "eastus",
        "tags": {"owner": "hand-edit"},
        "properties": {"userWrite": True, "provisioningState": "Succeeded"},
    }


def _seed_user_overlay(conn, rid: str, *, present: bool, body: dict | None) -> None:
    """Directly UPSERT a source='user' overlay row (present snapshot or tombstone).

    Simulates the Phase-23 write plane without a Rust handler. The sql/009 BEFORE
    trigger assigns the revision on INSERT."""
    from psycopg.types.json import Jsonb

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO synthetic.arm_overlay "
            "(id_lower, id, target_kind, source, present, body) "
            "VALUES (lower(%s), %s, 'resource', 'user', %s, %s) "
            "ON CONFLICT (id_lower) DO UPDATE SET "
            "source = 'user', present = EXCLUDED.present, body = EXCLUDED.body",
            (rid, rid, present, Jsonb(body) if body is not None else None),
        )
    conn.commit()


def _invoke_apply(*args):
    from click.testing import CliRunner

    runner = CliRunner()
    return runner.invoke(main, ["apply-drift", "--database-url", DATABASE_URL, *args])


def _invoke_revert(batch_id, *args):
    from click.testing import CliRunner

    runner = CliRunner()
    return runner.invoke(
        main,
        ["revert-drift", "--database-url", DATABASE_URL, "--batch-id", str(batch_id), *args],
    )


def _batch_ids_in_seq_order(conn):
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT batch_id FROM synthetic.drift_batches ORDER BY seq")
        return [r[0] for r in cur.fetchall()]


def _overlay_by_id(conn):
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, present, body, source, revision FROM synthetic.arm_overlay "
            "WHERE target_kind = 'resource'"
        )
        cols = ("id", "present", "body", "source", "revision")
        return {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}


# --------------------------------------------------------------------------- #
# Test A: apply-drift yields to source='user' rows (present + tombstone).
# --------------------------------------------------------------------------- #


def test_apply_yields_to_user_owned_rows(pg_conn):
    """apply-drift over ids that INCLUDE a user-owned present row AND a user-owned
    tombstone leaves BOTH byte-unchanged and un-revisioned (source='user',
    original body/present intact — the drift upsert is a no-op), while a
    drift-eligible id gets a fresh source='drift' row (drift still works for
    non-user ids)."""
    up_name, tomb_name, drift_name = "userpresent", "usertomb", "driftme"
    _seed_baseline(
        pg_conn,
        [
            {"name": up_name, "type": resources.T_STORAGE},
            {"name": tomb_name, "type": resources.T_STORAGE},
            {"name": drift_name, "type": resources.T_STORAGE},
        ],
    )
    up_id = _rid(up_name, resources.T_STORAGE)
    tomb_id = _rid(tomb_name, resources.T_STORAGE)
    drift_id = _rid(drift_name, resources.T_STORAGE)

    # Seed the two user-owned overlay rows (present snapshot + tombstone).
    up_body = _user_body(up_name, resources.T_STORAGE)
    _seed_user_overlay(pg_conn, up_id, present=True, body=up_body)
    _seed_user_overlay(pg_conn, tomb_id, present=False, body=None)

    before = _overlay_by_id(pg_conn)
    up_rev_before = before[up_id]["revision"]
    tomb_rev_before = before[tomb_id]["revision"]

    # apply-drift touches EVERY storage id (baseline read) → attempts to upsert
    # all three; the guard makes the two user ids no-ops.
    res = _invoke_apply(
        "--type", "chaos", "--codes", "DRIFT_STORAGE_PUBLIC_ACCESS", "--intensity", "1.0"
    )
    assert res.exit_code == 0, (res.output, res.exception)

    after = _overlay_by_id(pg_conn)

    # User PRESENT row: unchanged authority, body, present, AND revision.
    assert after[up_id]["source"] == "user"
    assert after[up_id]["present"] is True
    assert after[up_id]["body"] == up_body, "drift overwrote the user body"
    assert "allowBlobPublicAccess" not in after[up_id]["body"]["properties"]
    assert after[up_id]["revision"] == up_rev_before, "drift bumped the user revision"

    # User TOMBSTONE: still a user-owned tombstone, un-revisioned.
    assert after[tomb_id]["source"] == "user"
    assert after[tomb_id]["present"] is False
    assert after[tomb_id]["body"] is None
    assert after[tomb_id]["revision"] == tomb_rev_before

    # Drift-owned id moved normally → source='drift' present row with the code's mark.
    assert drift_id in after
    assert after[drift_id]["source"] == "drift"
    assert after[drift_id]["present"] is True
    assert after[drift_id]["body"]["properties"].get("allowBlobPublicAccess") is True


# --------------------------------------------------------------------------- #
# Test B: revert-drift's from-baseline recompute yields to source='user' rows.
# --------------------------------------------------------------------------- #


def test_revert_preserves_user_owned_rows(pg_conn):
    """After a drift batch, a user write takes ownership of two ids (a present row
    and a tombstone). Reverting that batch recomputes-from-baseline every affected
    id; the guard makes both the DELETE-if-baseline and the else-UPSERT a no-op for
    user-owned ids, so the user present row and the user tombstone SURVIVE
    (never deleted/overwritten, revision unchanged) while a drift-only id reverts
    normally (its overlay row deleted back to baseline)."""
    up_name, tomb_name, drift_name = "userpresent", "usertomb", "driftonly"
    _seed_baseline(
        pg_conn,
        [
            {"name": up_name, "type": resources.T_STORAGE},
            {"name": tomb_name, "type": resources.T_STORAGE},
            {"name": drift_name, "type": resources.T_STORAGE},
        ],
    )
    up_id = _rid(up_name, resources.T_STORAGE)
    tomb_id = _rid(tomb_name, resources.T_STORAGE)
    drift_id = _rid(drift_name, resources.T_STORAGE)

    # Apply a batch — writes source='drift' overlay rows for ALL storage ids.
    assert _invoke_apply(
        "--type", "chaos", "--codes", "DRIFT_STORAGE_PUBLIC_ACCESS", "--intensity", "1.0"
    ).exit_code == 0
    (batch,) = _batch_ids_in_seq_order(pg_conn)
    assert drift_id in _overlay_by_id(pg_conn)

    # NOW a user write takes ownership of two of those ids (over-writing the drift
    # rows): one present snapshot, one tombstone.
    up_body = _user_body(up_name, resources.T_STORAGE)
    _seed_user_overlay(pg_conn, up_id, present=True, body=up_body)
    _seed_user_overlay(pg_conn, tomb_id, present=False, body=None)

    before = _overlay_by_id(pg_conn)
    up_rev_before = before[up_id]["revision"]
    tomb_rev_before = before[tomb_id]["revision"]

    # Revert the batch: affected set includes ALL three ids.
    res = _invoke_revert(batch)
    assert res.exit_code == 0, (res.output, res.exception)

    after = _overlay_by_id(pg_conn)

    # User PRESENT row: preserved verbatim + un-revisioned (not deleted/overwritten).
    assert up_id in after, "revert deleted the user-owned present row"
    assert after[up_id]["source"] == "user"
    assert after[up_id]["present"] is True
    assert after[up_id]["body"] == up_body
    assert after[up_id]["revision"] == up_rev_before

    # User TOMBSTONE: preserved (not resurrected/deleted), un-revisioned.
    assert tomb_id in after, "revert deleted the user-owned tombstone"
    assert after[tomb_id]["source"] == "user"
    assert after[tomb_id]["present"] is False
    assert after[tomb_id]["revision"] == tomb_rev_before

    # Drift-only id reverts normally: lone batch → recompute equals baseline →
    # overlay row DELETED (no zombie).
    assert drift_id not in after, "drift-only overlay row should revert to baseline"


# --------------------------------------------------------------------------- #
# Test C: a user-owned row that drift TARGETS must not poison the fingerprint chain.
# --------------------------------------------------------------------------- #


def _batch_fps(conn, batch_id):
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT parent_fingerprint, result_fingerprint "
            "FROM synthetic.drift_batches WHERE batch_id = %s",
            (str(batch_id),),
        )
        return cur.fetchone()


def test_apply_result_fingerprint_chains_over_user_owned_row(pg_conn):
    """Drift's overlay upsert is a NO-OP on a source='user' row (D-04), so the row's
    SERVED state does not change. ``compute_drift`` still mutates that id's in-memory
    Resource, though — so folding the mutated values into ``result_fingerprint`` (or
    writing a drift_record for the skipped change) would describe a change that never
    persisted, and the digest would NOT match the next batch's ``parent_fingerprint``
    (read from the ACTUAL DB). This asserts the chain invariant
    ``parent_fp(batch2) == result_fp(batch1)`` holds ACROSS a user-owned target — the
    regression this guards is result_fp folding the un-persisted (phantom) mutation.
    """
    up_name, drift_name = "userpresent", "driftme"
    _seed_baseline(
        pg_conn,
        [
            {"name": up_name, "type": resources.T_STORAGE},
            {"name": drift_name, "type": resources.T_STORAGE},
        ],
    )
    up_id = _rid(up_name, resources.T_STORAGE)
    # A user write takes ownership of up_id BEFORE any drift runs.
    _seed_user_overlay(
        pg_conn, up_id, present=True, body=_user_body(up_name, resources.T_STORAGE)
    )

    # Two consecutive batches over EVERY storage id (both include the user-owned up_id,
    # a no-op for it). Same args → the second reads the live post-batch1 state as its parent.
    common_args = (
        "--type",
        "chaos",
        "--codes",
        "DRIFT_STORAGE_PUBLIC_ACCESS",
        "--intensity",
        "1.0",
    )
    assert _invoke_apply(*common_args).exit_code == 0
    assert _invoke_apply(*common_args).exit_code == 0

    b1, b2 = _batch_ids_in_seq_order(pg_conn)
    _parent_1, result_1 = _batch_fps(pg_conn, b1)
    parent_2, _result_2 = _batch_fps(pg_conn, b2)

    # The chain must be intact: batch2's parent (live DB) equals batch1's result. A phantom
    # drift mutation folded into result_1 over the user-owned row would break this equality.
    assert parent_2 == result_1, (
        "fingerprint chain broke: batch1 result_fp folded an un-persisted drift mutation "
        "over the user-owned row (parent_fp(batch2) != result_fp(batch1))"
    )


# --------------------------------------------------------------------------- #
# Test D: a temporal "appear" must not phantom over a user-owned tombstone.
# --------------------------------------------------------------------------- #


def _appeared_ids(conn):
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT resource_id FROM synthetic.drift_records WHERE field_path = '@appear'"
        )
        return sorted(row[0] for row in cur.fetchall())


def test_appear_does_not_phantom_over_user_tombstone(pg_conn):
    """A temporal ``appear`` mints new leaves, avoiding collisions only against the
    resolved-view ids (``seen_ids``). A user-owned TOMBSTONE is absent from that view,
    so a deterministic appear can mint onto a tombstoned id: the D-04 guard no-ops the
    overlay upsert (nothing persists), yet the phantom leaf would still enter
    ``result_fingerprint`` via ``minted_leaves`` and break the chain. The collision set
    now includes every overlay id INCLUDING tombstones, so appear mints a genuinely-fresh
    id instead — asserting ``parent_fp(next) == result_fp(collision batch)``.
    """
    specs = [{"name": f"s{i}", "type": resources.T_STORAGE} for i in range(3)]
    _seed_baseline(pg_conn, specs)

    # Phase 1 (seed 42, appear-only): discover which ids a deterministic appear mints.
    assert _invoke_apply(
        "--type", "temporal", "--codes", "DRIFT_APPEAR", "--intensity", "1.0", "--seed", "42"
    ).exit_code == 0
    minted = _appeared_ids(pg_conn)
    assert minted, "temporal appear minted at least one leaf"
    n_appear = len(minted)
    collide_id = minted[0]
    # Tombstone a CASE-VARIANT of the minted id (same id_lower, different bytes) so the test
    # exercises the case-INSENSITIVE collision path — a canonical mint candidate must be
    # blocked by a differently-cased tombstone, not just an exact-string one.
    collide_variant = collide_id.lower()
    assert collide_variant != collide_id, "the minted id must have canonical (mixed) casing"

    # Reset to the identical baseline, then the user DELETEs that id (as its lowercase
    # variant) → a source='user' tombstone (absent from the resolved view).
    _seed_baseline(pg_conn, specs)
    _seed_user_overlay(pg_conn, collide_variant, present=False, body=None)

    # Phase 2 (same seed 42): appear WANTS to mint collide_id again — now a case-variant
    # tombstone collision. Phase 3 (seed 43) reads the ACTUAL post-phase-2 state as its parent.
    assert _invoke_apply(
        "--type", "temporal", "--codes", "DRIFT_APPEAR", "--intensity", "1.0", "--seed", "42"
    ).exit_code == 0
    assert _invoke_apply(
        "--type", "temporal", "--codes", "DRIFT_APPEAR", "--intensity", "1.0", "--seed", "43"
    ).exit_code == 0

    batches = _batch_ids_in_seq_order(pg_conn)
    b_phase2, b_phase3 = batches[-2], batches[-1]
    _p2, result_2 = _batch_fps(pg_conn, b_phase2)
    parent_3, _r3 = _batch_fps(pg_conn, b_phase3)

    assert parent_3 == result_2, (
        "appear minted onto a case-variant user tombstone: the un-persisted phantom leaf "
        "entered result_fp and broke the chain (parent_fp(phase3) != result_fp(phase2))"
    )

    # The fix must mint a FULL set of FRESH leaves — not merely suppress the colliding one.
    # Phase 2 must persist the SAME number of @appear records as phase 1 (all at fresh ids).
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM synthetic.drift_records "
            "WHERE batch_id = %s AND field_path = '@appear'",
            (str(b_phase2),),
        )
        (phase2_appears,) = cur.fetchone()
    assert phase2_appears == n_appear, (
        f"appear must still mint {n_appear} fresh leaves under the collision, not suppress "
        f"one (got {phase2_appears})"
    )
    # None of phase 2's minted ids may reuse the tombstone's id_lower.
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT lower(resource_id) FROM synthetic.drift_records "
            "WHERE batch_id = %s AND field_path = '@appear'",
            (str(b_phase2),),
        )
        phase2_lowers = {row[0] for row in cur.fetchall()}
    assert collide_variant not in phase2_lowers, (
        "a fresh appear must NOT reuse the tombstoned id_lower"
    )

    # The user tombstone is never resurrected by the colliding appear.
    ov = _overlay_by_id(pg_conn)
    assert collide_variant in ov, "user tombstone must survive the colliding appear"
    assert ov[collide_variant]["source"] == "user"
    assert ov[collide_variant]["present"] is False
