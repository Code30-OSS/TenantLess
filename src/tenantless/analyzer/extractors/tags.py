"""Tag-distribution extractor (source-agnostic).

Produces the ``tag_distributions`` profile fragment from two pre-aggregated
reader frames:

    * ``tag_key_counts``   -> ``(tag_key, count)``: #resources carrying each key.
    * ``tag_value_counts`` -> ``(tag_key, tag_value, count)``: per-key value hist.

Output shape (profiles/schema.json):

    {
      "key_frequencies":  {<tag_key>: 0..1, ...},     # share of resources w/ key
      "value_distributions": {<tag_key>: {<value>: 0..1, ...}, ...}  # normalized
    }

Privacy (threats T-01.1-07 / privacy-extractor-leaks):
    Tag KEYS are MOSTLY generic schema (Environment, BU, CostCenter, ...) but
    real Azure system tags (``hidden-link:``/``hidden-related:``) carry FULL
    resource IDs as the key -- subscription UUIDs and real RG/resource names.
    Such identifier-shaped keys are DROPPED before they reach
    ``key_frequencies`` (see :func:`_is_identifier_shaped_key`).

    Tag VALUES are governed by a POSITIVE ALLOWLIST. ``value_distributions`` are
    emitted ONLY for keys on :data:`VALUE_ALLOWLIST_KEYS` -- a conservative,
    curated set of known low-cardinality categorical GOVERNANCE keys
    (Environment, BU, CostCenter, Criticality, Tier, ...). Identifier-bearing
    keys (Owner, Migrate Project, aks-managed-cluster-name, ClusterNameSql,
    databricks-instance-name, RunName, Name, ...) are NOT on the allowlist, so
    they keep their ``key_frequencies`` entry but their value maps are DROPPED
    entirely. This structurally removes real identifiers that are short,
    low-cardinality, and otherwise indistinguishable from legitimate enums --
    content-shape heuristics alone cannot tell ``Payroll Portal`` (identifier)
    from ``databricks`` (safe) for the same ``application`` key.

    Defense-in-depth retained for allowlisted keys: tag VALUES may still be real
    identifiers (32-hex instance IDs, UUIDs, resource paths) that legitimately
    repeat >= ``min_bucket_size`` and so survive the count-based min-bucket
    merge. Each surviving value is therefore ALSO shape-gated: identifier-shaped
    values fold into ``"__other__"`` regardless of count (see
    :func:`_is_identifier_shaped_value`). Sub-threshold values fold into
    ``"__other__"`` as before, and a high-cardinality surviving set drops the
    value map. These structural controls hold independent of the build_profile
    denylist scan, which is only a local gitignored backstop.

Source-agnostic: imports neither ``duckdb`` nor any reader type.
"""

from __future__ import annotations

import re

import polars as pl

from .. import privacy

OTHER_BUCKET = "__other__"

# Maximum number of distinct surviving (non-__other__) VALUE labels for a tag's
# value_distribution to be considered "low-cardinality enum-like" and therefore
# safe to emit. Above this, the value set is treated as a free-form identifier
# space (real subscription/resource/cluster names) and the value map is dropped
# -- only key_frequencies survives for that key. Calibrated against the real
# source scan: genuine enums (Environment, CountryCode, Criticality, Location)
# sit well under 40, while identifier-bearing keys (Subscription~190, Name~220,
# Application~467, ClusterId/ClusterName) sit far above it.
MAX_ENUM_CARDINALITY = 40

# --- Key/value shape guards (privacy-extractor-leaks fix) --------------------
# Resource-path / identifier substrings that must never appear in a tag KEY.
_PATH_MARKERS = ("/subscriptions/", "/resourcegroups/", "/providers/")
# Azure system tags whose KEY embeds a full resource id or resource NAME.
#   * ``hidden-link:`` / ``hidden-related:``  -> full resource id in the key.
#   * ``__SYSTEM__``                          -> Azure-injected system tag of the
#     form ``__SYSTEM__<Service>_<real-resource-name>[_suffix]`` (e.g. an
#     AzureOpenAI deployment name). The embedded name is a real identifier, so
#     the whole class is dropped exactly like hidden-link.
_HIDDEN_KEY_PREFIXES = ("hidden-link:", "hidden-related:", "__system__")

# Custom colon-namespaced tag keys (``<namespace>:<suffix>``) carry the
# CUSTOMER's namespace token as the prefix -- typically the tenant / org name
# (a real identifier per the CLAUDE.md data boundary). Azure's own colon-
# namespaced keys (hidden-link:/hidden-related:) are already dropped above; any
# OTHER colon-prefixed key is a customer convention whose namespace fingerprints
# the tenant, so it is dropped too. ``_SAFE_KEY_NAMESPACES`` is an escape hatch
# for known-generic namespaces should any emerge (currently none).
_SAFE_KEY_NAMESPACES: frozenset[str] = frozenset()
# A bare 8-4-4-4-12 UUID (whole-string).
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
# A long unbroken hex run (>= 24 chars), e.g. a 32-hex databricks instance id
# (whole-string).
_LONG_HEX_RE = re.compile(r"^[0-9a-f]{24,}$", re.IGNORECASE)

# Embedded identifiers (search ANYWHERE in a value). A full or PARTIAL UUID
# fragment (e.g. ``9b0c5ada-535a-4fd6-8f``) and a >=20-char unbroken hex run both
# fingerprint a real resource even when wrapped in a longer compound string.
_EMBEDDED_UUIDISH_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}", re.IGNORECASE
)
_EMBEDDED_LONG_HEX_RE = re.compile(r"[0-9a-f]{20,}", re.IGNORECASE)

# Tag VALUES longer than this are not plausible enum labels (real enums like
# ``prod``/``TLS1_2``/``GeneralPurpose`` are short); a long compound value is a
# free-form identifier (ADF run names, resource paths) and folds to __other__.
MAX_ENUM_VALUE_LEN = 40

# --- Positive VALUE allowlist (privacy-extractor-leaks DECISION 1) ------------
# ``value_distributions`` are emitted ONLY for keys here -- a conservative set of
# known LOW-CARDINALITY categorical GOVERNANCE tag keys whose VALUES are bounded
# enums (Environment -> {prod, dev, ...}, Criticality -> {High, Medium, Low}).
# Identifier-bearing keys (Owner, Migrate Project, aks-managed-cluster-name,
# ClusterNameSql, databricks-instance-name, RunName, Name, Application(free-form)
# ...) are DELIBERATELY ABSENT: their values are real person names / emails /
# RG / cluster / project names that are structurally indistinguishable from
# legitimate enums without content knowledge. Such keys keep their
# ``key_frequencies`` entry but DROP their value map. Matching is case-
# insensitive (see :func:`_value_allowed_for_key`). Curate conservatively --
# when a key is ambiguous, leave it OFF the allowlist.
VALUE_ALLOWLIST_KEYS: frozenset[str] = frozenset(
    k.lower()
    for k in {
        "Environment",
        "Env",
        "BU",
        "CostBU",
        "MobilityBU",
        "BusinessUnit",
        "CostCenter",
        "CostCentre",
        "BusinessCriticality",
        "Criticality",
        "Tier",
        "Severity",
        "Confidentiality",
        "DataClassification",
        "deployment-tool",
        "deployment_tool",
        "ManagedBy",
        "PatchGroup",
        "Backup",
        "Compliance",
        "Region",
        "CountryCode",
        "Country",
    }
)


def _value_allowed_for_key(key: str) -> bool:
    """True if a tag KEY's value_distribution may be emitted (on the allowlist).

    Case-insensitive membership in :data:`VALUE_ALLOWLIST_KEYS`. A key not on the
    allowlist keeps its ``key_frequencies`` entry but contributes NO value map.
    """
    return key.strip().lower() in VALUE_ALLOWLIST_KEYS


def _is_identifier_shaped_key(key: str) -> bool:
    """True if a tag KEY is resource-path / identifier-shaped and must be DROPPED.

    Matches:
      * Azure system tags whose key embeds an id/name (``hidden-link:``,
        ``hidden-related:``, ``__SYSTEM__...`` -- see ``_HIDDEN_KEY_PREFIXES``),
      * any key embedding ``/subscriptions/`` etc. (``_PATH_MARKERS``),
      * bare UUIDs and long hex runs,
      * custom colon-namespaced keys (``<namespace>:<suffix>``) whose namespace
        is not a known-safe Azure namespace -- the namespace prefix is the
        customer's tenant/org token and so fingerprints the tenant.
    """
    k = key.strip()
    lower = k.lower()
    if lower.startswith(_HIDDEN_KEY_PREFIXES):
        return True
    if any(marker in lower for marker in _PATH_MARKERS):
        return True
    if _UUID_RE.match(k) or _LONG_HEX_RE.match(k):
        return True
    # Custom ``namespace:suffix`` key -> drop unless the namespace is known-safe.
    if ":" in k:
        namespace = k.split(":", 1)[0].strip().lower()
        if namespace and namespace not in _SAFE_KEY_NAMESPACES:
            return True
    return False


def _is_identifier_shaped_value(value: str) -> bool:
    """True if a tag VALUE looks like a real identifier and must fold to __other__.

    Catches (regardless of repeat count):
      * whole-string UUIDs / long hex ids (e.g. databricks 32-hex),
      * EMBEDDED full-or-partial UUID fragments and >=20-char hex runs (e.g. the
        trailing fragment of an ADF pipeline run name),
      * values embedding a resource path,
      * over-length values that cannot plausibly be a bounded enum label.
    """
    v = value.strip()
    lower = v.lower()
    if _UUID_RE.match(v) or _LONG_HEX_RE.match(v):
        return True
    if _EMBEDDED_UUIDISH_RE.search(v) or _EMBEDDED_LONG_HEX_RE.search(v):
        return True
    if any(marker in lower for marker in _PATH_MARKERS):
        return True
    if len(v) > MAX_ENUM_VALUE_LEN:
        return True
    return False


def _key_frequencies(
    key_counts: pl.DataFrame, total_resources: int
) -> dict[str, float]:
    """Map each tag key to the SHARE of resources carrying it (in [0, 1])."""
    if key_counts.is_empty() or total_resources <= 0:
        return {}
    out: dict[str, float] = {}
    for row in key_counts.iter_rows(named=True):
        key = str(row["tag_key"])
        # Drop resource-path / identifier-shaped keys (hidden-link:, /subscriptions/,
        # UUID, long-hex) so subscription/resource ids never reach the profile.
        if _is_identifier_shaped_key(key):
            continue
        share = float(row["count"]) / total_resources
        # Clamp defensively; a key cannot appear on more resources than exist.
        out[key] = min(1.0, share)
    return out


def _merge_values_into_other(
    value_frame: pl.DataFrame, min_bucket_size: int
) -> dict[str, int]:
    """Fold sub-threshold tag VALUES for a single key into ``"__other__"``.

    Reuses :func:`privacy.merge_min_buckets` (no reimplementation): surviving
    values keep their counts, the dropped low-count mass accumulates in
    ``"__other__"``.
    """
    if value_frame.is_empty():
        return {}
    surviving = privacy.merge_min_buckets(value_frame, min_bucket_size)
    total = int(value_frame["count"].sum())
    kept = int(surviving["count"].sum()) if not surviving.is_empty() else 0
    dropped = total - kept

    merged: dict[str, int] = {}
    for row in surviving.iter_rows(named=True):
        value = str(row["value"])
        count = int(row["count"])
        # Shape gate: identifier-shaped values (UUID, long-hex, resource paths)
        # fold into __other__ even when they repeat above min_bucket_size, so a
        # 32-hex databricks instance id seen 50x never leaks verbatim.
        if _is_identifier_shaped_value(value):
            dropped += count
            continue
        merged[value] = merged.get(value, 0) + count
    if dropped > 0:
        merged[OTHER_BUCKET] = merged.get(OTHER_BUCKET, 0) + dropped
    return merged


def _normalize(counts: dict[str, int]) -> dict[str, float]:
    total = sum(counts.values())
    if total <= 0:
        return {}
    return {label: count / total for label, count in counts.items()}


def extract(
    key_counts: pl.DataFrame,
    value_counts: pl.DataFrame,
    total_resources: int,
    min_bucket_size: int = 5,
) -> dict[str, dict]:
    """Build the ``tag_distributions`` fragment.

    Parameters
    ----------
    key_counts:
        ``(tag_key, count)`` frame from ``reader.tag_key_counts``.
    value_counts:
        ``(tag_key, tag_value, count)`` frame from ``reader.tag_value_counts``.
    total_resources:
        Total resource count (denominator for key_frequencies).
    min_bucket_size:
        Tag values below this count fold into ``"__other__"``.

    Returns ``{"key_frequencies": {...}, "value_distributions": {...}}``.
    """
    key_frequencies = _key_frequencies(key_counts, total_resources)

    value_distributions: dict[str, dict] = {}
    if not value_counts.is_empty():
        # Group the long (tag_key, tag_value, count) frame by tag_key, rename the
        # value column to the generic ``value`` the privacy helper expects.
        for tag_key in value_counts["tag_key"].unique().to_list():
            # Drop identifier-shaped tag keys here too: value_distributions is
            # keyed by tag_key, so a hidden-link:/subscriptions/... key would
            # otherwise smuggle the full resource id in as a DICT KEY even though
            # key_frequencies already excludes it.
            if _is_identifier_shaped_key(str(tag_key)):
                continue
            # Positive value allowlist (DECISION 1): only known governance keys
            # emit value maps. Non-allowlisted safe keys still get a
            # key_frequencies entry above, but their values -- which may be real
            # person names / RG / cluster / project identifiers indistinguishable
            # from enums -- are dropped here.
            if not _value_allowed_for_key(str(tag_key)):
                continue
            per_key = (
                value_counts.filter(pl.col("tag_key") == tag_key)
                .select(
                    pl.col("tag_value").alias("value"),
                    pl.col("count"),
                )
            )
            merged = _merge_values_into_other(per_key, min_bucket_size)
            # Low-cardinality enum guard: only emit a value map that reads as a
            # bounded enum. A high-cardinality surviving set is a free-form
            # identifier space (real sub/resource/cluster names) -> drop the
            # value map entirely; key_frequencies still records the key.
            distinct_labels = sum(1 for label in merged if label != OTHER_BUCKET)
            if distinct_labels > MAX_ENUM_CARDINALITY:
                continue
            normalized = _normalize(merged)
            if normalized:
                value_distributions[str(tag_key)] = normalized

    return {
        "key_frequencies": key_frequencies,
        "value_distributions": value_distributions,
    }
