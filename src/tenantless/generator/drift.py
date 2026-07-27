"""Seeded drift mutation engine core (Plan 11-03, DRIFT-01/02).

The conceptual **4th inject twin** of ``violations.py`` — a table-driven
registry of ``(ctx, resource) -> delta`` mutators — but read-modify-write over an
already-generated tenant rather than build-into ``GenerationResult``. This module
is the DB-FREE compute core: it mutates in-memory :class:`resources.Resource`
objects and returns deltas; the SELECT/UPDATE/INSERT seam lives in the CLI/writer
(Plan 11-05), never here. There is no ``psycopg`` import.

This plan ships the CHAOS catalogue only (D-02 sequencing: chaos before temporal,
which lands in Plan 11-04). Every mutator overwrites the EXACT served JSONB key
the ARM server returns (``tags`` / ``sku`` / ``kind`` / ``properties``) per the
RESEARCH §"Served-property mutation map", so drift is ARM-visible (DRIFT-03).

Determinism contract (DRIFT-01, identical to ``violations.py`` / ``rng.py``):

- Codes live in a FRESH ``DRIFT_*`` namespace that never reuses a
  ``VIOLATION_REGISTRY`` code (RESEARCH Open Q1).
- The eligible population is SORTED by id before any ``ctx`` draw (Pitfall 3), so
  the same ``(seed, options, parent-state)`` yields byte-identical drift.
- Every draw goes through the injected :class:`SeededContext` — no global RNG,
  and no wall-clock anywhere in the seeded compute (``applied_at`` is the CLI's
  job, Plan 11-05).
- Each mutator returns the DRIFT-04 audit delta ``{field_path, before, after}``
  (the drift-engine rename of violations' ``{path, from, to}``).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Callable

import orjson

from ..analyzer.extractors.tags import _is_identifier_shaped_value
from . import resources
from .rng import SeededContext

# Sentinel ``eligible_type`` for specs whose population is "any taggable resource
# that carries the key" (the predicate does the filtering) rather than a single
# ``Resource.type`` match — mirror of the violations TAG_* type-agnostic codes.
_ANY_TYPE = "*"


@dataclass(frozen=True)
class DriftSpec:
    """One drift code as DATA (mirror of :class:`violations.ViolationSpec`).

    ``mutate(ctx, resource) -> {field_path, before, after}`` overwrites the served
    ``resource.properties`` / ``resource.tags`` key in place and returns the
    DRIFT-04 audit delta. ``drift_type`` is ``"chaos"`` (this plan) or
    ``"temporal"`` (Plan 11-04). ``eligible_predicate`` (when present) narrows the
    population beyond ``eligible_type`` — e.g. the tag-removal codes restrict to
    resources that currently HAVE the key.
    """

    eligible_type: str
    drift_type: str  # "chaos" | "temporal"
    mutate: Callable[[SeededContext, Any], dict]
    eligible_predicate: Callable[[Any], bool] | None = None


# --------------------------------------------------------------------------- #
# CHAOS FLIP mutators — overwrite an existing served field with an insecure value
# (the SAME key emitted by resources.assemble_properties() and served by
# resources.rs). before/after are the DRIFT-04 audit names.
# --------------------------------------------------------------------------- #


def _chaos_storage_public(ctx: SeededContext, r) -> dict:
    before = r.properties.get("allowBlobPublicAccess")
    r.properties["allowBlobPublicAccess"] = True
    return {
        "field_path": "properties.allowBlobPublicAccess",
        "before": before,
        "after": True,
    }


def _chaos_storage_http(ctx: SeededContext, r) -> dict:
    before = r.properties.get("supportsHttpsTrafficOnly")
    r.properties["supportsHttpsTrafficOnly"] = False
    return {
        "field_path": "properties.supportsHttpsTrafficOnly",
        "before": before,
        "after": False,
    }


def _chaos_storage_old_tls(ctx: SeededContext, r) -> dict:
    before = r.properties.get("minimumTlsVersion")
    r.properties["minimumTlsVersion"] = "TLS1_0"
    return {
        "field_path": "properties.minimumTlsVersion",
        "before": before,
        "after": "TLS1_0",
    }


def _chaos_kv_no_purge(ctx: SeededContext, r) -> dict:
    before = r.properties.get("enablePurgeProtection")
    r.properties["enablePurgeProtection"] = False
    return {
        "field_path": "properties.enablePurgeProtection",
        "before": before,
        "after": False,
    }


def _chaos_kv_no_soft_delete(ctx: SeededContext, r) -> dict:
    before = r.properties.get("enableSoftDelete")
    r.properties["enableSoftDelete"] = False
    return {
        "field_path": "properties.enableSoftDelete",
        "before": before,
        "after": False,
    }


def _chaos_aks_no_rbac(ctx: SeededContext, r) -> dict:
    before = r.properties.get("enableRBAC")
    r.properties["enableRBAC"] = False
    return {"field_path": "properties.enableRBAC", "before": before, "after": False}


# --------------------------------------------------------------------------- #
# CHAOS INSERT/DELETE mutators — NSG open-rule append + tag-key removal.
# --------------------------------------------------------------------------- #


def _chaos_nsg_open_inbound(ctx: SeededContext, r) -> dict:
    """Append a genuinely-open inbound rule to the served ``securityRules[]``.

    The new rule's priority is placed at ``max(existing) + 10`` so it never
    collides with the generated ``100 + i*10`` band (``_nsg_open_rule``
    precedent, violations.py).
    """
    rules = r.properties.setdefault("securityRules", [])
    used = [
        p for p in (x.get("properties", {}).get("priority") for x in rules) if p
    ]
    prio = (max(used) if used else 100) + 10
    rule = {
        "name": "AllowAll-Inbound-Drift",
        "properties": {
            "access": "Allow",
            "direction": "Inbound",
            "protocol": "*",
            "sourcePortRange": "*",
            "destinationPortRange": "*",
            "sourceAddressPrefix": "*",
            "destinationAddressPrefix": "*",
            "priority": prio,
        },
    }
    rules.append(rule)
    return {
        "field_path": "properties.securityRules[]",
        "before": None,
        "after": rule,
    }


def _chaos_remove_tag(key: str) -> Callable[[SeededContext, Any], dict]:
    def _mutate(ctx: SeededContext, r) -> dict:
        before = r.tags.get(key)
        r.tags.pop(key, None)
        return {"field_path": f"tags.{key}", "before": before, "after": None}

    return _mutate


def _has_tag(key: str) -> Callable[[Any], bool]:
    return lambda r: key in r.tags


def _has_sku(r) -> bool:
    return bool(getattr(r, "sku", None))


# --------------------------------------------------------------------------- #
# TEMPORAL mutators (Plan 11-04) — passage-of-time changes. Each overwrites the
# EXACT served JSONB key (RESEARCH §"Served-property mutation map" temporal rows)
# and returns the same {field_path, before, after} delta shape. Pitfall 1: the
# provisioning mutator writes properties.provisioningState (the served JSONB),
# NEVER the unserved column attribute — drift would be ARM-invisible otherwise.
# --------------------------------------------------------------------------- #


def _temporal_provisioning(ctx: SeededContext, r) -> dict:
    """Provisioning-state drift into the served properties JSONB (Pitfall 1)."""
    before = r.properties.get("provisioningState")
    r.properties["provisioningState"] = "Updating"
    return {
        "field_path": "properties.provisioningState",
        "before": before,
        "after": "Updating",
    }


# Adjacent-tier ladder for SKU shifts: shift UP one tier, wrapping DOWN from the
# top so the value always changes; an unknown tier escalates to "Premium".
_TIER_LADDER = ("Free", "Basic", "Standard", "Premium")


def _shift_tier(tier: str) -> str:
    try:
        i = _TIER_LADDER.index(tier)
    except ValueError:
        return "Premium"
    return _TIER_LADDER[i + 1] if i + 1 < len(_TIER_LADDER) else _TIER_LADDER[i - 1]


def _temporal_sku_shift(ctx: SeededContext, r) -> dict:
    """Shift sku.tier (and the name's tier prefix) to an adjacent ladder tier."""
    sku = dict(getattr(r, "sku", None) or {})
    before = dict(sku)
    tier = sku.get("tier")
    new_tier = _shift_tier(str(tier)) if tier is not None else _shift_tier("")
    sku["tier"] = new_tier
    name = sku.get("name")
    if isinstance(name, str):
        head, sep, tail = name.partition("_")
        if head in _TIER_LADDER:
            sku["name"] = f"{new_tier}{sep}{tail}"
    r.sku = sku
    return {"field_path": "sku", "before": before, "after": dict(sku)}


def _temporal_tag_churn(key: str) -> Callable[[SeededContext, Any], dict]:
    """Churn (change, never remove) a present tag VALUE — distinct from the chaos
    tag-removal mutator which deletes the key entirely."""

    def _mutate(ctx: SeededContext, r) -> dict:
        before = r.tags.get(key)
        after = "staging" if before != "staging" else "production"
        r.tags[key] = after
        return {"field_path": f"tags.{key}", "before": before, "after": after}

    return _mutate


def _temporal_prop_set(field_path: str, value) -> Callable[[SeededContext, Any], dict]:
    """Overwrite a single served ``properties.<key>`` to a fixed downgraded value
    (TLS/policy/TDE/website mutators share this shape)."""
    key = field_path.split(".", 1)[1]

    def _mutate(ctx: SeededContext, r) -> dict:
        before = r.properties.get(key)
        r.properties[key] = value
        return {"field_path": field_path, "before": before, "after": value}

    return _mutate


# --------------------------------------------------------------------------- #
# The DRIFT registry — fresh DRIFT_* namespace (RESEARCH Open Q1). Every entry
# overwrites a served JSONB key from the RESEARCH §"Served-property mutation map".
# --------------------------------------------------------------------------- #

DRIFT_REGISTRY: dict[str, DriftSpec] = {
    "DRIFT_STORAGE_PUBLIC_ACCESS": DriftSpec(
        resources.T_STORAGE, "chaos", _chaos_storage_public
    ),
    "DRIFT_STORAGE_HTTP_ALLOWED": DriftSpec(
        resources.T_STORAGE, "chaos", _chaos_storage_http
    ),
    "DRIFT_STORAGE_OLD_TLS": DriftSpec(
        resources.T_STORAGE, "chaos", _chaos_storage_old_tls
    ),
    "DRIFT_NSG_OPEN_INBOUND": DriftSpec(
        resources.T_NSG, "chaos", _chaos_nsg_open_inbound
    ),
    "DRIFT_KV_NO_PURGE_PROTECT": DriftSpec(
        resources.T_KV, "chaos", _chaos_kv_no_purge
    ),
    "DRIFT_KV_NO_SOFT_DELETE": DriftSpec(
        resources.T_KV, "chaos", _chaos_kv_no_soft_delete
    ),
    "DRIFT_AKS_NO_RBAC": DriftSpec(
        resources.T_AKS, "chaos", _chaos_aks_no_rbac
    ),
    # Tag removal applies to ANY taggable resource that currently carries the key
    # (predicate-filtered, type-agnostic).
    "DRIFT_TAGS_REMOVED": DriftSpec(
        _ANY_TYPE,
        "chaos",
        _chaos_remove_tag("environment"),
        eligible_predicate=_has_tag("environment"),
    ),
    # ----------------------------------------------------------------------- #
    # TEMPORAL codes (Plan 11-04) — drift_type="temporal"; inert under a chaos
    # run (compute_drift filters by drift_type) and vice versa.
    # ----------------------------------------------------------------------- #
    # Provisioning state into the served JSONB (Pitfall 1 — never the column).
    "DRIFT_PROVISIONING_STATE": DriftSpec(
        _ANY_TYPE, "temporal", _temporal_provisioning
    ),
    # SKU tier shift for any resource carrying a sku object.
    "DRIFT_SKU_TIER_SHIFT": DriftSpec(
        _ANY_TYPE, "temporal", _temporal_sku_shift, eligible_predicate=_has_sku
    ),
    # Tag value churn (change, never remove) on any resource that has the key.
    "DRIFT_TAG_CHURN": DriftSpec(
        _ANY_TYPE,
        "temporal",
        _temporal_tag_churn("environment"),
        eligible_predicate=_has_tag("environment"),
    ),
    # SQL server TLS / public-network downgrades.
    "DRIFT_SQL_TLS_DOWNGRADE": DriftSpec(
        resources.T_SQLSRV,
        "temporal",
        _temporal_prop_set("properties.minimalTlsVersion", "1.0"),
    ),
    "DRIFT_SQL_PUBLIC_NETWORK": DriftSpec(
        resources.T_SQLSRV,
        "temporal",
        _temporal_prop_set("properties.publicNetworkAccess", "Enabled"),
    ),
    # SQL database transparent-data-encryption disabled.
    "DRIFT_SQLDB_TDE_DISABLED": DriftSpec(
        resources.T_SQLDB,
        "temporal",
        _temporal_prop_set("properties.transparentDataEncryption", "Disabled"),
    ),
    # Web site HTTPS-only off / app stopped.
    "DRIFT_WEBSITE_HTTPS_OFF": DriftSpec(
        resources.T_WEBSITE,
        "temporal",
        _temporal_prop_set("properties.httpsOnly", False),
    ),
    "DRIFT_WEBSITE_STOPPED": DriftSpec(
        resources.T_WEBSITE,
        "temporal",
        _temporal_prop_set("properties.state", "Stopped"),
    ),
}


def _eligible_population(all_res: list, code: str, spec: DriftSpec) -> list:
    """The sorted eligible resource list for ``code`` (Pitfall 3 — sorted).

    Filters to ``spec.eligible_type`` (or any type for the ``_ANY_TYPE`` sentinel)
    and applies ``eligible_predicate`` when present, then sorts by id BEFORE any
    ``ctx`` draw so the same ``(seed, options, parent-state)`` yields identical
    selection.
    """
    pred = spec.eligible_predicate
    if spec.eligible_type == _ANY_TYPE:
        candidates = (r for r in all_res if pred is None or pred(r))
    else:
        candidates = (
            r
            for r in all_res
            if r.type == spec.eligible_type and (pred is None or pred(r))
        )
    return sorted(candidates, key=lambda r: r.id)


def planned_count(intensity: float, eligible: list) -> tuple[int, str | None]:
    """Map ``--intensity`` to a clamped count of eligible resources (D-14).

    ``0.0 <= intensity <= 1.0`` is read as a fraction (``round(intensity * n)``);
    a value ``> 1.0`` is read as an absolute target count. The result is clamped
    to ``n`` and a non-None note is returned whenever the clamp changed the target
    (never silent — D-14: clamp-and-report rather than weaken integrity).
    """
    n = len(eligible)
    target = round(intensity * n) if 0.0 <= intensity <= 1.0 else int(intensity)
    count = min(target, n)
    note = (
        None
        if count == target
        else f"clamped intensity target {target} -> {count} (only {n} eligible)"
    )
    return count, note


def state_fingerprint(rows: list[dict]) -> str:
    """Deterministic SHA-256 over the served-state of ``rows`` (D-08).

    ``rows`` carry DECODED Python objects (Pitfall 4 — never raw JSONB text) for
    ``{id, tags, sku, kind, properties, drift_deleted_at}``. Sorting by id plus
    ``orjson.OPT_SORT_KEYS`` normalizes order so the digest is stable across runs
    for the same logical state.

    ``drift_deleted_at`` is folded into the digest as a STABLE boolean presence
    flag (deleted vs active), NEVER its raw timestamp value (P2a / D-08): the
    column is a wall-clock ``applied_at`` on a soft-deleted row, so hashing the
    value would make the result_fingerprint non-deterministic across runs. Hashing
    presence keeps appear/disappear digest-visible while remaining byte-identical
    for the same ``(seed, options, parent-fp)``.
    """
    canon = sorted(
        (
            {**r, "drift_deleted_at": r.get("drift_deleted_at") is not None}
            for r in rows
        ),
        key=lambda r: r["id"],
    )
    blob = orjson.dumps(canon, option=orjson.OPT_SORT_KEYS)
    return hashlib.sha256(blob).hexdigest()


def _sample(ctx: SeededContext, eligible: list, count: int) -> list:
    """Pick ``count`` resources from the SORTED ``eligible`` list via seeded draws.

    Selection goes through ``ctx.rng`` only (no global RNG) over the already-sorted
    population, so the same ``(seed, options, parent-state)`` selects the same
    subset. The chosen indices are re-sorted so downstream delta order is stable.
    """
    n = len(eligible)
    if count <= 0 or n == 0:
        return []
    if count >= n:
        return list(eligible)
    idx = ctx.rng.choice(n, size=count, replace=False)
    return [eligible[int(i)] for i in sorted(idx)]


def compute_drift(
    ctx: SeededContext,
    rows: list,
    drift_type: str,
    codes: list[str] | None = None,
    resource_types: list[str] | None = None,
    intensity: float = 1.0,
    registry: dict[str, DriftSpec] | None = None,
) -> list[dict]:
    """Compute the seeded chaos deltas over in-memory ``rows`` (DRIFT-01/02).

    For each registered code matching ``drift_type`` (and the optional ``codes``
    filter), in sorted code order (Pitfall 3): build the sorted eligible
    population, optionally narrow it to ``resource_types``, take the D-14 clamped
    count via seeded draws over the sorted list, apply each mutator in place, and
    collect ``{resource_id, drift_code, field_path, before, after}`` deltas.

    Pure compute: mutates the passed in-memory resource objects and returns the
    deltas; there is NO DB access and NO wall-clock anywhere here (``applied_at``
    is resolved by the CLI/audit layer, Plan 11-05).
    """
    registry = registry or DRIFT_REGISTRY
    code_filter = set(codes) if codes is not None else None
    selected_codes = sorted(
        code
        for code, spec in registry.items()
        if spec.drift_type == drift_type
        and (code_filter is None or code in code_filter)
    )

    deltas: list[dict] = []
    for code in selected_codes:
        spec = registry[code]
        eligible = _eligible_population(rows, code, spec)
        if resource_types is not None:
            allowed = set(resource_types)
            eligible = [r for r in eligible if r.type in allowed]
        count, _note = planned_count(intensity, eligible)
        for r in sorted(_sample(ctx, eligible, count), key=lambda r: r.id):
            delta = spec.mutate(ctx, r)
            deltas.append({"resource_id": r.id, "drift_code": code, **delta})
    return deltas


# --------------------------------------------------------------------------- #
# TEMPORAL lifecycle (Plan 11-04, D-09/D-10/D-12) — safe disappear (soft-delete
# eligible leaves) + safe appear (mint new unreferenced leaves). DB-free: this
# module owns the pure eligibility predicate and the seeded mint; the CLI seam
# (Plan 11-05) supplies the reference sets from $N-bound anti-join SELECTs and
# persists drift_deleted_at / the appear INSERT.
# --------------------------------------------------------------------------- #

# Lifecycle codes (DRIFT_* namespace, kept OUT of DRIFT_REGISTRY: they are not
# field-mutators with an (eligible_type, mutate) shape — they operate on whole
# rows via compute_lifecycle, not compute_drift).
CODE_DISAPPEAR = "DRIFT_DISAPPEAR"
CODE_APPEAR = "DRIFT_APPEAR"

# The marker an apply records as the delta ``after`` so revert can act on it:
# disappear -> revert clears drift_deleted_at (unhide); appear -> revert DELETEs.
_DISAPPEAR_MARKER = "drift-deleted"
_APPEAR_MARKER = "appear"

# Appear mints a simple, inherently-unreferenced leaf type (no children, no
# inbound refs) — storage account (D-12).
_APPEAR_TYPE = resources.T_STORAGE


@dataclass(frozen=True)
class DisappearRefs:
    """The reference sets a resource must be ABSENT from to be disappear-eligible
    (Pitfall 6 / D-10). The CLI seam (Plan 11-05) fills these from $N-bound
    anti-join SELECTs over role_assignments / dependencies / violations and the
    managed_by column; child detection is computed from the rows themselves.
    """

    role_scopes: frozenset[str] = frozenset()
    dependency_ids: frozenset[str] = frozenset()
    violation_ids: frozenset[str] = frozenset()
    managed_by_ids: frozenset[str] = frozenset()


def disappear_eligible(rows: list, refs: DisappearRefs) -> list:
    """Sorted leaf-only resources safe to soft-delete (D-10, Pitfall 6).

    A resource qualifies only if ALL hold: it has no child resource (no OTHER row
    id starts with ``id + "/"``), and its id is referenced by no role-assignment
    scope, no dependency source/target, no violation, and no ``managed_by``. Never
    hide a referenced resource — that would dangle a reference in a re-scan.
    """
    referenced = (
        refs.role_scopes
        | refs.dependency_ids
        | refs.violation_ids
        | refs.managed_by_ids
    )
    ids = [r.id for r in rows]

    def _is_leaf(rid: str) -> bool:
        prefix = rid + "/"
        return not any(other != rid and other.startswith(prefix) for other in ids)

    eligible = [r for r in rows if r.id not in referenced and _is_leaf(r.id)]
    return sorted(eligible, key=lambda r: r.id)


def _disappear_delta(r) -> dict:
    """Soft-delete delta (D-09): set the dedicated visibility field, NEVER
    provisioningState. ``before=None`` (was visible) → revert unhides."""
    return {"field_path": "drift_deleted_at", "before": None, "after": _DISAPPEAR_MARKER}


def _appear_delta(leaf) -> dict:
    """Appear delta marker so revert can DELETE the minted row (D-13)."""
    return {"field_path": "@appear", "before": None, "after": _APPEAR_MARKER}


def mint_appear_leaf(
    ctx: SeededContext,
    rg,
    *,
    seen_ids: set[str] | None = None,
) -> resources.Resource:
    """Mint a NEW, unreferenced, seeded synthetic leaf into ``rg`` (D-12).

    Reuses ``resources.generate_resource`` for a simple leaf type with an EMPTY
    profile, so the leaf carries no profile-derived (potentially real) property
    values — only a seeded synthetic name and a deterministic ARM id. The
    data-boundary guard is reapplied on the new string-emitting naming path
    (memory: every new emitting path reapplies the guard + ships a leak test):
    the minted name must not be identifier-shaped; re-mint until it is clean.
    """
    for _ in range(8):
        leaf = resources.generate_resource(
            ctx,
            subscription_id=rg.subscription_id,
            rg_name=rg.name,
            location=rg.location,
            type_key=_APPEAR_TYPE,
            resource_type_distributions={},  # empty → no real-derived values
            seen_ids=seen_ids,
        )
        if not _is_identifier_shaped_value(leaf.name):
            leaf.tags = {}  # appear leaves are unreferenced AND untagged (no real data)
            return leaf
    raise RuntimeError("mint_appear_leaf could not produce a privacy-clean name")


def compute_lifecycle(
    ctx: SeededContext,
    rgs: list,
    refs: DisappearRefs,
    *,
    disappear_count: int = 0,
    appear_count: int = 0,
    seen_ids: set[str] | None = None,
) -> tuple[list[dict], list]:
    """Compute seeded disappear + appear over the in-memory ``rgs`` (D-09/12).

    Disappear soft-deletes a seeded subset of the sorted eligible leaves; appear
    mints new seeded leaves. Minted leaves are accumulated in a ``minted`` list
    and appended to their RG ONLY AFTER iteration completes — never mutate a
    ``.resources`` list mid-loop (violations.inject ``minted`` idiom). Returns the
    ``(deltas, minted_leaves)`` pair; DB persistence is the CLI's job (Plan 11-05).
    """
    rows = [r for rg in rgs for r in rg.resources]
    if seen_ids is None:
        seen_ids = {r.id for r in rows}

    deltas: list[dict] = []

    # Disappear: soft-delete the seeded clamped subset of eligible leaves.
    eligible = disappear_eligible(rows, refs)
    d_count = min(disappear_count, len(eligible))
    for r in sorted(_sample(ctx, eligible, d_count), key=lambda x: x.id):
        deltas.append(
            {"resource_id": r.id, "drift_code": CODE_DISAPPEAR, **_disappear_delta(r)}
        )

    # Appear: mint into a deferred list, append post-loop (never mutate the
    # .resources list while it is logically being iterated).
    minted: list[tuple] = []
    sorted_rgs = sorted(rgs, key=lambda g: g.name)
    for i in range(appear_count):
        if not sorted_rgs:
            break
        rg = sorted_rgs[i % len(sorted_rgs)]
        leaf = mint_appear_leaf(ctx, rg, seen_ids=seen_ids)
        minted.append((rg, leaf))
        deltas.append(
            {"resource_id": leaf.id, "drift_code": CODE_APPEAR, **_appear_delta(leaf)}
        )

    for rg, leaf in minted:
        rg.resources.append(leaf)

    return deltas, [leaf for _, leaf in minted]
