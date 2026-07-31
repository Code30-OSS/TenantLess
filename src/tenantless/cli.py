import click

# k-anonymity privacy floor for `--min-bucket-size` (single source of truth in
# privacy.py, shared with the build_profile guard). Imported at module scope
# because the IntRange minimum is bound at decorator-evaluation (import) time.
from tenantless.analyzer.privacy import MIN_BUCKET_FLOOR

# --------------------------------------------------------------------------- #
# apply-drift read-modify-write helpers (Plan 11-05, DRIFT-01/03/04).
#
# Module-level (importable by tests) so the SQL-builder + field→column map can be
# pinned for injection safety without a DB (T-11-13, project SQL bar). Every
# user value binds as a parameter; the only identifiers spliced into SQL are the
# STATIC read-column list and the CLOSED-MATCH update-column allowlist.
# --------------------------------------------------------------------------- #

# Columns read for the scoped state read (STATIC; never user input). Order is the
# unpack contract in apply_drift.
_READ_COLUMNS = (
    "id",
    "subscription_id",
    "type",
    "tags",
    "sku",
    "kind",
    "properties",
    "drift_deleted_at",
    "resource_group_name",
    "location",
    "name",
    "provisioning_state",
    "managed_by",
)

# The ONLY columns an UPDATE may target — a closed allowlist (the served JSONB
# columns the drift engine mutates + the soft-delete visibility column).
_UPDATE_COLUMN_ALLOWLIST = frozenset(
    {"tags", "sku", "kind", "properties", "drift_deleted_at"}
)

# Of the allowlist, the JSONB columns (wrapped in psycopg ``Jsonb`` on write).
_JSONB_COLUMNS = frozenset({"tags", "sku", "properties"})

# Fixed application-wide advisory-lock key serializing ALL drift workflow
# mutations (apply-drift / revert-drift). Both take pg_advisory_xact_lock on this
# key at the start of their mutation transaction so two concurrent commands cannot
# read the same parent state and clobber each other with stale read-modify-write
# snapshots (P1, 11-10). A constant int that fits a signed BIGINT; the xact-scoped
# lock auto-releases at transaction end.
DRIFT_LOCK_KEY = 0x0D_711F_7000  # "drift" lock, stable across the codebase

# Sibling advisory-lock key serializing the destructive GENERATE critical section
# (Wave2 #1). `generate` takes pg_advisory_xact_lock on this key at the start of its
# write transaction so the emptiness check and the truncate/write are one atomic
# section: a populated estate can never be truncated by a check-then-write race, and
# two generators on a fresh volume can't race the bare-CREATE ensure_* DDL. Distinct
# from DRIFT_LOCK_KEY so generate and drift never contend. xact-scoped: auto-released.
GENERATE_LOCK_KEY = 0x0E_711F_7000  # "generate" lock, stable across the codebase


def _split_csv(raw: str | None) -> list[str] | None:
    """Parse a comma-separated option into a clean list (or None)."""
    if raw is None:
        return None
    items = [s.strip() for s in raw.split(",") if s.strip()]
    return items or None


def _build_scoped_read_sql(
    subscription_id, resource_types: list[str] | None
) -> tuple[str, list]:
    """Build the $N-bound scoped state read (RESEARCH §"$N-bound scoped read").

    Returns ``(sql, params)`` where every user-supplied value (subscription,
    resource-type list) is a BOUND parameter — never spliced. The subscription is
    pre-parsed to a UUID by the caller; the type list binds as a ``text[]`` array.
    The placeholder count equals ``len(params)`` so a parametrized test can prove
    no user value leaks into the SQL text (Pitfall 3: ORDER BY id).
    """
    cols = ", ".join(_READ_COLUMNS)  # STATIC identifiers
    sql = (
        f"SELECT {cols} FROM synthetic.resources "
        "WHERE drift_deleted_at IS NULL "
        "AND (%s::uuid IS NULL OR subscription_id = %s::uuid) "
        "AND (%s::text[] IS NULL OR type = ANY(%s::text[])) "
        "ORDER BY id"
    )
    sub_param = str(subscription_id) if subscription_id is not None else None
    types_param = list(resource_types) if resource_types else None
    params = [sub_param, sub_param, types_param, types_param]
    return sql, params


def _field_to_column(field_path: str) -> str:
    """Map a drift delta ``field_path`` to its served column (CLOSED match).

    ``properties.*`` / ``properties.foo[]`` → ``properties``; ``tags.*`` →
    ``tags``; ``sku`` → ``sku``; ``kind`` → ``kind``; ``drift_deleted_at`` →
    itself. ANY other value raises ``ValueError`` — the column spliced into the
    UPDATE statement can therefore only ever be a member of the allowlist
    (T-11-13: no f-string splice of user values).
    """
    if field_path == "drift_deleted_at":
        return "drift_deleted_at"
    head = field_path.split(".", 1)[0].split("[", 1)[0]
    if head in ("properties", "sku", "tags", "kind"):
        return head
    raise ValueError(f"unmapped drift field_path: {field_path!r}")


def _resource_column_value(robj, col: str):
    """The current full value of ``col`` on the mutated in-memory resource."""
    if col == "tags":
        return robj.tags
    if col == "sku":
        return robj.sku
    if col == "kind":
        return robj.kind
    if col == "properties":
        return robj.properties
    raise ValueError(f"no column value for {col!r}")


class _RGView:
    """Lightweight resource-group view for ``drift.compute_lifecycle`` (Plan 11-06).

    ``compute_lifecycle`` iterates ``rgs`` reading ``.name`` / ``.subscription_id``
    / ``.location`` and appends minted appear-leaves to ``.resources`` — a
    pipeline ``ResourceGroup`` is overkill for the read-modify-write seam, so the
    apply path reconstructs this minimal view from the scoped read rows.
    """

    __slots__ = ("name", "subscription_id", "location", "resources")

    def __init__(self, name, subscription_id, location):
        self.name = name
        self.subscription_id = subscription_id
        self.location = location
        self.resources: list = []


def _group_into_rgs(res_objs) -> list:
    """Group the scoped-read resources into ``_RGView``s (sorted, deterministic)."""
    groups: dict = {}
    for r in res_objs:
        key = (str(r.subscription_id), r.resource_group_name)
        rg = groups.get(key)
        if rg is None:
            rg = _RGView(r.resource_group_name, r.subscription_id, r.location)
            groups[key] = rg
        rg.resources.append(r)
    return [groups[k] for k in sorted(groups)]


def _load_disappear_refs(conn):
    """Build ``drift.DisappearRefs`` from $N-bound anti-join source SELECTs (D-10).

    Reads the four reference sets a resource must be ABSENT from to be
    disappear-eligible (role-assignment scopes, dependency source/target ids,
    violation resource ids, ``managed_by`` ids). Every statement is STATIC SQL
    (no user/profile input spliced); each table is guarded by ``to_regclass`` so
    a volume predating Phase 9/10 (no identity/cost tables) degrades to an empty
    set rather than erroring (the resources table always exists here).
    """
    from tenantless.generator import drift

    role_scopes: set = set()
    dependency_ids: set = set()
    violation_ids: set = set()
    managed_by_ids: set = set()
    with conn.cursor() as cur:

        def _exists(tbl: str) -> bool:
            cur.execute("SELECT to_regclass(%s)", (tbl,))
            return cur.fetchone()[0] is not None

        if _exists("synthetic.role_assignments"):
            cur.execute(
                "SELECT scope FROM synthetic.role_assignments WHERE scope IS NOT NULL"
            )
            role_scopes = {r[0] for r in cur.fetchall()}
        if _exists("synthetic.dependencies"):
            cur.execute(
                "SELECT source_resource_id, target_resource_id "
                "FROM synthetic.dependencies"
            )
            for s, t in cur.fetchall():
                if s:
                    dependency_ids.add(s)
                if t:
                    dependency_ids.add(t)
        if _exists("synthetic.violations"):
            cur.execute(
                "SELECT resource_id FROM synthetic.violations "
                "WHERE resource_id IS NOT NULL"
            )
            violation_ids = {r[0] for r in cur.fetchall()}
        cur.execute(
            "SELECT managed_by FROM synthetic.resources WHERE managed_by IS NOT NULL"
        )
        managed_by_ids = {r[0] for r in cur.fetchall()}
    return drift.DisappearRefs(
        role_scopes=frozenset(role_scopes),
        dependency_ids=frozenset(dependency_ids),
        violation_ids=frozenset(violation_ids),
        managed_by_ids=frozenset(managed_by_ids),
    )


def _revert_nested(col_value, field_path: str, before, after):
    """Restore one nested field inside a JSONB container column to ``before``.

    ``field_path`` is ``properties.<key>`` / ``properties.<key>[]`` /
    ``tags.<key>``; ``before``/``after`` are the per-FIELD engine delta values the
    apply seam recorded (Plan 11-05: deltas are per-field, NOT full-column). A
    ``None`` ``before`` means the key was ABSENT pre-drift (the chaos/temporal
    catalogue never stores a present-None) so revert removes it; the ``[]`` suffix
    marks an append so revert removes the appended element (``after``). Returns the
    rebuilt container dict (caller writes it back as the full column — Pitfall 4,
    served-response byte-for-byte restore)."""
    container = dict(col_value or {})
    _head, _, rest = field_path.partition(".")
    if rest.endswith("[]"):
        key = rest[:-2]
        lst = [x for x in (container.get(key) or []) if x != after]
        if lst:
            container[key] = lst
        else:
            container.pop(key, None)
        return container
    if before is None:
        container.pop(rest, None)
    else:
        container[rest] = before
    return container


def _drift_clamp_notes(
    res_objs, drift_type, codes, resource_types, intensity
) -> list[str]:
    """Per-code D-14 clamp notes for the run (computed before mutation).

    Mirrors ``compute_drift``'s code/eligible selection but consumes NO RNG
    (``_eligible_population`` / ``planned_count`` are pure), so calling it before
    ``compute_drift`` leaves the seeded draw sequence unchanged. Surfaced via
    ``click.echo`` so a clamp is never silent (D-14: clamp-and-report)."""
    from tenantless.generator import drift

    code_filter = set(codes) if codes is not None else None
    selected = sorted(
        code
        for code, spec in drift.DRIFT_REGISTRY.items()
        if spec.drift_type == drift_type
        and (code_filter is None or code in code_filter)
    )
    allowed = set(resource_types) if resource_types is not None else None
    notes: list[str] = []
    for code in selected:
        spec = drift.DRIFT_REGISTRY[code]
        eligible = drift._eligible_population(res_objs, code, spec)
        if allowed is not None:
            eligible = [r for r in eligible if r.type in allowed]
        _count, note = drift.planned_count(intensity, eligible)
        if note:
            notes.append(f"{code}: {note}")
    return notes


@click.group()
@click.version_option(
    package_name="tenantless",
    prog_name="tenantless",
    message="%(prog)s %(version)s",
)
def main():
    """Tenantless: Azure Tenant Simulator"""
    pass


@main.command()
@click.option(
    "--source",
    required=True,
    help=(
        "Data source: duckdb:<path> to a DuckDB scan file (or a bare path), or "
        "azure:[<subId,...>] to scan a live tenant via Azure Resource Graph "
        "(empty filter = the enumerated default scope; needs the 'azure' extra)."
    ),
)
@click.option(
    "--out",
    default="profiles/derived.json",
    show_default=True,
    type=click.Path(dir_okay=False),
    help="Output path for the statistical profile JSON.",
)
@click.option(
    "--min-bucket-size",
    default=MIN_BUCKET_FLOOR,
    show_default=True,
    type=click.IntRange(min=MIN_BUCKET_FLOOR),
    help=(
        "Drop statistical buckets observed fewer than this many times. Values "
        f"below the k-anonymity floor ({MIN_BUCKET_FLOOR}) are rejected."
    ),
)
@click.option(
    "--denylist",
    default=None,
    type=click.Path(exists=False, dir_okay=False),
    help="Optional path to a JSON denylist of real identifiers (gitignored).",
)
@click.option(
    "--k",
    default=None,
    type=int,
    help="Number of subscription archetypes for k-means (default 5).",
)
@click.option(
    "--allow-no-denylist",
    "allow_no_denylist",
    is_flag=True,
    default=False,
    help=(
        "Permit profiling a sample/test source with no denylist; NEVER use for "
        "real-derived scans (the denylist is the data-boundary guard)."
    ),
)
@click.option(
    "--non-interactive",
    "non_interactive",
    is_flag=True,
    default=False,
    help=(
        "Suppress the stdout review dump. The "
        "<profile>_review.txt companion is ALWAYS written regardless; this only "
        "silences the interactive print (also implied when stdin is not a TTY)."
    ),
)
def analyze(source, out, min_bucket_size, denylist, k, allow_no_denylist, non_interactive):
    """Extract a statistical profile from a DuckDB scan."""
    import sys

    from tenantless.analyzer.profile import build_profile

    profile = build_profile(
        source=source,
        out=out,
        min_bucket_size=min_bucket_size,
        denylist=denylist,
        k=k,
        allow_no_denylist=allow_no_denylist,
    )
    stats = profile["source_stats"]
    n_types = len(profile["resource_type_distributions"])
    click.echo(
        f"Wrote {out}: {stats['total_subscriptions']} subscriptions, "
        f"{stats['total_resource_groups']} resource groups, "
        f"{stats['total_resources']} resources, {n_types} resource types."
    )
    # ANLZ-10 (D-04): build_profile already wrote <out>_review.txt (report-only,
    # never blocks). In interactive mode -- and only when --non-interactive is
    # not set and stdin is a TTY -- also echo the grouped review to stdout.
    if not non_interactive and sys.stdin.isatty():
        from tenantless.analyzer import review

        click.echo(review.render(profile))


@main.command()
@click.option(
    "--profile",
    required=True,
    type=str,
    help=(
        "Profile to invert: a bundled name (enterprise, small) OR a path to a "
        "statistical profile JSON (an existing file path wins)."
    ),
)
@click.option(
    "--resources",
    default=None,
    type=int,
    help="Primary scale knob: target resource count (defaults from source_stats).",
)
@click.option(
    "--subscriptions",
    default=None,
    type=int,
    help="Target subscription count (defaults from source_stats).",
)
@click.option(
    "--seed",
    default=42,
    show_default=True,
    type=int,
    help="Single seed driving all sampling + Faker (reproducible by default).",
)
@click.option(
    "--force",
    "--yes",
    "force",
    is_flag=True,
    default=False,
    help="Truncate the synthetic schema without prompting (required when no TTY).",
)
@click.option(
    "--only-if-empty",
    "only_if_empty",
    is_flag=True,
    default=False,
    help=(
        "Generate ONLY when the ENTIRE synthetic estate is empty; otherwise "
        "preserve the existing data and exit 0 without truncating. The whole-estate "
        "check and the write run under a Postgres advisory lock, so a populated (or "
        "partially populated) estate is never clobbered by a concurrent writer. "
        "Intended for the compose one-shot demo seeder — non-destructive, unlike "
        "--force which always truncates."
    ),
)
@click.option(
    "--violations/--no-violations",
    "inject_violations",
    default=True,
    show_default=True,
    help="Inject governance violations.",
)
@click.option(
    "--cross-sub/--no-cross-sub",
    "inject_cross_sub",
    default=True,
    show_default=True,
    help="Generate cross-subscription dependencies.",
)
@click.option(
    "--cost-granularity",
    "cost_granularity",
    type=click.Choice(["monthly", "daily"]),
    default="monthly",
    show_default=True,
    help=(
        "Cost fact-table grain. 'monthly' generates 12 first-of-month "
        "periods. 'daily' uses a SHORT window (the current month only, ~30 "
        "rows/resource) to avoid a full-year row blow-up."
    ),
)
@click.option(
    "--cost-as-of",
    "cost_as_of",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    metavar="YYYY-MM-DD",
    help=(
        "Calendar date the cost billing periods are anchored to. All "
        "periods are derived EXCLUSIVELY from this date, so a fixed "
        "(profile, seed, --cost-as-of) is byte-reproducible across calendar days. "
        "Defaults to today() for realism (the live Cost API resolves MonthToDate "
        "against 'now'); pin it for reproducible runs."
    ),
)
@click.option(
    "--identity/--no-identity",
    "inject_identity",
    default=True,
    show_default=True,
    help="Generate synthetic principals + role assignments.",
)
@click.option(
    "--over-privilege-rate",
    "over_privilege_rate",
    type=click.FloatRange(0.0, 1.0),
    default=0.05,
    show_default=True,
    help=(
        "Configurable rate of injected over-privilege role assignments: "
        "Owner-at-subscription / ServicePrincipal-granted-Owner grants — the identity "
        "analogue of --violations. 0.0 injects ZERO over-privilege rows (a clean "
        "tenant); the injected count is reported in the run summary."
    ),
)
@click.option(
    "--jobs",
    "jobs",
    type=click.IntRange(0, None),
    default=1,
    show_default=True,
    help=(
        "Worker processes for per-subscription generation. 1 (the "
        "default) uses the single-process reference path; 0 means all cores "
        "(os.cpu_count()). Any value is clamped to [1, os.cpu_count()] (so a huge "
        "--jobs never spawns an unbounded pool) and yields BYTE-IDENTICAL output "
        "for a fixed seed — the determinism gate proves --jobs 1 == --jobs N."
    ),
)
def generate(
    profile,
    resources,
    subscriptions,
    seed,
    force,
    only_if_empty,
    inject_violations,
    inject_cross_sub,
    cost_granularity,
    cost_as_of,
    inject_identity,
    over_privilege_rate,
    jobs,
):
    """Generate a synthetic Azure tenant from a statistical profile.

    This REPLACES the current synthetic estate: the schema is truncated and a
    freshly generated estate is written. Against a non-empty estate it prompts for
    confirmation, or requires ``--force`` / ``--yes`` when stdin is not a TTY.
    Re-running is not a no-op.

    Both post-passes default on: a bare ``generate`` injects violations AND
    cross-subscription dependencies. ``--no-violations`` / ``--no-cross-sub`` skip
    one pass independently.
    """
    import os
    import sys
    import time

    from tenantless.generator import archetypes, writer
    from tenantless.generator.pipeline import generate_tenant
    from tenantless.generator.profile_input import (
        load_profile,
        resolve_profile,
        resolve_targets,
    )

    # Security V5 (DoS-self): resolve --jobs to a concrete worker count clamped to
    # the core count BEFORE handing it to the pipeline. IntRange(0, None) already
    # rejects negatives at CLI validation, so 0 is the SOLE all-cores sentinel —
    # there is no negative branch. 1 (default) preserves the single-process
    # reference path; the clamp means a huge --jobs never spawns an unbounded pool.
    cpu = os.cpu_count() or 1
    effective_jobs = cpu if jobs == 0 else min(jobs, cpu)

    # PLAT-06 / D-18: plain progress lines to STDERR (no rich/tqdm dependency);
    # the structured run summary goes to STDOUT below. D-19: NO drift line.
    started = time.perf_counter()
    click.echo("fitting distributions...", err=True)
    profile_dict = load_profile(resolve_profile(profile))
    n_subs, n_resources = resolve_targets(profile_dict, resources, subscriptions)

    # D-14: derive the generation-profile IDENTITY from the raw --profile value,
    # mirroring resolve_profile's resolution order (path-if-exists → bundled-name):
    # an existing file path contributes its stem (e.g. `enterprise-eu.json` →
    # `enterprise-eu`); a bundled name IS the identity (e.g. `enterprise`, `small`).
    from pathlib import Path as _Path

    derived_profile_name = (
        _Path(profile).stem if _Path(profile).is_file() else profile
    )

    # P1 fix: resolve the cost anchor to a single calendar date ONCE (default
    # today()), so every billing period derives from it — never a per-call today().
    import datetime as _dt

    as_of = cost_as_of.date() if cost_as_of is not None else _dt.date.today()

    click.echo("generating tenant...", err=True)
    result = generate_tenant(
        profile_dict,
        seed=seed,
        n_subs=n_subs,
        n_resources=n_resources,
        inject_violations=inject_violations,
        inject_cross_sub=inject_cross_sub,
        cost_granularity=cost_granularity,
        cost_as_of=as_of,
        inject_identity=inject_identity,
        over_privilege_rate=over_privilege_rate,
        jobs=effective_jobs,
        profile_name=derived_profile_name,
    )
    click.echo("computing tag entropy...", err=True)
    tenant = result.tenant

    skipped = False
    with writer.open_writer() as conn:
        # Wave2 #1: serialize the whole check→truncate→write on a dedicated advisory
        # lock, taken FIRST so it spans the emptiness check AND the destructive write
        # as one atomic critical section. No concurrent generator (or the compose
        # one-shot) can populate the estate between the check and the write, and two
        # generators on a fresh volume can't race the bare-CREATE ensure_* DDL below.
        # xact-scoped: released when open_writer commits/rolls back. Behind the writer
        # seam so DB-free CLI tests stub it (never a raw conn.cursor() here).
        writer.acquire_generate_lock(conn, GENERATE_LOCK_KEY)
        # 260709-blf (Docker-optional / BYO-Postgres): ensure the BASE synthetic
        # schema (sql/001 tenant/subs/RGs/resources, sql/002 dependencies/violations,
        # sql/003 integrity+indexes) exists BEFORE any other ensure_* / truncate /
        # write. On a bare non-Docker PG16 the base tables were previously applied
        # ONLY by the docker initdb mount, so generate failed on the missing schema;
        # this self-provisions 001->002->003 first. A no-op on an already-provisioned
        # Docker volume (function-level to_regclass guard, since sql/001,002 are bare
        # CREATE, not IF NOT EXISTS). Called FIRST so the later cost/identity/
        # web_metadata migrations and the write have their base tables present.
        writer.ensure_base_schema(conn)
        # P1 fix: ensure the cost fact table exists before truncate/write — an
        # existing dev volume initialised before Phase 9 lacks synthetic.cost_records
        # (sql/004 is initdb-only). Idempotent; no-op on volumes that already have it.
        if result.cost_records:
            writer.ensure_cost_schema(conn)
        # P1 fix (Plan 10-01): ensure the identity tables exist before truncate/write
        # — an existing dev volume initialised before Phase 10 lacks
        # synthetic.principals / role_assignments (sql/005 is initdb-only).
        # Idempotent; no-op on volumes that already have them. Called
        # UNCONDITIONALLY (even with --no-identity / zero identity rows) so the empty
        # tables exist and the mock-server's roleAssignments SELECT serves [] rather
        # than 500ing on a missing relation.
        writer.ensure_identity_schema(conn)
        # P1 fix (Plan 14-05, D-14): ensure the profile_name column exists before
        # truncate/write — an existing dev volume initialised before Phase 14 lacks
        # synthetic.tenant.profile_name (sql/007 is initdb-only). Idempotent; no-op
        # on volumes that already have it. Called UNCONDITIONALLY so copy_tenant's
        # profile_name write never fails on a missing column on a pre-Phase-14 volume.
        writer.ensure_web_metadata_schema(conn)
        # Wave2 #1: --only-if-empty is the NON-destructive demo/one-shot guard. Under
        # the advisory lock it inspects the ENTIRE estate (every synthetic table, not
        # just resources); if ANY table holds rows — a full estate OR a partially
        # written / interrupted one — it PRESERVES the data and skips generation.
        # Because the check runs inside the locked write transaction, a populated
        # estate can never be truncated by a check-then-write race.
        if only_if_empty and not writer.estate_is_empty(conn):
            skipped = True
        else:
            # D-08: truncation is destructive — guard it.
            if not force and not writer.schema_is_empty(conn):
                if sys.stdin.isatty():
                    click.confirm(
                        "This will TRUNCATE the synthetic schema. Continue?",
                        abort=True,
                    )
                else:
                    raise click.UsageError(
                        "Refusing to truncate non-empty synthetic schema without "
                        "--force/--yes."
                    )
            writer.truncate_synthetic(conn)
            writer.write_tenant(
                conn,
                tenant,
                dependencies=result.dependencies,
                violations=result.violations,
                cost_records=result.cost_records,
                principals=result.principals,
                role_assignments=result.role_assignments,
            )

    if skipped:
        # The estate was already populated; nothing was truncated or written. Emit a
        # clear line (never the misleading "Generated tenant …" summary) and exit 0 so
        # the compose mock-server proceeds on the preserved data.
        click.echo(
            "[generate] estate already populated — preserving existing data; "
            "skipped generation (--only-if-empty).",
            err=True,
        )
        return

    n_res = sum(len(rg.resources) for rg in tenant.resource_groups)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    # PLAT-06 / D-18: human-readable structured run summary on STDOUT (counts,
    # seed, elapsed, tenant_id). NO drift line (D-19) — drift lands in Phase 11.
    click.echo(
        f"Generated tenant {tenant.tenant_id}: "
        f"{len(tenant.subscriptions)} subscriptions, "
        f"{len(tenant.resource_groups)} resource groups, "
        f"{n_res} resources, "
        f"{len(result.violations)} violations, "
        f"{len(result.dependencies)} dependencies, "
        f"{len(result.principals)} principals, "
        f"{len(result.role_assignments)} role assignments "
        f"({result.over_privilege_count} over-privilege) "
        f"(seed={seed}, target_resources={n_resources}, "
        f"jobs={effective_jobs}, elapsed={elapsed_ms:.0f}ms)."
    )
    # ARCH-03 / D-13: append the archetype→RG-count coverage line to the summary
    # (a plain STDOUT line — NO new command/API/UI surface, D-10). Reuse the
    # already-loaded profile_dict for the label map; count over the built tenant's
    # RG template types so the line reflects what was actually generated.
    _label_map = archetypes.build_label_map(
        profile_dict["resource_group_templates"]
    )
    _coverage = archetypes.archetype_coverage(
        _label_map, (rg.template_type for rg in tenant.resource_groups)
    )
    click.echo(archetypes.render_coverage_line(_coverage))
    # ARCH-03 / D-18: append the confirm-and-rename gate's outcome counts, tallied
    # by pipeline._confirm_and_rename and threaded out on GenerationResult. Plain
    # STDOUT beside the coverage line — NO new command/API/UI/DB surface (D-10).
    click.echo(archetypes.render_rg_naming_line(result.rg_naming_metrics))
    # D-03: surface every clamp note (never silent).
    for note in result.clamp_notes:
        click.echo(note)


@main.command()
@click.option(
    "--port",
    default=8080,
    show_default=True,
    type=int,
    help="TCP port to bind (mirrors the Rust clap default).",
)
@click.option(
    "--base-url",
    default="http://localhost:8080",
    show_default=True,
    help="Absolute base URL emitted in nextLinks (MOCK-08).",
)
@click.option(
    "--database-url",
    default="postgres://tenantless:tenantless_dev@localhost:5433/tenantless",
    show_default=True,
    help="Postgres connection string (must match writer.py / config.rs).",
)
@click.option(
    "--tls",
    is_flag=True,
    default=False,
    help=(
        "Also bind HTTPS on :8443 with an ephemeral in-memory self-signed cert. "
        "The plain HTTP port stays the default; this is additive."
    ),
)
@click.option(
    "--enforce-auth",
    is_flag=True,
    default=False,
    help=(
        "Validate Bearer tokens as real RS256 JWTs against the run's own JWKS. "
        "Default OFF preserves the any-Bearer scanner contract; ON rejects "
        "anything but a token minted by this server's /token endpoint."
    ),
)
@click.option(
    "--enable-control-plane",
    is_flag=True,
    default=False,
    help=(
        "Arm the /_control write surface. Default OFF keeps the read-only "
        "ARM server. Requires a non-empty --control-token (or TENANTLESS_CONTROL_TOKEN); "
        "armed without a token the server fails closed at startup."
    ),
)
@click.option(
    "--control-token",
    default=None,
    envvar="TENANTLESS_CONTROL_TOKEN",
    help=(
        "The control-plane admin SECRET presented by the browser in X-Control-Token. "
        "Prefer the TENANTLESS_CONTROL_TOKEN env var over the flag so the secret stays "
        "out of shell history / the process list; its value is never logged."
    ),
)
@click.option(
    "--control-data-dir",
    default=None,
    help=(
        "Server-owned root for control-plane artifacts (profiles/sources/snapshots). "
        "Omit to use the Rust default (./control-data)."
    ),
)
def serve(
    port,
    base_url,
    database_url,
    tls,
    enforce_auth,
    enable_control_plane,
    control_token,
    control_data_dir,
):
    """Start the ARM API mock server (delegates to the Rust binary).

    Discovers the server binary (PATH, then the repo's target/release|debug,
    then a ``cargo run`` fallback), runs a Postgres :5433 preflight, then launches
    the server in the FOREGROUND so Ctrl+C stops both and the exit code propagates.
    """
    from pathlib import Path

    from tenantless import serve as serve_mod

    serve_mod._preflight_postgres(database_url)
    repo_root = Path(__file__).resolve().parents[2]
    # Status line: never echo the full database_url (T-07-02).
    click.echo(f"Starting tenantless-server on {base_url} (port {port})...")
    serve_mod._launch_server(
        repo_root,
        port=port,
        base_url=base_url,
        database_url=database_url,
        tls=tls,
        enforce_auth=enforce_auth,
        enable_control_plane=enable_control_plane,
        control_token=control_token,
        control_data_dir=control_data_dir,
    )


@main.command("apply-drift")
@click.option(
    "--type",
    "drift_type",
    type=click.Choice(["temporal", "chaos"]),
    required=True,
    help="Drift family to apply: 'chaos' (adverse misconfig) or 'temporal'.",
)
@click.option(
    "--seed",
    default=42,
    show_default=True,
    type=int,
    help="Seed driving the mutation selection (reproducible by default).",
)
@click.option(
    "--intensity",
    type=click.FloatRange(0.0, 1e9),
    default=1.0,
    show_default=True,
    help=(
        "Per-code mutation volume: a fraction (0.0..1.0 -> round(I*eligible)) or "
        "an absolute count (>1.0). Clamped to the eligible population and reported."
    ),
)
@click.option(
    "--resource-types",
    "resource_types_raw",
    default=None,
    help="Comma-separated ARM type filter (bound as a parameter, never spliced).",
)
@click.option(
    "--codes",
    "codes_raw",
    default=None,
    help="Comma-separated DRIFT_* code allowlist (validated against the registry).",
)
@click.option(
    "--subscription",
    "subscription_raw",
    default=None,
    help="Restrict to one subscription UUID (parsed-to-UUID before bind).",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Report the planned record count + clamp note; mutate NOTHING.",
)
@click.option(
    "--database-url",
    "database_url",
    default=None,
    help="Postgres DSN (defaults to writer.DATABASE_URL / $DATABASE_URL).",
)
def apply_drift(
    drift_type,
    seed,
    intensity,
    resource_types_raw,
    codes_raw,
    subscription_raw,
    dry_run,
    database_url,
):
    """Apply seeded configuration drift to the live tenant.

    The read-modify-write seam: in ONE transaction, read the scoped live state
    ($N-bound), compute seeded mutations via ``tenantless.generator.drift``, UPDATE
    the served resource columns and INSERT the per-field ``drift_records`` plus a
    ``drift_batches`` row carrying the parent + result state fingerprints. Each run
    STACKS a new batch from the CURRENT state, so ``before`` captures the value at
    application time (the per-batch delta), not the original generated value.
    """
    import datetime as _dt
    import uuid as _uuid

    from psycopg.types.json import Jsonb

    from tenantless.generator import drift, resources as _resources, writer
    from tenantless.generator.rng import SeededContext

    db_url = database_url or writer.DATABASE_URL

    # Parse + validate user filters (V5: parse-before-bind).
    resource_types = _split_csv(resource_types_raw)
    codes = _split_csv(codes_raw)
    if codes is not None:
        # The lifecycle codes (DRIFT_DISAPPEAR / DRIFT_APPEAR) are valid --codes
        # filter values for a temporal run even though they live OUTSIDE
        # DRIFT_REGISTRY (they are whole-row operations, not field mutators).
        known_codes = set(drift.DRIFT_REGISTRY) | {
            drift.CODE_DISAPPEAR,
            drift.CODE_APPEAR,
        }
        unknown = [c for c in codes if c not in known_codes]
        if unknown:
            raise click.UsageError(f"unknown --codes: {', '.join(sorted(unknown))}")
    sub_uuid = None
    if subscription_raw is not None:
        try:
            sub_uuid = _uuid.UUID(subscription_raw)
        except ValueError as exc:
            raise click.UsageError(f"--subscription is not a valid UUID: {exc}")

    # Wall-clock anchored ONCE in the CLI/audit layer — NEVER fed into ctx or the
    # fingerprint (mirrors the cost --cost-as-of / token exp rule; A4).
    applied_at = _dt.datetime.now(_dt.timezone.utc)
    batch_id = _uuid.uuid4()

    with writer.open_writer(db_url) as conn:
        # Idempotent schema preflight, committed independently of the drift writes.
        writer.ensure_drift_schema(conn)
        conn.commit()

        # Serialize all drift workflow mutations on a fixed application-wide
        # advisory key BEFORE any read (P1, 11-10): apply-drift / revert-drift do
        # read-modify-write over JSONB columns with no other serialization, so two
        # concurrent commands would read the same parent state and clobber each
        # other with stale snapshots. The xact-scoped lock auto-releases at
        # transaction end (the open_writer commit). $N-bound.
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (DRIFT_LOCK_KEY,))

        # READ the live scoped state ($N-bound; Pitfall 3: ORDER BY id).
        sql, params = _build_scoped_read_sql(sub_uuid, resource_types)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            db_rows = cur.fetchall()

        parent_rows: list[dict] = []
        res_objs: list = []
        ddel_map: dict = {}
        for (
            rid, sub_id, rtype, tags, sku, kind, props, ddel,
            rg_name, loc, name, prov, managed,
        ) in db_rows:
            parent_rows.append(
                {
                    "id": rid,
                    "tags": tags,
                    "sku": sku,
                    "kind": kind,
                    "properties": props,
                    "drift_deleted_at": ddel,
                }
            )
            ddel_map[rid] = ddel
            res_objs.append(
                _resources.Resource(
                    id=rid,
                    subscription_id=sub_id,
                    resource_group_name=rg_name,
                    name=name,
                    type=rtype,
                    location=loc,
                    api_version="",
                    tags=dict(tags or {}),
                    sku=(dict(sku) if sku else None),
                    kind=kind,
                    properties=dict(props or {}),
                    provisioning_state=prov or "Succeeded",
                    managed_by=managed,
                )
            )
        res_by_id = {r.id: r for r in res_objs}

        # Parent fingerprint over the decoded pre-mutation state (Pitfall 4).
        parent_fp = drift.state_fingerprint(parent_rows)

        # D-14 clamp notes — computed BEFORE compute_drift (consumes no RNG).
        clamp_notes = _drift_clamp_notes(
            res_objs, drift_type, codes, resource_types, intensity
        )

        ctx = SeededContext(seed)
        deltas = drift.compute_drift(
            ctx,
            res_objs,
            drift_type,
            codes=codes,
            resource_types=resource_types,
            intensity=intensity,
        )

        # Carry-forward (Plan 11-06): a temporal run ALSO applies the
        # appear/disappear lifecycle (D-09/D-12) in the SAME transaction, so
        # revert's unhide/delete (D-13) has a real producer. The lifecycle
        # consumes the SAME seeded ctx (after the field draws) — deterministic.
        # Discretion (CONTEXT: intensity semantics are the planner's): the
        # disappear count is the D-14 clamped fraction/count of eligible leaves
        # and appear mints the SAME count (symmetric churn — vanish a few, add a
        # few). Appear has no eligible population, so it has no clamp.
        life_deltas: list[dict] = []
        minted_leaves: list = []
        disappeared_ids: set = set()
        if drift_type == "temporal":
            # Gate the lifecycle by the active --codes / --resource-types filters
            # (P2b): appear/disappear must NOT fire when the filter excludes them,
            # and appear must never mint its leaf type (_APPEAR_TYPE = storage) when
            # --resource-types excludes it.
            do_disappear = codes is None or drift.CODE_DISAPPEAR in codes
            do_appear = codes is None or drift.CODE_APPEAR in codes
            if (
                resource_types is not None
                and drift._APPEAR_TYPE not in resource_types
            ):
                do_appear = False  # never mint an excluded type

            refs = _load_disappear_refs(conn)
            rgs = _group_into_rgs(res_objs)
            all_rows = [r for rg in rgs for r in rg.resources]
            eligible = drift.disappear_eligible(all_rows, refs)
            d_count, dnote = drift.planned_count(intensity, eligible)
            if dnote and do_disappear:
                clamp_notes.append(f"{drift.CODE_DISAPPEAR}: {dnote}")
            # Symmetric churn (vanish a few, add a few), each gated independently.
            disappear_count = d_count if do_disappear else 0
            appear_count = d_count if do_appear else 0
            seen = {r.id for r in res_objs}
            life_deltas, minted_leaves = drift.compute_lifecycle(
                ctx,
                rgs,
                refs,
                disappear_count=disappear_count,
                appear_count=appear_count,
                seen_ids=seen,
            )
            disappeared_ids = {
                d["resource_id"]
                for d in life_deltas
                if d["field_path"] == "drift_deleted_at"
            }

        planned = len(deltas) + len(life_deltas)

        # --dry-run: report the full plan (count + clamp note) and persist NOTHING.
        # No drift_batches/drift_records INSERT and no resources UPDATE is issued,
        # so the transaction is read-only over the synthetic tables — provably no
        # mutation (T-11-16). The idempotent schema preflight was committed above.
        if dry_run:
            click.echo(
                f"[dry-run] planned {planned} drift records for {drift_type} drift "
                f"(seed={seed}, intensity={intensity}, batch NOT written)."
            )
            for note in clamp_notes:
                click.echo(note)
            return

        # Result fingerprint over the post-mutation ACTIVE state so it CHAINS to the
        # next apply's parent fingerprint (P2a / D-08). The parent read is the
        # scoped `WHERE drift_deleted_at IS NULL` view, so the result_fp must cover
        # the SAME active-set convention: rows disappeared in THIS batch leave the
        # served/active view and are dropped from the digest (they would otherwise
        # be present here but absent from the next parent read → unchainable). Minted
        # appear-leaves are active and are appended below.
        post_rows = [
            {
                "id": r.id,
                "tags": r.tags,
                "sku": r.sku,
                "kind": r.kind,
                "properties": r.properties,
                "drift_deleted_at": ddel_map.get(r.id),
            }
            for r in res_objs
            if r.id not in disappeared_ids
        ]
        post_rows.extend(
            {
                "id": leaf.id,
                "tags": leaf.tags,
                "sku": leaf.sku,
                "kind": leaf.kind,
                "properties": leaf.properties,
                "drift_deleted_at": None,
            }
            for leaf in minted_leaves
        )
        result_fp = drift.state_fingerprint(post_rows)

        options = {
            "intensity": intensity,
            "resource_types": resource_types,
            "codes": codes,
            "subscription": str(sub_uuid) if sub_uuid is not None else None,
        }

        with conn.cursor() as cur:
            # Batch row FIRST (drift_records.batch_id FK → drift_batches).
            cur.execute(
                "INSERT INTO synthetic.drift_batches "
                "(batch_id, drift_type, seed, options, parent_fingerprint, "
                "result_fingerprint, applied_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    batch_id,
                    drift_type,
                    seed,
                    Jsonb(options),
                    parent_fp,
                    result_fp,
                    applied_at,
                ),
            )
            for d in deltas:
                rid = d["resource_id"]
                col = _field_to_column(d["field_path"])  # closed match; raises otherwise
                # Defense-in-depth before splicing the (allowlisted) column name.
                if col not in _UPDATE_COLUMN_ALLOWLIST:  # pragma: no cover
                    raise click.ClickException(f"refusing UPDATE on column {col!r}")
                robj = res_by_id[rid]
                if col == "drift_deleted_at":
                    cur.execute(
                        "UPDATE synthetic.resources SET drift_deleted_at = %s "
                        "WHERE id = %s",
                        (applied_at, rid),
                    )
                elif col in _JSONB_COLUMNS:
                    cur.execute(
                        f"UPDATE synthetic.resources SET {col} = %s WHERE id = %s",
                        (Jsonb(_resource_column_value(robj, col)), rid),
                    )
                else:  # kind (text)
                    cur.execute(
                        f"UPDATE synthetic.resources SET {col} = %s WHERE id = %s",
                        (_resource_column_value(robj, col), rid),
                    )
                code = d["drift_code"]
                cur.execute(
                    "INSERT INTO synthetic.drift_records "
                    "(batch_id, resource_id, subscription_id, field_path, before, "
                    "after, drift_code, metadata) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        batch_id,
                        rid,
                        robj.subscription_id,
                        d["field_path"],
                        Jsonb(d["before"]),
                        Jsonb(d["after"]),
                        code,
                        Jsonb({"drift_code": code, "drift_type": drift_type}),
                    ),
                )

            # Lifecycle persistence (D-09/D-12) — disappear soft-deletes in place
            # (sets the dedicated visibility column; NEVER a hard DELETE), appear
            # INSERTs the minted leaf. Each records a drift_record so revert can
            # unhide (drift_deleted_at) / DELETE (@appear) it (D-13).
            minted_by_id = {leaf.id: leaf for leaf in minted_leaves}
            for d in life_deltas:
                rid = d["resource_id"]
                fpath = d["field_path"]
                if fpath == "drift_deleted_at":  # disappear (soft-delete, D-09)
                    cur.execute(
                        "UPDATE synthetic.resources SET drift_deleted_at = %s "
                        "WHERE id = %s",
                        (applied_at, rid),
                    )
                    code = d["drift_code"]  # CODE_DISAPPEAR
                    cur.execute(
                        "INSERT INTO synthetic.drift_records "
                        "(batch_id, resource_id, subscription_id, field_path, "
                        "before, after, drift_code, metadata) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        (
                            batch_id,
                            rid,
                            res_by_id[rid].subscription_id,
                            "drift_deleted_at",
                            Jsonb(d["before"]),  # None -> revert unhides
                            Jsonb(d["after"]),
                            code,
                            Jsonb({"drift_code": code, "drift_type": drift_type}),
                        ),
                    )
                elif fpath == "@appear":  # appear (mint new leaf, D-12)
                    leaf = minted_by_id[rid]
                    cur.execute(
                        "INSERT INTO synthetic.resources "
                        "(id, subscription_id, resource_group_name, name, type, "
                        "location, tags, sku, kind, properties, provisioning_state, "
                        "managed_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (
                            leaf.id,
                            leaf.subscription_id,
                            leaf.resource_group_name,
                            leaf.name,
                            leaf.type,
                            leaf.location,
                            Jsonb(leaf.tags),
                            Jsonb(leaf.sku) if leaf.sku is not None else None,
                            leaf.kind,
                            Jsonb(leaf.properties),
                            leaf.provisioning_state,
                            leaf.managed_by,
                        ),
                    )
                    code = d["drift_code"]  # CODE_APPEAR
                    cur.execute(
                        "INSERT INTO synthetic.drift_records "
                        "(batch_id, resource_id, subscription_id, field_path, "
                        "before, after, drift_code, metadata) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        (
                            batch_id,
                            rid,
                            leaf.subscription_id,
                            "@appear",  # revert DELETEs the minted row (D-13)
                            Jsonb(d["before"]),
                            Jsonb(d["after"]),
                            code,
                            Jsonb({"drift_code": code, "drift_type": drift_type}),
                        ),
                    )

    click.echo(
        f"apply-drift batch {batch_id}: {drift_type} drift, {planned} records "
        f"(seed={seed}, intensity={intensity}, "
        f"parent_fp={parent_fp[:12]}, result_fp={result_fp[:12]})."
    )
    for note in clamp_notes:
        click.echo(note)


@main.command("revert-drift")
@click.option(
    "--batch-id",
    "batch_id_raw",
    required=True,
    help="The drift batch UUID to revert (parsed-to-UUID before bind).",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Report what would be reverted; mutate NOTHING.",
)
@click.option(
    "--database-url",
    "database_url",
    default=None,
    help="Postgres DSN (defaults to writer.DATABASE_URL / $DATABASE_URL).",
)
def revert_drift(batch_id_raw, dry_run, database_url):
    """Revert one drift batch — LIFO-guarded, single-transaction restore.

    In ONE transaction: reject if a NEWER ACTIVE (``reverted_at IS NULL``) batch
    overlaps any of the target's resources (strict LIFO, BEFORE any mutation);
    else restore each affected resource from its ``drift_records`` per-field
    ``before`` value, unhide disappeared rows / DELETE appear rows, and mark the
    batch ``reverted_at`` WITHOUT deleting history. ``--dry-run`` reports the
    would-revert count and mutates nothing.
    """
    import datetime as _dt
    import uuid as _uuid

    from psycopg.types.json import Jsonb

    from tenantless.generator import writer

    db_url = database_url or writer.DATABASE_URL

    # V5: parse the batch-id to a UUID before any bind (no spliced id).
    try:
        bid = _uuid.UUID(batch_id_raw)
    except ValueError as exc:
        raise click.UsageError(f"--batch-id is not a valid UUID: {exc}")

    # Wall-clock anchored ONCE in the audit layer (A4) — the only time-derived
    # value, written on the reverted_at mark.
    reverted_at = _dt.datetime.now(_dt.timezone.utc)

    with writer.open_writer(db_url) as conn:
        writer.ensure_drift_schema(conn)
        conn.commit()

        with conn.cursor() as cur:
            # Serialize all drift workflow mutations on the fixed application-wide
            # advisory key BEFORE any read (P1, 11-10), the twin of apply-drift: a
            # concurrent apply/revert would otherwise read the same parent state
            # and clobber it with a stale read-modify-write snapshot. The
            # xact-scoped lock auto-releases at transaction end. $N-bound.
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (DRIFT_LOCK_KEY,))

            # Target batch must exist and not already be reverted (D-03). ``seq`` is
            # the monotonic total order (sql/006) the LIFO guard compares on.
            cur.execute(
                "SELECT seq, reverted_at FROM synthetic.drift_batches "
                "WHERE batch_id = %s",
                (bid,),
            )
            row = cur.fetchone()
            if row is None:
                raise click.UsageError(f"no drift batch {bid}")
            target_seq, already_reverted = row
            if already_reverted is not None:
                raise click.UsageError(
                    f"batch {bid} was already reverted at {already_reverted} "
                    "(history is preserved; a batch is reverted once, D-03)."
                )

            # STRICT-LIFO overlap guard (D-06, Pitfall 5): BEFORE any mutation,
            # reject if a strictly-NEWER ACTIVE sibling batch shares any resource_id
            # with the target. ``seq`` is a UNIQUE strictly-increasing total order
            # (sql/006), so ``b.seq > target_seq`` both excludes the target itself
            # (never > its own seq) AND breaks applied_at ties: two same-instant
            # batches get distinct seq, so the newer is revertable first and the
            # pair is never mutually deadlocked (P1, 11-10). $N-bound; raising here
            # rolls the transaction back untouched.
            cur.execute(
                "SELECT count(*) FROM synthetic.drift_batches b "
                "WHERE b.seq > %s "
                "AND b.reverted_at IS NULL "
                "AND EXISTS ("
                "  SELECT 1 FROM synthetic.drift_records nr "
                "  JOIN synthetic.drift_records tr "
                "    ON tr.resource_id = nr.resource_id "
                "  WHERE nr.batch_id = b.batch_id AND tr.batch_id = %s)",
                (target_seq, bid),
            )
            overlap = cur.fetchone()[0]
            if overlap > 0:
                raise click.UsageError(
                    f"refusing to revert {bid}: {overlap} newer active batch(es) "
                    "overlap its resources (strict LIFO, D-06). Revert the newer "
                    "batch(es) first."
                )

            # Read the target batch's per-field deltas (Plan 11-05: before/after
            # are per-FIELD, not full-column).
            cur.execute(
                "SELECT resource_id, field_path, before, after "
                "FROM synthetic.drift_records WHERE batch_id = %s ORDER BY record_id",
                (bid,),
            )
            records = cur.fetchall()
            would = len(records)

            # --dry-run: report the would-revert count, persist NOTHING (the txn
            # is read-only over the synthetic tables; the schema preflight already
            # committed). reverted_at stays NULL and no column changes (D-04).
            if dry_run:
                click.echo(
                    f"[dry-run] would revert {would} records for batch {bid} "
                    "(LIFO guard passed; nothing written)."
                )
                return

            # @appear rows are DELETEd (D-13); everything else is a column-level
            # restore (tags/sku/kind/properties/drift_deleted_at).
            appear_ids = [r[0] for r in records if r[1] == "@appear"]
            field_records = [r for r in records if r[1] != "@appear"]

            # Load the CURRENT served columns for the field-affected resources.
            # NO drift_deleted_at filter — disappeared rows MUST be read so they
            # can be unhidden. $N-bound array (T-11-24).
            affected = sorted({r[0] for r in field_records})
            state: dict = {}
            if affected:
                cur.execute(
                    "SELECT id, tags, sku, kind, properties, drift_deleted_at "
                    "FROM synthetic.resources WHERE id = ANY(%s)",
                    (affected,),
                )
                for rid, tags, sku, kind, props, ddel in cur.fetchall():
                    state[rid] = {
                        "tags": tags,
                        "sku": sku,
                        "kind": kind,
                        "properties": props,
                        "drift_deleted_at": ddel,
                    }

            # Apply each per-field revert into the in-memory column state, in
            # record order, so multiple deltas on one column compose (Pitfall 4:
            # the served response is restored byte-for-byte).
            for rid, field_path, before, after in field_records:
                st = state.get(rid)
                if st is None:
                    continue  # resource no longer present — skip
                col = _field_to_column(field_path)  # closed allowlist (T-11-24)
                if col not in _UPDATE_COLUMN_ALLOWLIST:  # pragma: no cover
                    raise click.ClickException(f"refusing restore on column {col!r}")
                if col == "drift_deleted_at":
                    st["drift_deleted_at"] = before  # None -> unhide (D-13)
                elif col == "sku":
                    st["sku"] = before  # full sku object (or None)
                elif col == "kind":
                    st["kind"] = before
                else:  # properties / tags nested field
                    st[col] = _revert_nested(st[col], field_path, before, after)

            # Write the restored columns back ($N-bound; one UPDATE per resource).
            for rid, st in state.items():
                cur.execute(
                    "UPDATE synthetic.resources SET tags = %s, sku = %s, "
                    "kind = %s, properties = %s, drift_deleted_at = %s "
                    "WHERE id = %s",
                    (
                        Jsonb(st["tags"] if st["tags"] is not None else {}),
                        Jsonb(st["sku"]) if st["sku"] is not None else None,
                        st["kind"],
                        Jsonb(
                            st["properties"] if st["properties"] is not None else {}
                        ),
                        st["drift_deleted_at"],
                        rid,
                    ),
                )

            # DELETE the rows this batch added via appear (D-13).
            for rid in appear_ids:
                cur.execute("DELETE FROM synthetic.resources WHERE id = %s", (rid,))

            # Mark reverted_at — NEVER delete drift history (D-03).
            cur.execute(
                "UPDATE synthetic.drift_batches SET reverted_at = %s "
                "WHERE batch_id = %s",
                (reverted_at, bid),
            )

    click.echo(
        f"revert-drift batch {bid}: restored {len(field_records)} field records, "
        f"deleted {len(appear_ids)} appear rows, marked reverted_at."
    )


@main.command("init-db")
@click.option(
    "--database-url",
    "database_url",
    default=None,
    help="Postgres DSN (defaults to writer.DATABASE_URL / $DATABASE_URL).",
)
def init_db(database_url):
    """Provision the full sql/001..007 schema against DATABASE_URL — no data.

    The provision-WITHOUT-generating path for a bring-your-own Postgres: a user who
    wants to ``serve`` an (initially empty) tenant, or who prefers to provision the
    schema explicitly before a first ``generate``, points ``DATABASE_URL`` at any
    reachable PG16 and runs this. It is a THIN wrapper over the existing idempotent
    ``ensure_*`` seams — no new SQL — applying, IN ORDER:
    base (sql/001..003) -> cost (004) -> identity (005) -> drift (006) ->
    web_metadata (007).

    Provisioning belongs to the write path (``generate``) or to this explicit
    ``init-db``; the server does not create the base schema at boot, so a
    bring-your-own-Postgres user runs ``generate`` or ``init-db`` before ``serve``.

    All five ensure_* functions are idempotent (base via a to_regclass guard, the
    rest via CREATE ... IF NOT EXISTS), so re-running ``init-db`` against an
    already-provisioned database is a harmless no-op.
    """
    from tenantless.generator import writer

    db_url = database_url or writer.DATABASE_URL
    with writer.open_writer(db_url) as conn:
        # Apply 001..007 in dependency order (base tables first).
        writer.ensure_base_schema(conn)
        writer.ensure_cost_schema(conn)
        writer.ensure_identity_schema(conn)
        writer.ensure_drift_schema(conn)
        writer.ensure_web_metadata_schema(conn)
    # Status line: never echo the full database_url (T-07-02) — host only.
    from urllib.parse import urlsplit

    host = urlsplit(db_url).hostname or "the configured host"
    click.echo(f"Provisioned schema 001..007 against {host}.")
