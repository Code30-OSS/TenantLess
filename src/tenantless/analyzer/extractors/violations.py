"""Governance-violation extractor (source-agnostic).

Maps the real ``findings`` table's ``finding_type`` values to the simulator's
violation-type vocabulary (the keys of ``governance_violations.type_frequencies``
in profiles/test-small.json) and emits NORMALIZED rates:

    type_frequencies[<VIOL_TYPE>] = findings_of_that_type / total_resources

clamped to ``[0, 1]``.

Rules:
    * The mapping is an explicit, documented ``finding_type -> VIOL_*`` table.
      Finding types NOT in the table are DROPPED (documented) -- the simulator
      vocabulary is fixed and unmapped detectors have no slot.
    * Buckets below ``min_bucket_size`` are dropped (privacy min-aggregation;
      a violation type seen too few times must not fingerprint a real tenant).
    * Multiple finding types mapping to the same VIOL_* accumulate.

Output keys are therefore always a subset of the known vocabulary with rates in
``[0, 1]``.

Source-agnostic: imports neither ``duckdb`` nor any reader type.
"""

from __future__ import annotations

import polars as pl

# finding_type (real detector name, from the source-scan findings table) -> the
# simulator violation vocabulary key (from profiles/test-small.json). Documented
# and intentionally conservative: only mappings with a clear semantic match are
# included; everything else is dropped.
FINDING_TYPE_TO_VIOLATION: dict[str, str] = {
    # Idle / unattached compute & network resources -> backup/hygiene violations.
    "unattached_nics": "VM_NO_BACKUP",
    "unattached_disks": "DISK_UNENCRYPTED",
    "stopped_vms": "VM_NO_BACKUP",
    "unused_public_ips": "VM_PUBLIC_IP",
    # Tagging hygiene.
    "empty_resource_groups": "TAG_MISSING_OWNER",
    # App-service hygiene (best-effort generic mapping).
    "unused_app_service_plans": "TAG_MISSING_COSTCENTER",
}

# The full known vocabulary, used to assert outputs stay within it.
KNOWN_VIOLATION_VOCABULARY: frozenset[str] = frozenset(
    {
        "STORAGE_NO_ENCRYPTION",
        "STORAGE_PUBLIC_ACCESS",
        "STORAGE_HTTP_ALLOWED",
        "STORAGE_OLD_TLS",
        "NSG_OPEN_SSH",
        "NSG_OPEN_RDP",
        "NSG_OPEN_ALL",
        "KV_NO_SOFT_DELETE",
        "KV_NO_PURGE_PROTECT",
        "VM_NO_BACKUP",
        "VM_PUBLIC_IP",
        "TAG_MISSING_ENV",
        "TAG_MISSING_OWNER",
        "TAG_MISSING_COSTCENTER",
        "SQL_NO_AUDIT",
        "SQL_NO_TDE",
        "AKS_RBAC_DISABLED",
        "DISK_UNENCRYPTED",
    }
)


def _clamp_rate(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _resolve(finding_type: str) -> str | None:
    """Resolve a source ``finding_type`` to a simulator violation key, or None.

    Two source families reach this extractor:

    1. A THIRD-PARTY scan, whose detector names are its own
       (``unattached_nics``, ``stopped_vms``, ...). These resolve through the
       explicit :data:`FINDING_TYPE_TO_VIOLATION` table; anything absent from it
       is dropped, because guessing a semantic match would silently invent a
       violation rate the source never measured.

    2. A TENANTLESS-GENERATED estate re-analyzed to derive a profile (the
       bootstrap chain that produces the bundled ``enterprise`` profile). Such a
       source already emits the simulator vocabulary verbatim, so the correct
       mapping is the IDENTITY -- not a guess, and not a drop.

    Case 2 is checked second so an explicit table entry always wins. Without it
    the whole simulator vocabulary falls through the ``None`` branch and
    ``type_frequencies`` comes back empty, which is why a profile derived from a
    generated estate previously modelled a violation-free tenant.
    """
    mapped = FINDING_TYPE_TO_VIOLATION.get(finding_type)
    if mapped is not None:
        return mapped
    if finding_type in KNOWN_VIOLATION_VOCABULARY:
        return finding_type
    return None


def extract(
    finding_counts: pl.DataFrame,
    total_resources: int,
    min_bucket_size: int = 5,
) -> dict[str, float]:
    """Build ``governance_violations.type_frequencies`` from finding counts.

    Parameters
    ----------
    finding_counts:
        ``(finding_type, count)`` frame from ``reader.finding_type_counts``.
    total_resources:
        Denominator for normalized rates.
    min_bucket_size:
        Finding-type buckets below this count are dropped.

    Returns ``{<VIOL_TYPE>: rate in [0, 1]}`` -- keys are a subset of
    ``KNOWN_VIOLATION_VOCABULARY``; unmapped finding types are dropped.
    """
    if finding_counts.is_empty() or total_resources <= 0:
        return {}

    accumulated: dict[str, int] = {}
    for row in finding_counts.iter_rows(named=True):
        finding_type = str(row["finding_type"])
        count = int(row["count"])
        if count < min_bucket_size:
            continue  # privacy min-aggregation
        violation = _resolve(finding_type)
        if violation is None:
            continue  # unmapped detector -> dropped
        accumulated[violation] = accumulated.get(violation, 0) + count

    return {
        viol: _clamp_rate(count / total_resources)
        for viol, count in accumulated.items()
    }
