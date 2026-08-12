"""apply-drift → arm_overlay copy-on-write migration proofs.

The overlay migration re-targets the *apply* half of drift off in-place
``synthetic.resources`` mutation and onto ``synthetic.arm_overlay`` copy-on-write
snapshots tagged ``source='drift'``:

  * tags / properties / sku / kind → a ``present=true`` overlay row carrying the
    COMPLETE served ARM body (satisfies every ``sql/009`` CHECK);
  * disappear → a ``present=false`` overlay tombstone (body NULL);
  * @appear → a ``present=true`` overlay row for the minted leaf, with the FULL
    minted-leaf body ALSO stashed in the ``@appear`` drift_record metadata under
    ``appear_body`` so a revert can reconstruct it from the ledger alone;
  * the drift read re-points at ``synthetic.arm_resolved_resources`` (the resolved
    view) so each apply stacks on the CURRENT resolved state;
  * the ``drift_batches`` row is stamped ``storage_mode='overlay'`` (else the
    boot guard trips);
  * ``synthetic.resources`` is NEVER mutated in place (baseline immutability).

DB-backed; the ``pg_conn`` fixture skips clean when Postgres is unavailable so
DB-less CI skips rather than fails (mirrors ``tests/test_drift_apply.py``).

NOTE (platform): native ``uv run pytest`` HANGS on the Windows dev host
(fork-vs-spawn + :5433 advisory-lock residue); these proofs run on the Linux
PG16 container gate.
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
_RG = "rg-drift-test"

# The full baseline column set drift could touch — the immutability comparison
# set for the baseline-pristine proof (matches the sql/001 served columns +
# the retired soft-delete column).
_BASELINE_COLS = (
    "id, tags, sku, kind, properties, drift_deleted_at, "
    "name, type, location, subscription_id, resource_group_name"
)


@pytest.fixture
def pg_conn():
    """Yield a live psycopg connection, or skip if Postgres is unavailable.

    ``autocommit=True`` is REQUIRED: a non-autocommit psycopg3 connection keeps a
    transaction open after every ``SELECT``, so the ``_baseline_rows`` /
    ``_overlay_rows`` reads would leave this connection idle-in-transaction
    holding ACCESS SHARE on ``synthetic.resources``. ``apply-drift`` (invoked
    in-process below) runs its idempotent schema-ensure preflight, whose DDL
    needs ACCESS EXCLUSIVE — it would deadlock behind that stray read lock (the
    server-startup ALTER-lock fragility). Autocommit means our reads hold no
    lock, so the ensure preflight never blocks.
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
    return (
        f"/subscriptions/{sub}/resourceGroups/{rg}/providers/{type_key}/{name}"
    )


def _seed(conn, specs, *, sub: str = _SUB):
    """Truncate the synthetic schema + overlay and insert the given resources.

    ``specs`` is a list of ``dict(name, type, tags, sku, kind, properties)``.
    ``arm_overlay`` is NOT in ``_SYNTHETIC_TABLES`` (it is mutable overlay state,
    excluded from the baseline truncate on purpose), so it is cleared explicitly
    for test isolation.
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


def _seed_storage(conn, count=3):
    _seed(
        conn,
        [
            {"name": f"stdrift{i:03d}", "type": resources.T_STORAGE}
            for i in range(count)
        ],
    )


def _invoke(*args):
    from click.testing import CliRunner

    runner = CliRunner()
    return runner.invoke(main, ["apply-drift", "--database-url", DATABASE_URL, *args])


def _overlay_rows(conn):
    conn.commit()  # fresh snapshot of the command's commit
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id_lower, id, target_kind, source, present, body, revision "
            "FROM synthetic.arm_overlay ORDER BY id"
        )
        cols = ("id_lower", "id", "target_kind", "source", "present", "body", "revision")
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _baseline_rows(conn):
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_BASELINE_COLS} FROM synthetic.resources ORDER BY id"
        )
        return cur.fetchall()


# --------------------------------------------------------------------------- #
# Per-kind overlay-write proofs. Each mutation kind writes an
# arm_overlay row whose shape the sql/009 CHECKs accept (the INSERT succeeding
# IS the CHECK proof) with source='drift'.
# --------------------------------------------------------------------------- #


def test_per_kind_properties_overlay_snapshot(pg_conn):
    """A chaos (properties) apply writes ONE present overlay snapshot per mutated
    resource: source='drift', target_kind='resource', present=true, a COMPLETE
    body (id/name/type/location strings, tags+properties objects) carrying the
    mutated properties. Baseline properties stay pristine ({}), and the batch is
    stamped storage_mode='overlay'."""
    _seed_storage(pg_conn, count=3)

    res = _invoke("--type", "chaos", "--intensity", "1.0")
    assert res.exit_code == 0, (res.output, res.exception)

    rows = _overlay_rows(pg_conn)
    assert len(rows) == 3
    for row in rows:
        assert row["source"] == "drift"
        assert row["target_kind"] == "resource"
        assert row["present"] is True
        assert row["revision"] > 0
        body = row["body"]
        assert body is not None and body != {}
        assert body["id"] == row["id"]                 # ck_arm_overlay_body_id_agree
        assert isinstance(body["name"], str)
        assert isinstance(body["type"], str)
        assert isinstance(body["location"], str)
        assert isinstance(body["tags"], dict)          # ck_arm_overlay_tags
        assert isinstance(body["properties"], dict)    # ck_arm_overlay_envelope
        # A seed with sku/kind None omits the keys (never a stored JSON null).
        assert "sku" not in body
        assert "kind" not in body
        # The mutated properties are carried in the snapshot.
        assert body["properties"]["allowBlobPublicAccess"] is True

    # Baseline untouched.
    with pg_conn.cursor() as cur:
        cur.execute("SELECT properties FROM synthetic.resources ORDER BY id")
        for (props,) in cur.fetchall():
            assert props == {}
        cur.execute("SELECT storage_mode FROM synthetic.drift_batches")
        assert cur.fetchone()[0] == "overlay"


def test_per_kind_tags_overlay_snapshot(pg_conn):
    """A tag-removal apply writes a present overlay snapshot whose body.tags no
    longer carries the removed key, while the baseline tags stay pristine."""
    _seed(
        pg_conn,
        [{"name": "tagres", "type": resources.T_STORAGE, "tags": {"environment": "prod"}}],
    )

    res = _invoke("--type", "chaos", "--codes", "DRIFT_TAGS_REMOVED", "--intensity", "1.0")
    assert res.exit_code == 0, (res.output, res.exception)

    rows = _overlay_rows(pg_conn)
    assert len(rows) == 1
    body = rows[0]["body"]
    assert rows[0]["source"] == "drift"
    assert rows[0]["present"] is True
    assert isinstance(body["tags"], dict)
    assert "environment" not in body["tags"]           # the drift removed it

    # Baseline tag still present (pristine).
    with pg_conn.cursor() as cur:
        cur.execute("SELECT tags FROM synthetic.resources")
        assert cur.fetchone()[0] == {"environment": "prod"}


def test_per_kind_sku_overlay_snapshot(pg_conn):
    """A temporal sku-tier-shift apply writes a present overlay snapshot whose
    body.sku is an OBJECT (ck_arm_overlay_optional_types) reflecting the shift,
    while the baseline sku stays pristine."""
    _seed(
        pg_conn,
        [
            {
                "name": "skures",
                "type": resources.T_STORAGE,
                "sku": {"name": "Standard_LRS", "tier": "Standard"},
            }
        ],
    )

    res = _invoke(
        "--type", "temporal", "--codes", "DRIFT_SKU_TIER_SHIFT", "--intensity", "1.0"
    )
    assert res.exit_code == 0, (res.output, res.exception)

    rows = _overlay_rows(pg_conn)
    assert len(rows) == 1
    body = rows[0]["body"]
    assert rows[0]["source"] == "drift"
    assert rows[0]["present"] is True
    assert isinstance(body["sku"], dict)               # object, never JSON null
    assert body["sku"]["tier"] == "Premium"            # shifted up one tier

    # Baseline sku unchanged.
    with pg_conn.cursor() as cur:
        cur.execute("SELECT sku FROM synthetic.resources")
        assert cur.fetchone()[0] == {"name": "Standard_LRS", "tier": "Standard"}


def test_per_kind_disappear_overlay_tombstone(pg_conn):
    """A temporal disappear apply writes a present=false tombstone (body NULL,
    source='drift') per disappeared resource; the baseline drift_deleted_at stays
    NULL (no in-place soft-delete)."""
    _seed_storage(pg_conn, count=3)

    res = _invoke(
        "--type", "temporal", "--codes", "DRIFT_DISAPPEAR", "--intensity", "1.0"
    )
    assert res.exit_code == 0, (res.output, res.exception)

    rows = _overlay_rows(pg_conn)
    assert len(rows) == 3
    for row in rows:
        assert row["source"] == "drift"
        assert row["target_kind"] == "resource"
        assert row["present"] is False                 # tombstone
        assert row["body"] is None                     # ck_arm_overlay_present_body
        assert row["revision"] > 0

    # Baseline never soft-deleted in place.
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM synthetic.resources WHERE drift_deleted_at IS NOT NULL"
        )
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM synthetic.resources")
        assert cur.fetchone()[0] == 3


def test_baseline_pristine_across_apply(pg_conn):
    """For every mutated id, the synthetic.resources columns are byte-identical
    before and after apply — the baseline is never mutated in place.
    The batch is stamped storage_mode='overlay' (else the boot guard trips)."""
    _seed(
        pg_conn,
        [
            {
                "name": "mixres",
                "type": resources.T_STORAGE,
                "tags": {"environment": "prod"},
                "sku": {"name": "Standard_LRS", "tier": "Standard"},
                "properties": {"minimumTlsVersion": "TLS1_2"},
            }
        ],
    )

    before = _baseline_rows(pg_conn)

    res = _invoke("--type", "chaos", "--intensity", "1.0")
    assert res.exit_code == 0, (res.output, res.exception)

    after = _baseline_rows(pg_conn)
    assert before == after, "synthetic.resources mutated in place (baseline not pristine)"

    # An overlay snapshot WAS written (the drift is not lost, it moved to overlay).
    assert len(_overlay_rows(pg_conn)) == 1
    with pg_conn.cursor() as cur:
        cur.execute("SELECT storage_mode FROM synthetic.drift_batches")
        assert cur.fetchone()[0] == "overlay"


# --------------------------------------------------------------------------- #
# @appear proofs. appear writes ONLY an overlay
# row (baseline never gains the leaf) and stashes the FULL minted-leaf body in
# the ledger under metadata.appear_body for replay-sufficiency.
# --------------------------------------------------------------------------- #


def test_appear_writes_overlay_not_baseline(pg_conn):
    """A temporal appear apply writes a present=true source='drift' overlay row
    for the minted leaf and adds NO synthetic.resources row (baseline never
    written)."""
    _seed_storage(pg_conn, count=3)

    res = _invoke("--type", "temporal", "--codes", "DRIFT_APPEAR", "--intensity", "1.0")
    assert res.exit_code == 0, (res.output, res.exception)

    # No new baseline rows (only the 3 seeded originals).
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM synthetic.resources")
        assert cur.fetchone()[0] == 3

    # @appear records identify the minted leaf ids.
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT resource_id FROM synthetic.drift_records WHERE field_path = '@appear'"
        )
        appeared = {r[0] for r in cur.fetchall()}
    assert appeared, "expected at least one @appear record"

    rows = {r["id"]: r for r in _overlay_rows(pg_conn)}
    for rid in appeared:
        # The minted leaf is NOT in the baseline.
        with pg_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM synthetic.resources WHERE id = %s", (rid,))
            assert cur.fetchone()[0] == 0
        # It IS a present source='drift' overlay row with a complete body.
        row = rows[rid]
        assert row["present"] is True
        assert row["source"] == "drift"
        assert row["target_kind"] == "resource"
        assert row["body"]["id"] == rid
        assert isinstance(row["body"]["properties"], dict)


def test_appear_persists_full_body_in_ledger(pg_conn):
    """The @appear drift_record stores the COMPLETE minted-leaf body under
    metadata.appear_body, byte-equal to the arm_overlay body — so a revert
    replay can reconstruct the overlay row from the ledger alone."""
    _seed_storage(pg_conn, count=3)

    res = _invoke("--type", "temporal", "--codes", "DRIFT_APPEAR", "--intensity", "1.0")
    assert res.exit_code == 0, (res.output, res.exception)

    overlay_by_id = {r["id"]: r["body"] for r in _overlay_rows(pg_conn)}
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT resource_id, metadata FROM synthetic.drift_records "
            "WHERE field_path = '@appear'"
        )
        records = cur.fetchall()
    assert records, "expected @appear drift_records"
    for rid, metadata in records:
        assert metadata is not None
        # The documented replay field is metadata['appear_body'].
        assert "appear_body" in metadata, "appear_body missing from @appear metadata"
        assert metadata["appear_body"] == overlay_by_id[rid], (
            "ledger appear_body must byte-equal the overlay body (replay-sufficiency)"
        )


def test_appear_resolvable_via_resolved_view(pg_conn):
    """A minted appear leaf surfaces through synthetic.arm_resolved_resources
    (the overlay branch's canonical-id scope derivation resolves it)."""
    _seed_storage(pg_conn, count=3)

    res = _invoke("--type", "temporal", "--codes", "DRIFT_APPEAR", "--intensity", "1.0")
    assert res.exit_code == 0, (res.output, res.exception)

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT resource_id FROM synthetic.drift_records WHERE field_path = '@appear'"
        )
        appeared = [r[0] for r in cur.fetchall()]
    assert appeared

    pg_conn.commit()
    with pg_conn.cursor() as cur:
        for rid in appeared:
            cur.execute(
                "SELECT id, subscription_id, resource_group_name "
                "FROM synthetic.arm_resolved_resources WHERE id = %s",
                (rid,),
            )
            got = cur.fetchone()
            assert got is not None, f"appeared id {rid} not resolvable via the view"
            assert got[0] == rid
            # Scope derivation is fail-closed (never NULL).
            assert got[1] is not None
            assert got[2] == _RG
