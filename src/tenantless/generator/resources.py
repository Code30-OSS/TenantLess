"""Per-RG resource generation: type mix (GEN-04) + ARM property/sku coercion
(GEN-07) + dependency-ordered reference wiring (GEN-08).

This module is the structural INVERSE of
``analyzer.extractors.type_shapes``: that extractor turned each type's
``properties``/``sku`` JSON into normalized ``{field: {value: share}}`` histogram
maps (stringifying every value and folding rare values into ``"__other__"``).
Here we sample those maps back into concrete, ARM-valid ``properties``/``sku``
JSONB — coercing strings back to typed JSON and NEVER echoing a sentinel.

Three jobs:

GEN-04 — Type mix per RG
    From the RG's template ``type_set``, sample which types fill the RG weighted
    by each type's global ``frequency`` (renormalized over the set). The synthetic
    ``__misc__`` type-set is skipped — it is a profile aggregation artifact, never
    a real resource type.

GEN-07 — ARM-valid properties / sku (DATA-DRIVEN)
    For every profile type that carries a non-empty ``property_distributions``,
    populate ``properties`` keyed on the SAME field set the extractor allow-listed
    (``type_shapes.PROPERTY_FIELD_ALLOWLIST``). The GEN-07-named types additionally
    get rich ARM-shaped wrappers (RESEARCH lines 391-415). All values pass through
    :data:`PROPERTY_COERCION`: ``"true"``→``True``, ``"127"``→``127``,
    ``"null"``→``None`` (omitted), and ``"__other__"``/``"__misc__"`` are resampled
    out or minted — they MUST NOT reach data (threat T-02-05).

GEN-08 — Reference wiring
    Wiring is performed by :func:`wire_references` over a subscription-scoped pool
    (see that function) so every VM→NIC, NIC→subnet, VM→disk reference resolves to
    a real generated resource id (threat T-02-06).

DB-free: imports neither psycopg nor duckdb. Operates on the profile dict + the
injected :class:`~tenantless.generator.rng.SeededContext`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..analyzer.extractors.type_shapes import PROPERTY_FIELD_ALLOWLIST
from . import arm
from .rng import SeededContext

_OTHER = "__other__"
_MISC = "__misc__"
_SENTINELS = frozenset({_OTHER, _MISC})
_NULL = "null"

# Canonical (Microsoft.-leading) type keys for the named GEN-07 types, so the
# rich-template dispatch is casing-robust against both profiles.
T_VM = "Microsoft.Compute/virtualMachines"
T_DISK = "Microsoft.Compute/disks"
T_NIC = "Microsoft.Network/networkInterfaces"
T_PIP = "Microsoft.Network/publicIPAddresses"
T_NSG = "Microsoft.Network/networkSecurityGroups"
T_VNET = "Microsoft.Network/virtualNetworks"
T_STORAGE = "Microsoft.Storage/storageAccounts"
T_WEBSITE = "Microsoft.Web/sites"
T_SERVERFARM = "Microsoft.Web/serverfarms"
T_SQLSRV = "Microsoft.Sql/servers"
T_SQLDB = "Microsoft.Sql/servers/databases"
T_KV = "Microsoft.KeyVault/vaults"
T_AKS = "Microsoft.ContainerService/managedClusters"

# --------------------------------------------------------------------------- #
# Coercion contract (Pitfall 1): profile values are STRINGS — re-type them.
# --------------------------------------------------------------------------- #

# Fields whose stringified histogram value is a JSON boolean ("true"/"false").
_BOOL_FIELDS = frozenset(
    {
        "enableAcceleratedNetworking",
        "enableIPForwarding",
        "supportsHttpsTrafficOnly",
        "allowBlobPublicAccess",
        "defaultSecurityRulesOnly",
        "enableDdosProtection",
        "httpsOnly",
        "reserved",
        "enableSoftDelete",
        "enablePurgeProtection",
        "enableRbacAuthorization",
        "enableRBAC",
        "registrationEnabled",
        "autoUpgradeMinorVersion",
        "enableAutomaticUpgrade",
        "DisableLocalAuth",
    }
)

# Fields whose stringified histogram value is a JSON integer.
_INT_FIELDS = frozenset(
    {
        "diskSizeGB",
        "securityRuleCount",
        "subnetCount",
        "addressSpacePrefixCount",
        "retentionInDays",
    }
)


def _coerce(field_name: str, value: str) -> Any:
    """Coerce a stringified profile value back to ARM-valid typed JSON.

    ``"true"``/``"false"`` → bool for boolean fields; integer string → int for
    count/size fields; the literal ``"null"`` → ``None``. Everything else stays
    a string (enums like ``"TLS1_2"``). Sentinels are handled upstream by
    :data:`PROPERTY_COERCION` resampling and never reach here.
    """
    if value == _NULL:
        return None
    if field_name in _BOOL_FIELDS:
        return value == "true"
    if field_name in _INT_FIELDS:
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    return value


# Public alias for the must_haves contract (``contains: "PROPERTY_COERCION"``):
# the coercion contract is the {field → typed-value} mapping realized by _coerce
# plus the sentinel-resampling in :func:`_sample_field`.
PROPERTY_COERCION = {
    "bool_fields": _BOOL_FIELDS,
    "int_fields": _INT_FIELDS,
    "null_literal": _NULL,
    "coerce": _coerce,
}


@dataclass
class Resource:
    """An in-memory synthetic Azure resource (a row of ``synthetic.resources``)."""

    id: str  # full (possibly nested) ARM path
    subscription_id: Any  # uuid.UUID
    resource_group_name: str
    name: str
    type: str  # single canonical casing (arm.canonical_type)
    location: str
    api_version: str
    tags: dict[str, str] = field(default_factory=dict)
    sku: dict[str, Any] | None = None
    kind: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)
    provisioning_state: str = "Succeeded"
    managed_by: str | None = None


# --------------------------------------------------------------------------- #
# GEN-04: type-mix sampling over a template's type_set.
# --------------------------------------------------------------------------- #


def sample_type_mix(
    ctx: SeededContext,
    type_set: list[str],
    resource_type_distributions: dict[str, dict],
    count: int,
) -> list[str]:
    """Sample ``count`` canonical type keys from ``type_set`` (GEN-04).

    Each type is weighted by its global ``frequency`` renormalized over the set
    (Pitfall 2). The synthetic ``__misc__`` sentinel type-set member is skipped
    so it is never emitted as a resource type.
    """
    # Deterministic order (Pitfall 3): sort the canonical keys.
    candidates = sorted(
        {arm.canonical_type(t) for t in type_set if t and t != _MISC}
    )
    if not candidates or count <= 0:
        return []

    weights = [
        float(resource_type_distributions.get(t, {}).get("frequency", 0.0))
        for t in candidates
    ]
    if sum(weights) <= 0.0:
        weights = [1.0] * len(candidates)

    # SPEED-01 (13-04): ONE batched draw of `count` type keys replaces `count`
    # scalar ctx.choice calls (13-01 measured sample_type_mix among the residual
    # categorical hot loops). Same sorted candidates + renormalize discipline
    # (Pitfall 2/3); draws flow through the per-substream Generator (ctx.rng), so
    # the grep-auditable RNG seam and jobs-1 == jobs-N determinism both hold.
    probs = np.asarray(weights, dtype=float)
    probs = probs / probs.sum()
    draws = ctx.rng.choice(candidates, size=count, p=probs)
    return [str(t) for t in draws]


def _sample_from_weights(
    ctx: SeededContext, weights: dict[str, float], count: int
) -> list[str]:
    """Sample ``count`` canonical type keys from an explicit ``{type: weight}`` map.

    The counterpart to :func:`sample_type_mix` for templates that carry a
    ``type_weights`` histogram (the ``__misc__`` privacy bucket) or for the global
    fallback. Canonicalizes + de-dups keys (summing colliding weights), skips the
    ``__misc__`` sentinel, then draws in ONE batched ``ctx.rng.choice`` (same
    sorted-candidates + renormalize discipline as ``sample_type_mix``).
    """
    agg: dict[str, float] = {}
    for t, w in weights.items():
        if not t or t == _MISC:
            continue
        key = arm.canonical_type(t)
        agg[key] = agg.get(key, 0.0) + float(w)
    candidates = sorted(agg)
    if not candidates or count <= 0:
        return []
    probs = np.asarray([agg[c] for c in candidates], dtype=float)
    if probs.sum() <= 0.0:
        probs = np.ones(len(candidates), dtype=float)
    probs = probs / probs.sum()
    draws = ctx.rng.choice(candidates, size=count, p=probs)
    return [str(t) for t in draws]


def sample_rg_types(
    ctx: SeededContext,
    template: dict[str, Any],
    resource_type_distributions: dict[str, dict],
    count: int,
) -> list[str]:
    """Resolve the ``count`` resource types for one RG from its template (GEN-04).

    Precedence (the fix for the ~55%-empty-RG / single-type-pile artifact — a
    ``__misc__`` template whose ``type_set`` is only the sentinel would otherwise
    generate EMPTY, and calibrate would then over-pad the survivors):

    1. **Real ``type_set``** (>=1 non-sentinel type) → the existing
       :func:`sample_type_mix` path, byte-for-byte unchanged for standalone
       templates.
    2. **``type_weights``** (analyzer-backed misc histogram) → sample from that
       distribution, so the privacy bucket carries its real resource-type mass.
    3. **Global fallback (guardrail B)** → sample from the tenant-wide
       ``resource_type_distributions`` frequencies so a sentinel-only template
       with NO histogram still NEVER generates empty. Lower fidelity than (2) — it
       ignores the bucket's own mix — and exists only as a defensive backstop.
    """
    type_set = template.get("type_set", []) or []
    real = [t for t in type_set if t and t != _MISC]
    if real:
        return sample_type_mix(ctx, type_set, resource_type_distributions, count)

    type_weights = template.get("type_weights")
    if type_weights:
        return _sample_from_weights(ctx, type_weights, count)

    global_weights = {
        t: float(e.get("frequency", 0.0))
        for t, e in resource_type_distributions.items()
    }
    return _sample_from_weights(ctx, global_weights, count)


# --------------------------------------------------------------------------- #
# GEN-07: per-field sampling + coercion, and per-type ARM property/sku assembly.
# --------------------------------------------------------------------------- #


def _sample_field(ctx: SeededContext, value_map: dict[str, float], field_name: str):
    """Draw one value for ``field_name`` and coerce it (sentinel-free).

    Re-draws while the result is a sentinel; if the map is sentinel-only, mints a
    plausible synthetic value rather than echoing the sentinel (threat T-02-05).
    """
    non_sentinel = {k: v for k, v in value_map.items() if k not in _SENTINELS}
    if not non_sentinel:
        # Sentinel-only field: mint a synthetic value. Booleans default False,
        # ints default 1, enums get a synthetic token — never the sentinel.
        if field_name in _BOOL_FIELDS:
            return False
        if field_name in _INT_FIELDS:
            return 1
        return f"{field_name}-{int(ctx.rng.integers(1, 1000)):03d}"
    raw = ctx.categorical(non_sentinel)
    return _coerce(field_name, raw)


def _sample_allowed_properties(
    ctx: SeededContext, type_key: str, prop_dists: dict[str, dict]
) -> dict[str, Any]:
    """Sample a flat ``{field: coerced_value}`` map over the allow-listed fields.

    Uses ``type_shapes.PROPERTY_FIELD_ALLOWLIST`` as the field contract; only
    fields that are BOTH allow-listed for the type AND present in the profile's
    ``property_distributions`` are emitted. ``None`` (from ``"null"``) is omitted.
    """
    allowed = PROPERTY_FIELD_ALLOWLIST.get(type_key)
    out: dict[str, Any] = {}
    # Deterministic field order (Pitfall 3).
    for fld in sorted(prop_dists):
        if allowed is not None and fld not in allowed:
            continue
        value = _sample_field(ctx, prop_dists[fld], fld)
        if value is None:
            continue  # "null" → omit the field (ARM-valid)
        out[fld] = value
    return out


def build_sku(
    ctx: SeededContext, sku_dists: dict[str, dict] | None
) -> dict[str, Any] | None:
    """Assemble a ``sku`` object from ``sku_distributions`` (name/tier/family)."""
    if not sku_dists:
        return None
    sku: dict[str, Any] = {}
    for fld in sorted(sku_dists):
        value = _sample_field(ctx, sku_dists[fld], fld)
        if value is None:
            continue
        sku[fld] = value
    return sku or None


def _provisioning_state(flat_props: dict[str, Any]) -> str:
    """Lift a provisioning state out of the sampled props, defaulting Succeeded."""
    ps = flat_props.pop("provisioningState", None)
    return str(ps) if ps else "Succeeded"


def _array_of(n: int, make) -> list[Any]:
    """Expand a count field into an ARM array of ``n`` shaped elements."""
    return [make(i) for i in range(max(0, int(n)))]


def assemble_properties(type_key: str, flat: dict[str, Any]) -> dict[str, Any]:
    """Wrap flat sampled fields in the ARM-shaped ``properties`` for the type.

    For the GEN-07-named types this produces the nested ARM shapes that
    ARM-compatible consumers / governance tooling read (e.g. storage flat fields
    stay flat; VM nests vmSize under
    hardwareProfile; NSG/VNet count-fields expand into ``securityRules``/
    ``subnets`` arrays). Reference ids (NIC/subnet/disk/publicIP) are left as
    placeholders here and filled by :func:`wire_references` (GEN-08). Types
    without a rich template keep their flat allow-listed fields (still ARM-valid,
    data-driven per Critical Finding 1).
    """
    if type_key == T_VM:
        props: dict[str, Any] = {
            "hardwareProfile": {"vmSize": flat.get("vmSize", "Standard_D2s_v3")},
            "storageProfile": {
                "osDisk": {
                    "osType": flat.get("osType", "Linux"),
                    # managedDisk.id wired in pass B
                    "managedDisk": {"id": None},
                }
            },
            # networkInterfaces[].id wired in pass B
            "networkProfile": {"networkInterfaces": [{"id": None}]},
        }
        return props

    if type_key == T_NIC:
        return {
            "ipConfigurations": [
                {
                    "name": "ipconfig1",
                    "properties": {
                        # subnet.id / publicIPAddress.id wired in pass B
                        "subnet": {"id": None},
                        "privateIPAllocationMethod": "Dynamic",
                    },
                }
            ],
            "enableAcceleratedNetworking": flat.get(
                "enableAcceleratedNetworking", False
            ),
            "enableIPForwarding": flat.get("enableIPForwarding", False),
            "nicType": flat.get("nicType", "Standard"),
        }

    if type_key == T_DISK:
        return {
            "diskSizeGB": flat.get("diskSizeGB", 128),
            "osType": flat.get("osType"),
            "diskState": "Unattached",
        }

    if type_key == T_PIP:
        return {
            "publicIPAllocationMethod": flat.get(
                "publicIPAllocationMethod", "Static"
            ),
            "publicIPAddressVersion": flat.get("publicIPAddressVersion", "IPv4"),
        }

    if type_key == T_NSG:
        n_rules = flat.get("securityRuleCount", 3)
        return {
            "securityRules": _array_of(
                n_rules,
                lambda i: {
                    "name": f"rule-{i:02d}",
                    "properties": {
                        "priority": 100 + i * 10,
                        "direction": "Inbound",
                        "access": "Allow",
                        "protocol": "Tcp",
                    },
                },
            ),
            "defaultSecurityRulesOnly": flat.get("defaultSecurityRulesOnly", False),
        }

    if type_key == T_VNET:
        n_subnets = flat.get("subnetCount", 1)
        n_prefixes = flat.get("addressSpacePrefixCount", 1)
        return {
            "addressSpace": {
                "addressPrefixes": _array_of(
                    n_prefixes, lambda i: f"10.{i}.0.0/16"
                )
                or ["10.0.0.0/16"]
            },
            # subnet child resources are minted in wire_references so they have
            # real nested ARM ids that NICs can reference.
            "subnets": _array_of(n_subnets, lambda i: {"_subnet_index": i}),
            "enableDdosProtection": flat.get("enableDdosProtection", False),
        }

    if type_key == T_STORAGE:
        # Flat fields are already the ARM shape governance tooling reads.
        return dict(flat)

    if type_key == T_WEBSITE:
        return {
            "state": flat.get("state", "Running"),
            "httpsOnly": flat.get("httpsOnly", True),
        }

    if type_key == T_SERVERFARM:
        return {"reserved": flat.get("reserved", False)}

    if type_key == T_SQLSRV:
        return {
            "version": flat.get("version", "12.0"),
            "publicNetworkAccess": flat.get("publicNetworkAccess", "Enabled"),
            "minimalTlsVersion": flat.get("minimalTlsVersion", "1.2"),
        }

    if type_key == T_SQLDB:
        return {
            "status": flat.get("status", "Online"),
            "collation": flat.get(
                "collation", "SQL_Latin1_General_CP1_CI_AS"
            ),
            "transparentDataEncryption": flat.get(
                "transparentDataEncryption", "Enabled"
            ),
        }

    if type_key == T_KV:
        return {
            "enableSoftDelete": flat.get("enableSoftDelete", True),
            "enablePurgeProtection": flat.get("enablePurgeProtection", True),
            "enableRbacAuthorization": flat.get("enableRbacAuthorization", False),
        }

    if type_key == T_AKS:
        return {
            "kubernetesVersion": flat.get("kubernetesVersion", "1.29"),
            "enableRBAC": flat.get("enableRBAC", True),
            "networkProfile": {"networkPlugin": flat.get("networkPlugin", "azure")},
        }

    # Data-driven fallback (Critical Finding 1): flat allow-listed fields, still
    # ARM-valid, for any type carrying property_distributions without a rich
    # template (covers the real source-scan top types).
    return dict(flat)


def _resource_kind(type_key: str, flat: dict[str, Any]) -> str | None:
    """The ``kind`` column where the type carries one (Web sites/serverfarms)."""
    if type_key in (T_WEBSITE, T_SERVERFARM):
        return flat.get("kind")
    return None


def generate_resource(
    ctx: SeededContext,
    *,
    subscription_id,
    rg_name: str,
    location: str,
    type_key: str,
    resource_type_distributions: dict[str, dict],
    parent_name: str | None = None,
    seen_ids: set[str] | None = None,
) -> Resource:
    """Generate one fully-formed resource (GEN-04 + GEN-07), unwired.

    The name is a seeded synthetic Azure-shaped token; properties/sku are sampled
    and coerced from the profile; reference ids are placeholders until pass B.

    ``seen_ids`` (when provided) guarantees the synthesized ARM id is unique
    across the tenant — the id is the ``synthetic.resources`` PRIMARY KEY, so a
    collision would otherwise abort the binary COPY at scale (96K resources).
    """
    entry = resource_type_distributions.get(type_key, {})
    prop_dists = entry.get("property_distributions", {}) or {}
    sku_dists = entry.get("sku_distributions", {}) or {}

    flat = _sample_allowed_properties(ctx, type_key, prop_dists)
    provisioning_state = _provisioning_state(flat)
    kind = _resource_kind(type_key, flat)
    properties = assemble_properties(type_key, flat)
    sku = build_sku(ctx, sku_dists)

    name = _resource_name(ctx, type_key)
    rid = arm.resource_id(
        subscription_id, rg_name, type_key, name, parent_name=parent_name
    )
    if seen_ids is not None:
        # Re-mint the name until the resulting ARM id is unique (PK safety).
        while rid in seen_ids:
            name = _resource_name(ctx, type_key)
            rid = arm.resource_id(
                subscription_id, rg_name, type_key, name, parent_name=parent_name
            )
        seen_ids.add(rid)
    return Resource(
        id=rid,
        subscription_id=subscription_id,
        resource_group_name=rg_name,
        name=name,
        type=type_key,
        location=location,
        api_version=arm.api_version_for(type_key),
        sku=sku,
        kind=kind,
        properties=properties,
        provisioning_state=provisioning_state,
    )


# A short per-namespace name abbreviation for Azure-shaped resource names.
_TYPE_ABBREV = {
    T_VM: "vm",
    T_DISK: "disk",
    T_NIC: "nic",
    T_PIP: "pip",
    T_NSG: "nsg",
    T_VNET: "vnet",
    T_STORAGE: "st",
    T_WEBSITE: "app",
    T_SERVERFARM: "plan",
    T_SQLSRV: "sql",
    T_SQLDB: "db",
    T_KV: "kv",
    T_AKS: "aks",
}


def _resource_name(ctx: SeededContext, type_key: str) -> str:
    """A seeded synthetic Azure-shaped resource name (D-11; never real data)."""
    abbrev = _TYPE_ABBREV.get(type_key)
    if abbrev is None:
        tail = type_key.split("/")[-1].lower()
        abbrev = "".join(ch for ch in tail if ch.isalnum())[:6] or "res"
    suffix = int(ctx.rng.integers(1, 10000))
    if type_key == T_STORAGE:
        # Storage accounts: 3-24 lowercase alnum, no hyphens.
        return f"st{ctx.faker.lexify('????').lower()}{suffix:04d}"[:24]
    return f"{abbrev}-{ctx.faker.lexify('????').lower()}-{suffix:04d}"


# --------------------------------------------------------------------------- #
# GEN-08: dependency-ordered reference wiring (subscription-scoped pool).
# --------------------------------------------------------------------------- #


def _subnet_id(vnet: "Resource", index: int) -> str:
    """Child ARM path of a VNet subnet (referenceable by NICs)."""
    return f"{vnet.id}/subnets/subnet-{index:02d}"


def _materialize_subnets(vnet: "Resource", host_rg: Any) -> list[str]:
    """Turn VNet subnet placeholders into real child rows + nested ids.

    Subnets are genuine ARM child resources, so each gets a standalone
    :class:`Resource` row (appended to the VNet's RG) AND a nested entry under the
    VNet's ``properties.subnets`` — so a NIC's ``subnet.id`` reference resolves to
    a generated resource id (GEN-08), exactly as in a real scan.

    **Idempotent:** a VNet whose subnets are already materialized (each nested
    subnet carries an ``id``) returns its existing subnet ids WITHOUT appending
    duplicate child rows. This matters because calibration may re-run
    :func:`wire_references` on a subscription whose VNets were materialized in the
    initial pass — re-materializing would mint duplicate subnet PKs and abort the
    binary COPY (resources_pkey UniqueViolation).
    """
    subnets = vnet.properties.get("subnets", [])
    # Already materialized → return existing ids, append nothing.
    if subnets and all(isinstance(sn, dict) and sn.get("id") for sn in subnets):
        return [sn["id"] for sn in subnets]
    ids: list[str] = []
    for i, sn in enumerate(subnets):
        sid = _subnet_id(vnet, i)
        sn.pop("_subnet_index", None)
        sn["name"] = f"subnet-{i:02d}"
        sn["id"] = sid
        sn["properties"] = {"addressPrefix": f"10.0.{i}.0/24"}
        host_rg.resources.append(
            Resource(
                id=sid,
                subscription_id=vnet.subscription_id,
                resource_group_name=vnet.resource_group_name,
                name=f"subnet-{i:02d}",
                type=f"{T_VNET}/subnets",
                location=vnet.location,
                api_version=arm.api_version_for(T_VNET),
                properties={"addressPrefix": f"10.0.{i}.0/24"},
                managed_by=vnet.id,
            )
        )
        ids.append(sid)
    return ids


def _mint_companion(
    ctx: SeededContext,
    type_key: str,
    subscription_id,
    rg: "Resource | Any",
    rtd,
    seen_ids: set[str] | None = None,
) -> "Resource":
    """Mint a minimal companion resource of ``type_key`` in the given RG.

    Used when a required reference pool is empty (Open Question 2 fallback (a)):
    a real companion is always preferable to a dangling/omitted reference for
    scan fidelity (threat T-02-06 — never fabricate an id with no resource).
    """
    return generate_resource(
        ctx,
        subscription_id=subscription_id,
        rg_name=rg.name,
        location=rg.location,
        type_key=type_key,
        resource_type_distributions=rtd,
        seen_ids=seen_ids,
    )


def wire_references(
    ctx: SeededContext,
    subscription_id,
    rgs: list[Any],
    rtd: dict[str, dict],
    seen_ids: set[str] | None = None,
) -> None:
    """Wire VM→NIC, NIC→subnet, VM→disk references within a subscription (GEN-08).

    Two-pass over a subscription-scoped pool (RESEARCH Pattern 2):

    * Pass A indexes referenceable resources already generated across all RGs of
      the subscription (VNets→subnets, publicIPs, disks, NICs) and materializes
      VNet subnet child-ids.
    * Pass B wires each dependent to a real id drawn (seeded) from the pool. When
      a pool is empty, a minimal companion is minted into the dependent's RG so
      the reference still resolves (fallback (a)); we never leave a placeholder
      ``None`` or fabricate a dangling id.

    Mutates the resources in ``rgs`` in place.
    """
    all_resources: list[Resource] = [r for rg in rgs for r in rg.resources]
    by_type: dict[str, list[Resource]] = {}
    for r in all_resources:
        by_type.setdefault(r.type, []).append(r)

    # Map each resource back to its RG (needed before Pass A for subnet rows).
    rg_of: dict[str, Any] = {}
    for rg in rgs:
        for r in rg.resources:
            rg_of[r.id] = rg

    # Pass A: materialize subnet child rows from every VNet in scope.
    subnet_ids: list[str] = []
    for vnet in by_type.get(T_VNET, []):
        sids = _materialize_subnets(vnet, rg_of[vnet.id])
        if seen_ids is not None:
            seen_ids.update(sids)
        subnet_ids.extend(sids)

    def pool_ids(type_key: str) -> list[str]:
        return sorted(r.id for r in by_type.get(type_key, []))

    nic_ids = pool_ids(T_NIC)
    disk_ids = pool_ids(T_DISK)
    pip_ids = pool_ids(T_PIP)

    def ensure_pool(ids: list[str], type_key: str, host_rg) -> str:
        """Return a real id from ``ids``; mint a companion into host_rg if empty."""
        if ids:
            return ctx.choice(ids)
        companion = _mint_companion(
            ctx, type_key, subscription_id, host_rg, rtd, seen_ids
        )
        host_rg.resources.append(companion)
        by_type.setdefault(type_key, []).append(companion)
        ids.append(companion.id)
        return companion.id

    # Pass B (VMs first): a VM may mint companion NICs/disks; wiring all NICs
    # afterwards guarantees even minted companion NICs get a real subnet.
    for vm in by_type.get(T_VM, []):
        host_rg = rg_of[vm.id]
        nic_id = ensure_pool(nic_ids, T_NIC, host_rg)
        if nic_id not in rg_of:
            rg_of[nic_id] = host_rg  # track minted companion NIC for its own wiring
        vm.properties["networkProfile"]["networkInterfaces"] = [{"id": nic_id}]
        disk_id = ensure_pool(disk_ids, T_DISK, host_rg)
        vm.properties["storageProfile"]["osDisk"]["managedDisk"] = {"id": disk_id}

    # Now wire EVERY NIC in scope (original + companions) to a real subnet.
    for nic in by_type.get(T_NIC, []):
        host_rg = rg_of.get(nic.id) or _rg_holding(rgs, nic)
        for cfg in nic.properties.get("ipConfigurations", []):
            props = cfg.setdefault("properties", {})
            if not subnet_ids:
                # Mint a companion VNet (with subnets) into the NIC's RG.
                vnet = _mint_companion(
                    ctx, T_VNET, subscription_id, host_rg, rtd, seen_ids
                )
                host_rg.resources.append(vnet)
                rg_of[vnet.id] = host_rg
                new_sids = _materialize_subnets(vnet, host_rg)
                if seen_ids is not None:
                    seen_ids.update(new_sids)
                subnet_ids.extend(new_sids)
            props["subnet"] = {"id": ctx.choice(subnet_ids)}
            if pip_ids:
                props["publicIPAddress"] = {"id": ctx.choice(pip_ids)}


def _rg_holding(rgs: list[Any], resource: "Resource") -> Any:
    """Find the RG object currently holding ``resource`` (companion fallback)."""
    for rg in rgs:
        if resource in rg.resources:
            return rg
    return rgs[0]
