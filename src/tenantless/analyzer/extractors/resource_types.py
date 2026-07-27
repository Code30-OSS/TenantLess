"""Resource-type frequency extractor (source-agnostic).

Consumes a (type, count) Polars frame and produces the
``resource_type_distributions`` profile fragment: a mapping of canonical
resource-type key -> ``{"frequency": <normalized share>, "property_distributions": {}}``.

Casing rule (see 01.1-CONTEXT.md "Casing normalization"):
    Real ``type`` strings from the scan are lowercase, e.g.
    ``microsoft.compute/virtualmachines``. We normalize the leading namespace
    segment to canonical ``Microsoft.`` casing (the only segment Azure guarantees
    a stable canonical form for from a lowercase source) and otherwise PRESERVE
    the source string. We do not attempt to recover camelCase for the type tail
    (e.g. ``virtualMachines``) because that mapping is not derivable from the
    lowercase source without an external vocabulary -- Phase 4 owns response
    casing. This keeps the rule deterministic and lossless: ``microsoft.X/y``
    -> ``Microsoft.X/y``; anything not starting with ``microsoft.`` is preserved
    verbatim.

This slice does NOT populate property shapes -- every entry carries an empty
``property_distributions`` object. Plan 03 fills real property distributions.
"""

from __future__ import annotations

import polars as pl


def normalize_type_key(raw: str) -> str:
    """Normalize a lowercase real type string to canonical ``Microsoft.*`` casing.

    Only the leading ``microsoft.`` namespace token is canonicalized; the
    remainder is preserved verbatim. Non-Microsoft types pass through unchanged.
    """
    if raw is None:
        return raw
    if raw.lower().startswith("microsoft."):
        return "Microsoft." + raw[len("microsoft."):]
    return raw


def extract(counts_frame: pl.DataFrame) -> dict[str, dict]:
    """Build ``resource_type_distributions`` from a (type, count) frame.

    Frequencies are normalized counts summing to ~1.0 over the surviving
    buckets. Every entry carries an empty ``property_distributions`` object so
    the partial profile satisfies the schema's required ``property_distributions``
    on each type.
    """
    if counts_frame.is_empty():
        return {}

    total = int(counts_frame["count"].sum())
    if total <= 0:
        return {}

    out: dict[str, dict] = {}
    for row in counts_frame.iter_rows(named=True):
        key = normalize_type_key(row["type"])
        share = float(row["count"]) / total
        out[key] = {"frequency": share, "property_distributions": {}}
    return out
