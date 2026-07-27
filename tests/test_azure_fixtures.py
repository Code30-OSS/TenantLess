"""Wave-0 fixture-load + import-isolation tests for the ARG scan path.

Proves the hand-authored ARG-shape JSON parses with the expected ObjectArray
keys, that the shared ``_FakeExecutor`` / planted-identifier material loads and
behaves (pop-based single page → ``QueryPage``), and — critically — that
importing the seam and the fixtures pulls NO ``azure-*`` into ``sys.modules``
(the core-install isolation invariant the whole phase rests on, D-08).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# tests/fixtures is put on sys.path by conftest.py (sys.path.insert at import),
# so the `fixtures.*` package import mirrors how Waves 1-2 consume these.
from fixtures.azure_rows import (  # noqa: E402
    PLANTED_NAME,
    PLANTED_RG,
    PLANTED_SUB,
    PLANTED_TAGVAL,
    _FakeExecutor,
    planted_identifiers,
    synthetic_arg_rows,
)

from tenantless.analyzer.azure.executor import QueryPage  # noqa: E402

_ARG_SHAPE = Path(__file__).resolve().parent / "fixtures" / "arg_shape.json"


def test_arg_shape_json_parses_with_objectarray_keys():
    """arg_shape.json loads and exposes the ARG ObjectArray response keys."""
    with _ARG_SHAPE.open(encoding="utf-8") as fh:
        doc = json.load(fh)
    assert isinstance(doc["data"], list) and doc["data"], "data must be a non-empty list"
    # The four paging/shape signals the parse contract pins.
    for key in ("skipToken", "resultTruncated", "totalRecords", "count"):
        assert key in doc, f"missing ARG response key: {key}"
    # The single synthetic resource carries the camelCase ARG fields.
    row = doc["data"][0]
    for field in (
        "id",
        "name",
        "type",
        "location",
        "resourceGroup",
        "subscriptionId",
        "tags",
        "properties",
        "sku",
        "kind",
    ):
        assert field in row, f"resource dict missing ARG field: {field}"


def test_planted_identifiers_are_the_four_synthetic_strings():
    """planted_identifiers() returns exactly the four planted fakes."""
    assert planted_identifiers() == [
        PLANTED_SUB,
        PLANTED_RG,
        PLANTED_NAME,
        PLANTED_TAGVAL,
    ]


def test_synthetic_arg_rows_carry_planted_identifiers():
    """The row builder produces n rows carrying every planted identifier."""
    rows = synthetic_arg_rows(8)
    assert len(rows) == 8
    r = rows[0]
    assert r["subscriptionId"] == PLANTED_SUB
    assert r["resourceGroup"] == PLANTED_RG
    assert r["name"] == PLANTED_NAME
    assert r["tags"]["Owner"] == PLANTED_TAGVAL
    assert r["tags"]["Environment"] == "prod"


def test_fake_executor_pops_one_page_and_returns_querypage():
    """A one-page _FakeExecutor returns QueryPage(rows, None) on a single run."""
    rows = synthetic_arg_rows(3)
    executor = _FakeExecutor([(rows, None)])
    page = executor.run("q", None, None)
    assert isinstance(page, QueryPage)
    assert page.rows == rows
    assert page.skip_token is None


def test_importing_azure_rows_pulls_no_azure_module():
    """Importing the fixtures (and the seam) adds no azure* to sys.modules.

    In-process check: meaningful ONLY on the core install. When the optional
    ``azure`` extra is installed (azure-leg dev runs), another test's
    ``importorskip`` may have already loaded ``azure`` into this process, so the
    sys.modules check cannot attribute the import — skip rather than false-fail.
    The subprocess test in ``test_azure_import_isolation.py`` guarantees isolation
    regardless of venv state."""
    import importlib.util

    if importlib.util.find_spec("azure") is not None:
        import pytest

        pytest.skip("azure extra installed; subprocess test covers isolation")
    leaked = [m for m in sys.modules if m == "azure" or m.startswith("azure.")]
    assert not leaked, f"azure-* leaked into sys.modules: {leaked}"
