"""Location-affinity extractor (source-agnostic).

Produces a normalized region-affinity distribution -- a mapping of location ->
share in ``[0, 1]`` summing to ~1.0 -- from a ``(location, count)`` Polars frame.

Privacy: any location bucket observed fewer than ``min_bucket_size`` times is
merged (via the privacy layer) into a single ``"__other__"`` bucket BEFORE
normalization, so no low-count region can fingerprint a real tenant
(ANLZ-09 / threat T-01.1-06). The merged ``"__other__"`` bucket keeps the
distribution summing to ~1.0.

This module is source-agnostic: it imports neither ``duckdb`` nor any
reader-specific type, operating purely on Polars frames so Phase 6 can reuse it.
"""

from __future__ import annotations

import polars as pl

from .. import privacy

OTHER_BUCKET = "__other__"


def _merge_into_other(
    counts_frame: pl.DataFrame,
    min_bucket_size: int,
    *,
    label_col: str,
    count_col: str = "count",
) -> dict[str, int]:
    """Return a ``{label: count}`` map with sub-threshold labels folded into
    ``"__other__"`` using :func:`privacy.merge_min_buckets` (no reimplementation).
    """
    if counts_frame.is_empty():
        return {}

    surviving = privacy.merge_min_buckets(
        counts_frame, min_bucket_size, count_col=count_col
    )

    total = int(counts_frame[count_col].sum())
    kept = int(surviving[count_col].sum()) if not surviving.is_empty() else 0
    dropped = total - kept

    merged: dict[str, int] = {}
    for row in surviving.iter_rows(named=True):
        merged[str(row[label_col])] = merged.get(str(row[label_col]), 0) + int(
            row[count_col]
        )
    if dropped > 0:
        merged[OTHER_BUCKET] = merged.get(OTHER_BUCKET, 0) + dropped
    return merged


def normalize_counts(counts: dict[str, int]) -> dict[str, float]:
    """Normalize a ``{label: count}`` map to shares summing to ~1.0."""
    total = sum(counts.values())
    if total <= 0:
        return {}
    return {label: count / total for label, count in counts.items()}


def extract(
    counts_frame: pl.DataFrame,
    min_bucket_size: int = 5,
    *,
    label_col: str = "location",
    count_col: str = "count",
) -> dict[str, float]:
    """Build a normalized location distribution from a ``(location, count)`` frame.

    Buckets below ``min_bucket_size`` fold into ``"__other__"``; the surviving
    distribution (incl. ``"__other__"``) is normalized to shares summing to ~1.0.
    """
    merged = _merge_into_other(
        counts_frame, min_bucket_size, label_col=label_col, count_col=count_col
    )
    return normalize_counts(merged)


def from_count_map(
    counts: dict[str, int], min_bucket_size: int = 5
) -> dict[str, float]:
    """Convenience for callers that already hold a ``{location: count}`` map.

    Builds a one-off frame and runs :func:`extract` so the privacy min-bucket
    merge stays in a single place.
    """
    if not counts:
        return {}
    frame = pl.DataFrame(
        {"location": list(counts.keys()), "count": list(counts.values())}
    )
    return extract(frame, min_bucket_size)
