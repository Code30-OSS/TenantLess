"""Deterministic one-per-policy drift injector (Phase 55, D-12).

The statistical violation engine (:mod:`tenantless.generator.violations`) samples
governance drift *probabilistically* over an eligible population. It structurally
CANNOT express the last-mile drift the promotion-readiness validation bed needs:
policies that require a *specific* resource shape, a *relational-existence* gap, or
a *topology-anchored* placement. This module is the deterministic post-generation
companion — a seed-pinned pass that places EXACTLY ONE violating resource per
targeted policy the statistical engine cannot reach:

    (a) agw-nsg cluster (c01 / NS, relational-existence) — N NSGs named
        ``*-agw-nsg`` in the hub VNet's RG, M of them WITH a sibling
        ``Microsoft.Authorization/locks`` named ``<nsg>-lock`` in the same RG,
        and N-M WITHOUT — the governance engine's ``is_agw_nsg`` + lock-satisfier hero finding.
    (b) custom role with authorization actions (p17 / IM) — a
        ``Microsoft.Authorization/roleDefinitions`` resource whose
        ``properties.permissions[].actions`` contain an authorization-scoped
        action. OQ-1 PLACEHOLDER: the exact action pattern the real
        azlz-policies-n-roles p17 matcher expects is reconciled when that repo is
        registered in-flow (D-11); ``Microsoft.Authorization/*`` is the documented
        placeholder.
    (c) wrong-region-on-hub (r06 / DP override->high, +centrality->critical) — one
        resource in a DISALLOWED region attached to the hub VNet's RG. Touching the
        hub is the designed centrality-escalation showcase.
    (d) off-naming (canonical 4) — one resource with a non-conforming name.
    (e) rogue SKU (canonical 4) — one resource carrying a rogue SKU value.

Runs AFTER :func:`tenantless.generator.pipeline.generate_tenant` and BEFORE
``writer.write_tenant``: it mutates the in-memory ``Tenant`` in place (appends the
injected :class:`~tenantless.generator.resources.Resource` objects to the hub RG)
and RETURNS the ``synthetic.violations`` rows to merge with
``GenerationResult.violations`` — the same ``{resource_id, violation_type,
severity, detail}`` contract the statistical engine emits (writer.copy_violations).

Determinism (D-14): every draw flows through an injected/constructed
:class:`~tenantless.generator.rng.SeededContext`; the injected resource NAMES are a
pure function of the pinned ``seed`` and the hub RG, so the same
``(tenant, seed)`` yields byte-identical injected resources and rows. Fails LOUD
(``RuntimeError``) when a required target population is absent — never a silent
skip (CLAUDE.md: no silent skips).
"""

from __future__ import annotations

from typing import Any

from . import arm, resources
from .rng import SeededContext

# --------------------------------------------------------------------------- #
# Type + value constants.
# --------------------------------------------------------------------------- #

# Authorization-plane types the statistical generator never mints.
T_ROLE_DEFINITION = "Microsoft.Authorization/roleDefinitions"
T_LOCK = "Microsoft.Authorization/locks"

# A region intentionally OUTSIDE every archetype's location_distribution in
# profiles/promotion-demo.json (a data-residency / r06 violation by construction).
WRONG_REGION = "brazilsouth"

# A SKU value that is not in any allowed sku_distribution catalogue (rogue SKU,
# canonical-4). Deliberately non-catalogue so a SKU-allow-list policy flags it.
ROGUE_SKU_NAME = "Standard_ROGUE_ZZZ9"

# A resource name that violates conventional Azure naming (uppercase, underscores,
# spaces, and a trailing bang — none of which a naming policy permits).
OFF_NAMING_NAME = "ROGUE_Bad Name!!"

# OQ-1 placeholder: the authorization-scoped action the custom role grants. The
# real azlz p17 matcher's exact pattern is confirmed when that repo is registered
# in-flow (D-11); this is the documented stand-in (see module docstring).
CUSTOM_ROLE_ACTION = "Microsoft.Authorization/*"

# Pinned default seed for the drift pass (D-14, "repeatable via sim restart").
DRIFT_SEED = 55003

# agw-nsg cluster split: N total, M locked, N-M unlocked (the c01 violations).
AGW_NSG_TOTAL = 5
AGW_NSG_LOCKED = 2

# vnet-peering dependency_type (mirrors cross_sub.DEP_VNET_PEERING) — the anchor
# used to locate a hub VNet from the generated dependency rows.
_DEP_VNET_PEERING = "vnet-peering"


def _all_resources(tenant) -> list:
    return [r for rg in tenant.resource_groups for r in rg.resources]


def _find_hub_anchor(tenant, dependencies) -> tuple[Any, Any]:
    """Return ``(hub_rg, hub_vnet)`` — the RG object hosting a hub VNet.

    A hub VNet is the ``source_resource_id`` of a ``vnet-peering`` dependency row
    (cross_sub emits one row per hub->spoke edge). We pick the lowest such id for
    determinism. When no peering rows exist (cross-sub disabled), fall back to the
    first VNet by sorted id. Fails LOUD when the tenant has no resource groups
    (no target population) — never a silent skip.
    """
    if not tenant.resource_groups:
        raise RuntimeError(
            "drift_injector: tenant has no resource groups — no target population "
            "for hub-anchored drift (generate the promotion-demo profile first)."
        )

    hub_vnet_ids = sorted(
        d["source_resource_id"]
        for d in (dependencies or [])
        if d.get("dependency_type") == _DEP_VNET_PEERING
    )
    by_id = {r.id: r for r in _all_resources(tenant)}

    hub_vnet = None
    for vid in hub_vnet_ids:
        if vid in by_id:
            hub_vnet = by_id[vid]
            break

    if hub_vnet is None:
        vnets = sorted(
            (r for r in _all_resources(tenant) if r.type == resources.T_VNET),
            key=lambda r: r.id,
        )
        hub_vnet = vnets[0] if vnets else None

    if hub_vnet is None:
        # No VNet at all — anchor on the first RG by id (still a real, resolvable
        # RG) and synthesize the hub topology anchor there. Loud note in detail.
        hub_rg = sorted(tenant.resource_groups, key=lambda rg: rg.id)[0]
        return hub_rg, None

    hub_rg = next(
        (
            rg
            for rg in tenant.resource_groups
            if rg.subscription_id == hub_vnet.subscription_id
            and rg.name == hub_vnet.resource_group_name
        ),
        None,
    )
    if hub_rg is None:
        hub_rg = sorted(tenant.resource_groups, key=lambda rg: rg.id)[0]
    return hub_rg, hub_vnet


def _mk_resource(
    rg,
    *,
    name: str,
    type_key: str,
    location: str,
    seen_ids: set[str],
    properties: dict[str, Any] | None = None,
    sku: dict[str, Any] | None = None,
    parent_name: str | None = None,
) -> resources.Resource:
    """Build a :class:`Resource` in ``rg`` with a unique ARM id (PK-safe)."""
    rid = arm.resource_id(
        rg.subscription_id, rg.name, type_key, name, parent_name=parent_name
    )
    if rid in seen_ids:
        raise RuntimeError(
            f"drift_injector: id collision for {rid} — injector is not idempotent "
            "against an already-injected tenant; run it once on a fresh generate."
        )
    seen_ids.add(rid)
    return resources.Resource(
        id=rid,
        subscription_id=rg.subscription_id,
        resource_group_name=rg.name,
        name=name,
        type=type_key,
        location=location,
        api_version=arm.api_version_for(type_key),
        properties=properties or {},
        sku=sku,
    )


def _inject_agw_nsg_cluster(
    ctx: SeededContext,
    hub_rg,
    seen_ids: set[str],
    *,
    total: int,
    locked: int,
) -> list[dict]:
    """Place an ``*-agw-nsg`` cluster: ``total`` NSGs, ``locked`` with a lock.

    Each NSG is ``Microsoft.Network/networkSecurityGroups`` named
    ``<base>-agw-nsg`` (matches the governance rule ``is_agw_nsg``: type + name-contains
    ``-agw-nsg``). ``locked`` of them get a sibling ``Microsoft.Authorization/locks``
    named ``<nsg-name>-lock`` in the SAME RG (the lock-satisfier shape). The
    remaining ``total - locked`` are the c01 relational-existence VIOLATIONS —
    one violation row each. Fails LOUD on an impossible split.
    """
    if total < 1 or not (0 <= locked <= total):
        raise RuntimeError(
            f"drift_injector: invalid agw-nsg split total={total} locked={locked}"
        )
    base = ctx.faker.lexify("agw????").lower()
    rows: list[dict] = []
    for i in range(total):
        nsg_name = f"{base}-{i:02d}-agw-nsg"
        nsg = _mk_resource(
            hub_rg,
            name=nsg_name,
            type_key=resources.T_NSG,
            location=hub_rg.location,
            seen_ids=seen_ids,
            properties={"securityRules": []},
        )
        hub_rg.resources.append(nsg)
        if i < locked:
            lock_name = f"{nsg_name}-lock"
            lock = _mk_resource(
                hub_rg,
                name=lock_name,
                type_key=T_LOCK,
                location=hub_rg.location,
                seen_ids=seen_ids,
                properties={"level": "CanNotDelete", "scope": nsg.id},
            )
            hub_rg.resources.append(lock)
        else:
            rows.append(
                {
                    "resource_id": nsg.id,
                    "violation_type": "AGW_NSG_MISSING_LOCK",
                    "severity": "High",
                    "detail": {
                        "policy": "c01",
                        "family": "NS",
                        "rationale": "agw-nsg has no CanNotDelete lock sibling",
                        "expected_lock_name": f"{nsg_name}-lock",
                        "expected_lock_type": T_LOCK,
                        "topology_anchor": "hub-vnet-rg",
                    },
                }
            )
    return rows


def _inject_custom_role(ctx: SeededContext, hub_rg, seen_ids: set[str]) -> dict:
    """Place a custom roleDefinition with authorization actions (p17 / IM)."""
    name = f"customrole-{ctx.faker.lexify('????').lower()}-authz"
    role = _mk_resource(
        hub_rg,
        name=name,
        type_key=T_ROLE_DEFINITION,
        location=hub_rg.location,
        seen_ids=seen_ids,
        properties={
            "roleName": "promotion-demo-custom-authz",
            "type": "CustomRole",
            "permissions": [
                {
                    "actions": [CUSTOM_ROLE_ACTION, "*/read"],
                    "notActions": [],
                    "dataActions": [],
                    "notDataActions": [],
                }
            ],
            "assignableScopes": [f"/subscriptions/{hub_rg.subscription_id}"],
        },
    )
    hub_rg.resources.append(role)
    return {
        "resource_id": role.id,
        "violation_type": "CUSTOM_ROLE_AUTH_ACTIONS",
        "severity": "High",
        "detail": {
            "policy": "p17",
            "family": "IM",
            "rationale": "custom role grants authorization-plane actions",
            "action_pattern": CUSTOM_ROLE_ACTION,
            "placeholder": "OQ-1 — reconcile against real azlz p17 matcher in-flow",
            "topology_anchor": "hub-vnet-rg",
        },
    }


def _inject_wrong_region_on_hub(
    ctx: SeededContext, hub_rg, seen_ids: set[str]
) -> dict:
    """Place one resource in a disallowed region on the hub RG (r06 + centrality)."""
    name = f"stwrongregion{ctx.faker.lexify('?????').lower()}"
    res = _mk_resource(
        hub_rg,
        name=name,
        type_key=resources.T_STORAGE,
        location=WRONG_REGION,
        seen_ids=seen_ids,
        properties={
            "accessTier": "Hot",
            "supportsHttpsTrafficOnly": True,
            "minimumTlsVersion": "TLS1_2",
            "allowBlobPublicAccess": False,
        },
        sku={"name": "Standard_LRS", "tier": "Standard"},
    )
    hub_rg.resources.append(res)
    return {
        "resource_id": res.id,
        "violation_type": "WRONG_REGION_ON_HUB",
        "severity": "High",
        "detail": {
            "policy": "r06",
            "family": "DP",
            "rationale": "resource deployed to a disallowed region on the hub RG",
            "region": WRONG_REGION,
            "escalation": "centrality (+1) -> critical because it touches the hub",
            "topology_anchor": "hub-vnet-rg",
        },
    }


def _inject_off_naming(ctx: SeededContext, hub_rg, seen_ids: set[str]) -> dict:
    """Place one off-named resource (canonical 4)."""
    res = _mk_resource(
        hub_rg,
        name=OFF_NAMING_NAME,
        type_key=resources.T_STORAGE,
        location=hub_rg.location,
        seen_ids=seen_ids,
        properties={
            "accessTier": "Hot",
            "supportsHttpsTrafficOnly": True,
            "minimumTlsVersion": "TLS1_2",
            "allowBlobPublicAccess": False,
        },
        sku={"name": "Standard_LRS", "tier": "Standard"},
    )
    hub_rg.resources.append(res)
    return {
        "resource_id": res.id,
        "violation_type": "OFF_NAMING",
        "severity": "Low",
        "detail": {
            "policy": "p16",
            "family": "AM",
            "rationale": "resource name violates the naming convention",
            "name": OFF_NAMING_NAME,
            "placeholder": "AM asset-naming — confirm exact code against azlz in-flow",
            "topology_anchor": "hub-vnet-rg",
        },
    }


def _inject_rogue_sku(ctx: SeededContext, hub_rg, seen_ids: set[str]) -> dict:
    """Place one resource carrying a rogue SKU (canonical 4)."""
    name = f"disk-roguesku-{ctx.faker.lexify('????').lower()}"
    res = _mk_resource(
        hub_rg,
        name=name,
        type_key=resources.T_DISK,
        location=hub_rg.location,
        seen_ids=seen_ids,
        properties={"diskSizeGB": 128},
        sku={"name": ROGUE_SKU_NAME},
    )
    hub_rg.resources.append(res)
    return {
        "resource_id": res.id,
        "violation_type": "ROGUE_SKU",
        "severity": "Low",
        "detail": {
            "policy": "p16",
            "family": "AM",
            "rationale": "resource carries a SKU outside the allowed catalogue",
            "sku": ROGUE_SKU_NAME,
            "placeholder": "AM asset-SKU governance — confirm code against azlz in-flow",
            "topology_anchor": "hub-vnet-rg",
        },
    }


def inject_drift(
    tenant,
    dependencies: list[dict] | tuple[dict, ...] = (),
    *,
    seed: int = DRIFT_SEED,
    agw_nsg_total: int = AGW_NSG_TOTAL,
    agw_nsg_locked: int = AGW_NSG_LOCKED,
    seen_ids: set[str] | None = None,
) -> list[dict]:
    """Inject the deterministic one-per-policy drift into ``tenant`` in place.

    Locates the hub VNet's RG (from the ``vnet-peering`` dependency rows, with a
    VNet/first-RG fallback), then places the five un-expressible drift items there.
    Returns the ``synthetic.violations`` rows to merge with the statistical pass.
    Seed-pinned: the same ``(tenant, seed)`` yields byte-identical injected
    resources and rows (D-14). Fails LOUD when there is no target population.

    ``agw_nsg_total`` / ``agw_nsg_locked`` control the N/M split of the agw-nsg
    cluster (default 5/2 -> 3 unlocked c01 violations).
    """
    ctx = SeededContext(seed)
    if seen_ids is None:
        seen_ids = {r.id for r in _all_resources(tenant)}

    hub_rg, _hub_vnet = _find_hub_anchor(tenant, dependencies)

    rows: list[dict] = []
    rows.extend(
        _inject_agw_nsg_cluster(
            ctx, hub_rg, seen_ids, total=agw_nsg_total, locked=agw_nsg_locked
        )
    )
    rows.append(_inject_custom_role(ctx, hub_rg, seen_ids))
    rows.append(_inject_wrong_region_on_hub(ctx, hub_rg, seen_ids))
    rows.append(_inject_off_naming(ctx, hub_rg, seen_ids))
    rows.append(_inject_rogue_sku(ctx, hub_rg, seen_ids))
    return rows
