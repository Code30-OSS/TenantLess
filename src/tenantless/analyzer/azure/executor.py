"""The import-isolated network seam for the ARG ingestion path (D-08).

This module defines ONLY the contract every analyzer module implements against:
a frozen :class:`QueryPage` carrying one ARG response page and the
:class:`QueryExecutor` ``Protocol`` the paging loop depends on. It imports
NOTHING from ``azure-*`` so the materializer and every leak test run under the
core CI install with zero Azure dependencies present. The real implementation
(``arg_client.make_arg_executor``) is the only place that wraps
``ResourceGraphClient`` behind the guarded ``azure`` optional extra.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class QueryPage:
    """One ARG ObjectArray response page.

    ``rows`` is the response ``data`` — a list of plain dicts (no Azure types).
    ``skip_token`` is the response continuation token; ``None`` when exhausted.
    """

    rows: list[dict]
    skip_token: str | None


class QueryExecutor(Protocol):
    """The injectable network seam: run one KQL query page.

    The real implementation wraps ``ResourceGraphClient.resources()``; tests
    inject a fake that pops hand-authored synthetic pages, so the paging /
    materialize / denylist-derive logic is exercised with no network.
    """

    def run(
        self,
        query: str,
        subscriptions: list[str] | None,
        skip_token: str | None,
    ) -> QueryPage: ...
