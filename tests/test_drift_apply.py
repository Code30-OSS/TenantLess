"""apply-drift CLI read-modify-write seam tests (overlay reconcile).

The ``tenantless apply-drift`` command reads the live tenant (the resolved view),
computes seeded mutations via ``generator/drift.py``, and persists them to
``synthetic.arm_overlay`` copy-on-write snapshots/tombstones (``source='drift'``)
plus ``synthetic.drift_records`` / ``drift_batches`` — all in ONE transaction.
The overlay migration retired in-place ``synthetic.resources`` mutation: the
mutated served value now lives in the overlay body and the baseline is never
mutated in place. Each run STACKS a new batch_id from the CURRENT *resolved*
state: ``before`` captures the value at application time, not the original
generated value.

DB-backed tests use the project ``pg_conn`` skip fixture so DB-less CI skips
clean; ``test_injection_safe_filters`` is a pure SQL-builder assertion (no DB).
The canonical NEW-behaviour coverage lives in ``tests/test_drift_overlay_apply.py``.
"""

from __future__ import annotations

import os
import uuid

import pytest

from tenantless import cli
from tenantless.cli import main
from tenantless.generator import resources

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgres://tenantless:tenantless_dev@localhost:5433/tenantless",
)

# A fixed synthetic tenant + subscription for the seeded test rows
# (resources.subscription_id carries an FK → subscriptions → tenant).
_TENANT = str(uuid.UUID(int=0x1))
_SUB = str(uuid.UUID(int=0x11))


@pytest.fixture
def pg_conn():
    """Yield a live psycopg connection, or skip if Postgres is unavailable.

    ``autocommit=True`` is REQUIRED (mirrors
    ``tests/test_drift_overlay_apply.py``): a non-autocommit psycopg3 connection
    keeps a transaction open after every ``SELECT``, so the overlay/baseline reads
    below would leave this connection idle-in-transaction holding ACCESS SHARE on
    ``synthetic.resources``. ``apply-drift`` (invoked in-process) runs its
    idempotent schema-ensure preflight, whose DDL needs ACCESS EXCLUSIVE — it would
    DEADLOCK behind that stray read lock (the server-startup ALTER-lock fragility).
    Autocommit means our reads hold no lock, so the ensure preflight never blocks.
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


def _seed_storage(conn, *, count=3, sub=_SUB, type_key=resources.T_STORAGE):
    """Truncate the synthetic schema and insert ``count`` empty leaf resources.

    Empty ``properties``/``tags`` make the chaos storage mutators
    (allowBlobPublicAccess / supportsHttpsTrafficOnly / minimumTlsVersion)
    eligible while DRIFT_TAGS_REMOVED (needs an ``environment`` tag) stays inert,
    so a chaos run at intensity 1.0 produces a DETERMINISTIC 3×count deltas.
    ``type_key`` (default storage) lets a test seed a different leaf type — e.g. a
    KV-only tenant to prove the temporal appear lifecycle respects
    --resource-types (it mints storage, an excluded type).
    """
    from tenantless.generator import writer
    from psycopg.types.json import Jsonb

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
                f"{type_key}/{name}"
            )
            cur.execute(
                "INSERT INTO synthetic.resources "
                "(id, subscription_id, resource_group_name, name, type, location, "
                "tags, sku, kind, properties, provisioning_state, managed_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    rid, sub, "rg-drift-test", name, type_key, "eastus",
                    Jsonb({}), None, None, Jsonb({}), "Succeeded", None,
                ),
            )
    conn.commit()


def _invoke(*args):
    from click.testing import CliRunner

    runner = CliRunner()
    return runner.invoke(
        main, ["apply-drift", "--database-url", DATABASE_URL, *args]
    )


def _overlay_rows(conn):
    """The arm_overlay rows for resources (mirrors test_drift_overlay_apply.py)."""
    conn.commit()  # fresh snapshot of the command's commit (no-op under autocommit)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, target_kind, source, present, body, revision "
            "FROM synthetic.arm_overlay WHERE target_kind = 'resource' ORDER BY id"
        )
        cols = ("id", "target_kind", "source", "present", "body", "revision")
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# --------------------------------------------------------------------------- #
# records-written, stacking, injection-safe filters
# --------------------------------------------------------------------------- #


def test_records_written(pg_conn):
    """After a chaos apply against the seeded tenant, synthetic.drift_records has
    one row per mutation (field_path/before/after populated) and a single
    drift_batches row carries drift_type/seed/parent_fingerprint/result_fingerprint.
    The mutated value now lives in the arm_overlay
    body (source='drift'), NOT in synthetic.resources — the baseline is pristine."""
    _seed_storage(pg_conn, count=3)

    res = _invoke("--type", "chaos", "--seed", "42", "--intensity", "1.0")
    assert res.exit_code == 0, (res.output, res.exception)

    pg_conn.commit()  # fresh read-committed snapshot of the command's commit
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM synthetic.drift_batches")
        assert cur.fetchone()[0] == 1
        cur.execute(
            "SELECT drift_type, seed, parent_fingerprint, result_fingerprint, "
            "storage_mode FROM synthetic.drift_batches"
        )
        drift_type, seed, parent_fp, result_fp, storage_mode = cur.fetchone()
        assert drift_type == "chaos"
        assert seed == 42
        assert parent_fp and result_fp and parent_fp != result_fp
        # The batch is stamped storage_mode='overlay' (else the boot guard trips).
        assert storage_mode == "overlay"

        # 3 storage chaos codes × 3 resources = 9 deltas, each its own record.
        cur.execute("SELECT count(*) FROM synthetic.drift_records")
        assert cur.fetchone()[0] == 9

        cur.execute(
            "SELECT field_path, after, subscription_id FROM synthetic.drift_records"
        )
        rows = cur.fetchall()
        assert {fp for fp, _after, _sub in rows} == {
            "properties.allowBlobPublicAccess",
            "properties.supportsHttpsTrafficOnly",
            "properties.minimumTlsVersion",
        }
        for _fp, after, sub_id in rows:
            assert after is not None
            assert str(sub_id) == _SUB

        # synthetic.resources baseline is UNCHANGED (never mutated in place).
        cur.execute("SELECT properties FROM synthetic.resources ORDER BY id")
        for (props,) in cur.fetchall():
            assert props == {}

    # The mutation moved to arm_overlay copy-on-write: one present='drift' snapshot
    # per resource carries the complete mutated served body.
    rows = _overlay_rows(pg_conn)
    assert len(rows) == 3
    for row in rows:
        assert row["source"] == "drift"
        assert row["present"] is True
        body = row["body"]
        assert body["properties"]["allowBlobPublicAccess"] is True
        assert body["properties"]["supportsHttpsTrafficOnly"] is False
        assert body["properties"]["minimumTlsVersion"] == "TLS1_0"


def test_batch_stacks(pg_conn):
    """Two apply runs create two distinct batch_ids; stacking now reads the CURRENT
    *resolved* state (baseline ∪ overlay), so the second batch's `before` reflects
    the value the FIRST batch wrote to the overlay, and each apply UPSERTs
    the same overlay rows so their revision advances. The baseline stays pristine."""
    _seed_storage(pg_conn, count=3)

    r1 = _invoke("--type", "chaos", "--intensity", "1.0")
    assert r1.exit_code == 0, (r1.output, r1.exception)
    r2 = _invoke("--type", "chaos", "--intensity", "1.0")
    assert r2.exit_code == 0, (r2.output, r2.exception)

    pg_conn.commit()
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(DISTINCT batch_id) FROM synthetic.drift_batches")
        assert cur.fetchone()[0] == 2

        # Group the per-field `before` values by batch for allowBlobPublicAccess.
        cur.execute(
            "SELECT batch_id, before FROM synthetic.drift_records "
            "WHERE field_path = %s",
            ("properties.allowBlobPublicAccess",),
        )
        per_batch: dict = {}
        for bid, before in cur.fetchall():
            per_batch.setdefault(bid, set()).add(before)

    # Two batches: the first read before=None (baseline empty props), the second
    # read before=True — the value the FIRST batch applied to the OVERLAY, proving
    # the stack reads the resolved (overlay-aware) state, not the raw baseline.
    assert len(per_batch) == 2
    before_sets = {frozenset(v) for v in per_batch.values()}
    assert before_sets == {frozenset({None}), frozenset({True})}

    # The two applies UPSERT the SAME overlay rows (dedup one snapshot per id) — the
    # sql/009 trigger advances the revision each time, so it is > 1 after two stacks.
    rows = _overlay_rows(pg_conn)
    assert len(rows) == 3
    for row in rows:
        assert row["source"] == "drift"
        assert row["present"] is True
        assert row["revision"] > 1, "overlay revision must advance across stacked applies"

    # Baseline never mutated in place.
    with pg_conn.cursor() as cur:
        cur.execute("SELECT properties FROM synthetic.resources ORDER BY id")
        for (props,) in cur.fetchall():
            assert props == {}


def test_injection_safe_filters():
    """User-supplied --resource-types/--codes never reach SQL as spliced text:
    the scoped-read builder binds every user value as a parameter, and the
    field→column map is a CLOSED allowlist (project SQL bar)."""
    evil = "Microsoft.Foo'; DROP TABLE synthetic.resources; --"

    sql, params = cli._build_scoped_read_sql(None, [evil])
    # The malicious literal is never spliced into the SQL text.
    assert evil not in sql
    # Every placeholder is matched by exactly one bound parameter.
    assert sql.count("%s") == len(params)
    # The value travels as a bound parameter (inside the array param).
    flat = []
    for p in params:
        flat.extend(p) if isinstance(p, list) else flat.append(p)
    assert evil in flat

    # Closed-match field→column: only the served-column allowlist is reachable.
    assert cli._field_to_column("properties.allowBlobPublicAccess") == "properties"
    assert cli._field_to_column("properties.securityRules[]") == "properties"
    assert cli._field_to_column("tags.environment") == "tags"
    assert cli._field_to_column("sku") == "sku"
    assert cli._field_to_column("kind") == "kind"
    assert cli._field_to_column("drift_deleted_at") == "drift_deleted_at"
    assert cli._field_to_column("properties.x") in cli._UPDATE_COLUMN_ALLOWLIST

    with pytest.raises(ValueError):
        cli._field_to_column("id = 1; DROP TABLE synthetic.resources")


# --------------------------------------------------------------------------- #
# fingerprint determinism + chainability
# --------------------------------------------------------------------------- #


def test_result_fp_chains_to_next_parent_fp(pg_conn):
    """The result_fingerprint of a temporal batch (which disappears + appears rows)
    equals the parent_fingerprint the NEXT apply reads — the active-set convention
    lines up so drift batches form an unbroken fingerprint chain.

    Pre-fix this fails: the result_fp row-set included the disappeared (hidden)
    rows while the next parent read excludes them (WHERE drift_deleted_at IS NULL),
    so result_fp(N) could never equal parent_fp(N+1)."""
    _seed_storage(pg_conn, count=3)

    r1 = _invoke("--type", "temporal", "--intensity", "1.0")
    assert r1.exit_code == 0, (r1.output, r1.exception)
    r2 = _invoke("--type", "temporal", "--intensity", "1.0")
    assert r2.exit_code == 0, (r2.output, r2.exception)

    pg_conn.commit()
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT result_fingerprint, parent_fingerprint "
            "FROM synthetic.drift_batches ORDER BY seq"
        )
        rows = cur.fetchall()
    assert len(rows) == 2
    (result_fp_1, _parent_fp_1), (_result_fp_2, parent_fp_2) = rows
    # Batch 1's post-state ACTIVE set is exactly what batch 2's parent read sees.
    assert result_fp_1 == parent_fp_2


# --------------------------------------------------------------------------- #
# temporal lifecycle honors --codes / --resource-types.
# Pre-fix the appear/disappear lifecycle ALWAYS ran on a temporal drift,
# ignoring the filters, and appear always minted storage even when excluded.
# --------------------------------------------------------------------------- #


def test_temporal_lifecycle_respects_codes(pg_conn):
    """--codes that selects only a field-mutator (neither DRIFT_DISAPPEAR nor
    DRIFT_APPEAR) produces ZERO appear/disappear records and no soft-deletes /
    minted rows — the temporal lifecycle honors --codes."""
    _seed_storage(pg_conn, count=3)

    res = _invoke(
        "--type", "temporal", "--intensity", "1.0",
        "--codes", "DRIFT_PROVISIONING_STATE",
    )
    assert res.exit_code == 0, (res.output, res.exception)

    pg_conn.commit()
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM synthetic.drift_records "
            "WHERE field_path IN ('drift_deleted_at', '@appear')"
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            "SELECT count(*) FROM synthetic.resources "
            "WHERE drift_deleted_at IS NOT NULL"
        )
        assert cur.fetchone()[0] == 0
        # no minted leaves: the 3 originals only.
        cur.execute("SELECT count(*) FROM synthetic.resources")
        assert cur.fetchone()[0] == 3
        # the requested field-mutator DID fire (3 provisioningState records).
        cur.execute(
            "SELECT count(*) FROM synthetic.drift_records "
            "WHERE field_path = 'properties.provisioningState'"
        )
        assert cur.fetchone()[0] == 3


def test_temporal_appear_respects_resource_types(pg_conn):
    """--resource-types excluding the appear leaf type (storage) produces ZERO
    appear records and inserts no minted row — apply never mints an excluded type.
    Disappear (type-agnostic over eligible leaves) still fires on the
    KV-only tenant."""
    # KV-only tenant: every leaf is disappear-eligible, but appear mints storage.
    _seed_storage(pg_conn, count=3, type_key=resources.T_KV)

    res = _invoke(
        "--type", "temporal", "--intensity", "1.0",
        "--resource-types", resources.T_KV,
    )
    assert res.exit_code == 0, (res.output, res.exception)

    pg_conn.commit()
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM synthetic.drift_records WHERE field_path = '@appear'"
        )
        assert cur.fetchone()[0] == 0
        # no storage row was minted: only the 3 (now soft-deleted) KV leaves remain.
        cur.execute("SELECT count(*) FROM synthetic.resources")
        assert cur.fetchone()[0] == 3
        cur.execute(
            "SELECT count(*) FROM synthetic.resources "
            "WHERE type = %s",
            (resources.T_STORAGE,),
        )
        assert cur.fetchone()[0] == 0
        # disappear is type-agnostic and still fired over the eligible KV leaves.
        cur.execute(
            "SELECT count(*) FROM synthetic.drift_records "
            "WHERE field_path = 'drift_deleted_at'"
        )
        assert cur.fetchone()[0] == 3


# --------------------------------------------------------------------------- #
# persist the engine drift_code + metadata.
# Pre-fix both drift_records INSERTs discarded the computed drift_code, so audit
# consumers could not tell which mutation produced a record.
# --------------------------------------------------------------------------- #


def test_chaos_records_persist_drift_code(pg_conn):
    """Every chaos drift_records row persists a non-null drift_code matching the
    engine code that produced it, plus a metadata JSONB carrying the code."""
    _seed_storage(pg_conn, count=3)

    res = _invoke("--type", "chaos", "--intensity", "1.0")
    assert res.exit_code == 0, (res.output, res.exception)

    fp_to_code = {
        "properties.allowBlobPublicAccess": "DRIFT_STORAGE_PUBLIC_ACCESS",
        "properties.supportsHttpsTrafficOnly": "DRIFT_STORAGE_HTTP_ALLOWED",
        "properties.minimumTlsVersion": "DRIFT_STORAGE_OLD_TLS",
    }
    pg_conn.commit()
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT field_path, drift_code, metadata FROM synthetic.drift_records"
        )
        rows = cur.fetchall()
    assert rows
    for field_path, drift_code, metadata in rows:
        assert drift_code is not None
        assert drift_code == fp_to_code[field_path]
        assert metadata is not None and metadata.get("drift_code") == drift_code


def test_lifecycle_records_persist_drift_code(pg_conn):
    """Temporal lifecycle drift_records persist their lifecycle drift_code:
    disappear carries DRIFT_DISAPPEAR, appear carries DRIFT_APPEAR."""
    _seed_storage(pg_conn, count=3)

    res = _invoke("--type", "temporal", "--intensity", "1.0")
    assert res.exit_code == 0, (res.output, res.exception)

    pg_conn.commit()
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT drift_code, metadata FROM synthetic.drift_records "
            "WHERE field_path = 'drift_deleted_at'"
        )
        dis = cur.fetchall()
        assert dis and all(
            c == "DRIFT_DISAPPEAR" and m and m.get("drift_code") == "DRIFT_DISAPPEAR"
            for c, m in dis
        )
        cur.execute(
            "SELECT drift_code, metadata FROM synthetic.drift_records "
            "WHERE field_path = '@appear'"
        )
        app = cur.fetchall()
        assert app and all(
            c == "DRIFT_APPEAR" and m and m.get("drift_code") == "DRIFT_APPEAR"
            for c, m in app
        )


# --------------------------------------------------------------------------- #
# --dry-run no-mutation guarantee + clamp reporting
# --------------------------------------------------------------------------- #


def test_dry_run_no_mutation(pg_conn):
    """--dry-run reports the planned record count, and afterwards
    synthetic.drift_records / drift_batches are empty AND no resource column
    changed — the run mutated NOTHING."""
    _seed_storage(pg_conn, count=3)

    res = _invoke("--type", "chaos", "--intensity", "1.0", "--dry-run")
    assert res.exit_code == 0, (res.output, res.exception)
    # 3 storage chaos codes × 3 resources = 9 planned records, reported not silent.
    assert "planned 9" in res.output

    pg_conn.commit()
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM synthetic.drift_records")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM synthetic.drift_batches")
        assert cur.fetchone()[0] == 0
        # The sampled resource's served column is byte-identical to pre-run ({}).
        cur.execute("SELECT properties FROM synthetic.resources ORDER BY id LIMIT 1")
        assert cur.fetchone()[0] == {}


def test_intensity_clamp_reported(pg_conn):
    """When --intensity exceeds eligibility, --dry-run prints the clamp note
    and the planned count equals the clamped count."""
    _seed_storage(pg_conn, count=3)

    # intensity 5.0 (absolute count) over only 3 eligible storage accounts.
    res = _invoke("--type", "chaos", "--intensity", "5.0", "--dry-run")
    assert res.exit_code == 0, (res.output, res.exception)
    # clamp surfaced (never silent).
    assert "clamped" in res.output
    # planned == clamped count: each storage code clamps 5 -> 3, 3 codes = 9.
    assert "planned 9" in res.output

    pg_conn.commit()
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM synthetic.drift_records")
        assert cur.fetchone()[0] == 0
