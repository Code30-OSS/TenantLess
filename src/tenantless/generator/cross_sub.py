"""Cross-subscription topology engine (XSUB-01..06, D-03/D-04).

The structural sibling of :func:`resources.wire_references`, lifted from RG scope
to **subscription scope**: a two-pass "index every real id → ensure host
resources exist FIRST → mint spoke/consumer resources and build dependency rows
that point at real, already-created ids → validate every reference resolves
before COPY". After this pass a real ``generate`` produces a resolvable
cross-subscription dependency graph (hub-spoke peering + shared services) that
cross-subscription risk tooling can model.

D-04 LOCKED ordering (the load-bearing contract):

1. base tenant is already generated (the pipeline calls us at the very end);
2. seed-select host subscriptions deterministically (``ctx.choice`` over a
   sorted sub list — Pitfall 3);
3. ensure host RGs/resources (hub VNets, central Key Vaults, Log Analytics
   workspaces, ACRs, private endpoints) exist FIRST;
4. then mint spoke/consumer resources + build ``synthetic.dependencies`` rows
   whose source/target ids point at REAL already-created ids;
5. VALIDATE every dependency source AND target resolves to an actual generated
   resource BEFORE returning (the pre-COPY gate) — this is the executable form
   of XSUB-06 and is pinned by the planted-dangling-ref test.

D-03 (clamp + report): counts are absolute targets at REAL scale. On a small
profile every count is clamped to the available subs via :func:`_clamp` and each
clamp is recorded in ``clamp_notes`` — never silent, never fail. Counts come from
the spec defaults in :class:`Targets` (Pitfall 1 — NOT the degenerate
real-source xsub profile block).

All randomness flows through the injected :class:`~tenantless.generator.rng.SeededContext`
(no bare ``random``/``uuid.uuid4``) so the same ``(profile, seed, targets)`` yields
identical hosts, spokes, and dependency rows. DB-free: operates on the in-memory
:class:`~tenantless.generator.pipeline.Tenant` before any COPY.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import resources
from .rng import SeededContext

# Host / consumer anchor type keys (canonical casing; casing-robust minting since
# generate_resource degrades gracefully when a type has no profile distributions).
T_LOG_ANALYTICS = "Microsoft.OperationalInsights/workspaces"
T_ACR = "Microsoft.ContainerRegistry/registries"
T_PE = "Microsoft.Network/privateEndpoints"
T_STORAGE = resources.T_STORAGE
T_SQLSRV = resources.T_SQLSRV
T_KV = resources.T_KV
T_VNET = resources.T_VNET
T_AKS = resources.T_AKS

# LOCKED dependency_type vocabulary (CONTEXT D-06 / topology_spec).
DEP_VNET_PEERING = "vnet-peering"
DEP_SHARED_KV = "shared-keyvault"
DEP_LOG_ANALYTICS = "log-analytics"
DEP_PRIVATE_ENDPOINT = "private-endpoint"
DEP_SHARED_ACR = "shared-acr"


@dataclass(frozen=True)
class Targets:
    """Spec-range cross-sub topology targets at REAL scale (Pitfall 1).

    Each ``*_range`` is an inclusive ``(lo, hi)`` count target taken from the
    REQUIREMENTS spec defaults — NEVER profile-derived (the real-source xsub block
    is degenerate, e.g. hub_count mean=393). On small profiles :func:`_clamp`
    caps each draw to the available subscriptions and reports the clamp.
    """

    # XSUB-01 vnet-peering: 5-8 hub subs, each peered to 10-30 spokes.
    hub_count: tuple[int, int] = (5, 8)
    spokes_per_hub: tuple[int, int] = (10, 30)
    # XSUB-02 shared-keyvault: 3-5 central-KV subs referenced by 20-50 consumers.
    shared_kv_count: tuple[int, int] = (3, 5)
    kv_consumers: tuple[int, int] = (20, 50)
    # XSUB-03 log-analytics: 1-3 workspace subs referenced by min(100, eligible).
    log_analytics_count: tuple[int, int] = (1, 3)
    log_consumers_cap: int = 100
    # XSUB-05 shared-acr: 1-2 ACR subs referenced by AKS across subs.
    acr_count: tuple[int, int] = (1, 2)


def _clamp(
    requested_range: tuple[int, int],
    eligible: int,
    *,
    req_id: str,
    label: str,
) -> tuple[int, str | None]:
    """Clamp a requested count range to ``eligible`` subs; report when clamped.

    Draws the upper bound of the spec range as the absolute target at real scale,
    then ``n = min(requested, eligible)`` (D-03). When the request exceeds the
    eligible population a human-readable note naming ``req_id`` and the clamped
    value is returned (Pitfall 4 — never silent). Pure function of its inputs, so
    clamping is deterministic.
    """
    lo, hi = requested_range
    requested = max(0, int(hi))
    n = min(requested, max(0, int(eligible)))
    if n < requested:
        note = (
            f"{req_id} {label}: requested {lo}-{hi}, clamped to {n} "
            f"— only {eligible} eligible subs"
        )
        return n, note
    return n, None


def _clamp_count(
    requested: int,
    eligible: int,
    *,
    req_id: str,
    label: str,
) -> tuple[int, str | None]:
    """Clamp a single requested integer to ``eligible`` (XSUB-03 ``min(100, n)``)."""
    requested = max(0, int(requested))
    n = min(requested, max(0, int(eligible)))
    if n < requested:
        note = (
            f"{req_id} {label}: requested {requested}, clamped to {n} "
            f"— only {eligible} eligible subs"
        )
        return n, note
    return n, None


def _draw_in_range(ctx: SeededContext, rng_range: tuple[int, int], cap: int) -> int:
    """Seeded integer draw within ``rng_range`` (inclusive), capped at ``cap``."""
    lo, hi = rng_range
    lo = max(0, int(lo))
    hi = max(lo, int(hi))
    hi = min(hi, max(0, int(cap)))
    lo = min(lo, hi)
    if hi <= lo:
        return hi
    # rng.integers is half-open; +1 for an inclusive upper bound.
    return int(ctx.rng.integers(lo, hi + 1))


def _rgs_of_sub(tenant, sub_id) -> list[Any]:
    """Sorted list of RG objects belonging to ``sub_id`` (deterministic order)."""
    return sorted(
        (rg for rg in tenant.resource_groups if rg.subscription_id == sub_id),
        key=lambda rg: rg.name,
    )


def _first_of_type(rgs: list[Any], type_key: str):
    """First resource of ``type_key`` across ``rgs`` (deterministic by id)."""
    pool = sorted(
        (r for rg in rgs for r in rg.resources if r.type == type_key),
        key=lambda r: r.id,
    )
    return pool[0] if pool else None


def _ensure_anchor(
    ctx: SeededContext,
    tenant,
    sub_id,
    type_key: str,
    all_ids: set[str],
    rtd: dict[str, dict],
    seen_ids: set[str] | None,
):
    """Ensure ``sub_id`` owns a resource of ``type_key``; mint one FIRST if not.

    Mirrors :func:`resources.wire_references` ``ensure_pool`` discipline: a real
    resource is always preferable to a dangling reference (threat T-05-DANGLE).
    The minted resource is appended to an existing RG of the sub and its id is
    registered in BOTH ``seen_ids`` (PK uniqueness) and ``all_ids`` (the live
    reference pool).
    """
    rgs = _rgs_of_sub(tenant, sub_id)
    if not rgs:
        return None
    existing = _first_of_type(rgs, type_key)
    if existing is not None:
        return existing
    host_rg = rgs[0]
    minted = resources.generate_resource(
        ctx,
        subscription_id=sub_id,
        rg_name=host_rg.name,
        location=host_rg.location,
        type_key=type_key,
        resource_type_distributions=rtd,
        seen_ids=seen_ids,
    )
    host_rg.resources.append(minted)
    all_ids.add(minted.id)
    return minted


def _select_hosts(
    ctx: SeededContext,
    subs: list[Any],
    n: int,
) -> list[Any]:
    """Seed-select ``n`` DISTINCT host subs from a sorted ``subs`` list.

    Deterministic ``ctx.choice`` draws without replacement (a sub hosts a given
    service at most once); ``n`` is already clamped to ``len(subs)`` by the caller.
    """
    pool = list(subs)
    chosen: list[Any] = []
    for _ in range(min(n, len(pool))):
        pick = ctx.choice(pool)
        chosen.append(pick)
        pool.remove(pick)
    return chosen


def _dep_row(dep_type: str, src_id: str, tgt_id: str, src_sub, tgt_sub) -> dict:
    """The exact 5-key ``writer.copy_dependencies`` contract (writer.py:227-245)."""
    return {
        "dependency_type": dep_type,
        "source_resource_id": src_id,
        "target_resource_id": tgt_id,
        "source_subscription": src_sub,
        "target_subscription": tgt_sub,
    }


def build_cross_sub(
    ctx: SeededContext,
    tenant,
    targets: "Targets | dict | None" = None,
    seen_ids: set[str] | None = None,
    resource_type_distributions: dict[str, dict] | None = None,
) -> tuple[list[dict], list[str]]:
    """Build the cross-subscription dependency graph (XSUB-01..06, D-03/D-04).

    Returns ``(dependency_rows, clamp_notes)``. ``resource_type_distributions`` is
    the pipeline's ``rtd`` (threaded so :func:`resources.generate_resource` can
    mint host/spoke/PE resources); never reference an undefined ``rtd`` local.

    Task 1 implements the spine: D-04 steps 2-3 (host selection + host-first
    creation) and the D-03 clamp/report. Topology rows (step 4) and the step-5
    gate are filled by Tasks 2-3.
    """
    if targets is None:
        targets = Targets()
    rtd = resource_type_distributions or {}

    # Pass A — index every real id (the referenceable pool) and sort subs.
    all_ids = {r.id for rg in tenant.resource_groups for r in rg.resources}
    subs = sorted(tenant.subscriptions, key=lambda s: str(s.subscription_id))
    eligible = len(subs)

    clamp_notes: list[str] = []

    # D-04.2 seed-select host subs per topology (D-03 clamp + report).
    n_hubs, note = _clamp(
        targets.hub_count, eligible, req_id="XSUB-01", label="hubs"
    )
    if note:
        clamp_notes.append(note)
    n_kv, note = _clamp(
        targets.shared_kv_count, eligible, req_id="XSUB-02", label="shared-KV subs"
    )
    if note:
        clamp_notes.append(note)
    n_log, note = _clamp(
        targets.log_analytics_count,
        eligible,
        req_id="XSUB-03",
        label="log-analytics subs",
    )
    if note:
        clamp_notes.append(note)
    n_acr, note = _clamp(
        targets.acr_count, eligible, req_id="XSUB-05", label="ACR subs"
    )
    if note:
        clamp_notes.append(note)

    # XSUB-03 references min(100, eligible) consumer subs — clamp + report the
    # consumer fan-out here (the spine owns this count; Task 2 emits the rows).
    _n_log_consumers, note = _clamp_count(
        targets.log_consumers_cap,
        eligible,
        req_id="XSUB-03",
        label="log-analytics consumer subs",
    )
    if note:
        clamp_notes.append(note)

    hub_subs = _select_hosts(ctx, subs, n_hubs)
    kv_subs = _select_hosts(ctx, subs, n_kv)
    log_subs = _select_hosts(ctx, subs, n_log)
    acr_subs = _select_hosts(ctx, subs, n_acr)

    # D-04.3 ensure host resources exist FIRST (before any reference points at them).
    for hub in hub_subs:
        _ensure_anchor(
            ctx, tenant, hub.subscription_id, T_VNET, all_ids, rtd, seen_ids
        )
    for kv in kv_subs:
        _ensure_anchor(
            ctx, tenant, kv.subscription_id, T_KV, all_ids, rtd, seen_ids
        )
    for log in log_subs:
        _ensure_anchor(
            ctx,
            tenant,
            log.subscription_id,
            T_LOG_ANALYTICS,
            all_ids,
            rtd,
            seen_ids,
        )
    for acr in acr_subs:
        _ensure_anchor(
            ctx, tenant, acr.subscription_id, T_ACR, all_ids, rtd, seen_ids
        )

    rows: list[dict] = []

    # D-04.4 mint spoke/consumer resources + build dependency rows at REAL ids.
    rows += _build_vnet_peering(
        ctx, tenant, hub_subs, subs, targets, all_ids, rtd, seen_ids, clamp_notes
    )
    rows += _build_shared_keyvault(
        ctx, tenant, kv_subs, subs, targets, all_ids, clamp_notes
    )
    rows += _build_log_analytics(
        ctx, tenant, log_subs, subs, targets, all_ids, clamp_notes
    )
    rows += _build_private_endpoints(
        ctx, tenant, subs, all_ids, rtd, seen_ids
    )
    rows += _build_shared_acr(
        ctx, tenant, acr_subs, subs, all_ids, rtd, seen_ids
    )

    # D-04.5 PRE-COPY GATE — XSUB-06 by construction. Rebuild the live id set from
    # the CURRENT tenant (every resource minted during this pass) and assert every
    # row's source AND target resolves; a dangling ref raises before any COPY.
    current_ids = {r.id for rg in tenant.resource_groups for r in rg.resources}
    assert_references_resolve(rows, current_ids)

    return rows, clamp_notes


def assert_references_resolve(rows: list[dict], all_ids: set[str]) -> None:
    """The D-04 step-5 pre-COPY gate (executable XSUB-06; threat T-05-DANGLE).

    Raises :class:`ValueError` on the first dependency row whose
    ``source_resource_id`` OR ``target_resource_id`` is not in ``all_ids`` — a
    dangling reference must fail fast before the binary COPY, never reach Postgres.
    A clean build (every minted host/spoke/PE id registered) never raises.
    """
    for d in rows:
        if (
            d["source_resource_id"] not in all_ids
            or d["target_resource_id"] not in all_ids
        ):
            raise ValueError(f"dangling dependency (XSUB-06 gate): {d}")


def _other_subs(subs: list[Any], host_sub_ids: set) -> list[Any]:
    """Subs that are NOT in ``host_sub_ids`` (consumer candidates), sorted."""
    return [s for s in subs if s.subscription_id not in host_sub_ids]


def _build_vnet_peering(
    ctx, tenant, hub_subs, subs, targets, all_ids, rtd, seen_ids, clamp_notes
) -> list[dict]:
    """XSUB-01: one ``vnet-peering`` row per hub→spoke edge (A5 — one direction).

    source = hub VNet id, target = spoke VNet id. Spoke VNets are companion-minted
    when a selected spoke sub lacks one; minted companions are appended AFTER the
    selection loop (never mutate a list mid-iteration — RESEARCH Anti-pattern).
    """
    rows: list[dict] = []
    hub_ids = {h.subscription_id for h in hub_subs}
    for hub in hub_subs:
        hub_rgs = _rgs_of_sub(tenant, hub.subscription_id)
        hub_vnet = _first_of_type(hub_rgs, T_VNET)
        if hub_vnet is None:
            continue
        candidates = _other_subs(subs, hub_ids)
        n_spokes, note = _clamp_count(
            _draw_in_range(ctx, targets.spokes_per_hub, len(candidates)),
            len(candidates),
            req_id="XSUB-01",
            label=f"spokes for hub {hub.subscription_id}",
        )
        if note:
            clamp_notes.append(note)
        spoke_subs = _select_hosts(ctx, candidates, n_spokes)
        for spoke in spoke_subs:
            spoke_vnet = _ensure_anchor(
                ctx, tenant, spoke.subscription_id, T_VNET, all_ids, rtd, seen_ids
            )
            if spoke_vnet is None:
                continue
            rows.append(
                _dep_row(
                    DEP_VNET_PEERING,
                    hub_vnet.id,
                    spoke_vnet.id,
                    hub.subscription_id,
                    spoke.subscription_id,
                )
            )
    return rows


def _build_shared_keyvault(
    ctx, tenant, kv_subs, subs, targets, all_ids, clamp_notes
) -> list[dict]:
    """XSUB-02: 20-50 consumer subs each reference a central-KV sub's host KV.

    One ``shared-keyvault`` row per consumer→central-KV (source = a consumer
    resource id, target = the central KV id). The host KVs were created in the
    spine (D-04.3) so the target always resolves.
    """
    rows: list[dict] = []
    kv_ids = {k.subscription_id for k in kv_subs}
    central = []
    for kv in kv_subs:
        host_kv = _first_of_type(_rgs_of_sub(tenant, kv.subscription_id), T_KV)
        if host_kv is not None:
            central.append((kv, host_kv))
    if not central:
        return rows
    candidates = _other_subs(subs, kv_ids)
    n_consumers, note = _clamp_count(
        _draw_in_range(ctx, targets.kv_consumers, len(candidates)),
        len(candidates),
        req_id="XSUB-02",
        label="shared-KV consumer subs",
    )
    if note:
        clamp_notes.append(note)
    consumers = _select_hosts(ctx, candidates, n_consumers)
    for consumer in consumers:
        src = _consumer_source(tenant, consumer.subscription_id, all_ids)
        if src is None:
            continue
        kv_sub, host_kv = ctx.choice(sorted(central, key=lambda t: t[1].id))
        rows.append(
            _dep_row(
                DEP_SHARED_KV,
                src,
                host_kv.id,
                consumer.subscription_id,
                kv_sub.subscription_id,
            )
        )
    return rows


def _build_log_analytics(
    ctx, tenant, log_subs, subs, targets, all_ids, clamp_notes
) -> list[dict]:
    """XSUB-03: min(100, eligible) consumer subs reference a Log Analytics sub.

    One ``log-analytics`` row per consumer→workspace (source = a consumer resource
    id, target = the workspace id created in the spine).
    """
    rows: list[dict] = []
    log_ids = {l.subscription_id for l in log_subs}
    workspaces = []
    for log in log_subs:
        ws = _first_of_type(
            _rgs_of_sub(tenant, log.subscription_id), T_LOG_ANALYTICS
        )
        if ws is not None:
            workspaces.append((log, ws))
    if not workspaces:
        return rows
    candidates = _other_subs(subs, log_ids)
    n_consumers, note = _clamp_count(
        min(targets.log_consumers_cap, len(candidates)),
        len(candidates),
        req_id="XSUB-03",
        label="log-analytics consumer subs (min(100, eligible))",
    )
    if note:
        clamp_notes.append(note)
    consumers = _select_hosts(ctx, candidates, n_consumers)
    for consumer in consumers:
        src = _consumer_source(tenant, consumer.subscription_id, all_ids)
        if src is None:
            continue
        log_sub, ws = ctx.choice(sorted(workspaces, key=lambda t: t[1].id))
        rows.append(
            _dep_row(
                DEP_LOG_ANALYTICS,
                src,
                ws.id,
                consumer.subscription_id,
                log_sub.subscription_id,
            )
        )
    return rows


def _build_private_endpoints(
    ctx, tenant, subs, all_ids, rtd, seen_ids
) -> list[dict]:
    """XSUB-04: cross-sub ``private-endpoint`` rows for storage/SQL/KV targets.

    For each shared target type, pick a target resource in one sub and a DIFFERENT
    consumer sub, mint a minimal ``Microsoft.Network/privateEndpoints`` resource in
    the consumer (so source_resource_id resolves — RESEARCH Open Q3), and emit a
    row source = PE id, target = shared resource id.
    """
    rows: list[dict] = []
    for target_type in (T_STORAGE, T_SQLSRV, T_KV):
        # All resources of this type across the tenant, with their owning sub.
        pool = sorted(
            (
                (rg.subscription_id, r)
                for rg in tenant.resource_groups
                for r in rg.resources
                if r.type == target_type
            ),
            key=lambda t: t[1].id,
        )
        if not pool:
            continue
        target_sub_id, target_res = ctx.choice(pool)
        # A consumer sub different from the target's owner.
        consumer_candidates = [
            s for s in subs if s.subscription_id != target_sub_id
        ]
        if not consumer_candidates:
            continue
        consumer = ctx.choice(consumer_candidates)
        consumer_rgs = _rgs_of_sub(tenant, consumer.subscription_id)
        if not consumer_rgs:
            continue
        host_rg = consumer_rgs[0]
        pe = resources.generate_resource(
            ctx,
            subscription_id=consumer.subscription_id,
            rg_name=host_rg.name,
            location=host_rg.location,
            type_key=T_PE,
            resource_type_distributions=rtd,
            seen_ids=seen_ids,
        )
        host_rg.resources.append(pe)
        all_ids.add(pe.id)
        rows.append(
            _dep_row(
                DEP_PRIVATE_ENDPOINT,
                pe.id,
                target_res.id,
                consumer.subscription_id,
                target_sub_id,
            )
        )
    return rows


def _build_shared_acr(
    ctx, tenant, acr_subs, subs, all_ids, rtd, seen_ids
) -> list[dict]:
    """XSUB-05: AKS clusters across subs reference a shared ACR sub's registry.

    One ``shared-acr`` row per AKS→ACR (source = an AKS cluster id, target = the
    ACR id created in the spine). If no AKS exists anywhere, no rows are emitted.
    """
    rows: list[dict] = []
    acr_ids = {a.subscription_id for a in acr_subs}
    registries = []
    for acr in acr_subs:
        reg = _first_of_type(_rgs_of_sub(tenant, acr.subscription_id), T_ACR)
        if reg is not None:
            registries.append((acr, reg))
    if not registries:
        return rows
    # Every AKS cluster NOT in an ACR-host sub references a (deterministic) ACR.
    aks_pool = sorted(
        (
            (rg.subscription_id, r)
            for rg in tenant.resource_groups
            for r in rg.resources
            if r.type == T_AKS and rg.subscription_id not in acr_ids
        ),
        key=lambda t: t[1].id,
    )
    for aks_sub_id, aks in aks_pool:
        acr_sub, reg = ctx.choice(sorted(registries, key=lambda t: t[1].id))
        rows.append(
            _dep_row(
                DEP_SHARED_ACR,
                aks.id,
                reg.id,
                aks_sub_id,
                acr_sub.subscription_id,
            )
        )
    return rows


def _consumer_source(tenant, sub_id, all_ids: set[str]) -> str | None:
    """A deterministic real resource id owned by ``sub_id`` (the dep source).

    Any resource the consumer sub owns is a valid source for a shared-service
    reference; we pick the lexicographically-first owned id so the choice is
    reproducible and always resolves (it is already in ``all_ids``).
    """
    owned = sorted(
        r.id
        for rg in tenant.resource_groups
        if rg.subscription_id == sub_id
        for r in rg.resources
    )
    for rid in owned:
        if rid in all_ids:
            return rid
    return owned[0] if owned else None
