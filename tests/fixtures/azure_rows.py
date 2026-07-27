"""Shared synthetic ARG fixtures for the Phase 12 direct-tenant-scan path.

Single source of truth (mirrors ``build_fixture_duckdb.py``) for the material
Waves 1-2 consume: the PLANTED fake-identifier constants, an ARG-shape row
builder carrying those planted identifiers, and a pop-based ``_FakeExecutor``
that injects hand-authored pages through the REAL paging/materialize/denylist
path with no network and zero ``azure-*`` imported.

The planted identifiers are deliberately real-LOOKING but entirely synthetic
(D-09: never recorded from a tenant). Downstream leak tests assert these exact
strings are ABSENT from the output profile — so they MUST be distinctive. None
of them may contain any forbidden OSS brand token (the private-ecosystem product
names / customer literal enforced by ``test_scrub_gate``); that whole-tree gate
scans this file too (Pitfall 7). The neutral ``fake-*-secret`` / UUID style
matches the established fixture naming.
"""

from __future__ import annotations

from tenantless.analyzer.azure.executor import QueryPage

# --- Planted fake identifiers (D-01: subscription id, RG name, resource name,
# tag value). Distinctive synthetic strings the leak test scans the output
# profile for. Brand-token-free (scrub gate spans tests).
PLANTED_SUB = "11111111-2222-3333-4444-555555555555"
PLANTED_RG = "rg-fake-payroll-secret"
PLANTED_NAME = "fake-vm-payroll-007"
PLANTED_TAGVAL = "fake-owner-jdoe-secret"


def planted_identifiers() -> list[str]:
    """Return the four planted fake identifiers (single source of truth)."""
    return [PLANTED_SUB, PLANTED_RG, PLANTED_NAME, PLANTED_TAGVAL]


def synthetic_arg_rows(n: int) -> list[dict]:
    """Build ``n`` ARG-shape resource dicts carrying the planted identifiers.

    Each row mirrors the ARG ObjectArray ``data`` element shape (camelCase
    fields: ``id``/``name``/``type``/``location``/``resourceGroup``/
    ``subscriptionId``/``tags``/``properties``/``sku``/``kind``). The ``Owner``
    tag value is the below-allowlist ``PLANTED_TAGVAL`` (must be dropped); the
    ``Environment`` tag is a generic above-allowlist enum (safe). Producing ``n``
    copies lets a leak test push the type above ``min_bucket_size`` while the
    identifiers must NOT survive.
    """
    return [
        {
            "id": (
                f"/subscriptions/{PLANTED_SUB}/resourceGroups/{PLANTED_RG}"
                f"/providers/Microsoft.Compute/virtualMachines/{PLANTED_NAME}"
            ),
            "name": PLANTED_NAME,
            "type": "microsoft.compute/virtualmachines",
            "location": "eastus2",
            "resourceGroup": PLANTED_RG,
            "subscriptionId": PLANTED_SUB,
            "tags": {"Environment": "prod", "Owner": PLANTED_TAGVAL},
            "properties": {"vmSize": "Standard_D2s_v3"},
            "sku": {"name": "Standard_D2s_v3"},
            "kind": None,
        }
        for _ in range(n)
    ]


class _FakeExecutor:
    """Pop-based, single-use, query-agnostic injectable :class:`QueryExecutor`.

    Constructed over a list of ``(rows, skip_token)`` pages. Each ``run`` call
    pops the next page front-to-back and returns ``QueryPage(rows, skip_token)``
    — exactly one pop per call, independent of ``query``/``subscriptions``/
    ``skip_token`` (it does NOT key off them). This mirrors the canonical
    RESEARCH executor: the paging loop drives it until ``skip_token is None``.
    """

    def __init__(self, pages):
        self._pages = list(pages)

    def run(self, query, subscriptions, skip_token):
        rows, nxt = self._pages.pop(0)
        return QueryPage(rows=rows, skip_token=nxt)
