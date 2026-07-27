"""GEN-04 misc-bucket generation: the ``__misc__`` privacy template must carry
its resource-type MASS (via ``type_weights``) and reproduce genuine empties (via
``empty_share``) instead of generating every folded RG empty.

Regression guard for the ~55%-empty-RG / single-type-pile artifact: a template
whose ``type_set`` is only the ``["__misc__"]`` sentinel used to sample NO types
(empty RG), which then forced calibrate to over-pad the standalone-template RGs
into unnatural single-type piles. DB-free.
"""

from __future__ import annotations

from numpy.random import SeedSequence

from tenantless.generator import arm, pipeline, resources
from tenantless.generator.rng import SeededContext

STOR = arm.canonical_type("Microsoft.Storage/storageAccounts")
KV = arm.canonical_type("Microsoft.KeyVault/vaults")
VM = arm.canonical_type("Microsoft.Compute/virtualMachines")

RTD = {
    STOR: {"frequency": 0.5, "property_distributions": {}},
    KV: {"frequency": 0.3, "property_distributions": {}},
    VM: {"frequency": 0.2, "property_distributions": {}},
}


def _ctx(seed: int = 0) -> SeededContext:
    return SeededContext.from_seed_sequence(SeedSequence(seed))


def _sub(ctx: SeededContext) -> "pipeline.Subscription":
    return pipeline.Subscription(
        subscription_id=ctx.uuid4(),
        tenant_id=ctx.uuid4(),
        display_name="sub-x",
        archetype="arch-0",
    )


def _rg(sub, name: str) -> "pipeline.ResourceGroup":
    return pipeline.ResourceGroup(
        id=f"/subscriptions/{sub.subscription_id}/resourceGroups/{name}",
        subscription_id=sub.subscription_id,
        name=name,
        location="westeurope",
        template_type="__misc__",
    )


# --------------------------------------------------------------------------- #
# sample_rg_types precedence: real type_set → type_weights → global fallback
# --------------------------------------------------------------------------- #


def test_real_type_set_path_unchanged():
    """A real (non-sentinel) type_set still routes through sample_type_mix."""
    ctx = _ctx()
    out = resources.sample_rg_types(ctx, {"type_set": [KV]}, RTD, 5)
    assert out == [KV] * 5


def test_sentinel_with_type_weights_samples_the_histogram():
    """__misc__ with a type_weights histogram generates real types (never empty,
    never the sentinel), dominated by the heaviest weight."""
    ctx = _ctx()
    template = {"type_set": ["__misc__"], "type_weights": {STOR: 0.7, KV: 0.3}}
    out = resources.sample_rg_types(ctx, template, RTD, 300)
    assert out, "a misc RG with a histogram must NOT be empty"
    assert "__misc__" not in out
    assert set(out) <= {STOR, KV}
    frac_storage = out.count(STOR) / len(out)
    assert 0.6 < frac_storage < 0.8  # ~0.7


def test_sentinel_without_weights_falls_back_to_global_rtd():
    """Guardrail B: a sentinel-only template with NO histogram still samples from
    the global resource_type_distributions rather than generating empty."""
    ctx = _ctx()
    out = resources.sample_rg_types(ctx, {"type_set": ["__misc__"]}, RTD, 100)
    assert out, "sentinel-only template must fall back, never empty"
    assert "__misc__" not in out
    assert set(out) <= set(RTD)


# --------------------------------------------------------------------------- #
# empty_share: reproduce genuine empties at the source rate
# --------------------------------------------------------------------------- #


def test_empty_share_one_always_empty():
    ctx = _ctx()
    sub = _sub(ctx)
    template = {
        "type_set": ["__misc__"],
        "type_weights": {STOR: 1.0},
        "resource_count": {"mean": 5, "std": 1, "min": 1, "max": 10},
        "empty_share": 1.0,
    }
    out = pipeline._generate_rg_resources(ctx, sub, _rg(sub, "rg-0"), template, RTD, set())
    assert out == []


def test_no_empty_share_populates_from_weights():
    ctx = _ctx()
    sub = _sub(ctx)
    template = {
        "type_set": ["__misc__"],
        "type_weights": {STOR: 1.0},
        "resource_count": {"mean": 5, "std": 0.1, "min": 1, "max": 10},
    }
    out = pipeline._generate_rg_resources(ctx, sub, _rg(sub, "rg-1"), template, RTD, set())
    assert out, "a misc RG with no empty_share and a histogram must be populated"
    assert all(r.type == STOR for r in out)


def test_trunc_lognormal_preserves_mean_not_inflated():
    """A heavy-tailed count (mean 28.7, std 242 — the __misc__ RG-size bucket)
    sampled as a lognormal PRESERVES the arithmetic mean (so the total lands near
    target) and stays right-skewed — unlike trunc_normal, whose clamped negatives
    inflate the realized mean ~5x and blow the total past target (which then forces
    calibrate to empty the small RGs)."""
    n = 20000
    ctx_ln = _ctx(7)
    lognrm = [ctx_ln.trunc_lognormal(28.7, 242.0, 1, 12148) for _ in range(n)]
    ctx_nm = _ctx(7)
    normal = [max(1, ctx_nm.trunc_normal(28.7, 242.0, 1, 12148)) for _ in range(n)]

    assert all(d >= 1 for d in lognrm)
    ln_mean = sum(lognrm) / n
    nm_mean = sum(normal) / n
    # Mean preserved near the target 28.7 (heavy-tail sampling tolerance)...
    assert 15 < ln_mean < 55
    # ...and dramatically below the clamped-normal's inflated mean.
    assert ln_mean < nm_mean * 0.5
    # Right-skewed: the median sits well below the mean.
    assert sorted(lognrm)[n // 2] < ln_mean


def test_low_variance_template_still_uses_normal_path():
    """A standalone template (std < mean) is unchanged — count stays near mean,
    never the lognormal path (keeps existing determinism-sensitive output)."""
    ctx = _ctx()
    sub = _sub(ctx)
    template = {
        "type_set": [STOR],
        "resource_count": {"mean": 4, "std": 1, "min": 1, "max": 8},
    }
    counts = []
    for i in range(200):
        out = pipeline._generate_rg_resources(ctx, sub, _rg(sub, f"rg-{i}"), template, RTD, set())
        counts.append(len(out))
    avg = sum(counts) / len(counts)
    assert 3 < avg < 5  # tight around the mean, no heavy-tail blow-up


def test_empty_share_reproduces_source_rate():
    """Over many misc RGs, ~empty_share of them come out empty (the rest populated)."""
    ctx = _ctx()
    sub = _sub(ctx)
    template = {
        "type_set": ["__misc__"],
        "type_weights": {STOR: 1.0},
        "resource_count": {"mean": 3, "std": 0.1, "min": 1, "max": 5},
        "empty_share": 0.5,
    }
    n, empties = 400, 0
    for i in range(n):
        out = pipeline._generate_rg_resources(ctx, sub, _rg(sub, f"rg-{i}"), template, RTD, set())
        if not out:
            empties += 1
    assert 0.4 < empties / n < 0.6
