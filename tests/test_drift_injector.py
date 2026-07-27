"""Deterministic drift-injector tests (Phase 55, D-12/D-14).

Covers: seed reproducibility (same seed -> identical injected rows + resource ids),
the agw-nsg N/M lock split (the c01 relational-existence hero), one-per-policy
placement of the un-expressible drift items, and the fail-loud contract when no
target population exists (CLAUDE.md: no silent skips). DB-free — the injector
operates on the in-memory ``Tenant`` produced by ``generate_tenant`` before COPY.
"""

from __future__ import annotations

from pathlib import Path

import orjson
import pytest

from tenantless.generator import drift_injector as di, resources
from tenantless.generator.pipeline import Tenant, generate_tenant

_PROMO_PROFILE = (
    Path(__file__).resolve().parents[1] / "profiles" / "promotion-demo.json"
)


@pytest.fixture
def promo_profile() -> dict:
    return orjson.loads(_PROMO_PROFILE.read_bytes())


def _build(profile, *, seed=55, n_subs=16, n_resources=520):
    return generate_tenant(
        profile,
        seed=seed,
        n_subs=n_subs,
        n_resources=n_resources,
        inject_violations=True,
        inject_cross_sub=True,
        inject_cost=False,
        inject_identity=False,
    )


def _all(tenant):
    return [r for rg in tenant.resource_groups for r in rg.resources]


def test_injected_rows_reproducible_at_fixed_seed(promo_profile):
    """Same (tenant, seed) -> byte-identical injected violation rows (D-14)."""
    r1 = _build(promo_profile)
    rows1 = di.inject_drift(r1.tenant, r1.dependencies, seed=di.DRIFT_SEED)

    r2 = _build(promo_profile)
    rows2 = di.inject_drift(r2.tenant, r2.dependencies, seed=di.DRIFT_SEED)

    assert rows1 == rows2
    # injected resource ids (names are seed-derived) are identical too.
    ids1 = sorted(row["resource_id"] for row in rows1)
    ids2 = sorted(row["resource_id"] for row in rows2)
    assert ids1 == ids2


def test_agw_nsg_cluster_lock_split(promo_profile):
    """N ``*-agw-nsg`` NSGs, M with a ``<name>-lock`` sibling, N-M without (c01)."""
    result = _build(promo_profile)
    rows = di.inject_drift(
        result.tenant, result.dependencies, seed=di.DRIFT_SEED
    )
    allres = _all(result.tenant)

    agw = [r for r in allres if r.type == resources.T_NSG and "-agw-nsg" in r.name]
    lock_names = {r.name for r in allres if r.type == di.T_LOCK}
    locked = [n for n in agw if f"{n.name}-lock" in lock_names]
    unlocked = [n for n in agw if f"{n.name}-lock" not in lock_names]

    assert len(agw) == di.AGW_NSG_TOTAL
    assert len(locked) == di.AGW_NSG_LOCKED
    assert len(unlocked) == di.AGW_NSG_TOTAL - di.AGW_NSG_LOCKED
    # one c01 violation row per UNLOCKED agw-nsg (relational-existence gap).
    missing = [r for r in rows if r["violation_type"] == "AGW_NSG_MISSING_LOCK"]
    assert len(missing) == len(unlocked)
    # lock siblings carry the exact satisfier shape the governance engine matches.
    for lname in lock_names:
        assert lname.endswith("-agw-nsg-lock")


def test_one_violating_resource_per_unexpressible_policy(promo_profile):
    """Exactly one custom role / wrong-region / off-name / rogue-SKU resource."""
    result = _build(promo_profile)
    di.inject_drift(result.tenant, result.dependencies, seed=di.DRIFT_SEED)
    allres = _all(result.tenant)

    roledefs = [r for r in allres if r.type == di.T_ROLE_DEFINITION]
    assert len(roledefs) == 1
    actions = roledefs[0].properties["permissions"][0]["actions"]
    assert di.CUSTOM_ROLE_ACTION in actions

    wrong = [r for r in allres if r.location == di.WRONG_REGION]
    assert len(wrong) == 1

    off = [r for r in allres if r.name == di.OFF_NAMING_NAME]
    assert len(off) == 1

    rogue = [r for r in allres if (r.sku or {}).get("name") == di.ROGUE_SKU_NAME]
    assert len(rogue) == 1


def test_wrong_region_resource_lands_on_a_hub_vnet_rg(promo_profile):
    """The r06 resource attaches to the RG of a hub VNet (centrality showcase)."""
    result = _build(promo_profile)
    di.inject_drift(result.tenant, result.dependencies, seed=di.DRIFT_SEED)
    allres = _all(result.tenant)

    peering = [
        d
        for d in result.dependencies
        if d.get("dependency_type") == "vnet-peering"
    ]
    assert peering, "profile must generate hub-spoke peering (D-13)"
    hub_vnet_ids = {d["source_resource_id"] for d in peering}
    hub_rgs = {
        (v.subscription_id, v.resource_group_name)
        for v in allres
        if v.type == resources.T_VNET and v.id in hub_vnet_ids
    }
    wrong = next(r for r in allres if r.location == di.WRONG_REGION)
    assert (wrong.subscription_id, wrong.resource_group_name) in hub_rgs


def test_fails_loud_when_no_target_population():
    """An empty tenant raises rather than silently skipping (no-silent-skip rule)."""
    empty = Tenant(
        tenant_id=__import__("uuid").uuid4(),
        display_name="empty",
        profile_version="1.2",
        scale_params={},
    )
    with pytest.raises(RuntimeError):
        di.inject_drift(empty, [], seed=di.DRIFT_SEED)
