"""ANLZ-08 unit + privacy scaffold: tokenized resource-naming patterns.

Drives the (not-yet-existing) naming extractor on a small in-memory Polars frame.
The extractor learns tokenized naming TEMPLATES (e.g. ``<prefix>-<env>-<nnn>``)
from real resource names WITHOUT leaking any real name into the output — the
HARD privacy bar for Phase 6.

The no-leak assertion is seeded from ``build_fixture_duckdb.fake_identifiers()``
(the single source of truth for the fixture's fake-but-real-looking identifiers),
so a regression that echoes a raw name into the tokenized output is caught here.

Wave-0 status: ``tenantless.analyzer.extractors.naming`` does not exist yet.
``importorskip`` makes this file COLLECT but SKIP cleanly; later plans turn it
green. ``uv run pytest tests/test_naming.py`` resolves to these real tests.
"""

from __future__ import annotations

import polars as pl
import pytest

from fixtures.build_fixture_duckdb import (
    FAKE_RESOURCE_NAME,
    FAKE_SUB_DISPLAY_NAME,
    fake_identifiers,
)

# A small frame of resource names including the fixture's fake real-looking ones,
# so the privacy assertion has material that MUST NOT survive tokenization.
_NAMES = pl.DataFrame(
    {
        "name": [
            "vm-prod-001",
            "vm-prod-002",
            "vm-dev-003",
            FAKE_RESOURCE_NAME,  # fake-vm-payroll-007
            FAKE_SUB_DISPLAY_NAME,  # FAKE-HUB-EMEA-PROD
        ],
        "type": ["microsoft.compute/virtualmachines"] * 5,
    }
)


def test_naming_templates_derived():
    """The extractor derives tokenized naming templates from resource names.

    Skips until ``extractors.naming`` exists (Wave-0 scaffold).
    """
    naming = pytest.importorskip(
        "tenantless.analyzer.extractors.naming",
        reason="naming extractor (ANLZ-08) lands in a later Phase-6 plan.",
    )
    result = naming.extract(_NAMES)
    assert result is not None


def test_naming_no_real_name_leaks():
    """HARD privacy bar: no real/fake-real identifier survives tokenization.

    Seeded from ``fake_identifiers()`` so the denylist material is a single
    source of truth. Skips until the extractor lands; once green, the tokenized
    output must contain none of the raw identifiers.
    """
    naming = pytest.importorskip(
        "tenantless.analyzer.extractors.naming",
        reason="naming extractor (ANLZ-08) lands in a later Phase-6 plan.",
    )
    result = naming.extract(_NAMES)
    blob = str(result)
    for ident in fake_identifiers():
        assert ident not in blob, f"real identifier leaked into naming output: {ident}"
