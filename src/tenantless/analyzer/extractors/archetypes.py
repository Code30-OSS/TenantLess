"""Subscription-archetype extractor via k-means (source-agnostic).

Consumes the per-subscription feature frame produced by the reader
(``subscription_features``) and discovers ``k`` archetypes with
``sklearn.cluster.KMeans``. Each archetype is a synthetic, aggregate-only
summary of the subscriptions in its cluster:

    {
      "id": "archetype-<i>",                # synthetic, never a real name
      "weight": <cluster share, sums ~1.0>,
      "resource_group_count": {mean,std,min,max},
      "resource_count":       {mean,std,min,max},
      "location_distribution": {<loc>: 0..1, ...},   # normalized, sums ~1.0
      "tag_density": {mean, std},
    }

Design notes:
- Features are STANDARDIZED (``StandardScaler``) before clustering so the
  resource-count magnitude does not dominate the location-mix and tag-density
  signals.
- The ``k`` passed in (ultimately the CLI ``--k`` flag, threaded through
  ``build_profile``) is honored EXACTLY: ``KMeans(n_clusters=k)`` so the result
  has exactly ``k`` archetypes whenever ``k <= number of subscriptions``.
- Per-archetype ``location_distribution`` reuses the privacy min-bucket merge
  (via :mod:`extractors.locations`) so low-count regions inside a cluster fold
  into ``"__other__"`` -- no single subscription's region can fingerprint it.

Source-agnostic: imports neither ``duckdb`` nor any reader type. Operates purely
on a Polars frame, so the Phase 6 ConnectorX reader reuses it unchanged.
"""

from __future__ import annotations

import math
from typing import Any

import polars as pl
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler

from . import locations

DEFAULT_K = 5

# Non-feature / identifier columns that must NOT be fed to the clusterer.
_ID_COLS = {"subscription_id"}


def _location_columns(frame: pl.DataFrame) -> list[str]:
    return [c for c in frame.columns if c.startswith("loc__")]


def _feature_columns(frame: pl.DataFrame) -> list[str]:
    return [c for c in frame.columns if c not in _ID_COLS]


def _dist_stats(values: list[float]) -> dict[str, float]:
    """{mean,std,min,max} for a list of values (population std; empty -> zeros)."""
    if not values:
        return {"mean": 0.0, "std": 0.0, "min": 0.0, "max": 0.0}
    n = len(values)
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    return {
        "mean": float(mean),
        "std": float(math.sqrt(var)),
        "min": float(min(values)),
        "max": float(max(values)),
    }


def _dist_stats_no_minmax(values: list[float]) -> dict[str, float]:
    """{mean,std} for a list of values (population std; empty -> zeros)."""
    if not values:
        return {"mean": 0.0, "std": 0.0}
    n = len(values)
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    return {"mean": float(mean), "std": float(math.sqrt(var))}


def _cluster_location_distribution(
    members: pl.DataFrame, loc_cols: list[str], min_bucket_size: int
) -> dict[str, float]:
    """Sum the cluster members' per-location counts and normalize (privacy-merged)."""
    counts: dict[str, int] = {}
    for col in loc_cols:
        loc = col[len("loc__"):]
        total = int(members[col].sum())
        if total > 0:
            counts[loc] = total
    if not counts:
        return {}
    return locations.from_count_map(counts, min_bucket_size=min_bucket_size)


def extract(
    features: pl.DataFrame,
    k: int | None = None,
    *,
    min_bucket_size: int = 5,
    random_state: int = 42,
) -> list[dict[str, Any]]:
    """Discover ``k`` subscription archetypes via k-means over ``features``.

    Parameters
    ----------
    features:
        Per-subscription feature frame from ``reader.subscription_features``
        (columns: subscription_id, resource_count, rg_count, tag_density,
        loc__<location> mix columns).
    k:
        Number of clusters. Defaults to :data:`DEFAULT_K`. Clamped to the number
        of subscriptions (KMeans cannot produce more clusters than samples).
    min_bucket_size:
        Min-bucket threshold for the per-archetype location merge.

    Returns a list of EXACTLY ``min(k, n_subscriptions)`` archetype dicts.
    """
    if features.is_empty():
        return []

    n = features.height
    k_eff = DEFAULT_K if k is None else int(k)
    k_eff = max(1, min(k_eff, n))

    feature_cols = _feature_columns(features)
    loc_cols = _location_columns(features)

    X = features.select(feature_cols).to_numpy().astype(float)
    X_scaled = StandardScaler().fit_transform(X)

    km = KMeans(n_clusters=k_eff, random_state=random_state, n_init=10)
    labels = km.fit_predict(X_scaled)

    labelled = features.with_columns(pl.Series("__cluster__", labels))

    archetypes: list[dict[str, Any]] = []
    for ci in range(k_eff):
        members = labelled.filter(pl.col("__cluster__") == ci)
        # KMeans can in principle leave a cluster empty; guard with zeros so we
        # still emit exactly k archetypes.
        if members.is_empty():
            archetypes.append(
                {
                    "id": f"archetype-{ci}",
                    "weight": 0.0,
                    "resource_group_count": _dist_stats([]),
                    "resource_count": _dist_stats([]),
                    "location_distribution": {locations.OTHER_BUCKET: 1.0},
                    "tag_density": _dist_stats_no_minmax([]),
                }
            )
            continue

        rg_counts = members["rg_count"].to_list()
        res_counts = members["resource_count"].to_list()
        tag_dens = members["tag_density"].to_list()

        loc_dist = _cluster_location_distribution(members, loc_cols, min_bucket_size)
        if not loc_dist:
            loc_dist = {locations.OTHER_BUCKET: 1.0}

        archetypes.append(
            {
                "id": f"archetype-{ci}",
                "weight": members.height / n,
                "resource_group_count": _dist_stats([float(v) for v in rg_counts]),
                "resource_count": _dist_stats([float(v) for v in res_counts]),
                "location_distribution": loc_dist,
                "tag_density": _dist_stats_no_minmax(
                    [float(v) for v in tag_dens]
                ),
            }
        )

    return archetypes
