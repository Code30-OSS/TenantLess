"""Source-agnostic samplers over the profile dict + an injected SeededContext.

These invert the analyzer's histogram-building extractors: where the extractor
produced ``{label: share}`` maps (sorted, normalized), the sampler sorts the
items, renormalizes (Pitfall 2), and draws via the seeded RNG. Every routine
takes the ``SeededContext`` as its first argument so all randomness flows from
the one seed (D-03).

The ``__other__`` aggregation sentinel is a profile artifact, never real data:
:func:`sample_location` maps it to a default real region (Anti-Patterns).
"""

from __future__ import annotations

from typing import Any

from .rng import SeededContext

# A default real Azure region used when a location draw lands on the
# ``__other__`` aggregation sentinel (never emit the sentinel as data).
DEFAULT_REGION = "eastus"
_OTHER = "__other__"


def normalize(weights: list[float]) -> list[float]:
    """Renormalize a probability/weight vector so it sums to 1.0 (Pitfall 2).

    Real archetype weights sum to ~0.998; every vector is routed through here
    before a draw. An all-zero vector falls back to a uniform distribution.
    """
    total = float(sum(weights))
    n = len(weights)
    if n == 0:
        return []
    if total <= 0.0:
        return [1.0 / n] * n
    return [w / total for w in weights]


def sample_archetype(ctx: SeededContext, archetypes: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick one subscription archetype weighted by its ``weight`` (GEN-02).

    Archetypes are sorted by id first for deterministic seed→outcome mapping.
    """
    ordered = sorted(archetypes, key=lambda a: a["id"])
    weights = normalize([float(a["weight"]) for a in ordered])
    return ctx.choice(ordered, weights)


def sample_template(ctx: SeededContext, templates: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick one resource-group template weighted by its ``weight`` (GEN-03).

    Templates are sorted by id first for deterministic seed→outcome mapping.
    The synthetic ``__misc__`` type-set template is sampleable but its sentinel
    type-set is handled downstream (resources are not emitted in this plan).
    """
    ordered = sorted(templates, key=lambda t: t["id"])
    weights = normalize([float(t["weight"]) for t in ordered])
    return ctx.choice(ordered, weights)


def sample_location(ctx: SeededContext, location_distribution: dict[str, float]) -> str:
    """Sample a region from an archetype's ``location_distribution`` (GEN-03).

    The ``__other__`` sentinel is mapped to :data:`DEFAULT_REGION` so it is never
    written as a location value.
    """
    if not location_distribution:
        return DEFAULT_REGION
    loc = ctx.categorical(location_distribution)
    return DEFAULT_REGION if loc == _OTHER else loc


def sample_rg_count(ctx: SeededContext, archetype: dict[str, Any]) -> int:
    """#RGs for a subscription from ``archetype.resource_group_count`` (GEN-03).

    Truncated-normal over ``{mean,std,min,max}``, floored at 1 so every
    subscription owns at least one resource group.
    """
    stats = archetype["resource_group_count"]
    n = ctx.trunc_normal(
        stats["mean"], stats["std"], lo=stats["min"], hi=stats["max"]
    )
    return max(1, n)
