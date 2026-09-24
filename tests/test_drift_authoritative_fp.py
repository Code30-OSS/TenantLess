"""apply-drift authoritative persisted-state fingerprint proofs.

The ``drift_batches.result_fingerprint`` must describe the resolved state that was
ACTUALLY persisted, not an in-memory prediction. A non-dry-run apply therefore:

  * applies its overlay upserts / tombstones FIRST,
  * re-reads the SAME scoped resolved view (``synthetic.arm_resolved_resources``)
    through the SAME ``_build_scoped_read_sql`` builder, inside the same txn,
  * fingerprints those persisted rows (the authoritative value),
  * compares against the predicted fingerprint and FAILS CLOSED on mismatch —
    rolling back BOTH the overlay writes and the ledger,
  * and only then inserts ``drift_batches`` (authoritative value only) + the staged
    ``drift_records``.

The re-read is always-on (D-14): even an effective no-op apply re-reads and
compares; a dry-run computes the prediction only. A failed comparison may leave a
revision-sequence GAP (sequences are non-transactional) but must change no
committed overlay revision (D-15). Mismatch diagnostics are bounded to the first
20 divergent resources by canonical key and value-safe (sha256 + structural
metadata only, never raw tag/property values — D-16).

DB-backed tests are marked ``integration`` and skip clean when Postgres is
unavailable; the diagnostics-builder unit tests are pure and run everywhere.

NOTE (platform): native ``uv run pytest`` HANGS on the Windows dev host; the DB
proofs run on the Linux PG16 container gate.
"""

from __future__ import annotations

import os
import re
import uuid

import pytest

import tenantless.cli as cli_mod
from tenantless.cli import main
from tenantless.generator import resources
from tenantless.identity import arm_id_key

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgres://tenantless:tenantless_dev@localhost:5433/tenantless",
)

_TENANT = str(uuid.UUID(int=0x1))
_SUB = str(uuid.UUID(int=0x11))
_RG = "rg-drift-test"

# Distinctive VALUE strings seeded into tags / properties. None of them may ever
# appear in mismatch diagnostics (value-safety leak guard).
_TAG_VALUE = "tagval-LEAKCANARY-7f3a"
_PROP_VALUE = "propval-LEAKCANARY-91c2"
_INJECTED_KEY = "injkey-LEAKCANARY-k"
_INJECTED_VALUE = "injval-LEAKCANARY-secret-token"
_CANARY = "LEAKCANARY"


# --------------------------------------------------------------------------- #
# Fixtures + helpers (mirrors tests/test_drift_overlay_apply.py).
# --------------------------------------------------------------------------- #


@pytest.fixture
def pg_conn():
    """Yield a live AUTOCOMMIT psycopg connection, or skip if Postgres is absent.

    ``autocommit=True`` is REQUIRED: a non-autocommit connection would sit
    idle-in-transaction holding ACCESS SHARE on ``synthetic.resources`` after a
    read, and apply-drift's schema-ensure DDL (ACCESS EXCLUSIVE) would deadlock
    behind it.
    """
    psycopg = pytest.importorskip("psycopg")
    try:
        conn = psycopg.connect(DATABASE_URL, connect_timeout=3, autocommit=True)
    except Exception as exc:  # noqa: BLE001 - any connection failure -> skip
        pytest.skip(f"Postgres unavailable: {exc}")
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


def _seed_canary_storage(conn, count=4):
    """Storage accounts carrying distinctive tag/property VALUES (leak canaries)."""
    _seed(
        conn,
        [
            {
                "name": f"stauth{i:03d}",
                "type": resources.T_STORAGE,
                "tags": {"environment": _TAG_VALUE, "owner": _TAG_VALUE},
                "properties": {"note": _PROP_VALUE},
            }
            for i in range(count)
        ],
    )


def _invoke(*args):
    from click.testing import CliRunner

    return CliRunner().invoke(
        main, ["apply-drift", "--database-url", DATABASE_URL, *args]
    )


def _overlay_snapshot(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id_lower, id, target_kind, source, present, body, revision "
            "FROM synthetic.arm_overlay ORDER BY id_lower"
        )
        return cur.fetchall()


def _ledger_counts(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM synthetic.drift_batches")
        batches = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM synthetic.drift_records")
        records = cur.fetchone()[0]
    return batches, records


def _revision_seq_last(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT last_value FROM synthetic.arm_overlay_revision_seq")
        return cur.fetchone()[0]


def _resolved_fingerprint(conn, sub=None, types=None):
    """Independent fingerprint of the COMMITTED resolved view for a scope."""
    from tenantless.generator import drift

    sql, params = cli_mod._build_scoped_read_sql(sub, types)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return drift.state_fingerprint(
        [
            {
                "id": r[0],
                "tags": r[3],
                "sku": r[4],
                "kind": r[5],
                "properties": r[6],
                "drift_deleted_at": None,
            }
            for r in rows
        ]
    )


def _install_divergence(monkeypatch):
    """Force predicted != persisted: after the FIRST present overlay upsert, add an
    extra tag to that persisted body on the same cursor/txn (a divergence the
    in-memory prediction cannot know about)."""
    original = cli_mod._overlay_upsert_present
    state = {"done": False}

    def diverging_upsert(cur, robj, Jsonb):
        original(cur, robj, Jsonb)
        if not state["done"]:
            state["done"] = True
            cur.execute(
                "UPDATE synthetic.arm_overlay "
                "SET body = jsonb_set(body, ARRAY['tags', %s], to_jsonb(%s::text)) "
                "WHERE id_lower = synthetic.arm_id_key(%s)",
                (_INJECTED_KEY, _INJECTED_VALUE, robj.id),
            )

    monkeypatch.setattr(cli_mod, "_overlay_upsert_present", diverging_upsert)
    return state


# --------------------------------------------------------------------------- #
# Fail-closed rollback (D-11 / D-15 / D-17).
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_forced_mismatch_rolls_back(pg_conn, monkeypatch):
    """Given a tenant with a prior committed drift batch (overlay rows + ledger),
    When a second apply's persisted overlay diverges from its prediction,
    Then the apply exits non-zero, arm_overlay is byte-identical to its pre-apply
    state (rows AND revisions), NO new drift_batches / drift_records row exists,
    and the revision sequence did not rewind (a gap is tolerated, D-15)."""
    _seed_canary_storage(pg_conn, count=4)

    # A first, clean apply commits overlay rows + a ledger batch to roll back TO.
    first = _invoke("--type", "chaos", "--seed", "3", "--intensity", "0.5")
    assert first.exit_code == 0, (first.output, first.exception)

    overlay_before = _overlay_snapshot(pg_conn)
    ledger_before = _ledger_counts(pg_conn)
    seq_before = _revision_seq_last(pg_conn)
    fp_before = _resolved_fingerprint(pg_conn)
    assert overlay_before, "precondition: the first apply wrote overlay rows"

    state = _install_divergence(monkeypatch)
    res = _invoke(
        "--type", "chaos", "--codes", "DRIFT_STORAGE_PUBLIC_ACCESS",
        "--seed", "5", "--intensity", "1.0",
    )

    assert state["done"], "the divergence seam was never reached"
    assert res.exit_code != 0, (
        "a persisted-state fingerprint mismatch must fail closed",
        res.output,
    )
    assert "fingerprint mismatch" in res.output.lower()

    # (b) overlay rolled back: rows, bodies AND revisions byte-identical.
    assert _overlay_snapshot(pg_conn) == overlay_before
    # (c) ledger rolled back: no new batch, no new record.
    assert _ledger_counts(pg_conn) == ledger_before
    # (d) the non-transactional revision sequence never rewinds (gap allowed —
    # contiguity is deliberately NOT asserted).
    assert _revision_seq_last(pg_conn) >= seq_before
    # The served resolved state is exactly what it was before the failed apply.
    assert _resolved_fingerprint(pg_conn) == fp_before


# --------------------------------------------------------------------------- #
# Always-on re-read through the SAME builder (D-12 / D-14).
# --------------------------------------------------------------------------- #


def _count_builder_calls(monkeypatch):
    calls: list[tuple] = []
    original = cli_mod._build_scoped_read_sql

    def counting(subscription_id, resource_types):
        calls.append((subscription_id, resource_types))
        return original(subscription_id, resource_types)

    monkeypatch.setattr(cli_mod, "_build_scoped_read_sql", counting)
    return calls


@pytest.mark.integration
def test_noop_apply_still_reread(pg_conn, monkeypatch):
    """Given a scoped apply that selects no resource (an effective no-op),
    When it runs (non-dry-run),
    Then it STILL re-reads through the same builder with the identical scope
    binding, commits a batch with zero records, and its result fingerprint equals
    its parent fingerprint (and the committed resolved state)."""
    _seed_canary_storage(pg_conn, count=3)
    calls = _count_builder_calls(monkeypatch)

    res = _invoke(
        "--type", "chaos",
        "--subscription", _SUB,
        "--resource-types", resources.T_WEBSITE,  # none seeded -> no-op
        "--intensity", "1.0",
    )
    assert res.exit_code == 0, (res.output, res.exception)

    # Parent read + authoritative re-read: two calls, identical scope binding.
    assert len(calls) == 2, calls
    assert calls[0] == calls[1]

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT batch_id, parent_fingerprint, result_fingerprint "
            "FROM synthetic.drift_batches"
        )
        rows = cur.fetchall()
        assert len(rows) == 1
        batch_id, parent_fp, result_fp = rows[0]
        assert result_fp == parent_fp
        cur.execute(
            "SELECT count(*) FROM synthetic.drift_records WHERE batch_id = %s",
            (batch_id,),
        )
        assert cur.fetchone()[0] == 0
    assert result_fp == _resolved_fingerprint(
        pg_conn, uuid.UUID(_SUB), [resources.T_WEBSITE]
    )


@pytest.mark.integration
def test_dry_run_computes_predicted_only(pg_conn, monkeypatch):
    """Given a tenant, When apply-drift runs with --dry-run,
    Then it performs ONLY the parent read (no re-read), and writes no overlay row
    and no ledger row."""
    _seed_canary_storage(pg_conn, count=3)
    calls = _count_builder_calls(monkeypatch)

    res = _invoke("--type", "chaos", "--intensity", "1.0", "--dry-run")
    assert res.exit_code == 0, (res.output, res.exception)

    assert len(calls) == 1, calls
    assert _overlay_snapshot(pg_conn) == []
    assert _ledger_counts(pg_conn) == (0, 0)


@pytest.mark.integration
@pytest.mark.parametrize(
    "args",
    [
        ("--type", "chaos", "--seed", "9", "--intensity", "0.7"),
        ("--type", "temporal", "--seed", "9", "--intensity", "0.5"),
    ],
    ids=["chaos", "temporal-lifecycle"],
)
def test_ledger_result_fp_is_persisted_state(pg_conn, args):
    """Given a correct apply (including temporal appear/disappear lifecycle),
    When it commits,
    Then the ledger's result_fingerprint equals an independent fingerprint of the
    COMMITTED resolved view, and the next apply's parent chains to it."""
    _seed_canary_storage(pg_conn, count=6)

    res = _invoke(*args)
    assert res.exit_code == 0, (res.output, res.exception)
    with pg_conn.cursor() as cur:
        cur.execute("SELECT result_fingerprint FROM synthetic.drift_batches")
        (result_fp,) = cur.fetchone()
    assert result_fp == _resolved_fingerprint(pg_conn)

    again = _invoke("--type", "chaos", "--seed", "10", "--intensity", "0.3")
    assert again.exit_code == 0, (again.output, again.exception)
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT parent_fingerprint FROM synthetic.drift_batches "
            "ORDER BY applied_at DESC LIMIT 1"
        )
        assert cur.fetchone()[0] == result_fp


# --------------------------------------------------------------------------- #
# Value-safe, bounded mismatch diagnostics (D-16).
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_diagnostics_value_safe(pg_conn, monkeypatch):
    """Given a forced persisted-state divergence on a tag,
    When the apply fails closed,
    Then the emitted diagnostics carry the batch UUID, both fingerprints,
    predicted/persisted sha256 and key-count structural metadata for the differing
    field — and NEVER a raw tag/property value or the injected key/value."""
    _seed_canary_storage(pg_conn, count=4)
    _install_divergence(monkeypatch)

    res = _invoke(
        "--type", "chaos", "--codes", "DRIFT_STORAGE_PUBLIC_ACCESS",
        "--seed", "5", "--intensity", "1.0",
    )
    assert res.exit_code != 0, res.output
    out = res.output

    assert re.search(r"batch=[0-9a-f-]{36}", out), out
    assert re.search(r"predicted_fp=[0-9a-f]{64}", out), out
    assert re.search(r"persisted_fp=[0-9a-f]{64}", out), out
    assert re.search(r"predicted_sha256=[0-9a-f]{64}", out), out
    assert re.search(r"persisted_sha256=[0-9a-f]{64}", out), out
    # The injected tag adds exactly one key on the persisted side.
    assert "tags differ" in out
    assert "predicted_keys=2 persisted_keys=3" in out
    # Leak guard: no raw value (seeded tag/property values, the injected key or
    # value) ever reaches the diagnostics.
    assert _CANARY not in out, out
    for raw in (_TAG_VALUE, _PROP_VALUE, _INJECTED_KEY, _INJECTED_VALUE):
        assert raw not in out


# --- pure diagnostics-builder unit tests (no DB) ---------------------------- #


def _row(rid, **fields):
    return {
        "id": rid,
        "tags": fields.get("tags", {}),
        "sku": fields.get("sku"),
        "kind": fields.get("kind"),
        "properties": fields.get("properties", {}),
        "drift_deleted_at": None,
    }


def _diag(**kw):
    from tenantless.cli import _fingerprint_mismatch_diagnostics

    kw.setdefault("batch_id", uuid.UUID(int=0xB))
    kw.setdefault("predicted_fp", "a" * 64)
    kw.setdefault("persisted_fp", "b" * 64)
    return "\n".join(_fingerprint_mismatch_diagnostics(**kw))


def test_diagnostics_bounded_to_first_20_by_canonical_key():
    """Given 25 divergent resources whose raw-id order differs from their
    canonical-key order, When diagnostics are built, Then exactly the first 20 by
    canonical key are listed (in key order) and the other 5 are counted as
    omitted."""
    ids = [f"/s/x/{'R' if i % 2 else 'r'}{i:02d}" for i in range(25)]
    out = _diag(predicted_rows=[_row(i) for i in ids], persisted_rows=[])

    listed = [ln.split(":", 1)[0].strip() for ln in out.splitlines() if ln.startswith("  /s/")]
    expected = sorted(ids, key=lambda r: (arm_id_key(r), r))[:20]
    assert listed == expected
    assert "divergent=25" in out
    assert "omitted=5" in out


def test_diagnostics_category_taxonomy():
    """Each divergence is tagged with its category, and category counts are
    reported."""
    predicted = [
        _row("/s/x/missing"),                         # predicted-present/persisted-absent
        _row("/s/x/CaseMe"),                          # casing mismatch vs /s/x/caseme
        _row("/s/x/tagdiff", tags={"a": "1"}),        # field differs
        _row("/s/x/owned", properties={"p": "1"}),    # user-owned, differs
    ]
    persisted = [
        _row("/s/x/extra"),                           # predicted-absent/persisted-present
        _row("/s/x/caseme"),
        _row("/s/x/tagdiff", tags={"a": "2", "b": "3"}),
        _row("/s/x/owned", properties={"p": "2"}),
        _row("/s/x/vanished"),                        # predicted disappeared, still present
    ]
    out = _diag(
        predicted_rows=predicted,
        persisted_rows=persisted,
        disappeared_ids={"/s/x/vanished"},
        user_owned_keys={arm_id_key("/s/x/owned")},
    )
    assert "/s/x/missing: predicted-present/persisted-absent" in out
    assert "/s/x/extra: predicted-absent/persisted-present" in out
    assert "/s/x/CaseMe: canonical-id-casing-mismatch" in out
    assert "/s/x/tagdiff: field-differs" in out
    assert "tags differ" in out
    assert "predicted_keys=1 persisted_keys=2" in out
    assert "/s/x/owned: ownership/tombstone-discrepancy" in out
    assert "/s/x/vanished: ownership/tombstone-discrepancy" in out
    assert "divergent=6" in out
    assert "predicted-present/persisted-absent=1" in out
    assert "predicted-absent/persisted-present=1" in out
    assert "canonical-id-casing-mismatch=1" in out
    assert "field-differs=1" in out
    assert "ownership/tombstone-discrepancy=2" in out


def test_diagnostics_never_emit_raw_values():
    """sha256 + structural metadata only: raw tag/property/sku/kind values and
    tag keys never appear in the diagnostics."""
    predicted = [
        _row(
            "/s/x/r1",
            tags={"k-LEAKCANARY": "v-LEAKCANARY"},
            properties={"secret": "LEAKCANARY-pw"},
            sku={"name": "LEAKCANARY-sku"},
            kind="LEAKCANARY-kind",
        )
    ]
    persisted = [
        _row(
            "/s/x/r1",
            tags={"k-LEAKCANARY": "w-LEAKCANARY"},
            properties={"secret": "LEAKCANARY-pw2"},
            sku={"name": "LEAKCANARY-sku2"},
            kind="LEAKCANARY-kind2",
        )
    ]
    out = _diag(predicted_rows=predicted, persisted_rows=persisted)
    assert _CANARY not in out, out
    for field in ("tags", "properties", "sku", "kind"):
        assert f"{field} differ" in out
    assert len(re.findall(r"predicted_sha256=[0-9a-f]{64}", out)) == 4
