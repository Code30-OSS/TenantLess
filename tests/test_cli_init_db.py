"""``tenantless init-db`` — the provision-without-generating subcommand (260709-blf).

``init-db`` is a thin wrapper over the existing idempotent ``ensure_*`` seams: it
applies the full sql/001..008 chain (base -> cost -> identity -> drift ->
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
    """A DB-free init-db invocation calls the six ensure_* seams IN ORDER:
    base -> cost -> identity -> drift -> web_metadata -> rg_index (sql/001..008)."""
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
    monkeypatch.setattr(
        writer_mod,
        "ensure_rg_index_schema",
        lambda conn: (calls.append("rg_index"), True)[1],
    )

    runner = CliRunner()
    result = runner.invoke(main, ["init-db"])
    assert result.exit_code == 0, result.output + (result.stderr or "")
    assert calls == [
        "base",
        "cost",
        "identity",
        "drift",
        "web_metadata",
        "rg_index",
    ], f"init-db must apply 001..008 in order; got {calls}"


# --------------------------------------------------------------------------- #
# atomic init-db — pre-flight file gate + all-or-nothing apply
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def _spy_open_writer(spies):
    """A fake ``open_writer`` mirroring the real contract: commit on a clean exit,
    rollback + re-raise on any exception. Records commit/rollback counts."""

    class _FakeConn:
        def commit(self):
            spies["commit"] += 1

        def rollback(self):
            spies["rollback"] += 1

    conn = _FakeConn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def test_init_db_missing_file_never_opens_db(monkeypatch, tmp_path):
    """A missing bundled migration file aborts in the pre-flight gate — the missing
    filename is named and ``open_writer`` is NEVER entered (no DB touched)."""
    entered = {"opened": False}

    @contextlib.contextmanager
    def sentinel_open_writer(*a, **k):
        entered["opened"] = True
        yield object()

    monkeypatch.setattr(writer_mod, "open_writer", sentinel_open_writer)
    # Report the 008 migration ABSENT (a non-existent tmp path); the other seven real.
    real = writer_mod._all_migration_sql_files()
    monkeypatch.setattr(
        writer_mod,
        "_all_migration_sql_files",
        lambda: real[:7] + [tmp_path / "008_rg_lower_index.sql"],
    )

    runner = CliRunner()
    result = runner.invoke(main, ["init-db"])

    assert result.exit_code != 0, "a missing bundled file must exit nonzero"
    combined = (result.output or "") + (result.stderr or "")
    assert "008_rg_lower_index.sql" in combined, f"missing file not named: {combined!r}"
    assert entered["opened"] is False, (
        "open_writer must NOT be entered when a migration file is missing (DB untouched)"
    )


def test_init_db_rolls_back_on_apply_failure(monkeypatch):
    """A mid-apply twin failure raises INSIDE the writer context so open_writer rolls
    back — no commit, no false 'Provisioned' line, the failing migration named."""
    spies = {"commit": 0, "rollback": 0}
    monkeypatch.setattr(writer_mod, "open_writer", lambda *a, **k: _spy_open_writer(spies))
    # All migration files present -> the pre-flight gate passes (real paths).
    monkeypatch.setattr(writer_mod, "ensure_base_schema", lambda conn: True)
    monkeypatch.setattr(writer_mod, "ensure_cost_schema", lambda conn: False)  # 004 unavailable
    monkeypatch.setattr(writer_mod, "ensure_identity_schema", lambda conn: True)
    monkeypatch.setattr(writer_mod, "ensure_drift_schema", lambda conn: True)
    monkeypatch.setattr(writer_mod, "ensure_web_metadata_schema", lambda conn: True)
    monkeypatch.setattr(writer_mod, "ensure_rg_index_schema", lambda conn: True)

    runner = CliRunner()
    result = runner.invoke(main, ["init-db"])

    assert result.exit_code != 0, "a mid-apply failure must exit nonzero"
    combined = (result.output or "") + (result.stderr or "")
    assert "004" in combined, f"the failing migration must be named: {combined!r}"
    assert spies["rollback"] == 1, "a mid-apply failure must roll back"
    assert spies["commit"] == 0, "no commit may happen on a mid-apply failure"
    assert "Provisioned schema 001..008" not in combined, (
        "no false-success line on a rolled-back apply"
    )


def test_init_db_all_present_commits(monkeypatch):
    """All files present and every ensure_* True -> exit 0, commit, host-only line."""
    spies = {"commit": 0, "rollback": 0}
    monkeypatch.setattr(writer_mod, "open_writer", lambda *a, **k: _spy_open_writer(spies))
    for fn in (
        "ensure_base_schema",
        "ensure_cost_schema",
        "ensure_identity_schema",
        "ensure_drift_schema",
        "ensure_web_metadata_schema",
        "ensure_rg_index_schema",
    ):
        monkeypatch.setattr(writer_mod, fn, lambda conn: True)

    runner = CliRunner()
    result = runner.invoke(
        main, ["init-db", "--database-url", "postgres://u:secretpw@example-host:5433/db"]
    )

    assert result.exit_code == 0, result.output + (result.stderr or "")
    assert spies["commit"] == 1, "the all-present path must commit exactly once"
    assert spies["rollback"] == 0, "no rollback on the success path"
    out = result.output or ""
    assert "example-host" in out, "host-only status line must name the host"
    assert "secretpw" not in out, "password must never be echoed (T-07-02)"
    assert "Provisioned schema 001..008" in out
