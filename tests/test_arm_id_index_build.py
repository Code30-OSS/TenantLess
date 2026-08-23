"""Post-review proofs for the additive ARM-ID fold expression index builder
(``writer.build_arm_id_key_indexes_concurrently``) — INV-01, D-28, D-22a.

Two operator-review findings are pinned here against a live PG16 (``:5433``):

* FIX 1 — the fold-backed RG-name index ``idx_res_rg_ascii_fold`` must mirror the
  RETAINED sql/008 ``idx_res_rg_lower`` scoped/pagination shape
  ``(subscription_id, <fold>(resource_group_name), id)`` — NOT a single-column
  ``(<fold>(resource_group_name))`` — so the 00a-ii RG-predicate cutover keeps the
  same scoped + keyset-pagination plan the ``lower()`` index served.

* FIX 3 — ``CREATE INDEX CONCURRENTLY IF NOT EXISTS`` can FALSELY succeed on a
  leftover INVALID (interrupted-build) or stale-shaped same-named index: the
  ``IF NOT EXISTS`` skips it and the builder would return True on a broken index.
  The builder must catalog-validate ``indisvalid``/``indisready`` + the def AFTER
  each build and REPAIR (drop+rebuild) a leftover, or FAIL LOUD.

These are ADDITIVE-only: they touch only the NEW ``idx_res_arm_id_key`` /
``idx_res_rg_ascii_fold`` indexes — never the retained ``idx_res_lower_id`` /
``idx_res_rg_lower`` ``lower()`` indexes (D-22a). DB-backed, marked ``integration``.
"""

from __future__ import annotations

import os

import pytest

from tenantless.generator import writer

pytestmark = pytest.mark.integration

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgres://tenantless:tenantless_dev@localhost:5433/tenantless",
)


@pytest.fixture
def pg_conn():
    """Autocommit psycopg conn with base schema + fold functions provisioned, or skip.

    ``autocommit=True`` is REQUIRED (SP-5) — ``CREATE/DROP INDEX CONCURRENTLY``
    and the schema-ensure DDL cannot run inside a transaction. Self-provisions the
    base synthetic schema (so ``synthetic.resources`` exists), the retained
    sql/008 ``idx_res_rg_lower`` twin (NOT part of base schema — required by
    ``test_retained_lower_indexes_untouched`` and absent on a fresh CI database),
    and the sql/011 fold functions (so the expression indexes are buildable).
    """
    psycopg = pytest.importorskip("psycopg")
    try:
        conn = psycopg.connect(DATABASE_URL, connect_timeout=3, autocommit=True)
    except Exception as exc:  # noqa: BLE001 - any connection failure -> skip
        pytest.skip(f"Postgres on 5433 unavailable: {exc}")
    try:
        writer.ensure_base_schema(conn)
        writer.ensure_rg_index_schema(conn)
        writer.ensure_arm_id_key_schema(conn)
        yield conn
    finally:
        conn.close()


def _index_row(conn, name: str):
    """Return ``(indisvalid, indisready, pg_get_indexdef)`` for ``name`` or None."""
    return conn.execute(
        "SELECT i.indisvalid, i.indisready, pg_get_indexdef(i.indexrelid) "
        "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'synthetic' AND c.relname = %s",
        (name,),
    ).fetchone()


def _assert_ordered(indexdef: str, fragments: list[str]) -> None:
    """Assert every fragment appears in ``indexdef`` in the given left-to-right order."""
    pos = 0
    for frag in fragments:
        found = indexdef.find(frag, pos)
        assert found >= 0, (
            f"expected fragment {frag!r} (in order) missing from index def: {indexdef!r}"
        )
        pos = found + len(frag)


# --------------------------------------------------------------------------- #
# FIX 1 — the RG fold index must match sql/008's scoped/pagination shape
# --------------------------------------------------------------------------- #
def test_rg_ascii_fold_index_has_scoped_pagination_shape(pg_conn):
    """``idx_res_rg_ascii_fold`` must be
    ``(subscription_id, ascii_fold(resource_group_name), id)`` — mirroring the
    RETAINED sql/008 ``idx_res_rg_lower`` — NOT a single-column fold index."""
    # Clean slate so the builder creates it fresh under the current ON clause.
    pg_conn.execute("DROP INDEX CONCURRENTLY IF EXISTS synthetic.idx_res_rg_ascii_fold")
    assert writer.build_arm_id_key_indexes_concurrently(DATABASE_URL) is True

    row = _index_row(pg_conn, "idx_res_rg_ascii_fold")
    assert row is not None, "idx_res_rg_ascii_fold must exist after the build"
    valid, ready, indexdef = row
    assert valid and ready, f"index must be valid+ready: {row!r}"
    # subscription_id FIRST, then the ascii_fold(resource_group_name) key, then a
    # TRAILING id (keyset pagination) — in that column order.
    _assert_ordered(
        indexdef,
        ["subscription_id", "ascii_fold(resource_group_name)", "id)"],
    )


def test_retained_lower_indexes_untouched(pg_conn):
    """The additive builder must NOT touch the retained ``lower()`` indexes (D-22a)."""
    writer.build_arm_id_key_indexes_concurrently(DATABASE_URL)
    for name, expected in (
        ("idx_res_lower_id", "lower(id)"),
        ("idx_res_rg_lower", "lower(resource_group_name)"),
    ):
        row = _index_row(pg_conn, name)
        assert row is not None, f"retained index {name} must still exist"
        assert expected in row[2], f"{name} def changed: {row[2]!r}"


# --------------------------------------------------------------------------- #
# FIX 3 — CONCURRENTLY IF NOT EXISTS must not FALSELY succeed on a leftover
#         INVALID / stale-shaped same-named index
# --------------------------------------------------------------------------- #
def test_normal_build_yields_valid_indexes_with_expected_def(pg_conn):
    """After a clean build BOTH additive indexes are ``indisvalid`` AND
    ``indisready`` with their expected definition (the positive half of FIX 3)."""
    pg_conn.execute("DROP INDEX CONCURRENTLY IF EXISTS synthetic.idx_res_arm_id_key")
    pg_conn.execute("DROP INDEX CONCURRENTLY IF EXISTS synthetic.idx_res_rg_ascii_fold")
    assert writer.build_arm_id_key_indexes_concurrently(DATABASE_URL) is True
    for name, frags in (
        ("idx_res_arm_id_key", ["arm_id_key(id)"]),
        (
            "idx_res_rg_ascii_fold",
            ["subscription_id", "ascii_fold(resource_group_name)", "id)"],
        ),
    ):
        row = _index_row(pg_conn, name)
        assert row is not None, f"{name} must exist after build"
        assert row[0] and row[1], f"{name} must be valid+ready: {row!r}"
        _assert_ordered(row[2], frags)


def test_injected_invalid_leftover_is_repaired_not_falsely_accepted(pg_conn):
    """A leftover INVALID same-named index (as an interrupted CONCURRENTLY build
    leaves) must be DETECTED and REPAIRED — never silently accepted because
    ``IF NOT EXISTS`` skipped it and the builder returned True on a broken index."""
    # Ensure the index exists, then mark it INVALID in the catalog (superuser) to
    # simulate a leftover from an interrupted CONCURRENTLY build.
    writer.build_arm_id_key_indexes_concurrently(DATABASE_URL)
    pg_conn.execute(
        "UPDATE pg_index SET indisvalid = false "
        "WHERE indexrelid = 'synthetic.idx_res_arm_id_key'::regclass"
    )
    setup = _index_row(pg_conn, "idx_res_arm_id_key")
    assert setup is not None and setup[0] is False, (
        f"setup: idx_res_arm_id_key must be marked INVALID, got {setup!r}"
    )

    # The builder must not accept the invalid leftover as success.
    assert writer.build_arm_id_key_indexes_concurrently(DATABASE_URL) is True
    row = _index_row(pg_conn, "idx_res_arm_id_key")
    assert row is not None, "index must exist after repair"
    assert row[0] and row[1], (
        f"the INVALID leftover must be repaired to valid+ready, got {row!r}"
    )
    _assert_ordered(row[2], ["arm_id_key(id)"])


def test_stale_shape_leftover_is_repaired(pg_conn):
    """A pre-FIX-1 single-column ``idx_res_rg_ascii_fold`` left on an upgraded volume
    must be detected as stale and rebuilt to the scoped shape — ``IF NOT EXISTS``
    alone would silently keep the wrong-shaped index forever."""
    pg_conn.execute("DROP INDEX CONCURRENTLY IF EXISTS synthetic.idx_res_rg_ascii_fold")
    # The OLD single-column fold index (valid, but stale shape).
    pg_conn.execute(
        "CREATE INDEX idx_res_rg_ascii_fold ON synthetic.resources "
        "(synthetic.ascii_fold(resource_group_name))"
    )
    stale = _index_row(pg_conn, "idx_res_rg_ascii_fold")
    assert stale is not None and "subscription_id" not in stale[2], (
        f"setup: index must start single-column, got {stale!r}"
    )

    assert writer.build_arm_id_key_indexes_concurrently(DATABASE_URL) is True
    row = _index_row(pg_conn, "idx_res_rg_ascii_fold")
    assert row is not None and row[0] and row[1], f"must be valid+ready: {row!r}"
    _assert_ordered(
        row[2], ["subscription_id", "ascii_fold(resource_group_name)", "id)"]
    )
