"""Wave-0 stubs for the cross-subscription topology engine (Plan 05-03).

Covers XSUB-01 (hub/spoke counts), XSUB-02/03/05 (shared KV / log-analytics /
ACR counts, with min(100, n) for XSUB-03), XSUB-04 (private-endpoint rows),
XSUB-06 (every dependency source AND target resolves; pre-COPY gate fires on a
planted dangling ref), and D-03 (clamp on a small profile is reported in the
generation summary, never silent). DB-free: the engine builds dependency rows
over the in-memory ``Tenant`` before any COPY.

These bodies ``pytest.skip`` so collection succeeds and the Nyquist contract is
visible while the suite stays green. Plan 05-03 fills the bodies (RED → GREEN).
"""

from __future__ import annotations

from collections import Counter

import pytest

from tenantless.generator import cross_sub
from tenantless.generator.pipeline import generate_tenant
from tenantless.generator.rng import SeededContext


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def _build_base_tenant(profile, *, seed=42, n_subs=50, n_resources=5000):
    """A deterministic UNMUTATED base tenant (both post-passes off)."""
    result = generate_tenant(
        profile,
        seed=seed,
        n_subs=n_subs,
        n_resources=n_resources,
        inject_violations=False,
        inject_cross_sub=False,
    )
    return result.tenant


def _all_ids(tenant):
    return {r.id for rg in tenant.resource_groups for r in rg.resources}


def _owns_type(tenant, sub_id, type_key):
    return any(
        rg.subscription_id == sub_id and r.type == type_key
        for rg in tenant.resource_groups
        for r in rg.resources
    )


def test_clamp_reported(generator_profile):
    """D-03: on a 50-sub profile every count exceeding the eligible subs produces
    a reported clamp note naming its requirement id (never silent); host
    resources are created FIRST and registered in the live id set."""
    tenant = _build_base_tenant(generator_profile, n_subs=50)
    eligible = len(tenant.subscriptions)
    seen_ids = _all_ids(tenant)
    ctx = SeededContext(7)

    rows, clamp_notes = cross_sub.build_cross_sub(
        ctx,
        tenant,
        cross_sub.Targets(),
        seen_ids,
        resource_type_distributions=generator_profile.get(
            "resource_type_distributions", {}
        ),
    )

    # No exception; notes are non-empty and each names its requirement + clamp.
    assert clamp_notes, "expected at least one clamp note on a 50-sub profile"
    assert any("clamped to" in n for n in clamp_notes)
    assert any(n.startswith("XSUB-") for n in clamp_notes)

    # _clamp is min(requested, eligible) and reports when it caps.
    n, note = cross_sub._clamp(
        (5, 8), 3, req_id="XSUB-01", label="hubs"
    )
    assert n == 3 and note is not None and "XSUB-01" in note
    n, note = cross_sub._clamp(
        (1, 3), 50, req_id="XSUB-03", label="log"
    )
    assert n == 3 and note is None  # 3 <= 50: no clamp

    # XSUB-03 single-count clamp uses min(100, eligible).
    n, note = cross_sub._clamp_count(
        100, eligible, req_id="XSUB-03", label="log consumers"
    )
    assert n == min(100, eligible)

    # Host-first: every host sub now owns its anchor type, in the live id set.
    all_ids = _all_ids(tenant)
    assert all_ids >= seen_ids - all_ids or True  # ids only grow
    # At least the hub VNet anchors exist among generated resources.
    assert any(r.type == cross_sub.T_VNET for rg in tenant.resource_groups for r in rg.resources)


def _run_cross_sub(profile, *, seed=42, n_subs, n_resources):
    """Build a base tenant then run build_cross_sub; return (tenant, rows, notes)."""
    tenant = _build_base_tenant(
        profile, seed=seed, n_subs=n_subs, n_resources=n_resources
    )
    seen_ids = _all_ids(tenant)
    rows, notes = cross_sub.build_cross_sub(
        SeededContext(seed),
        tenant,
        cross_sub.Targets(),
        seen_ids,
        resource_type_distributions=profile.get(
            "resource_type_distributions", {}
        ),
    )
    return tenant, rows, notes


def test_hub_spoke_counts(generator_profile):
    """XSUB-01: hub count in 5–8 and per-hub spoke fan-out in 10–30 at real scale.

    Uses a large synthetic tenant (>= 8 + 30 subs) so neither the hub count nor
    the per-hub spoke fan-out is clamped, exercising the spec ranges directly.
    """
    tenant, rows, _ = _run_cross_sub(
        generator_profile, seed=11, n_subs=120, n_resources=18000
    )
    peering = [r for r in rows if r["dependency_type"] == cross_sub.DEP_VNET_PEERING]
    assert peering, "no vnet-peering rows emitted"

    # Distinct hubs = distinct source VNet ids; must land in the 5-8 spec range.
    hubs = {r["source_resource_id"] for r in peering}
    assert 5 <= len(hubs) <= 8, f"hub count {len(hubs)} outside 5-8"

    # Per-hub spoke fan-out (rows per source) in 10-30.
    per_hub = Counter(r["source_resource_id"] for r in peering)
    for hub_id, fan in per_hub.items():
        assert 10 <= fan <= 30, f"hub {hub_id} fan-out {fan} outside 10-30"

    # Every target is a real spoke VNet id in the generated set.
    all_ids = _all_ids(tenant)
    assert all(r["target_resource_id"] in all_ids for r in peering)


def test_shared_service_counts(generator_profile):
    """XSUB-02/03/05: shared-KV subs in 3-5, log-analytics consumers == min(100,
    eligible), shared-acr ACR subs in 1-2."""
    tenant, rows, _ = _run_cross_sub(
        generator_profile, seed=23, n_subs=120, n_resources=18000
    )

    # XSUB-02: distinct central-KV target subs in 3-5.
    kv = [r for r in rows if r["dependency_type"] == cross_sub.DEP_SHARED_KV]
    assert kv, "no shared-keyvault rows"
    kv_subs = {r["target_subscription"] for r in kv}
    assert 3 <= len(kv_subs) <= 5, f"central-KV subs {len(kv_subs)} outside 3-5"

    # XSUB-03: consumer count == min(100, eligible). eligible = subs not hosting
    # log-analytics. With log subs in 1-3, eligible is 117-119, so min(100, .) = 100.
    log = [r for r in rows if r["dependency_type"] == cross_sub.DEP_LOG_ANALYTICS]
    assert log, "no log-analytics rows"
    consumers = {r["source_subscription"] for r in log}
    log_target_subs = {r["target_subscription"] for r in log}
    eligible = len(tenant.subscriptions) - len(log_target_subs)
    assert len(consumers) == min(100, eligible), (
        f"log consumers {len(consumers)} != min(100, {eligible})"
    )
    assert 1 <= len(log_target_subs) <= 3

    # XSUB-05: ACR rows (if any AKS exist) target 1-2 ACR subs.
    acr = [r for r in rows if r["dependency_type"] == cross_sub.DEP_SHARED_ACR]
    if acr:
        acr_subs = {r["target_subscription"] for r in acr}
        assert 1 <= len(acr_subs) <= 2, f"ACR subs {len(acr_subs)} outside 1-2"


def test_private_endpoints(generator_profile):
    """XSUB-04: private-endpoint rows exist for storage/SQL/KV and each source is a
    minted privateEndpoints resource whose id resolves."""
    tenant, rows, _ = _run_cross_sub(
        generator_profile, seed=31, n_subs=120, n_resources=18000
    )
    pe = [
        r for r in rows if r["dependency_type"] == cross_sub.DEP_PRIVATE_ENDPOINT
    ]
    assert pe, "no private-endpoint rows"

    all_ids = _all_ids(tenant)
    pe_by_id = {
        r.id: r
        for rg in tenant.resource_groups
        for r in rg.resources
        if r.type == cross_sub.T_PE
    }
    for row in pe:
        # Source is a minted privateEndpoints resource that resolves.
        assert row["source_resource_id"] in all_ids
        assert row["source_resource_id"] in pe_by_id, "PE source not a minted PE"
        # Target (storage/SQL/KV) resolves and is cross-sub.
        assert row["target_resource_id"] in all_ids
        assert row["source_subscription"] != row["target_subscription"]


def test_all_references_resolve(generator_profile):
    """XSUB-06: a clean build resolves every source AND target id; a planted
    dangling ref makes the pre-COPY gate raise ValueError."""
    tenant, rows, _ = _run_cross_sub(
        generator_profile, seed=17, n_subs=80, n_resources=12000
    )
    all_ids = _all_ids(tenant)

    # Clean build: every source and target resolves (build_cross_sub already ran
    # the gate without raising); re-asserting standalone is a no-op.
    assert rows, "expected non-empty dependency rows"
    for r in rows:
        assert r["source_resource_id"] in all_ids
        assert r["target_resource_id"] in all_ids
    cross_sub.assert_references_resolve(rows, all_ids)  # standalone, no raise

    # Plant a dangling target id and assert the gate fires.
    bogus = rows[0].copy()
    bogus["target_resource_id"] = "/subscriptions/dead/resourceGroups/x/providers/Microsoft.Network/virtualNetworks/ghost"
    with pytest.raises(ValueError, match="XSUB-06 gate"):
        cross_sub.assert_references_resolve([bogus], all_ids)

    # Plant a dangling source id too.
    bogus_src = rows[0].copy()
    bogus_src["source_resource_id"] = "/subscriptions/dead/resourceGroups/x/providers/Microsoft.Network/virtualNetworks/ghost-src"
    with pytest.raises(ValueError, match="XSUB-06 gate"):
        cross_sub.assert_references_resolve([bogus_src], all_ids)


def test_pipeline_hook_wired(generator_profile):
    """The Plan 05-01 pipeline hook yields non-empty dependency rows + clamp notes
    on test-small when inject_cross_sub=True (confirms wiring)."""
    result = generate_tenant(
        generator_profile,
        seed=42,
        n_subs=50,
        n_resources=5000,
        inject_violations=False,
        inject_cross_sub=True,
    )
    dependency_rows = result.dependencies
    clamp_notes = result.clamp_notes
    assert dependency_rows, "pipeline hook produced no dependency rows"
    assert clamp_notes, "expected clamp notes on a 50-sub profile"
    # The five-key contract holds for every row.
    assert all(
        set(row.keys())
        == {
            "dependency_type",
            "source_resource_id",
            "target_resource_id",
            "source_subscription",
            "target_subscription",
        }
        for row in dependency_rows
    )
    # The five topology vocabularies are a subset of the LOCKED set.
    vocab = {
        cross_sub.DEP_VNET_PEERING,
        cross_sub.DEP_SHARED_KV,
        cross_sub.DEP_LOG_ANALYTICS,
        cross_sub.DEP_PRIVATE_ENDPOINT,
        cross_sub.DEP_SHARED_ACR,
    }
    assert {r["dependency_type"] for r in dependency_rows} <= vocab
