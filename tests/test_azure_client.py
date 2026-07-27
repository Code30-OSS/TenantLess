"""Real-wrapper construction + auth-error-hygiene test (T-12-01, D-07).

This is the ONLY test that needs the optional ``azure`` extra, so it guards with
``pytest.importorskip("azure.mgmt.resourcegraph")`` at module top — exactly the
``conftest.py::pg_conn`` skip convention — and is collected-but-skipped on the
bare core install. It proves two things WITHOUT reaching a live tenant:

1. ``make_arg_executor()`` constructs and returns an object exposing ``run``.
2. An azure ``ClientAuthenticationError`` whose aggregated message names a
   (synthetic) tenant/account is surfaced as a FIXED, identifier-free
   ``click.ClickException`` with ``__cause__ is None`` — proving the chained
   azure message (the leak surface) was dropped via ``raise ... from None``.
"""

from __future__ import annotations

import click
import pytest

pytest.importorskip("azure.mgmt.resourcegraph")

from azure.core.exceptions import (  # noqa: E402
    ClientAuthenticationError,
    HttpResponseError,
)

from tenantless.analyzer.azure.arg_client import make_arg_executor  # noqa: E402

# Synthetic tenant/account substrings the surfaced error must NEVER echo.
# Brand-token-free (the scrub gate spans tests).
_PLANTED_TENANT = "00000000-1111-2222-3333-444444444444"
_PLANTED_ACCOUNT = "admin@fake-corp-example"
_PLANTED_CHAIN_MESSAGE = (
    f"DefaultAzureCredential failed: EnvironmentCredential tenant "
    f"{_PLANTED_TENANT} account {_PLANTED_ACCOUNT} could not be authenticated"
)

# Must equal arg_client._AUTH_ERROR_MESSAGE verbatim (kept local so the test
# pins the exact surfaced string rather than re-importing the private const).
_FIXED_AUTH_MESSAGE = (
    "Azure authentication failed. Run `az login` or set service-principal "
    "env vars. Use AZURE_TOKEN_CREDENTIALS to restrict the credential chain."
)

# A non-auth ARG error (403/throttle/invalid query) whose raw message embeds the
# in-scope subscription id + caller principal — the WR-01 leak surface.
_PLANTED_SUB = "11111111-2222-3333-4444-555555555555"
_PLANTED_PRINCIPAL = "55555555-6666-7777-8888-999999999999"
_PLANTED_403_MESSAGE = (
    f"(AuthorizationFailed) The client '{_PLANTED_PRINCIPAL}' does not have "
    f"authorization to perform action over scope "
    f"'/subscriptions/{_PLANTED_SUB}'."
)
# Must equal arg_client._QUERY_ERROR_MESSAGE verbatim.
_FIXED_QUERY_MESSAGE = (
    "Azure Resource Graph query failed (permission denied, throttling, or an "
    "invalid scope/query). The underlying error detail was suppressed because "
    "it can contain tenant identifiers (subscription id / caller principal)."
)


class _ForbiddenClient:
    """A ``ResourceGraphClient`` stand-in whose query raises a non-auth 403.

    The error message embeds a synthetic subscription id + caller principal —
    the WR-01 leak surface ``run`` must suppress (it is NOT a credential error,
    so the auth-only handler used to let it through).
    """

    def __init__(self, *args, **kwargs):
        pass

    def resources(self, request):
        raise HttpResponseError(message=_PLANTED_403_MESSAGE)


class _FakeCredential:
    """A no-op ``DefaultAzureCredential`` stand-in (no live auth at all)."""

    def __init__(self, *args, **kwargs):
        pass


class _AuthFailingClient:
    """A ``ResourceGraphClient`` stand-in whose query raises an auth error.

    The error's aggregated message names a synthetic tenant/account — the exact
    leak surface D-07 closes; ``run`` must never surface it.
    """

    def __init__(self, *args, **kwargs):
        pass

    def resources(self, request):
        raise ClientAuthenticationError(message=_PLANTED_CHAIN_MESSAGE)


def _patch_azure(monkeypatch):
    monkeypatch.setattr(
        "azure.identity.DefaultAzureCredential", _FakeCredential
    )
    monkeypatch.setattr(
        "azure.mgmt.resourcegraph.ResourceGraphClient", _AuthFailingClient
    )


def test_make_arg_executor_returns_object_with_callable_run(monkeypatch):
    """Construction yields a QueryExecutor; it never reaches a live tenant."""
    _patch_azure(monkeypatch)
    executor = make_arg_executor()
    assert callable(getattr(executor, "run", None))


def test_auth_error_is_identifier_free(monkeypatch):
    """An auth failure surfaces the FIXED message with no planted id and no cause."""
    _patch_azure(monkeypatch)
    executor = make_arg_executor()

    with pytest.raises(click.ClickException) as excinfo:
        executor.run("Resources | project id", ["sub-x"], None)

    exc = excinfo.value
    assert str(exc) == _FIXED_AUTH_MESSAGE
    assert _PLANTED_TENANT not in str(exc)
    assert _PLANTED_ACCOUNT not in str(exc)
    # `raise ... from None` dropped the chained azure exception entirely.
    assert exc.__cause__ is None


def test_non_auth_error_is_identifier_free(monkeypatch):
    """A non-auth 403 (WR-01) surfaces the FIXED query message — no real id/cause.

    A raw ARG 403 names the in-scope subscription id + caller principal; the
    pre-fix handler caught only auth errors, so this propagated verbatim to
    operator stderr. The broadened ``AzureError`` catch must suppress it.
    """
    monkeypatch.setattr(
        "azure.identity.DefaultAzureCredential", _FakeCredential
    )
    monkeypatch.setattr(
        "azure.mgmt.resourcegraph.ResourceGraphClient", _ForbiddenClient
    )
    executor = make_arg_executor()

    with pytest.raises(click.ClickException) as excinfo:
        executor.run("Resources | project id", ["sub-x"], None)

    exc = excinfo.value
    assert str(exc) == _FIXED_QUERY_MESSAGE
    assert _PLANTED_SUB not in str(exc)
    assert _PLANTED_PRINCIPAL not in str(exc)
    assert exc.__cause__ is None
