"""Plan 12-04 profile.py seam tests (owned by THIS plan).

Covers the `azure:` branch of ``_parse_source`` + ``build_profile``: parse-source
dispatch, the injected-executor happy path (default-scope enumeration resolved
through the SAME executor BEFORE the resource scan), the explicit-filter
short-circuit (no enumeration round-trip), and the byte-preserved duckdb
fail-closed gate after its relocation below the file∪derived term union.

All four behaviors drive the REAL resolve_subscriptions -> open_azure ->
DuckDB -> build_profile path through the pop-based ``_FakeExecutor`` (Plan
12-01) with NO network and zero ``azure-*`` imported -- they run on the core CI
install.
"""

from __future__ import annotations

import pytest

from tenantless.analyzer.azure.arg_client import SUBSCRIPTION_ENUM_QUERY
from tenantless.analyzer.azure.materialize import (
    ARG_PROJECTION,
    RESOURCE_GROUP_ENUM_QUERY,
)
from tenantless.analyzer.profile import (
    DenylistRequiredError,
    _parse_source,
    build_profile,
)

from fixtures.azure_rows import (
    PLANTED_SUB,
    _FakeExecutor,
    synthetic_arg_rows,
)


class _RecordingExecutor(_FakeExecutor):
    """A pop-based ``_FakeExecutor`` that records each ``run`` call in order.

    Lets a test assert WHICH query a pop answered (enumeration vs resource scan)
    and the resolved subscription scope handed to the resource scan.
    """

    def __init__(self, pages):
        super().__init__(pages)
        self.calls: list[tuple[str, list[str] | None, str | None]] = []

    def run(self, query, subscriptions, skip_token):
        self.calls.append((query, subscriptions, skip_token))
        return super().run(query, subscriptions, skip_token)


def test_parse_source_recognizes_azure_scheme():
    """``azure:`` parses to ``("azure", <sub-filter csv>)``; empty target = all."""
    assert _parse_source("azure:") == ("azure", "")
    assert _parse_source("azure:sub-a,sub-b") == ("azure", "sub-a,sub-b")


def test_no_filter_enumerates_default_scope_before_resource_scan(tmp_path):
    """`azure:` with no explicit filter enumerates the default scope FIRST.

    Pop 1 answers ``resolve_subscriptions``' enumeration query (default scope
    resolved to ``[PLANTED_SUB]``); pop 2 answers ``open_azure``'s resource scan
    carrying the resolved scope. The auto-derived denylist is non-empty so the
    run does NOT raise ``DenylistRequiredError``; a schema-valid profile is
    written with ``provenance.source == "azure"`` (scheme only).
    """
    executor = _RecordingExecutor(
        [
            ([{"subscriptionId": PLANTED_SUB}], None),  # sub enumeration
            (synthetic_arg_rows(8), None),  # resource scan
            ([], None),  # RG enumeration (empty)
        ]
    )
    out = tmp_path / "derived.json"

    profile = build_profile(source="azure:", out=out, _executor=executor)

    # Order: sub enumeration -> resource scan -> RG enumeration.
    assert len(executor.calls) == 3
    assert executor.calls[0][0] == SUBSCRIPTION_ENUM_QUERY
    assert executor.calls[0][1] is None  # enumeration is scope-agnostic
    # ...the resource scan ran SECOND, carrying the resolved scope.
    assert executor.calls[1][0] == ARG_PROJECTION
    assert executor.calls[1][1] == [PLANTED_SUB]
    # ...and the RG enumeration ran THIRD, scoped to the resolved subscriptions.
    assert executor.calls[2][0] == RESOURCE_GROUP_ENUM_QUERY
    assert executor.calls[2][1] == [PLANTED_SUB]

    assert out.exists()
    assert profile["provenance"]["source"] == "azure"
    # The surviving VM type proves the path actually fitted real rows end to end.
    assert profile["resource_type_distributions"]


def test_explicit_filter_short_circuits_enumeration(tmp_path):
    """An explicit ``azure:<subId,...>`` filter is honored verbatim, no enum pop.

    The first seeded page IS the resource page -- ``resolve_subscriptions``
    returns the filter without an enumeration round-trip; the RG enumeration
    still runs (scoped to the explicit filter).
    """
    executor = _RecordingExecutor(
        [(synthetic_arg_rows(8), None), ([], None)]  # resource scan + RG enum
    )
    out = tmp_path / "derived.json"

    build_profile(source="azure:sub-a,sub-b", out=out, _executor=executor)

    # No SUBSCRIPTION enumeration: resource scan FIRST, then RG enumeration.
    assert len(executor.calls) == 2
    assert executor.calls[0][0] == ARG_PROJECTION
    assert executor.calls[0][1] == ["sub-a", "sub-b"]
    assert executor.calls[1][0] == RESOURCE_GROUP_ENUM_QUERY
    assert executor.calls[1][1] == ["sub-a", "sub-b"]


def test_duckdb_fail_closed_gate_preserved_after_relocation(fixture_duckdb, tmp_path):
    """The duckdb path STILL fails closed after the gate moved below the union.

    With no denylist and ``allow_no_denylist=False`` the run raises
    ``DenylistRequiredError`` before any output is written (the duckdb branch
    derives no terms, so the relocated gate fires exactly as before).
    """
    out = tmp_path / "should-not-write.json"
    with pytest.raises(DenylistRequiredError):
        build_profile(source=f"duckdb:{fixture_duckdb}", out=out, min_bucket_size=5)
    assert not out.exists()
