"""Seeded identity generation post-pass (Plan 10-01, IAM-01/IAM-02, D-01..D-07).

The FOURTH instance of the Phase-5 in-memory post-pass idiom (twin of
``violations.inject`` / ``cross_sub.build_cross_sub`` / ``cost.inject_cost``): an
in-memory pass over the built ``Tenant`` from the same ``SeededContext`` that
mints synthetic principals (users / groups / service principals as ARM-opaque
GUIDs — IAM-01/D-01) and draws ``role_assignments`` that reference those
principals, the built-in roleDefinition catalogue, and the tenant's REAL scopes
(subscription / RG / resource ids — IAM-02/D-04/D-07). A SECOND configurable pass
injects over-privilege patterns (Owner-at-subscription, ServicePrincipal-granted
-Owner — D-05), the IAM analogue of the Phase-5 violation injector.

Identity is SYNTHETIC, not fit-from-seed (D-01): the seed (an ARG *resource* scan)
carries no identity/RBAC data, so there is nothing to fit and there is NO analyzer
emission path — hence no profile-leak surface (Pitfall 5 / the privacy reviewer's
sign-off). Principals stay ARM-opaque GUIDs; ``display_name`` is left ``None`` so
no real-identifier-shaped string is ever minted.

Determinism (D-01 / T-10-03 / Pitfall 6): EVERY draw goes through the injected
``SeededContext`` (``ctx.rng`` / ``ctx.choice`` / ``ctx.bernoulli`` / ``ctx.uuid4``)
and every iterated population is SORTED by id before any draw — never a
process-global RNG, never a freshly-constructed Faker instance, never a
wall-clock value. Token expiry / issued-at / signing keys are SERVER-mint only
and never touch this generator (Pitfall 6). The same
``(tenant, seed, over_privilege_rate)`` therefore yields byte-identical
principals + role_assignments, and the module's sole import is the seeded RNG
(no wall-clock or name-faking module is imported here at all).

Cross-language constant (Pitfall 3): :data:`BUILTIN_ROLE_DEFINITIONS` is the
canonical built-in roleDefinition GUID+roleName list, copied VERBATIM into the
Rust ``handlers/authorization.rs`` catalogue (Plan 10-03) — exactly like
``serve.py``'s ``BINARY_NAME``. Owner / Contributor / Reader are VERIFIED Azure
constants; the rest provide the specialized over-privilege signal. Every
``role_definition_id`` this module draws is one of these GUIDs, tenant-scoped.
"""

from __future__ import annotations

from .rng import SeededContext

# Built-in roleDefinition GUID + roleName catalogue (RESEARCH Q3). CROSS-LANGUAGE
# CONSTANT (Pitfall 3): byte-identical to the Rust authorization.rs catalogue in
# Plan 10-03. Owner/Contributor/Reader are VERIFIED; the rest give the specialized
# over-privilege signal (e.g. User Access Administrator = "can grant access").
BUILTIN_ROLE_DEFINITIONS: tuple[tuple[str, str], ...] = (
    ("8e3af657-bb00-4899-acbc-f0f7f5db61aa", "Owner"),
    ("b24988ac-6180-42a0-ab88-20f7382dd24c", "Contributor"),
    ("acdd72a7-3385-48ef-bd42-f606fba81ae7", "Reader"),
    ("18d7d88d-d35e-4fb5-a5c3-7773c20a72d9", "User Access Administrator"),
    ("ba92f5b4-2d11-453d-a403-e96b0029c9fe", "Storage Blob Data Contributor"),
    ("4633458b-17de-408a-b874-0445c86b69e6", "Key Vault Secrets User"),
    ("9980e02c-c2be-4d73-94e8-173b1dc7cf3c", "Virtual Machine Contributor"),
    ("4d97b98b-1d4f-4787-a291-c67834d212e7", "Network Contributor"),
)

# roleDefinitionId is stored TENANT-scoped (RESEARCH Q3): no /subscriptions/{sub}
# prefix — the assignment references the canonical (unscoped) roleDefinition id.
ROLE_DEF_ID_PREFIX = "/providers/Microsoft.Authorization/roleDefinitions/"

# roleName → GUID (the catalogue, indexed for the assignment draw).
GUID_BY_ROLE: dict[str, str] = {name: guid for guid, name in BUILTIN_ROLE_DEFINITIONS}

# Principal type mix (D-02): ~70 % User / 15 % Group / 15 % ServicePrincipal.
PRINCIPAL_TYPES: tuple[str, ...] = ("User", "Group", "ServicePrincipal")
_PRINCIPAL_TYPE_WEIGHTS: tuple[float, ...] = (0.70, 0.15, 0.15)

# Principal population formula (D-02): scales with tenant size — a per-subscription
# identity footprint plus one principal per ~8 resources. The exact constants are
# Claude's discretion (the decision is "scales with the tenant," not a fixed cast).
_PRINCIPALS_PER_SUB = 2
_RESOURCES_PER_PRINCIPAL = 8

# Baseline role mix (D-04): Reader-heavy, fewer Contributors, few Owners, plus a few
# specialized built-ins. Weights are renormalized by ``ctx.choice`` before the draw.
_BASELINE_ROLE_WEIGHTS: dict[str, float] = {
    "Reader": 55.0,
    "Contributor": 20.0,
    "Owner": 5.0,
    "Storage Blob Data Contributor": 5.0,
    "Key Vault Secrets User": 5.0,
    "Virtual Machine Contributor": 4.0,
    "Network Contributor": 4.0,
    "User Access Administrator": 2.0,
}

# Scope-tier mix (D-04): weighted toward RG / resource with some subscription scope.
_SCOPE_TIER_WEIGHTS = {"subscription": 0.15, "resourceGroup": 0.35, "resource": 0.50}


def _role_definition_id(role_name: str) -> str:
    """Tenant-scoped roleDefinition id string for a catalogue role name."""
    return f"{ROLE_DEF_ID_PREFIX}{GUID_BY_ROLE[role_name]}"


def _principal_count(n_subs: int, n_resources: int) -> int:
    """Deterministic principal-population size from tenant scale (D-02).

    ``_PRINCIPALS_PER_SUB * n_subs + n_resources // _RESOURCES_PER_PRINCIPAL`` —
    monotonic in BOTH the subscription and the resource count, so a larger tenant
    always yields more principals (the ``test_principal_count_scales`` contract).
    """
    return _PRINCIPALS_PER_SUB * n_subs + n_resources // _RESOURCES_PER_PRINCIPAL


def generate_principals(ctx: SeededContext, tenant) -> list[dict]:
    """Mint the synthetic principal directory for ``tenant`` (IAM-01/D-01/D-02).

    The count derives from tenant scale (:func:`_principal_count`); each principal
    is an ARM-opaque GUID ``oid`` (``ctx.uuid4``) with a ~70/15/15 ``principal_type``
    (``ctx.choice`` over the fixed :data:`PRINCIPAL_TYPES` weight vector). A
    ServicePrincipal additionally carries an ``app_id`` (``ctx.uuid4``); User/Group
    leave it ``None``. ``display_name`` is always ``None`` — principals stay
    ARM-opaque, so NO real-identifier-shaped string is minted (Pitfall 5). Returns
    ``list[dict]`` rows ``{oid, principal_type, display_name, app_id}``.

    All randomness flows through the injected ``ctx`` (the draw order is the fixed
    ``range(count)`` sequence), so a fixed ``(tenant, seed)`` is byte-reproducible.
    """
    n_subs = len(tenant.subscriptions)
    n_resources = sum(len(rg.resources) for rg in tenant.resource_groups)
    count = _principal_count(n_subs, n_resources)

    types = list(PRINCIPAL_TYPES)
    weights = list(_PRINCIPAL_TYPE_WEIGHTS)

    rows: list[dict] = []
    for _ in range(count):
        oid = ctx.uuid4()
        principal_type = ctx.choice(types, weights)
        app_id = ctx.uuid4() if principal_type == "ServicePrincipal" else None
        rows.append(
            {
                "oid": oid,
                "principal_type": principal_type,
                "display_name": None,  # ARM-opaque (IAM-01 / Pitfall 5)
                "app_id": app_id,
            }
        )
    return rows


def _scope_pools(tenant) -> dict[str, list[tuple[str, object]]]:
    """The three REAL scope pools (Pitfall 1: select, never invent).

    Each entry is ``(scope_id, subscription_id)`` so the assignment's
    ``subscription_id`` column always names the owning subscription. Every pool is
    SORTED by scope id for deterministic draw order (the determinism contract).
    """
    subscription = sorted(
        (f"/subscriptions/{s.subscription_id}", s.subscription_id)
        for s in tenant.subscriptions
    )
    resource_group = sorted(
        (rg.id, rg.subscription_id) for rg in tenant.resource_groups
    )
    resource = sorted(
        (r.id, r.subscription_id)
        for rg in tenant.resource_groups
        for r in rg.resources
    )
    return {
        "subscription": subscription,
        "resourceGroup": resource_group,
        "resource": resource,
    }


def _draw_scope(
    ctx: SeededContext, pools: dict[str, list[tuple[str, object]]]
) -> tuple[str, object]:
    """Draw one ``(scope_id, subscription_id)`` weighted toward RG/resource (D-04).

    Only NON-EMPTY tiers participate (a tenant always has ≥1 subscription, so the
    subscription tier is the guaranteed fallback). Both draws flow through ``ctx``.
    """
    tiers = [
        (pools[name], _SCOPE_TIER_WEIGHTS[name])
        for name in ("subscription", "resourceGroup", "resource")
        if pools[name]
    ]
    pool = ctx.choice([p for p, _ in tiers], [w for _, w in tiers])
    return ctx.choice(pool)


def _assignment(ctx: SeededContext, sub_id, principal: dict, role_name: str,
                scope: str) -> dict:
    """Build one role_assignment row referencing a real principal/role/scope (D-07).

    No "finding"/marker column (D-06): the over-privilege IS the assignment — a
    governance scanner infers it from role+scope exactly as in real Azure.
    """
    return {
        "assignment_id": ctx.uuid4(),
        "subscription_id": sub_id,
        "principal_oid": principal["oid"],
        "principal_type": principal["principal_type"],
        "role_definition_id": _role_definition_id(role_name),
        "scope": scope,
    }


def assign_roles(
    ctx: SeededContext,
    tenant,
    principals: list[dict],
    *,
    over_privilege_rate: float,
) -> tuple[list[dict], int]:
    """Draw role_assignments + inject over-privilege at a configurable rate (IAM-02).

    Two passes over the principal pool (sorted by ``oid`` for deterministic draw
    order), both driven by the injected ``ctx``:

    1. **Baseline (D-04):** one Reader-heavy assignment per principal — a role drawn
       from :data:`_BASELINE_ROLE_WEIGHTS` (Reader >> Contributor > Owner + a few
       specialized) at a scope drawn from the three REAL scope pools (Pitfall 1),
       weighted toward RG/resource with some subscription scope.
    2. **Over-privilege (D-05):** a SECOND ``ctx.bernoulli(over_privilege_rate)``
       pass injecting Owner-at-subscription-scope grants (the IAM analogue of the
       Phase-5 violation injector). ``over_privilege_rate=0`` injects ZERO rows;
       a higher rate injects proportionally more (``rate=1`` → one grant per
       principal). ServicePrincipal-granted-Owner is itself the spicy signal — an
       SP that should rarely be Owner — and is captured by the same broad grant.

    Returns ``(rows, injected_over_privilege_count)``. The count is RETURNED (not a
    marker column, D-06) so the ``generate`` run summary can report it. An EMPTY
    ``principals`` pool is a clean no-op — zero rows, zero RNG draws — so a
    no-identity ``generate`` stays byte-identical to the Phase-8 baseline.
    """
    if not principals:
        return [], 0  # no-op: no pool → no assignments, no draws (baseline guard).

    sorted_principals = sorted(principals, key=lambda p: str(p["oid"]))
    pools = _scope_pools(tenant)

    role_names = sorted(_BASELINE_ROLE_WEIGHTS)
    role_weights = [_BASELINE_ROLE_WEIGHTS[name] for name in role_names]

    rows: list[dict] = []

    # Pass 1 — baseline Reader-heavy mix (one per principal, sorted by oid).
    for principal in sorted_principals:
        role_name = ctx.choice(role_names, role_weights)
        scope, sub_id = _draw_scope(ctx, pools)
        rows.append(_assignment(ctx, sub_id, principal, role_name, scope))

    # Pass 2 — over-privilege injection (D-05): Owner at SUBSCRIPTION scope (broad).
    subscription_pool = pools["subscription"]
    over_privilege_count = 0
    for principal in sorted_principals:
        if not ctx.bernoulli(over_privilege_rate):
            continue
        scope, sub_id = ctx.choice(subscription_pool)
        rows.append(_assignment(ctx, sub_id, principal, "Owner", scope))
        over_privilege_count += 1

    return rows, over_privilege_count
