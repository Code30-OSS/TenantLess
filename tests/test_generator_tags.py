"""GEN-06 tag-generation contract.

Tag presence follows ``key_frequencies`` (independent Bernoulli per key, capped
by the archetype ``tag_density``); values come from ``value_distributions`` where
the profile carries one, else a SEEDED synthetic per-key vocabulary with a
dominant-one shape — never uniform and never the ``__other__`` sentinel.
"""

from __future__ import annotations

from collections import Counter

from tenantless.generator.rng import SeededContext


def _profile_tags(generator_profile):
    return generator_profile["tag_distributions"]


def test_tag_value_distribution(generator_profile):
    """GEN-06: tag keys are present roughly per key_frequencies, and values are
    drawn from value_distributions (not uniform); the "__other__" sentinel is
    never written as a tag value."""
    from tenantless.generator import tags

    td = _profile_tags(generator_profile)
    key_freqs = td["key_frequencies"]
    value_dists = td["value_distributions"]
    # A generous density so presence is gated by key_frequencies, not the cap.
    tag_density = {"mean": 6.0, "std": 1.0}

    ctx = SeededContext(42)
    n = 4000
    presence = Counter()
    values_by_key: dict[str, Counter] = {k: Counter() for k in key_freqs}

    for _ in range(n):
        resource_tags = tags.generate_tags(ctx, td, tag_density)
        # Never emit the sentinel as a key or a value.
        for k, v in resource_tags.items():
            assert k != "__other__"
            assert v != "__other__"
            assert "__other__" not in str(v)
            presence[k] += 1
            if k in values_by_key:
                values_by_key[k][v] += 1

    # 1. Presence rate tracks key_frequencies (within a tolerance band).
    for key, freq in key_freqs.items():
        observed = presence[key] / n
        assert abs(observed - freq) < 0.07, (
            f"{key}: observed {observed:.3f} vs expected {freq:.3f}"
        )

    # 2. Governance-keyed values follow the profile value_distributions and are
    #    NOT uniform — the dominant profile value must dominate the samples.
    for key, dist in value_dists.items():
        counts = values_by_key[key]
        assert counts, f"no values sampled for {key}"
        # Only values from the profile enum appear (no minted noise for these).
        assert set(counts) <= set(dist), (
            f"{key} sampled unexpected values {set(counts) - set(dist)}"
        )
        top_profile_value = max(dist, key=dist.get)
        top_sampled_value = counts.most_common(1)[0][0]
        assert top_sampled_value == top_profile_value, (
            f"{key}: sampled mode {top_sampled_value} != profile mode "
            f"{top_profile_value}"
        )
        # Non-uniform: the most common value's share is well above 1/cardinality.
        share = counts[top_sampled_value] / sum(counts.values())
        assert share > (1.0 / len(dist)), f"{key} values look uniform"

    # 3. A key WITHOUT a value map ("project"/"createdBy") still gets values from
    #    a synthetic, non-uniform vocabulary (one dominant value), never empty.
    for key in key_freqs:
        if key in value_dists:
            continue
        counts = values_by_key[key]
        assert counts, f"no synthetic values minted for {key}"
        assert len(counts) > 1, f"{key} synthetic vocab is degenerate"
        top_share = counts.most_common(1)[0][1] / sum(counts.values())
        # Dominant-one shape: the top value clearly leads (not uniform).
        assert top_share > (1.5 / len(counts)), (
            f"{key} synthetic vocab looks uniform (top_share {top_share:.3f})"
        )


def test_tag_density_caps_key_count(generator_profile):
    """GEN-06: the per-resource key count is capped by the archetype tag_density,
    never exceeding the number of available keys."""
    from tenantless.generator import tags

    td = _profile_tags(generator_profile)
    n_keys = len(td["key_frequencies"])
    # A tiny density cap should bound the emitted key count.
    tag_density = {"mean": 2.0, "std": 0.5}

    ctx = SeededContext(7)
    for _ in range(500):
        resource_tags = tags.generate_tags(ctx, td, tag_density)
        assert len(resource_tags) <= n_keys


def test_tags_reproducible(generator_profile):
    """D-01: identical seed → identical tag assignment sequence."""
    from tenantless.generator import tags

    td = _profile_tags(generator_profile)
    density = {"mean": 4.0, "std": 1.0}

    a = [tags.generate_tags(SeededContext(123), td, density) for _ in range(1)]
    ctx_b = SeededContext(123)
    b = [tags.generate_tags(ctx_b, td, density)]
    assert a == b
