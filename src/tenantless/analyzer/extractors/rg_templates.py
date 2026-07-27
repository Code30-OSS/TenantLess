"""Resource-group architecture-template extractor (source-agnostic).

Consumes the per-RG frame from the reader (``rg_type_sets``: one row per
resource group with its ``type_set`` -- the sorted distinct resource types in
that RG -- and ``resource_count``) and derives ``resource_group_templates``:
clusters of RGs that share the same type-set COMPOSITION (ANLZ-03).

Rules:
- Group RGs by their (normalized, sorted, distinct) type-set composition.
- Keep the TOP 30 most frequent compositions as standalone templates.
- Fold ALL remaining RGs -- the long tail beyond the top 30, AND any composition
  observed fewer than ``min_bucket_size`` times -- into a single ``"__misc__"``
  template (privacy min-aggregation; threat T-01.1-06). This yields at most 31
  templates.
- Each template emits: ``id`` (synthetic: ``"template-<i>"`` or ``"__misc__"``),
  ``weight`` (share of RGs, summing to ~1.0), ``type_set`` (>=1 entry), and
  ``resource_count`` ``{mean,std,min,max}`` over the member RGs.
- The ``__misc__`` template ADDITIONALLY emits (so a privacy bucket still carries
  the resource-type MASS needed to reproduce the estate, instead of generating
  empty — the ``["__misc__"]`` sentinel type_set is otherwise un-generatable):
  * ``type_weights`` — ``{type: weight}`` normalized histogram of the resource
    types across all folded RGs (mass-approximated as ``resource_count / |type_set|``
    per RG). Public type strings only, never identifiers.
  * ``empty_share`` — fraction (0..1) of the folded RGs that are TRULY empty
    (zero resources) in the source, so the generator reproduces genuine empties
    at their real rate rather than emitting every folded RG empty.

Casing: every type string in a template ``type_set`` is run through the SAME
``resource_types.normalize_type_key`` rule used for the
``resource_type_distributions`` keys, so the two sections agree on casing
(canonical leading ``Microsoft.`` namespace, remainder preserved verbatim).

Source-agnostic: imports neither ``duckdb`` nor any reader type.
"""

from __future__ import annotations

import math
from typing import Any

import polars as pl

from . import resource_types
from .tags import _is_identifier_shaped_key

TOP_N = 30
MISC_ID = "__misc__"
# The __misc__ type_set must be non-empty to satisfy the schema (minItems: 1).
MISC_TYPE_SET = ["__misc__"]


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


def _normalize_composition(raw_type_set: Any) -> tuple[str, ...]:
    """Normalize a raw type_set into a canonical, sorted, distinct tuple.

    Each entry is passed through ``resource_types.normalize_type_key`` so the
    casing matches ``resource_type_distributions`` keys.
    """
    if raw_type_set is None:
        return ()
    normalized = {
        resource_types.normalize_type_key(str(t))
        for t in raw_type_set
        if t is not None
    }
    return tuple(sorted(normalized))


def extract(
    rg_frame: pl.DataFrame,
    min_bucket_size: int = 5,
    *,
    top_n: int = TOP_N,
) -> list[dict[str, Any]]:
    """Build ``resource_group_templates`` from a per-RG type-set frame.

    Parameters
    ----------
    rg_frame:
        Per-RG frame with columns ``type_set`` (list[str]) and ``resource_count``
        (int), one row per resource group.
    min_bucket_size:
        Compositions seen fewer than this many times fold into ``"__misc__"``.
    top_n:
        Number of standalone templates to keep before folding the rest.

    Returns a list of at most ``top_n + 1`` template dicts; weights sum to ~1.0.
    """
    if rg_frame.is_empty():
        return []

    # Bucket RGs by their normalized composition -> list of resource_counts.
    by_comp: dict[tuple[str, ...], list[float]] = {}
    for row in rg_frame.iter_rows(named=True):
        comp = _normalize_composition(row["type_set"])
        if not comp:
            # An RG with no resolvable types still counts toward __misc__.
            comp = ()
        by_comp.setdefault(comp, []).append(float(row["resource_count"]))

    total_rgs = sum(len(v) for v in by_comp.values())
    if total_rgs == 0:
        return []

    # Order compositions by frequency (RG count) descending; ties by composition
    # for determinism.
    ordered = sorted(
        by_comp.items(),
        key=lambda kv: (-len(kv[1]), kv[0]),
    )

    standalone: list[tuple[tuple[str, ...], list[float]]] = []
    # ALL folded RG counts (for the misc weight + empty_share denominator) and the
    # NON-EMPTY subset (for resource_count stats). These MUST be separate: a
    # resource_count computed over all folded counts would include the true-empty
    # RGs' zeros, so it would already encode the emptiness — and the generator,
    # which applies empty_share INDEPENDENTLY, would then double-count it and
    # under-size the non-empty RGs. resource_count is the size of a NON-empty misc
    # RG; empty_share is the separate probability it is empty at all.
    misc_all_counts: list[float] = []
    misc_nonempty_counts: list[float] = []
    # Aggregate resource-type MASS across the folded (misc) RGs so the __misc__
    # template can still be generated with a realistic type mix instead of the
    # bare sentinel (which the generator would empty). For an RG of ``k`` distinct
    # types and ``c`` total resources we credit ``c/k`` to each of its types
    # (a mass approximation: the reader frame carries distinct-types + total count
    # per RG, not per-type counts). Summed over every folded RG, then normalized.
    misc_type_mass: dict[str, float] = {}
    # Count of folded RGs that are TRULY empty in the source (zero resources), so
    # the generator can reproduce genuine empties at their real rate rather than
    # emitting every folded RG empty.
    misc_empty = 0

    for idx, (comp, counts) in enumerate(ordered):
        below_threshold = len(counts) < min_bucket_size
        over_top_n = idx >= top_n
        empty_comp = len(comp) == 0
        if below_threshold or over_top_n or empty_comp:
            misc_all_counts.extend(counts)
            for c in counts:
                if c <= 0:
                    misc_empty += 1
                else:
                    misc_nonempty_counts.append(c)
            k = len(comp)
            if k > 0:
                share = sum(counts) / k
                for t in comp:
                    # Shared privacy guard: a new string-emitting path must reapply
                    # _is_identifier_shaped_key (a malformed identifier-shaped "type"
                    # must never cross the boundary via type_weights).
                    if _is_identifier_shaped_key(t):
                        continue
                    misc_type_mass[t] = misc_type_mass.get(t, 0.0) + share
        else:
            standalone.append((comp, counts))

    templates: list[dict[str, Any]] = []
    for i, (comp, counts) in enumerate(standalone):
        templates.append(
            {
                "id": f"template-{i}",
                "weight": len(counts) / total_rgs,
                "type_set": list(comp),
                "resource_count": _dist_stats(counts),
            }
        )

    if misc_all_counts:
        misc: dict[str, Any] = {
            "id": MISC_ID,
            # weight is the share of ALL RGs (empty + non-empty) that are misc, so
            # the generator makes the right NUMBER of misc RGs.
            "weight": len(misc_all_counts) / total_rgs,
            "type_set": list(MISC_TYPE_SET),
            # resource_count is the size of a NON-EMPTY misc RG (excludes the
            # true-empty zeros); empty_share below carries the emptiness separately.
            "resource_count": _dist_stats(misc_nonempty_counts or misc_all_counts),
        }
        total_mass = sum(misc_type_mass.values())
        if total_mass > 0:
            # Normalized histogram, deterministic (sorted) key order. Public Azure
            # type strings only — never identifiers — so no min-aggregation gate is
            # needed (the data-boundary risk is identifiers, not type names).
            misc["type_weights"] = {
                t: misc_type_mass[t] / total_mass for t in sorted(misc_type_mass)
            }
        misc["empty_share"] = misc_empty / len(misc_all_counts)
        templates.append(misc)

    return templates
