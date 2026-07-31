"""copy_cost_records: no redundant tuple->list re-copy.

``GenerationResult.cost_records`` is a frozen ``tuple``; the writer used to do
``rows = list(rows or [])`` — a full re-copy of (at scale) 6-15M cost dicts held
in memory only to be iterated once. This drops the re-copy: a tuple is iterated
DIRECTLY into COPY, ``None`` still normalizes to a no-op.

These are DB-free: a minimal fake conn/cursor/copy records every ``write_row``
tuple, so we can assert the COPY payload is byte-order-identical to the source
rows (the fingerprint-order guarantee) and that None/empty open no COPY at all.
"""

from __future__ import annotations

import datetime as _dt
import uuid

from tenantless.generator import writer as writer_mod
from tenantless.generator.cost import CostSpool


class _FakeCopy:
    def __init__(self, sql, log):
        self.sql = sql
        self._log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def set_types(self, types):
        self._log["types"] = types

    def write_row(self, row):
        self._log["rows"].append(row)


class _FakeCursor:
    def __init__(self, log):
        self._log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def copy(self, sql):
        self._log["copy_opened"] = True
        self._log["sql"] = sql
        return _FakeCopy(sql, self._log)


class _FakeConn:
    def __init__(self):
        self.log = {"rows": [], "copy_opened": False}

    def cursor(self):
        return _FakeCursor(self.log)


def _cost_dict(rid, sid, day, amount, currency="USD"):
    return {
        "resource_id": rid,
        "subscription_id": sid,
        "billing_period": _dt.date(2026, 1, day),
        "cost_amount": amount,
        "currency": currency,
    }


def _expected_tuple(c):
    return (
        c["resource_id"],
        c["subscription_id"],
        c["billing_period"],
        c["cost_amount"],
        c["currency"],
    )


def test_tuple_input_copy_payload_identical():
    """A tuple of cost dicts is written to COPY as the exact 5-field tuples, in the
    SAME order as the input — byte-order-identical payload (fingerprint guarantee)."""
    rows = (
        _cost_dict("/r/a", "11111111-1111-1111-1111-111111111111", 1, 10.0),
        _cost_dict("/r/b", "22222222-2222-2222-2222-222222222222", 2, 20.5),
        _cost_dict("/r/c", "33333333-3333-3333-3333-333333333333", 3, 0.0),
    )
    conn = _FakeConn()
    writer_mod.copy_cost_records(conn, rows)

    assert conn.log["copy_opened"] is True
    assert conn.log["rows"] == [_expected_tuple(c) for c in rows]
    # Order identity spelled out: COPY order == input tuple order.
    assert [r[0] for r in conn.log["rows"]] == [c["resource_id"] for c in rows]
    # The binary type contract is unchanged.
    assert conn.log["types"] == ["text", "uuid", "date", "float8", "text"]


def test_none_and_empty_are_noops():
    """copy_cost_records(conn, None) and (conn, ()) record ZERO write_row calls and
    never open a COPY (matches the copy_violations no-op contract)."""
    for empty in (None, (), []):
        conn = _FakeConn()
        writer_mod.copy_cost_records(conn, empty)
        assert conn.log["rows"] == []
        assert conn.log["copy_opened"] is False


def test_cost_records_from_inject_match_copy_order():
    """Closes the loop: the COPY write_row order equals direct iteration of the same
    cost_records tuple (cost_records order == COPY order == fingerprint order).

    Uses a hand-built tuple of the inject_cost dict shape — the load-bearing
    assertion is order/value identity, not live generation."""
    cost_records = tuple(
        _cost_dict(f"/sub/x/r{i}", "44444444-4444-4444-4444-444444444444", (i % 27) + 1, float(i))
        for i in range(50)
    )
    conn = _FakeConn()
    writer_mod.copy_cost_records(conn, cost_records)

    # Recorded payload == direct iteration of the source tuple, same order.
    assert conn.log["rows"] == [_expected_tuple(c) for c in cost_records]


def _real_uuid_cost_dict(rid, sid: uuid.UUID, day, amount):
    """A cost dict whose subscription_id is a REAL uuid.UUID (as the pipeline emits)."""
    return {
        "resource_id": rid,
        "subscription_id": sid,
        "billing_period": _dt.date(2026, 1, day),
        "cost_amount": amount,
        "currency": "USD",
    }


def test_spool_input_copy_payload_identical_to_tuple(tmp_path=None):
    """iterating a CostSpool writes the EXACT same COPY payload as
    the equivalent tuple — same 5-field tuples, same order, same per-field types
    (incl. subscription_id as uuid.UUID). Byte-order-identical (the fingerprint
    guarantee holds through the spool round-trip)."""
    sids = [
        uuid.UUID("11111111-1111-1111-1111-111111111111"),
        uuid.UUID("22222222-2222-2222-2222-222222222222"),
        uuid.UUID("33333333-3333-3333-3333-333333333333"),
    ]
    rows = [
        _real_uuid_cost_dict("/r/a", sids[0], 1, 10.0),
        _real_uuid_cost_dict("/r/b", sids[1], 2, 20.5),
        _real_uuid_cost_dict("/r/c", sids[2], 3, 0.0),
    ]
    # Reference: COPY payload from the in-memory tuple.
    tuple_conn = _FakeConn()
    writer_mod.copy_cost_records(tuple_conn, tuple(rows))

    # COPY payload from the spool (round-tripped through disk).
    with CostSpool() as spool:
        for r in rows:
            spool.append(r)
        spool_conn = _FakeConn()
        writer_mod.copy_cost_records(spool_conn, spool)

    assert spool_conn.log["copy_opened"] is True
    assert spool_conn.log["rows"] == tuple_conn.log["rows"], (
        "spool COPY payload must be byte-order-identical to the tuple payload"
    )
    # The subscription_id column reached COPY as a uuid.UUID, NOT a str — the
    # psycopg binary UUIDBinaryDumper blocker.
    for recorded in spool_conn.log["rows"]:
        assert type(recorded[1]) is uuid.UUID, (
            f"subscription_id reached COPY as {type(recorded[1])!r}, not uuid.UUID"
        )
    assert spool_conn.log["types"] == ["text", "uuid", "date", "float8", "text"]


def test_empty_spool_opens_no_copy():
    """An empty CostSpool is falsy → copy_cost_records is a no-op (no COPY opened)."""
    with CostSpool() as spool:
        conn = _FakeConn()
        writer_mod.copy_cost_records(conn, spool)
    assert conn.log["rows"] == []
    assert conn.log["copy_opened"] is False
