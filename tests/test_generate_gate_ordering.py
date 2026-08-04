"""Gate-before-generation ordering (DoS mitigation).

The ``generate`` command must evaluate the cheap emptiness / destructive-confirm
gates BEFORE it pays the (multi-GB at 500K resources) in-memory tenant + cost
materialization. A DECLINED truncate confirmation, or an ``--only-if-empty`` run
against a populated estate, must abort/skip WITHOUT ever calling
``generate_tenant()``.

These are DB-free ``CliRunner`` tests (mirroring
``tests/test_cli_generate_telemetry.py``): the whole writer seam is monkeypatched
and ``generate_tenant`` is spied on ``tenantless.generator.pipeline`` (cli.py
imports it at call time, so patching the module attribute before ``invoke()``
takes effect). The happy-path test is a fingerprint-order guard — it proves the
reorder changed only WHEN generation runs, never its output: the exact
``cost_records`` object that the real pipeline returned is the exact object handed
to ``write_tenant``.
"""

from __future__ import annotations

import contextlib

import pytest
from click.testing import CliRunner

from tenantless.cli import main
from tenantless.generator import writer as writer_mod


@pytest.fixture
def db_free_writer(monkeypatch):
    """Stub the whole Postgres writer seam so generate runs DB-free.

    Mirrors ``mocked_writer`` in test_cli_generate_telemetry, EXCEPT it does NOT
    force ``schema_is_empty`` / ``estate_is_empty`` — each test sets those to
    exercise a specific gate.
    """

    class _FakeConn:
        pass

    @contextlib.contextmanager
    def fake_open_writer(*a, **k):
        yield _FakeConn()

    monkeypatch.setattr(writer_mod, "open_writer", fake_open_writer)
    monkeypatch.setattr(writer_mod, "acquire_generate_lock", lambda conn, key: None)
    # the generate path now rides a SESSION advisory lock on a
    # dedicated autocommit connection — stub the new seam so these stay DB-free.
    monkeypatch.setattr(writer_mod, "open_lock_connection", fake_open_writer)
    monkeypatch.setattr(
        writer_mod, "acquire_generate_lock_session", lambda conn, key: None
    )
    monkeypatch.setattr(
        writer_mod, "release_generate_lock_session", lambda conn, key: None
    )
    monkeypatch.setattr(writer_mod, "truncate_synthetic", lambda conn: None)
    monkeypatch.setattr(writer_mod, "ensure_base_schema", lambda conn: True)
    monkeypatch.setattr(writer_mod, "ensure_cost_schema", lambda conn: True)
    monkeypatch.setattr(writer_mod, "ensure_identity_schema", lambda conn: True)
    monkeypatch.setattr(writer_mod, "ensure_web_metadata_schema", lambda conn: True)
    monkeypatch.setattr(writer_mod, "ensure_rg_index_schema", lambda conn: True)


def _spy_generate(monkeypatch):
    """Spy on the real generate_tenant: count calls, capture the returned result,
    and delegate to the real single-process pipeline (jobs=1) so a genuine
    GenerationResult (with real cost_records) flows to write_tenant."""
    from tenantless.generator import pipeline as pipeline_mod

    state: dict = {"calls": 0, "result": None}
    real = pipeline_mod.generate_tenant

    def spy(*a, **k):
        state["calls"] += 1
        res = real(*a, **{**k, "jobs": 1})
        state["result"] = res
        return res

    monkeypatch.setattr(pipeline_mod, "generate_tenant", spy)
    return state


def test_declined_confirmation_aborts_before_generation(db_free_writer, monkeypatch):
    """A non-empty schema + no --force + non-TTY raises UsageError (exit != 0) and
    generate_tenant is NEVER called — the destructive gate runs first.

    RED against the eager ordering (generate runs before the gate → 1 call)."""
    monkeypatch.setattr(writer_mod, "schema_is_empty", lambda conn: False)
    monkeypatch.setattr(writer_mod, "write_tenant", lambda *a, **k: None)
    spy = _spy_generate(monkeypatch)

    runner = CliRunner()
    # No --force; CliRunner stdin is non-TTY → the else-branch raises UsageError.
    result = runner.invoke(main, ["generate", "--profile", "small", "--seed", "7"])

    assert result.exit_code != 0, result.output + (result.stderr or "")
    assert "Refusing to truncate non-empty synthetic schema" in (
        result.output + (result.stderr or "")
    )
    assert spy["calls"] == 0, (
        "generate_tenant must NOT run when the truncate confirmation is declined "
        f"(got {spy['calls']} calls — expensive materialization on a rejected run)"
    )


def test_only_if_empty_skips_before_generation(db_free_writer, monkeypatch):
    """--only-if-empty against a populated estate skips (exit 0, skip line) and
    generate_tenant is NEVER called.

    RED against the eager ordering (generate runs before the estate gate)."""
    monkeypatch.setattr(writer_mod, "estate_is_empty", lambda conn: False)
    monkeypatch.setattr(writer_mod, "schema_is_empty", lambda conn: False)
    monkeypatch.setattr(writer_mod, "write_tenant", lambda *a, **k: None)
    spy = _spy_generate(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(
        main, ["generate", "--profile", "small", "--seed", "7", "--only-if-empty"]
    )

    assert result.exit_code == 0, result.output + (result.stderr or "")
    assert "estate already populated" in (result.stderr or "")
    assert spy["calls"] == 0, (
        "generate_tenant must NOT run when --only-if-empty skips a populated estate "
        f"(got {spy['calls']} calls)"
    )


def test_empty_schema_still_generates_and_writes(db_free_writer, monkeypatch):
    """Fingerprint-order guard: an empty schema + --force generates EXACTLY once and
    the cost_records handed to write_tenant is the SAME object the pipeline returned
    (same contents, same order). Proves the reorder changed only WHEN generation
    runs, never its output/order."""
    monkeypatch.setattr(writer_mod, "schema_is_empty", lambda conn: True)
    captured: dict = {}

    def fake_write_tenant(conn, tenant, **kwargs):
        captured["cost_records"] = kwargs.get("cost_records")

    monkeypatch.setattr(writer_mod, "write_tenant", fake_write_tenant)
    spy = _spy_generate(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(
        main, ["generate", "--profile", "small", "--seed", "7", "--force"]
    )

    assert result.exit_code == 0, result.output + (result.stderr or "")
    assert spy["calls"] == 1, f"expected exactly one generation, got {spy['calls']}"
    # Identity: write_tenant got the SAME cost_records object the pipeline returned —
    # not a re-copied/reordered clone. This is the fingerprint-order guarantee.
    assert "cost_records" in captured, "write_tenant was never called"
    assert captured["cost_records"] is spy["result"].cost_records, (
        "write_tenant received a different cost_records object than the pipeline "
        "returned — the reorder must not clone or reorder the cost payload"
    )
    # Value/order identity spelled out explicitly (belt-and-suspenders over `is`).
    assert list(captured["cost_records"]) == list(spy["result"].cost_records)


def test_progress_line_only_after_gate_pass(db_free_writer, monkeypatch):
    """The 'generating tenant...' progress line appears on the --force happy path but
    NOT on the declined-confirm path — generation never starts when the gate fails."""
    # Happy path: --force, empty schema → generation runs, progress line present.
    monkeypatch.setattr(writer_mod, "schema_is_empty", lambda conn: True)
    monkeypatch.setattr(writer_mod, "write_tenant", lambda *a, **k: None)
    _spy_generate(monkeypatch)
    runner = CliRunner()
    ok = runner.invoke(main, ["generate", "--profile", "small", "--seed", "7", "--force"])
    assert ok.exit_code == 0, ok.output + (ok.stderr or "")
    assert "generating tenant" in (ok.stderr or "").lower()

    # Declined path: non-empty schema, no --force, non-TTY → abort before generation,
    # so the "generating tenant..." line must NOT have been emitted.
    monkeypatch.setattr(writer_mod, "schema_is_empty", lambda conn: False)
    spy2 = _spy_generate(monkeypatch)
    declined = runner.invoke(main, ["generate", "--profile", "small", "--seed", "7"])
    assert declined.exit_code != 0
    combined = (declined.output + (declined.stderr or "")).lower()
    assert "generating tenant" not in combined, (
        "'generating tenant...' must not print when the confirmation is declined "
        "(generation never started)"
    )
    assert spy2["calls"] == 0
