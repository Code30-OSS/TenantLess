"""Per-type property/sku shape extractor (source-agnostic).

For the TOP 15 resource types by count, builds ``property_distributions`` and
``sku_distributions`` -- per selected field, a NORMALIZED value-frequency map --
from the resources' ``properties``/``sku`` JSON. The result is merged INTO the
existing ``resource_type_distributions`` entries (which already carry
``frequency`` from Plan 01) under the SAME canonical type key, so no duplicate
type keys with different casing are created.

Privacy (threats T-01.1-07 / T-01.1-08):
    * Only ENUM / BOOL / SIZE style fields are emitted verbatim. The target field
      set per type mirrors ``profiles/test-small.json`` (e.g. storage:
      accessTier / supportsHttpsTrafficOnly / minimumTlsVersion). Fields NOT in
      the allow-set are excluded so free-form identifier values cannot leak.
    * Every surviving field's value map is still routed through the privacy
      min-bucket merge, so a rare value (below ``min_bucket_size``) folds into
      ``"__other__"`` rather than appearing verbatim. The build_profile denylist
      scan is the final loud gate.

Source-agnostic: imports neither ``duckdb`` nor any reader type. It consumes a
small per-type ``(field, value, count)`` frame supplied by the caller, which
holds the reader handle.
"""

from __future__ import annotations

from typing import Callable

import polars as pl

from .. import privacy
from . import resource_types

TOP_N = 15
OTHER_BUCKET = "__other__"

# Allow-list of property fields per CANONICAL type key (mirrors
# profiles/test-small.json). Restricting to enum/bool/size fields keeps
# free-form identifier values (names, ids, connection strings) out of the
# output. A type absent from this map contributes NO property fields (deny-all
# is the privacy-safe default -- see ``_field_value_maps``); the sku allow-list
# is applied analogously. Keys are canonical (normalize_type_key).
PROPERTY_FIELD_ALLOWLIST: dict[str, set[str]] = {
    "Microsoft.compute/virtualmachines": {
        "vmSize",
        "osType",
        "provisioningState",
    },
    "Microsoft.network/networkinterfaces": {
        "enableAcceleratedNetworking",
        "enableIPForwarding",
        "nicType",
    },
    "Microsoft.compute/disks": {"diskSizeGB", "osType"},
    "Microsoft.network/publicipaddresses": {
        "publicIPAllocationMethod",
        "publicIPAddressVersion",
    },
    "Microsoft.network/networksecuritygroups": {"defaultSecurityRulesOnly"},
    "Microsoft.storage/storageaccounts": {
        "accessTier",
        "supportsHttpsTrafficOnly",
        "minimumTlsVersion",
        "allowBlobPublicAccess",
    },
    "Microsoft.network/virtualnetworks": {"enableDdosProtection"},
    "Microsoft.web/sites": {"kind", "state", "httpsOnly"},
    "Microsoft.web/serverfarms": {"kind", "reserved"},
    "Microsoft.sql/servers": {
        "version",
        "publicNetworkAccess",
        "minimalTlsVersion",
    },
    "Microsoft.sql/servers/databases": {
        "status",
        "collation",
        "transparentDataEncryption",
    },
    "Microsoft.keyvault/vaults": {
        "enableSoftDelete",
        "enablePurgeProtection",
        "enableRbacAuthorization",
    },
    "Microsoft.containerservice/managedclusters": {
        "kubernetesVersion",
        "enableRBAC",
        "networkPlugin",
    },
    "Microsoft.operationalinsights/workspaces": {
        "publicNetworkAccessForIngestion",
    },
    # --- Real top-15 types (privacy-extractor-leaks fix) -------------------
    # Curated enum/bool-only safe fields. Identifier-bearing fields
    # (ids, connection strings, settings blobs, *Connections, ipConfigurations,
    # scope/actionGroups, AppId/TenantId, backup/file-size) are DELIBERATELY
    # excluded -- when unsure, exclude.
    "Microsoft.network/privatednszones/virtualnetworklinks": {
        "virtualNetworkLinkState",
        "resolutionPolicy",
        "registrationEnabled",
        "provisioningState",
    },
    "Microsoft.compute/virtualmachines/extensions": {
        "type",
        "autoUpgradeMinorVersion",
        "enableAutomaticUpgrade",
        "publisher",
        "typeHandlerVersion",
    },
    "Microsoft.network/privateendpoints": {
        "provisioningState",
    },
    "Microsoft.maintenance/maintenanceconfigurations": {
        "configurationType",
        "visibility",
        "namespace",
    },
    "Microsoft.alertsmanagement/smartdetectoralertrules": {
        "frequency",
        "state",
    },
    "Microsoft.insights/components": {
        "publicNetworkAccessForIngestion",
        "Flow_Type",
        "DisableLocalAuth",
    },
    "Microsoft.azurearcdata/sqlserverinstances/databases": {
        "createMode",
    },
}

# Allow-list of sku fields per canonical type key. sku objects are small and
# enum-like (name / tier / family) across Azure; allow those three everywhere.
SKU_FIELD_ALLOWLIST_DEFAULT: set[str] = {"name", "tier", "family"}


def _normalize(counts: dict[str, int]) -> dict[str, float]:
    total = sum(counts.values())
    if total <= 0:
        return {}
    return {label: count / total for label, count in counts.items()}


def _field_value_maps(
    frame: pl.DataFrame,
    allowed_fields: set[str] | None,
    min_bucket_size: int,
) -> dict[str, dict[str, float]]:
    """Build ``{field: {value: share}}`` from a ``(field, value, count)`` frame.

    Fields outside ``allowed_fields`` are dropped. ``allowed_fields`` of ``None``
    means DENY-ALL (an unlisted type contributes NO fields) -- this is the
    privacy-safe default that matches the module docstring ("a type absent from
    this map contributes NO property fields"). Each surviving field's value
    histogram is min-bucket merged (sub-threshold -> ``"__other__"``) then
    normalized.
    """
    if frame.is_empty():
        return {}

    # None => deny-all. Free-form identifier fields on an unlisted type must
    # never leak; only explicitly allow-listed enum/bool fields are emitted.
    allowed: set[str] = allowed_fields if allowed_fields is not None else set()
    if not allowed:
        return {}

    out: dict[str, dict[str, float]] = {}
    for field in frame["field"].unique().to_list():
        field_s = str(field)
        if field_s not in allowed:
            continue
        per_field = frame.filter(pl.col("field") == field)

        surviving = privacy.merge_min_buckets(per_field, min_bucket_size)
        total = int(per_field["count"].sum())
        kept = int(surviving["count"].sum()) if not surviving.is_empty() else 0
        dropped = total - kept

        counts: dict[str, int] = {}
        for row in surviving.iter_rows(named=True):
            value = "null" if row["value"] is None else str(row["value"])
            counts[value] = counts.get(value, 0) + int(row["count"])
        if dropped > 0:
            counts[OTHER_BUCKET] = counts.get(OTHER_BUCKET, 0) + dropped

        normalized = _normalize(counts)
        if normalized:
            out[field_s] = normalized
    return out


def top_type_keys(
    resource_type_distributions: dict[str, dict], top_n: int = TOP_N
) -> list[str]:
    """Return the canonical keys of the top ``top_n`` types by frequency."""
    ranked = sorted(
        resource_type_distributions.items(),
        key=lambda kv: (-float(kv[1].get("frequency", 0.0)), kv[0]),
    )
    return [key for key, _ in ranked[:top_n]]


# Allow-list of kind fields. ``kind`` is read into a single synthetic field
# named ``kind`` by the reader (``type_kind_counts``), so the allow-set is just
# that one field. The resulting value map is flattened to a top-level
# ``kind_distributions`` (``{value: share}``), not nested under a field.
KIND_FIELD_ALLOWLIST_DEFAULT: set[str] = {"kind"}


def extract_into(
    resource_type_distributions: dict[str, dict],
    property_frame_for: Callable[[str], pl.DataFrame],
    sku_frame_for: Callable[[str], pl.DataFrame],
    min_bucket_size: int = 5,
    *,
    kind_frame_for: Callable[[str], pl.DataFrame] | None = None,
    top_n: int = TOP_N,
) -> dict[str, dict]:
    """Attach property/sku/kind shapes to the top ``top_n`` types, IN PLACE-safe.

    Parameters
    ----------
    resource_type_distributions:
        The Plan 01 fragment ``{<canonical type>: {"frequency", "property_distributions"}}``.
    property_frame_for / sku_frame_for:
        Callables that, given a RAW (lowercase, source-cased) resource type,
        return that type's ``(field, value, count)`` frame from the reader. The
        caller owns the reader handle so this module stays source-agnostic.
    min_bucket_size:
        Sub-threshold field values fold into ``"__other__"``.
    kind_frame_for:
        Optional callable returning a ``(field, value, count)`` frame for the
        type's ``kind`` (ANLZ-05). When provided and non-empty, the type's
        ``kind`` value distribution is attached as ``kind_distributions``
        (``{value: share}``). ``api-version`` is intentionally absent (no source).
    top_n:
        Number of types (by frequency) that receive property/sku shapes.

    Returns the same dict with property/sku/kind distributions filled for the top
    types. Types outside the top-N keep their empty ``property_distributions``.
    """
    if not resource_type_distributions:
        return resource_type_distributions

    for canonical_key in top_type_keys(resource_type_distributions, top_n):
        entry = resource_type_distributions[canonical_key]
        raw_type = _to_raw_type(canonical_key)

        allowed_props = PROPERTY_FIELD_ALLOWLIST.get(canonical_key)
        prop_maps = _field_value_maps(
            property_frame_for(raw_type), allowed_props, min_bucket_size
        )
        if prop_maps:
            entry["property_distributions"] = prop_maps

        sku_maps = _field_value_maps(
            sku_frame_for(raw_type), SKU_FIELD_ALLOWLIST_DEFAULT, min_bucket_size
        )
        if sku_maps:
            entry["sku_distributions"] = sku_maps

        # ANLZ-05: kind value distribution (from the ``kind`` column on the DuckDB
        # path). Flatten the single ``kind`` field's value map to a top-level
        # ``kind_distributions``.
        if kind_frame_for is not None:
            kind_maps = _field_value_maps(
                kind_frame_for(raw_type),
                KIND_FIELD_ALLOWLIST_DEFAULT,
                min_bucket_size,
            )
            kind_dist = kind_maps.get("kind")
            if kind_dist:
                entry["kind_distributions"] = kind_dist

    return resource_type_distributions


def _to_raw_type(canonical_key: str) -> str:
    """Invert ``normalize_type_key`` for the reader query.

    ``normalize_type_key`` only canonicalizes the leading ``Microsoft.`` token
    from a lowercase source; the real ``type`` column is fully lowercase, so the
    raw type for a query is the canonical key lowercased. This is consistent with
    the Plan-01 casing rule (``microsoft.X/y`` <-> ``Microsoft.X/y``).
    """
    # Idempotency guard: if the key already round-trips, lowercasing the leading
    # namespace recovers the source form used in the resources.type column.
    if canonical_key.startswith("Microsoft."):
        return "microsoft." + canonical_key[len("Microsoft."):]
    return canonical_key


# Re-export so callers can reference the casing rule from one place.
normalize_type_key = resource_types.normalize_type_key
