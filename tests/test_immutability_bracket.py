"""Immutability bracket — the baseline is byte-identical across apply AND revert.

The overlay migration makes baseline immutability TRUE: neither ``apply-drift`` nor
``revert-drift`` mutates ``synthetic.resources`` / ``synthetic.resource_groups`` in
place — all drift lands on the ``synthetic.arm_overlay`` copy-on-write plane. This
test PROVES it end-to-end by hashing the seeded baseline with the SINGLE canonical
digest shared with the reset proof (``mock-server/tests/common/immutability_hash.sql``,
read VERBATIM — never re-implemented here so the two "immutability" proofs cannot
measure different things) and asserting:

    H_before == H_after_apply == H_after_revert

over a drift mix that exercises EVERY write path — field mutation (properties + tags
+ sku) AND the two lifecycle paths (@appear + disappear) — so the whole drift surface
is bracketed, not just the field-mutation half.

NOTE (kind): the drift registry (``generator/drift.py``) has NO ``kind``-mutating code —
``kind`` is not a drift-mutable served field in this model — so the mix covers
tags/properties/sku + appear + disappear. The bracket's
invariant is independent of WHICH fields drift: the baseline hash must not move at all.

DB-backed; the ``pg_conn`` fixture skips clean when Postgres is unavailable. Native
``uv run pytest`` HANGS on the Windows dev host (fork-vs-spawn + :5433 advisory-lock
residue — a known platform issue, not this code); this proof runs on the Linux PG16
container gate.
"""

from __future__ import annotations

import os
import re
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
_RG = "rg-drift-test"

# The single canonical digest, shared with the reset proof — read verbatim, NEVER
# re-implemented. Path: <repo-root>/mock-server/tests/common/immutability_hash.sql.
_HASH_SQL_PATH = (
    Path(__file__).resolve().parent.parent
    / "mock-server"
    / "tests"
    / "common"
    / "immutability_hash.sql"
)

# `apply-drift batch <uuid>: <type> drift, <N> records (...)` — capture the batch id and
# the record count so we only revert batches that actually wrote records.
_BATCH_RE = re.compile(
    r"apply-drift batch ([0-9a-fA-F-]{36}): \w+ drift, (\d+) records"
)


@pytest.fixture
def pg_conn():
    """Yield a live psycopg connection, or skip if Postgres is unavailable.

    ``autocommit=True`` is REQUIRED (mirrors ``tests/test_drift_overlay_apply.py``): a
    non-autocommit connection keeps a txn open after every SELECT, so our hash/baseline
    reads would sit idle-in-transaction holding ACCESS SHARE on ``synthetic.resources``
    and DEADLOCK the apply/revert idempotent schema-ensure preflight (which needs ACCESS
    EXCLUSIVE — the server-startup ALTER-lock fragility). Autocommit → our reads hold no
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


def _rid(name: str, type_key: str) -> str:
    return f"/subscriptions/{_SUB}/resourceGroups/{_RG}/providers/{type_key}/{name}"


def _seed(conn, specs):
    """Truncate + reseed a baseline tenant (tenant, subscription, ONE resource_group, and
    ``specs`` resources), clearing the overlay for isolation.

    Both relations the hash covers are NON-empty: ``synthetic.resources`` (the specs) and
    ``synthetic.resource_groups`` (one RG). Drift never touches RGs, so a non-empty RG
    relation makes the bracket cover both hashed relations without changing the invariant.
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


def _apply(*args) -> tuple[str | None, int]:
    """Invoke ``apply-drift`` in-process; return ``(batch_id, record_count)`` parsed from
    the emitted ``apply-drift batch <uuid>: ... <N> records`` line."""
    from click.testing import CliRunner

    runner = CliRunner()
    res = runner.invoke(
        main, ["apply-drift", "--database-url", DATABASE_URL, *args]
    )
    assert res.exit_code == 0, (res.output, res.exception)
    m = _BATCH_RE.search(res.output)
    if not m:
        return None, 0
    return m.group(1), int(m.group(2))


def _revert(batch_id: str) -> None:
    from click.testing import CliRunner

    runner = CliRunner()
    res = runner.invoke(
        main,
        ["revert-drift", "--batch-id", batch_id, "--database-url", DATABASE_URL],
    )
    assert res.exit_code == 0, (res.output, res.exception)


def _reset() -> None:
    """Invoke the ``reset`` command in-process (the AUTHORITATIVE reset path —
    the real CLI performing the FK-ordered overlay + ledger DELETEs)."""
    from click.testing import CliRunner

    runner = CliRunner()
    res = runner.invoke(main, ["reset", "--database-url", DATABASE_URL])
    assert res.exit_code == 0, (res.output, res.exception)


def test_baseline_immutable_across_apply_and_revert(pg_conn):
    """H_before == H_after_apply == H_after_revert over a full drift mix (properties + tags
    + sku + @appear + disappear) — the immutability bracket.

    The baseline hash covers ``synthetic.resources`` + ``synthetic.resource_groups`` (the
    two relations drift can touch). Because every drift write lands on ``arm_overlay`` and
    NEVER on the baseline, the digest must be byte-identical at all three points: before any
    drift, after applying the whole mix, and after reverting every batch.
    """
    # Seed a mix carrying an `environment` tag (→ tags-removed eligible), an `sku` object
    # (→ sku-tier-shift eligible), and properties (→ chaos storage props). Multiple storage
    # leaves so disappear + appear both have a non-empty eligible population.
    _seed(
        pg_conn,
        [
            {
                "name": f"stbracket{i:03d}",
                "type": resources.T_STORAGE,
                "tags": {"environment": "prod"},
                "sku": {"name": "Standard_LRS", "tier": "Standard"},
                "properties": {"minimumTlsVersion": "TLS1_2"},
            }
            for i in range(5)
        ],
    )

    h_before = _estate_hash(pg_conn)

    # Apply the full mix as separate batches (recompute-from-ledger makes each independently
    # revertable). Each entry: (label, apply-drift args). appear + disappear are explicit.
    # ORDER MATTERS: appear MUST precede disappear. The CLI derives appear_count from the
    # disappear-eligible population of the CURRENT resolved read (cli.py: symmetric-churn
    # "vanish a few, add a few"). A prior disappear at intensity 1.0 tombstones every eligible
    # leaf, leaving that population empty, so a following appear would mint 0 rows and skip the
    # lifecycle path. Running appear first (population intact) exercises both lifecycle halves.
    applies = [
        ("chaos-props+tags", ["--type", "chaos", "--intensity", "1.0"]),
        (
            "sku-shift",
            ["--type", "temporal", "--codes", "DRIFT_SKU_TIER_SHIFT", "--intensity", "1.0"],
        ),
        (
            "appear",
            ["--type", "temporal", "--codes", "DRIFT_APPEAR", "--intensity", "1.0"],
        ),
        (
            "disappear",
            ["--type", "temporal", "--codes", "DRIFT_DISAPPEAR", "--intensity", "1.0"],
        ),
    ]

    batches: list[str] = []
    for label, args in applies:
        batch_id, records = _apply(*args)
        assert records > 0, f"{label}: expected the drift mix to write records"
        assert batch_id is not None, f"{label}: no batch id emitted"
        batches.append(batch_id)

    # Baseline is UNTOUCHED by the whole apply mix (the drift moved entirely to the overlay).
    h_after_apply = _estate_hash(pg_conn)
    assert h_after_apply == h_before, (
        "synthetic.* mutated in place across apply — baseline not immutable"
    )

    # An overlay WAS written (the drift is not lost, it lives on the overlay plane).
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM synthetic.arm_overlay")
        assert cur.fetchone()[0] > 0, "the drift mix wrote overlay rows"

    # Revert every batch; recompute-from-ledger rebuilds the overlay from the immutable baseline.
    for batch_id in batches:
        _revert(batch_id)

    h_after_revert = _estate_hash(pg_conn)
    assert h_after_revert == h_before, (
        "synthetic.* mutated in place across revert — baseline not immutable"
    )


def test_baseline_immutable_across_reset(pg_conn):
    """H_before == H_after_reset over the full drift mix — the reset leg, the
    AUTHORITATIVE proof that the real ``reset`` CLI performs the correct
    overlay + ledger DELETEs.

    Complements the apply/revert bracket above: after applying the whole drift surface
    (properties + tags + sku + @appear + disappear) the overlay is non-empty; the Python
    ``reset`` command then clears the overlay + drift ledger and the baseline digest
    returns to its pre-drift value BYTE-FOR-BYTE. Reads the SAME single canonical
    ``immutability_hash.sql`` (no second hash expression) so the reset leg cannot measure
    a different scope than the apply/revert legs.
    """
    _seed(
        pg_conn,
        [
            {
                "name": f"stbracket{i:03d}",
                "type": resources.T_STORAGE,
                "tags": {"environment": "prod"},
                "sku": {"name": "Standard_LRS", "tier": "Standard"},
                "properties": {"minimumTlsVersion": "TLS1_2"},
            }
            for i in range(5)
        ],
    )

    h_before = _estate_hash(pg_conn)

    # Apply the full mix (appear MUST precede disappear — see the apply/revert test).
    applies = [
        ("chaos-props+tags", ["--type", "chaos", "--intensity", "1.0"]),
        (
            "sku-shift",
            ["--type", "temporal", "--codes", "DRIFT_SKU_TIER_SHIFT", "--intensity", "1.0"],
        ),
        ("appear", ["--type", "temporal", "--codes", "DRIFT_APPEAR", "--intensity", "1.0"]),
        (
            "disappear",
            ["--type", "temporal", "--codes", "DRIFT_DISAPPEAR", "--intensity", "1.0"],
        ),
    ]
    for label, args in applies:
        _batch_id, records = _apply(*args)
        assert records > 0, f"{label}: expected the drift mix to write records"

    # The drift IS on the overlay plane (not lost) before we reset.
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM synthetic.arm_overlay")
        assert cur.fetchone()[0] > 0, "the drift mix wrote overlay rows"

    # Reset clears the overlay + ledger; the baseline was never touched.
    _reset()

    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM synthetic.arm_overlay")
        assert cur.fetchone()[0] == 0, "reset did not clear the overlay"
        cur.execute("SELECT count(*) FROM synthetic.drift_batches")
        assert cur.fetchone()[0] == 0, "reset did not clear the drift ledger"

    h_after_reset = _estate_hash(pg_conn)
    assert h_after_reset == h_before, (
        "estate hash changed across reset — baseline is not the pristine pre-drift "
        "state"
    )
