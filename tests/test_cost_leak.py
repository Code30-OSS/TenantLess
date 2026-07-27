"""COST-05 privacy leak guard for the cost extractor (Phase-6 CR-01 rule).

A new string/number-emitting analyzer path silently bypasses the data boundary
unless it reapplies the privacy controls explicitly. This test feeds
``extract_cost_distributions`` a ``(type, monthly_cost)`` frame that ALSO carries
fake-real ``resource_id``/``subscription_id`` strings as extra columns, then
proves NONE of those identifiers appear anywhere in the emitted
``cost_distributions`` dict -- only canonical type keys + numeric lognormal
params cross the boundary.
"""

from __future__ import annotations

import json

import polars as pl
import pytest

from tenantless.analyzer import privacy
from tenantless.analyzer.extractors.cost import extract_cost_distributions

# Fake-but-real-looking identifiers planted on the cost rows. If the extractor
# read any id column into the output, one of these would surface.
FAKE_RESOURCE_ID = (
    "/subscriptions/11111111-2222-3333-4444-555555555555/resourceGroups/"
    "rg-payroll/providers/Microsoft.Compute/virtualMachines/fake-vm-payroll-007"
)
FAKE_SUBSCRIPTION_ID = "FAKE-HUB-EMEA-PROD-00000000-aaaa-bbbb-cccc-dddddddddddd"

VM_TYPE = "microsoft.compute/virtualmachines"


def _leaky_frame() -> pl.DataFrame:
    """A cost frame whose rows carry fake-real id columns alongside the data."""
    return pl.DataFrame(
        {
            "type": [VM_TYPE] * 5,
            "monthly_cost": [12.0, 18.0, 25.0, 31.0, 44.0],
            "resource_id": [FAKE_RESOURCE_ID] * 5,
            "subscription_id": [FAKE_SUBSCRIPTION_ID] * 5,
        }
    )


def test_no_real_identifier_crosses_into_cost_distributions():
    """The assembled cost dict contains none of the fake-real id strings."""
    out = extract_cost_distributions(_leaky_frame(), min_bucket_size=5)

    blob = json.dumps(out)
    assert FAKE_RESOURCE_ID not in blob
    assert FAKE_SUBSCRIPTION_ID not in blob
    assert "fake-vm-payroll-007" not in blob
    assert "FAKE-HUB-EMEA-PROD" not in blob


def test_denylist_scan_passes_over_emitted_cost_dict():
    """scan_denylist over the cost dict does not trip on the planted ids."""
    out = extract_cost_distributions(_leaky_frame(), min_bucket_size=5)

    # The shared boundary guard raises DenylistLeakError if any term leaks.
    privacy.scan_denylist(
        out,
        [FAKE_RESOURCE_ID, FAKE_SUBSCRIPTION_ID, "fake-vm-payroll-007"],
    )

    # Sanity: the guard DOES trip when a denylisted token really is present,
    # so the clean pass above is meaningful (not a no-op).
    with pytest.raises(privacy.DenylistLeakError):
        privacy.scan_denylist({"fake-vm-payroll-007": 1}, ["fake-vm-payroll-007"])
