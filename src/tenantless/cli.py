import click

# k-anonymity privacy floor for `--min-bucket-size` (single source of truth in
# privacy.py, shared with the build_profile guard). Imported at module scope
# because the IntRange minimum is bound at decorator-evaluation (import) time.
from tenantless.analyzer.privacy import MIN_BUCKET_FLOOR

# --------------------------------------------------------------------------- #
# apply-drift read-modify-write helpers.
#
# Module-level (importable by tests) so the SQL-builder + field→column map can be
# pinned for injection safety without a DB (project SQL bar). Every
# user value binds as a parameter; the only identifiers spliced into SQL are the
# STATIC read-column list and the CLOSED-MATCH update-column allowlist.
# --------------------------------------------------------------------------- #

# Columns read for the scoped state read (STATIC; never user input). Order is the
# unpack contract in apply_drift.
#
# The read is re-pointed at the resolved view
# ``synthetic.arm_resolved_resources``, which exposes the full baseline
# column set MINUS ``drift_deleted_at`` — the retired soft-delete oracle.
# The view already excludes tombstones, so ``drift_deleted_at`` is neither read
# nor filtered on any more; the unpack contract drops it.
_READ_COLUMNS = (
    "id",
    "subscription_id",
    "type",
    "tags",
    "sku",
    "kind",
    "properties",
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
# snapshots. A constant int that fits a signed BIGINT; the xact-scoped
# lock auto-releases at transaction end.
DRIFT_LOCK_KEY = 0x0D_711F_7000  # "drift" lock, stable across the codebase

# Sibling advisory-lock key serializing the destructive GENERATE critical section
# `generate` takes pg_advisory_xact_lock on this key at the start of its
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
    """Build the $N-bound scoped state read.

    Returns ``(sql, params)`` where every user-supplied value (subscription,
    resource-type list) is a BOUND parameter — never spliced. The subscription is
    pre-parsed to a UUID by the caller; the type list binds as a ``text[]`` array.
    The placeholder count equals ``len(params)`` so a parametrized test can prove
    no user value leaks into the SQL text (deterministic ORDER BY id).

    Reads ``synthetic.arm_resolved_resources`` — the
    liveness authority (``baseline ∪ overlay(present) − tombstones``) — NOT the raw
    baseline, so each apply stacks on the CURRENT resolved state. The retired
    soft-delete visibility conjunct is DROPPED: the view already excludes
    tombstoned ids, so no soft-delete column is read or filtered on.
    """
    cols = ", ".join(_READ_COLUMNS)  # STATIC identifiers
    sql = (
        f"SELECT {cols} FROM synthetic.arm_resolved_resources "
        "WHERE (%s::uuid IS NULL OR subscription_id = %s::uuid) "
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
    (no f-string splice of user values).
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


# --------------------------------------------------------------------------- #
# arm_overlay copy-on-write helpers. Drift stops
# mutating synthetic.* IN PLACE; every apply-time mutation writes a full
# copy-on-write snapshot (present=true) or a tombstone (present=false) to
# synthetic.arm_overlay tagged source='drift'. The revision is assigned by the
# sql/009 BEFORE trigger — NEVER set from Python (the trigger reassigns it on
# both INSERT and ON CONFLICT DO UPDATE, so stacking advances it).
#
# The upsert is a STATIC statement — the ONLY spliced identifiers are the static
# table/column names; id / present / body all bind as %s (body via Jsonb()), so
# no user value is ever f-string-spliced (project SQL bar).
#
# D-04 drift-precedence guard (STATE-03): a user write takes OWNERSHIP of the
# overlay row — it flips source='user' and is authoritative. Drift is the
# overlay's OTHER writer and MUST NEVER overwrite or tombstone a user-owned row.
# The `WHERE synthetic.arm_overlay.source <> 'user'` on the ON CONFLICT DO UPDATE
# makes drift's upsert a NO-OP when the existing row is source='user' (the row is
# neither rewritten nor re-revisioned — the BEFORE UPDATE trigger does not fire
# when the conflict-update WHERE is false), so a source='user' row is a ONE-WAY
# latch: once user-owned, drift yields. Mental model: hand-editing a resource
# detaches it from drift simulation. (The symmetric revert-side DELETE guard —
# `AND source <> 'user'` — lives in revert_drift's recompute below.)
# --------------------------------------------------------------------------- #

_OVERLAY_UPSERT_SQL = (
    "INSERT INTO synthetic.arm_overlay "
    "(id_lower, id, target_kind, source, present, body) "
    "VALUES (lower(%s), %s, 'resource', 'drift', %s, %s) "
    "ON CONFLICT (id_lower) DO UPDATE SET "
    "id = EXCLUDED.id, "
    "target_kind = EXCLUDED.target_kind, "
    "source = 'drift', "
    "present = EXCLUDED.present, "
    "body = EXCLUDED.body "
    # D-04: drift never clobbers a user-owned row (one-way ownership latch).
    "WHERE synthetic.arm_overlay.source <> 'user'"
)


def _overlay_body(robj) -> dict:
    """Build the COMPLETE served ARM body for an ``arm_overlay`` present snapshot.

    Carries EVERY served field so the sql/009 body CHECKs pass: string
    ``id``/``name``/``type``/``location``, object ``tags``, object ``properties``,
    and the OPTIONAL object ``sku`` / string ``kind`` ONLY when set — an absent key
    (never a stored JSON null) matches the baseline ``Option::None`` decode
    (``ck_arm_overlay_optional_types``). Built from the fully-mutated in-memory
    ``Resource`` (``drift.compute_drift`` / ``compute_lifecycle`` mutate it in
    place), so the snapshot reflects the post-drift served state."""
    body = {
        "id": robj.id,
        "name": robj.name,
        "type": robj.type,
        "location": robj.location,
        "tags": dict(robj.tags or {}),
        "properties": dict(robj.properties or {}),
    }
    if robj.sku is not None:
        body["sku"] = dict(robj.sku)
    if robj.kind is not None:
        body["kind"] = robj.kind
    return body


def _overlay_upsert_present(cur, robj, Jsonb) -> None:
    """UPSERT a present=true copy-on-write overlay snapshot for ``robj``."""
    cur.execute(
        _OVERLAY_UPSERT_SQL,
        (robj.id, robj.id, True, Jsonb(_overlay_body(robj))),
    )


def _overlay_upsert_tombstone(cur, rid: str) -> None:
    """UPSERT a present=false overlay tombstone (body NULL) for ``rid``."""
    cur.execute(_OVERLAY_UPSERT_SQL, (rid, rid, False, None))


def _overlay_body_from_replay(body: dict) -> dict:
    """Build a clean arm_overlay present body from a recompute-replay dict.

    The revert recompute replays a dict carrying at least
    ``id``/``name``/``type``/``location`` + ``tags``/``properties`` (and maybe
    ``sku``/``kind``). Re-project it into the canonical served shape the sql/009
    CHECKs accept — object ``tags``/``properties`` always present, optional object
    ``sku`` / string ``kind`` ONLY when set (never a stored JSON null, matching the
    baseline ``Option::None`` decode; ``ck_arm_overlay_optional_types``). Same
    shape as the apply-side ``_overlay_body`` so a replayed row is byte-comparable
    to an applied one."""
    out = {
        "id": body["id"],
        "name": body["name"],
        "type": body["type"],
        "location": body["location"],
        "tags": dict(body.get("tags") or {}),
        "properties": dict(body.get("properties") or {}),
    }
    if body.get("sku") is not None:
        out["sku"] = dict(body["sku"])
    if body.get("kind") is not None:
        out["kind"] = body["kind"]
    return out


def _replay_equals_baseline(present, body, brow) -> bool:
    """Does a recompute-replay result equal the immutable baseline for one id?

    Used by revert to decide DELETE-if-baseline vs UPSERT-else (no zombie).
    ``brow`` is the raw baseline row dict (``None`` for an @appear id with no
    baseline). Equality rules:

      * baseline ABSENT (``brow is None``): equal iff the replay is also absent
        (``present is None``) — the appeared leaf is gone, so the overlay row is
        removed;
      * baseline LIVE: equal iff the replay is present with mutable served fields
        (``tags``/``sku``/``kind``/``properties``) byte-equal to baseline — a
        tombstone or an absent replay always DIFFERS. ``id``/``name``/``type``/
        ``location`` never drift, so they are not compared."""
    if brow is None:
        return present is None
    if present is not True or body is None:
        return False
    return (
        (body.get("tags") or {}) == (brow.get("tags") or {})
        and (body.get("properties") or {}) == (brow.get("properties") or {})
        and body.get("sku") == brow.get("sku")
        and body.get("kind") == brow.get("kind")
    )


class _RGView:
    """Lightweight resource-group view for ``drift.compute_lifecycle``.

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
    """Build ``drift.DisappearRefs`` from $N-bound anti-join source SELECTs.

    Reads the four reference sets a resource must be ABSENT from to be
    disappear-eligible (role-assignment scopes, dependency source/target ids,
    violation resource ids, ``managed_by`` ids). Every statement is STATIC SQL
    (no user/profile input spliced); each table is guarded by ``to_regclass`` so
    a volume predating the identity/cost tables degrades to an empty
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
            "SELECT managed_by FROM synthetic.resources WHERE managed_by IS NOT NULL"  # SYNRES-ALLOW[baseline-replay]: reads the immutable baseline to pick drift-disappear candidates
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
    apply seam recorded (deltas are per-field, NOT full-column). A
    ``None`` ``before`` means the key was ABSENT pre-drift (the chaos/temporal
    catalogue never stores a present-None) so revert removes it; the ``[]`` suffix
    marks an append so revert removes the appended element (``after``). Returns the
    rebuilt container dict (caller writes it back as the full column —
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


def _apply_nested(col_value, field_path: str, before, after):
    """Forward-replay twin of ``_revert_nested`` — apply a per-field ``after``.

    The recompute-from-ledger revert rebuilds each affected id
    by replaying every still-active batch's ``after`` value FORWARD from the
    immutable baseline, in ``(seq, record_id)`` order. Because each batch records
    the ABSOLUTE post-value of a field at apply time, forward replay = last-writer
    -wins per field = the current state (deterministic).

    ``field_path`` is ``properties.<key>`` / ``properties.<key>[]`` / ``tags.<key>``.
    Semantics MIRROR ``_revert_nested`` symmetrically: where ``_revert_nested``
    treats a ``None`` ``before`` as "the key was ABSENT pre-drift" (remove it),
    ``_apply_nested`` treats a ``None`` ``after`` as "the key is ABSENT post-drift"
    (remove it) — so a tag-removal delta (``after=None``) forward-applies as a key
    drop. The ``[]`` suffix marks an append, so forward appends ``after`` (the twin
    of revert removing that appended element). Returns a FRESH container dict
    (never mutates the caller's column value — byte-for-byte restore)."""
    container = dict(col_value or {})
    _head, _, rest = field_path.partition(".")
    if rest.endswith("[]"):
        key = rest[:-2]
        lst = list(container.get(key) or [])
        lst.append(after)
        container[key] = lst
        return container
    if after is None:
        container.pop(rest, None)
    else:
        container[rest] = after
    return container


def _drift_clamp_notes(
    res_objs, drift_type, codes, resource_types, intensity
) -> list[str]:
    """Per-code clamp notes for the run (computed before mutation).

    Mirrors ``compute_drift``'s code/eligible selection but consumes NO RNG
    (``_eligible_population`` / ``planned_count`` are pure), so calling it before
    ``compute_drift`` leaves the seeded draw sequence unchanged. Surfaced via
    ``click.echo`` so a clamp is never silent (clamp-and-report)."""
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
    # build_profile already wrote <out>_review.txt (report-only,
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

    # DoS self-protection: resolve --jobs to a concrete worker count clamped to
    # the core count BEFORE handing it to the pipeline. IntRange(0, None) already
    # rejects negatives at CLI validation, so 0 is the SOLE all-cores sentinel —
    # there is no negative branch. 1 (default) preserves the single-process
    # reference path; the clamp means a huge --jobs never spawns an unbounded pool.
    cpu = os.cpu_count() or 1
    effective_jobs = cpu if jobs == 0 else min(jobs, cpu)

    # plain progress lines to STDERR (no rich/tqdm dependency);
    # the structured run summary goes to STDOUT below. NO drift line.
    started = time.perf_counter()
    click.echo("fitting distributions...", err=True)
    profile_dict = load_profile(resolve_profile(profile))
    n_subs, n_resources = resolve_targets(profile_dict, resources, subscriptions)

    # derive the generation-profile IDENTITY from the raw --profile value,
    # mirroring resolve_profile's resolution order (path-if-exists → bundled-name):
    # an existing file path contributes its stem (e.g. `enterprise-eu.json` →
    # `enterprise-eu`); a bundled name IS the identity (e.g. `enterprise`, `small`).
    from pathlib import Path as _Path

    derived_profile_name = (
        _Path(profile).stem if _Path(profile).is_file() else profile
    )

    # resolve the cost anchor to a single calendar date ONCE (default
    # today()), so every billing period derives from it — never a per-call today().
    import datetime as _dt

    as_of = cost_as_of.date() if cost_as_of is not None else _dt.date.today()

    # DoS self-protection: generation is HOISTED past the emptiness /
    # destructive-confirm gates below — a declined confirmation or an
    # --only-if-empty skip must abort/skip WITHOUT paying the (multi-GB at 500K
    # resources) in-memory tenant + cost materialization. `result` / `tenant` are
    # bound inside the generate branch only after the gate passes; the skip path
    # returns before referencing them, so their scoping stays valid.
    result = None
    tenant = None

    # the cost post-pass streams into a bounded on-disk CostSpool during
    # the CPU phase (bounded memory), so 6-15M cost dicts are never all resident.
    from tenantless.generator import cost as _cost

    skipped = False
    # Lock/txn-regression fix: the destructive-generate exclusion now
    # rides a SESSION advisory lock on a dedicated idle (autocommit) connection instead
    # of an xact lock inside one long write transaction. This preserves the
    # check→generate→write mutual exclusion WITHOUT holding a write transaction or the
    # ensure_* DDL locks across the CPU / multiprocessing-fork phase (the earlier ordering fix had
    # wrapped ALL of generation in a single open_writer txn). Provisioning commits in
    # its own short transaction BEFORE generation; the CPU phase runs with no open
    # write txn; the write phase opens a FRESH txn INSIDE the CostSpool block.
    with writer.open_lock_connection() as lock_conn:
        # Session-scoped lock (pg_advisory_lock), taken FIRST so it spans the gate AND
        # the destructive write as one critical section. Held on the autocommit
        # lock_conn across all three short transactions below; released explicitly at
        # the end (and again by the connection close). Behind the writer seam so DB-free
        # CLI tests stub it (never a raw conn.cursor() here).
        writer.acquire_generate_lock_session(lock_conn, GENERATE_LOCK_KEY)

        # Provisioning in its OWN short transaction that COMMITS before generation, so
        # the bare-CREATE ensure_base DDL and the idempotent ensure_* twins release
        # their table locks BEFORE the CPU phase — no DDL lock crosses the fork.
        with writer.open_writer() as prov_conn:
            # Docker-optional / BYO-Postgres: ensure the BASE synthetic
            # schema (sql/001..003) exists FIRST. A no-op on an already-provisioned
            # Docker volume (function-level to_regclass guard, since sql/001,002 are
            # bare CREATE, not IF NOT EXISTS).
            writer.ensure_base_schema(prov_conn)
            # identity tables — called UNCONDITIONALLY (even with
            # --no-identity) so the mock-server's roleAssignments SELECT serves [].
            writer.ensure_identity_schema(prov_conn)
            # the profile_name column — UNCONDITIONAL so
            # copy_tenant never fails on an older volume.
            writer.ensure_web_metadata_schema(prov_conn)
            # v1.1.10: the case-insensitive resource-group functional index —
            # UNCONDITIONAL (idempotent twin) so a pre-v1.1.10 volume gains it here,
            # committing before the CPU phase so no DDL lock crosses the fork.
            writer.ensure_rg_index_schema(prov_conn)
            # The additive ARM-ID identity fold functions (sql/011) — UNCONDITIONAL
            # (idempotent CREATE OR REPLACE FUNCTION twin). Provisioned here so the
            # functions exist before the post-generation CONCURRENT expression index
            # build + the D-04 fold audit. Behaviour-neutral in this unit: no seam,
            # CHECK, view, or predicate consumes them yet (D-22a additive-only).
            writer.ensure_arm_id_key_schema(prov_conn)
            # D-04 fail-loud pre-cutover audit — run right after the fold functions are
            # provisioned. On the current all-ASCII estate all three checks return 0
            # rows (behaviour-neutral); a non-ASCII divergence or fold-collision RAISES,
            # naming the offending ARM ids, and open_writer rolls the provisioning back.
            writer.audit_arm_id_identity(prov_conn)

        # Emptiness / destructive-confirm gate (gate-before-generate preserved) in
        # its OWN short transaction, under the session lock. --only-if-empty inspects
        # the ENTIRE estate (every synthetic table); a populated OR partially written
        # estate is PRESERVED. A declined/refused truncate aborts HERE, before the
        # expensive generation.
        with writer.open_writer() as gate_conn:
            if only_if_empty and not writer.estate_is_empty(gate_conn):
                skipped = True
            elif not force and not writer.schema_is_empty(gate_conn):
                # truncation is destructive — guard it.
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

        if not skipped:
            # only NOW — every gate passed — do we pay the
            # expensive generation. Same generate_tenant kwargs / cost draw order;
            # the identity assertions in test_generate_gate_ordering.py pin the
            # fingerprint. The "generating tenant..." progress line stays here (after
            # the gate passes, before generate).
            click.echo("generating tenant...", err=True)
            # the CostSpool block SPANS generate AND write — the CPU phase
            # drains cost rows into the bounded on-disk spool with NO open write txn;
            # the write phase (nested below) then streams from the spool into COPY while
            # the file STILL exists. RAII removes it on every exit path.
            with _cost.CostSpool() as spool:
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
                    cost_sink=spool,
                )
                click.echo("computing tag entropy...", err=True)
                tenant = result.tenant
                # Fresh write transaction INSIDE the spool block (the spool file must
                # exist while COPY streams from it). ensure_cost_schema only when there
                # are cost rows — an empty spool is falsy, so an empty-cost profile
                # skips it (and opens no COPY). Idempotent; no-op on volumes that
                # already have synthetic.cost_records.
                with writer.open_writer() as write_conn:
                    if result.cost_records:
                        writer.ensure_cost_schema(write_conn)
                    writer.truncate_synthetic(write_conn)
                    writer.write_tenant(
                        write_conn,
                        tenant,
                        dependencies=result.dependencies,
                        violations=result.violations,
                        cost_records=result.cost_records,
                        principals=result.principals,
                        role_assignments=result.role_assignments,
                    )

        # Release the session lock explicitly (belt-and-suspenders; the autocommit
        # lock connection close on __exit__ also releases it).
        writer.release_generate_lock_session(lock_conn, GENERATE_LOCK_KEY)

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
    # human-readable structured run summary on STDOUT (counts,
    # seed, elapsed, tenant_id). NO drift line — drift is a separate command.
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
    # append the archetype→RG-count coverage line to the summary
    # (a plain STDOUT line — NO new command/API/UI surface). Reuse the
    # already-loaded profile_dict for the label map; count over the built tenant's
    # RG template types so the line reflects what was actually generated.
    _label_map = archetypes.build_label_map(
        profile_dict["resource_group_templates"]
    )
    _coverage = archetypes.archetype_coverage(
        _label_map, (rg.template_type for rg in tenant.resource_groups)
    )
    click.echo(archetypes.render_coverage_line(_coverage))
    # append the confirm-and-rename gate's outcome counts, tallied
    # by pipeline._confirm_and_rename and threaded out on GenerationResult. Plain
    # STDOUT beside the coverage line — NO new command/API/UI/DB surface.
    click.echo(archetypes.render_rg_naming_line(result.rg_naming_metrics))
    # surface every clamp note (never silent).
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
    help="Absolute base URL emitted in nextLinks.",
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
    # Status line: never echo the full database_url.
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

    # Parse + validate user filters (parse-before-bind).
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
    # fingerprint (mirrors the cost --cost-as-of / token exp rule).
    applied_at = _dt.datetime.now(_dt.timezone.utc)
    batch_id = _uuid.uuid4()

    with writer.open_writer(db_url) as conn:
        # Idempotent schema preflight, committed independently of the drift writes.
        # the apply path READS the resolved view and WRITES arm_overlay,
        # and the drift_batches row carries the storage_mode provenance column (from
        # sql/010) — so ensure the overlay (009) + resolver (010) substrates exist too
        # (both fully idempotent; no-op on an already-provisioned tenant).
        writer.ensure_drift_schema(conn)
        writer.ensure_arm_overlay_schema(conn)
        # Additive identity fold functions (sql/011) — provisioned BEFORE the resolver
        # (010) so they exist before any future 010 referencing arm_id_key (00a-ii).
        # Behaviour-neutral in this unit: nothing consumes them yet.
        writer.ensure_arm_id_key_schema(conn)
        writer.ensure_arm_resolver_schema(conn)
        conn.commit()

        # Serialize all drift workflow mutations on a fixed application-wide
        # advisory key BEFORE any read: apply-drift / revert-drift do
        # read-modify-write over JSONB columns with no other serialization, so two
        # concurrent commands would read the same parent state and clobber each
        # other with stale snapshots. The xact-scoped lock auto-releases at
        # transaction end (the open_writer commit). $N-bound.
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (DRIFT_LOCK_KEY,))

        # READ the live scoped state ($N-bound; deterministic ORDER BY id).
        sql, params = _build_scoped_read_sql(sub_uuid, resource_types)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            db_rows = cur.fetchall()

        parent_rows: list[dict] = []
        res_objs: list = []
        ddel_map: dict = {}
        for (
            rid, sub_id, rtype, tags, sku, kind, props,
            rg_name, loc, name, prov, managed,
        ) in db_rows:
            # The resolved view excludes tombstones, so a read row is always
            # LIVE — drift_deleted_at is retired and treated as NULL throughout.
            ddel = None
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

        # Parent fingerprint over the decoded pre-mutation state.
        parent_fp = drift.state_fingerprint(parent_rows)

        # clamp notes — computed BEFORE compute_drift (consumes no RNG).
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

        # Carry-forward: a temporal run ALSO applies the
        # appear/disappear lifecycle in the SAME transaction, so
        # revert's unhide/delete has a real producer. The lifecycle
        # consumes the SAME seeded ctx (after the field draws) — deterministic.
        # By design: the
        # disappear count is the clamped fraction/count of eligible leaves
        # and appear mints the SAME count (symmetric churn — vanish a few, add a
        # few). Appear has no eligible population, so it has no clamp.
        life_deltas: list[dict] = []
        minted_leaves: list = []
        disappeared_ids: set = set()
        if drift_type == "temporal":
            # Gate the lifecycle by the active --codes / --resource-types filters
            # appear/disappear must NOT fire when the filter excludes them,
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
            # Appear-mint collision set: the resolved-view ids (present resources + present
            # overlay rows) PLUS every arm_overlay id INCLUDING tombstones. Tombstones are
            # absent from the resolved view, so without them a deterministic appear could mint
            # onto a user-owned tombstone id — the D-04 guard would no-op the upsert, yet the
            # phantom leaf would still enter result_fp via minted_leaves and break the
            # fingerprint chain (drift-phantom-fingerprint, appear-vs-tombstone). Minting a
            # genuinely-fresh id keeps result_fp equal to the persisted served state.
            seen = {r.id for r in res_objs}
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM synthetic.arm_overlay WHERE target_kind = 'resource'"
                )
                seen |= {row[0] for row in cur.fetchall()}
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
        # mutation. The idempotent schema preflight was committed above.
        if dry_run:
            click.echo(
                f"[dry-run] planned {planned} drift records for {drift_type} drift "
                f"(seed={seed}, intensity={intensity}, batch NOT written)."
            )
            for note in clamp_notes:
                click.echo(note)
            return

        # D-04 / drift-phantom-fingerprint guard: drift's overlay upsert is a NO-OP on a
        # source='user' row (the `_OVERLAY_UPSERT_SQL` `WHERE source <> 'user'` latch), so a
        # drift delta over a user-owned id NEVER persists. `compute_drift` already mutated that
        # id's in-memory Resource, though — so recording its drift_records or folding its
        # mutated values into result_fp would describe a change that did not happen, and the
        # NEXT batch's parent_fp (read from the ACTUAL DB) would not chain. Detect the
        # user-owned ids among the affected set ONCE, then (a) drop their deltas so no phantom
        # drift_record is written, and (b) fingerprint their pre-drift (persisted) values.
        affected_ids = {d["resource_id"] for d in deltas}
        affected_ids |= {d["resource_id"] for d in life_deltas}
        user_owned_lower: set = set()
        if affected_ids:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM synthetic.arm_overlay "
                    "WHERE source = 'user' AND id_lower = ANY(%s)",
                    ([a.lower() for a in affected_ids],),
                )
                user_owned_lower = {row[0].lower() for row in cur.fetchall()}

        if user_owned_lower:
            deltas = [d for d in deltas if d["resource_id"].lower() not in user_owned_lower]
            life_deltas = [
                d for d in life_deltas if d["resource_id"].lower() not in user_owned_lower
            ]
            # Recompute the disappear set from the FILTERED life_deltas so a user-owned row
            # drift wanted to disappear is NOT dropped from the active-set digest.
            disappeared_ids = {
                d["resource_id"]
                for d in life_deltas
                if d["field_path"] == "drift_deleted_at"
            }
            # The persisted-record count is what the applied echo should report.
            planned = len(deltas) + len(life_deltas)

        # Result fingerprint over the post-mutation ACTIVE state so it CHAINS to the
        # next apply's parent fingerprint. The parent read is the
        # scoped `WHERE drift_deleted_at IS NULL` view, so the result_fp must cover
        # the SAME active-set convention: rows disappeared in THIS batch leave the
        # served/active view and are dropped from the digest (they would otherwise
        # be present here but absent from the next parent read → unchainable). Minted
        # appear-leaves are active and are appended below. A user-owned id contributes its
        # PRE-drift (parent_rows) values — identical to what parent_fp hashed and to what
        # actually persisted (drift skipped it) — so the fingerprint chain holds.
        parent_by_id = {row["id"]: row for row in parent_rows}
        post_rows = []
        for r in res_objs:
            if r.id in disappeared_ids:
                continue
            if r.id.lower() in user_owned_lower:
                src = parent_by_id[r.id]
                post_rows.append(
                    {
                        "id": r.id,
                        "tags": src["tags"],
                        "sku": src["sku"],
                        "kind": src["kind"],
                        "properties": src["properties"],
                        "drift_deleted_at": ddel_map.get(r.id),
                    }
                )
            else:
                post_rows.append(
                    {
                        "id": r.id,
                        "tags": r.tags,
                        "sku": r.sku,
                        "kind": r.kind,
                        "properties": r.properties,
                        "drift_deleted_at": ddel_map.get(r.id),
                    }
                )
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
            # Stamp storage_mode='overlay' (sql/010 provenance marker):
            # this batch writes the overlay, not synthetic.* in place, so the
            # fail-closed boot guard (which trips on an ACTIVE
            # storage_mode='synthetic' batch) lets a migrated tenant boot.
            cur.execute(
                "INSERT INTO synthetic.drift_batches "
                "(batch_id, drift_type, seed, options, parent_fingerprint, "
                "result_fingerprint, applied_at, storage_mode) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, 'overlay')",
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
            # Copy-on-write onto arm_overlay: each affected
            # resource gets ONE full-body snapshot (compute_drift already applied
            # ALL its field mutations to the in-memory Resource, so a single upsert
            # per id captures the complete post-drift served state). The BEFORE
            # trigger assigns the revision. synthetic.resources is NEVER touched.
            overlaid_ids: set = set()
            for d in deltas:
                rid = d["resource_id"]
                col = _field_to_column(d["field_path"])  # closed match; raises otherwise
                # Defense-in-depth: the field→column map may only yield an allowlist
                # member (belt-and-suspenders behind the closed _field_to_column).
                if col not in _UPDATE_COLUMN_ALLOWLIST:  # pragma: no cover
                    raise click.ClickException(f"refusing overlay write for column {col!r}")
                robj = res_by_id[rid]
                if rid not in overlaid_ids:
                    if col == "drift_deleted_at":
                        _overlay_upsert_tombstone(cur, rid)
                    else:  # tags / sku / kind / properties → full-body snapshot
                        _overlay_upsert_present(cur, robj, Jsonb)
                    overlaid_ids.add(rid)
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

            # Lifecycle persistence — disappear writes an overlay
            # TOMBSTONE (present=false, source='drift'; NEVER an in-place soft-delete
            # of synthetic.resources), appear writes an overlay PRESENT row for the
            # minted leaf (baseline never gains it). Each records a drift_record so a
            # revert can recompute-from-ledger.
            minted_by_id = {leaf.id: leaf for leaf in minted_leaves}
            for d in life_deltas:
                rid = d["resource_id"]
                fpath = d["field_path"]
                if fpath == "drift_deleted_at":  # disappear → overlay tombstone
                    _overlay_upsert_tombstone(cur, rid)
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
                elif fpath == "@appear":  # appear → overlay present row
                    leaf = minted_by_id[rid]
                    # The minted leaf becomes a present=true source='drift' overlay
                    # row — the BASELINE is NEVER written. Build the FULL served body
                    # once and reuse it for the ledger (replay-sufficiency below).
                    appear_body = _overlay_body(leaf)
                    cur.execute(
                        _OVERLAY_UPSERT_SQL,
                        (leaf.id, leaf.id, True, Jsonb(appear_body)),
                    )
                    code = d["drift_code"]  # CODE_APPEAR
                    # the minted leaf no longer lives
                    # in synthetic.resources, so persist its COMPLETE served body in
                    # the @appear drift_record under metadata['appear_body'] — a
                    # revert replay reconstructs the overlay row from
                    # the ledger ALONE, byte-equal to this stored body.
                    cur.execute(
                        "INSERT INTO synthetic.drift_records "
                        "(batch_id, resource_id, subscription_id, field_path, "
                        "before, after, drift_code, metadata) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        (
                            batch_id,
                            rid,
                            leaf.subscription_id,
                            "@appear",  # revert reconstructs from appear_body
                            Jsonb(d["before"]),
                            Jsonb(d["after"]),
                            code,
                            Jsonb(
                                {
                                    "drift_code": code,
                                    "drift_type": drift_type,
                                    "appear_body": appear_body,
                                }
                            ),
                        ),
                    )

    click.echo(
        f"apply-drift batch {batch_id}: {drift_type} drift, {planned} records "
        f"(seed={seed}, intensity={intensity}, "
        f"parent_fp={parent_fp[:12]}, result_fp={result_fp[:12]})."
    )
    for note in clamp_notes:
        click.echo(note)


@main.command("reset")
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Report the overlay/ledger row counts that WOULD be cleared; mutate NOTHING.",
)
@click.option(
    "--database-url",
    "database_url",
    default=None,
    help="Postgres DSN (defaults to writer.DATABASE_URL / $DATABASE_URL).",
)
def reset(dry_run, database_url):
    """Return the served tenant to the immutable pre-drift baseline.

    Transactionally clears the drift ledger + ARM overlay so the resolver
    (``baseline ∪ overlay(present) − tombstones``) once again resolves to the
    pristine seeded baseline, WITHOUT rebuilding the tenant. Concretely, in ONE
    transaction under the fixed ``DRIFT_LOCK_KEY`` advisory lock (so reset
    serializes against ``apply-drift`` / ``revert-drift``), it issues the FK-ordered
    clears ``DELETE FROM synthetic.drift_records`` → ``drift_batches`` →
    ``arm_overlay``.

    This is NOT the full-wipe ``POST /_control/reset`` (``job.rs::run_reset``): that
    path wholesale-empties every ``synthetic`` baseline relation and re-mints the
    tenant signer to produce a BLANK tenant. Baseline-reset preserves tenant identity
    — it leaves ALL four baseline relations (resources, resource_groups,
    subscriptions, tenant) untouched, does not re-mint the signer, and does not rewind
    or rebase the ``arm_overlay_revision`` sequence (it stays monotonic across resets
    — ETag/revision stability). Clearing ``drift_batches`` also clears the
    ``storage_mode`` provenance marker (it is only a ``drift_batches`` column), so a
    reset tenant passes the fail-closed boot guard. All statements are static
    literals (no user value spliced).
    """
    from tenantless.generator import writer

    db_url = database_url or writer.DATABASE_URL

    with writer.open_writer(db_url) as conn:
        # Idempotent schema preflight, committed independently of the reset clears
        # (mirrors apply-drift): ensure the drift ledger + overlay + resolver
        # substrates exist so a never-drifted tenant resets cleanly too. Fully
        # idempotent; a no-op on an already-provisioned tenant.
        writer.ensure_drift_schema(conn)
        writer.ensure_arm_overlay_schema(conn)
        # Additive identity fold functions (sql/011) — provisioned BEFORE the resolver
        # (010) so they exist before any future 010 referencing arm_id_key (00a-ii).
        # Behaviour-neutral in this unit: nothing consumes them yet.
        writer.ensure_arm_id_key_schema(conn)
        writer.ensure_arm_resolver_schema(conn)
        conn.commit()

        with conn.cursor() as cur:
            # Serialize on the SAME fixed advisory key apply/revert take,
            # BEFORE any read, so reset can never interleave with an in-flight drift
            # read-modify-write. The xact-scoped lock auto-releases at transaction end
            # (the open_writer commit). $N-bound.
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (DRIFT_LOCK_KEY,))

            # Snapshot the counts we are about to clear (for the report / dry-run).
            cur.execute("SELECT count(*) FROM synthetic.drift_records")
            n_records = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM synthetic.drift_batches")
            n_batches = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM synthetic.arm_overlay")
            n_overlay = cur.fetchone()[0]

            # --dry-run: report the clear plan and persist NOTHING. Roll back the
            # lock-only transaction so no mutation (and no lock) survives.
            if dry_run:
                conn.rollback()
                click.echo(
                    f"[dry-run] reset would clear {n_overlay} overlay rows, "
                    f"{n_records} drift_records, {n_batches} drift_batches "
                    "(baseline + revision sequence preserved; nothing written)."
                )
                return

            # FK-ordered clears in ONE transaction (drift_records.batch_id FK →
            # drift_batches). The revision sequence is left as-is (preserve
            # monotonicity — never rewound). The four baseline relations
            # (resources, resource_groups, subscriptions, tenant) are left untouched;
            # the signer is not re-minted.
            cur.execute("DELETE FROM synthetic.drift_records")
            cur.execute("DELETE FROM synthetic.drift_batches")
            cur.execute("DELETE FROM synthetic.arm_overlay")

    click.echo(
        f"reset: cleared {n_overlay} overlay rows, {n_records} drift_records, "
        f"{n_batches} drift_batches (baseline + revision sequence preserved)."
    )


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
    """Revert one drift batch — recompute-from-ledger onto arm_overlay.

    The drift ledger (``drift_batches`` + ``drift_records``) is the authoritative
    per-batch delta history; ``arm_overlay`` is materialized current state. In ONE
    transaction under the drift advisory lock: for each id the target touched,
    rebuild its overlay state from the IMMUTABLE baseline (raw ``synthetic.resources``)
    by REPLAYING every still-active overlay batch EXCEPT the target
    (``storage_mode='overlay' AND reverted_at IS NULL AND batch_id<>target``) in
    ``(seq, record_id)`` order (last-writer-wins), then DELETE the overlay row iff
    the replayed result equals baseline (no zombie) else UPSERT a fresh
    ``source='drift'`` snapshot, and mark the target ``reverted_at`` WITHOUT
    deleting history. ``synthetic.*`` is NEVER mutated in place.

    The strict-LIFO overlap guard is intentionally REMOVED: recompute-from-ledger
    rebuilds the overlay from whatever active batches remain, so ANY batch —
    including a middle batch under a newer active overlapping batch — is
    independently revertable. ``--dry-run``
    reports the would-revert count and mutates nothing.
    """
    import datetime as _dt
    import uuid as _uuid

    from psycopg.types.json import Jsonb

    from tenantless.generator import writer

    db_url = database_url or writer.DATABASE_URL

    # parse the batch-id to a UUID before any bind (no spliced id).
    try:
        bid = _uuid.UUID(batch_id_raw)
    except ValueError as exc:
        raise click.UsageError(f"--batch-id is not a valid UUID: {exc}")

    # Wall-clock anchored ONCE in the audit layer — the only time-derived
    # value, written on the reverted_at mark.
    reverted_at = _dt.datetime.now(_dt.timezone.utc)

    with writer.open_writer(db_url) as conn:
        # Idempotent schema preflight, committed independently. Revert
        # now RECOMPUTES onto arm_overlay and reads drift_batches.storage_mode /
        # reverted_at, so ensure the overlay (009) + resolver (010) substrates too
        # (both fully idempotent; no-op on an already-provisioned tenant).
        writer.ensure_drift_schema(conn)
        writer.ensure_arm_overlay_schema(conn)
        # Additive identity fold functions (sql/011) — provisioned BEFORE the resolver
        # (010) so they exist before any future 010 referencing arm_id_key (00a-ii).
        # Behaviour-neutral in this unit: nothing consumes them yet.
        writer.ensure_arm_id_key_schema(conn)
        writer.ensure_arm_resolver_schema(conn)
        conn.commit()

        with conn.cursor() as cur:
            # Serialize all drift workflow mutations on the fixed application-wide
            # advisory key BEFORE any read, the twin of apply-drift: a
            # concurrent apply/revert would otherwise read the same parent state
            # and clobber it with a stale read-modify-write snapshot. The
            # xact-scoped lock auto-releases at transaction end. $N-bound.
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (DRIFT_LOCK_KEY,))

            # Target batch must exist and not already be reverted.
            cur.execute(
                "SELECT reverted_at FROM synthetic.drift_batches WHERE batch_id = %s",
                (bid,),
            )
            row = cur.fetchone()
            if row is None:
                raise click.UsageError(f"no drift batch {bid}")
            (already_reverted,) = row
            if already_reverted is not None:
                raise click.UsageError(
                    f"batch {bid} was already reverted at {already_reverted} "
                    "(history is preserved; a batch is reverted once)."
                )

            # The strict-LIFO overlap guard is intentionally REMOVED here.
            # Recompute-from-ledger rebuilds each affected id's
            # overlay from the immutable baseline by replaying whatever active
            # batches remain — so reverting a MIDDLE batch (under a newer active
            # overlapping batch) is well-defined and correct, and the old
            # strictly-newer-sibling rejection would contradict the "any batch is
            # revertable" model.

            # Read the target batch's per-field deltas to determine the affected
            # id set (before/after are per-FIELD, not full-column).
            cur.execute(
                "SELECT resource_id, field_path, before, after "
                "FROM synthetic.drift_records WHERE batch_id = %s ORDER BY record_id",
                (bid,),
            )
            records = cur.fetchall()
            would = len(records)

            # --dry-run: report the would-revert count, persist NOTHING (the txn
            # is read-only; the schema preflight already committed). reverted_at
            # stays NULL and no overlay/baseline changes.
            if dry_run:
                click.echo(
                    f"[dry-run] would revert {would} records for batch {bid} "
                    "(recompute-from-ledger; nothing written)."
                )
                return

            # The affected id set = every resource the TARGET batch touched.
            affected = sorted({r[0] for r in records})

            # (1) Immutable baseline for each affected id, read from RAW
            # synthetic.resources (NOT the resolved view — the view already folds in
            # the overlay). @appear ids have NO baseline row (baseline absent).
            # $N-bound array. This is a READ ONLY — synthetic.* is never
            # mutated in the revert path.
            baseline: dict = {}
            if affected:
                cur.execute(
                    "SELECT id, name, type, location, tags, sku, kind, properties "
                    "FROM synthetic.resources WHERE id = ANY(%s)",  # SYNRES-ALLOW[baseline-replay]: revert recomputes overlay state from the IMMUTABLE baseline, not the resolved view
                    (affected,),
                )
                for rid, name, rtype, loc, tags, sku, kind, props in cur.fetchall():
                    baseline[rid] = {
                        "id": rid,
                        "name": name,
                        "type": rtype,
                        "location": loc,
                        "tags": dict(tags or {}),
                        "properties": dict(props or {}),
                        "sku": dict(sku) if sku is not None else None,
                        "kind": kind,
                    }

            # (2) Every STILL-ACTIVE overlay batch EXCEPT the target, and its
            # drift_records for the affected ids, ordered by (seq, record_id) — the
            # deterministic total order (seq = GENERATED IDENTITY, sql/006; NOT
            # applied_at, which ties). Forward-replaying each `after` in this order =
            # last-writer-wins per field = current state minus the target.
            # Determinism; all values $N-bound.
            replay_by_id: dict = {rid: [] for rid in affected}
            if affected:
                cur.execute(
                    "SELECT r.resource_id, r.field_path, r.before, r.after, r.metadata "
                    "FROM synthetic.drift_records r "
                    "JOIN synthetic.drift_batches b ON b.batch_id = r.batch_id "
                    "WHERE b.storage_mode = 'overlay' "
                    "AND b.reverted_at IS NULL "
                    "AND b.batch_id <> %s "
                    "AND r.resource_id = ANY(%s) "
                    "ORDER BY r.resource_id, b.seq, r.record_id",
                    (bid, affected),
                )
                for rid, fpath, before, after, metadata in cur.fetchall():
                    replay_by_id[rid].append((fpath, before, after, metadata))

            # (3) Replay forward from baseline per id → (present, body). present is
            # None (absent — no baseline row and no active appear), True (live), or
            # False (tombstone). @appear rebuilds the FULL body from the ledger's
            # stored metadata.appear_body; disappear → tombstone; field
            # deltas via _apply_nested(after) / full sku|kind set.
            deleted = 0
            upserted = 0
            for rid in affected:
                brow = baseline.get(rid)
                if brow is not None:
                    present: bool | None = True
                    body = dict(brow)
                else:
                    present = None  # baseline absent (an @appear id)
                    body = None

                for fpath, before, after, metadata in replay_by_id[rid]:
                    if fpath == "@appear":
                        # The minted leaf's full served body lives in the ledger.
                        body = dict((metadata or {}).get("appear_body") or {})
                        present = True
                    elif fpath == "drift_deleted_at":
                        # Forward-apply disappear: a non-None `after` marker hides the
                        # id (tombstone); a None `after` (unhide) makes it present.
                        present = False if after is not None else True
                    else:
                        col = _field_to_column(fpath)  # closed allowlist
                        if col not in _UPDATE_COLUMN_ALLOWLIST:  # pragma: no cover
                            raise click.ClickException(
                                f"refusing replay on column {col!r}"
                            )
                        if body is None:
                            # Field delta on a not-yet-present id (e.g. its appear is
                            # the reverted target) — no live body to mutate; skip.
                            continue
                        if col == "sku":
                            if after is None:
                                body.pop("sku", None)
                            else:
                                body["sku"] = after  # full sku object
                        elif col == "kind":
                            if after is None:
                                body.pop("kind", None)
                            else:
                                body["kind"] = after
                        else:  # properties / tags nested field
                            body[col] = _apply_nested(
                                body.get(col) or {}, fpath, before, after
                            )

                # (4) DELETE-if-baseline else UPSERT-fresh. Compare the replayed
                # result to baseline; if EQUAL (no active batch still drifts this id)
                # the overlay row is superfluous → DELETE it (no zombie).
                # Otherwise UPSERT a fresh source='drift' snapshot via the
                # ON CONFLICT upsert (the sql/009 trigger advances the revision).
                # A blind delete is NEVER issued — the else-branch always rewrites.
                if _replay_equals_baseline(present, body, brow):
                    # D-04: revert's from-baseline recompute NEVER deletes (or
                    # overwrites — see the _OVERLAY_UPSERT_SQL guard) a user-owned
                    # row. `AND source <> 'user'` makes this DELETE a no-op when the
                    # id is source='user', so a user PUT/DELETE survives revert
                    # untouched (the one-way ownership latch; STATE-03).
                    cur.execute(
                        "DELETE FROM synthetic.arm_overlay "
                        "WHERE id_lower = lower(%s) AND target_kind = 'resource' "
                        "AND source <> 'user'",
                        (rid,),
                    )
                    deleted += 1
                elif present is False:
                    _overlay_upsert_tombstone(cur, rid)
                    upserted += 1
                else:  # present live row with a full body
                    cur.execute(
                        _OVERLAY_UPSERT_SQL,
                        (rid, rid, True, Jsonb(_overlay_body_from_replay(body))),
                    )
                    upserted += 1

            # (5) Mark reverted_at — NEVER delete drift history. synthetic.*
            # is untouched throughout: only arm_overlay + this mark write.
            cur.execute(
                "UPDATE synthetic.drift_batches SET reverted_at = %s "
                "WHERE batch_id = %s",
                (reverted_at, bid),
            )

    click.echo(
        f"revert-drift batch {bid}: recomputed {len(affected)} ids from ledger "
        f"({deleted} overlay rows deleted, {upserted} upserted), marked reverted_at."
    )


@main.command("init-db")
@click.option(
    "--database-url",
    "database_url",
    default=None,
    help="Postgres DSN (defaults to writer.DATABASE_URL / $DATABASE_URL).",
)
def init_db(database_url):
    """Provision the full sql/001..010 schema against DATABASE_URL — no data.

    The provision-WITHOUT-generating path for a bring-your-own Postgres: a user who
    wants to ``serve`` an (initially empty) tenant, or who prefers to provision the
    schema explicitly before a first ``generate``, points ``DATABASE_URL`` at any
    reachable PG16 and runs this. It is a THIN wrapper over the existing idempotent
    ``ensure_*`` seams — no new SQL — applying, IN ORDER:
    base (sql/001..003) -> cost (004) -> identity (005) -> drift (006) ->
    web_metadata (007) -> rg_index (008) -> arm_overlay (009) ->
    arm_id_key (011) -> arm_resolver (010). The identity fold functions (011) are
    applied BEFORE the resolver (010) so they exist before any future 010 that
    references arm_id_key (00a-ii) — the boot-safety ordering.

    Provisioning belongs to the write path (``generate``) or to this explicit
    ``init-db``; the server does not create the base schema at boot, so a
    bring-your-own-Postgres user runs ``generate`` or ``init-db`` before ``serve``.

    All ensure_* functions are idempotent (base via a to_regclass guard, the
    rest via CREATE ... IF NOT EXISTS + guarded DO blocks), so re-running
    ``init-db`` against an already-provisioned database is a harmless no-op.

    Reports HONESTLY: if a bundled migration file is absent — for example an
    installed package shipped without its ``sql/`` data files — the command exits
    nonzero and NAMES the missing migration(s) instead of printing a false
    "Applied migrations 001..010" success. The host-only status line prints ONLY
    on full success.

    ATOMIC (all-or-nothing): BEFORE opening any transaction, a pre-flight gate
    verifies all ten migration files exist — a missing bundled file aborts with
    the database untouched (no half-open connection). Only if all ten are present
    is a single writer transaction opened; ANY failure inside it (a partially
    applied base schema, or a migration whose file vanished at apply time) is raised
    inside the transaction so it rolls the whole thing back — the schema is never
    left half-provisioned, and the status line prints only after a clean commit.
    """
    from tenantless.generator import writer

    db_url = database_url or writer.DATABASE_URL

    # Pre-flight file gate: verify ALL 10 migration files exist BEFORE opening
    # any transaction. A missing bundled file (the packaging bug) aborts here — no
    # DB connection is opened, nothing is touched.
    missing = [p for p in writer._all_migration_sql_files() if not p.is_file()]
    if missing:
        raise click.ClickException(
            "init-db could not provision — missing bundled migration file(s): "
            + ", ".join(p.name for p in missing)
            + ". The installed package is missing bundled sql/ — reinstall a wheel "
            "built with force-include, or run against a repo checkout / docker initdb."
        )

    # All ten present -> apply all-or-nothing inside ONE writer transaction. Any
    # exception raised here propagates OUT of the `with`, so open_writer rolls back
    # everything (never a record-then-commit-then-raise partial provision).
    with writer.open_writer(db_url) as conn:
        # Base (001..003): a PartialBaseSchemaError (a ClickException) surfaces a
        # partly-migrated base and rolls back; True/False is applied-vs-already-
        # present (Docker volume / re-run no-op), NOT a failure.
        writer.ensure_base_schema(conn)
        # Twins (004..010) IN ORDER: a False return means the file vanished between
        # the pre-flight gate and apply (should not happen after the gate) — treat
        # it as a hard failure and raise INSIDE the with so the base apply rolls back.
        for name, ensure in (
            ("004_cost", writer.ensure_cost_schema),
            ("005_identity", writer.ensure_identity_schema),
            ("006_drift", writer.ensure_drift_schema),
            ("007_web_metadata", writer.ensure_web_metadata_schema),
            ("008_rg_lower_index", writer.ensure_rg_index_schema),
            ("009_arm_overlay", writer.ensure_arm_overlay_schema),
            # 011 (identity fold functions) applied BEFORE 010 so the functions exist
            # before any future 010 referencing arm_id_key (00a-ii) — boot-safety order.
            ("011_arm_id_key", writer.ensure_arm_id_key_schema),
            ("010_arm_resolver", writer.ensure_arm_resolver_schema),
        ):
            if not ensure(conn):
                raise click.ClickException(
                    f"init-db could not provision — migration {name} became "
                    "unavailable during apply (bundled sql/ missing). Nothing was "
                    "committed; the database is unchanged."
                )
        # D-04 fail-loud pre-cutover ARM-ID identity audit — after the full chain
        # (base + overlay + fold functions) is applied so both synthetic.resources and
        # synthetic.arm_overlay exist. On the current all-ASCII estate all checks return
        # 0 rows (behaviour-neutral); any divergence / fold-collision RAISES (naming the
        # offending ARM ids) INSIDE the with, so open_writer rolls the whole apply back.
        writer.audit_arm_id_identity(conn)

    # Status line: prints ONLY after a clean commit. Never echo the full
    # database_url — host only.
    from urllib.parse import urlsplit

    host = urlsplit(db_url).hostname or "the configured host"
    # Honest status: init-db APPLIES the idempotent migrations; it does not perform
    # the deep structural inventory the mock-server runs at boot (arm_overlay_inventory). Say so
    # rather than imply a verification this path did not do.
    click.echo(
        f"Applied migrations 001..011 against {host}. "
        "The mock-server enforces structural verification of the overlay + resolver "
        "substrate at boot."
    )
