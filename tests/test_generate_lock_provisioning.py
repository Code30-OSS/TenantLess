"""Session-lock / committed-provisioning / no-txn-during-CPU decoupling
 (lock-regression pin).

The earlier ordering fix wrapped the ENTIRE generation inside ONE ``open_writer``
transaction that also held the xact-scoped advisory lock and the ensure_* DDL
locks — an open write connection + DDL locks held across the whole CPU /
multiprocessing-fork phase. This restructure decouples them:

  1. a SESSION advisory lock on a dedicated idle (autocommit) connection spans
     the whole check→generate→write critical section;
  2. provisioning (ensure_base/identity/web_metadata) COMMITS in its own short
     transaction BEFORE generation, so no DDL table lock crosses the CPU phase;
  3. ``generate_tenant`` runs with NO open write transaction (the #3 regression pin);
  4. the write phase opens a FRESH transaction INSIDE the CostSpool block.

These are DB-free ``CliRunner`` tests: the whole writer seam is monkeypatched and
``generate_tenant`` is spied. An ordered event log records the enter/exit of every
``open_writer`` context, the session lock acquire/release, and the generate call, so
the assertions can prove the ordering and — critically — that NO write transaction is
active at the instant generation runs.
"""

from __future__ import annotations

import contextlib

import pytest
from click.testing import CliRunner

from tenantless.cli import main
from tenantless.generator import writer as writer_mod


@pytest.fixture
def event_log(monkeypatch):
    """Stub the writer seam DB-free and record an ordered event log.

    Events: ("open_writer", "enter"|"exit"), ("lock", "acquire"|"release"),
    ("gate", "schema_is_empty"), ("generate",), ("write_tenant",),
    ("ensure_cost", ...). Each open_writer context enter/exit is paired so the
    'active write transaction' depth can be computed at any point.
    """
    log: list[tuple] = []

    class _FakeConn:
        pass

    @contextlib.contextmanager
    def fake_open_writer(*a, **k):
        log.append(("open_writer", "enter"))
        try:
            yield _FakeConn()
        finally:
            log.append(("open_writer", "exit"))

    @contextlib.contextmanager
    def fake_open_lock_connection(*a, **k):
        log.append(("lock_conn", "enter"))
        try:
            yield _FakeConn()
        finally:
            log.append(("lock_conn", "exit"))

    monkeypatch.setattr(writer_mod, "open_writer", fake_open_writer)
    monkeypatch.setattr(writer_mod, "open_lock_connection", fake_open_lock_connection)
    monkeypatch.setattr(
        writer_mod, "acquire_generate_lock_session",
        lambda conn, key: log.append(("lock", "acquire")),
    )
    monkeypatch.setattr(
        writer_mod, "release_generate_lock_session",
        lambda conn, key: log.append(("lock", "release")),
    )
    # The old xact lock must NOT be used on the generate path anymore.
    monkeypatch.setattr(
        writer_mod, "acquire_generate_lock",
        lambda conn, key: log.append(("xact_lock", "acquire")),
    )
    monkeypatch.setattr(writer_mod, "ensure_base_schema", lambda conn: True)
    monkeypatch.setattr(
        writer_mod, "ensure_cost_schema",
        lambda conn: log.append(("ensure_cost",)) or True,
    )
    monkeypatch.setattr(writer_mod, "ensure_identity_schema", lambda conn: True)
    monkeypatch.setattr(writer_mod, "ensure_web_metadata_schema", lambda conn: True)
    monkeypatch.setattr(writer_mod, "ensure_rg_index_schema", lambda conn: True)
    monkeypatch.setattr(
        writer_mod, "schema_is_empty",
        lambda conn: log.append(("gate", "schema_is_empty")) or True,
    )
    monkeypatch.setattr(
        writer_mod, "truncate_synthetic",
        lambda conn: log.append(("truncate",)),
    )
    monkeypatch.setattr(
        writer_mod, "write_tenant",
        lambda *a, **k: log.append(("write_tenant",)),
    )
    return log


def _spy_generate(monkeypatch, log):
    from tenantless.generator import pipeline as pipeline_mod

    state = {"calls": 0}
    real = pipeline_mod.generate_tenant

    def spy(*a, **k):
        state["calls"] += 1
        log.append(("generate",))
        return real(*a, **{**k, "jobs": 1})

    monkeypatch.setattr(pipeline_mod, "generate_tenant", spy)
    return state


def _idx(log, event):
    return [i for i, e in enumerate(log) if e == event]


def test_provisioning_committed_and_no_write_txn_during_generate(event_log, monkeypatch):
    """The load-bearing #3 regression pin: provisioning is COMMITTED before generate,
    and NO write transaction is active at the instant generate_tenant runs."""
    spy = _spy_generate(monkeypatch, event_log)

    runner = CliRunner()
    result = runner.invoke(
        main, ["generate", "--profile", "small", "--seed", "7", "--force"]
    )
    assert result.exit_code == 0, result.output + (result.stderr or "")
    assert spy["calls"] == 1, f"expected exactly one generation, got {spy['calls']}"

    log = event_log
    gen_i = _idx(log, ("generate",))[0]

    # (a) A provisioning open_writer context ENTERED and EXITED before generate.
    #     Compute write-transaction depth just before the generate event.
    depth = 0
    saw_committed_prov = False
    for e in log[:gen_i]:
        if e == ("open_writer", "enter"):
            depth += 1
        elif e == ("open_writer", "exit"):
            depth -= 1
            if depth == 0:
                saw_committed_prov = True
    assert saw_committed_prov, (
        "no open_writer transaction committed (entered-and-exited) before generate — "
        "provisioning must commit before the CPU phase"
    )

    # (b) THE PIN: NO open_writer context is active (entered-not-exited) when generate
    #     runs. depth must be 0 at the generate event.
    assert depth == 0, (
        f"a write transaction was still open (depth={depth}) when generate_tenant ran "
        "— the #3 regression (DDL locks + open write conn across the CPU/fork phase)"
    )

    # (c) The SESSION lock is acquired FIRST (before provisioning/gate/generate) and
    #     released only AFTER the write — so it spans the whole check→generate→write
    #     critical section. (Under --force the gate short-circuits schema_is_empty, so
    #     the load-bearing span check is acquire-before-generate + release-after-write;
    #     the gate-before-generate ordering itself is pinned by
    #     tests/test_generate_gate_ordering.py.)
    assert _idx(log, ("lock", "acquire")), "session lock never acquired"
    acquire_i = _idx(log, ("lock", "acquire"))[0]
    write_i = _idx(log, ("write_tenant",))[0]
    first_prov_i = _idx(log, ("open_writer", "enter"))[0]
    assert acquire_i < first_prov_i, (
        "session lock must be taken before the first (provisioning) transaction"
    )
    assert acquire_i < gen_i, "session lock must be held before generation"
    release_i = _idx(log, ("lock", "release"))
    assert release_i and release_i[0] > write_i, (
        "session lock must be released after the write"
    )

    # (d) The deprecated xact lock is NOT used on the generate path.
    assert not _idx(log, ("xact_lock", "acquire")), (
        "generate must use the SESSION lock, not the xact-scoped advisory lock"
    )


def test_write_phase_runs_and_lock_wraps_it(event_log, monkeypatch):
    """The write (truncate + write_tenant) runs AFTER generate and is still inside the
    session-lock critical section (lock_conn not yet exited)."""
    _spy_generate(monkeypatch, event_log)
    runner = CliRunner()
    result = runner.invoke(
        main, ["generate", "--profile", "small", "--seed", "7", "--force"]
    )
    assert result.exit_code == 0, result.output + (result.stderr or "")
    log = event_log
    gen_i = _idx(log, ("generate",))[0]
    write_i = _idx(log, ("write_tenant",))[0]
    lock_exit_i = _idx(log, ("lock_conn", "exit"))[0]
    assert gen_i < write_i < lock_exit_i, (
        "write must run after generate and before the lock connection closes"
    )


def test_empty_cost_skips_ensure_cost_schema(event_log, monkeypatch):
    """The 'small' profile has no cost distributions -> empty spool -> ensure_cost_schema
    is NOT called (and no COPY), while the tenant is still written."""
    _spy_generate(monkeypatch, event_log)
    runner = CliRunner()
    result = runner.invoke(
        main, ["generate", "--profile", "small", "--seed", "7", "--force"]
    )
    assert result.exit_code == 0, result.output + (result.stderr or "")
    log = event_log
    assert _idx(log, ("write_tenant",)), "tenant must still be written"
    assert not _idx(log, ("ensure_cost",)), (
        "ensure_cost_schema must be skipped for an empty-cost (empty-spool) profile"
    )
