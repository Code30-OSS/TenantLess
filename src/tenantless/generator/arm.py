"""ARM resource-ID synthesis, type-casing rule, and api-version map.

Inverse-companion of the analyzer's ``resource_types.normalize_type_key``: where
the analyzer canonicalizes a lowercase scan ``type`` to a ``Microsoft.``-leading
key, this module *consumes* the profile's already-canonical type keys and builds
the canonical ARM resource-ID paths (incl. arbitrarily nested types) that the
Phase-3/4 mock will serve.

Casing decision (Critical Finding 3 — DOCUMENTED, single stored casing)
-----------------------------------------------------------------------
``resources.type`` stores the profile's ``resource_type_distributions`` KEY
verbatim — i.e. the canonical ``Microsoft.``-leading form exactly as it appears
in the profile (``Microsoft.Compute/virtualMachines`` for test-small,
``Microsoft.compute/virtualmachines`` for the real source scan). We do NOT attempt to
recover camelCase for the type tail because that mapping is not derivable from a
lowercase source without an external vocabulary — the same reasoning the analyzer
uses (``resource_types.normalize_type_key`` docstring). :func:`canonical_type`
therefore only canonicalizes the leading ``microsoft.`` namespace token and is
idempotent on already-canonical keys, so the generator stores ONE consistent
casing per profile and the **mock owns response casing (MOCK-12)** downstream.
This is a Tampering-class *accept* in the threat register (T-02-07): a single
documented casing is a correctness convention, not a security control.

ARM ID format (RESEARCH Pattern 1)
----------------------------------
``/subscriptions/{subId}/resourceGroups/{rgName}/providers/{namespace}/{type}/{name}``
Nested types embed the parent name(s) between the type segments, e.g.
``.../providers/Microsoft.Sql/servers/{server}/databases/{db}``.

No psycopg, no duckdb, no profile string values echoed as identifiers — IDs are
built purely from synthetic subscription/RG/resource names.
"""

from __future__ import annotations

# Static per-provider recent api-version map (Assumption A3 / Open Question 3).
# The profile carries no api-version distribution; the mock accepts any version
# (MOCK-11), so a plausible recent constant per provider namespace is sufficient.
# Keyed by the lowercased provider namespace (the leading ``microsoft.<x>`` token)
# for casing-robust lookup across both profiles.
_API_VERSION_BY_PROVIDER: dict[str, str] = {
    "microsoft.compute": "2024-07-01",
    "microsoft.network": "2024-05-01",
    "microsoft.storage": "2023-05-01",
    "microsoft.web": "2023-12-01",
    "microsoft.sql": "2023-08-01-preview",
    "microsoft.keyvault": "2023-07-01",
    "microsoft.containerservice": "2024-05-01",
    "microsoft.containerregistry": "2023-07-01",
    "microsoft.operationalinsights": "2023-09-01",
    "microsoft.managedidentity": "2023-01-31",
}
_DEFAULT_API_VERSION = "2023-01-01"


def canonical_type(type_key: str) -> str:
    """Return the single consistent stored casing for ``resources.type``.

    Canonicalizes only the leading ``microsoft.`` namespace token (mirroring
    :func:`analyzer.extractors.resource_types.normalize_type_key`) and preserves
    the remainder verbatim. Idempotent on already-canonical profile keys.
    """
    if type_key is None:
        return type_key
    if type_key.lower().startswith("microsoft."):
        return "Microsoft." + type_key[len("microsoft.") :]
    return type_key


def _split_type(type_key: str) -> tuple[str, list[str]]:
    """Split a (possibly nested) type key into (namespace, [type segments]).

    ``Microsoft.Sql/servers/databases`` → ("Microsoft.Sql", ["servers", "databases"]).
    """
    canonical = canonical_type(type_key)
    namespace, _, rest = canonical.partition("/")
    segments = [s for s in rest.split("/") if s]
    return namespace, segments


def rg_id(sub_id, rg_name: str) -> str:
    """The RG ARM path PK — must match the value the pipeline wrote in 02-01.

    ``/subscriptions/{subId}/resourceGroups/{rgName}``.
    """
    return f"/subscriptions/{sub_id}/resourceGroups/{rg_name}"


def resource_id(
    sub_id,
    rg_name: str,
    type_key: str,
    resource_name: str,
    *,
    parent_name: str | None = None,
) -> str:
    """Synthesize a canonical ARM resource-ID path (incl. nested types).

    For a flat type ``Microsoft.X/y`` →
    ``.../providers/Microsoft.X/y/{name}``.

    For a nested type ``Microsoft.X/parents/children`` the parent name must be
    supplied via ``parent_name`` so the child path embeds it:
    ``.../providers/Microsoft.X/parents/{parent}/children/{name}``. Deeper
    nesting is supported by passing ``parent_name`` as a "/"-joined chain of the
    ancestor names (one per intermediate segment, root-first).
    """
    namespace, segments = _split_type(type_key)
    base = f"{rg_id(sub_id, rg_name)}/providers/{namespace}"

    if len(segments) <= 1:
        # Flat type: /providers/{namespace}/{type}/{name}
        type_tail = segments[0] if segments else ""
        return f"{base}/{type_tail}/{resource_name}"

    # Nested type: interleave each non-leaf segment with an ancestor name, then
    # append the leaf segment + this resource's name.
    parents = parent_name.split("/") if parent_name else []
    n_intermediate = len(segments) - 1
    if len(parents) < n_intermediate:
        # Defensive: pad with the resource name so the path stays well-formed
        # rather than dropping a segment (scan fidelity over strict failure).
        parents = parents + [resource_name] * (n_intermediate - len(parents))

    path = base
    for seg, ancestor in zip(segments[:-1], parents[:n_intermediate]):
        path = f"{path}/{seg}/{ancestor}"
    path = f"{path}/{segments[-1]}/{resource_name}"
    return path


def api_version_for(type_key: str) -> str:
    """Return a plausible recent api-version for the type's provider (A3).

    Keyed on the leading ``microsoft.<x>`` namespace token (case-insensitive);
    unknown providers fall back to a generic recent constant (never empty).
    """
    namespace, _ = _split_type(type_key)
    return _API_VERSION_BY_PROVIDER.get(namespace.lower(), _DEFAULT_API_VERSION)
