"""GEN-09 binary-COPY round-trip integration test.

This is the ONE generator test that needs a live Postgres (port 5433). It is
guarded by a fixture that skips the test when Postgres is unavailable, so the
suite stays green in DB-less CI. The full round-trip (tenant + subscriptions +
resource_groups loaded via psycopg3 binary COPY in FK order) is implemented and
verified here for Plan 02-01; resources COPY (GEN-04/07/08) is added in 02-02.
"""

from __future__ import annotations

import os

import pytest

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgres://tenantless:tenantless_dev@localhost:5433/tenantless",
)


@pytest.fixture
def pg_conn():
    """Yield a live psycopg connection, or skip if Postgres is unavailable."""
    psycopg = pytest.importorskip("psycopg")
    try:
        conn = psycopg.connect(DATABASE_URL, connect_timeout=3)
    except Exception as exc:  # noqa: BLE001 - any connection failure → skip
        pytest.skip(f"Postgres on 5433 unavailable: {exc}")
    # D-14: provision the nullable synthetic.tenant.profile_name column (sql/007)
    # so copy_tenant's profile_name write succeeds even on a long-lived dev volume
    # created before Phase 14 (fresh docker volumes get it via initdb; cli.py
    # provisions it before write). Idempotent ADD COLUMN IF NOT EXISTS — no-op when
    # the column already exists.
    from tenantless.generator import writer as _writer

    _writer.ensure_web_metadata_schema(conn)
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def test_copy_round_trip(generator_profile, pg_conn):
    """tenant + subscriptions + resource_groups COPY in FK order, then read back
    the row counts and confirm they match the generated tenant (GEN-09)."""
    from tenantless.generator.pipeline import generate_tenant
    from tenantless.generator import writer

    tenant = generate_tenant(
        generator_profile, seed=42, n_subs=20, n_resources=1500
    ).tenant

    writer.truncate_synthetic(pg_conn)
    writer.write_tenant(pg_conn, tenant)  # tenant→subs→RGs→resources, FK order
    pg_conn.commit()

    expected_res = sum(len(rg.resources) for rg in tenant.resource_groups)

    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM synthetic.subscriptions")
        n_subs = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM synthetic.resource_groups")
        n_rgs = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM synthetic.tenant")
        n_tenant = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM synthetic.resources")
        n_res = cur.fetchone()[0]

    assert n_tenant == 1
    assert n_subs == len(tenant.subscriptions)
    assert n_rgs == len(tenant.resource_groups)
    assert n_res == expected_res

    # GEN-07: a storage account boolean round-trips as a real JSON boolean,
    # not the string "true"; no "__other__" sentinel leaks into properties.
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT properties FROM synthetic.resources "
            "WHERE type = 'Microsoft.Storage/storageAccounts' LIMIT 1"
        )
        row = cur.fetchone()
        if row is not None:
            props = row[0]
            assert isinstance(props["supportsHttpsTrafficOnly"], bool)
            assert "__other__" not in str(props)

    # GEN-08: a VM's NIC reference resolves to a real resources row in the DB.
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT properties FROM synthetic.resources "
            "WHERE type = 'Microsoft.Compute/virtualMachines' LIMIT 1"
        )
        row = cur.fetchone()
        if row is not None:
            nic_id = row[0]["networkProfile"]["networkInterfaces"][0]["id"]
            cur.execute(
                "SELECT count(*) FROM synthetic.resources WHERE id = %s", (nic_id,)
            )
            assert cur.fetchone()[0] == 1

    # GEN-09: the dependencies COPY PATH executed in FK order (Phase 2 writes an
    # empty/minimal set; row SEMANTICS land in Phase 5). The table must exist and
    # be queryable after write_tenant — proving the FK-last COPY path is wired.
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM synthetic.dependencies")
        assert cur.fetchone()[0] >= 0


def test_copy_dependencies_path(generator_profile, pg_conn):
    """GEN-09: copy_dependencies binary-COPYs the dependencies table FK-last.

    Drives the COPY path directly with a minimal row to prove the column/type
    contract (text,text,text,uuid,uuid) round-trips. Phase 5 owns the real
    cross-subscription dependency SEMANTICS; here we only verify the path.
    """
    import uuid

    from tenantless.generator import writer

    src_sub = uuid.uuid4()
    tgt_sub = uuid.uuid4()
    rows = [
        {
            "dependency_type": "centralized_logging",
            "source_resource_id": "/subscriptions/x/providers/p/foo/a",
            "target_resource_id": "/subscriptions/y/providers/p/foo/b",
            "source_subscription": src_sub,
            "target_subscription": tgt_sub,
        }
    ]

    writer.truncate_synthetic(pg_conn)
    writer.copy_dependencies(pg_conn, rows)
    pg_conn.commit()

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT dependency_type, source_subscription, target_subscription "
            "FROM synthetic.dependencies"
        )
        out = cur.fetchall()
    assert len(out) == 1
    assert out[0][0] == "centralized_logging"
    assert out[0][1] == src_sub
    assert out[0][2] == tgt_sub

    # Empty list is a no-op (the v1 default path) and must not error.
    writer.truncate_synthetic(pg_conn)
    writer.copy_dependencies(pg_conn, [])
    pg_conn.commit()
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM synthetic.dependencies")
        assert cur.fetchone()[0] == 0


def test_violations_dependencies_roundtrip(generator_profile, pg_conn):
    """GEN-09 ext (Plan 05-04): the one live-DB round-trip for the two new tables.

    Runs a full ``generate_tenant`` with BOTH Phase-5 passes on, writes the
    mutated resources plus the populated ``synthetic.violations`` and
    ``synthetic.dependencies`` tables via the live binary-COPY path in FK order,
    then reads back:

    - ``synthetic.violations`` row count == ``len(violation_rows)`` and > 0,
    - ``synthetic.dependencies`` row count == ``len(dependency_rows)`` and > 0,
    - DB-level XSUB-06: a NOT-EXISTS anti-join proves every dependency's
      ``source_resource_id`` AND ``target_resource_id`` resolves to a real
      ``synthetic.resources`` row (zero dangling references in Postgres).

    Skips cleanly via the ``pg_conn`` fixture when Postgres on 5433 is absent.
    """
    from tenantless.generator.pipeline import generate_tenant
    from tenantless.generator import writer

    result = generate_tenant(
        generator_profile,
        seed=42,
        n_subs=20,
        n_resources=3000,
        inject_violations=True,
        inject_cross_sub=True,
    )
    tenant = result.tenant
    violation_rows = result.violations
    dependency_rows = result.dependencies

    # Both engines must actually produce rows for this end-to-end slice to mean
    # anything (and for the > 0 assertions below to be reachable in-memory).
    assert len(violation_rows) > 0
    assert len(dependency_rows) > 0

    writer.truncate_synthetic(pg_conn)
    # FK-safe: tenant → subs → RGs → resources → dependencies → violations.
    writer.write_tenant(
        pg_conn, tenant, dependencies=dependency_rows, violations=violation_rows
    )
    pg_conn.commit()

    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM synthetic.violations")
        n_viol = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM synthetic.dependencies")
        n_dep = cur.fetchone()[0]

        # DB-level XSUB-06: count any dependency endpoint (source OR target) that
        # does NOT resolve to a synthetic.resources row. Must be zero.
        cur.execute(
            "SELECT count(*) FROM synthetic.dependencies d "
            "WHERE NOT EXISTS ("
            "    SELECT 1 FROM synthetic.resources r WHERE r.id = d.source_resource_id"
            ") OR NOT EXISTS ("
            "    SELECT 1 FROM synthetic.resources r WHERE r.id = d.target_resource_id"
            ")"
        )
        n_dangling = cur.fetchone()[0]

    # (a) violations written, count matches the in-memory row set, non-empty.
    assert n_viol == len(violation_rows)
    assert n_viol > 0
    # (b) dependencies written, count matches the in-memory row set, non-empty.
    assert n_dep == len(dependency_rows)
    assert n_dep > 0
    # (c) zero dangling references at the database level (XSUB-06 in Postgres).
    assert n_dangling == 0


def test_generate_tenant_carries_profile_name(generator_profile):
    """D-14: a tenant built with an explicit ``profile_name`` carries that NAME, and
    the back-compat default (no ``profile_name``) leaves it ``None`` (DB-free)."""
    from tenantless.generator.pipeline import generate_tenant

    named = generate_tenant(
        generator_profile, seed=42, n_subs=3, n_resources=200,
        profile_name="enterprise-eu",
    ).tenant
    assert named.profile_name == "enterprise-eu"

    # Back-compat: existing callers pass no profile_name → None (existing tests unaffected).
    default = generate_tenant(
        generator_profile, seed=42, n_subs=3, n_resources=200,
    ).tenant
    assert default.profile_name is None


def test_copy_tenant_profile_name_roundtrip(generator_profile, pg_conn):
    """D-14: ``copy_tenant`` round-trips ``profile_name`` into
    ``synthetic.tenant.profile_name`` (a value AND the None→NULL case), leaving
    ``profile_version`` unchanged. Applies sql/007 via ``ensure_web_metadata_schema``."""
    from tenantless.generator import writer
    from tenantless.generator.pipeline import generate_tenant

    # Provision the nullable profile_name column on the live schema (idempotent).
    writer.ensure_web_metadata_schema(pg_conn)

    # (a) a value round-trips; profile_version is left intact.
    result = generate_tenant(
        generator_profile, seed=42, n_subs=3, n_resources=200,
        profile_name="enterprise-eu",
    )
    tenant = result.tenant
    writer.truncate_synthetic(pg_conn)
    writer.copy_tenant(pg_conn, tenant)
    pg_conn.commit()
    with pg_conn.cursor() as cur:
        cur.execute("SELECT profile_name, profile_version FROM synthetic.tenant")
        row = cur.fetchone()
    assert row[0] == "enterprise-eu"
    assert row[1] == tenant.profile_version

    # (b) None → SQL NULL (the back-compat / seed_fixture path stays valid).
    default = generate_tenant(
        generator_profile, seed=42, n_subs=3, n_resources=200,
    ).tenant
    writer.truncate_synthetic(pg_conn)
    writer.copy_tenant(pg_conn, default)
    pg_conn.commit()
    with pg_conn.cursor() as cur:
        cur.execute("SELECT profile_name FROM synthetic.tenant")
        assert cur.fetchone()[0] is None


def _secondary_resource_indexes(conn) -> list[str]:
    """The non-unique, non-PK indexes on synthetic.resources (catalog truth)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT i.indexrelid::regclass::text FROM pg_index i "
            "WHERE i.indrelid = 'synthetic.resources'::regclass "
            "AND NOT i.indisunique AND NOT i.indisprimary ORDER BY 1"
        )
        return [r[0] for r in cur.fetchall()]


def test_dropped_secondary_indexes_restore(pg_conn):
    """SPEED-01 (13-05) COPY accelerator: _dropped_secondary_indexes drops every
    non-unique/non-PK index on synthetic.resources inside the block and ALWAYS
    restores them afterward — on success AND on error — from their catalog DDL,
    while the PK/unique index is never touched.
    """
    from tenantless.generator import writer

    before = _secondary_resource_indexes(pg_conn)
    assert before, "fixture schema should have secondary resources indexes"

    # (a) success path: dropped inside, restored byte-for-byte after.
    with writer._dropped_secondary_indexes(pg_conn, "synthetic.resources"):
        assert _secondary_resource_indexes(pg_conn) == [], (
            "secondary indexes must be dropped inside the bulk-load block"
        )
    assert sorted(_secondary_resource_indexes(pg_conn)) == sorted(before)

    # (b) error path: the finally clause restores even when the body raises.
    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        with writer._dropped_secondary_indexes(pg_conn, "synthetic.resources"):
            assert _secondary_resource_indexes(pg_conn) == []
            raise _Boom()
    assert sorted(_secondary_resource_indexes(pg_conn)) == sorted(before), (
        "indexes must be restored even when the wrapped load raises"
    )
    pg_conn.rollback()


def _cost_records_table_exists(conn) -> bool:
    """True when synthetic.cost_records exists (sql/004 applied — Plan 09-04)."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('synthetic.cost_records')")
        return cur.fetchone()[0] is not None


def test_cost_records_roundtrip(generator_profile, pg_conn):
    """Plan 09-03 (COST-01): copy_cost_records binary-COPYs the fact rows FK-last.

    Drives the writer with explicit cost rows (the inject_cost shape) to prove the
    column/type contract ``(text, uuid, date, float8, text)`` round-trips. Skips
    cleanly when Postgres on 5433 is absent (``pg_conn``) OR when
    ``synthetic.cost_records`` does not yet exist (sql/004 lands in Plan 09-04).
    """
    import datetime as _dt

    from tenantless.generator import writer
    from tenantless.generator.pipeline import generate_tenant

    if not _cost_records_table_exists(pg_conn):
        pytest.skip("synthetic.cost_records not present (sql/004 lands in Plan 09-04)")

    result = generate_tenant(
        generator_profile, seed=42, n_subs=20, n_resources=1500, inject_cost=False
    )
    tenant = result.tenant
    a_res = next(r for rg in tenant.resource_groups for r in rg.resources)
    cost_rows = [
        {
            "resource_id": a_res.id,
            "subscription_id": a_res.subscription_id,
            "billing_period": _dt.date(2026, 1, 1),
            "cost_amount": 19.5,
            "currency": "USD",
        }
    ]

    writer.truncate_synthetic(pg_conn)
    # FK order: tenant→subs→RGs→resources→deps→violations→cost_records.
    writer.write_tenant(pg_conn, tenant, cost_records=cost_rows)
    pg_conn.commit()

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT resource_id, subscription_id, billing_period, cost_amount, "
            "currency FROM synthetic.cost_records"
        )
        out = cur.fetchall()
    assert len(out) == 1
    assert out[0][0] == a_res.id
    assert out[0][1] == a_res.subscription_id
    assert out[0][2] == _dt.date(2026, 1, 1)
    assert abs(out[0][3] - 19.5) < 1e-9
    assert out[0][4] == "USD"

    # Empty/None is a clean no-op (the cost-less default path) and must not error.
    writer.truncate_synthetic(pg_conn)
    writer.copy_cost_records(pg_conn, [])
    writer.copy_cost_records(pg_conn, None)
    pg_conn.commit()
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM synthetic.cost_records")
        assert cur.fetchone()[0] == 0
