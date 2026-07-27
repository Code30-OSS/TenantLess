"""Privacy-first resource-naming extractor (ANLZ-08, source-agnostic).

Learns the STRUCTURE of resource names without ever emitting a verbatim name --
the HARD data-boundary bar for Phase 6 (Pitfall 6, threat T-06-09). A real name
like ``vm-prod-001`` is tokenized into a sequence of STRUCTURAL CLASSES
(``<word>-<env>-<int>``); only the class sequences (structural patterns) and the
per-position class distributions cross the boundary -- never the tokens
themselves.

Output shape (profiles/schema.json naming_conventions, v1.1):

    {
      "pattern_frequencies":    {"<word>-<env>-<int>": 0.6, ...},   # normalized
      "position_token_classes": {"0": {"word": 1.0}, "1": {...}, ...}  # per index
    }

Privacy controls (mirrors tags.py):
    * Each name is split on common Azure delimiters (``-``, ``_``, ``.``) into
      tokens. A token that is IDENTIFIER-SHAPED -- a UUID, a long unbroken hex
      run, an embedded UUID/hex fragment, or a resource-path marker -- causes the
      WHOLE name to be DROPPED (it carries a real identifier and no safe
      structure can be salvaged). The exact ``_UUID_RE`` / ``_LONG_HEX_RE`` /
      ``_EMBEDDED_*`` guards from tags.py are REUSED (single source of truth).
    * Every surviving token maps to a bounded structural CLASS (never the token
      text): ``int`` / ``env`` / ``region`` / ``word`` / ``__other__``. Only the
      class label is ever emitted, so even a non-identifier token (a real RG
      fragment) never leaves verbatim.
    * Pattern frequencies and per-position class frequencies are routed through
      the SHARED ``privacy.merge_min_buckets`` floor: a structural pattern (or a
      positional class) observed fewer than ``min_bucket_size`` times folds into
      ``__other__`` rather than appearing, so a one-off structure cannot
      fingerprint a single real resource.

Source-agnostic: imports neither ``duckdb`` nor any reader type. It consumes a
``(name[, type])`` Polars frame the caller supplies from the reader.
"""

from __future__ import annotations

import re

import polars as pl

from .. import privacy
from .tags import (  # REUSE the tag identifier guards (single source of truth)
    _EMBEDDED_LONG_HEX_RE,
    _EMBEDDED_UUIDISH_RE,
    _LONG_HEX_RE,
    _PATH_MARKERS,
    _UUID_RE,
)

OTHER_BUCKET = "__other__"

# Token delimiters common in Azure resource names.
_SPLIT_RE = re.compile(r"[-_./]+")

# Bounded structural token classes. Only the CLASS label is ever emitted.
_INT_RE = re.compile(r"^\d+$")
# Alphanumeric token that is neither pure-int nor an env/region keyword.
_WORD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")

# Known low-cardinality environment tokens (case-insensitive). These are generic
# Azure governance vocabulary, not tenant identifiers, so a dedicated ``env``
# class is privacy-safe (still emitted only as the label ``env``).
_ENV_TOKENS = frozenset(
    {
        "prod", "prd", "production",
        "dev", "development",
        "test", "tst",
        "uat", "qa", "stage", "staging", "stg",
        "sandbox", "sbx", "demo", "preprod", "nonprod", "nprd",
    }
)

# Known Azure region tokens (a representative subset; case-insensitive). A token
# matching a region maps to the ``region`` class.
_REGION_TOKENS = frozenset(
    {
        "eastus", "eastus2", "westus", "westus2", "westus3", "centralus",
        "northcentralus", "southcentralus", "westcentralus",
        "northeurope", "westeurope", "uksouth", "ukwest",
        "francecentral", "germanywestcentral", "switzerlandnorth",
        "eastasia", "southeastasia", "australiaeast", "australiasoutheast",
        "japaneast", "japanwest", "koreacentral", "centralindia", "southindia",
        "brazilsouth", "canadacentral", "canadaeast", "uaenorth", "southafricanorth",
    }
)


def _is_identifier_shaped_token(token: str) -> bool:
    """True if a token is identifier-shaped (REUSES the tag guards)."""
    if _UUID_RE.match(token) or _LONG_HEX_RE.match(token):
        return True
    if _EMBEDDED_UUIDISH_RE.search(token) or _EMBEDDED_LONG_HEX_RE.search(token):
        return True
    lower = token.lower()
    if any(marker in lower for marker in _PATH_MARKERS):
        return True
    return False


def _classify(token: str) -> str:
    """Map a token to its bounded structural class (never the token text)."""
    lower = token.lower()
    if _INT_RE.match(token):
        return "int"
    if lower in _ENV_TOKENS:
        return "env"
    if lower in _REGION_TOKENS:
        return "region"
    if _WORD_RE.match(token):
        return "word"
    return OTHER_BUCKET


def _tokenize(name: str) -> list[str] | None:
    """Split a name into structural class labels, or None if it must be DROPPED.

    Returns ``None`` when ANY token is identifier-shaped (the whole name carries
    a real identifier); otherwise the list of class labels, one per token.
    """
    if not name:
        return None
    raw_tokens = [t for t in _SPLIT_RE.split(name) if t]
    if not raw_tokens:
        return None
    classes: list[str] = []
    for tok in raw_tokens:
        if _is_identifier_shaped_token(tok):
            return None  # drop the entire name -- it embeds a real identifier
        classes.append(_classify(tok))
    return classes


def _normalize(counts: dict[str, int]) -> dict[str, float]:
    total = sum(counts.values())
    if total <= 0:
        return {}
    return {label: count / total for label, count in counts.items()}


def _merge_counts_min_bucket(
    counts: dict[str, int], min_bucket_size: int
) -> dict[str, int]:
    """Fold sub-threshold labels into ``__other__`` (mirror tags merge)."""
    merged: dict[str, int] = {}
    dropped = 0
    for label, count in counts.items():
        if count < min_bucket_size:
            dropped += count
        else:
            merged[label] = merged.get(label, 0) + count
    if dropped > 0:
        merged[OTHER_BUCKET] = merged.get(OTHER_BUCKET, 0) + dropped
    return merged


def extract(
    name_frame: pl.DataFrame,
    min_bucket_size: int = 5,
) -> dict[str, dict]:
    """Build the ``naming_conventions`` fragment from a name-sampling frame.

    Parameters
    ----------
    name_frame:
        A Polars frame with a ``name`` column (other columns, e.g. ``type``, are
        ignored). Each name is tokenized into structural classes; names embedding
        a real identifier are dropped wholesale.
    min_bucket_size:
        Structural patterns / positional classes below this count fold into
        ``__other__`` (privacy min-aggregation, D-03).

    Returns ``{"pattern_frequencies": {...}, "position_token_classes": {...}}``.
    Emits ONLY structural class labels -- never a verbatim name or token.
    """
    if name_frame.is_empty() or "name" not in name_frame.columns:
        return {"pattern_frequencies": {}, "position_token_classes": {}}

    pattern_counts: dict[str, int] = {}
    # position index -> {class label -> count}
    position_counts: dict[int, dict[str, int]] = {}

    for name in name_frame["name"].to_list():
        if name is None:
            continue
        classes = _tokenize(str(name))
        if classes is None:
            continue  # dropped -- identifier-shaped
        pattern = "-".join(f"<{c}>" for c in classes)
        pattern_counts[pattern] = pattern_counts.get(pattern, 0) + 1
        for idx, cls in enumerate(classes):
            position_counts.setdefault(idx, {})[cls] = (
                position_counts.setdefault(idx, {}).get(cls, 0) + 1
            )

    # Min-bucket gate + normalize the structural-pattern frequencies.
    merged_patterns = _merge_counts_min_bucket(pattern_counts, min_bucket_size)
    pattern_frequencies = _normalize(merged_patterns)

    # Per-position class distributions, each min-bucket gated + normalized. The
    # key is the position index as a string (schema additionalProperties object).
    position_token_classes: dict[str, dict[str, float]] = {}
    for idx, class_counts in sorted(position_counts.items()):
        merged = _merge_counts_min_bucket(class_counts, min_bucket_size)
        normalized = _normalize(merged)
        if normalized:
            position_token_classes[str(idx)] = normalized

    return {
        "pattern_frequencies": pattern_frequencies,
        "position_token_classes": position_token_classes,
    }
