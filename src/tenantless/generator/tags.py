"""Per-resource tag generation by key-frequency + value-shape (GEN-06).

This module is the structural INVERSE of
``analyzer.extractors.tags``: that extractor turned real resource tags into a
``tag_distributions`` fragment ::

    {
      "key_frequencies":     {<key>: share-of-resources},   # Bernoulli source
      "value_distributions": {<key>: {<value>: prob, ...}},  # allowlisted keys
    }

Here we invert it — for each resource:

1. **Key presence** — independent Bernoulli on ``key_frequencies[key]`` over a
   deterministic (sorted) key order. The number of present keys is then capped by
   the archetype's ``tag_density`` truncated-normal (≥0, ≤#available keys), so a
   resource never carries more keys than the archetype's density allows.
2. **Value assignment** — a key WITH a ``value_distributions[key]`` map samples
   that governance enum (categorical). Drawing the ``__other__`` sentinel mints a
   fresh synthetic value rather than echoing the sentinel (threat T-02-08). A key
   WITHOUT a value map mints a small SEEDED synthetic per-key vocabulary with a
   *dominant-one* shape (one dominant value + a few minor ones) — never a uniform
   draw and never a profile string (D-11).

Co-occurrence is **OUT OF SCOPE for v1** (Open Question 1 deferral): the profile
schema carries no per-key co-occurrence matrix (ANLZ-07 is Phase-6 pending), so
key presence is INDEPENDENT Bernoulli per key. A later phase that adds a
co-occurrence descriptor can replace the independent draw with a joint sample
without changing this module's call signature.

Privacy (threat T-02-08): the ``__other__`` sentinel is never written as a tag
key OR value — governance enums resample/mint past it, and keys without a value
map get an entirely synthetic vocabulary seeded from the RNG, so no real profile
string leaks into ``resources.tags``.

DB-free: imports neither psycopg nor duckdb; operates on the profile fragment +
the injected :class:`~tenantless.generator.rng.SeededContext`.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from .rng import SeededContext

# Sentinel emitted by the analyzer's min-bucket merge; NEVER a real tag value.
_OTHER = "__other__"

# Synthetic value-vocabulary sizing for keys WITHOUT a profile value map. A small
# bounded vocab with a clear dominant value keeps the distribution non-uniform
# (dominant-one shape) without fabricating a real identifier (D-11). Values are
# minted as ``<key>-NN`` tokens — obviously synthetic, never a profile string.
_SYNTH_VOCAB_SIZE = 5
# Geometric-ish weights: one dominant value then a decaying tail. Normalized at
# use. Length defines the synthetic vocab cardinality.
_SYNTH_WEIGHTS = (0.45, 0.25, 0.15, 0.10, 0.05)


def _present_keys(
    ctx: SeededContext, key_frequencies: dict[str, float]
) -> list[str]:
    """Independent Bernoulli over sorted keys → the present-key list.

    Sorting first makes the seed→draw mapping stable (Pitfall 3); each key is an
    independent Bernoulli on its frequency share (co-occurrence is v1-deferred).
    """
    present: list[str] = []
    for key in sorted(key_frequencies):
        if ctx.bernoulli(float(key_frequencies[key])):
            present.append(key)
    return present


def _cap_by_density(
    ctx: SeededContext, present: list[str], tag_density: dict[str, float] | None
) -> list[str]:
    """Cap the present-key list to the archetype ``tag_density`` (≤#available).

    The cap is a truncated-normal over ``{mean, std}`` (≥0, ≤len(present)). When
    fewer keys than the cap are present, all present keys are kept. The kept
    subset is drawn without replacement from the present keys, preserving the
    deterministic order so the result is reproducible.
    """
    if not present:
        return []
    if not tag_density:
        return present
    mean = float(tag_density.get("mean", len(present)))
    std = float(tag_density.get("std", 0.0))
    cap = ctx.trunc_normal(mean, std, 0, len(present))
    if cap >= len(present):
        return present
    if cap <= 0:
        return []
    # Deterministic subset: shuffle a copy via seeded permutation, take ``cap``,
    # then re-sort so the emitted tag dict order stays stable.
    order = sorted(present)
    idx = ctx.rng.permutation(len(order))[:cap]
    chosen = sorted(order[i] for i in idx)
    return chosen


def _synthetic_vocabulary(key: str) -> dict[str, float]:
    """Mint a SEEDED-independent synthetic ``{value: prob}`` vocab for ``key``.

    Dominant-one shape (one clearly leading value + a decaying tail). Values are
    ``<key>-NN`` tokens — obviously synthetic, never a profile string (D-11).
    The map is deterministic in ``key`` alone (the draw over it is what consumes
    RNG), so the same key always offers the same vocabulary.
    """
    total = sum(_SYNTH_WEIGHTS)
    return {
        f"{key}-{i + 1:02d}": _SYNTH_WEIGHTS[i] / total
        for i in range(_SYNTH_VOCAB_SIZE)
    }


def _value_for_key(
    ctx: SeededContext,
    key: str,
    value_distributions: dict[str, dict[str, float]],
) -> str:
    """Assign one tag value for ``key`` (sentinel-free).

    Governance keys (those carrying a ``value_distributions[key]`` map) sample
    that enum; drawing ``__other__`` re-draws past it, minting a synthetic token
    only if the map is sentinel-only. Keys without a map draw from a synthetic
    dominant-one vocabulary.
    """
    dist = value_distributions.get(key)
    if dist:
        non_sentinel = {k: v for k, v in dist.items() if k != _OTHER}
        if non_sentinel:
            return ctx.categorical(non_sentinel)
        # Sentinel-only governance map: mint synthetic rather than echo sentinel.
        return ctx.categorical(_synthetic_vocabulary(key))
    # No value map → seeded synthetic dominant-one vocabulary.
    return ctx.categorical(_synthetic_vocabulary(key))


def generate_tags(
    ctx: SeededContext,
    tag_distributions: dict[str, Any],
    tag_density: dict[str, float] | None = None,
) -> dict[str, str]:
    """Generate one resource's ``{key: value}`` tag map (GEN-06).

    Parameters
    ----------
    ctx:
        The single seeded RNG source (all draws flow through it, D-03).
    tag_distributions:
        The profile ``tag_distributions`` fragment: ``key_frequencies`` +
        ``value_distributions``.
    tag_density:
        The archetype ``tag_density`` ``{mean, std}`` capping the key count; when
        ``None`` every Bernoulli-present key is kept.

    Returns a ``{key: value}`` dict where keys appear ~per ``key_frequencies``
    (capped by ``tag_density``) and values follow ``value_distributions`` for
    governance keys or a synthetic dominant-one vocabulary otherwise — never the
    ``__other__`` sentinel.
    """
    key_frequencies = tag_distributions.get("key_frequencies", {}) or {}
    value_distributions = tag_distributions.get("value_distributions", {}) or {}
    if not key_frequencies:
        return {}

    present = _present_keys(ctx, key_frequencies)
    kept = _cap_by_density(ctx, present, tag_density)

    out: dict[str, str] = {}
    for key in kept:  # already deterministically ordered
        out[key] = _value_for_key(ctx, key, value_distributions)
    return out


def _value_pool(
    key: str, value_distributions: dict[str, dict[str, float]]
) -> tuple[list[str], np.ndarray]:
    """``(sorted values, renormalized probs)`` for one tag key's value draw.

    Mirrors :func:`_value_for_key` + :meth:`SeededContext.categorical` EXACTLY
    (governance enum without the sentinel, else a synthetic dominant-one vocab;
    items sorted then the weight vector renormalized) so the batched per-key
    ``rng.choice`` below is value-for-value equivalent to the scalar path.
    """
    dist = value_distributions.get(key)
    if dist:
        non_sentinel = {k: v for k, v in dist.items() if k != _OTHER}
        source = non_sentinel if non_sentinel else _synthetic_vocabulary(key)
    else:
        source = _synthetic_vocabulary(key)
    items = sorted(source.items())
    vals = [k for k, _ in items]
    probs = np.array([p for _, p in items], dtype=float)
    probs = probs / probs.sum()
    return vals, probs


def assign_tags(
    ctx: SeededContext,
    tenant: Any,
    tag_distributions: dict[str, Any],
    density_by_sub: dict[Any, dict[str, float] | None],
) -> None:
    """Vectorized tenant-wide tag assignment (GEN-06, SPEED-01).

    Batched replacement for the per-resource :func:`generate_tags` storm that
    13-01 measured as the dominant generator hotspot (tags = 77.9% cumulative;
    ~49.5M scalar ``rng.bernoulli``). Two batched phases, both driven entirely
    by the injected per-substream ``ctx`` (the ``tags_ss`` post-pass context):

    1. **Key presence + density cap — ONE Bernoulli matrix per resource-group.**
       Every resource in an RG shares one subscription → one ``tag_density``, so
       ``rng.random((m, n_keys)) < freqs`` decides presence for all ``m``
       resources at once over ``keys = sorted(key_frequencies)``. The density
       cap reuses one batched ``rng.normal`` draw per RG, truncated per resource
       exactly like :func:`_cap_by_density`.
    2. **Values — ONE categorical per tag key, tenant-wide.** For each key, the
       resources that kept it draw their values in a single
       ``rng.choice(values, size=k, p=probs)`` over the same sorted+renormalized
       pool :func:`_value_for_key` would use — collapsing ~676K scalar
       categorical calls into one per key.

    Semantically identical to calling :func:`generate_tags` per resource (same
    sorted-key order, same value pools, ``__other__``-sentinel-free); only the
    RNG dispatch is batched. Mutates each resource's ``.tags`` in place.

    ``density_by_sub`` maps ``subscription_id`` → its archetype ``tag_density``
    ``{mean, std}`` (or ``None`` to keep every present key).
    """
    key_frequencies = tag_distributions.get("key_frequencies", {}) or {}
    value_distributions = tag_distributions.get("value_distributions", {}) or {}

    all_res = [res for rg in tenant.resource_groups for res in rg.resources]
    if not key_frequencies or not all_res:
        for res in all_res:
            res.tags = {}
        return

    keys = sorted(key_frequencies)  # deterministic order (Pitfall 3)
    n_keys = len(keys)
    freqs = np.array(
        [min(1.0, max(0.0, float(key_frequencies[k]))) for k in keys],
        dtype=float,
    )

    # ---- Phase 1: key presence + density cap, batched per resource-group ---- #
    # kept_per_res is aligned to the flat all_res order (same RG/resource walk).
    kept_per_res: list[list[str]] = []
    for rg in tenant.resource_groups:
        m = len(rg.resources)
        if m == 0:
            continue
        # ONE batched Bernoulli draw replaces m × n_keys scalar rng.bernoulli.
        present_mask = ctx.rng.random((m, n_keys)) < freqs
        density = density_by_sub.get(rg.subscription_id)
        caps = None
        if density:
            mean = float(density.get("mean", n_keys))
            std = float(density.get("std", 0.0))
            # One batched normal draw → per-resource truncated cap, mirroring
            # trunc_normal(mean, std, 0, len(present)) (round, clamp [0, hi]).
            caps = np.round(ctx.rng.normal(mean, max(std, 1e-9), size=m))
        for r in range(m):
            present = [keys[j] for j in np.flatnonzero(present_mask[r])]
            if caps is None or not present:
                kept_per_res.append(present)
                continue
            cap = int(min(len(present), max(0, caps[r])))
            if cap >= len(present):
                kept_per_res.append(present)
            elif cap <= 0:
                kept_per_res.append([])
            else:
                # Deterministic subset without replacement, re-sorted — mirrors
                # _cap_by_density's permutation-then-sort.
                pick = ctx.rng.permutation(len(present))[:cap]
                kept_per_res.append(sorted(present[j] for j in pick))

    # ---- Phase 2: tag values, ONE categorical per key tenant-wide ---------- #
    out: list[dict[str, str]] = [{} for _ in all_res]
    holders: dict[str, list[int]] = {k: [] for k in keys}
    for i, kept in enumerate(kept_per_res):
        for k in kept:
            holders[k].append(i)
    for k in keys:  # sorted-key order → each tag dict built in stable order
        idxs = holders[k]
        if not idxs:
            continue
        vals, probs = _value_pool(k, value_distributions)
        draws = ctx.rng.choice(vals, size=len(idxs), p=probs)
        for pos, i in enumerate(idxs):
            out[i][k] = str(draws[pos])

    for i, res in enumerate(all_res):
        res.tags = out[i]
