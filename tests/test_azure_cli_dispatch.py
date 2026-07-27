"""SCAN-01 CLI dispatch test for the ``azure:`` scheme (core install).

``analyze --source azure:`` must route to the azure branch of ``build_profile``.
On the CORE install (no ``azure`` extra) the guarded ``make_arg_executor`` boundary
raises a friendly, identifier-free message naming the extra -- proving the scheme
dispatches correctly AND that the dispatch-failure surface carries NO tenant
identifier (D-07). No live tenant and no ``azure-*`` required.
"""

from __future__ import annotations

from click.testing import CliRunner

from tenantless.cli import main


def test_analyze_help_mentions_azure_scheme():
    """``analyze --help`` documents the azure: source scheme."""
    result = CliRunner().invoke(main, ["analyze", "--help"])
    assert result.exit_code == 0
    assert "azure:" in result.output


def test_azure_source_dispatches_and_fails_friendly_without_extra(
    tmp_path, monkeypatch
):
    """`--source azure:` routes to the azure branch; absent the extra it fails
    with a friendly message naming `uv sync --extra azure` and no identifier.

    P1-a: ``make_arg_executor`` is monkeypatched to raise the extra-absent error
    so this test is HERMETIC — it can never construct a real ``ResourceGraphClient``
    /``DefaultAzureCredential`` and therefore never performs a live ARG scan, even
    when the ``azure`` extra and active credentials happen to be installed."""

    def _no_extra():
        raise RuntimeError(
            "the 'azure' extra is required for --source azure: "
            "install with `uv sync --extra azure`"
        )

    monkeypatch.setattr(
        "tenantless.analyzer.azure.arg_client.make_arg_executor", _no_extra
    )

    out = tmp_path / "derived.json"
    result = CliRunner().invoke(
        main, ["analyze", "--source", "azure:", "--out", str(out)]
    )

    # Routed to the azure branch and failed (the guarded import boundary).
    assert result.exit_code != 0
    assert result.exception is not None

    message = str(result.exception)
    # Names the optional extra + the exact install command.
    assert "azure" in message
    assert "uv sync --extra azure" in message

    # The dispatch-failure surface carries NO tenant identifier (D-07).
    for ident in ("subscription", "tenant", "/subscriptions/"):
        assert ident not in message.lower()

    # Nothing was written when dispatch failed.
    assert not out.exists()
