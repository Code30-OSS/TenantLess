"""``tenantless init-db`` — the provision-without-generating subcommand (260709-blf).

``init-db`` is a thin wrapper over the existing idempotent ``ensure_*`` seams: it
applies the full sql/001..007 chain (base -> cost -> identity -> drift ->
web_metadata) against ``DATABASE_URL`` WITHOUT generating any data — the serve-only
/ empty-DB path on a bare bring-your-own Postgres.

These tests are DB-FREE (mirror ``test_cli_generate_telemetry.py::mocked_writer``):
``open_writer`` and all five ``ensure_*`` seams are monkeypatched, so nothing ever
touches the shared :5433 dev tenant.
"""

from __future__ import annotations

import contextlib

from click.testing import CliRunner

from tenantless.cli import main
from tenantless.generator import writer as writer_mod


def test_init_db_in_top_level_help():
    """``tenantless --help`` lists the init-db subcommand."""
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0, result.output
    assert "init-db" in result.output


def test_init_db_help_documents_database_url():
    """``tenantless init-db --help`` documents the --database-url option."""
    runner = CliRunner()
    result = runner.invoke(main, ["init-db", "--help"])
    assert result.exit_code == 0, result.output
    assert "--database-url" in result.output


def test_init_db_applies_full_chain_in_order(monkeypatch):
    """A DB-free init-db invocation calls the five ensure_* seams IN ORDER:
    base -> cost -> identity -> drift -> web_metadata (sql/001..007)."""
    calls: list[str] = []

    class _FakeConn:
        pass

    @contextlib.contextmanager
    def fake_open_writer(*a, **k):
        yield _FakeConn()

    monkeypatch.setattr(writer_mod, "open_writer", fake_open_writer)
    monkeypatch.setattr(
        writer_mod, "ensure_base_schema", lambda conn: (calls.append("base"), True)[1]
    )
    monkeypatch.setattr(
        writer_mod, "ensure_cost_schema", lambda conn: (calls.append("cost"), True)[1]
    )
    monkeypatch.setattr(
        writer_mod,
        "ensure_identity_schema",
        lambda conn: (calls.append("identity"), True)[1],
    )
    monkeypatch.setattr(
        writer_mod, "ensure_drift_schema", lambda conn: (calls.append("drift"), True)[1]
    )
    monkeypatch.setattr(
        writer_mod,
        "ensure_web_metadata_schema",
        lambda conn: (calls.append("web_metadata"), True)[1],
    )

    runner = CliRunner()
    result = runner.invoke(main, ["init-db"])
    assert result.exit_code == 0, result.output + (result.stderr or "")
    assert calls == ["base", "cost", "identity", "drift", "web_metadata"], (
        f"init-db must apply 001..007 in order; got {calls}"
    )
