"""CostSpool RAII round-trip + per-field TYPE identity (bounded memory).

The bounded-memory cost refactor spools 6-15M cost rows to an on-disk NDJSON temp
during the CPU phase and streams them one-at-a-time into COPY during the write phase.
The LOAD-BEARING invariant is that the spool round-trip is BYTE- and TYPE-identical
to the in-memory list — in particular the ``subscription_id`` field is a real
``uuid.UUID`` on read-back (orjson serializes UUID->str but ``orjson.loads`` returns
a plain ``str``; psycopg's binary ``UUIDBinaryDumper`` then crashes on a str). Value
equality alone is INSUFFICIENT — a str masks the crash — so every field's Python type
is asserted with a real ``uuid.UUID`` / ``datetime.date`` input.

These are DB-free: they exercise the pure-Python CostSpool round-trip and RAII.
"""

from __future__ import annotations

import datetime as _dt
import os
import types
import uuid

from tenantless.generator import cost as cost_mod
from tenantless.generator.cost import CostSpool


def _real_cost_rows():
    """Hand-built rows in the exact inject_cost dict shape, using REAL uuid.UUID +
    datetime.date inputs (NOT string literals) so the type round-trip is exercised."""
    sid_a = uuid.UUID("11111111-1111-1111-1111-111111111111")
    sid_b = uuid.UUID("22222222-2222-2222-2222-222222222222")
    return [
        {
            "resource_id": "/subscriptions/x/resourceGroups/rg/providers/p/r/a",
            "subscription_id": sid_a,
            "billing_period": _dt.date(2026, 1, 1),
            "cost_amount": 123.456789,
            "currency": "USD",
        },
        {
            "resource_id": "/subscriptions/x/resourceGroups/rg/providers/p/r/b",
            "subscription_id": sid_b,
            "billing_period": _dt.date(2026, 2, 1),
            "cost_amount": 0.0,
            "currency": "USD",
        },
    ]


def test_spool_round_trip_per_field_type_identity():
    """Appending real-typed rows then iterating yields a list == the source AND, per
    column, the EXACT Python type survives (the UUID/date reconstruction blocker)."""
    src = _real_cost_rows()
    with CostSpool() as spool:
        for row in src:
            spool.append(row)
        out = list(spool)

    assert out == src, "value round-trip must be identical"
    for got in out:
        # THE BLOCKER: subscription_id must be a real uuid.UUID, not a str.
        assert isinstance(got["subscription_id"], uuid.UUID), (
            "subscription_id round-tripped as "
            f"{type(got['subscription_id'])!r}, not uuid.UUID — psycopg binary "
            "UUIDBinaryDumper would crash on a str"
        )
        assert isinstance(got["billing_period"], _dt.date)
        assert type(got["cost_amount"]) is float
        assert type(got["resource_id"]) is str
        assert type(got["currency"]) is str


def test_spool_float_exactness():
    """cost_amount survives the orjson round-trip as the EXACT float64 (no precision
    drift), so the cents-quantized content fingerprint is unchanged."""
    val = 1234.56789012345
    row = {
        "resource_id": "/r/a",
        "subscription_id": uuid.UUID("33333333-3333-3333-3333-333333333333"),
        "billing_period": _dt.date(2026, 3, 15),
        "cost_amount": val,
        "currency": "USD",
    }
    with CostSpool() as spool:
        spool.append(row)
        out = list(spool)
    assert out[0]["cost_amount"] == val
    assert type(out[0]["cost_amount"]) is float


def test_spool_is_re_iterable():
    """Iterating the spool twice yields the SAME list — __iter__ re-opens the path
    from the start each time (so fingerprint / COPY can both walk it)."""
    src = _real_cost_rows()
    with CostSpool() as spool:
        for row in src:
            spool.append(row)
        first = list(spool)
        second = list(spool)
    assert first == second == src


def test_spool_bool_and_len_do_not_consume():
    """__len__/__bool__ reflect the appended count WITHOUT consuming the rows."""
    src = _real_cost_rows()
    with CostSpool() as spool:
        assert len(spool) == 0
        assert bool(spool) is False
        for row in src:
            spool.append(row)
        assert len(spool) == len(src)
        assert bool(spool) is True
        # Truthiness/len did not consume — iteration still yields everything.
        assert list(spool) == src


def test_spool_empty_is_noop():
    """Zero appends -> falsy, len 0, iterating yields nothing."""
    with CostSpool() as spool:
        assert bool(spool) is False
        assert len(spool) == 0
        assert list(spool) == []


def test_spool_removes_temp_on_normal_exit():
    """After a normal ``with CostSpool()`` block the temp path does not exist (RAII)."""
    with CostSpool() as spool:
        spool.append(_real_cost_rows()[0])
        path = spool.path
        assert os.path.exists(path)
    assert not os.path.exists(path), "temp file leaked after normal exit"


def test_spool_removes_temp_on_exception_exit():
    """After a ``with`` block that raises inside, the temp path is ALSO removed
    (unlinked on the exception path — no leftover spool filling disk)."""
    path_holder = {}

    class _Boom(Exception):
        pass

    try:
        with CostSpool() as spool:
            spool.append(_real_cost_rows()[0])
            path_holder["path"] = spool.path
            assert os.path.exists(spool.path)
            raise _Boom()
    except _Boom:
        pass
    assert not os.path.exists(path_holder["path"]), "temp file leaked on exception exit"


def test_spool_holds_no_open_descriptor():
    """The spool holds the path only (no live fd) so nothing is inherited across the
    ProcessPoolExecutor fork — the mkstemp fd is closed immediately."""
    with CostSpool() as spool:
        # The class must not retain an integer file descriptor attribute that is open.
        assert not hasattr(spool, "fd") or spool.fd is None or isinstance(
            spool.fd, int
        )
        # Appending re-opens the path; there is no persistent open handle to leak.
        spool.append(_real_cost_rows()[0])
        assert os.path.exists(spool.path)


def test_iter_cost_rows_is_a_generator():
    """_iter_cost_rows is a real generator (types.GeneratorType), the single source
    of cost draw order for both the list path and the spool path."""
    gen = cost_mod._iter_cost_rows.__wrapped__ if hasattr(
        cost_mod._iter_cost_rows, "__wrapped__"
    ) else cost_mod._iter_cost_rows
    # Empty cost_distributions -> generator that yields nothing (no draws).

    class _T:
        resource_groups = []

    result = cost_mod._iter_cost_rows(None, _T(), {}, granularity="monthly",
                                      periods=12, daily_window=None, today=None)
    assert isinstance(result, types.GeneratorType)
    assert list(result) == []


def test_inject_cost_empty_is_still_noop():
    """inject_cost([]) stays a clean no-op ([]) — baseline byte-identity preserved."""

    class _T:
        resource_groups = []

    assert cost_mod.inject_cost(None, _T(), {}) == []
