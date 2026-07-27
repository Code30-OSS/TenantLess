"""Co-occurrence + tag-relationship extractors (source-agnostic).

Two requirement areas land here, both Polars-only and reader-agnostic (they
import neither ``duckdb`` nor any reader type):

* **ANLZ-04** resource-type co-occurrence WITHIN a resource group. Given which
  TYPES appear together in each RG, derive ``typeA -> {typeB: probability}`` --
  the conditional probability that ``typeB`` is present in an RG given ``typeA``
  is present. The generator later uses this to reproduce realistic RG
  compositions.
* **ANLZ-07** tag-key co-occurrence (``keyA -> {keyB: probability}``), tag value
  cardinality (distinct value count per key), and untagged-rate-by-type (share of
  a type's resources carrying NO tags).

Privacy (D-03 hybrid coverage): every distribution is gated by the SHARED
``privacy.merge_min_buckets`` floor -- a pair / value observed fewer than
``min_bucket_size`` times is DROPPED before normalization, so no low-count
relationship can fingerprint a real tenant. Below the threshold the relevant
entry is simply omitted (never a failure): the build_profile caller skips the
optional section, and the provenance coverage record is Plan 04's job.

Type keys are canonicalized via ``resource_types.normalize_type_key`` so the
co-occurrence keys agree with ``resource_type_distributions`` casing.
"""

from __future__ import annotations

import polars as pl

from .. import privacy
from . import resource_types
from .tags import _is_identifier_shaped_key


def _normalize(counts: dict[str, int | float]) -> dict[str, float]:
    """Normalize a label->count map to shares summing to ~1.0 (empty -> {})."""
    total = sum(counts.values())
    if total <= 0:
        return {}
    return {label: count / total for label, count in counts.items()}


# --- ANLZ-04: resource-type co-occurrence within an RG -----------------------

def extract(
    rg_type_frame: pl.DataFrame,
    min_bucket_size: int = 5,
) -> dict[str, dict[str, float]]:
    """Build the resource-type co-occurrence matrix from per-RG type membership.

    Parameters
    ----------
    rg_type_frame:
        A long ``(subscription_id, resource_group, type)`` frame: one row per
        (subscription, RG, distinct type) present. (Duplicate rows are tolerated
        -- distinctness is enforced here.) The ``subscription_id`` column is
        REQUIRED to scope RG membership: Azure resource-group names are NOT
        globally unique (``networking``/``rg-shared`` recur across subscriptions),
        so the self-join below must key on (subscription_id, resource_group) to
        match the reader's ``rg_type_pairs`` SQL path. When the column is absent,
        every RG is treated as belonging to a single synthetic subscription (the
        legacy single-tenant shape) so RGs are NOT collided by name.
    min_bucket_size:
        Type pairs co-occurring in fewer than this many RGs are dropped before
        normalization (privacy min-aggregation, D-03).

    Returns ``{typeA: {typeB: probability}}`` where the probability is the share
    of ``typeA``'s co-occurring mass attributable to ``typeB`` (normalized per
    source type over surviving pairs). Empty when nothing clears the threshold.
    """
    if rg_type_frame.is_empty():
        return {}

    # Scope RG membership by subscription so identically-named RGs in different
    # subscriptions are NOT conflated (CR-02). Callers that omit subscription_id
    # are treated as a single synthetic subscription -- equivalent to the prior
    # name-only join for genuinely single-tenant frames, but never collides RGs
    # across subscriptions when the column IS present.
    if "subscription_id" in rg_type_frame.columns:
        sub_col = pl.col("subscription_id")
    else:
        sub_col = pl.lit("__single_subscription__").alias("subscription_id")

    # Distinct (subscription, RG, canonical type) membership -- an RG with N
    # copies of a type counts that type ONCE for co-occurrence.
    members = (
        rg_type_frame.select(
            sub_col,
            pl.col("resource_group"),
            pl.col("type")
            .map_elements(resource_types.normalize_type_key, return_dtype=pl.Utf8)
            .alias("type"),
        )
        .unique()
    )

    # Self-join on (subscription_id, resource_group) to form unordered type pairs
    # (a.type < b.type), then count how many RGs each pair co-occurs in. Joining
    # on both columns matches the reader's rg_type_pairs SQL self-join.
    pairs = (
        members.join(
            members, on=["subscription_id", "resource_group"], suffix="_b"
        )
        .filter(pl.col("type") < pl.col("type_b"))
        .group_by(["type", "type_b"])
        .agg(pl.len().alias("count"))
    )
    if pairs.is_empty():
        return {}

    surviving = privacy.merge_min_buckets(pairs, min_bucket_size)
    if surviving.is_empty():
        return {}

    # Accumulate symmetric directed counts (typeA->typeB and typeB->typeA).
    directed: dict[str, dict[str, int]] = {}
    for row in surviving.iter_rows(named=True):
        a = str(row["type"])
        b = str(row["type_b"])
        c = int(row["count"])
        directed.setdefault(a, {})[b] = directed.setdefault(a, {}).get(b, 0) + c
        directed.setdefault(b, {})[a] = directed.setdefault(b, {}).get(a, 0) + c

    return {src: _normalize(targets) for src, targets in directed.items()}


def extract_from_pairs(
    pair_frame: pl.DataFrame,
    min_bucket_size: int = 5,
) -> dict[str, dict[str, float]]:
    """Build the co-occurrence matrix from a pre-aggregated pair frame.

    Parameters
    ----------
    pair_frame:
        A ``(type_a, type_b, cooccur)`` frame (the reader's self-join CTE output,
        ``type_a < type_b``). ``cooccur`` is the number of RGs the pair shares.
    min_bucket_size:
        Pairs below this co-occurrence count are dropped before normalization.

    Returns ``{typeA: {typeB: probability}}`` (symmetric), canonicalized keys.
    Empty when nothing clears the threshold. This is the SQL-side analog of
    :func:`extract` for the Postgres/DuckDB reader path.
    """
    if pair_frame.is_empty():
        return {}

    surviving = privacy.merge_min_buckets(
        pair_frame, min_bucket_size, count_col="cooccur"
    )
    if surviving.is_empty():
        return {}

    directed: dict[str, dict[str, int]] = {}
    for row in surviving.iter_rows(named=True):
        a = resource_types.normalize_type_key(str(row["type_a"]))
        b = resource_types.normalize_type_key(str(row["type_b"]))
        c = int(row["cooccur"])
        directed.setdefault(a, {})[b] = directed.setdefault(a, {}).get(b, 0) + c
        directed.setdefault(b, {})[a] = directed.setdefault(b, {}).get(a, 0) + c

    return {src: _normalize(targets) for src, targets in directed.items()}


# --- ANLZ-07: tag key co-occurrence ------------------------------------------

def tag_key_cooccurrence(
    pair_counts: pl.DataFrame,
    min_bucket_size: int = 5,
) -> dict[str, dict[str, float]]:
    """Build tag-key co-occurrence from a ``(key_a, key_b, count)`` frame.

    ``count`` is the number of resources carrying BOTH keys. Pairs below
    ``min_bucket_size`` are dropped; the surviving mass is normalized per source
    key. Returns ``{keyA: {keyB: probability}}`` (symmetric: both directions
    emitted). Empty when nothing clears the threshold.

    Privacy (CR-01 / data boundary): identifier-shaped tag keys (e.g. an Azure
    ``hidden-link:/subscriptions/<uuid>/...`` system tag embedding a real
    resource id) are DROPPED before they can become a dict key here -- the same
    ``_is_identifier_shaped_key`` guard ``tags.extract`` applies. The reader
    pair-count frames do NOT pre-filter these keys, so the guard MUST live here.
    """
    if pair_counts.is_empty():
        return {}

    surviving = privacy.merge_min_buckets(pair_counts, min_bucket_size)
    if surviving.is_empty():
        return {}

    directed: dict[str, dict[str, int]] = {}
    for row in surviving.iter_rows(named=True):
        a = str(row["key_a"])
        b = str(row["key_b"])
        # Drop any pair touching an identifier-shaped key so a hidden-link:/
        # subscriptions/<uuid>/... key never becomes a dict key in the output.
        if _is_identifier_shaped_key(a) or _is_identifier_shaped_key(b):
            continue
        c = int(row["count"])
        directed.setdefault(a, {})[b] = directed.setdefault(a, {}).get(b, 0) + c
        directed.setdefault(b, {})[a] = directed.setdefault(b, {}).get(a, 0) + c

    return {src: _normalize(targets) for src, targets in directed.items()}


# --- ANLZ-07: tag value cardinality ------------------------------------------

def tag_value_cardinality(
    value_counts: pl.DataFrame,
    min_bucket_size: int = 5,
) -> dict[str, int]:
    """Per tag key, the count of DISTINCT above-threshold values (bucketed).

    Consumes the same ``(tag_key, tag_value, count)`` frame the tags extractor
    uses. Only values surviving the min-bucket floor are counted, so the
    cardinality is a privacy-safe distinct-count (never the raw values). A key
    whose every value is sub-threshold contributes 0.

    Privacy (CR-01 / data boundary): the output is keyed by tag_key, so an
    identifier-shaped key (e.g. ``hidden-link:/subscriptions/<uuid>/...``) would
    otherwise smuggle a full resource id in as a DICT KEY. Such keys are DROPPED
    before seeding and grouping -- the same ``_is_identifier_shaped_key`` guard
    ``tags.extract`` applies. The reader value-count frame does NOT pre-filter
    these keys, so the guard MUST live here.
    """
    if value_counts.is_empty():
        return {}

    surviving = privacy.merge_min_buckets(value_counts, min_bucket_size)
    out: dict[str, int] = {}
    # Seed every NON-identifier-shaped key at 0 so a fully-sub-threshold key
    # reports 0 (not absent surprise); then count distinct surviving values.
    # Identifier-shaped keys are skipped entirely so they never appear as a key.
    for key in value_counts["tag_key"].unique().to_list():
        if _is_identifier_shaped_key(str(key)):
            continue
        out[str(key)] = 0
    if not surviving.is_empty():
        grouped = (
            surviving.group_by("tag_key")
            .agg(pl.col("tag_value").n_unique().alias("cardinality"))
        )
        for row in grouped.iter_rows(named=True):
            key = str(row["tag_key"])
            if _is_identifier_shaped_key(key):
                continue
            out[key] = int(row["cardinality"])
    return out


# --- ANLZ-07: untagged rate by type ------------------------------------------

def untagged_rate_by_type(per_type: pl.DataFrame) -> dict[str, float]:
    """Per canonical resource type, the share of resources carrying NO tags.

    Consumes a ``(type, total, tagged)`` frame (one row per type). The untagged
    rate is ``(total - tagged) / total`` in [0, 1]. Type keys are canonicalized
    so they agree with ``resource_type_distributions``. Only aggregate
    rates cross the boundary -- never any resource identifier.
    """
    if per_type.is_empty():
        return {}

    out: dict[str, float] = {}
    for row in per_type.iter_rows(named=True):
        total = int(row["total"])
        if total <= 0:
            continue
        tagged = int(row["tagged"])
        rate = (total - tagged) / total
        key = resource_types.normalize_type_key(str(row["type"]))
        out[key] = min(1.0, max(0.0, rate))
    return out
