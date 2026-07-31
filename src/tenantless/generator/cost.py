"""Per-resource cost materialization post-pass (Plan 09-03, COST-01).

The third instance of the Phase-5 in-memory post-pass idiom (twin of
``violations.inject`` / ``cross_sub.build_cross_sub``): an in-memory pass over
the built ``Tenant`` from the same ``SeededContext`` that samples a synthetic
monthly cost per resource per period from the profile's fitted
``cost_distributions`` and returns the rows that ``writer.copy_cost_records``
binary-COPYs into ``synthetic.cost_records`` (FK order, after resources).

Determinism (COST-01 / T-9-REPRO): every draw goes through ``ctx.rng`` over a
population SORTED by resource id (``sorted(all_res, key=lambda r: r.id)``,
exactly like ``violations._eligible_population``), and the period list is a fixed
deterministic sequence. The same ``(tenant, cost_distributions, seed)`` therefore
yields byte-identical ``cost_records``. Every draw flows through the injected
``SeededContext`` (``ctx.rng``) — never a process-global RNG or a fresh Faker
instance — so the byte-reproducibility rule stays auditable by grep.

Currency (D-11): the seed magnitudes are EUR, but the fit captures a distribution
*shape* (lognormal mu/sigma) and we *sample* synthetic numbers, so the magnitudes
carry over relabeled as **USD** with no FX conversion and no real-bill leak.

Granularity (D-08 / D-09):
- ``monthly`` (default) → 12 first-of-month periods ending at the current month
  (~6M rows at 500K resources). Covers TheLastMonth / MonthToDate /
  BillingMonthToDate and a year of trend.
- ``daily`` → one row per day of the CURRENT month only (the D-09 short window,
  ~30 rows/resource ≈ 15M at 500K) — deliberately NOT the 180M-row full-year case
  (Pitfall 6).

Back-compat (D-02): an empty ``cost_distributions`` (a profile fitted from a
source with no usable cost data) is a clean no-op — zero rows, zero RNG draws —
so a cost-less ``generate`` stays byte-identical to the Phase-8 baseline. A
resource whose canonical type has no fitted distribution (and no ``__default__``)
also yields zero cost rows: the faithful fallback, since the un-fitted Azure
types (NSG, Key Vault, VNet, NIC, ...) genuinely cost ~0.
"""

from __future__ import annotations

import calendar
import datetime as _dt
import os
import tempfile
import uuid as _uuid
from collections.abc import Iterator

import orjson

from .rng import SeededContext

CURRENCY = "USD"  # D-11: USD for v2.0 (EUR seed magnitudes relabeled, no FX).
DEFAULT_PERIODS = 12  # D-08: 12 monthly periods by default.
_DEFAULT_KEY = "__default__"  # optional unmapped-type fallback distribution.


def _monthly_periods(n: int, ref: _dt.date) -> list[_dt.date]:
    """``n`` first-of-month dates, oldest first, ending at ``ref``'s month."""
    y, m = ref.year, ref.month
    out: list[_dt.date] = []
    for i in range(n - 1, -1, -1):
        yy, mm = y, m - i
        while mm <= 0:
            mm += 12
            yy -= 1
        out.append(_dt.date(yy, mm, 1))
    return out


def _daily_periods(ref: _dt.date, window: int | None) -> list[_dt.date]:
    """Every day of ``ref``'s month (the D-09 short window).

    ``window`` optionally caps the day count (from the 1st); ``None`` = the whole
    current month. Never spans more than the current month — the 180M-row
    full-year daily case is out of scope (Pitfall 6).
    """
    days_in_month = calendar.monthrange(ref.year, ref.month)[1]
    n = days_in_month if window is None else max(0, min(window, days_in_month))
    return [_dt.date(ref.year, ref.month, day) for day in range(1, n + 1)]


def _draw_cost(ctx: SeededContext, dist: dict) -> float:
    """One non-negative cost draw from a fitted distribution via ``ctx.rng``.

    ``lognormal`` uses ``ctx.rng.lognormal(mu, sigma)`` (numpy ``Generator`` —
    the only sanctioned RNG). A ``gamma`` fit (the documented alternative family)
    draws via ``ctx.rng.gamma(shape, scale)``. Both are strictly positive; the
    ``max(0.0, ...)`` is a defensive floor.
    """
    family = dist.get("distribution", "lognormal")
    if family == "gamma":
        shape = float(dist.get("shape", 1.0))
        scale = float(dist.get("scale", 1.0))
        value = float(ctx.rng.gamma(shape, scale))
    else:  # lognormal (default)
        mu = float(dist.get("mu", 0.0))
        sigma = float(dist.get("sigma", 1.0))
        value = float(ctx.rng.lognormal(mu, sigma))
    return max(0.0, value)


def inject_cost(
    ctx: SeededContext,
    tenant,
    cost_distributions: dict,
    *,
    granularity: str = "monthly",
    periods: int = DEFAULT_PERIODS,
    daily_window: int | None = None,
    today: _dt.date | None = None,
) -> list[dict]:
    """Materialize per-resource synthetic cost rows (COST-01).

    For each resource (in sorted-by-id order, for deterministic RNG draw order)
    whose canonical ``type`` has a fitted distribution in ``cost_distributions``
    (or, failing that, a ``__default__`` distribution), draw one cost per period
    via ``ctx.rng`` and emit a row::

        {resource_id, subscription_id, billing_period(date), cost_amount(float),
         currency: "USD"}

    Resources whose type has no distribution (and no ``__default__``) emit zero
    rows. An empty ``cost_distributions`` is a clean no-op (no draws, no rows) —
    preserving the Phase-8 baseline byte-for-byte.

    ``granularity`` ∈ ``{"monthly","daily"}``: monthly emits ``periods`` (default
    12) first-of-month rows ending at the current month; daily emits one row per
    day of the current month only (the D-09 short window; ``daily_window`` caps
    the day count). ``today`` is injectable for testing.

    This is the eager list wrapper (byte-identical to the pre-spool baseline) over
    the single-source :func:`_iter_cost_rows` generator: direct callers (the
    fingerprint / reproducibility / determinism tests) keep the tuple/list path,
    while the streaming CLI path drains the same generator INTO a :class:`CostSpool`
    so 6-15M dicts are never all resident at once.
    """
    return list(
        _iter_cost_rows(
            ctx,
            tenant,
            cost_distributions,
            granularity=granularity,
            periods=periods,
            daily_window=daily_window,
            today=today,
        )
    )


def _iter_cost_rows(
    ctx: SeededContext,
    tenant,
    cost_distributions: dict,
    *,
    granularity: str = "monthly",
    periods: int = DEFAULT_PERIODS,
    daily_window: int | None = None,
    today: _dt.date | None = None,
) -> Iterator[dict]:
    """The SINGLE source of cost draw order.

    Yields each cost row dict in the EXACT order the pre-spool ``inject_cost`` loop
    produced them (resources ``sorted(all_res, key=lambda r: r.id)`` × the fixed
    deterministic period list), driving every draw through ``ctx.rng``. Both the
    eager list path (:func:`inject_cost`) and the streaming spool path drain THIS
    generator, so the byte-reproducible draw order is defined in exactly one place.

    An empty ``cost_distributions`` returns immediately with no draws (the D-02
    back-compat no-op); a resource whose canonical type has no fitted distribution
    (and no ``__default__``) yields zero rows (the faithful free-type fallback).
    """
    if not cost_distributions:
        return  # D-02 back-compat: no cost section → no-op (no RNG drawn).

    ref = (today or _dt.date.today()).replace(day=1)
    if granularity == "daily":
        period_dates = _daily_periods(ref, daily_window)
    else:
        period_dates = _monthly_periods(periods, ref)

    default_dist = cost_distributions.get(_DEFAULT_KEY)
    all_res = [r for rg in tenant.resource_groups for r in rg.resources]

    for r in sorted(all_res, key=lambda res: res.id):
        dist = cost_distributions.get(r.type, default_dist)
        if dist is None:
            continue  # unmapped/free type → zero cost rows (the faithful fallback).
        for period in period_dates:
            yield {
                "resource_id": r.id,
                "subscription_id": r.subscription_id,
                "billing_period": period,
                "cost_amount": _draw_cost(ctx, dist),
                "currency": CURRENCY,
            }


class CostSpool:
    """Bounded on-disk NDJSON spool for cost rows (bounded memory).

    RAII analogue of the sibling branch's ``mock-server/src/snapshot.rs::TempSqlFile``:
    an unpredictable, 0o600-restricted temp file whose create HANDLE drops
    IMMEDIATELY (the ``mkstemp`` fd is closed right after creation so NO descriptor is
    inherited by the ``ProcessPoolExecutor`` fork), re-opened by path on append/read,
    and unlinked on EVERY exit path (success / failure / cancel).

    The streaming CLI generate drains :func:`_iter_cost_rows` into ``.append(row)``
    during the CPU phase (bounded peak residency of one row), then ``copy_cost_records``
    iterates the spool once into binary COPY during the write phase. ``__iter__``
    reconstructs every field to its ORIGINAL Python type — critically
    ``subscription_id`` back to ``uuid.UUID`` and ``billing_period`` back to
    ``datetime.date`` — because orjson serializes those to JSON strings and
    ``orjson.loads`` returns plain ``str``; psycopg3's binary ``UUIDBinaryDumper``
    would crash on a ``str`` (``AttributeError: 'str' object has no attribute 'int'``).
    """

    __slots__ = ("path", "fd", "_count")

    def __init__(self) -> None:
        self.path: str | None = None
        self.fd: int | None = None
        self._count = 0

    def __enter__(self) -> "CostSpool":
        fd, path = tempfile.mkstemp(prefix="tnl-cost-", suffix=".ndjson")
        # Restrict perms (best-effort on non-POSIX where chmod is a partial no-op),
        # then close the descriptor IMMEDIATELY so the class holds only the path —
        # no live fd is inherited across the multiprocessing fork.
        try:
            os.chmod(path, 0o600)
        except (OSError, NotImplementedError):
            pass  # non-POSIX / restricted FS: name is still unpredictable (mkstemp)
        os.close(fd)
        self.fd = None  # path-only; re-opened on append/read
        self.path = path
        self._count = 0
        return self

    def append(self, row: dict) -> None:
        """Serialize one row as an NDJSON line (orjson dumps UUID/date/float natively)
        by re-opening the path append-binary — no persistent handle to leak."""
        if self.path is None:  # pragma: no cover - misuse outside the with block
            raise RuntimeError("CostSpool.append called outside an active context")
        with open(self.path, "ab") as f:
            f.write(orjson.dumps(row))
            f.write(b"\n")
        self._count += 1

    def __iter__(self) -> Iterator[dict]:
        """Re-open the path from the start and yield each row with EVERY field
        reconstructed to its original Python type (re-iterable: re-opens each call)."""
        if self.path is None:  # empty/closed spool → nothing to yield
            return
        with open(self.path, "rb") as f:
            for line in f:
                if not line.strip():
                    continue
                d = orjson.loads(line)
                # Per-field type reconstruction (the load-bearing invariant):
                d["subscription_id"] = _uuid.UUID(d["subscription_id"])
                d["billing_period"] = _dt.date.fromisoformat(d["billing_period"])
                # resource_id (str), cost_amount (float), currency (str) survive as-is.
                yield d

    def __len__(self) -> int:
        return self._count

    def __bool__(self) -> bool:
        return self._count > 0

    def __exit__(self, *exc) -> bool:
        """Unlink the temp file on EVERY exit path (success / failure / cancel)."""
        try:
            if self.path is not None and os.path.exists(self.path):
                os.unlink(self.path)
        finally:
            self.path = None
        return False  # never suppress an in-block exception
