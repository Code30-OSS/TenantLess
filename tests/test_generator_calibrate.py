"""GEN-05 scale-calibration contract.

After initial generation the resource total is trimmed or padded until it lands
within ±5% of the ``--resources`` target, PRESERVING the archetype/template/type
mix (proportional adjustment) and WITHOUT orphaning any reference target (D-06).
"""

from __future__ import annotations

from tenantless.generator.pipeline import generate_tenant


def _total_resources(tenant) -> int:
    return sum(len(rg.resources) for rg in tenant.resource_groups)


def _all_resource_ids(tenant) -> set[str]:
    return {r.id for rg in tenant.resource_groups for r in rg.resources}


def _reference_ids(tenant) -> list[str]:
    """Collect every intra-tenant reference id a resource points at."""
    refs: list[str] = []
    for rg in tenant.resource_groups:
        for r in rg.resources:
            props = r.properties or {}
            np_ = props.get("networkProfile", {})
            for nic in np_.get("networkInterfaces", []):
                if nic.get("id"):
                    refs.append(nic["id"])
            sp = props.get("storageProfile", {})
            osd = sp.get("osDisk", {})
            md = osd.get("managedDisk", {})
            if md.get("id"):
                refs.append(md["id"])
            for cfg in props.get("ipConfigurations", []):
                cp = cfg.get("properties", {})
                if cp.get("subnet", {}).get("id"):
                    refs.append(cp["subnet"]["id"])
                if cp.get("publicIPAddress", {}).get("id"):
                    refs.append(cp["publicIPAddress"]["id"])
    return refs


def test_within_5_percent(generator_profile):
    """GEN-05: the calibrated resource total is within +/-5% of the --resources
    target, preserving distribution shape (D-06)."""
    target = 1500
    tenant = generate_tenant(
        generator_profile, seed=42, n_subs=20, n_resources=target
    ).tenant
    total = _total_resources(tenant)
    assert abs(total - target) / target <= 0.05, (
        f"total {total} not within 5% of target {target}"
    )


def test_calibration_preserves_type_mix(generator_profile):
    """GEN-05: calibration does not collapse the tenant to a single type — the
    proportional type mix survives the trim/pad (shape preserved)."""
    tenant = generate_tenant(
        generator_profile, seed=42, n_subs=20, n_resources=1500
    ).tenant
    types = {r.type for rg in tenant.resource_groups for r in rg.resources}
    # A realistic tenant spans many resource types; collapsing to ~1 would mean
    # the mix was distorted to hit the number.
    assert len(types) >= 5


def test_calibration_keeps_references_resolvable(generator_profile):
    """GEN-08 must still hold post-calibration (T-02-09): every reference id a
    kept resource points at still exists as a kept resource id."""
    tenant = generate_tenant(
        generator_profile, seed=42, n_subs=20, n_resources=1500
    ).tenant
    ids = _all_resource_ids(tenant)
    for ref in _reference_ids(tenant):
        assert ref in ids, f"dangling reference after calibration: {ref}"


def test_calibration_reproducible(generator_profile):
    """D-01: identical (profile, seed, target) → identical calibrated total."""
    a = generate_tenant(generator_profile, seed=42, n_subs=20, n_resources=1500).tenant
    b = generate_tenant(generator_profile, seed=42, n_subs=20, n_resources=1500).tenant
    assert _total_resources(a) == _total_resources(b)
    assert _all_resource_ids(a) == _all_resource_ids(b)
