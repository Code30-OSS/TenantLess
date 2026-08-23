"""``generate --only-if-empty`` populated-skip must STILL build the additive
ARM-ID fold indexes (INV-01, D-28, post-review FIX 2).

The additive index builder ``build_arm_id_key_indexes_concurrently`` originally sat
INSIDE the ``if not skipped:`` block, so a populated estate hitting the
``--only-if-empty`` skip NEVER built the indexes — yet that skip IS the normal
upgrade path for an existing demo volume (the sql/011 fold functions are already
provisioned BEFORE the skip decision, so the volume is ready for the indexes). The
builder must therefore run on BOTH paths (generation AND populated-skip), exactly
once, after the fold functions are provisioned. ``CONCURRENTLY IF NOT EXISTS`` is
idempotent, so running it on the skip path is safe.

DB-free ``CliRunner`` tests (mirroring ``tests/test_generate_gate_ordering.py``):
the whole writer seam is stubbed and ``build_arm_id_key_indexes_concurrently`` is a
call-counter, so the assertion is purely about WHICH paths invoke the builder.
"""

from __future__ import annotations

import contextlib

import pytest
from click.testing import CliRunner

from tenantless.cli import main
from tenantless.generator import writer as writer_mod


@pytest.fixture
def db_free_writer(monkeypatch):
    """Stub the whole Postgres writer seam DB-free and COUNT index-builder calls.

    Returns the shared counter dict (``{"build": N}``) so each test can assert how
    many times the additive index builder ran. Does NOT force ``schema_is_empty`` /
    ``estate_is_empty`` — each test sets those to exercise a specific path.
    """
    counter = {"build": 0}

    class _FakeConn:
        pass

    @contextlib.contextmanager
    def fake_open_writer(*a, **k):
        yield _FakeConn()

    monkeypatch.setattr(writer_mod, "open_writer", fake_open_writer)
    monkeypatch.setattr(writer_mod, "open_lock_connection", fake_open_writer)
    monkeypatch.setattr(writer_mod, "acquire_generate_lock", lambda conn, key: None)
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
    monkeypatch.setattr(writer_mod, "ensure_arm_id_key_schema", lambda conn: True)
    monkeypatch.setattr(writer_mod, "audit_arm_id_identity", lambda conn: None)

    def _count_build(*a, **k):
        counter["build"] += 1
        return True

    monkeypatch.setattr(
        writer_mod, "build_arm_id_key_indexes_concurrently", _count_build
    )
    return counter


def _spy_generate(monkeypatch):
    from tenantless.generator import pipeline as pipeline_mod

    state: dict = {"calls": 0}
    real = pipeline_mod.generate_tenant

    def spy(*a, **k):
        state["calls"] += 1
        return real(*a, **{**k, "jobs": 1})

    monkeypatch.setattr(pipeline_mod, "generate_tenant", spy)
    return state


def test_only_if_empty_skip_still_builds_indexes(db_free_writer, monkeypatch):
    """A populated ``--only-if-empty`` skip must STILL build the additive indexes
    exactly once, even though generation is skipped.

    RED against the pre-fix placement (builder inside ``if not skipped:`` → 0 builds
    on the skip path)."""
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
    # Skip => no generation ...
    assert spy["calls"] == 0, f"generation must be skipped (got {spy['calls']} calls)"
    # ... but the additive indexes ARE still built, exactly once (the upgrade path).
    assert db_free_writer["build"] == 1, (
        "the --only-if-empty populated skip must STILL build the additive indexes "
        f"exactly once (got {db_free_writer['build']}) — it is the normal upgrade "
        "path for an existing demo volume"
    )


def test_generate_write_path_builds_indexes_exactly_once(db_free_writer, monkeypatch):
    """The normal (non-skip) generate+write path builds the additive indexes exactly
    once — moving the builder out of ``if not skipped:`` must not double-build."""
    monkeypatch.setattr(writer_mod, "schema_is_empty", lambda conn: True)
    monkeypatch.setattr(writer_mod, "write_tenant", lambda *a, **k: None)
    spy = _spy_generate(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(
        main, ["generate", "--profile", "small", "--seed", "7", "--force"]
    )

    assert result.exit_code == 0, result.output + (result.stderr or "")
    assert spy["calls"] == 1, f"expected exactly one generation, got {spy['calls']}"
    assert db_free_writer["build"] == 1, (
        f"the write path must build the additive indexes exactly once (got "
        f"{db_free_writer['build']} — a duplicate build indicates the move left the "
        "old call in place)"
    )
