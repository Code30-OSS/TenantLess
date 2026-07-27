"""Governance-violation injection engine tests (Plan 05-02).

Covers VIOL-01 (injection records + mutates), VIOL-02..08 (the 18-row mutation
table + per-type rates), VIOL-09 (independent stacking on one resource), and
VIOL-10 (reproducible at a fixed (profile, seed)). DB-free: the engine operates
on the in-memory ``Tenant`` produced by ``generate_tenant`` before any COPY.
"""

from __future__ import annotations

import math
from collections import Counter

import pytest

from tenantless.generator import resources, violations
from tenantless.generator.pipeline import generate_tenant
from tenantless.generator.rng import SeededContext


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def _build_tenant(profile, *, seed=42, n_subs=40, n_resources=5000):
    """A deterministic, UNMUTATED base tenant (both post-passes off)."""
    result = generate_tenant(
        profile,
        seed=seed,
        n_subs=n_subs,
        n_resources=n_resources,
        inject_violations=False,
        inject_cross_sub=False,
    )
    return result.tenant


def _all_res(tenant):
    return [r for rg in tenant.resource_groups for r in rg.resources]


def _first_of_type(tenant, type_key, *, predicate=None):
    for r in _all_res(tenant):
        if r.type == type_key and (predicate is None or predicate(r)):
            return r
    return None


# For each flip/delete code: the eligible type + an assertion the targeted ARM
# path holds the exact insecure value after a forced inject.
_FLIP_DELETE_CASES = {
    "STORAGE_PUBLIC_ACCESS": (
        resources.T_STORAGE,
        lambda r: r.properties.get("allowBlobPublicAccess") is True,
    ),
    "STORAGE_HTTP_ALLOWED": (
        resources.T_STORAGE,
        lambda r: r.properties.get("supportsHttpsTrafficOnly") is False,
    ),
    "STORAGE_OLD_TLS": (
        resources.T_STORAGE,
        lambda r: r.properties.get("minimumTlsVersion") == "TLS1_0",
    ),
    "KV_NO_SOFT_DELETE": (
        resources.T_KV,
        lambda r: r.properties.get("enableSoftDelete") is False,
    ),
    "KV_NO_PURGE_PROTECT": (
        resources.T_KV,
        lambda r: r.properties.get("enablePurgeProtection") is False,
    ),
    "TAG_MISSING_ENV": (None, lambda r: "environment" not in r.tags),
    "TAG_MISSING_OWNER": (None, lambda r: "owner" not in r.tags),
    "TAG_MISSING_COSTCENTER": (None, lambda r: "costCenter" not in r.tags),
    "SQL_NO_TDE": (
        resources.T_SQLDB,
        lambda r: r.properties.get("transparentDataEncryption") == "Disabled",
    ),
    "AKS_RBAC_DISABLED": (
        resources.T_AKS,
        lambda r: r.properties.get("enableRBAC") is False,
    ),
}

# Insert/invent/wire codes: eligible type + post-mutation assertion.
_INSERT_CASES = {
    "STORAGE_NO_ENCRYPTION": (
        resources.T_STORAGE,
        lambda r: r.properties["encryption"]["services"]["blob"]["enabled"]
        is False,
    ),
    "NSG_OPEN_SSH": (
        resources.T_NSG,
        lambda r: any(
            x["name"] == "AllowSSH-Inbound"
            and x["properties"]["destinationPortRange"] == "22"
            and x["properties"]["sourceAddressPrefix"] == "*"
            for x in r.properties["securityRules"]
        ),
    ),
    "NSG_OPEN_RDP": (
        resources.T_NSG,
        lambda r: any(
            x["name"] == "AllowRDP-Inbound"
            and x["properties"]["destinationPortRange"] == "3389"
            for x in r.properties["securityRules"]
        ),
    ),
    "NSG_OPEN_ALL": (
        resources.T_NSG,
        lambda r: any(
            x["name"] == "AllowAll-Inbound"
            and x["properties"]["protocol"] == "*"
            and x["properties"]["destinationPortRange"] == "*"
            for x in r.properties["securityRules"]
        ),
    ),
    "SQL_NO_AUDIT": (
        resources.T_SQLSRV,
        lambda r: r.properties["auditingSettings"]["state"] == "Disabled",
    ),
    "DISK_UNENCRYPTED": (
        resources.T_DISK,
        lambda r: r.properties["encryption"]["enabled"] is False,
    ),
    "VM_NO_BACKUP": (
        resources.T_VM,
        lambda r: r.properties.get("_noRecoveryServicesLink") is True,
    ),
    "VM_PUBLIC_IP": (resources.T_VM, None),  # asserted specially below
}


@pytest.mark.parametrize(
    "code", sorted({**_FLIP_DELETE_CASES, **_INSERT_CASES})
)
def test_mutation_table(generator_profile, code):
    """VIOL-02..08: each code mutates the correct ARM path to its insecure value.

    Forces the violation (rate 1.0) on its eligible type and asserts the targeted
    path holds the exact insecure value from the RESEARCH 18-row mutation table.
    """
    cases = {**_FLIP_DELETE_CASES, **_INSERT_CASES}
    eligible_type, check = cases[code]
    spec = violations.VIOLATION_REGISTRY[code]
    tenant = _build_tenant(generator_profile)

    # For TAG_* the eligible type is "any taggable WITH the key": pick a resource
    # that currently has the key. Otherwise pick by the spec's eligible type.
    if eligible_type is None:
        predicate = spec.eligible_predicate
        target = _first_of_type(
            tenant, None, predicate=None
        )
        target = next(
            (r for r in _all_res(tenant) if predicate is None or predicate(r)),
            None,
        )
    else:
        target = _first_of_type(tenant, eligible_type)
    assert target is not None, f"no eligible resource for {code}"

    rows = violations.inject(
        SeededContext(1),
        tenant,
        {code: spec},
        rates={code: 1.0},
    )
    assert rows, f"forced inject produced no rows for {code}"
    assert all(row["violation_type"] == code for row in rows)

    if code == "VM_PUBLIC_IP":
        # The VM's NIC must carry a resolvable publicIPAddress.id.
        hit_ids = {row["resource_id"] for row in rows}
        hit_details = [row["detail"] for row in rows]
        assert all("public_ip_id" in d and d["public_ip_id"] for d in hit_details)
        assert hit_ids  # at least one VM wired
        return

    # Every hit resource now satisfies the insecure-value check.
    hit_ids = {row["resource_id"] for row in rows}
    hit_res = [r for r in _all_res(tenant) if r.id in hit_ids]
    assert hit_res
    assert all(check(r) for r in hit_res), f"{code} did not apply insecure value"


def test_injection_records_and_mutates(generator_profile):
    """VIOL-01: each hit appends exactly one row {resource_id, violation_type,
    severity, detail} AND mutates the resource; detail carries the mutated path."""
    tenant = _build_tenant(generator_profile)
    # Force STORAGE_PUBLIC_ACCESS on every storage account.
    code = "STORAGE_PUBLIC_ACCESS"
    spec = violations.VIOLATION_REGISTRY[code]
    storages = [r for r in _all_res(tenant) if r.type == resources.T_STORAGE]
    assert storages

    rows = violations.inject(
        SeededContext(3), tenant, {code: spec}, rates={code: 1.0}
    )
    # One row per eligible storage account.
    assert len(rows) == len(storages)
    for row in rows:
        assert set(row.keys()) == {
            "resource_id",
            "violation_type",
            "severity",
            "detail",
        }
        assert row["violation_type"] == code
        assert row["severity"] == "High"
        assert isinstance(row["detail"], dict)
        assert row["detail"]["path"] == "properties.allowBlobPublicAccess"
    # Every storage account was mutated in place.
    assert all(s.properties.get("allowBlobPublicAccess") is True for s in storages)


# --------------------------------------------------------------------------- #
# Task 3: rate fidelity, stacking, reproducibility, pipeline wiring.
# --------------------------------------------------------------------------- #


def _wilson_interval(k, n, z=2.576):
    """Wilson score interval (99% by default) for k successes in n trials."""
    if n == 0:
        return (0.0, 1.0)
    phat = k / n
    denom = 1 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    margin = (
        z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    )
    return (center - margin, center + margin)


def test_rates_match_spec(generator_profile):
    """VIOL-02..08: observed per-type rate ≈ spec rate (Wilson 99% interval).

    Per-type denominator; TAG_* denominator = resources that HAD the key.
    """
    tenant = _build_tenant(
        generator_profile, seed=99, n_subs=120, n_resources=20000
    )
    all_res = _all_res(tenant)
    rows = violations.inject(
        SeededContext(99), tenant, violations.VIOLATION_REGISTRY
    )
    by_code = Counter(row["violation_type"] for row in rows)

    for code, spec in violations.VIOLATION_REGISTRY.items():
        # Denominator = eligible population per the spec (per-type; TAG_* = key-present).
        # Snapshot BEFORE mutation is unavailable post-hoc, so recompute on the
        # mutated tenant: flips/inserts don't change type or (for non-TAG) tag keys.
        if spec.eligible_predicate is not None:
            # TAG_* — the key was deleted on hits, so add hits back to the
            # eligible denominator to recover "had the key" population.
            denom = sum(1 for r in all_res if spec.eligible_predicate(r))
            denom += by_code.get(code, 0)
        else:
            denom = sum(1 for r in all_res if r.type == spec.eligible_type)
        assert denom > 0, f"no eligible population for {code}"
        k = by_code.get(code, 0)
        lo, hi = _wilson_interval(k, denom)
        assert lo <= spec.default_rate <= hi, (
            f"{code}: observed {k}/{denom}={k / denom:.4f} excludes spec "
            f"rate {spec.default_rate} (Wilson 99% [{lo:.4f},{hi:.4f}])"
        )


def test_independent_stacking(generator_profile):
    """VIOL-09: at least one resource id carries >=2 distinct violation rows."""
    tenant = _build_tenant(
        generator_profile, seed=7, n_subs=120, n_resources=20000
    )
    rows = violations.inject(
        SeededContext(7), tenant, violations.VIOLATION_REGISTRY
    )
    counts = Counter(row["resource_id"] for row in rows)
    assert counts, "no violations injected"
    assert counts.most_common(1)[0][1] >= 2, "no resource stacked >=2 violations"


def test_reproducible(generator_profile):
    """VIOL-10: two runs at the same (profile, seed) → identical row lists."""

    def _run():
        tenant = _build_tenant(
            generator_profile, seed=42, n_subs=60, n_resources=8000
        )
        return violations.inject(
            SeededContext(42), tenant, violations.VIOLATION_REGISTRY
        )

    first = _run()
    second = _run()
    assert first == second
    assert first, "expected non-empty violation rows"


def test_pipeline_hook_wired(generator_profile):
    """The Plan 05-01 hook yields non-empty violation rows when toggled on."""
    violation_rows = generate_tenant(
        generator_profile,
        seed=42,
        n_subs=40,
        n_resources=5000,
        inject_violations=True,
        inject_cross_sub=False,
    ).violations
    assert violation_rows, "pipeline hook produced no violation rows"
    assert all(
        set(row.keys())
        == {"resource_id", "violation_type", "severity", "detail"}
        for row in violation_rows
    )
