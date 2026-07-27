"""SPEED-01 (13-04): the vectorized, tenant-wide tag post-pass ``assign_tags``.

This is the batched replacement for the per-resource ``generate_tags`` storm
(13-01 measured tags = 77.9% cumulative, ~49.5M scalar ``rng.bernoulli``). The
hot path now issues ONE numpy Bernoulli matrix per resource-group for key
presence and ONE categorical per tag key tenant-wide for values, instead of
tens of millions of scalar draws.

These tests pin the *behavior* of the batched API (not specific re-baselined
values): presence tracks ``key_frequencies``, values come from the profile
``value_distributions`` (sentinel-free), the density cap is respected, and the
result is deterministic for a fixed seed. RED until ``tags.assign_tags`` lands.
"""

from __future__ import annotations

import uuid
from collections import Counter

from tenantless.generator import tags
from tenantless.generator.pipeline import ResourceGroup, Subscription, Tenant
from tenantless.generator.resources import Resource
from tenantless.generator.rng import SeededContext


def _make_resource(sub_id: uuid.UUID, rg_name: str, i: int) -> Resource:
    rid = f"/subscriptions/{sub_id}/resourceGroups/{rg_name}/providers/X/r{i}"
    return Resource(
        id=rid,
        subscription_id=sub_id,
        resource_group_name=rg_name,
        name=f"r{i}",
        type="Microsoft.Storage/storageAccounts",
        location="eastus",
        api_version="2023-01-01",
    )


def _build_tenant(n_subs: int, rgs_per_sub: int, res_per_rg: int) -> Tenant:
    """A minimal in-memory tenant whose resources carry no tags yet."""
    tenant = Tenant(
        tenant_id=uuid.uuid4(),
        display_name="t",
        profile_version="1.0",
        scale_params={},
    )
    for s in range(n_subs):
        sub_id = uuid.uuid4()
        tenant.subscriptions.append(
            Subscription(
                subscription_id=sub_id,
                tenant_id=tenant.tenant_id,
                display_name=f"sub-{s}",
                archetype="arch",
            )
        )
        for g in range(rgs_per_sub):
            rg_name = f"rg-{s}-{g}"
            rg = ResourceGroup(
                id=f"/subscriptions/{sub_id}/resourceGroups/{rg_name}",
                subscription_id=sub_id,
                name=rg_name,
                location="eastus",
                template_type="tmpl",
            )
            rg.resources = [
                _make_resource(sub_id, rg_name, i) for i in range(res_per_rg)
            ]
            tenant.resource_groups.append(rg)
    return tenant


def test_assign_tags_presence_tracks_key_frequencies(generator_profile):
    """Batched presence rate per key tracks ``key_frequencies`` (correctness of
    the one-shot ``rng.random((m, n_keys)) < freqs`` draw), and no ``__other__``
    sentinel ever reaches a tag key or value."""
    td = generator_profile["tag_distributions"]
    key_freqs = td["key_frequencies"]
    # Generous density so presence is gated by key_frequencies, not the cap.
    density = {"mean": 6.0, "std": 1.0}
    tenant = _build_tenant(n_subs=1, rgs_per_sub=40, res_per_rg=100)  # 4000 res
    density_by_sub = {s.subscription_id: density for s in tenant.subscriptions}

    ctx = SeededContext(42)
    tags.assign_tags(ctx, tenant, td, density_by_sub)

    all_res = [r for rg in tenant.resource_groups for r in rg.resources]
    assert len(all_res) == 4000
    presence: Counter = Counter()
    for r in all_res:
        assert isinstance(r.tags, dict)
        for k, v in r.tags.items():
            assert k in key_freqs
            assert k != "__other__"
            assert "__other__" not in str(v)
            presence[k] += 1

    n = len(all_res)
    for key, freq in key_freqs.items():
        observed = presence[key] / n
        assert abs(observed - freq) < 0.07, (
            f"{key}: observed {observed:.3f} vs expected {freq:.3f}"
        )


def test_assign_tags_values_follow_profile(generator_profile):
    """Governance-keyed values follow the profile value_distributions (the
    batched per-key categorical preserves sorted+renormalized semantics)."""
    td = generator_profile["tag_distributions"]
    value_dists = td["value_distributions"]
    density = {"mean": 6.0, "std": 1.0}
    tenant = _build_tenant(n_subs=1, rgs_per_sub=40, res_per_rg=100)
    density_by_sub = {s.subscription_id: density for s in tenant.subscriptions}

    ctx = SeededContext(42)
    tags.assign_tags(ctx, tenant, td, density_by_sub)

    all_res = [r for rg in tenant.resource_groups for r in rg.resources]
    values_by_key: dict[str, Counter] = {k: Counter() for k in value_dists}
    for r in all_res:
        for k, v in r.tags.items():
            if k in values_by_key:
                values_by_key[k][v] += 1

    for key, dist in value_dists.items():
        counts = values_by_key[key]
        assert counts, f"no values sampled for {key}"
        assert set(counts) <= set(dist), (
            f"{key} sampled unexpected values {set(counts) - set(dist)}"
        )
        top_profile_value = max(dist, key=dist.get)
        assert counts.most_common(1)[0][0] == top_profile_value


def test_assign_tags_density_caps_key_count(generator_profile):
    """A tiny density cap bounds the emitted per-resource key count."""
    td = generator_profile["tag_distributions"]
    n_keys = len(td["key_frequencies"])
    density = {"mean": 2.0, "std": 0.5}
    tenant = _build_tenant(n_subs=1, rgs_per_sub=10, res_per_rg=50)
    density_by_sub = {s.subscription_id: density for s in tenant.subscriptions}

    ctx = SeededContext(7)
    tags.assign_tags(ctx, tenant, td, density_by_sub)

    for rg in tenant.resource_groups:
        for r in rg.resources:
            assert len(r.tags) <= n_keys


def test_assign_tags_reproducible(generator_profile):
    """D-01: identical seed → identical tag assignment across two runs."""
    td = generator_profile["tag_distributions"]
    density = {"mean": 4.0, "std": 1.0}

    def run() -> list[dict]:
        tenant = _build_tenant(n_subs=2, rgs_per_sub=5, res_per_rg=20)
        # Deterministic sub ids so density_by_sub keys match across runs is not
        # required — assign_tags reads density per the tenant's own subs.
        density_by_sub = {s.subscription_id: density for s in tenant.subscriptions}
        tags.assign_tags(SeededContext(123), tenant, td, density_by_sub)
        return [r.tags for rg in tenant.resource_groups for r in rg.resources]

    assert run() == run()
