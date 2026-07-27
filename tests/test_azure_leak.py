"""SCAN-02 leak guard for the direct ARG scan path (the phase's central gate).

GIVEN synthetic ARG rows carrying planted fake identifiers (subscription id, RG
name, resource name, and an identifier-bearing ``Owner`` tag value), WHEN they
are fitted through the REAL path resolve_subscriptions -> materialize -> DuckDB
-> build_profile (executor injected, ``source="azure:"``), THEN zero planted
identifiers appear in the output profile AND the denylist gate is meaningful
(demonstrably trips on a deliberately-present token).

Mirrors ``tests/test_cost_leak.py`` (absence assertion + meaningful
``scan_denylist``). Runs on the CORE install -- no ``azure-*`` imported.
"""

from __future__ import annotations

import json

import pytest

from tenantless.analyzer import privacy
from tenantless.analyzer.profile import build_profile

from fixtures.azure_rows import (
    PLANTED_NAME,
    PLANTED_RG,
    PLANTED_SUB,
    PLANTED_TAGVAL,
    _FakeExecutor,
    planted_identifiers,
    synthetic_arg_rows,
)


def _build_azure_profile(tmp_path):
    """Fit a profile through the full ARG path with planted-identifier rows.

    Pop 1 answers ``resolve_subscriptions``' enumeration (default scope ->
    ``[PLANTED_SUB]``); pop 2 answers ``open_azure``'s resource scan; pop 3
    answers the RG enumeration (empty here). The 8 identical VMs push the type
    above ``min_bucket_size`` so it survives -- the surviving type proves the
    path ran end to end while the identifiers must NOT.
    """
    executor = _FakeExecutor(
        [
            ([{"subscriptionId": PLANTED_SUB}], None),  # sub enumeration
            (synthetic_arg_rows(8), None),  # resource scan
            ([], None),  # RG enumeration (empty)
        ]
    )
    out = tmp_path / "derived.json"
    return build_profile(source="azure:", out=out, _executor=executor)


def test_no_planted_identifier_reaches_profile(tmp_path):
    """None of the four planted identifiers appears in the emitted profile."""
    profile = _build_azure_profile(tmp_path)

    blob = json.dumps(profile)
    assert PLANTED_SUB not in blob
    assert PLANTED_RG not in blob
    assert PLANTED_NAME not in blob
    # Owner is NOT on the value allowlist -> its value map is dropped.
    assert PLANTED_TAGVAL not in blob

    # The surviving VM type proves the ARG path actually fitted real rows end to
    # end (enumeration pop included), so the clean blob is not a vacuous pass.
    assert profile["resource_type_distributions"]
    assert profile["provenance"]["source"] == "azure"


def test_denylist_scan_over_clean_profile_is_meaningful(tmp_path):
    """scan_denylist passes over the clean profile but DOES trip when forced."""
    profile = _build_azure_profile(tmp_path)

    # The shared boundary guard does not trip on the planted ids (guards held).
    privacy.scan_denylist(profile, planted_identifiers())

    # Sanity: the guard DOES trip when a denylisted token really is present, so
    # the clean pass above is meaningful (not a no-op).
    with pytest.raises(privacy.DenylistLeakError):
        privacy.scan_denylist({"fake-vm-payroll-007": 1}, ["fake-vm-payroll-007"])


def _arg_row(sub, rg, name, tags):
    """One ARG-shape row with the given scope + tags."""
    return {
        "id": f"/subscriptions/{sub}/resourceGroups/{rg}/providers/p/x/{name}",
        "name": name,
        "type": "microsoft.compute/virtualmachines",
        "location": "eastus2",
        "resourceGroup": rg,
        "subscriptionId": sub,
        "tags": tags,
        "properties": {"vmSize": "Standard_D2s_v3"},
        "sku": {"name": "Standard_D2s_v3"},
        "kind": None,
    }


def test_published_enum_value_colliding_with_name_does_not_abort(tmp_path):
    """P1-c: a valid scan must NOT abort when a published governance enum value
    (Environment=prod) collides with a real RG name ("prod").

    Pre-fix the RG name "prod" entered the denylist and then ``scan_denylist``
    tripped on the legitimately-published ``Environment=prod`` value, raising
    DenylistLeakError and aborting an otherwise-clean scan.
    """
    sub = "22222222-3333-4444-5555-666666666666"
    # >= min_bucket_size identical VMs in an RG literally named "prod", all
    # tagged Environment=prod (the published enum value == the RG name token).
    rows = [_arg_row(sub, "prod", f"vm-{i}", {"Environment": "prod"}) for i in range(8)]
    executor = _FakeExecutor(
        [([{"subscriptionId": sub}], None), (rows, None), ([], None)]
    )
    out = tmp_path / "derived.json"

    # Must not raise DenylistLeakError (or any error) — the scan is valid.
    profile = build_profile(source="azure:", out=out, _executor=executor)

    # The collision condition was real: "prod" IS published in the output.
    blob = json.dumps(profile)
    assert "prod" in blob


def test_custom_tag_key_absent_from_profile(tmp_path):
    """P1-b full path: a tenant-specific custom tag key + its value never reach
    the output profile, while a generic key is still represented."""
    sub = "33333333-4444-5555-6666-777777777777"
    rows = [
        _arg_row(sub, "rg1", f"vm-{i}",
                 {"Environment": "prod", "ContosoCostCode": "cc-9981-secret"})
        for i in range(8)
    ]
    executor = _FakeExecutor(
        [([{"subscriptionId": sub}], None), (rows, None), ([], None)]
    )
    out = tmp_path / "derived.json"

    profile = build_profile(source="azure:", out=out, _executor=executor)

    blob = json.dumps(profile)
    assert "ContosoCostCode" not in blob  # custom key dropped
    assert "cc-9981-secret" not in blob  # its value dropped
    assert "Environment" in blob  # generic key still represented
