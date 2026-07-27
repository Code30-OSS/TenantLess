"""ANLZ-04 unit scaffold: resource-type co-occurrence matrix extractor.

Drives the (not-yet-existing) co-occurrence extractor on a small in-memory Polars
frame. The extractor derives, per resource group, which resource TYPES appear
together, then aggregates a co-occurrence matrix / pair-frequency table across all
RGs (so the generator can later reproduce realistic RG compositions).

Wave-0 status: ``tenantless.analyzer.extractors.cooccurrence`` does not exist yet.
``importorskip`` makes this file COLLECT but SKIP cleanly until the extractor
lands; later plans turn it green. ``uv run pytest tests/test_cooccurrence.py``
resolves to this real test.
"""

from __future__ import annotations

import polars as pl
import pytest

# Tiny known fixture: two RGs. rg-a holds {vnet, nic}; rg-b holds {vnet, vm}.
# Expected co-occurrence pairs: (nic,vnet) x1, (vm,vnet) x1; (vnet) appears in 2 RGs.
_RG_TYPES = pl.DataFrame(
    {
        "resource_group": ["rg-a", "rg-a", "rg-b", "rg-b"],
        "type": [
            "microsoft.network/virtualnetworks",
            "microsoft.network/networkinterfaces",
            "microsoft.network/virtualnetworks",
            "microsoft.compute/virtualmachines",
        ],
    }
)


def test_cooccurrence_pairs_from_rg_type_sets():
    """The extractor turns per-RG type membership into a co-occurrence table.

    Skips until ``extractors.cooccurrence`` exists (Wave-0 scaffold).
    """
    cooccurrence = pytest.importorskip(
        "tenantless.analyzer.extractors.cooccurrence",
        reason="co-occurrence extractor (ANLZ-04) lands in a later Phase-6 plan.",
    )

    result = cooccurrence.extract(_RG_TYPES)

    # The concrete return shape (pair table vs symmetric matrix) is the later
    # plan's call; this scaffold pins only that something non-empty is derived
    # from the two RGs above.
    assert result is not None
