"""Default-scope subscription enumeration tests (A2 RESOLVED, core install).

These exercise ONLY the azure-free ``resolve_subscriptions`` through the shared
pop-based ``_FakeExecutor`` (D-08 injectable seam), so the file carries NO
``importorskip`` and MUST run green on the bare core install (no ``--extra
azure``). It proves three things:

1. The default no-arg scope is DECIDED by paging the static
   ``SUBSCRIPTION_ENUM_QUERY`` and returning the sorted distinct ids — the
   resource scan that follows is therefore scoped to exactly those ids.
2. An explicit ``azure:<subId,...>`` filter is the escape hatch: returned
   verbatim, with NO enumeration round-trip.
3. An empty enumeration surfaces a FIXED identifier-free error pointing the
   operator at ``azure:<subId,...>`` — no enumerated/tenant id echoed.

All planted ids are brand-token-free (the scrub gate spans tests).
"""

from __future__ import annotations

import sys
from pathlib import Path

import click
import pytest

# tests/fixtures is on sys.path via conftest.py (mirrors Waves 1-2 consumers).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures.azure_rows import _FakeExecutor  # noqa: E402

from tenantless.analyzer.azure.arg_client import (  # noqa: E402
    SUBSCRIPTION_ENUM_QUERY,
    resolve_subscriptions,
)


class _RecordingExecutor(_FakeExecutor):
    """A ``_FakeExecutor`` that records the queries it is driven with.

    The base fake is query-agnostic (it just pops pages); recording lets a test
    assert the enumeration path used the static ``SUBSCRIPTION_ENUM_QUERY``.
    """

    def __init__(self, pages):
        super().__init__(pages)
        self.queries: list[str] = []

    def run(self, query, subscriptions, skip_token):
        self.queries.append(query)
        return super().run(query, subscriptions, skip_token)


def test_default_scope_enumerates_via_static_query():
    """No-arg scope: distinct ids returned sorted; driven by the static query."""
    # Out-of-order + a duplicate prove the sorted/distinct contract.
    fake = _RecordingExecutor(
        [
            (
                [
                    {"subscriptionId": "sub-bbb"},
                    {"subscriptionId": "sub-aaa"},
                    {"subscriptionId": "sub-bbb"},
                ],
                None,
            ),
        ]
    )

    result = resolve_subscriptions(None, fake)

    assert result == ["sub-aaa", "sub-bbb"]
    # The resource scan that follows is scoped to exactly these enumerated ids.
    assert fake.queries == [SUBSCRIPTION_ENUM_QUERY]


def test_explicit_filter_returned_verbatim_without_enumeration():
    """An explicit filter short-circuits — the fake's run is never called."""
    # No pages seeded: any enumeration round-trip would raise IndexError.
    fake = _RecordingExecutor([])

    result = resolve_subscriptions(["sub-x"], fake)

    assert result == ["sub-x"]
    assert fake.queries == []


def test_empty_enumeration_raises_identifier_free_error():
    """An empty enumeration surfaces a fixed error naming no id."""
    fake = _FakeExecutor([([], None)])

    with pytest.raises(click.ClickException) as excinfo:
        resolve_subscriptions(None, fake)

    message = str(excinfo.value)
    assert "azure:<subId" in message
    # No enumerated/tenant id or credential diagnostic leaks.
    assert "sub-" not in message
    assert "%s" not in message


def test_enumeration_aborts_on_repeated_continuation_token():
    """P2-b: a repeated continuation token aborts the enumeration loop.

    Identifier-free message; prevents an unbounded enumerate-forever loop when
    ARG hands back a non-progressing/cyclic token.
    """
    fake = _FakeExecutor(
        [
            ([{"subscriptionId": "sub-aaa"}], "loop-tok"),
            ([{"subscriptionId": "sub-bbb"}], "loop-tok"),
        ]
    )

    with pytest.raises(click.ClickException) as excinfo:
        resolve_subscriptions(None, fake)

    message = str(excinfo.value)
    assert "did not progress" in message
    assert "sub-" not in message  # no enumerated id leaks
