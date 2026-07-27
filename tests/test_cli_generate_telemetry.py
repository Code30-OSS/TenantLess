"""generate telemetry split: progress -> stderr, summary -> stdout (PLAT-06).

D-18: plain stderr progress lines (fitting / generating / tag-entropy) plus a
human-readable run summary on stdout. D-19: NO drift line anywhere.

These tests drive the ``generate`` Click command with ``CliRunner`` and a
fully-mocked writer (no live Postgres) — mirroring the mocked-seam idiom in
``tests/test_serve.py``. The DB layer (``writer.open_writer`` /
``schema_is_empty`` / ``truncate_synthetic`` / ``write_tenant``) is monkeypatched
so the command runs end-to-end against the real generator pipeline but never
touches a database. Click 8.2+ captures stderr separately by default, so
``result.stdout`` and ``result.stderr`` are already two distinct streams.
"""

from __future__ import annotations

import contextlib
import re

import pytest
from click.testing import CliRunner

from tenantless.cli import main
from tenantless.generator import writer as writer_mod


@pytest.fixture
def mocked_writer(monkeypatch):
    """Stub out the Postgres writer so generate runs DB-free."""

    class _FakeConn:
        pass

    @contextlib.contextmanager
    def fake_open_writer(*a, **k):
        yield _FakeConn()

    monkeypatch.setattr(writer_mod, "open_writer", fake_open_writer)
    monkeypatch.setattr(writer_mod, "schema_is_empty", lambda conn: True)
    monkeypatch.setattr(writer_mod, "truncate_synthetic", lambda conn: None)
    monkeypatch.setattr(writer_mod, "write_tenant", lambda *a, **k: None)
    # Identity is on by default (Phase 10), so generate calls ensure_identity_schema
    # before writing — stub the idempotent-migration seam too (it would otherwise
    # call conn.execute on the DB-free _FakeConn). ensure_cost_schema is stubbed for
    # symmetry so a future cost-bearing profile keeps this fixture DB-free.
    # Plan 260709-blf: generate now calls ensure_base_schema FIRST (self-provisions
    # sql/001..003 on a bare non-Docker PG16). Stub it so the DB-free _FakeConn never
    # reaches the real to_regclass guard / conn.execute.
    monkeypatch.setattr(writer_mod, "ensure_base_schema", lambda conn: True)
    monkeypatch.setattr(writer_mod, "ensure_cost_schema", lambda conn: True)
    monkeypatch.setattr(writer_mod, "ensure_identity_schema", lambda conn: True)
    # Plan 14-05 (D-14): generate now calls ensure_web_metadata_schema unconditionally
    # (the profile_name column preflight) before write — stub the idempotent-migration
    # seam so it stays DB-free on the _FakeConn (mirrors the identity/cost stubs above).
    monkeypatch.setattr(writer_mod, "ensure_web_metadata_schema", lambda conn: True)


def _run_generate(extra=None):
    # Click 8.2+ captures stderr separately by default (result.stderr distinct).
    runner = CliRunner()
    args = ["generate", "--profile", "small", "--seed", "7", "--force"]
    if extra:
        args += extra
    return runner.invoke(main, args)


def test_progress_lines_go_to_stderr(mocked_writer):
    """fitting / generating / tag-entropy progress lines appear on STDERR."""
    result = _run_generate()
    assert result.exit_code == 0, result.output + (result.stderr or "")
    err = result.stderr.lower()
    assert "fitting" in err
    assert "generating" in err
    assert "tag entropy" in err or "tag-entropy" in err


def test_summary_reports_rg_naming_metrics(mocked_writer):
    """ARCH-03 / D-18 (Plan 19-05): a generate run prints the confirm-and-rename
    gate's outcome counts on STDOUT, alongside (not replacing) the D-13 archetype
    coverage line. Pins the cli.py wiring — the renderer's own contract is covered
    by tests/test_generator_naming.py."""
    result = _run_generate()
    assert result.exit_code == 0, result.output + (result.stderr or "")
    out = result.stdout
    # The pre-existing D-13 coverage line survives (never replaced).
    assert "archetypes: " in out
    # The new D-18 gap-metrics line carries all three counts as integers.
    assert "rg-naming: " in out
    match = re.search(
        r"rg-naming: confirmed=(\d+) downgraded_to_generic=(\d+) "
        r"child_credit_confirmed=(\d+)",
        out,
    )
    assert match is not None, f"D-18 metrics line missing/malformed:\n{out}"


def test_summary_fields_go_to_stdout(mocked_writer):
    """The structured run summary (counts/seed/timing/tenant_id) is on STDOUT."""
    result = _run_generate()
    assert result.exit_code == 0, result.output + (result.stderr or "")
    out = result.stdout.lower()
    # Summary fields (D-18): tenant_id, the four counts, seed, and elapsed.
    assert "tenant" in out
    assert "subscription" in out
    assert "resource group" in out or "resource_group" in out
    assert "resource" in out
    assert "violation" in out
    assert "dependenc" in out
    assert "seed" in out
    assert "elapsed" in out or "elapsed_ms" in out or "ms" in out


def test_no_drift_line_anywhere(mocked_writer):
    """D-19: no 'drift' token on EITHER stream (drift lands in Phase 11)."""
    result = _run_generate()
    assert result.exit_code == 0, result.output + (result.stderr or "")
    assert "drift" not in result.stdout.lower()
    assert "drift" not in (result.stderr or "").lower()


def test_progress_not_on_stdout(mocked_writer):
    """The progress stages must NOT bleed onto stdout (clean machine-readable split)."""
    result = _run_generate()
    assert result.exit_code == 0, result.output + (result.stderr or "")
    out = result.stdout.lower()
    assert "fitting" not in out
    assert "generating tenant" not in out


def test_no_identity_still_provisions_identity_schema(monkeypatch):
    """P2 regression (Plan 10-01): a ``--no-identity`` generate emits ZERO identity
    rows, but ``ensure_identity_schema`` must STILL run so ``synthetic.principals`` /
    ``synthetic.role_assignments`` exist — otherwise the mock-server roleAssignments
    SELECT 500s on a missing relation instead of serving an empty list. The migration
    is now called UNCONDITIONALLY, regardless of identity row count.

    Fails against the old guarded ``if result.principals or result.role_assignments:``
    call (no identity rows → skipped → recorded zero calls); passes after the guard
    is removed.
    """
    calls: list[str] = []

    class _FakeConn:
        pass

    @contextlib.contextmanager
    def fake_open_writer(*a, **k):
        yield _FakeConn()

    monkeypatch.setattr(writer_mod, "open_writer", fake_open_writer)
    monkeypatch.setattr(writer_mod, "schema_is_empty", lambda conn: True)
    monkeypatch.setattr(writer_mod, "truncate_synthetic", lambda conn: None)
    monkeypatch.setattr(writer_mod, "write_tenant", lambda *a, **k: None)
    monkeypatch.setattr(writer_mod, "ensure_base_schema", lambda conn: True)
    monkeypatch.setattr(writer_mod, "ensure_cost_schema", lambda conn: True)
    monkeypatch.setattr(writer_mod, "ensure_web_metadata_schema", lambda conn: True)
    monkeypatch.setattr(
        writer_mod,
        "ensure_identity_schema",
        lambda conn: (calls.append("identity"), True)[1],
    )

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["generate", "--profile", "small", "--seed", "7", "--force", "--no-identity"],
    )
    assert result.exit_code == 0, result.output + (result.stderr or "")
    assert calls == ["identity"], (
        "ensure_identity_schema must run UNCONDITIONALLY even with --no-identity "
        "(zero identity rows) so the empty tables exist"
    )


def test_generate_ensures_base_schema_first(monkeypatch):
    """Plan 260709-blf: generate must call ensure_base_schema BEFORE the
    cost/identity/web_metadata twins — the base tables (sql/001..003) have to exist
    on a bare non-Docker PG16 before any later ensure_* / write runs. Records the
    ensure_* order into a calls[] list and asserts 'base' is first."""
    calls: list[str] = []

    class _FakeConn:
        pass

    @contextlib.contextmanager
    def fake_open_writer(*a, **k):
        yield _FakeConn()

    monkeypatch.setattr(writer_mod, "open_writer", fake_open_writer)
    monkeypatch.setattr(writer_mod, "schema_is_empty", lambda conn: True)
    monkeypatch.setattr(writer_mod, "truncate_synthetic", lambda conn: None)
    monkeypatch.setattr(writer_mod, "write_tenant", lambda *a, **k: None)
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
        writer_mod,
        "ensure_web_metadata_schema",
        lambda conn: (calls.append("web_metadata"), True)[1],
    )

    result = _run_generate()
    assert result.exit_code == 0, result.output + (result.stderr or "")
    assert calls, "no ensure_* seam was called"
    assert calls[0] == "base", (
        f"ensure_base_schema must run first (before cost/identity/web_metadata); "
        f"got order {calls}"
    )


def test_cost_granularity_flag_in_help():
    """generate --help documents --cost-granularity with the monthly|daily choices
    and the daily short-window note (COST-01 / D-09)."""
    runner = CliRunner()
    result = runner.invoke(main, ["generate", "--help"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "--cost-granularity" in out
    low = out.lower()
    assert "monthly" in low and "daily" in low
    # The daily short-window safeguard must be documented (Pitfall 6).
    assert "short window" in low or "current month" in low


def test_cost_granularity_rejects_unknown_choice(mocked_writer):
    """An out-of-set --cost-granularity value is rejected by click.Choice."""
    result = _run_generate(extra=["--cost-granularity", "hourly"])
    assert result.exit_code != 0
    assert "hourly" in result.output or "Invalid value" in result.output


def test_jobs_flag_in_help():
    """generate --help documents --jobs (default 1, 0 = all cores) — SPEED-01."""
    runner = CliRunner()
    result = runner.invoke(main, ["generate", "--help"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "--jobs" in out
    low = out.lower()
    assert "default: 1" in low
    assert "all cores" in low or "cpu_count" in low


def test_jobs_rejects_negative():
    """IntRange(0, None) rejects a negative --jobs at CLI validation (no -1 sentinel)."""
    runner = CliRunner()
    result = runner.invoke(
        main, ["generate", "--profile", "small", "--seed", "7", "--force", "--jobs", "-1"]
    )
    assert result.exit_code != 0
    assert "Invalid value" in result.output or "-1" in result.output


def test_summary_reports_jobs(mocked_writer):
    """The run summary appends the effective job count (operator visibility)."""
    result = _run_generate(extra=["--jobs", "1"])
    assert result.exit_code == 0, result.output + (result.stderr or "")
    assert "jobs=1" in result.stdout.lower()


def _spy_jobs(monkeypatch):
    """Capture the jobs kwarg threaded into generate_tenant, forcing the real
    generation to run single-process (jobs=1) so the test never spawns a pool."""
    from tenantless.generator import pipeline as pipeline_mod

    captured: dict[str, int] = {}
    real = pipeline_mod.generate_tenant

    def spy(*a, **k):
        captured["jobs"] = k.get("jobs")
        return real(*a, **{**k, "jobs": 1})

    monkeypatch.setattr(pipeline_mod, "generate_tenant", spy)
    return captured


def test_jobs_zero_resolves_to_all_cores(mocked_writer, monkeypatch):
    """--jobs 0 resolves to os.cpu_count() (the all-cores sentinel) before pipeline."""
    import os

    captured = _spy_jobs(monkeypatch)
    result = _run_generate(extra=["--jobs", "0"])
    assert result.exit_code == 0, result.output + (result.stderr or "")
    assert captured["jobs"] == (os.cpu_count() or 1)


def test_jobs_clamped_to_cpu_count(mocked_writer, monkeypatch):
    """A huge --jobs is clamped to os.cpu_count() (Security V5: never an unbounded pool)."""
    import os

    captured = _spy_jobs(monkeypatch)
    result = _run_generate(extra=["--jobs", "100000"])
    assert result.exit_code == 0, result.output + (result.stderr or "")
    assert captured["jobs"] == (os.cpu_count() or 1)


def test_bare_generate_threads_jobs_one(mocked_writer, monkeypatch):
    """A bare generate (no --jobs) threads jobs=1 — the single-process reference path."""
    captured = _spy_jobs(monkeypatch)
    result = _run_generate()
    assert result.exit_code == 0, result.output + (result.stderr or "")
    assert captured["jobs"] == 1
