"""revert-drift → arm_overlay recompute-from-ledger proofs.

The overlay migration moves the *revert* half of configuration drift onto
``synthetic.arm_overlay`` (recompute-from-ledger): the drift ledger
(``drift_batches`` + ``drift_records``) stays the authoritative per-batch delta
history and ``arm_overlay`` is materialized current state. On ``revert-drift
<batch>`` — under the drift advisory lock, in ONE transaction — each affected id
is rebuilt from the IMMUTABLE baseline (raw ``synthetic.resources``, NOT the
resolved view) by replaying every STILL-ACTIVE overlay batch EXCEPT the target
(``storage_mode='overlay' AND reverted_at IS NULL AND batch_id<>target``) in
``(seq, record_id)`` order (last-writer-wins per field), then:

  * DELETE the overlay row iff the replayed result equals baseline (no zombie);
  * else UPSERT a fresh ``source='drift'`` snapshot (present row or tombstone,
    revision advanced by the sql/009 trigger);
  * mark ``reverted_at`` on the target (history preserved, never deleted).

The strict-LIFO overlap guard is GONE (recompute-from-ledger obsoletes it):
any batch, including a MIDDLE batch under a newer active
overlapping batch, is independently revertable. ``synthetic.*`` is NEVER
mutated in place (baseline immutability true across apply AND revert).

Two test tiers:
  * DB-FREE unit tests of ``_apply_nested`` (the forward-replay twin of
    ``_revert_nested``) — composition + round-trip;
  * DB-backed integration tests (the ``pg_conn`` fixture skips clean when
    Postgres is unavailable) proving middle-batch revert, A,B,C→revert-B
    recompute, no-zombie DELETE, disappear/appear revert, idempotency, and
    baseline byte-immutability.

NOTE (platform): native ``uv run pytest`` HANGS on the Windows dev host
(fork-vs-spawn + :5433 advisory-lock residue); the DB-backed proofs run on the
Linux PG16 container gate.
"""

from __future__ import annotations

import os
import uuid

import pytest

from tenantless.cli import _apply_nested, _revert_nested, main
from tenantless.generator import resources

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgres://tenantless:tenantless_dev@localhost:5433/tenantless",
)

_TENANT = str(uuid.UUID(int=0x1))
_SUB = str(uuid.UUID(int=0x11))
_RG = "rg-drift-test"

_BASELINE_COLS = (
    "id, tags, sku, kind, properties, drift_deleted_at, "
    "name, type, location, subscription_id, resource_group_name"
)


# --------------------------------------------------------------------------- #
# DB-FREE unit tests: _apply_nested is the forward twin of _revert_nested.
# Replaying `after` forward then reverting `before` round-trips a field; the
# None/[] semantics mirror _revert_nested symmetrically.
# --------------------------------------------------------------------------- #


def test_apply_nested_sets_absent_key_then_revert_removes_it():
    """A field ABSENT pre-drift (before=None): _apply_nested sets `after`, and
    _revert_nested (before=None) removes it again — round-trip to {}."""
    applied = _apply_nested({}, "properties.allowBlobPublicAccess", None, True)
    assert applied == {"allowBlobPublicAccess": True}
    reverted = _revert_nested(applied, "properties.allowBlobPublicAccess", None, True)
    assert reverted == {}


def test_apply_nested_overwrites_present_key_then_revert_restores():
    """A field PRESENT pre-drift: _apply_nested writes `after`, _revert_nested
    restores `before` — round-trip to the original value."""
    start = {"minimumTlsVersion": "TLS1_2"}
    applied = _apply_nested(start, "properties.minimumTlsVersion", "TLS1_2", "TLS1_0")
    assert applied == {"minimumTlsVersion": "TLS1_0"}
    reverted = _revert_nested(
        applied, "properties.minimumTlsVersion", "TLS1_2", "TLS1_0"
    )
    assert reverted == {"minimumTlsVersion": "TLS1_2"}


def test_apply_nested_none_after_removes_key():
    """A tag-removal delta (after=None) forward-applies as a key removal — the
    symmetric inverse of _revert_nested's before=None removal (documented)."""
    applied = _apply_nested({"environment": "prod"}, "tags.environment", "prod", None)
    assert applied == {}


def test_apply_nested_append_then_revert_removes_element():
    """A `[]` append path: _apply_nested appends `after`, _revert_nested removes
    that appended element — round-trip preserving pre-existing elements."""
    start = {"ipRules": ["10.0.0.0/8"]}
    applied = _apply_nested(start, "properties.ipRules[]", None, "0.0.0.0/0")
    assert applied == {"ipRules": ["10.0.0.0/8", "0.0.0.0/0"]}
    reverted = _revert_nested(applied, "properties.ipRules[]", None, "0.0.0.0/0")
    assert reverted == {"ipRules": ["10.0.0.0/8"]}


def test_apply_nested_does_not_mutate_input():
    """_apply_nested returns a fresh container (no in-place aliasing
    of the caller's column value)."""
    start = {"a": 1}
    _apply_nested(start, "properties.b", None, 2)
    assert start == {"a": 1}


# --------------------------------------------------------------------------- #
# DB-backed integration fixtures / helpers (mirrors test_drift_overlay_apply.py).
# --------------------------------------------------------------------------- #


@pytest.fixture
def pg_conn():
    """Yield a live psycopg connection, or skip if Postgres is unavailable.

    ``autocommit=True`` is REQUIRED: a non-autocommit psycopg3 connection keeps a
    transaction open after every ``SELECT``, so the baseline/overlay reads below
    would leave this connection idle-in-transaction holding ACCESS SHARE on
    ``synthetic.resources``. ``revert-drift`` (invoked in-process) runs its
    idempotent schema-ensure preflight, whose DDL needs ACCESS EXCLUSIVE — it
    would DEADLOCK behind that stray read lock (the server-startup ALTER-lock
    fragility). Autocommit means our reads hold no lock, so the
    ensure preflight never blocks.
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


def _rid(name: str, type_key: str, *, sub: str = _SUB, rg: str = _RG) -> str:
    return f"/subscriptions/{sub}/resourceGroups/{rg}/providers/{type_key}/{name}"


def _seed(conn, specs, *, sub: str = _SUB):
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
            (_TENANT, "drift-test", "1.0", Jsonb({})),
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
            "SELECT id, present, body, source FROM synthetic.arm_overlay "
            "WHERE target_kind = 'resource'"
        )
        cols = ("id", "present", "body", "source")
        return {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}


def _baseline_rows(conn):
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_BASELINE_COLS} FROM synthetic.resources ORDER BY id")
        return cur.fetchall()


# --------------------------------------------------------------------------- #
# Middle-batch revert is permitted (strict-LIFO guard removed).
# --------------------------------------------------------------------------- #


def test_middle_batch_revert_permitted(pg_conn):
    """Reverting a MIDDLE batch (older than a still-active newer batch that
    overlaps the same resource) is NO LONGER rejected — recompute-from-ledger
    rebuilds the overlay from the remaining active batches. The old
    strict-LIFO UsageError must not fire."""
    _seed(
        pg_conn,
        [
            {
                "name": "midres",
                "type": resources.T_STORAGE,
                "tags": {"environment": "prod"},
                "sku": {"name": "Standard_LRS", "tier": "Standard"},
            }
        ],
    )

    # A (chaos props), B (chaos tag-removal) — both touch the SAME resource; then
    # C (temporal sku) is the NEWER active overlapping batch.
    assert _invoke_apply("--type", "chaos", "--codes", "DRIFT_STORAGE_PUBLIC_ACCESS",
                         "--intensity", "1.0").exit_code == 0
    assert _invoke_apply("--type", "chaos", "--codes", "DRIFT_TAGS_REMOVED",
                         "--intensity", "1.0").exit_code == 0
    assert _invoke_apply("--type", "temporal", "--codes", "DRIFT_SKU_TIER_SHIFT",
                         "--intensity", "1.0").exit_code == 0

    a, b, _c = _batch_ids_in_seq_order(pg_conn)

    # Revert the MIDDLE batch B while C (newer, overlapping) stays active.
    res = _invoke_revert(b)
    assert res.exit_code == 0, (res.output, res.exception)
    assert "strict LIFO" not in res.output


# --------------------------------------------------------------------------- #
# A,B,C → revert B recompute correctness (overlay == baseline ⊕ A ⊕ C).
# --------------------------------------------------------------------------- #


def test_abc_revert_b_recomputes_from_baseline(pg_conn):
    """apply A (props), B (tag-removal), C (sku) then revert B: each affected id's
    overlay is baseline replayed with A and C only — B's tag removal is gone (the
    tag is BACK), while A's property and C's sku shift REMAIN."""
    _seed(
        pg_conn,
        [
            {
                "name": "abcres",
                "type": resources.T_STORAGE,
                "tags": {"environment": "prod"},
                "sku": {"name": "Standard_LRS", "tier": "Standard"},
            }
        ],
    )
    rid = _rid("abcres", resources.T_STORAGE)

    assert _invoke_apply("--type", "chaos", "--codes", "DRIFT_STORAGE_PUBLIC_ACCESS",
                         "--intensity", "1.0").exit_code == 0            # A: properties
    assert _invoke_apply("--type", "chaos", "--codes", "DRIFT_TAGS_REMOVED",
                         "--intensity", "1.0").exit_code == 0            # B: tag removal
    assert _invoke_apply("--type", "temporal", "--codes", "DRIFT_SKU_TIER_SHIFT",
                         "--intensity", "1.0").exit_code == 0            # C: sku tier

    _a, b, _c = _batch_ids_in_seq_order(pg_conn)
    assert _invoke_revert(b).exit_code == 0

    overlay = _overlay_by_id(pg_conn)
    assert rid in overlay
    body = overlay[rid]["body"]
    assert overlay[rid]["present"] is True
    assert overlay[rid]["source"] == "drift"
    # A stays: the property drift is still present.
    assert body["properties"]["allowBlobPublicAccess"] is True
    # C stays: sku shifted up a tier (Premium).
    assert body["sku"]["tier"] == "Premium"
    # B is undone: the removed tag is BACK (baseline value replayed).
    assert body["tags"]["environment"] == "prod"


# --------------------------------------------------------------------------- #
# No-zombie: a field mutated by ONLY the target, reverted to baseline, leaves
# NO overlay row (DELETE-if-baseline).
# --------------------------------------------------------------------------- #


def test_single_batch_revert_deletes_overlay_no_zombie(pg_conn):
    """A lone batch's mutation reverted to baseline (no other active batch drifts
    the id) DELETES the overlay row entirely — no zombie residue; the resolved
    view falls back to the pristine baseline."""
    _seed(
        pg_conn,
        [{"name": "zres", "type": resources.T_STORAGE}],
    )
    rid = _rid("zres", resources.T_STORAGE)

    assert _invoke_apply("--type", "chaos", "--codes", "DRIFT_STORAGE_PUBLIC_ACCESS",
                         "--intensity", "1.0").exit_code == 0
    assert rid in _overlay_by_id(pg_conn)  # overlay written by apply

    (a,) = _batch_ids_in_seq_order(pg_conn)
    assert _invoke_revert(a).exit_code == 0

    assert rid not in _overlay_by_id(pg_conn), "reverted overlay row must be DELETED"


# --------------------------------------------------------------------------- #
# Disappear / appear revert.
# --------------------------------------------------------------------------- #


def test_disappear_revert_removes_tombstone(pg_conn):
    """A disappear reverted (no other active tombstone) removes the overlay
    tombstone — the id is live again via baseline."""
    _seed(pg_conn, [{"name": f"dres{i}", "type": resources.T_STORAGE} for i in range(3)])

    assert _invoke_apply("--type", "temporal", "--codes", "DRIFT_DISAPPEAR",
                         "--intensity", "1.0").exit_code == 0
    overlay = _overlay_by_id(pg_conn)
    tombstoned = [rid for rid, r in overlay.items() if r["present"] is False]
    assert tombstoned, "disappear apply must write at least one tombstone"

    (batch,) = _batch_ids_in_seq_order(pg_conn)
    assert _invoke_revert(batch).exit_code == 0

    after = _overlay_by_id(pg_conn)
    for rid in tombstoned:
        assert rid not in after, "reverted disappear tombstone must be DELETED"


def test_appear_revert_deletes_overlay_row(pg_conn):
    """An @appear reverted DELETEs the overlay row (baseline never had the leaf);
    baseline gains no row and the minted id no longer resolves."""
    _seed(pg_conn, [{"name": f"ares{i}", "type": resources.T_STORAGE} for i in range(3)])

    assert _invoke_apply("--type", "temporal", "--codes", "DRIFT_APPEAR",
                         "--intensity", "1.0").exit_code == 0
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT resource_id FROM synthetic.drift_records WHERE field_path = '@appear'"
        )
        appeared = [r[0] for r in cur.fetchall()]
    assert appeared

    (batch,) = _batch_ids_in_seq_order(pg_conn)
    assert _invoke_revert(batch).exit_code == 0

    after = _overlay_by_id(pg_conn)
    for rid in appeared:
        assert rid not in after, "reverted @appear overlay row must be DELETED"
        with pg_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM synthetic.resources WHERE id = %s", (rid,))
            assert cur.fetchone()[0] == 0, "baseline must never gain the reverted leaf"


# --------------------------------------------------------------------------- #
# Idempotency + already-reverted + baseline immutability.
# --------------------------------------------------------------------------- #


def test_revert_twice_is_already_reverted(pg_conn):
    """Reverting the same batch twice: the second call is rejected as
    already-reverted (history preserved, marked once) — the recompute itself is
    deterministic so no double-tombstone/zombie arises."""
    _seed(pg_conn, [{"name": "idem", "type": resources.T_STORAGE}])

    assert _invoke_apply("--type", "chaos", "--codes", "DRIFT_STORAGE_PUBLIC_ACCESS",
                         "--intensity", "1.0").exit_code == 0
    (batch,) = _batch_ids_in_seq_order(pg_conn)

    first = _invoke_revert(batch)
    assert first.exit_code == 0
    second = _invoke_revert(batch)
    assert second.exit_code != 0
    assert "already reverted" in second.output


def test_baseline_byte_unchanged_across_revert(pg_conn):
    """synthetic.resources columns are byte-identical before/after a revert — the
    baseline is NEVER mutated in place (recompute-only)."""
    _seed(
        pg_conn,
        [
            {
                "name": "immut",
                "type": resources.T_STORAGE,
                "tags": {"environment": "prod"},
                "sku": {"name": "Standard_LRS", "tier": "Standard"},
                "properties": {"minimumTlsVersion": "TLS1_2"},
            }
        ],
    )

    assert _invoke_apply("--type", "chaos", "--intensity", "1.0").exit_code == 0
    before = _baseline_rows(pg_conn)

    (batch,) = _batch_ids_in_seq_order(pg_conn)
    assert _invoke_revert(batch).exit_code == 0

    after = _baseline_rows(pg_conn)
    assert before == after, "synthetic.resources mutated in place during revert"
