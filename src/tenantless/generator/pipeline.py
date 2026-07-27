"""DB-free orchestration: profile + targets → a sampled synthetic tenant.

Inverse of the analyzer's ``profile.build_profile``: instead of reading real data
and emitting a profile, this samples a profile into concrete rows. All sampling
is pure and DB-free so GEN-01/02/03 and the D-01 reproducibility test run without
Postgres; only :mod:`tenantless.generator.writer` touches the database.

Pipeline (this plan — Plan 02-02 extends 02-01):
    seed → SeededContext → mint tenant_id → sample subscriptions (per archetype
    weight, GEN-02) → per sub sample RG count/template/location (GEN-03) → per RG
    sample the resource type mix and generate ARM-valid resources (GEN-04/07),
    then wire intra-resource references per subscription (GEN-08).

Reference wiring (GEN-08) runs in a SECOND pass scoped to each subscription so a
VM can reference a NIC/subnet generated in any RG of the same subscription
(RESEARCH Open Question 2, recommendation (b) — subscription-scoped pool, with a
minimal-companion fallback when a pool is empty).
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import uuid
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from numpy.random import SeedSequence

from . import archetypes, calibrate, naming, resources, sampling, tags
from .rng import SeededContext


@dataclass
class ResourceGroup:
    id: str  # full ARM path: /subscriptions/{subId}/resourceGroups/{name}
    subscription_id: uuid.UUID
    name: str
    location: str
    template_type: str
    tags: dict[str, str] = field(default_factory=dict)
    provisioning_state: str = "Succeeded"
    resources: list[Any] = field(default_factory=list)  # filled in 02-02


@dataclass
class Subscription:
    subscription_id: uuid.UUID
    tenant_id: uuid.UUID
    display_name: str
    archetype: str
    state: str = "Enabled"
    tags: dict[str, str] = field(default_factory=dict)
    authorization_source: str = "RoleBased"
    spending_limit: str = "Off"


@dataclass
class Tenant:
    tenant_id: uuid.UUID
    display_name: str
    profile_version: str
    scale_params: dict[str, Any]
    # D-14: the generation-profile IDENTITY (e.g. `enterprise-eu`) the Web Console's
    # /_sim/summary surfaces as `profile`. Defaults to None so every existing Tenant
    # constructor stays valid and the None→NULL COPY path keeps back-compat.
    profile_name: str | None = None
    subscriptions: list[Subscription] = field(default_factory=list)
    resource_groups: list[ResourceGroup] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class GenerationResult:
    """Immutable snapshot of one ``generate_tenant`` run (PLAT-01, D-01..D-03).

    Replaces the legacy positional 4-tuple so later v2 phases (cost/identity/
    drift) can add their own fields without breaking the signature. ``frozen``
    blocks attribute rebinding (D-02); the collection fields are ``tuple`` —
    not ``list`` — so the *contents* are immutable too (D-03). ``Tenant`` itself
    stays a mutable dataclass: it is built by ``.append()`` during generation
    and is only frozen by reference here, not deep-frozen.
    """

    tenant: Tenant
    violations: tuple[dict, ...]
    dependencies: tuple[dict, ...]
    clamp_notes: tuple[str, ...]
    cost_records: tuple[dict, ...] = ()
    principals: tuple[dict, ...] = ()  # Phase-10 IAM-01
    role_assignments: tuple[dict, ...] = ()  # Phase-10 IAM-02
    # D-06: the over-privilege injection count is recorded ONLY here (and surfaced
    # in the run summary) — NOT as a marker column on the served role_assignments.
    over_privilege_count: int = 0
    # ARCH-03 / D-18 (Plan 19-05): the confirm-and-rename gap-closure tally
    # {confirmed, downgraded_to_generic, child_credit_confirmed, already_generic}.
    # Default empty dict so every existing GenerationResult constructor stays valid.
    rg_naming_metrics: dict[str, int] = field(default_factory=dict)


def _default_targets(profile: dict[str, Any], n_subs, n_resources) -> tuple[int, int]:
    """Default omitted targets from ``source_stats`` (D-05)."""
    stats = profile["source_stats"]
    if n_subs is None:
        n_subs = int(stats["total_subscriptions"])
    if n_resources is None:
        n_resources = int(stats["total_resources"])
    return int(n_subs), int(n_resources)


# --------------------------------------------------------------------------- #
# ARCH-GAP-01 (Plan 19-05, D-14/D-16/D-17): the deterministic post-materialization
# confirm-and-rename pass. The RG-name token minted at creation time derives from
# the *template*; the RG's served resources are an independent per-instance sample
# (plus calibrate trim/pad and cross_sub injection). This pass reconciles the NAME
# to the FINAL materialized CONTENTS — downgrade-only (never relabel, D-14), empty
# RGs -> generic (D-17) — and rewrites every id/reference so the served tenant stays
# referentially intact. Pure / RNG-free (grep-verifiable): the collision suffix scan
# is a deterministic numeric walk, so jobs=1 stays byte-identical to jobs=N (T-19-02).
# --------------------------------------------------------------------------- #

# Matches a resolvable ARM id's SUBSCRIPTION + RG segment (WITH the trailing slash —
# resource ids always carry `/subscriptions/{sub}/resourceGroups/{name}/providers/...`).
# The RG's OWN id (tail `/resourceGroups/{name}`, no trailing slash) is rewritten
# structurally instead.
#
# T-19-14: the subscription MUST be part of the match. RG names are unique only WITHIN
# a subscription, so a name-keyed rewrite applied tenant-wide corrupts a same-named RG's
# references in a DIFFERENT subscription (measured: 1339 shared RG names on the seed-7
# enterprise tenant). Capturing both makes the lookup key `(subscription, old_name)`.
_RG_SEG_RE = re.compile(r"/subscriptions/([^/]+)/resourceGroups/([^/]+)/")
# Grammar parser for the defensive rename fallback: rg-{bu}-{env}-{token}-{nn} where
# bu/env are single hyphen-free words and the token may itself contain hyphens.
_RG_NAME_RE = re.compile(r"^(rg-[a-z0-9]+-[a-z0-9]+)-(.+)-(\d+)$")


def _rewrite_refs(value: Any, seg_map: dict[tuple[str, str], str]) -> Any:
    """Recursively rewrite every `/resourceGroups/{old}/` segment in any str leaf,
    SCOPED to the subscription that owns the reference.

    Walks arbitrary dict/list/str structure. For each STRING leaf it does a SINGLE
    left-to-right regex pass keyed on `/subscriptions/{sub}/resourceGroups/{name}/`,
    substituting via ONE ``seg_map`` lookup per match on the ``(subscription, old_name)``
    tuple — ``re.sub`` never re-examines replacement text, so a value rewritten to a new
    name that happens to equal another OLD name is NOT double-rewritten. A reference whose
    (subscription, name) pair was not renamed is returned verbatim, which is what keeps a
    rename in one subscription from touching an identically-named RG in another (T-19-14).
    Returns a rewritten copy (scalars returned as-is). Pure — no RNG/DB/wall-clock.
    """
    if isinstance(value, str):
        return _RG_SEG_RE.sub(
            lambda m: (
                f"/subscriptions/{m.group(1)}/resourceGroups/"
                f"{seg_map.get((m.group(1), m.group(2)), m.group(2))}/"
            ),
            value,
        )
    if isinstance(value, dict):
        return {k: _rewrite_refs(v, seg_map) for k, v in value.items()}
    if isinstance(value, list):
        return [_rewrite_refs(v, seg_map) for v in value]
    return value


def _next_free(stem: str, taken: set[str]) -> str:
    """First free ``f"{stem}{nn}"`` by a deterministic numeric walk (no RNG).

    Scans ``nn`` upward as 2-digit (01..99), then widens digit count (001..999, …)
    until a name absent from ``taken`` is found. Purely numeric and ordered, so the
    result is a pure function of ``(stem, taken)`` — no seeded draw.
    """
    width = 2
    n = 1
    while True:
        cand = f"{stem}{n:0{width}d}"
        if cand not in taken:
            return cand
        n += 1
        if n >= 10 ** width:
            width += 1
            n = 1


def _rebuild_name(
    old_name: str, template_token: str, new_token: str
) -> tuple[str, str]:
    """Rebuild ``old_name`` with ``new_token`` in place of ``template_token``.

    Returns ``(base_name, stem)`` where ``base_name`` keeps the original ``nn`` tail
    and ``stem`` is ``base_name`` minus that tail (for :func:`_next_free`). Primary
    path (D-16): ``rpartition`` on the KNOWN separator ``f"-{template_token}-"`` so a
    multi-hyphen token (``data-platform``) is never mis-split. Defensive fallback
    (separator absent): parse the ``rg-{bu}-{env}-{token}-{nn}`` grammar. Both are
    RNG-free (no re-mint through the seeded ``naming`` draws).
    """
    sep = f"-{template_token}-"
    prefix, found, tail = old_name.rpartition(sep)
    if found and tail.isdigit():
        stem = f"{prefix}-{new_token}-"
        return f"{stem}{tail}", stem
    m = _RG_NAME_RE.match(old_name)
    if m:
        pre, _tok, nn = m.groups()
        stem = f"{pre}-{new_token}-"
        return f"{stem}{nn}", stem
    # Last resort (never hit for a well-formed minted name): append the token.
    stem = f"{old_name}-{new_token}-"
    return f"{stem}01", stem


def _rename_rg(
    rg: "ResourceGroup",
    new_name: str,
    id_remap: dict[str, str],
    seg_map: dict[tuple[str, str], str],
) -> None:
    """Phase-A STRUCTURAL rename of one RG: rewrite rg.id, every child resource id
    and resource_group_name, and record the remap tables. Does NOT touch resource
    properties — a property may reference an RG not yet renamed, so the reference
    sweep is deferred to Phase B once ``seg_map`` is complete.

    ``seg_map`` is keyed on ``(str(subscription_id), old_name)``, never on the name
    alone: RG names are unique only within a subscription (T-19-14).
    """
    old_name = rg.name
    old_seg = f"/resourceGroups/{old_name}/"
    new_seg = f"/resourceGroups/{new_name}/"
    seg_map[(str(rg.subscription_id), old_name)] = new_name
    # The RG's own id ends with `/resourceGroups/{old}` (no trailing slash).
    own_tail = f"/resourceGroups/{old_name}"
    if rg.id.endswith(own_tail):
        rg.id = rg.id[: -len(old_name)] + new_name
    else:  # defensive — id built elsewhere; targeted replace of the RG segment
        rg.id = rg.id.replace(own_tail, f"/resourceGroups/{new_name}")
    rg.name = new_name
    for r in rg.resources:
        new_id = r.id.replace(old_seg, new_seg)
        id_remap[r.id] = new_id
        r.id = new_id
        r.resource_group_name = new_name


def _confirm_and_rename(
    tenant: "Tenant",
    label_map: dict[str, str],
    violation_rows: list[dict],
    dependency_rows: list[dict],
) -> dict[str, int]:
    """Reconcile every RG's NAME to its FINAL materialized CONTENTS (D-14/16/17).

    Two phases so cross-RG references are never rewritten against a half-built
    remap:

    - PHASE A: per-RG confirm (``archetypes.confirm_token_detail``) + structural
      rename (rg.id / child ids / resource_group_name), building ``seg_map``
      (old-name -> new-name) and ``id_remap`` (old-id -> new-id). The per-sub
      taken-name set is seeded from EVERY existing rg.name BEFORE any rename, so
      the deterministic suffix scan can never converge two RGs onto one name
      (TEXT PRIMARY KEY duplicate = COPY crash, T-19-04).
    - PHASE B: a SINGLE subscription-wide (whole-tenant superset) sweep applying
      ``_rewrite_refs`` to every resource's properties (cross-RG subnet/pip pool,
      T-19-03), then the id-remap to ``violation_rows`` (top-level resource_id AND
      the nested ``detail`` JSONB served verbatim) and ``dependency_rows``
      (source/target ids).

    Returns the D-18 metrics tally. RNG-free: the collision scan is a numeric walk,
    so the pass is a pure function of the index-ordered tenant + row lists.
    """
    metrics = {
        "confirmed": 0,
        "downgraded_to_generic": 0,
        "child_credit_confirmed": 0,
        "already_generic": 0,
    }
    seg_map: dict[tuple[str, str], str] = {}
    id_remap: dict[str, str] = {}

    # Seed the per-subscription taken-name set from EVERY existing name FIRST
    # (untouched AND to-be-renamed) — the collision-pressure fix (BLOCKER 3).
    taken: dict[Any, set[str]] = {}
    for rg in tenant.resource_groups:
        taken.setdefault(rg.subscription_id, set()).add(rg.name)

    # PHASE A — decide + structural rename (index-ordered, RNG-free).
    for rg in tenant.resource_groups:
        template_token = label_map.get(rg.template_type)
        if template_token is None:  # defensive — unknown template, leave as-is
            continue
        if template_token in (archetypes.TOKEN_SHARED, archetypes.TOKEN_CORE):
            metrics["already_generic"] += 1  # already generic — never re-promote
            continue
        materialized = {r.type for r in rg.resources}
        detail = archetypes.confirm_token_detail(template_token, materialized)
        if detail.token == template_token:  # confirmed — keep the semantic token
            metrics["confirmed"] += 1
            if detail.child_credit_decisive:
                metrics["child_credit_confirmed"] += 1
            continue
        # Downgrade to the honest generic token (D-14) — rename.
        metrics["downgraded_to_generic"] += 1
        base, stem = _rebuild_name(rg.name, template_token, detail.token)
        taken_set = taken[rg.subscription_id]
        taken_set.discard(rg.name)  # the old name is being vacated
        chosen = base if base not in taken_set else _next_free(stem, taken_set)
        taken_set.add(chosen)
        _rename_rg(rg, chosen, id_remap, seg_map)

    if not seg_map:  # nothing renamed — skip the sweep entirely (no-op fast path)
        return metrics

    # PHASE B — subscription-wide reference sweep (seg_map/id_remap now complete).
    for rg in tenant.resource_groups:
        for r in rg.resources:
            r.properties = _rewrite_refs(r.properties, seg_map)
            # T-19-13: `managed_by` is a SEPARATE field — outside both `id` (rewritten
            # in Phase A) and `properties` (swept above) — so it was the one parent
            # link a rename left dangling. Remap by id, not by segment: ids are
            # globally unique, so this stays correct across subscriptions.
            if r.managed_by:
                r.managed_by = id_remap.get(r.managed_by, r.managed_by)
    for row in violation_rows:
        if "resource_id" in row:
            row["resource_id"] = id_remap.get(row["resource_id"], row["resource_id"])
        if "detail" in row:
            row["detail"] = _rewrite_refs(row["detail"], seg_map)
    for row in dependency_rows:
        for key in ("source_resource_id", "target_resource_id"):
            if key in row:
                row[key] = id_remap.get(row[key], row[key])
    return metrics


def generate_tenant(
    profile: dict[str, Any],
    seed: int = 42,
    n_subs: int | None = None,
    n_resources: int | None = None,
    inject_violations: bool = True,
    inject_cross_sub: bool = True,
    inject_cost: bool = True,
    cost_granularity: str = "monthly",
    cost_as_of: _dt.date | None = None,
    inject_identity: bool = True,
    over_privilege_rate: float = 0.05,
    rates: dict[str, float] | None = None,
    targets: Any = None,
    jobs: int = 1,
    profile_name: str | None = None,
) -> GenerationResult:
    """Sample a full DB-free synthetic tenant from ``profile`` (GEN-01).

    ``n_subs`` / ``n_resources`` default from ``source_stats`` when omitted
    (D-05). Reproducible for a fixed ``(profile, seed, targets)`` (D-01).

    Phase 5 (D-01): two independent in-memory post-passes run AFTER calibrate +
    tagging, BEFORE COPY, both driven by the same ``ctx``/``seed``:

    - ``inject_violations`` (default on) → governance violation injection
      (VIOL-*); engine lands in Plan 05-02 (``generator.violations``).
    - ``inject_cross_sub`` (default on) → cross-subscription topology + the
      ``synthetic.dependencies`` rows (XSUB-*); engine lands in Plan 05-03
      (``generator.cross_sub``).

    Returns a frozen+slotted :class:`GenerationResult` with named fields
    ``tenant`` / ``violations`` / ``dependencies`` / ``clamp_notes`` (PLAT-01,
    D-01..D-03). The collection fields are tuples, so the snapshot's contents
    are immutable; it is constructed only AFTER both in-memory post-passes
    complete (D-02). When a pass is off — or until its engine module exists —
    its row tuple is empty and clamp_notes is empty, so the
    ``--no-violations --no-cross-sub`` baseline is byte-identical to the
    Phase-2 tenant at the same seed.
    """
    n_subs, n_resources = _default_targets(profile, n_subs, n_resources)

    # SPEED-02 substream tree (13-RESEARCH "Determinism Architecture"). The root
    # SeedSequence fans out into a FIXED, named set of children — the ORDER here
    # is the determinism contract, never data-dependent. tenant_id gets its own
    # dedicated stream (rule 2); each per-sub worker is keyed by INDEX (rule 1);
    # each tenant-wide post-pass gets its own named stream (rule 3). All Generator
    # / Faker construction stays inside rng.py via from_seed_sequence (the
    # grep-auditable seam) — pipeline.py only spawns SeedSequences here.
    root = SeedSequence(seed)
    (
        tenant_ss,
        subs_ss,
        calibrate_ss,
        tags_ss,
        viol_ss,
        xsub_ss,
        cost_ss,
        ident_ss,
    ) = root.spawn(8)

    tenant_ctx = SeededContext.from_seed_sequence(tenant_ss)

    # Local list renamed `sub_archetypes` to unshadow the imported `archetypes`
    # module (the confirm-and-rename pass below needs archetypes.confirm_token_detail
    # / build_label_map), mirroring the same unshadow in _build_one_subscription.
    sub_archetypes = profile["subscription_archetypes"]
    templates = profile["resource_group_templates"]
    rtd = profile.get("resource_type_distributions", {})
    tag_dists = profile.get("tag_distributions", {})
    by_arch = {a["id"]: a for a in sub_archetypes}

    tenant_id = tenant_ctx.uuid4()
    tenant = Tenant(
        tenant_id=tenant_id,
        display_name=f"{naming._word(tenant_ctx, naming._BUSINESS_UNITS)}-tenant",
        profile_version=str(profile.get("version", "1.0")),
        profile_name=profile_name,  # D-14: generation-profile IDENTITY (None when unspecified)
        scale_params={
            "seed": seed,
            "target_subscriptions": n_subs,
            "target_resources": n_resources,
        },
    )

    # One independent substream PER SUBSCRIPTION INDEX. Worker *i* consumes ONLY
    # per_sub_seeds[i], so the bytes it produces are a pure function of (seed, i),
    # independent of how many workers run or in what order they finish.
    per_sub_seeds = list(subs_ss.spawn(n_subs))

    # Per-subscription generation: in-process for jobs<=1 (the reference path), or
    # across a ProcessPoolExecutor for jobs>1. The merge is ALWAYS index-ordered
    # (ex.map preserves input order; an explicit sort-by-index is belt-and-
    # suspenders) so subscription/RG order — and thus every downstream post-pass —
    # is identical regardless of worker scheduling (never imap_unordered).
    results = _run_subscriptions(profile, tenant_id, per_sub_seeds, jobs)
    for _, sub, sub_rgs in results:
        tenant.subscriptions.append(sub)
        tenant.resource_groups.extend(sub_rgs)

    # Each worker deduped resource / RG ids within its OWN per-subscription set
    # (SAFE: both id namespaces embed subscription_id, so per-sub uniqueness is
    # tenant-wide uniqueness). Rebuild the tenant-wide resource-id set the minting
    # post-passes (calibrate padding, cross_sub host/PE resources) need.
    seen_ids: set[str] = {
        r.id for rg in tenant.resource_groups for r in rg.resources
    }

    # GEN-05: trim/pad the resource total to within ±5% of the target, preserving
    # the type/template mix and referential integrity (must run BEFORE tagging so
    # padded resources are tagged too). Drives its OWN named substream (rule 3) so
    # it is reproducible regardless of how the per-sub workers were scheduled; it
    # iterates RGs largest-first with id tiebreaks, a canonical order independent
    # of the merge.
    calibrate_ctx = SeededContext.from_seed_sequence(calibrate_ss)
    calibrate.calibrate(
        tenant, n_resources, calibrate_ctx, rtd, templates, seen_ids
    )

    # GEN-06: assign realistic per-resource tags (key frequency + value-shape,
    # capped by the resource's archetype tag_density) across the FINAL resource
    # set — including any companions/subnets/padded resources.
    # Own named substream (rule 3); iterates RGs/resources in the index-ordered
    # merge order, identical for jobs=1 and jobs=N.
    tags_ctx = SeededContext.from_seed_sequence(tags_ss)
    density_by_sub = {
        s.subscription_id: by_arch[s.archetype].get("tag_density")
        for s in tenant.subscriptions
    }
    # SPEED-01 (13-04): ONE batched Bernoulli matrix per RG for key presence +
    # ONE categorical per tag key tenant-wide for values, replacing the ~49.5M
    # scalar rng.bernoulli storm (13-01 measured tags = 77.9% cumulative). Runs
    # on the dedicated tags_ss substream so it stays jobs-1 == jobs-N identical.
    tags.assign_tags(tags_ctx, tenant, tag_dists, density_by_sub)

    # Phase 5 (D-01): two independent in-memory post-passes, BEFORE COPY. Each
    # drives its OWN named substream (rule 3: viol_ss / xsub_ss) instead of a
    # shared ctx, so neither output depends on the per-sub loop's draw count or
    # worker scheduling. Engine modules land in Plans 05-02 (violations) and 05-03
    # (cross_sub); until then the deferred imports degrade to a clean no-op so a
    # bare `generate` keeps producing the Phase-2 baseline. seen_ids stays live
    # (PK uniqueness for any minted host/consumer resource) and the live `rtd`
    # local is threaded so build_cross_sub can mint host/spoke/PE resources.
    violation_rows: list[dict] = []
    if inject_violations:
        try:
            from . import violations as _violations  # Plan 05-02
        except ImportError:
            _violations = None
        if _violations is not None:
            viol_ctx = SeededContext.from_seed_sequence(viol_ss)
            violation_rows = _violations.inject(
                viol_ctx, tenant, _violations.VIOLATION_REGISTRY, rates
            )

    dependency_rows: list[dict] = []
    clamp_notes: list[str] = []
    if inject_cross_sub:
        try:
            from . import cross_sub as _cross_sub  # Plan 05-03
        except ImportError:
            _cross_sub = None
        if _cross_sub is not None:
            xsub_ctx = SeededContext.from_seed_sequence(xsub_ss)
            dependency_rows, clamp_notes = _cross_sub.build_cross_sub(
                xsub_ctx,
                tenant,
                targets,
                seen_ids,
                resource_type_distributions=rtd,
            )

    # ARCH-GAP-01 (Plan 19-05, D-16): the confirm-and-rename gap-closure pass runs
    # HERE — immediately after cross_sub and BEFORE cost/identity. This is the
    # earliest point at which every rg.resources mutation is complete (per-sub
    # sample -> calibrate trim/pad -> cross_sub injection), so the confirmation gate
    # sees the FINAL materialized type set. cost/identity run AFTER, so they read
    # the CORRECTED names/ids (no remap needed for them). label_map is a pure
    # function of `templates` (byte-identical for jobs=1 and jobs=N); the pass draws
    # no RNG, so it never perturbs the SPEED-02 determinism contract.
    label_map = archetypes.build_label_map(templates)
    rg_naming_metrics = _confirm_and_rename(
        tenant, label_map, violation_rows, dependency_rows
    )

    # Phase 9 (D-01): the cost materialization post-pass runs AFTER violations and
    # cross_sub and BEFORE constructing GenerationResult, driving its OWN named
    # substream (rule 3: cost_ss). The deferred import + the empty-cost_distributions
    # no-op keep a cost-less profile (or --no-cost) byte-identical to the Phase-8
    # baseline (no RNG drawn).
    cost_rows: list[dict] = []
    if inject_cost:
        try:
            from . import cost as _cost  # Plan 09-03
        except ImportError:
            _cost = None
        if _cost is not None:
            cost_ctx = SeededContext.from_seed_sequence(cost_ss)
            cost_dists = profile.get("cost_distributions", {})
            # P1 fix: billing periods are derived EXCLUSIVELY from `cost_as_of`
            # (a calendar date), never an internal `date.today()` — so a fixed
            # (profile, seed, cost_as_of) is byte-reproducible across calendar days.
            if cost_granularity == "daily":
                cost_rows = _cost.inject_cost(
                    cost_ctx, tenant, cost_dists, granularity="daily", today=cost_as_of
                )
            else:
                cost_rows = _cost.inject_cost(
                    cost_ctx, tenant, cost_dists, granularity="monthly", today=cost_as_of
                )

    # Phase 10 (D-01): the identity post-pass runs AFTER cost and BEFORE constructing
    # GenerationResult, driving its OWN named substream (rule 3: ident_ss). Principals
    # are generated FIRST (the pool, Pitfall 1) so assign_roles can select real
    # principal_oids; the scope pool is the tenant's own subscriptions/RGs/resources.
    # Both draws share the one ident_ctx so the principal pool and the role draws
    # stay on a single ordered stream. The deferred import + the empty-pool no-op
    # keep an --no-identity generate byte-identical to the Phase-8/9 baseline (no
    # RNG drawn when inject_identity is off).
    principal_rows: list[dict] = []
    assignment_rows: list[dict] = []
    over_privilege_count = 0
    if inject_identity:
        try:
            from . import identity as _identity  # Plan 10-01
        except ImportError:
            _identity = None
        if _identity is not None:
            ident_ctx = SeededContext.from_seed_sequence(ident_ss)
            principal_rows = _identity.generate_principals(ident_ctx, tenant)
            assignment_rows, over_privilege_count = _identity.assign_roles(
                ident_ctx, tenant, principal_rows, over_privilege_rate=over_privilege_rate
            )

    return GenerationResult(
        tenant=tenant,
        violations=tuple(violation_rows),
        dependencies=tuple(dependency_rows),
        clamp_notes=tuple(clamp_notes),
        cost_records=tuple(cost_rows),
        principals=tuple(principal_rows),
        role_assignments=tuple(assignment_rows),
        over_privilege_count=over_privilege_count,
        rg_naming_metrics=rg_naming_metrics,
    )


def _generate_rg_resources(
    ctx: SeededContext,
    sub: "Subscription",
    rg: "ResourceGroup",
    template: dict[str, Any],
    rtd: dict[str, Any],
    seen_ids: set[str],
) -> list[resources.Resource]:
    """Generate the resource list for one RG following its template type mix.

    Resource count comes from the template's ``resource_count`` truncated-normal;
    the type mix is sampled per :func:`resources.sample_type_mix` (GEN-04). Nested
    types (e.g. ``Microsoft.Sql/servers/databases``) get their parent name from a
    same-RG parent of the parent type when one exists.
    """
    # TRUE-EMPTY reproduction (drawn FIRST so an empty RG consumes only this one
    # bernoulli): a template carrying ``empty_share`` — the ``__misc__`` privacy
    # bucket — makes that fraction of its RGs genuinely empty, reproducing the
    # source's real empty-RG rate instead of the all-empty generation artifact.
    empty_share = template.get("empty_share")
    if empty_share and ctx.bernoulli(float(empty_share)):
        return []

    rc = template.get("resource_count")
    if rc:
        mean, std = rc["mean"], rc["std"]
        # Heavy-tailed bucket (std > mean, e.g. __misc__): a symmetric trunc_normal
        # overshoots wildly (clamped negatives inflate the mean; the tail explodes),
        # forcing calibrate to trim so hard it empties the small RGs. A mean-
        # preserving lognormal keeps the total near target with a realistic right
        # skew. Low-variance standalone templates keep the unchanged normal path.
        if std > mean:
            count = max(1, ctx.trunc_lognormal(mean, std, rc["min"], rc["max"]))
        else:
            count = max(1, ctx.trunc_normal(mean, std, rc["min"], rc["max"]))
    else:
        count = 1

    # GEN-04 type resolution: real type_set → sample_type_mix (unchanged);
    # else the __misc__ type_weights histogram; else the global fallback — so a
    # privacy-folded template still carries resource-type mass and never empties.
    type_keys = resources.sample_rg_types(ctx, template, rtd, count)

    out: list[resources.Resource] = []
    # Track generated names per type so a nested child can embed a real parent.
    names_by_type: dict[str, list[str]] = {}
    # Generate parents before children so the parent-name pool is populated.
    for type_key in sorted(type_keys, key=lambda t: t.count("/")):
        parent_name = _parent_name_for(ctx, type_key, names_by_type)
        res = resources.generate_resource(
            ctx,
            subscription_id=sub.subscription_id,
            rg_name=rg.name,
            location=rg.location,
            type_key=type_key,
            resource_type_distributions=rtd,
            parent_name=parent_name,
            seen_ids=seen_ids,
        )
        out.append(res)
        names_by_type.setdefault(type_key, []).append(res.name)
    return out


def _parent_name_for(
    ctx: SeededContext, type_key: str, names_by_type: dict[str, list[str]]
) -> str | None:
    """Resolve a parent name for a nested type from same-RG parents, or None.

    ``Microsoft.Sql/servers/databases`` → a generated ``Microsoft.Sql/servers``
    name when one exists in this RG; otherwise the resource_id helper synthesizes
    a well-formed path (defensive padding), keeping scan fidelity.
    """
    if type_key.count("/") < 2:
        return None
    parent_type = type_key.rsplit("/", 1)[0]
    pool = names_by_type.get(parent_type)
    if pool:
        return ctx.choice(sorted(pool))
    return None


# --------------------------------------------------------------------------- #
# Per-subscription parallel orchestration (SPEED-02 / SPEED-01).
#
# The per-sub body is a MODULE-LEVEL worker so it is picklable under the Windows
# `spawn` start method (Pitfall 6). The read-only profile + tenant_id are shared
# via a process global set by the pool initializer (one copy per worker, never
# re-pickled per task); the per-task payload is only (index, spawned SeedSequence).
# --------------------------------------------------------------------------- #

# Worker-process globals, populated by _init_worker (in-process or via the pool
# initializer). The profile is read-only; never mutate it in a worker.
_WORKER_PROFILE: dict[str, Any] | None = None
_WORKER_TENANT_ID: uuid.UUID | None = None


def _init_worker(profile: dict[str, Any], tenant_id: uuid.UUID) -> None:
    """Set the read-only per-process worker state (pool initializer + in-process)."""
    global _WORKER_PROFILE, _WORKER_TENANT_ID
    _WORKER_PROFILE = profile
    _WORKER_TENANT_ID = tenant_id


def _pool_worker(
    i: int, ss: SeedSequence
) -> tuple[int, Subscription, list[ResourceGroup]]:
    """ProcessPoolExecutor task: read the per-process read-only state that the pool
    ``initializer`` (:func:`_init_worker`) set ONCE, then delegate to the pure
    builder. Module globals are touched ONLY inside worker processes — never in the
    parent — so concurrent in-process ``generate_tenant`` calls cannot clobber each
    other's state (the in-process path passes ``profile``/``tenant_id`` explicitly).
    """
    profile = _WORKER_PROFILE
    tenant_id = _WORKER_TENANT_ID
    assert profile is not None and tenant_id is not None  # set by _init_worker
    return _build_one_subscription(i, ss, profile, tenant_id)


def _build_one_subscription(
    i: int, ss: SeedSequence, profile: dict[str, Any], tenant_id: uuid.UUID
) -> tuple[int, Subscription, list[ResourceGroup]]:
    """Generate subscription ``i`` from its own index-keyed substream ``ss``.

    Pure: ``profile`` and ``tenant_id`` are passed in explicitly (never read from
    module globals), so the function is safe to call directly in-process and from
    multiple concurrent ``generate_tenant`` calls without cross-run contamination.
    Byte-identical regardless of which process runs it: every draw comes from
    ``SeededContext.from_seed_sequence(ss)``, a pure function of the spawned
    child for index ``i``. Uniqueness sets are per-worker — SAFE because both the
    resource-id and RG-id namespaces embed ``subscription_id`` (a name repeated
    across subscriptions yields a distinct full id), so per-sub dedup IS
    tenant-wide dedup. Returns ``(i, Subscription, [ResourceGroup])`` so the
    parent can merge in INDEX order (never completion order).
    """
    sub_archetypes = profile["subscription_archetypes"]
    templates = profile["resource_group_templates"]
    rtd = profile.get("resource_type_distributions", {})
    by_arch = {a["id"]: a for a in sub_archetypes}
    by_template = {t["id"]: t for t in templates}
    # ARCH-03: label each RG template's measured type_set to its archetype token.
    # Computed LOCALLY here (beside by_template) from the already-local `templates`
    # — NOT threaded across the ProcessPoolExecutor boundary. build_label_map is a
    # pure function of `templates`, so the jobs=1 and jobs=N paths compute a
    # byte-identical map and cannot diverge (determinism preserved).
    label_map = archetypes.build_label_map(templates)

    ctx = SeededContext.from_seed_sequence(ss)
    seen_ids: set[str] = set()
    seen_rg_ids: set[str] = set()

    archetype = sampling.sample_archetype(ctx, sub_archetypes)
    sub = Subscription(
        subscription_id=ctx.uuid4(),
        tenant_id=tenant_id,
        display_name=naming.subscription_name(ctx),
        archetype=archetype["id"],
    )

    sub_rgs: list[ResourceGroup] = []
    n_rgs = sampling.sample_rg_count(ctx, by_arch[sub.archetype])
    for _ in range(n_rgs):
        template = sampling.sample_template(ctx, templates)
        # ARCH-03: the workload token is the archetype label of this template's
        # measured contents (pure lookup — adds no RNG draw).
        token = label_map[template["id"]]
        location = sampling.sample_location(
            ctx, archetype.get("location_distribution", {})
        )
        rg_name = naming.resource_group_name(ctx, workload=token)
        rg_id = f"/subscriptions/{sub.subscription_id}/resourceGroups/{rg_name}"
        # Re-mint until the full rg_id is unique (PK safety). On the no-collision
        # path the while body never runs, so the draw order is unchanged.
        while rg_id in seen_rg_ids:
            rg_name = naming.resource_group_name(ctx, workload=token)
            rg_id = f"/subscriptions/{sub.subscription_id}/resourceGroups/{rg_name}"
        seen_rg_ids.add(rg_id)
        rg = ResourceGroup(
            id=rg_id,
            subscription_id=sub.subscription_id,
            name=rg_name,
            location=location,
            template_type=template["id"],
        )
        rg.resources = _generate_rg_resources(
            ctx, sub, rg, by_template.get(template["id"], template), rtd, seen_ids
        )
        sub_rgs.append(rg)

    # GEN-08: wire intra-resource references within this subscription scope.
    resources.wire_references(ctx, sub.subscription_id, sub_rgs, rtd, seen_ids)
    return i, sub, sub_rgs


def _run_subscriptions(
    profile: dict[str, Any],
    tenant_id: uuid.UUID,
    per_sub_seeds: list[SeedSequence],
    jobs: int,
) -> list[tuple[int, Subscription, list[ResourceGroup]]]:
    """Run every per-subscription worker and return results in INDEX order.

    ``jobs <= 1`` runs in-process (the reference path). ``jobs == 0`` means all
    cores. Otherwise a ``ProcessPoolExecutor`` fans the workers out; ``ex.map``
    preserves INPUT order, and an explicit sort-by-index is belt-and-suspenders —
    so the merge is identical no matter how workers are scheduled (the SPEED-02
    contract). ``imap_unordered`` / completion-order merges are never used.

    ``jobs`` is clamped to ``[1, cpu_count]`` HERE (not only at the CLI): a direct
    caller — a test, the scale benchmark's ``TENANTLESS_SCALE_JOBS`` env, or the
    public API — must never be able to spawn an unbounded process pool (Security
    V5 DoS-self, T-13-05-DOS). ``0`` resolves to all cores; values above the core
    count are pinned to the core count (oversubscription buys nothing for a
    CPU-bound generator); negatives collapse to the single-process path.
    """
    n_subs = len(per_sub_seeds)
    cpu = os.cpu_count() or 1
    # Resolve + clamp: 0 -> all cores; otherwise pin to [1, cpu]. This is the same
    # bound the CLI applies, enforced again at the engine so EVERY caller is safe.
    effective = cpu if jobs == 0 else max(1, min(jobs, cpu))

    if effective <= 1 or n_subs <= 1:
        # In-process reference path: pass profile/tenant_id EXPLICITLY. Never call
        # _init_worker here — mutating the module globals in the parent would let
        # two concurrent in-process generate_tenant calls clobber each other.
        return [
            _build_one_subscription(i, per_sub_seeds[i], profile, tenant_id)
            for i in range(n_subs)
        ]

    with ProcessPoolExecutor(
        max_workers=effective,
        initializer=_init_worker,
        initargs=(profile, tenant_id),
    ) as ex:
        # .map preserves INPUT order (no completion-order leak — Pitfall 2).
        # _pool_worker reads the per-process globals the initializer set.
        results = list(
            ex.map(
                _pool_worker,
                range(n_subs),
                per_sub_seeds,
                chunksize=8,
            )
        )
    results.sort(key=lambda t: t[0])  # belt-and-suspenders index sort
    return results
