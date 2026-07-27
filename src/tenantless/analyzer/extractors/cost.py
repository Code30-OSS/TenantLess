"""Per-resource-type lognormal cost extractor (COST-01 / COST-05).

Consumes the ``(type, monthly_cost)`` sample frame produced by
``reader.resource_cost_samples()`` and produces the ``cost_distributions``
profile fragment: a mapping of canonical resource-type key ->
``{"distribution": "lognormal", "mu": float, "sigma": float, "sample_count": int}``.

The fit is a maximum-likelihood lognormal with the location pinned to zero
(``scipy.stats.lognorm.fit(values, floc=0)``). scipy parameterizes lognorm as
``shape`` (= sigma of the underlying normal) and ``scale`` (= exp(mu)); we map
those back to the natural lognormal params the generator samples with:

    sigma = shape
    mu    = log(scale)

so ``ctx.rng.lognormal(mu, sigma)`` in the generator reproduces this fit.

Privacy / data boundary (CLAUDE.md hard constraint, COST-05 / Phase-6 CR-01):
the seed carries real ``resource_id``/``subscription_id`` strings, but ONLY the
``type`` and ``monthly_cost`` columns are ever read here (we ``select`` them
defensively before iterating), and ONLY canonical type keys + numeric params are
emitted -- never a real identifier, tag, or meter string. The min-bucket floor
(the ``merge_min_buckets`` analogue) DROPS any type with fewer than
``min_bucket_size`` samples BEFORE fitting, so no low-count type can fingerprint
a real tenant. A dedicated leak test (``tests/test_cost_leak.py``) pins this.

Currency note (D-11): the seed amounts are EUR; magnitudes carry over relabeled
as USD for v2.0. Unmapped resource types are simply absent from the output -- the
generator zero-fills cost for types with no fitted distribution (D-02).
"""

from __future__ import annotations

import numpy as np
import polars as pl
from scipy.stats import lognorm

from .resource_types import normalize_type_key


def extract_cost_distributions(
    samples: pl.DataFrame,
    *,
    min_bucket_size: int = 5,
) -> dict[str, dict]:
    """Fit a per-type lognormal monthly-cost distribution behind a privacy floor.

    Parameters
    ----------
    samples:
        A ``(type, monthly_cost)`` frame (one row per resource-month sample) as
        returned by ``reader.resource_cost_samples()``. Extra columns (e.g. a
        stray ``resource_id``) are IGNORED -- only ``type``/``monthly_cost`` are
        read, so no identifier column can cross into the output.
    min_bucket_size:
        Privacy floor: a resource type with fewer than this many samples is
        dropped entirely (the ``merge_min_buckets`` analogue, default 5).

    Returns
    -------
    dict
        ``{"<canonical type>": {"distribution": "lognormal", "mu": float,
        "sigma": float, "sample_count": int}}`` over surviving types; ``{}`` for
        an empty / cost-less source.
    """
    if samples.is_empty():
        return {}

    # Read ONLY type/monthly_cost and canonicalize the type key up front, so that
    # (a) no identifier column is ever touched, and (b) lowercase seed types and
    # any already-canonical types collapse onto the same canonical bucket.
    frame = samples.select(
        pl.col("type")
        .map_elements(normalize_type_key, return_dtype=pl.Utf8)
        .alias("type"),
        pl.col("monthly_cost").cast(pl.Float64).alias("monthly_cost"),
    )

    out: dict[str, dict] = {}
    for type_key in frame["type"].unique(maintain_order=True).to_list():
        if type_key is None:
            continue
        group = frame.filter(pl.col("type") == type_key)

        # Min-bucket privacy floor: drop sub-threshold types BEFORE fitting.
        if group.height < min_bucket_size:
            continue

        # lognorm.fit requires strictly-positive support; guard against any
        # non-positive amount sneaking in (the seed is positive, but be safe).
        values = group["monthly_cost"].to_numpy()
        values = values[values > 0]
        if values.size < min_bucket_size:
            continue

        shape, _loc, scale = lognorm.fit(values, floc=0)
        out[type_key] = {
            "distribution": "lognormal",
            "mu": float(np.log(scale)),
            "sigma": float(shape),
            "sample_count": int(values.size),
        }

    return out
