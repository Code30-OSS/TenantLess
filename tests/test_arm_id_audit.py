"""D-04 fail-loud pre-cutover ARM-ID identity audit (INV-01).

The audit is a PRODUCTION migration helper (``writer.audit_arm_id_identity``)
invoked by the init-db / generate seam right after the fold functions are
provisioned. It asserts, against live PG, that for EVERY persisted id the legacy
``lower(id)`` equals the new ``synthetic.arm_id_key(id)`` (no non-ASCII divergence)
and that no two DISTINCT baseline ids fold to the SAME key (no collision). On any
divergence / collision it RAISES non-zero, NAMING the offending ARM ids ONLY (never
tags / properties / bodies / tokens — T-24ai-04), so 00a-ii cannot cut over the
CHECK / drop the old indexes / migrate predicates on a divergent estate.

These proofs WRAP the SAME production helper: they assert it passes clean on an
all-ASCII estate and RAISES (naming the id) on a seeded non-ASCII divergence
(proving the audit is non-vacuous). DB-backed + marked ``integration`` (live PG16).
"""

from __future__ import annotations

import os
import uuid

import pytest

from tenantless.generator import writer

pytestmark = pytest.mark.integration

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgres://tenantless:tenantless_dev@localhost:5433/tenantless",
)

# A fixed FK chain (tenant -> subscription) the seeded resource rows hang off, and a
# unique RG namespace so these proofs never collide with other suites' fixtures.
_TENANT = str(uuid.UUID(int=0xA17D))
_SUB = str(uuid.UUID(int=0xA17D0001))
_RG = "rg-arm-id-audit"
# A non-ASCII id whose PG lower() (en_US.utf8, locale-aware) folds the accented
# capital to its accented minuscule, while arm_id_key (ASCII-only) leaves it
# untouched -> lower(id) <> arm_id_key(id). Written as an escape so the source stays
# ASCII-clean regardless of the editing console encoding.
_DIVERGENT_ID = f"/subscriptions/{_SUB}/resourceGroups/{_RG}/providers/x/ÀLPHA"
_ASCII_ID = f"/subscriptions/{_SUB}/resourceGroups/{_RG}/providers/x/alpha"
_ASCII_ID_2 = f"/subscriptions/{_SUB}/resourceGroups/{_RG}/providers/x/beta"


@pytest.fixture
def pg_conn():
    """Autocommit psycopg conn with base + overlay + fold schema provisioned, or skip."""
    psycopg = pytest.importorskip("psycopg")
    try:
        conn = psycopg.connect(DATABASE_URL, connect_timeout=3, autocommit=True)
    except Exception as exc:  # noqa: BLE001 - any connection failure -> skip
        pytest.skip(f"Postgres on 5433 unavailable: {exc}")
    try:
        writer.ensure_base_schema(conn)
        writer.ensure_arm_overlay_schema(conn)
        writer.ensure_arm_id_key_schema(conn)
        _cleanup(conn)
        _seed_parents(conn)
        yield conn
    finally:
        _cleanup(conn)
        conn.close()


def _seed_parents(conn) -> None:
    conn.execute(
        "INSERT INTO synthetic.tenant (tenant_id, display_name, profile_version, scale_params) "
        "VALUES (%s, 'audit', 'v0', '{}'::jsonb) ON CONFLICT (tenant_id) DO NOTHING",
        (_TENANT,),
    )
    conn.execute(
        "INSERT INTO synthetic.subscriptions (subscription_id, tenant_id, display_name, archetype) "
        "VALUES (%s, %s, 'audit-sub', 'test') ON CONFLICT (subscription_id) DO NOTHING",
        (_SUB, _TENANT),
    )


def _cleanup(conn) -> None:
    conn.execute("DELETE FROM synthetic.resources WHERE resource_group_name = %s", (_RG,))
    conn.execute("DELETE FROM synthetic.subscriptions WHERE subscription_id = %s", (_SUB,))
    conn.execute("DELETE FROM synthetic.tenant WHERE tenant_id = %s", (_TENANT,))


def _seed_resource(conn, rid: str) -> None:
    conn.execute(
        "INSERT INTO synthetic.resources "
        "(id, subscription_id, resource_group_name, name, type, location) "
        "VALUES (%s, %s, %s, 'n', 'x', 'eastus') ON CONFLICT (id) DO NOTHING",
        (rid, _SUB, _RG),
    )


def test_audit_passes_clean_on_all_ascii_estate(pg_conn):
    """All-ASCII persisted ids -> lower == arm_id_key, no collision -> returns cleanly."""
    _seed_resource(pg_conn, _ASCII_ID)
    _seed_resource(pg_conn, _ASCII_ID_2)
    # Must NOT raise.
    writer.audit_arm_id_identity(pg_conn)


def test_audit_raises_and_names_non_ascii_divergence(pg_conn):
    """A seeded non-ASCII id whose lower() diverges -> helper RAISES, naming the id."""
    _seed_resource(pg_conn, _DIVERGENT_ID)
    with pytest.raises(writer.ArmIdIdentityAuditError) as exc:
        writer.audit_arm_id_identity(pg_conn)
    msg = str(exc.value)
    assert _DIVERGENT_ID in msg, f"the offending ARM id must be named: {msg!r}"


def test_audit_raises_on_fold_collision(pg_conn):
    """Two DISTINCT baseline ids folding to one key -> collision audit RAISES, names both."""
    lhs = f"/subscriptions/{_SUB}/resourceGroups/{_RG}/providers/x/RES"
    rhs = f"/subscriptions/{_SUB}/resourceGroups/{_RG}/providers/x/res"
    _seed_resource(pg_conn, lhs)
    _seed_resource(pg_conn, rhs)
    with pytest.raises(writer.ArmIdIdentityAuditError) as exc:
        writer.audit_arm_id_identity(pg_conn)
    msg = str(exc.value)
    assert lhs in msg and rhs in msg, f"both colliding ids must be named: {msg!r}"


def test_audit_output_names_ids_only_never_values(pg_conn):
    """The audit output carries ARM ids only — never tag / property values (T-24ai-04)."""
    secret = "s3cr3t-tag-value-must-not-leak"
    pg_conn.execute(
        "INSERT INTO synthetic.resources "
        "(id, subscription_id, resource_group_name, name, type, location, tags) "
        "VALUES (%s, %s, %s, 'n', 'x', 'eastus', %s::jsonb) ON CONFLICT (id) DO NOTHING",
        (_DIVERGENT_ID, _SUB, _RG, f'{{"owner": "{secret}"}}'),
    )
    with pytest.raises(writer.ArmIdIdentityAuditError) as exc:
        writer.audit_arm_id_identity(pg_conn)
    assert secret not in str(exc.value), "audit must NEVER emit tag/property values"
