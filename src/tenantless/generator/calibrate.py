"""GEN-05: proportional scale calibration to within ±5% of the target.

After the pipeline samples a tenant, the resource total reflects the archetype /
template / type distributions sampled from the profile — it will rarely land
exactly on ``--resources``. ``calibrate`` nudges the total into the ±5% band
(D-06) WITHOUT distorting the mix: it trims or pads PROPORTIONALLY across resource
groups so the archetype/template/type shares are preserved (the analog of
``rg_templates``'s proportional-share spirit, RESEARCH Pattern 6).

Two directions:

* **Over target** (> +5%): trim resources proportionally from the largest RGs
  first. Referential integrity is sacred (threat T-02-09 / GEN-08): a resource
  that is the reference TARGET of a kept resource is never removed, and child
  rows (subnets) are never orphaned from their parent VNet. Only "leaf" resources
  that nothing points at are eligible to trim.

* **Under target** (< -5%): pad resources proportionally into existing RGs by
  re-sampling the same template type mix (reusing ``resources.generate_resource``),
  then re-wire references for each affected subscription so the new resources are
  fully resolvable (no dangling ids).

All randomness flows from the injected :class:`~tenantless.generator.rng.SeededContext`
(D-01): for a fixed ``(profile, seed, target)`` the calibrated tenant is identical.

DB-free: imports neither psycopg nor duckdb.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import resources
from .rng import SeededContext

if TYPE_CHECKING:  # pragma: no cover
    from .pipeline import ResourceGroup, Tenant

# Hit the BAND, not the exact number: aim a touch inside ±5% so rounding never
# leaves us a hair outside. We target within ±4% to keep margin.
_TOLERANCE = 0.05
_AIM = 0.04


def _total(tenant: "Tenant") -> int:
    return sum(len(rg.resources) for rg in tenant.resource_groups)


def _referenced_ids(tenant: "Tenant") -> set[str]:
    """Every id that some kept resource points at — these are NOT trimmable.

    Covers VM→NIC, VM→disk, NIC→subnet, NIC→publicIP references, plus subnet
    child rows' parent VNet (``managed_by``). Removing any of these would create a
    dangling reference (GEN-08 / T-02-09).
    """
    refs: set[str] = set()
    for rg in tenant.resource_groups:
        for r in rg.resources:
            props = r.properties or {}
            np_ = props.get("networkProfile", {})
            for nic in np_.get("networkInterfaces", []):
                if nic.get("id"):
                    refs.add(nic["id"])
            md = props.get("storageProfile", {}).get("osDisk", {}).get(
                "managedDisk", {}
            )
            if md.get("id"):
                refs.add(md["id"])
            for cfg in props.get("ipConfigurations", []):
                cp = cfg.get("properties", {})
                if cp.get("subnet", {}).get("id"):
                    refs.add(cp["subnet"]["id"])
                if cp.get("publicIPAddress", {}).get("id"):
                    refs.add(cp["publicIPAddress"]["id"])
            # A subnet child row is owned by its VNet (managed_by); keep the VNet.
            if r.managed_by:
                refs.add(r.managed_by)
    return refs


def _is_child_row(res: "resources.Resource") -> bool:
    """True for materialized child rows (subnets) — never trim these directly."""
    return bool(res.managed_by) or res.type.endswith("/subnets")


def _trim(
    tenant: "Tenant", target: int, ctx: SeededContext, seen_ids: set[str]
) -> None:
    """Remove leaf resources proportionally from the largest RGs until in-band.

    Only resources that nothing references (and that are not child rows) are
    eligible, so calibration never orphans a reference target (T-02-09).
    """
    while _total(tenant) > round(target * (1 + _AIM)):
        referenced = _referenced_ids(tenant)
        # Largest RGs first (proportional trim biases the biggest groups), with a
        # deterministic tiebreak on the RG id.
        candidates = sorted(
            tenant.resource_groups,
            key=lambda rg: (-len(rg.resources), rg.id),
        )
        removed_any = False
        for rg in candidates:
            # Eligible = leaf (unreferenced) AND not a child row.
            eligible = [
                r
                for r in rg.resources
                if r.id not in referenced and not _is_child_row(r)
            ]
            if not eligible:
                continue
            # Deterministic pick: remove the lexicographically-last eligible id.
            victim = max(eligible, key=lambda r: r.id)
            rg.resources.remove(victim)
            seen_ids.discard(victim.id)
            removed_any = True
            if _total(tenant) <= round(target * (1 + _AIM)):
                return
        if not removed_any:
            # Everything left is referenced/child — cannot trim further without
            # breaking integrity. Accept the (closer) total.
            return


def _pad_batch(
    tenant: "Tenant",
    n_to_add: int,
    ctx: SeededContext,
    rtd: dict[str, Any],
    templates_by_id: dict[str, dict],
    seen_ids: set[str],
) -> int:
    """Add up to ``n_to_add`` resources proportionally into existing RGs, wire.

    New resources are sampled from each RG's own template ``type_set`` (so the
    type mix is preserved), appended round-robin across RGs largest-first (growth
    tracks current shares), then references for every touched subscription are
    re-wired so the additions resolve (no dangling ids). Returns the number of
    *primary* resources actually appended (wiring may add further companions).
    """
    # Grow only RGs that ALREADY hold resources — never resurrect a
    # deliberately-empty RG (empty_share) when padding toward the scale target.
    # Padding to a target above the profile's natural total is a scale knob, not a
    # reason to populate an RG the source says is empty (that would silently defeat
    # the true-empty-RG model and refill every empty in the round-robin).
    ordered = sorted(
        (rg for rg in tenant.resource_groups if rg.resources),
        key=lambda rg: (-len(rg.resources), rg.id),
    )
    if not ordered or n_to_add <= 0:
        return 0
    touched_subs: set[Any] = set()
    added = 0
    i = 0
    stalls = 0
    while added < n_to_add:
        rg = ordered[i % len(ordered)]
        i += 1
        template = templates_by_id.get(rg.template_type)
        # Use the same GEN-04 resolution as the pipeline so padding can also grow
        # __misc__ RGs (from their type_weights) instead of stalling on the
        # sentinel type_set — keeps the mix realistic and avoids piling padding
        # onto only the standalone-template RGs.
        type_keys = (
            resources.sample_rg_types(ctx, template, rtd, 1) if template else []
        )
        if not type_keys:
            stalls += 1
            if stalls > len(ordered):
                break
            continue
        stalls = 0
        res = resources.generate_resource(
            ctx,
            subscription_id=rg.subscription_id,
            rg_name=rg.name,
            location=rg.location,
            type_key=type_keys[0],
            resource_type_distributions=rtd,
            seen_ids=seen_ids,
        )
        rg.resources.append(res)
        touched_subs.add(rg.subscription_id)
        added += 1

    # Re-wire references per touched subscription so new VMs/NICs resolve. This
    # may MINT companion NICs/disks/subnets, so the post-wire total can exceed the
    # number of primaries added — the caller's loop reconciles against the band.
    if touched_subs:
        rgs_by_sub: dict[Any, list] = {}
        for rg in tenant.resource_groups:
            rgs_by_sub.setdefault(rg.subscription_id, []).append(rg)
        for sub_id in sorted(touched_subs, key=str):
            resources.wire_references(
                ctx, sub_id, rgs_by_sub[sub_id], rtd, seen_ids
            )
    return added


def calibrate(
    tenant: "Tenant",
    target_resources: int,
    ctx: SeededContext,
    rtd: dict[str, Any],
    templates: list[dict],
    seen_ids: set[str],
) -> "Tenant":
    """Trim or pad ``tenant`` to within ±5% of ``target_resources`` (GEN-05).

    Preserves the proportional type/template mix (no collapse to one type),
    keeps every reference resolvable (T-02-09), and is deterministic under
    ``ctx`` (D-01). Mutates and returns ``tenant``.

    Padding re-wires references, which MINTS companion resources (NICs/disks/
    subnets) and can overshoot the deficit; we therefore pad in deficit-sized
    batches and reconcile in a bounded loop — padding toward the band, then
    trimming any wiring overshoot back into it.
    """
    if target_resources <= 0:
        return tenant

    templates_by_id = {t["id"]: t for t in templates}
    low = target_resources * (1 - _TOLERANCE)
    high = target_resources * (1 + _TOLERANCE)

    # Bounded reconciliation loop: each pass moves toward the band; we cap passes
    # so a pathological profile can never spin forever (it accepts the closest).
    for _ in range(12):
        total = _total(tenant)
        if low <= total <= high:
            return tenant
        if total > high:
            _trim(tenant, target_resources, ctx, seen_ids)
            # If trimming cannot reach the band (all remaining are referenced),
            # accept the closest achievable total.
            if _total(tenant) >= total:
                return tenant
        else:
            deficit = round(target_resources * (1 - _AIM)) - total
            added = _pad_batch(
                tenant, max(1, deficit), ctx, rtd, templates_by_id, seen_ids
            )
            if added == 0:
                return tenant  # no RG can grow — accept closest
    return tenant
