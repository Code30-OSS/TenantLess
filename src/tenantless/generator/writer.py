"""psycopg3 binary-COPY seam — the ONLY generator module importing psycopg.

Mirror-image of the analyzer's ``reader`` (the only duckdb seam): all Postgres
coupling lives here so the sampler layers stay pure and DB-free. Bulk writes use
``cursor.copy("... FROM STDIN (FORMAT BINARY)")`` + ``copy.set_types([...])`` +
``Jsonb(...)`` wrappers for JSONB columns, loaded in FK order
(tenant → subscriptions → resource_groups → resources → dependencies).

Truncation is destructive and scoped strictly to the ``synthetic`` schema; the
CLI guards it behind ``--force``/TTY confirmation. Column lists below are
STATIC code literals (never profile-derived) — values pass through parameterized
binary encoding, never string-concatenated SQL.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Iterator

import click
import psycopg
from psycopg.types.json import Jsonb

from tenantless._resources import resource_path

if TYPE_CHECKING:
    from .pipeline import Tenant

# The default DSN literal — kept as a named constant so parity tests can compare
# it against serve.py's default without being defeated by a runtime DATABASE_URL
# override.
_DEFAULT_DATABASE_URL = "postgres://tenantless:tenantless_dev@localhost:5433/tenantless"
DATABASE_URL = os.environ.get("DATABASE_URL", _DEFAULT_DATABASE_URL)

# Tables truncated each run; scoped to the synthetic schema only.
_SYNTHETIC_TABLES = (
    "synthetic.tenant",
    "synthetic.subscriptions",
    "synthetic.resource_groups",
    "synthetic.resources",  # SYNRES-ALLOW[reset]: static truncate allowlist entry (twin of job.rs SYNTHETIC_TABLES), never a resolver read
    "synthetic.dependencies",
    "synthetic.violations",
    "synthetic.cost_records",
    "synthetic.role_assignments",  # before principals: FK principal_oid → principals
    "synthetic.principals",
    "synthetic.drift_records",  # before batches: FK batch_id → drift_batches
    "synthetic.drift_batches",
    # The ARM overlay copy-on-write plane (sql/009). LAST entry —
    # arm_overlay has no outgoing FK, so truncating it last is FK-safe. Without it a
    # `generate --force` re-seed leaks the previous tenant's `present=true` overlay ids.
    "synthetic.arm_overlay",
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


class PartialBaseSchemaError(click.ClickException):
    """Raised when the synthetic base schema is PARTIALLY applied.

    Subclasses :class:`click.ClickException` so an UNCAUGHT raise is formatted
    cleanly (non-zero exit, no traceback) by BOTH callers — ``generate`` and
    ``init-db`` — with zero per-caller handling. ``open_writer`` already rolls back
    on any exception, so a partial base surfaced here leaves the DB untouched.

    "Partial" means SOME base objects (of the exhaustive 19 in
    :data:`_BASE_SCHEMA_INVENTORY`) exist while others are missing. Blindly
    re-running sql/001+002 (bare ``CREATE TABLE``, not ``IF NOT EXISTS``) would
    error, and silently reporting "complete" would hide a corrupt/partly-migrated
    schema — so this is a hard, actionable failure naming the missing object(s).
    """


# EXHAUSTIVE base-object inventory — every CREATE TABLE / CREATE INDEX / named
# CONSTRAINT in sql/001 + sql/002 + sql/003 (6 relations + 2 FK constraints + 11
# indexes = 19). A base missing ANY of these is NOT complete. ``kind`` drives the
# catalog probe in :func:`_base_object_present`; ``name`` is the bare identifier
# (relations/indexes live in the ``synthetic`` schema, so they are probed as
# ``to_regclass('synthetic.<name>')``; constraints via ``pg_constraint.conname``).
# NOTE: index names are unqualified in the DDL but live in the synthetic schema.
_BASE_SCHEMA_INVENTORY: tuple[tuple[str, str], ...] = (
    # relations (6) — sql/001 (4) + sql/002 (2)
    ("relation", "tenant"),
    ("relation", "subscriptions"),
    ("relation", "resource_groups"),
    ("relation", "resources"),
    ("relation", "dependencies"),
    ("relation", "violations"),
    # constraints (2) — sql/003 guarded DO-blocks
    ("constraint", "fk_resources_subscription"),
    ("constraint", "fk_violations_resource"),
    # indexes (11) — sql/001 (6) + sql/002 (4) + sql/003 (1)
    ("index", "idx_subs_tenant"),
    ("index", "idx_rg_sub"),
    ("index", "idx_res_sub"),
    ("index", "idx_res_rg"),
    ("index", "idx_res_type"),
    ("index", "idx_res_location"),
    ("index", "idx_dep_source_sub"),
    ("index", "idx_dep_target_sub"),
    ("index", "idx_viol_resource"),
    ("index", "idx_viol_type"),
    ("index", "idx_res_lower_id"),
)


def _base_schema_sql_files() -> list[Path]:
    """The ordered sql/001 -> 002 -> 003 base-schema migration files (STATIC project
    files).

    Split out as a seam so the installed-package branch of
    :func:`ensure_base_schema` (no bundled ``sql/`` on disk) is unit-testable
    without a checkout. Resolves via the shared packaged-or-repo resolver, exactly
    like the ``ensure_cost/identity/drift`` twins locate their single file.
    """
    return [
        resource_path("sql", "001_synthetic_tenant.sql"),
        resource_path("sql", "002_cross_sub_dependencies.sql"),
        resource_path("sql", "003_integrity_and_index.sql"),
    ]


def _all_migration_sql_files() -> list[Path]:
    """Every migration file the full sql/001..010 chain needs, in order — the 3
    base files (001..003) plus the seven twin migrations (004..010).

    The pre-flight file gate in ``init-db`` (P2a) checks all ten exist BEFORE
    opening any DB transaction, so a missing bundled file (the packaging bug)
    aborts without touching the database. Resolves via the shared packaged-or-repo
    resolver; ``parts`` are STATIC filenames, never user input.
    """
    return _base_schema_sql_files() + [
        resource_path("sql", "004_cost.sql"),
        resource_path("sql", "005_identity.sql"),
        resource_path("sql", "006_drift.sql"),
        resource_path("sql", "007_web_metadata.sql"),
        resource_path("sql", "008_rg_lower_index.sql"),
        resource_path("sql", "009_arm_overlay.sql"),
        # 011 (the identity fold functions) is applied BEFORE 010 so the functions
        # exist before any future sql/010 that references arm_id_key (00a-ii) — the
        # boot-safety ordering that mirrors the Rust boot preflight.
        resource_path("sql", "011_arm_id_key.sql"),
        resource_path("sql", "010_arm_resolver.sql"),
    ]


def _base_object_present(conn: psycopg.Connection, kind: str, name: str) -> bool:
    """Catalog-probe seam: True if the base object ``name`` (a ``kind`` of
    ``relation`` / ``index`` / ``constraint``) already exists.

    Split out so :func:`ensure_base_schema`'s complete/partial/bare branch logic is
    monkeypatch-testable DB-free. Relations and indexes both live in the
    ``synthetic`` schema and are probed via ``to_regclass('synthetic.<name>')``;
    constraints are looked up by ``pg_constraint.conname``. The ``name`` is bound as
    a ``%s`` parameter (never spliced) — the project SQL bar.
    """
    with conn.cursor() as cur:
        if kind == "constraint":
            cur.execute("SELECT 1 FROM pg_constraint WHERE conname = %s", (name,))
            return cur.fetchone() is not None
        # relation or index — both qualified into the synthetic schema.
        cur.execute("SELECT to_regclass(%s)", (f"synthetic.{name}",))
        return cur.fetchone()[0] is not None


def ensure_base_schema(conn: psycopg.Connection) -> bool:
    """Apply the BASE synthetic schema (sql/001 -> 002 -> 003) on a bare Postgres.

    This closes the Docker-optional / bring-your-own-Postgres gap. ``docker
    compose up`` does two jobs: it provides Postgres 16 AND auto-applies
    sql/001..009 via the ``./sql -> /docker-entrypoint-initdb.d`` mount. The
    runtime already self-provisions sql/004..009 idempotently (the ``ensure_*``
    twins below), but the BASE tables (sql/001 tenant/subs/RGs/resources, sql/002
    dependencies/violations, sql/003 integrity + indexes) were applied ONLY by the
    Docker initdb mount — so a bare non-Docker PG16 was missing the base schema and
    ``tenantless generate`` failed. Wiring this into ``generate`` (and the explicit
    ``init-db`` command) lets anyone point ``DATABASE_URL`` at any reachable PG16
    and run the simulator end to end.

    WHY it needs a FUNCTION-LEVEL guard (unlike the 004..008 twins, which blindly
    read-and-execute): sql/001 and sql/002 use BARE ``CREATE TABLE`` / ``CREATE
    INDEX`` (NOT ``IF NOT EXISTS``) — only sql/003..007 are internally idempotent.
    Blindly running sql/001 against an already-migrated Docker volume would raise
    ``relation "synthetic.tenant" already exists``. The project convention (sql/003
    + sql/004 headers: "sql/001 and sql/002 are never edited") forbids adding
    ``IF NOT EXISTS`` to 001/002. So this function guards instead: it checks
    ``to_regclass('synthetic.tenant')`` and no-ops (returns False) if the base
    schema already exists (Docker volume / prior run), applying the full 001->003
    chain in order only on a bare DB.

    EXHAUSTIVE completeness check. The old guard probed ONLY
    ``to_regclass('synthetic.tenant')``: a base that had the tenant table but was
    missing (say) an index or a later table read as "complete", so ``generate`` /
    ``init-db`` ran on a partly-migrated schema and later failed cryptically. This
    now checks the full :data:`_BASE_SCHEMA_INVENTORY` (6 relations + 2 FK
    constraints + 11 indexes) and resolves to exactly three outcomes:

    - **complete** (none of the 19 missing) -> ``False`` no-op (unchanged contract);
    - **partial** (some present AND some missing) -> raise
      :class:`PartialBaseSchemaError` naming the missing object(s); applies nothing.
      ``open_writer`` rolls back, so the DB is left untouched;
    - **bare** (none present) -> the original file-existence guard then the
      001->002->003 apply, returning ``True`` (or ``False`` when the bundled
      ``sql/`` is absent — the installed-package / docker-initdb deployment).

    The statement text is STATIC project files read via ``read_text()``, never
    user/profile input — no injection surface (the project SQL bar, identical to
    the cost/identity/drift twins).
    """
    present: list[tuple[str, str]] = []
    missing: list[tuple[str, str]] = []
    for kind, name in _BASE_SCHEMA_INVENTORY:
        (present if _base_object_present(conn, kind, name) else missing).append(
            (kind, name)
        )

    if not missing:
        return False  # base schema fully present (Docker volume / prior run) — no-op

    if present:
        # PARTIAL: some base objects exist, some are gone. Refuse to blindly re-run
        # bare CREATE (it would error) and refuse to falsely report complete.
        def _fmt(kind: str, name: str) -> str:
            return f"constraint {name}" if kind == "constraint" else f"{kind} synthetic.{name}"

        raise PartialBaseSchemaError(
            "the synthetic base schema is PARTIALLY applied — missing "
            + f"{len(missing)} of {len(_BASE_SCHEMA_INVENTORY)} base object(s): "
            + ", ".join(_fmt(k, n) for k, n in missing)
            + ". Restore the missing object(s), or drop and re-provision the base "
            "schema (sql/001..003) against a clean database."
        )

    # BARE: nothing present — apply the full 001 -> 003 chain (file-existence guard
    # preserves the installed-package / docker-initdb path).
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
    the cost migration existed has no ``synthetic.cost_records`` table, so ``copy_cost_records``
    would fail at runtime for a cost-bearing profile. ``sql/004`` is fully
    idempotent (``CREATE … IF NOT EXISTS`` + a guarded FK ``DO`` block), so
    applying it here is safe to repeat.

    Locates the migration via the shared packaged-or-repo resolver (installed
    wheel first, repo checkout fallback). Returns True if applied, False if the
    file was not found (e.g. an installed package with no bundled ``sql/`` — those
    deployments apply the schema via docker initdb). The statement text is a
    STATIC project file, never user/profile input — no injection surface.
    """
    sql_path = resource_path("sql", "004_cost.sql")
    if not sql_path.is_file():
        return False
    # psycopg3 runs a multi-statement script in one execute() when no params are
    # bound (simple-query protocol) — sql/004's DDL + DO block apply atomically.
    conn.execute(sql_path.read_text(encoding="utf-8"))
    return True


def ensure_identity_schema(conn: psycopg.Connection) -> bool:
    """Apply the idempotent ``sql/005_identity.sql`` migration before writing
    principals / role_assignments.

    Verbatim twin of :func:`ensure_cost_schema`, swapping ``004_cost.sql`` for
    ``005_identity.sql``. An existing dev volume initialised before the identity migration existed has
    no ``synthetic.principals`` / ``synthetic.role_assignments`` tables, so
    :func:`copy_principals` / :func:`copy_role_assignments` would fail at runtime
    for an identity-bearing generate. ``sql/005`` is fully idempotent
    (``CREATE … IF NOT EXISTS`` + a guarded FK ``DO`` block), so applying it here
    is safe to repeat. The statement text is a STATIC project file, never
    user/profile input — no injection surface. Returns True if applied, False if
    the file was not found (installed package with no bundled ``sql/`` — those
    deployments apply the schema via docker initdb).
    """
    sql_path = resource_path("sql", "005_identity.sql")
    if not sql_path.is_file():
        return False
    conn.execute(sql_path.read_text(encoding="utf-8"))
    return True


def ensure_drift_schema(conn: psycopg.Connection) -> bool:
    """Apply the idempotent ``sql/006_drift.sql`` migration before applying or
    reverting configuration drift.

    Verbatim twin of :func:`ensure_identity_schema`, swapping ``005_identity.sql``
    for ``006_drift.sql``. An existing dev volume initialised before the drift migration existed has
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
    sql_path = resource_path("sql", "006_drift.sql")
    if not sql_path.is_file():
        return False
    conn.execute(sql_path.read_text(encoding="utf-8"))
    return True


def ensure_web_metadata_schema(conn: psycopg.Connection) -> bool:
    """Apply the idempotent ``sql/007_web_metadata.sql`` migration before writing
    the generation-profile NAME.

    Verbatim twin of :func:`ensure_drift_schema`, swapping ``006_drift.sql`` for
    ``007_web_metadata.sql``. An existing dev volume initialised before the web-metadata migration existed
    has no ``synthetic.tenant.profile_name`` column, so :func:`copy_tenant` would
    fail at runtime when it writes ``profile_name``. ``sql/007`` is fully
    idempotent (``ADD COLUMN IF NOT EXISTS``), so applying it here is safe to
    repeat. The statement text is a STATIC project file, never user/profile input
    — no injection surface. Returns True if applied, False if the file was not
    found (installed package with no bundled ``sql/`` — those deployments apply
    the schema via docker initdb).
    """
    sql_path = resource_path("sql", "007_web_metadata.sql")
    if not sql_path.is_file():
        return False
    conn.execute(sql_path.read_text(encoding="utf-8"))
    return True


def ensure_rg_index_schema(conn: psycopg.Connection) -> bool:
    """Apply the idempotent ``sql/008_rg_lower_index.sql`` migration — the functional
    index backing the case-insensitive resource-group predicate added in v1.1.8.

    Verbatim twin of :func:`ensure_web_metadata_schema`, swapping ``007_web_metadata.sql``
    for ``008_rg_lower_index.sql``. Applied UNCONDITIONALLY by ``generate``/``init-db`` so
    a database provisioned before v1.1.10 gains the index automatically on the next run —
    the index is deliberately a twin migration, NOT a base-schema object, so an existing
    healthy install is never reported as an incomplete base schema over an additive
    performance index. ``sql/008`` is fully idempotent (``CREATE INDEX IF NOT EXISTS``), so
    applying it here is safe to repeat. The statement text is a STATIC project file, never
    user/profile input — no injection surface. Returns True if applied, False if the file
    was not found (installed package with no bundled ``sql/`` — those deployments apply the
    schema via docker initdb).
    """
    sql_path = resource_path("sql", "008_rg_lower_index.sql")
    if not sql_path.is_file():
        return False
    conn.execute(sql_path.read_text(encoding="utf-8"))
    return True


def ensure_arm_overlay_schema(conn: psycopg.Connection) -> bool:
    """Apply the idempotent ``sql/009_arm_overlay.sql`` migration — the ARM
    overlay/tombstone/revision substrate (``synthetic.arm_overlay`` + the unowned revision
    sequence + the revision trigger + the NAMED row-model CHECK constraints).

    Verbatim twin of :func:`ensure_rg_index_schema`, swapping ``008_rg_lower_index.sql`` for
    ``009_arm_overlay.sql``. Applied UNCONDITIONALLY by ``init-db`` (and available for
    BYO-Postgres completeness) so a database provisioned before the overlay existed gains the overlay
    substrate automatically on the next ``init-db``. NOT wired into ``generate`` provisioning
    — ``generate`` writes no overlay data (the overlay is populated only by the
    write plane), and the Rust server self-provisions it at boot via
    ``ensure_arm_overlay_schema``. ``sql/009`` is fully idempotent (``CREATE ... IF NOT
    EXISTS`` + guarded ``DO`` blocks), so applying it here is safe to repeat. The statement
    text is a STATIC project file, never user/profile input — no injection surface. Returns
    True if applied, False if the file was not found (installed package with no bundled
    ``sql/`` — those deployments apply the schema via docker initdb).
    """
    sql_path = resource_path("sql", "009_arm_overlay.sql")
    if not sql_path.is_file():
        return False
    # Transaction-scoped preamble RELOCATED from sql/009 so the .sql file stays
    # honest under Docker initdb autocommit. ``conn.transaction()`` GUARANTEES an explicit
    # transaction (a savepoint when init-db's outer writer tx is already open), so the bounded
    # ``lock_timeout`` and the serializing advisory lock actually take effect and cover the DDL.
    with conn.transaction():
        conn.execute("SET LOCAL lock_timeout = '3s'")
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('synthetic.arm_overlay:009'))")
        conn.execute(sql_path.read_text(encoding="utf-8"))
    return True


def ensure_arm_id_key_schema(conn: psycopg.Connection) -> bool:
    """Apply the idempotent ``sql/011_arm_id_key.sql`` migration — the ARM-ID identity
    fold functions (``synthetic.ascii_fold`` primitive + ``synthetic.arm_id_key``
    whole-ID wrapper, both IMMUTABLE STRICT ``translate()`` functions; INV-01, D-01/D-02/D-28).

    Verbatim twin of :func:`ensure_arm_overlay_schema`, swapping ``009_arm_overlay.sql`` for
    ``011_arm_id_key.sql``. Applied UNCONDITIONALLY by ``generate`` / ``init-db`` so a database
    provisioned before the fold existed gains the functions automatically on the next run.
    ``sql/011`` is ``CREATE OR REPLACE FUNCTION`` only — a no-op-equivalent re-definition on an
    already-migrated schema, taking NO table lock — so applying it here is safe to repeat and
    needs no advisory-lock preamble (unlike 009/010, function redefinition does not contend).

    ADDITIVE + behaviour-neutral (D-22a): this ONLY defines the two functions. It changes NO
    CHECK, builds NO index, edits NO view, and cuts over NO predicate. In THIS unit NOTHING
    consumes the functions (``sql/010`` still references ``lower(...)`` and is UNCHANGED); it is
    applied BEFORE ``ensure_arm_resolver_schema`` (010) only so the functions EXIST before any
    future 010 that references ``arm_id_key`` (00a-ii) — the boot-safety ordering.

    The statement text is a STATIC project file, never user/profile input — no injection
    surface. Returns True if applied, False if the file was not found (installed package with
    no bundled ``sql/`` — those deployments apply the schema via docker initdb).
    """
    sql_path = resource_path("sql", "011_arm_id_key.sql")
    if not sql_path.is_file():
        return False
    conn.execute(sql_path.read_text(encoding="utf-8"))
    return True


class ArmIdIdentityAuditError(click.ClickException):
    """Raised when the pre-cutover ARM-ID identity audit (D-04) finds divergence/collision.

    Subclasses :class:`click.ClickException` so an UNCAUGHT raise is formatted cleanly
    (non-zero exit, no traceback) by BOTH callers — ``generate`` and ``init-db`` — with
    zero per-caller handling, exactly like :class:`PartialBaseSchemaError`. ``open_writer``
    already rolls back on any exception, so a tripped audit leaves the DB untouched and NO
    identity is silently changed / merged.

    The message NAMES the offending ARM ids ONLY — never tags / properties / bodies /
    tokens (T-24ai-04) — honouring the project no-request-body logging rule.
    """


def audit_arm_id_identity(conn: psycopg.Connection) -> None:
    """Fail-loud pre-cutover ARM-ID identity audit (D-04) — a PRODUCTION migration helper.

    Runs THREE read-only checks against live PG (requires ``synthetic.arm_id_key`` to
    exist — call AFTER :func:`ensure_arm_id_key_schema`):

    1. **Baseline divergence** — any ``synthetic.resources`` id whose legacy ``lower(id)``
       differs from the new ``synthetic.arm_id_key(id)`` (a non-ASCII id that a
       locale-aware ``lower()`` would fold differently than the ASCII-only key).
    2. **Overlay divergence** — the same check on the ``synthetic.arm_overlay`` stored
       ``id_lower`` derivation (skipped when the overlay table is absent, e.g. a bare
       ``generate`` provisioning path that never provisions the overlay).
    3. **Fold collision** — two DISTINCT baseline ids that fold to the SAME
       ``arm_id_key`` (a case-only duplicate the identity contract would silently merge).

    Each check must return 0 rows; ANY hit raises :class:`ArmIdIdentityAuditError`
    naming the offending ARM ids ONLY (bounded to the first 50 per check; never emits
    tag / property / body / token values — T-24ai-04). This is the gate 00a-ii MUST pass
    BEFORE it converts the ``arm_overlay`` CHECK, drops the retained ``lower()`` indexes,
    or cuts over any predicate. It is ADDITIVE + behaviour-neutral on the current
    all-ASCII estate (it only trips on real divergence / collision) and changes NO
    identity itself. All values bind as ``%s``; relation/column names are static.
    """
    _CAP = 50
    problems: list[str] = []
    with conn.cursor() as cur:
        # (1) baseline resources: lower(id) <> arm_id_key(id)
        cur.execute(
            "SELECT id FROM synthetic.resources "
            "WHERE lower(id) <> synthetic.arm_id_key(id) "
            "ORDER BY id LIMIT %s",
            (_CAP,),
        )
        div = [r[0] for r in cur.fetchall()]
        if div:
            problems.append(
                f"{len(div)} baseline id(s) whose lower(id) <> arm_id_key(id): "
                + ", ".join(div)
            )

        # (2) overlay stored id_lower derivation — only if the overlay table exists.
        cur.execute("SELECT to_regclass('synthetic.arm_overlay')")
        if cur.fetchone()[0] is not None:
            cur.execute(
                "SELECT id FROM synthetic.arm_overlay "
                "WHERE id_lower <> synthetic.arm_id_key(id) "
                "ORDER BY id LIMIT %s",
                (_CAP,),
            )
            odiv = [r[0] for r in cur.fetchall()]
            if odiv:
                problems.append(
                    f"{len(odiv)} overlay id(s) whose id_lower <> arm_id_key(id): "
                    + ", ".join(odiv)
                )

        # (3) fold collision: two DISTINCT baseline ids folding to one key.
        cur.execute(
            "SELECT array_agg(id ORDER BY id) FROM synthetic.resources "
            "GROUP BY synthetic.arm_id_key(id) HAVING count(DISTINCT id) > 1 "
            "ORDER BY 1 LIMIT %s",
            (_CAP,),
        )
        collisions = ["{" + ", ".join(row[0]) + "}" for row in cur.fetchall()]
        if collisions:
            problems.append(
                f"{len(collisions)} fold-collision group(s) (distinct ids sharing one "
                "arm_id_key): " + "; ".join(collisions)
            )

    if problems:
        raise ArmIdIdentityAuditError(
            "ARM-ID identity audit FAILED before cutover — refusing to change identity "
            "or merge collisions (D-04). Offending ARM ids: "
            + " | ".join(problems)
            + ". Resolve the divergence/collision (rename the non-ASCII id, or de-dup the "
            "case-only collision) before migrating identity to arm_id_key."
        )


# Advisory-lock key serializing concurrent additive-index builders (a session lock,
# NOT a transaction lock — CONCURRENTLY cannot run inside a transaction).
_ARM_ID_KEY_INDEX_LOCK = "synthetic.arm_id_key_indexes:011"

# The additive ARM-ID fold expression indexes, as
# ``(index_name, ON-clause, ordered pg_get_indexdef fragments)`` — a STATIC project
# constant (never user/profile input), so interpolating any of these into DDL below
# introduces NO injection surface. The ``fragments`` are the substrings that MUST
# appear, in left-to-right column order, in ``pg_get_indexdef`` for the built index to
# be considered the CORRECT (non-stale) shape (FIX 3 catalog validation).
_ARM_ID_KEY_INDEX_SPECS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "idx_res_arm_id_key",
        "ON synthetic.resources (synthetic.arm_id_key(id))",
        ("arm_id_key(id)",),
    ),
    (
        "idx_res_rg_ascii_fold",
        "ON synthetic.resources "
        "(subscription_id, synthetic.ascii_fold(resource_group_name), id)",
        ("subscription_id", "ascii_fold(resource_group_name)", "id)"),
    ),
)


class ArmIdIndexBuildError(click.ClickException):
    """Raised when an additive ARM-ID fold index cannot be built VALID even after a
    repair attempt (FIX 3).

    Subclasses :class:`click.ClickException` so an uncaught raise exits non-zero
    with a clean, actionable message (no traceback) from both callers — ``generate``
    and ``init-db`` — exactly like :class:`ArmIdIdentityAuditError`. This is the
    fail-loud half of the leftover-index guard: a ``CREATE INDEX CONCURRENTLY
    IF NOT EXISTS`` that got FALSELY skipped by an interrupted-build INVALID index (or
    a stale-shaped same-named index) must never be reported as success.
    """


def _validate_arm_id_index(
    conn: psycopg.Connection, name: str, fragments: tuple[str, ...]
) -> tuple[bool, str]:
    """Catalog-validate the just-built index ``name`` (FIX 3).

    Reads ``pg_index.indisvalid`` / ``indisready`` + ``pg_get_indexdef`` (the same
    trusted catalog infra :func:`_dropped_secondary_indexes` uses). Returns
    ``(ok, detail)`` where ``ok`` is True only when the index is PRESENT, VALID,
    READY, and its definition contains ``fragments`` in the given column order — so a
    leftover INVALID (interrupted-build) index OR a stale-shaped same-named index
    (which ``IF NOT EXISTS`` would silently keep) is reported as NOT-ok. ``name`` is
    bound as ``%s``; the fold-schema qualifier is a static literal.
    """
    row = conn.execute(
        "SELECT i.indisvalid, i.indisready, pg_get_indexdef(i.indexrelid) "
        "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'synthetic' AND c.relname = %s",
        (name,),
    ).fetchone()
    if row is None:
        return False, f"{name} is absent after the build"
    valid, ready, indexdef = row
    if not valid or not ready:
        return False, (
            f"{name} is INVALID/not-ready (indisvalid={valid}, indisready={ready}) — "
            "a leftover from an interrupted CONCURRENTLY build"
        )
    pos = 0
    for frag in fragments:
        found = indexdef.find(frag, pos)
        if found < 0:
            return False, (
                f"{name} definition is stale/unexpected (missing {frag!r} in expected "
                f"column order): {indexdef}"
            )
        pos = found + len(frag)
    return True, indexdef


def _build_and_validate_arm_id_index(
    conn: psycopg.Connection, name: str, on_clause: str, fragments: tuple[str, ...]
) -> None:
    """Build ONE additive fold index CONCURRENTLY, then catalog-validate + repair it
    (FIX 3).

    ``CREATE INDEX CONCURRENTLY IF NOT EXISTS`` can FALSELY succeed on a leftover
    same-named index that is INVALID (an interrupted prior build) or stale-shaped: the
    ``IF NOT EXISTS`` skips it and this function would otherwise report success on a
    broken/wrong index. So after the build we validate ``indisvalid``/``indisready`` +
    the def; on failure we REPAIR ONCE (``DROP INDEX CONCURRENTLY`` the leftover +
    rebuild) and re-validate. If it STILL cannot be made valid we FAIL LOUD
    (:class:`ArmIdIndexBuildError`). ``name`` / ``on_clause`` come from the STATIC
    :data:`_ARM_ID_KEY_INDEX_SPECS` — no injection surface.
    """
    conn.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} {on_clause}")
    ok, detail = _validate_arm_id_index(conn, name, fragments)
    if ok:
        return
    # Repair: the IF NOT EXISTS FALSELY skipped a leftover INVALID / stale-shaped
    # index. Drop it CONCURRENTLY (deadlock-safe like the build) and rebuild once.
    conn.execute(f"DROP INDEX CONCURRENTLY IF EXISTS synthetic.{name}")
    conn.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} {on_clause}")
    ok, detail = _validate_arm_id_index(conn, name, fragments)
    if not ok:
        raise ArmIdIndexBuildError(
            f"additive ARM-ID index {name} could not be built valid after a repair "
            f"attempt: {detail}. Manually drop the leftover index "
            f"(DROP INDEX CONCURRENTLY IF EXISTS synthetic.{name}) and re-run the "
            "provisioning (init-db / generate)."
        )


def build_arm_id_key_indexes_concurrently(conn_str: str | None = None) -> bool:
    """Build the TWO additive ARM-ID fold expression indexes CONCURRENTLY (INV-01, D-28).

    Creates, on a DEDICATED autocommit connection (``CREATE INDEX CONCURRENTLY`` CANNOT
    run inside a transaction, so it must NOT ride the boot-time ``apply_schema_batch``
    path nor the init-db/generate writer transaction):

    * ``idx_res_arm_id_key`` ON ``synthetic.resources (synthetic.arm_id_key(id))`` — the
      identity index the 00a-ii predicate cutover will hit (single-column, mirroring
      sql/003's single-column ``lower(id)`` identity index);
    * ``idx_res_rg_ascii_fold`` ON
      ``synthetic.resources (subscription_id, synthetic.ascii_fold(resource_group_name), id)``
      — the fold-backed RG-name index (D-28) the 00a-ii RG-predicate cutover will use. It
      MIRRORS the RETAINED sql/008 ``idx_res_rg_lower``
      ``(subscription_id, lower(resource_group_name), id)`` shape EXACTLY so the cutover
      keeps the same scoped (``subscription_id`` prefix) + keyset-pagination (trailing
      ``id``) plan — a single-column fold index could serve neither.

    ADDITIVE (D-22a): both are created ALONGSIDE the RETAINED ``idx_res_lower_id`` (sql/003)
    and ``idx_res_rg_lower`` (sql/008) ``lower()`` indexes — this unit drops NOTHING and cuts
    over NO predicate; the old-index drops + predicate cutover are deferred to 00a-ii after
    the D-04 audit passes.

    Deadlock-safe (Pitfall 1, project memory ``server-startup-alter-lock-deadlock``):
    ``CONCURRENTLY`` takes only ``SHARE UPDATE EXCLUSIVE`` (never the ACCESS EXCLUSIVE that
    a plain ``CREATE INDEX`` on the populated ~520K-row heap would take at boot), a bounded
    session ``lock_timeout`` caps the brief locks it still needs, and a SESSION advisory lock
    serializes racing builders. ``IF NOT EXISTS`` makes a re-run a no-op. Requires
    ``synthetic.arm_id_key`` / ``synthetic.ascii_fold`` to already exist (call AFTER
    :func:`ensure_arm_id_key_schema` has COMMITTED). The relation / column / index names are
    STATIC — no injection surface.

    FIX 3 — leftover-index guard: ``CREATE INDEX CONCURRENTLY IF NOT EXISTS`` can
    FALSELY succeed on a leftover same-named index that is INVALID (an interrupted
    prior build) or stale-shaped — ``IF NOT EXISTS`` skips it and a naive builder
    returns True on a broken/wrong index. After EACH build this validates
    ``indisvalid``/``indisready`` + the definition via the catalog and REPAIRS
    (drop-concurrently + rebuild) a leftover, or FAILS LOUD
    (:class:`ArmIdIndexBuildError`) if a valid index still cannot be produced.

    Returns True once BOTH indexes are confirmed present, valid, ready, and correctly
    shaped.
    """
    conn = psycopg.connect(conn_str or DATABASE_URL, autocommit=True)
    try:
        # Bounded session lock_timeout: a brief ACCESS SHARE UPDATE wait cannot hang the
        # provisioning path (autocommit -> session-scoped, reverted on close).
        conn.execute("SET lock_timeout = '3s'")
        # Serialize concurrent builders (a SESSION advisory lock — a transaction/xact lock
        # is impossible here since CONCURRENTLY forbids a transaction). Released in finally.
        conn.execute("SELECT pg_advisory_lock(hashtext(%s))", (_ARM_ID_KEY_INDEX_LOCK,))
        try:
            for name, on_clause, fragments in _ARM_ID_KEY_INDEX_SPECS:
                _build_and_validate_arm_id_index(conn, name, on_clause, fragments)
        finally:
            conn.execute(
                "SELECT pg_advisory_unlock(hashtext(%s))", (_ARM_ID_KEY_INDEX_LOCK,)
            )
    finally:
        conn.close()
    return True


def ensure_arm_resolver_schema(conn: psycopg.Connection) -> bool:
    """Apply the idempotent ``sql/010_arm_resolver.sql`` migration — the resolver
    substrate (``synthetic.drift_batches.storage_mode`` provenance column + the two per-kind
    resolved views ``synthetic.arm_resolved_resources`` / ``synthetic.arm_resolved_resource_groups``
    + the ``(target_kind, id_lower)`` overlay resolution index).

    Verbatim twin of :func:`ensure_arm_overlay_schema`, swapping ``009_arm_overlay.sql`` for
    ``010_arm_resolver.sql`` and the advisory-lock key for the DISTINCT ``synthetic.arm_resolver:010``.
    Applied UNCONDITIONALLY by ``init-db`` (and available for BYO-Postgres completeness) so a
    database provisioned before the resolver existed gains the resolver substrate automatically
    on the next ``init-db``. NOT wired into ``generate`` provisioning — ``generate`` writes no
    overlay/resolver data (the overlay is populated only by the write plane), and the Rust
    server self-provisions it at boot via ``ensure_arm_resolver_schema``. ``sql/010`` is fully
    idempotent (``CREATE OR REPLACE VIEW`` + ``CREATE INDEX IF NOT EXISTS`` + a guarded
    ``DO`` block for the column), and it touches NOTHING on the populated ``synthetic.resources``
    table, so applying it here is safe to repeat. The statement text is a STATIC project file,
    never user/profile input — no injection surface. Returns True if applied, False if the
    file was not found (installed package with no bundled ``sql/`` — those deployments apply
    the schema via docker initdb).
    """
    sql_path = resource_path("sql", "010_arm_resolver.sql")
    if not sql_path.is_file():
        return False
    # Transaction-scoped preamble RELOCATED from sql/010 so the .sql file stays honest under
    # Docker initdb autocommit. ``conn.transaction()`` GUARANTEES an explicit transaction (a
    # savepoint when init-db's outer writer tx is already open), so the bounded ``lock_timeout``
    # and the serializing advisory lock actually take effect and cover the DDL. The key is
    # DISTINCT from the 009 key so a 009 apply and a 010 apply do not needlessly serialize.
    with conn.transaction():
        conn.execute("SET LOCAL lock_timeout = '3s'")
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('synthetic.arm_resolver:010'))")
        conn.execute(sql_path.read_text(encoding="utf-8"))
    return True


def schema_is_empty(conn: psycopg.Connection) -> bool:
    """True when the synthetic schema holds no tenant/subscription/RG rows.

    Used by the force-confirmation guard to allow a bare ``generate`` against a fresh schema.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT (SELECT count(*) FROM synthetic.tenant) "
            "+ (SELECT count(*) FROM synthetic.subscriptions) "
            "+ (SELECT count(*) FROM synthetic.resource_groups)"
        )
        return int(cur.fetchone()[0]) == 0


def acquire_generate_lock(conn: psycopg.Connection, key: int) -> None:
    """Take the xact-scoped advisory lock serializing the destructive generate
    critical section. Behind the writer seam (like truncate_synthetic /
    write_tenant) so the DB-free CLI tests can stub it — cli.py must never touch a raw
    ``conn.cursor()`` directly, which would bypass those mocks.

    Superseded on the ``generate`` path by the SESSION-scoped seam below: the xact lock forced the whole check→generate→write to share one
    open transaction (holding DDL locks + an open write connection across the CPU/
    multiprocessing-fork phase). Kept for any remaining single-transaction callers.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (key,))


@contextmanager
def open_lock_connection(
    conn_str: str | None = None,
) -> Iterator[psycopg.Connection]:
    """Open a DEDICATED autocommit connection to hold a SESSION advisory lock across
    the whole check→generate→write critical section.

    ``autocommit=True`` means this connection holds NO implicit transaction — so the
    session lock spans three independent short transactions (provisioning, gate,
    write) WITHOUT an idle-in-transaction connection or DDL locks held across the
    CPU/multiprocessing-fork phase (the P2 regression the xact lock caused). Closing
    the connection on exit releases any session advisory lock it still holds.
    """
    conn = psycopg.connect(conn_str or DATABASE_URL)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


def acquire_generate_lock_session(conn: psycopg.Connection, key: int) -> None:
    """Take the SESSION-scoped advisory lock (blocking) on the dedicated idle
    connection. Session-scoped (``pg_advisory_lock``, not
    ``pg_advisory_xact_lock``) so it survives the per-phase transactions and is
    released only by :func:`release_generate_lock_session` or connection close.
    Behind the writer seam so DB-free CLI tests stub it (no raw ``conn.cursor()``
    in cli.py). The key binds as a ``%s`` literal — no string-spliced SQL."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (key,))


def release_generate_lock_session(conn: psycopg.Connection, key: int) -> None:
    """Release the session advisory lock taken by
    :func:`acquire_generate_lock_session`. Belt-and-suspenders —
    closing the autocommit lock connection also releases it — but an explicit unlock
    keeps the critical section's end unambiguous. Bound ``%s`` literal, no spliced
    SQL."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(%s)", (key,))


def estate_is_empty(conn: psycopg.Connection) -> bool:
    """True only when EVERY existing synthetic table holds zero rows.

    Stronger than :func:`schema_is_empty` (which inspects only tenant/subscriptions/
    resource_groups): a partially written or interrupted estate — rows in
    resources/cost/identity/drift but no tenant row, or any other single populated
    table — reads as NON-empty here. The demo one-shot guard (``--only-if-empty``)
    uses this so it NEVER truncates a populated OR partially populated volume
    Absent tables (a later migration than the target schema) are skipped
    via ``to_regclass``, mirroring :func:`truncate_synthetic`; the table names come
    from the STATIC ``_SYNTHETIC_TABLES`` literal, so the f-string is injection-free.
    """
    with conn.cursor() as cur:
        for t in _SYNTHETIC_TABLES:
            cur.execute("SELECT to_regclass(%s)", (t,))
            if cur.fetchone()[0] is None:
                continue
            cur.execute(f"SELECT EXISTS (SELECT 1 FROM {t})")  # STATIC name
            if cur.fetchone()[0]:
                return False
    return True


def truncate_synthetic(conn: psycopg.Connection) -> None:
    """TRUNCATE every EXISTING synthetic table (RESTART IDENTITY CASCADE), FK-safe.

    Only tables that currently exist are truncated: a synthetic table may be
    introduced by a later migration than the one applied to the target schema
    (e.g. ``synthetic.cost_records`` arrives with sql/004 while the
    writer that lists it ships separately). ``to_regclass`` returns NULL for an
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

    ``profile_name`` (the generation-profile IDENTITY) is written after
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
                    tenant.profile_name,  # None → SQL NULL (back-compat)
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


# COPY-tuning lever: above this many resource rows, dropping the
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
    """Binary-COPY all resources (load LAST; no FK but FK-order anyway).

    Column contract (sql/001):
    ``id, subscription_id, resource_group_name, name, type, location, tags, sku,
    kind, properties, provisioning_state, managed_by`` →
    ``text, uuid, text, text, text, text, jsonb, jsonb, text, jsonb, text, text``.
    JSONB columns are wrapped in ``Jsonb``; ``sku`` may be NULL.

    For large loads (≥ ``_RESOURCES_INDEX_DROP_THRESHOLD`` rows —
    the 500K-scale path) the secondary indexes are dropped around the bulk COPY and
    rebuilt once, the standard Postgres bulk-load accelerator. Small loads keep the
    plain path (byte-identical to the pre-tuning writer).
    """
    n_res = sum(len(rg.resources) for rg in tenant.resource_groups)
    if n_res >= _RESOURCES_INDEX_DROP_THRESHOLD:
        with _dropped_secondary_indexes(conn, "synthetic.resources"):  # SYNRES-ALLOW[generation/writer]: the generator drops/rebuilds the base table's bulk-load indexes
            _copy_resources_rows(conn, tenant)
    else:
        _copy_resources_rows(conn, tenant)


def _copy_resources_rows(conn: psycopg.Connection, tenant: "Tenant") -> None:
    """The resources binary-COPY itself — STATIC column literal + ``set_types``
    binary encoding (the SQL-injection bar). Split out from
    :func:`copy_resources` so the optional index drop/recreate can wrap it without
    touching the column contract.
    """
    cols = (
        "id, subscription_id, resource_group_name, name, type, location, "
        "tags, sku, kind, properties, provisioning_state, managed_by"
    )
    with conn.cursor() as cur:
        with cur.copy(
            f"COPY synthetic.resources ({cols}) FROM STDIN (FORMAT BINARY)"  # SYNRES-ALLOW[generation/writer]: the generator WRITES the immutable baseline via bulk COPY
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

    ``rows`` is an iterable of dicts with those five keys. **Scope note:** the
    dependency-row SEMANTICS (which resources actually depend cross-subscription,
    hub-spoke / centralized-logging topologies) are handled by a later stage — this
    change only closes the COPY PATH, so the default is an empty list (a no-op that
    still exercises the binary-COPY surface end-to-end). A later stage populates real
    rows by passing them here; the column/type contract is fixed now.

    ``None``/empty is a clean no-op (the v1 default). Column literals are STATIC
    (never profile-derived); values pass through parameterized binary encoding —
    no string-concatenated SQL.
    """
    rows = list(rows or [])
    if not rows:
        return  # v1 default: COPY path exists; no rows to write (a later stage fills it)
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
    dict wrapped here via ``Jsonb``. Each violation's resource_id
    references an already-written ``synthetic.resources`` row (load after
    :func:`copy_resources`), though no DB FK is declared.

    ``None``/empty is a clean no-op (matches :func:`copy_dependencies`). The
    column literal is STATIC (never profile-derived); every value passes through
    parameterized binary encoding — no string-concatenated SQL (project memory
    "mock-server SQL injection bar").
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
                        Jsonb(v["detail"]),  # JSONB must be wrapped
                    )
                )


def copy_cost_records(
    conn: psycopg.Connection, rows: "Iterable[dict] | None" = None
) -> None:
    """Binary-COPY per-resource cost rows (load after resources; FK → resources).

    Verbatim sibling of :func:`copy_violations` for the narrow ``cost_records``
    fact table. Column contract (sql/004_cost.sql), with NO
    JSONB column (so no ``Jsonb`` wrap)::

        (resource_id, subscription_id, billing_period, cost_amount, currency)
        → text, uuid, date, float8, text

    ``rows`` is an iterable of dicts with those five keys (the shape
    :func:`tenantless.generator.cost.inject_cost` emits). ``billing_period`` is a
    ``datetime.date``; ``cost_amount`` a float; ``currency`` is ``"USD"``.
    Each row's ``resource_id`` references an already-written ``synthetic.resources``
    row — the ``fk_cost_resource`` FK (sql/004) rejects any dangling reference at
    COPY time (the 0-dangling gate), so this MUST run after
    :func:`copy_resources`.

    ``None``/empty is a clean no-op (matches :func:`copy_violations`). The column
    literal is STATIC (never profile-derived); every value passes through
    parameterized binary encoding — no string-concatenated SQL (project memory
    "mock-server SQL injection bar").
    """
    # iterate the source rows DIRECTLY — GenerationResult.cost_records
    # is a frozen tuple, and materializing it into a fresh list here re-copied (at
    # scale) 6-15M cost dicts for a single pass. `rows or ()` normalizes None to an
    # empty no-op; the tuple/list/iterable is iterated once into COPY unchanged, so
    # the write_row payload order stays byte-identical (fingerprint guarantee).
    rows = rows or ()
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
    directory, with NO JSONB column (so no ``Jsonb`` wrap)::

        (oid, principal_type, display_name, app_id)
        → uuid, text, text, uuid

    ``rows`` is an iterable of dicts with those four keys (the shape
    :func:`tenantless.generator.identity.generate_principals` emits).
    ``display_name`` is ``None`` (ARM-opaque); ``app_id`` is a UUID for
    ServicePrincipals and ``None`` otherwise — both nullable columns. Principals
    load BEFORE :func:`copy_role_assignments` so the ``fk_ra_principal`` FK
    (sql/005) holds at COPY time (the 0-dangling gate).

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
    fact table, no JSONB column::

        (assignment_id, subscription_id, principal_oid, principal_type,
         role_definition_id, scope)
        → uuid, uuid, uuid, text, text, text

    ``rows`` is an iterable of dicts with those six keys (the shape
    :func:`tenantless.generator.identity.assign_roles` emits). Each row's
    ``principal_oid`` references an already-written ``synthetic.principals`` row —
    the ``fk_ra_principal`` FK (sql/005) rejects any dangling reference at COPY
    time (the 0-dangling gate) — and its ``scope`` references a real
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

    All bulk tables load via psycopg3 binary COPY in FK order; the
    dependencies COPY runs after resources, violations COPY after
    that, cost_records COPY after resources (its ``resource_id`` FK), then the
    identity tables LAST: principals (after subscriptions) and role_assignments
    (after principals AND resources — the three-way FK chain: ``principal_oid``
    → principals, ``scope`` → a real sub/RG/resource id). Truncation is the caller's
    responsibility (guarded by the CLI). ``dependencies`` / ``violations`` /
    ``cost_records`` / ``principals`` / ``role_assignments`` default to empty sets —
    a no-identity profile passes no identity rows (a clean no-op).
    """
    copy_tenant(conn, tenant)
    copy_subscriptions(conn, tenant)
    copy_resource_groups(conn, tenant)
    copy_resources(conn, tenant)
    copy_dependencies(conn, dependencies)
    copy_violations(conn, violations)  # after resources
    copy_cost_records(conn, cost_records)  # FK-after-resources
    copy_principals(conn, principals)  # NEW — after subscriptions
    copy_role_assignments(  # NEW — after principals AND resources
        conn, role_assignments
    )
