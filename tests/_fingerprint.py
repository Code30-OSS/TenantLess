"""Canonical, DB-free content fingerprint over a full ``GenerationResult``.

The Phase-13 determinism gate (SPEED-02) must compare ``--jobs 1`` to
``--jobs N`` by *content*, never by physical/insertion order: Postgres row
order is undefined and the ``SERIAL`` PKs on dependencies/violations are assigned
at COPY time, so any order-sensitive comparison is meaningless (13-RESEARCH
"Canonical Tenant Fingerprint" + "Anti-Patterns").

``fingerprint(result)`` returns a hex sha256 built from, for every table, its
rows serialized canonically and **sorted** before hashing — so the digest is a
pure function of the generated content and is invariant to the order in which
subscriptions/resources were produced or merged. Two same-seed
``generate_tenant`` runs share a digest with TODAY's code (the helper is GREEN
immediately); it changes if ANY captured field differs.

This generalizes the existing
``test_generator_reproducibility.py::_identity_rows`` hashable-tuple-view idiom
to every collection in the result. It is DB-free: it operates only on the
in-memory ``GenerationResult``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from typing import Any, Iterable

# ASCII unit/record separators keep table blocks and rows from colliding with
# any JSON content in the serialized rows.
_FIELD_SEP = "\x1f"
_ROW_SEP = "\x1e"
_TABLE_SEP = b"\x00"


def _canon(obj: Any) -> str:
    """Stable JSON for one row: keys sorted, uuids/dates via ``str``."""
    return json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))


def _as_dict(row: Any) -> dict:
    """Dataclass (Subscription/ResourceGroup/Resource) or dict → plain dict."""
    if is_dataclass(row) and not isinstance(row, type):
        return asdict(row)
    return dict(row)


def _sorted_block(name: str, rows: Iterable[Any]) -> str:
    """One table block: every row canonicalized then **sorted by content**.

    Sorting the full canonical serialization (the natural key is a prefix of the
    content) makes the block independent of insertion / SERIAL / merge order
    (13-RESEARCH: never hash raw tuple order — always sort first).
    """
    serialized = sorted(_canon(_as_dict(r)) for r in rows)
    return name + _FIELD_SEP + _ROW_SEP.join(serialized)


def fingerprint(result: Any) -> str:
    """sha256 hex over the sorted-by-natural-key rows of every result table.

    Captures: tenant_id; subscriptions; resource_groups; resources (flattened
    out of their RGs and sorted by id — id/type/location/sku/properties/tags/
    provisioning_state/managed_by/...); dependencies; violations; cost_records;
    principals; role_assignments; and over_privilege_count. Invariant to the
    physical ordering of every collection.
    """
    tenant = result.tenant

    # resource_groups carry their resources nested; lift resources out so they
    # are fingerprinted as their own globally-sorted table (never RG-local order).
    rg_dicts: list[dict] = []
    all_resources: list[Any] = []
    for rg in tenant.resource_groups:
        d = _as_dict(rg)
        all_resources.extend(d.pop("resources", []) or [])
        rg_dicts.append(d)

    blocks: list[str] = [
        "tenant_id" + _FIELD_SEP + str(tenant.tenant_id),
        _sorted_block("subscriptions", tenant.subscriptions),
        # resource_groups already converted (resources stripped); sort by content.
        "resource_groups" + _FIELD_SEP
        + _ROW_SEP.join(sorted(_canon(d) for d in rg_dicts)),
        # resources flattened across all RGs, sorted by content (id-prefixed).
        "resources" + _FIELD_SEP
        + _ROW_SEP.join(sorted(_canon(_as_dict(r)) for r in all_resources)),
        _sorted_block("dependencies", result.dependencies),
        _sorted_block("violations", result.violations),
        _sorted_block("cost_records", result.cost_records),
        _sorted_block("principals", result.principals),
        _sorted_block("role_assignments", result.role_assignments),
        "over_privilege_count" + _FIELD_SEP + str(result.over_privilege_count),
    ]

    h = hashlib.sha256()
    for block in blocks:
        h.update(block.encode("utf-8"))
        h.update(_TABLE_SEP)
    return h.hexdigest()
