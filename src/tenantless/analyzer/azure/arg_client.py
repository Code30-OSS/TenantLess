"""The ONLY module in the codebase that imports ``azure-*`` (D-06/D-07).

It provides :func:`make_arg_executor` — a :class:`QueryExecutor` that wraps
``ResourceGraphClient.resources()`` with the SDK-default ARM scope,
``ResultFormat.OBJECT_ARRAY``, ``top=1000`` and ``$skipToken`` paging — and
converts azure failures into a FIXED, identifier-free ``click.ClickException``.
Both the aggregated credential-chain message (auth path) AND non-auth ARG
errors (403 permission / throttling / invalid query, which embed the in-scope
``subscriptionId`` + caller principal) are leak surfaces, so the underlying
message is dropped via ``raise ... from None`` — two fixed messages, one for
the auth path and one for the generic-query path (WR-01).

Every ``azure.*`` import is function-LOCAL to :func:`make_arg_executor`, so this
module parses and imports cleanly on the bare core install (no ``azure-*``
present) and pulls no ``azure`` key into ``sys.modules`` at import time.

:func:`resolve_subscriptions` is DELIBERATELY azure-free: it decides the default
no-arg scan scope by enumerating accessible subscriptions through the SAME
injectable :class:`QueryExecutor` seam (a static ``ResourceContainers`` ARG
query), so it needs no ``azure-mgmt-resource``/``SubscriptionClient`` and is
fully exercisable under the core install with the fake executor.
"""

from __future__ import annotations

import click

from tenantless.analyzer.azure.executor import QueryExecutor, QueryPage

# Fixed, identifier-free operator messages. NO ``%s``/format placeholder, NO
# reference to ``exc``/``.message``/any enumerated id — both error paths are
# themselves leak surfaces (D-07).
_AUTH_ERROR_MESSAGE = (
    "Azure authentication failed. Run `az login` or set service-principal "
    "env vars. Use AZURE_TOKEN_CREDENTIALS to restrict the credential chain."
)
# Non-auth ARG failures (403 permission, throttling, invalid scope/query)
# routinely embed the in-scope subscriptionId + caller principal id in their
# message; that text would print to operator stderr (WR-01). Suppress it the
# same way as the auth path — a FIXED, identifier-free message, ``from None``.
_QUERY_ERROR_MESSAGE = (
    "Azure Resource Graph query failed (permission denied, throttling, or an "
    "invalid scope/query). The underlying error detail was suppressed because "
    "it can contain tenant identifiers (subscription id / caller principal)."
)
_EMPTY_ENUM_MESSAGE = (
    "No accessible Azure subscriptions were found. Check the credential's "
    "permissions, or pass azure:<subId,...> to scope the scan explicitly."
)

# Static enumeration query (A2 RESOLVED). The subscription filter is NEVER
# spliced into KQL; subscription ids pass only as ``QueryRequest.subscriptions``.
SUBSCRIPTION_ENUM_QUERY = (
    "ResourceContainers "
    "| where type =~ 'microsoft.resources/subscriptions' "
    "| project subscriptionId "
    "| order by subscriptionId asc"
)


class _ArgExecutor:
    """Real :class:`QueryExecutor`: pages one ARG query via ``ResourceGraphClient``.

    Scope-agnostic — the subscription list arrives per call as the ``run``
    ``subscriptions`` argument (``open_azure`` passes the resolved list), so the
    "need an executor to enumerate, need subs to build the executor" cycle never
    arises. The azure model/exception classes are injected by
    :func:`make_arg_executor` so ``run`` itself contains no ``azure`` import.
    """

    def __init__(
        self,
        *,
        client,
        query_request,
        query_options,
        result_format,
        auth_errors,
        azure_errors,
    ):
        self._client = client
        self._query_request = query_request
        self._query_options = query_options
        self._result_format = result_format
        self._auth_errors = auth_errors
        self._azure_errors = azure_errors

    def run(
        self,
        query: str,
        subscriptions: list[str] | None,
        skip_token: str | None,
    ) -> QueryPage:
        options = self._query_options(
            result_format=self._result_format.OBJECT_ARRAY,
            top=1000,
            skip_token=skip_token,
        )
        request = self._query_request(
            subscriptions=subscriptions,
            query=query,
            options=options,
        )
        try:
            resp = self._client.resources(request)
        except self._auth_errors:
            # Drop the chained azure message (can name tenant/account) — D-07.
            raise click.ClickException(_AUTH_ERROR_MESSAGE) from None
        except self._azure_errors:
            # Non-auth ARG failures (403/throttle/invalid query) embed the
            # in-scope subscriptionId + caller principal — suppress (D-07/WR-01).
            raise click.ClickException(_QUERY_ERROR_MESSAGE) from None
        return QueryPage(rows=list(resp.data or []), skip_token=resp.skip_token)


def make_arg_executor() -> QueryExecutor:
    """Build the real ARG paging executor (the guarded azure boundary).

    Takes NO subscription argument — the scope arrives per call via ``run``.
    Constructs ``ResourceGraphClient(DefaultAzureCredential())`` with the
    SDK-default ARM scope only (no extra ``credential_scopes``;
    ``AZURE_TOKEN_CREDENTIALS`` is honored by DAC automatically). Never enables
    ``logging_enable``. Raises a friendly ``RuntimeError`` if the optional
    ``azure`` extra is not installed.
    """
    try:
        from azure.core.exceptions import (
            AzureError,
            ClientAuthenticationError,
        )
        from azure.identity import (
            CredentialUnavailableError,
            DefaultAzureCredential,
        )
        from azure.mgmt.resourcegraph import ResourceGraphClient
        from azure.mgmt.resourcegraph.models import (
            QueryRequest,
            QueryRequestOptions,
            ResultFormat,
        )
    except ImportError as exc:
        raise RuntimeError(
            "the 'azure' extra is required for --source azure: "
            "install with `uv sync --extra azure`"
        ) from exc

    client = ResourceGraphClient(DefaultAzureCredential())
    return _ArgExecutor(
        client=client,
        query_request=QueryRequest,
        query_options=QueryRequestOptions,
        result_format=ResultFormat,
        auth_errors=(ClientAuthenticationError, CredentialUnavailableError),
        azure_errors=AzureError,
    )


def resolve_subscriptions(
    subscription_filter: list[str] | None,
    executor: QueryExecutor,
) -> list[str]:
    """Decide the subscription scope for the resource scan (A2 RESOLVED).

    An explicit ``azure:<subId,...>`` filter is honored verbatim (the escape
    hatch — no enumeration round-trip). Otherwise the default no-arg scope is
    DECIDED by paging the static :data:`SUBSCRIPTION_ENUM_QUERY` through the
    SAME injectable ``executor`` to exhaustion and returning the sorted distinct
    subscription ids. An empty enumeration surfaces a FIXED identifier-free
    error (no tenant id / account / credential diagnostic). This function
    imports nothing from ``azure``.
    """
    if subscription_filter:
        return list(subscription_filter)

    ids: set[str] = set()
    skip_token: str | None = None
    seen_tokens: set[str] = set()  # P2-b: non-progress / cycle guard
    while True:
        page = executor.run(SUBSCRIPTION_ENUM_QUERY, None, skip_token)
        for row in page.rows:
            sub_id = row.get("subscriptionId")
            if sub_id:
                ids.add(sub_id)
        next_token = page.skip_token
        if next_token is None:
            break
        # P2-b: a repeated continuation token means no forward progress -- abort
        # rather than enumerate forever. Identifier-free message (D-07).
        if next_token in seen_tokens:
            raise click.ClickException(
                "Azure Resource Graph subscription enumeration did not progress "
                "(a continuation token repeated); aborting to avoid a loop."
            )
        seen_tokens.add(next_token)
        skip_token = next_token

    if not ids:
        raise click.ClickException(_EMPTY_ENUM_MESSAGE)
    return sorted(ids)
