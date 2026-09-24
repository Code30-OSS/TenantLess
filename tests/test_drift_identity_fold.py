"""The drift identity seams share the ONE canonical ARM-ID fold (``arm_id_key``).

Drift writes / reads ``synthetic.arm_overlay`` whose identity is ``id_lower =
synthetic.arm_id_key(id)`` (ASCII ``A-Z -> a-z``, every other character verbatim). Every
drift-side identity comparison must use that same fold — never a locale ``lower()`` /
``str.lower()``, which folds non-ASCII letters (``À`` -> ``à``) the key keeps distinct:

* ``state_fingerprint`` ORDERS rows by the identity key but HASHES the verbatim served id;
* the appear-leaf collision guard (``mint_appear_leaf``) and the lifecycle collision mirror
  (``compute_lifecycle``) compare identity keys;
* ``apply-drift``'s overlay upsert keys rows with ``synthetic.arm_id_key`` (a locale key for a
  non-ASCII id would violate the overlay CHECK), and its user-owned (source='user') latch
  detection matches on the key;
* ``revert-drift``'s from-baseline DELETE matches on the key (else a zombie overlay row
  survives the revert), keeping the ``source <> 'user'`` ownership latch;
* the resolver views' shadow anti-joins (sql/010) match on the key, so a tombstone hides the
  baseline row it names.

DB proofs run in their own throwaway database (see ``test_arm_id_cutover``) and are marked
``integration``; the fold / fingerprint / collision proofs are DB-free.
"""

from __future__ import annotations

import hashlib
import types
import uuid

import orjson
import pytest

from tenantless.generator import drift, resources
from tenantless.identity import arm_id_key

# tests/ is on sys.path (conftest) — reuse the throwaway-database helpers.
from test_arm_id_cutover import _connect, _throwaway_database  # noqa: E402

_TENANT = str(uuid.UUID(int=0xD1F7))
_SUB = str(uuid.UUID(int=0xD1F70001))
_RG = "rg-drift-fold"


def _rid(name: str, *, rg: str = _RG) -> str:
    return f"/subscriptions/{_SUB}/resourceGroups/{rg}/providers/{resources.T_STORAGE}/{name}"


# --------------------------------------------------------------------------- #
# DB-free: fingerprint ordering + collision guards
# --------------------------------------------------------------------------- #


def _row(rid: str) -> dict:
    return {"id": rid, "tags": {}, "sku": None, "kind": None, "properties": {}}


def test_state_fingerprint_orders_by_identity_key_and_hashes_served_id():
    upper = _row("/subscriptions/s/resourceGroups/r/providers/p/B")
    lower = _row("/subscriptions/s/resourceGroups/r/providers/p/a")
    # Ordered by the identity key: ".../a" < ".../b" (raw ordering would put "B" first).
    canon = [
        {**lower, "drift_deleted_at": False},
        {**upper, "drift_deleted_at": False},
    ]
    expected = hashlib.sha256(orjson.dumps(canon, option=orjson.OPT_SORT_KEYS)).hexdigest()
    assert drift.state_fingerprint([upper, lower]) == expected
    assert drift.state_fingerprint([lower, upper]) == expected
    # The HASHED id is the verbatim served id, not the key: a casing change is visible.
    recased = _row("/subscriptions/s/resourceGroups/r/providers/p/b")
    assert drift.state_fingerprint([recased, lower]) != expected


def _stub_rg():
    return types.SimpleNamespace(
        subscription_id=_SUB, name=_RG, location="eastus", resources=[]
    )


def _stub_generator(monkeypatch, ids: list[str]):
    """Make resources.generate_resource yield the given ids in order (clean names)."""
    it = iter(ids)

    def fake_generate_resource(ctx, **kwargs):
        rid = next(it)
        return types.SimpleNamespace(id=rid, name="leafname", tags={"x": "y"})

    monkeypatch.setattr(resources, "generate_resource", fake_generate_resource)


def test_appear_collision_guard_uses_identity_key(monkeypatch):
    # A different identity (non-ASCII case differs) is already seen...
    seen = {arm_id_key(_rid("àccount"))}
    # ...so a minted "Àccount" is NOT a collision under the ASCII-only fold.
    _stub_generator(monkeypatch, [_rid("Àccount")])
    leaf = drift.mint_appear_leaf(None, _stub_rg(), seen_ids=set(), seen_lower=seen)
    assert leaf.id == _rid("Àccount")
    assert arm_id_key(leaf.id) in seen, "the minted identity key is recorded"


def test_appear_collision_guard_still_rejects_ascii_case_duplicate(monkeypatch):
    seen = {arm_id_key(_rid("FOO"))}
    _stub_generator(monkeypatch, [_rid("foo"), _rid("bar")])
    leaf = drift.mint_appear_leaf(None, _stub_rg(), seen_ids=set(), seen_lower=seen)
    assert leaf.id == _rid("bar"), "an ASCII-case duplicate of a seen id is re-minted"


def test_lifecycle_collision_mirror_uses_identity_key(monkeypatch):
    # seen_ids carries "ÀCCOUNT"; a minted "àccount" is a DIFFERENT identity.
    _stub_generator(monkeypatch, [_rid("àccount")])
    rg = _stub_rg()
    deltas, minted = drift.compute_lifecycle(
        None,
        [rg],
        drift.DisappearRefs(),
        disappear_count=0,
        appear_count=1,
        seen_ids={_rid("ÀCCOUNT")},
    )
    assert [leaf.id for leaf in minted] == [_rid("àccount")]


# --------------------------------------------------------------------------- #
# DB-backed: apply / revert / resolver on a non-ASCII id
# --------------------------------------------------------------------------- #


def _provision(conn) -> None:
    from tenantless.generator import writer

    writer.ensure_base_schema(conn)
    writer.ensure_drift_schema(conn)
    writer.ensure_arm_overlay_schema(conn)
    writer.ensure_arm_id_key_schema(conn)
    writer.ensure_arm_id_identity_cutover_schema(conn)
    writer.ensure_arm_resolver_schema(conn)


def _seed(conn, names: list[str]) -> None:
    from psycopg.types.json import Jsonb

    conn.execute(
        "INSERT INTO synthetic.tenant "
        "(tenant_id, display_name, generated_at, profile_version, scale_params) "
        "VALUES (%s, 'drift-fold', now(), '1.0', %s)",
        (_TENANT, Jsonb({})),
    )
    conn.execute(
        "INSERT INTO synthetic.subscriptions "
        "(subscription_id, tenant_id, display_name, state, archetype, tags, "
        "authorization_source, spending_limit) "
        "VALUES (%s, %s, 'sub', 'Enabled', 'test', %s, 'RoleBased', 'On')",
        (_SUB, _TENANT, Jsonb({})),
    )
    conn.execute(
        "INSERT INTO synthetic.resource_groups "
        "(id, subscription_id, name, location, template_type, tags, provisioning_state) "
        "VALUES (%s, %s, %s, 'eastus', 'network', %s, 'Succeeded')",
        (f"/subscriptions/{_SUB}/resourceGroups/{_RG}", _SUB, _RG, Jsonb({})),
    )
    for name in names:
        conn.execute(
            "INSERT INTO synthetic.resources "
            "(id, subscription_id, resource_group_name, name, type, location, "
            "tags, sku, kind, properties, provisioning_state, managed_by) "
            "VALUES (%s, %s, %s, %s, %s, 'eastus', %s, NULL, NULL, %s, 'Succeeded', NULL)",
            (_rid(name), _SUB, _RG, name, resources.T_STORAGE, Jsonb({}), Jsonb({})),
        )


def _cli(*args):
    from click.testing import CliRunner

    from tenantless.cli import main

    return CliRunner().invoke(main, list(args))


def _overlay(conn) -> dict[str, tuple]:
    rows = conn.execute(
        "SELECT id, id_lower, source, present FROM synthetic.arm_overlay ORDER BY id"
    ).fetchall()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


@pytest.mark.integration
def test_apply_then_revert_keys_non_ascii_ids_with_the_identity_fold():
    name = "ÀccountDrift"
    with _throwaway_database() as dsn:
        with _connect(dsn) as conn:
            _provision(conn)
            _seed(conn, [name])
        # When drift applies to a resource whose id carries a non-ASCII uppercase letter
        res = _cli("apply-drift", "--database-url", dsn, "--type", "chaos", "--intensity", "1.0")
        assert res.exit_code == 0, (res.output, res.exception)
        with _connect(dsn) as conn:
            ov = _overlay(conn)
            # Then the overlay row is keyed by the ASCII-only identity fold
            assert ov[_rid(name)][0] == arm_id_key(_rid(name)), ov
            assert ov[_rid(name)][1] == "drift"
        # When the batch is reverted, the from-baseline recompute DELETEs that overlay row
        with _connect(dsn) as conn:
            batch_id = str(
                conn.execute("SELECT batch_id FROM synthetic.drift_batches").fetchone()[0]
            )
        res = _cli("revert-drift", "--database-url", dsn, "--batch-id", batch_id)
        assert res.exit_code == 0, (res.output, res.exception)
        with _connect(dsn) as conn:
            assert _rid(name) not in _overlay(conn), "no zombie overlay row after revert"


@pytest.mark.integration
def test_drift_yields_to_a_user_owned_non_ascii_row():
    name = "ÀccountUser"
    with _throwaway_database() as dsn:
        with _connect(dsn) as conn:
            _provision(conn)
            _seed(conn, [name])
            # Given a user-written (PUT) overlay row over the baseline resource, keyed the way
            # the server's write plane keys it
            conn.execute(
                "INSERT INTO synthetic.arm_overlay "
                "(id_lower, id, target_kind, source, present, body) "
                "VALUES (synthetic.arm_id_key(%s), %s, 'resource', 'user', true, "
                "jsonb_build_object('id', %s::text, 'name', %s::text, 'type', %s::text, "
                "'location', 'eastus', 'tags', '{}'::jsonb, 'properties', '{}'::jsonb))",
                (_rid(name), _rid(name), _rid(name), name, resources.T_STORAGE),
            )
            before = conn.execute(
                "SELECT revision, body FROM synthetic.arm_overlay WHERE id = %s", (_rid(name),)
            ).fetchone()
        # When drift applies over the resolved state (which serves the user row)
        res = _cli("apply-drift", "--database-url", dsn, "--type", "chaos", "--intensity", "1.0")
        assert res.exit_code == 0, (res.output, res.exception)
        with _connect(dsn) as conn:
            # Then the user row is untouched (the one-way ownership latch)
            assert _overlay(conn)[_rid(name)][1:] == ("user", True)
            after = conn.execute(
                "SELECT revision, body FROM synthetic.arm_overlay WHERE id = %s", (_rid(name),)
            ).fetchone()
            assert after == before, "drift never rewrites (or re-revisions) a user row"
            n = conn.execute(
                "SELECT count(*) FROM synthetic.drift_records WHERE resource_id = %s",
                (_rid(name),),
            ).fetchone()[0]
            assert n == 0, "no phantom drift record for a user-owned id"


@pytest.mark.integration
def test_resolver_tombstone_hides_the_non_ascii_baseline_row_it_names():
    name = "ÀccountShadow"
    other = "àccountShadow"  # a DIFFERENT identity under the ASCII-only fold
    with _throwaway_database() as dsn:
        with _connect(dsn) as conn:
            _provision(conn)
            _seed(conn, [name, other])
            conn.execute(
                "INSERT INTO synthetic.arm_overlay "
                "(id_lower, id, target_kind, source, present, body) "
                "VALUES (synthetic.arm_id_key(%s), %s, 'resource', 'user', false, NULL)",
                (_rid(name), _rid(name)),
            )
            live = {
                r[0]
                for r in conn.execute("SELECT id FROM synthetic.arm_resolved_resources").fetchall()
            }
            assert _rid(name) not in live, "the tombstone hides the baseline row it names"
            assert _rid(other) in live, "a distinct (non-ASCII-case) identity stays live"
            # The RG view shadows on the same key.
            rg_id = f"/subscriptions/{_SUB}/resourceGroups/{_RG}"
            conn.execute(
                "UPDATE synthetic.resource_groups SET id = %s, name = %s WHERE id = %s",
                (rg_id + "À", _RG + "À", rg_id),
            )
            conn.execute(
                "INSERT INTO synthetic.arm_overlay "
                "(id_lower, id, target_kind, source, present, body) "
                "VALUES (synthetic.arm_id_key(%s), %s, 'resource_group', 'user', false, NULL)",
                (rg_id + "À", rg_id + "À"),
            )
            rgs = {
                r[0]
                for r in conn.execute(
                    "SELECT id FROM synthetic.arm_resolved_resource_groups"
                ).fetchall()
            }
            assert rg_id + "À" not in rgs, "the RG tombstone hides the RG it names"
