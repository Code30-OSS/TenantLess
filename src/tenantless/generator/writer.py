"""psycopg3 binary-COPY seam (GEN-09) — the ONLY generator module importing psycopg.

Mirror-image of the analyzer's ``reader`` (the only duckdb seam): all Postgres
coupling lives here so the sampler layers stay pure and DB-free. Bulk writes use
``cursor.copy("... FROM STDIN (FORMAT BINARY)")`` + ``copy.set_types([...])`` +
``Jsonb(...)`` wrappers for JSONB columns (Pitfall 5), loaded in FK order
(tenant → subscriptions → resource_groups → resources → dependencies; Pitfall 6).

Truncation is destructive and scoped strictly to the ``synthetic`` schema; the
CLI guards it behind ``--force``/TTY confirmation (D-08). Column lists below are
STATIC code literals (never profile-derived) — values pass through parameterized
binary encoding, never string-concatenated SQL (threat T-02-02).
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Iterator

import psycopg
from psycopg.types.json import Jsonb

if TYPE_CHECKING:
    from .pipeline import Tenant

# The default DSN literal — kept as a named constant so parity tests can compare
# it against serve.py's default without being defeated by a runtime DATABASE_URL
# override (WR-01).
_DEFAULT_DATABASE_URL = "postgres://tenantless:tenantless_dev@localhost:5433/tenantless"
DATABASE_URL = os.environ.get("DATABASE_URL", _DEFAULT_DATABASE_URL)

# Tables truncated each run (D-07); scoped to the synthetic schema only.
_SYNTHETIC_TABLES = (
    "synthetic.tenant",
    "synthetic.subscriptions",
    "synthetic.resource_groups",
    "synthetic.resources",
    "synthetic.dependencies",
    "synthetic.violations",
    "synthetic.cost_records",
    "synthetic.role_assignments",  # before principals: FK principal_oid → principals
    "synthetic.principals",
    "synthetic.drift_records",  # before batches: FK batch_id → drift_batches
    "synthetic.drift_batches",
)


@contextmanager
def open_writer(conn_str: str | None = None) -> Iterator[psycopg.Connection]:
    """Open a Postgres connection for bulk writes; commit on success.

    Mirrors the analyzer ``reader.open_duckdb`` seam shape. Defaults to the
    project ``DATABASE_URL`` (port 5433).
    """
    conn = psycopg.connect(conn_str or DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _base_schema_sql_files() -> list[Path]:
    """The ordered sql/001 -> 002 -> 003 base-schema migration files (STATIC project
    files).

    Split out as a seam so the installed-package branch of
    :func:`ensure_base_schema` (no bundled ``sql/`` on disk) is unit-testable
    without a checkout. Resolves relative to the repo root, exactly like the
    ``ensure_cost/identity/drift`` twins locate their single file.
    """
    base = Path(__file__).resolve().parents[3] / "sql"
    return [
        base / "001_synthetic_tenant.sql",
        base / "002_cross_sub_dependencies.sql",
        base / "003_integrity_and_index.sql",
    ]


def ensure_base_schema(conn: psycopg.Connection) -> bool:
    """Apply the BASE synthetic schema (sql/001 -> 002 -> 003) on a bare Postgres.

    This closes the Docker-optional / bring-your-own-Postgres gap. ``docker
    compose up`` does two jobs: it provides Postgres 16 AND auto-applies
    sql/001..007 via the ``./sql -> /docker-entrypoint-initdb.d`` mount. The
    runtime already self-provisions sql/004..007 idempotently (the ``ensure_*``
    twins below), but the BASE tables (sql/001 tenant/subs/RGs/resources, sql/002
    dependencies/violations, sql/003 integrity + indexes) were applied ONLY by the
    Docker initdb mount — so a bare non-Docker PG16 was missing the base schema and
    ``tenantless generate`` failed. Wiring this into ``generate`` (and the explicit
    ``init-db`` command) lets anyone point ``DATABASE_URL`` at any reachable PG16
    and run the simulator end to end.

    WHY it needs a FUNCTION-LEVEL guard (unlike the 004..007 twins, which blindly
    read-and-execute): sql/001 and sql/002 use BARE ``CREATE TABLE`` / ``CREATE
    INDEX`` (NOT ``IF NOT EXISTS``) — only sql/003..007 are internally idempotent.
    Blindly running sql/001 against an already-migrated Docker volume would raise
    ``relation "synthetic.tenant" already exists``. The project convention (sql/003
    + sql/004 headers: "sql/001 and sql/002 are never edited") forbids adding
    ``IF NOT EXISTS`` to 001/002. So this function guards instead: it checks
    ``to_regclass('synthetic.tenant')`` and no-ops (returns False) if the base
    schema already exists (Docker volume / prior run), applying the full 001->003
    chain in order only on a bare DB.

    Returns True if the base schema was applied, False if it was already present
    (guard short-circuit) OR the bundled ``sql/`` files are absent (installed
    package with no checkout — those deployments provision via docker initdb). The
    statement text is STATIC project files read via ``read_text()``, never
    user/profile input — no injection surface (the project SQL bar, identical to
    the cost/identity/drift twins).
    """
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('synthetic.tenant')")
        if cur.fetchone()[0] is not None:
            return False  # base schema already present (Docker volume) — no-op
    files = _base_schema_sql_files()
    if not all(p.is_file() for p in files):
        return False  # installed package, no bundled sql/ (docker initdb path)
    # Apply 001 -> 002 -> 003 IN ORDER. Each file is a multi-statement script with
    # no bound params, so psycopg3 runs it via the simple-query protocol.
    for path in files:
        conn.execute(path.read_text(encoding="utf-8"))
    return True


def ensure_cost_schema(conn: psycopg.Connection) -> bool:
    """Apply the idempotent ``sql/004_cost.sql`` migration before writing cost rows.

    P1 fix: ``sql/004`` is only auto-applied by docker ``initdb`` (fresh volumes)
    and the testcontainers fixture — an EXISTING dev volume initialised before
    Phase 9 has no ``synthetic.cost_records`` table, so ``copy_cost_records``
    would fail at runtime for a cost-bearing profile. ``sql/004`` is fully
    idempotent (``CREATE … IF NOT EXISTS`` + a guarded FK ``DO`` block), so
    applying it here is safe to repeat.

    Locates the migration relative to the repo root (the dev/generate workflow
    runs from a checkout). Returns True if applied, False if the file was not
    found (e.g. an installed package with no bundled ``sql/`` — those deployments
    apply the schema via docker initdb). The statement text is a STATIC project
    file, never user/profile input — no injection surface.
    """
    sql_path = Path(__file__).resolve().parents[3] / "sql" / "004_cost.sql"
    if not sql_path.is_file():
        return False
    # psycopg3 runs a multi-statement script in one execute() when no params are
    # bound (simple-query protocol) — sql/004's DDL + DO block apply atomically.
    conn.execute(sql_path.read_text(encoding="utf-8"))
    return True


def ensure_identity_schema(conn: psycopg.Connection) -> bool:
    """Apply the idempotent ``sql/005_identity.sql`` migration before writing
    principals / role_assignments (Plan 10-01, IAM-01/IAM-02).

    Verbatim twin of :func:`ensure_cost_schema`, swapping ``004_cost.sql`` for
    ``005_identity.sql``. An existing dev volume initialised before Phase 10 has
    no ``synthetic.principals`` / ``synthetic.role_assignments`` tables, so
    :func:`copy_principals` / :func:`copy_role_assignments` would fail at runtime
    for an identity-bearing generate. ``sql/005`` is fully idempotent
    (``CREATE … IF NOT EXISTS`` + a guarded FK ``DO`` block), so applying it here
    is safe to repeat. The statement text is a STATIC project file, never
    user/profile input — no injection surface. Returns True if applied, False if
    the file was not found (installed package with no bundled ``sql/`` — those
    deployments apply the schema via docker initdb).
    """
    sql_path = Path(__file__).resolve().parents[3] / "sql" / "005_identity.sql"
    if not sql_path.is_file():
        return False
    conn.execute(sql_path.read_text(encoding="utf-8"))
    return True


def ensure_drift_schema(conn: psycopg.Connection) -> bool:
    """Apply the idempotent ``sql/006_drift.sql`` migration before applying or
    reverting configuration drift (Plan 11-01, DRIFT-03/DRIFT-04).

    Verbatim twin of :func:`ensure_identity_schema`, swapping ``005_identity.sql``
    for ``006_drift.sql``. An existing dev volume initialised before Phase 11 has
    no ``synthetic.drift_batches`` / ``synthetic.drift_records`` tables and no
    ``synthetic.resources.drift_deleted_at`` column, so the apply-drift /
    revert-drift writes — and the server's soft-delete list/detail filter — would
    fail at runtime. ``sql/006`` is fully idempotent (``CREATE … IF NOT EXISTS`` +
    ``ADD COLUMN IF NOT EXISTS`` + a guarded FK ``DO`` block), so applying it here
    is safe to repeat. The statement text is a STATIC project file, never
    user/profile input — no injection surface. Returns True if applied, False if
    the file was not found (installed package with no bundled ``sql/`` — those
    deployments apply the schema via docker initdb).
    """
    sql_path = Path(__file__).resolve().parents[3] / "sql" / "006_drift.sql"
    if not sql_path.is_file():
        return False
    conn.execute(sql_path.read_text(encoding="utf-8"))
    return True


def ensure_web_metadata_schema(conn: psycopg.Connection) -> bool:
    """Apply the idempotent ``sql/007_web_metadata.sql`` migration before writing
    the generation-profile NAME (Plan 14-05, WAPI-03 / D-14).

    Verbatim twin of :func:`ensure_drift_schema`, swapping ``006_drift.sql`` for
    ``007_web_metadata.sql``. An existing dev volume initialised before Phase 14
    has no ``synthetic.tenant.profile_name`` column, so :func:`copy_tenant` would
    fail at runtime when it writes ``profile_name``. ``sql/007`` is fully
    idempotent (``ADD COLUMN IF NOT EXISTS``), so applying it here is safe to
    repeat. The statement text is a STATIC project file, never user/profile input
    — no injection surface. Returns True if applied, False if the file was not
    found (installed package with no bundled ``sql/`` — those deployments apply
    the schema via docker initdb).
    """
    sql_path = Path(__file__).resolve().parents[3] / "sql" / "007_web_metadata.sql"
    if not sql_path.is_file():
        return False
    conn.execute(sql_path.read_text(encoding="utf-8"))
    return True


def schema_is_empty(conn: psycopg.Connection) -> bool:
    """True when the synthetic schema holds no tenant/subscription/RG rows.

    Used by the D-08 guard to allow a bare ``generate`` against a fresh schema.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT (SELECT count(*) FROM synthetic.tenant) "
            "+ (SELECT count(*) FROM synthetic.subscriptions) "
            "+ (SELECT count(*) FROM synthetic.resource_groups)"
        )
        return int(cur.fetchone()[0]) == 0


def truncate_synthetic(conn: psycopg.Connection) -> None:
    """TRUNCATE every EXISTING synthetic table (RESTART IDENTITY CASCADE), FK-safe.

    Only tables that currently exist are truncated: a synthetic table may be
    introduced by a later migration than the one applied to the target schema
    (e.g. ``synthetic.cost_records`` arrives with sql/004 in Plan 09-04 while the
    writer that lists it ships in Plan 09-03). ``to_regclass`` returns NULL for an
    absent table, so it is skipped rather than aborting the whole TRUNCATE. The
    table-name list is a STATIC code literal (``_SYNTHETIC_TABLES``), never
    user/profile input, so the membership check introduces no injection surface.
    """
    with conn.cursor() as cur:
        existing: list[str] = []
        for t in _SYNTHETIC_TABLES:
            cur.execute("SELECT to_regclass(%s)", (t,))
            if cur.fetchone()[0] is not None:
                existing.append(t)
        if not existing:
            return
        cur.execute(f"TRUNCATE {', '.join(existing)} RESTART IDENTITY CASCADE")


def copy_tenant(conn: psycopg.Connection, tenant: "Tenant") -> None:
    """Binary-COPY the single tenant row.

    D-14: ``profile_name`` (the generation-profile IDENTITY) is written after
    ``profile_version`` — a nullable ``text`` column (sql/007); psycopg writes a
    ``None`` value as SQL NULL, so a tenant built without a profile_name (the
    back-compat path) round-trips cleanly. STATIC column literal; values pass
    through parameterized binary encoding (never string-concatenated SQL).
    """
    cols = (
        "tenant_id, display_name, generated_at, profile_version, profile_name, "
        "scale_params"
    )
    with conn.cursor() as cur:
        with cur.copy(
            f"COPY synthetic.tenant ({cols}) FROM STDIN (FORMAT BINARY)"
        ) as copy:
            copy.set_types(
                ["uuid", "text", "timestamptz", "text", "text", "jsonb"]
            )
            from datetime import datetime, timezone

            copy.write_row(
                (
                    tenant.tenant_id,
                    tenant.display_name,
                    datetime.now(timezone.utc),
                    tenant.profile_version,
                    tenant.profile_name,  # None → SQL NULL (D-14 back-compat)
                    Jsonb(tenant.scale_params),
                )
            )


def copy_subscriptions(conn: psycopg.Connection, tenant: "Tenant") -> None:
    """Binary-COPY all subscriptions (FK → tenant; load after copy_tenant)."""
    cols = (
        "subscription_id, tenant_id, display_name, state, archetype, tags, "
        "authorization_source, spending_limit"
    )
    with conn.cursor() as cur:
        with cur.copy(
            f"COPY synthetic.subscriptions ({cols}) FROM STDIN (FORMAT BINARY)"
        ) as copy:
            copy.set_types(
                ["uuid", "uuid", "text", "text", "text", "jsonb", "text", "text"]
            )
            for s in tenant.subscriptions:
                copy.write_row(
                    (
                        s.subscription_id,
                        s.tenant_id,
                        s.display_name,
                        s.state,
                        s.archetype,
                        Jsonb(s.tags),
                        s.authorization_source,
                        s.spending_limit,
                    )
                )


def copy_resource_groups(conn: psycopg.Connection, tenant: "Tenant") -> None:
    """Binary-COPY all resource groups (FK → subscriptions; load after subs)."""
    cols = (
        "id, subscription_id, name, location, template_type, tags, "
        "provisioning_state"
    )
    with conn.cursor() as cur:
        with cur.copy(
            f"COPY synthetic.resource_groups ({cols}) FROM STDIN (FORMAT BINARY)"
        ) as copy:
            copy.set_types(
                ["text", "uuid", "text", "text", "text", "jsonb", "text"]
            )
            for rg in tenant.resource_groups:
                copy.write_row(
                    (
                        rg.id,
                        rg.subscription_id,
                        rg.name,
                        rg.location,
                        rg.template_type,
                        Jsonb(rg.tags),
                        rg.provisioning_state,
                    )
                )


# SPEED-01 (13-05) COPY-tuning lever: above this many resource rows, dropping the
# secondary (non-unique, non-PK) indexes on ``synthetic.resources`` before the bulk
# COPY and rebuilding them ONCE afterward beats maintaining them incrementally for
# every inserted row — the classic Postgres bulk-load accelerator (a single
# sort-based index build vs N per-row index maintenances + the bloat they leave).
# Below the threshold the per-row maintenance is cheaper than a full rebuild, so the
# plain direct COPY is kept and the write path stays BYTE-IDENTICAL to the
# pre-tuning writer for tests / demo loads (the lever is net-negative at small N).
_RESOURCES_INDEX_DROP_THRESHOLD = 50_000


@contextmanager
def _dropped_secondary_indexes(
    conn: psycopg.Connection, table: str
) -> Iterator[None]:
    """Drop every NON-unique, NON-primary index on ``table`` for the duration of a
    bulk load, then ALWAYS recreate them (even on exception) from their catalog
    definitions.

    Index identities and DDL come from the Postgres catalog (``pg_index`` /
    ``pg_get_indexdef``), never from user/profile input — the recreated DDL is
    byte-identical to what sql/001 + sql/003 declared. The catalog lookup binds
    ``table`` as a ``%s::regclass`` parameter; the DROP/CREATE then replay trusted
    catalog strings (an identifier/DDL cannot be a bound ``$N`` literal), so this
    introduces NO profile-derived SQL and NO injection surface — the COPY column
    contract below is untouched (project memory "mock-server SQL injection bar").

    Unique / primary-key indexes are NEVER dropped: the PK index backs the
    ``fk_violations_resource`` / ``fk_cost_resource`` foreign keys and the id
    uniqueness must hold throughout the load.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT i.indexrelid::regclass::text AS index_name, "
            "       pg_get_indexdef(i.indexrelid) AS index_def "
            "FROM pg_index i "
            "WHERE i.indrelid = %s::regclass "
            "  AND NOT i.indisunique "
            "  AND NOT i.indisprimary",
            (table,),
        )
        saved = cur.fetchall()  # [(index_name, index_def), ...] — all catalog-derived
    try:
        with conn.cursor() as cur:
            for index_name, _ in saved:
                cur.execute(f"DROP INDEX IF EXISTS {index_name}")
        yield
    finally:
        # Restore on success AND on error — the indexes must never be left dropped.
        # CAVEAT: if the COPY raised, the connection is in an aborted transaction,
        # so these CREATE INDEX statements will themselves raise
        # (InFailedSqlTransaction). We must NOT let that cleanup error MASK the
        # original COPY failure (which is the real, actionable cause). When an
        # exception is already propagating, recreate best-effort and re-raise the
        # ORIGINAL on cleanup failure; the caller's rollback undoes the
        # in-transaction DROP INDEX, so the dropped indexes are restored anyway.
        original = sys.exc_info()[1]
        try:
            with conn.cursor() as cur:
                for _, index_def in saved:
                    cur.execute(index_def)
        except Exception:
            if original is None:
                raise  # success path: a genuine index-restore failure must surface
            raise original  # preserve the COPY error; cleanup error becomes context


def copy_resources(conn: psycopg.Connection, tenant: "Tenant") -> None:
    """Binary-COPY all resources (load LAST; no FK but FK-order anyway, Pitfall 6).

    Column contract (sql/001 / RESEARCH lines 380-389):
    ``id, subscription_id, resource_group_name, name, type, location, tags, sku,
    kind, properties, provisioning_state, managed_by`` →
    ``text, uuid, text, text, text, text, jsonb, jsonb, text, jsonb, text, text``.
    JSONB columns are wrapped in ``Jsonb`` (Pitfall 5); ``sku`` may be NULL.

    SPEED-01 (13-05): for large loads (≥ ``_RESOURCES_INDEX_DROP_THRESHOLD`` rows —
    the 500K-scale path) the secondary indexes are dropped around the bulk COPY and
    rebuilt once, the standard Postgres bulk-load accelerator. Small loads keep the
    plain path (byte-identical to the pre-tuning writer).
    """
    n_res = sum(len(rg.resources) for rg in tenant.resource_groups)
    if n_res >= _RESOURCES_INDEX_DROP_THRESHOLD:
        with _dropped_secondary_indexes(conn, "synthetic.resources"):
            _copy_resources_rows(conn, tenant)
    else:
        _copy_resources_rows(conn, tenant)


def _copy_resources_rows(conn: psycopg.Connection, tenant: "Tenant") -> None:
    """The resources binary-COPY itself — STATIC column literal + ``set_types``
    binary encoding (GEN-09 + the SQL-injection bar). Split out from
    :func:`copy_resources` so the optional index drop/recreate can wrap it without
    touching the column contract.
    """
    cols = (
        "id, subscription_id, resource_group_name, name, type, location, "
        "tags, sku, kind, properties, provisioning_state, managed_by"
    )
    with conn.cursor() as cur:
        with cur.copy(
            f"COPY synthetic.resources ({cols}) FROM STDIN (FORMAT BINARY)"
        ) as copy:
            copy.set_types(
                [
                    "text", "uuid", "text", "text", "text", "text",
                    "jsonb", "jsonb", "text", "jsonb", "text", "text",
                ]
            )
            for rg in tenant.resource_groups:
                for r in rg.resources:
                    copy.write_row(
                        (
                            r.id,
                            r.subscription_id,
                            r.resource_group_name,
                            r.name,
                            r.type,
                            r.location,
                            Jsonb(r.tags),
                            Jsonb(r.sku) if r.sku is not None else None,
                            r.kind,
                            Jsonb(r.properties),
                            r.provisioning_state,
                            r.managed_by,
                        )
                    )


def copy_dependencies(
    conn: psycopg.Connection, rows: "Iterable[dict] | None" = None
) -> None:
    """Binary-COPY cross-subscription dependencies (FK-last; load after resources).

    Column contract (sql/002_cross_sub_dependencies.sql) — the SERIAL ``id`` PK is
    OMITTED so Postgres assigns it::

        (dependency_type, source_resource_id, target_resource_id,
         source_subscription, target_subscription)
        → text, text, text, uuid, uuid

    ``rows`` is an iterable of dicts with those five keys. **Phase 2 scope:** the
    dependency-row SEMANTICS (which resources actually depend cross-subscription,
    hub-spoke / centralized-logging topologies) are PHASE 5 — this plan only
    closes the GEN-09 COPY PATH, so the default is an empty list (a no-op that
    still exercises the binary-COPY surface end-to-end). Phase 5 populates real
    rows by passing them here; the column/type contract is fixed now.

    ``None``/empty is a clean no-op (the v1 default). Column literals are STATIC
    (never profile-derived); values pass through parameterized binary encoding —
    no string-concatenated SQL (threat T-02-10).
    """
    rows = list(rows or [])
    if not rows:
        return  # v1 default: COPY path exists; no rows to write (Phase 5 fills it)
    cols = (
        "dependency_type, source_resource_id, target_resource_id, "
        "source_subscription, target_subscription"
    )
    with conn.cursor() as cur:
        with cur.copy(
            f"COPY synthetic.dependencies ({cols}) FROM STDIN (FORMAT BINARY)"
        ) as copy:
            copy.set_types(["text", "text", "text", "uuid", "uuid"])
            for d in rows:
                copy.write_row(
                    (
                        d["dependency_type"],
                        d["source_resource_id"],
                        d["target_resource_id"],
                        d["source_subscription"],
                        d["target_subscription"],
                    )
                )


def copy_violations(
    conn: psycopg.Connection, rows: "Iterable[dict] | None" = None
) -> None:
    """Binary-COPY governance violations (load after resources; no FK).

    Verbatim sibling of :func:`copy_dependencies` with the ``Jsonb`` wrap from
    :func:`copy_resources`. Column contract (sql/002_cross_sub_dependencies.sql)
    — the SERIAL ``id`` PK is OMITTED so Postgres assigns it::

        (resource_id, violation_type, severity, detail)
        → text, text, text, jsonb

    ``rows`` is an iterable of dicts with those four keys; ``detail`` is a plain
    dict wrapped here via ``Jsonb`` (Pitfall 5). Each violation's resource_id
    references an already-written ``synthetic.resources`` row (load after
    :func:`copy_resources`), though no DB FK is declared.

    ``None``/empty is a clean no-op (matches :func:`copy_dependencies`). The
    column literal is STATIC (never profile-derived); every value passes through
    parameterized binary encoding — no string-concatenated SQL (threat T-05-SQLi,
    project memory "mock-server SQL injection bar").
    """
    rows = list(rows or [])
    if not rows:
        return  # no-op default, like copy_dependencies
    cols = "resource_id, violation_type, severity, detail"  # STATIC; SERIAL id omitted
    with conn.cursor() as cur:
        with cur.copy(
            f"COPY synthetic.violations ({cols}) FROM STDIN (FORMAT BINARY)"
        ) as copy:
            copy.set_types(["text", "text", "text", "jsonb"])
            for v in rows:
                copy.write_row(
                    (
                        v["resource_id"],
                        v["violation_type"],
                        v["severity"],
                        Jsonb(v["detail"]),  # Pitfall 5: JSONB must be wrapped
                    )
                )


def copy_cost_records(
    conn: psycopg.Connection, rows: "Iterable[dict] | None" = None
) -> None:
    """Binary-COPY per-resource cost rows (load after resources; FK → resources).

    Verbatim sibling of :func:`copy_violations` for the narrow ``cost_records``
    fact table (Plan 09-03, COST-01). Column contract (sql/004_cost.sql), with NO
    JSONB column (so no ``Jsonb`` wrap)::

        (resource_id, subscription_id, billing_period, cost_amount, currency)
        → text, uuid, date, float8, text

    ``rows`` is an iterable of dicts with those five keys (the shape
    :func:`tenantless.generator.cost.inject_cost` emits). ``billing_period`` is a
    ``datetime.date``; ``cost_amount`` a float; ``currency`` is ``"USD"`` (D-11).
    Each row's ``resource_id`` references an already-written ``synthetic.resources``
    row — the ``fk_cost_resource`` FK (sql/004) rejects any dangling reference at
    COPY time (the XSUB-06-analogue 0-dangling gate), so this MUST run after
    :func:`copy_resources`.

    ``None``/empty is a clean no-op (matches :func:`copy_violations`). The column
    literal is STATIC (never profile-derived); every value passes through
    parameterized binary encoding — no string-concatenated SQL (threat T-9-SQLi,
    project memory "mock-server SQL injection bar").
    """
    rows = list(rows or [])
    if not rows:
        return  # no-op default, like copy_violations
    cols = "resource_id, subscription_id, billing_period, cost_amount, currency"
    with conn.cursor() as cur:
        with cur.copy(
            f"COPY synthetic.cost_records ({cols}) FROM STDIN (FORMAT BINARY)"
        ) as copy:
            copy.set_types(["text", "uuid", "date", "float8", "text"])
            for c in rows:
                copy.write_row(
                    (
                        c["resource_id"],
                        c["subscription_id"],
                        c["billing_period"],
                        c["cost_amount"],
                        c["currency"],
                    )
                )


def copy_principals(
    conn: psycopg.Connection, rows: "Iterable[dict] | None" = None
) -> None:
    """Binary-COPY synthetic principals (load after subscriptions; no FK).

    Verbatim sibling of :func:`copy_cost_records` for the ``synthetic.principals``
    directory (Plan 10-01, IAM-01), with NO JSONB column (so no ``Jsonb`` wrap)::

        (oid, principal_type, display_name, app_id)
        → uuid, text, text, uuid

    ``rows`` is an iterable of dicts with those four keys (the shape
    :func:`tenantless.generator.identity.generate_principals` emits).
    ``display_name`` is ``None`` (ARM-opaque, IAM-01); ``app_id`` is a UUID for
    ServicePrincipals and ``None`` otherwise — both nullable columns. Principals
    load BEFORE :func:`copy_role_assignments` so the ``fk_ra_principal`` FK
    (sql/005) holds at COPY time (the 0-dangling gate, D-07).

    ``None``/empty is a clean no-op (matches :func:`copy_cost_records`). The column
    literal is STATIC (never profile-derived); every value passes through
    parameterized binary encoding — no string-concatenated SQL (project memory
    "mock-server SQL injection bar").
    """
    rows = list(rows or [])
    if not rows:
        return  # no-op default, like copy_cost_records
    cols = "oid, principal_type, display_name, app_id"
    with conn.cursor() as cur:
        with cur.copy(
            f"COPY synthetic.principals ({cols}) FROM STDIN (FORMAT BINARY)"
        ) as copy:
            copy.set_types(["uuid", "text", "text", "uuid"])
            for p in rows:
                copy.write_row(
                    (
                        p["oid"],
                        p["principal_type"],
                        p["display_name"],
                        p["app_id"],
                    )
                )


def copy_role_assignments(
    conn: psycopg.Connection, rows: "Iterable[dict] | None" = None
) -> None:
    """Binary-COPY role_assignments (load AFTER principals AND resources; FK → principals).

    Verbatim sibling of :func:`copy_principals` for the ``synthetic.role_assignments``
    fact table (Plan 10-01, IAM-02), no JSONB column::

        (assignment_id, subscription_id, principal_oid, principal_type,
         role_definition_id, scope)
        → uuid, uuid, uuid, text, text, text

    ``rows`` is an iterable of dicts with those six keys (the shape
    :func:`tenantless.generator.identity.assign_roles` emits). Each row's
    ``principal_oid`` references an already-written ``synthetic.principals`` row —
    the ``fk_ra_principal`` FK (sql/005) rejects any dangling reference at COPY
    time (the 0-dangling gate, D-07) — and its ``scope`` references a real
    subscription / RG / resource id (checked by the UNION anti-join test, not a
    single FK). MUST run after :func:`copy_principals` AND :func:`copy_resources`.

    ``None``/empty is a clean no-op (matches :func:`copy_principals`). The column
    literal is STATIC (never profile-derived); every value passes through
    parameterized binary encoding — no string-concatenated SQL.
    """
    rows = list(rows or [])
    if not rows:
        return  # no-op default, like copy_principals
    cols = (
        "assignment_id, subscription_id, principal_oid, principal_type, "
        "role_definition_id, scope"
    )
    with conn.cursor() as cur:
        with cur.copy(
            f"COPY synthetic.role_assignments ({cols}) FROM STDIN (FORMAT BINARY)"
        ) as copy:
            copy.set_types(["uuid", "uuid", "uuid", "text", "text", "text"])
            for a in rows:
                copy.write_row(
                    (
                        a["assignment_id"],
                        a["subscription_id"],
                        a["principal_oid"],
                        a["principal_type"],
                        a["role_definition_id"],
                        a["scope"],
                    )
                )


def write_tenant(
    conn: psycopg.Connection,
    tenant: "Tenant",
    dependencies: "Iterable[dict] | None" = None,
    violations: "Iterable[dict] | None" = None,
    cost_records: "Iterable[dict] | None" = None,
    principals: "Iterable[dict] | None" = None,
    role_assignments: "Iterable[dict] | None" = None,
) -> None:
    """COPY tenant → subscriptions → resource_groups → resources → dependencies
    → violations → cost_records → principals → role_assignments.

    All bulk tables load via psycopg3 binary COPY in FK order (Pitfall 6); the
    dependencies COPY runs after resources (GEN-09 closure), violations COPY after
    that, cost_records COPY after resources (its ``resource_id`` FK), then the
    identity tables LAST: principals (after subscriptions) and role_assignments
    (after principals AND resources — the three-way FK chain, D-07: ``principal_oid``
    → principals, ``scope`` → a real sub/RG/resource id). Truncation is the caller's
    responsibility (guarded by the CLI per D-08). ``dependencies`` / ``violations`` /
    ``cost_records`` / ``principals`` / ``role_assignments`` default to empty sets —
    a no-identity profile passes no identity rows (a clean no-op).
    """
    copy_tenant(conn, tenant)
    copy_subscriptions(conn, tenant)
    copy_resource_groups(conn, tenant)
    copy_resources(conn, tenant)
    copy_dependencies(conn, dependencies)
    copy_violations(conn, violations)  # after resources (Phase 5)
    copy_cost_records(conn, cost_records)  # FK-after-resources (Plan 09-03, COST-01)
    copy_principals(conn, principals)  # NEW — after subscriptions (Plan 10-01, IAM-01)
    copy_role_assignments(  # NEW — after principals AND resources (IAM-02, D-07)
        conn, role_assignments
    )
