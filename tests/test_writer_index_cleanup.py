"""The COPY index-drop helper must not let index-restore failure mask the COPY
error (13 post-review fix #2).

When the bulk COPY raises, the connection is in an aborted transaction, so the
``finally``-block CREATE INDEX statements raise InFailedSqlTransaction. The
helper must surface the ORIGINAL COPY error (the actionable cause), not the
cleanup error.
"""

from __future__ import annotations

import pytest

from tenantless.generator.writer import _dropped_secondary_indexes


class _FakeCursor:
    def __init__(self, conn: "_FakeConn"):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql: str, params=None):
        stripped = sql.strip().upper()
        if stripped.startswith("CREATE INDEX"):
            # Mimics InFailedSqlTransaction after the COPY aborted the txn.
            raise RuntimeError("current transaction is aborted (cleanup)")
        self._conn.executed.append(sql.strip())

    def fetchall(self):
        # One catalog-derived secondary index to drop + recreate.
        return [("synthetic.idx_res_sub", "CREATE INDEX idx_res_sub ON synthetic.resources (subscription_id)")]


class _FakeConn:
    def __init__(self):
        self.executed: list[str] = []

    def cursor(self):
        return _FakeCursor(self)


class _CopyFailure(Exception):
    """Stand-in for a psycopg COPY error raised inside the managed block."""


def test_copy_error_survives_index_restore_failure():
    conn = _FakeConn()
    with pytest.raises(_CopyFailure) as excinfo:
        with _dropped_secondary_indexes(conn, "synthetic.resources"):
            raise _CopyFailure("binary COPY rejected row 42")

    # The ORIGINAL COPY error propagates, not the cleanup RuntimeError.
    assert "binary COPY rejected row 42" in str(excinfo.value)
    # The cleanup failure is preserved as context (chained), not lost silently.
    assert isinstance(excinfo.value.__context__, RuntimeError)
    assert "transaction is aborted" in str(excinfo.value.__context__)


def test_index_restore_failure_surfaces_on_success_path():
    """With NO original error, a genuine restore failure must NOT be swallowed."""
    conn = _FakeConn()
    with pytest.raises(RuntimeError, match="transaction is aborted"):
        with _dropped_secondary_indexes(conn, "synthetic.resources"):
            pass  # body succeeds; the finally CREATE INDEX raises and must surface
