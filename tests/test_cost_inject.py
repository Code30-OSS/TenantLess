"""Generator cost post-pass tests (Plan 09-03, COST-01).

DB-free unit tests for ``generator.cost.inject_cost`` — the deterministic,
``SeededContext``-driven per-resource cost materializer that is the twin of
``violations.inject``. Covers:

- COST-01 reproducibility: the same ``(tenant, dists, seed)`` yields
  byte-identical ``cost_records`` (same order, same values) because every draw
  goes through ``ctx.rng`` over a population sorted by resource id.
- COST-01 granularity: ``granularity="monthly"`` emits 12 first-of-month periods
  per cost-bearing resource; ``granularity="daily"`` emits one row per day of the
  current month only (the D-09 short window).
- Back-compat: an empty ``cost_distributions`` is a clean no-op (zero rows), and a
  cost-less ``generate_tenant`` keeps the Phase-8 baseline byte-identical
  (violations / dependencies unchanged) with an empty ``cost_records`` tuple.

The engine operates on the in-memory ``Tenant`` produced by ``generate_tenant``
before any COPY, so these tests need no Postgres.
"""

from __future__ import annotations

import calendar
import datetime as _dt

from tenantless.generator import cost, resources
from tenantless.generator.pipeline import generate_tenant
from tenantless.generator.rng import SeededContext


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def _base_tenant(profile, *, seed=42, n_subs=20, n_resources=2000):
    """A deterministic, UNMUTATED base tenant (every post-pass off)."""
    return generate_tenant(
        profile,
        seed=seed,
        n_subs=n_subs,
        n_resources=n_resources,
        inject_violations=False,
        inject_cross_sub=False,
        inject_cost=False,
    ).tenant


def _all_res(tenant):
    return [r for rg in tenant.resource_groups for r in rg.resources]


def _present_types(tenant):
    return {r.type for r in _all_res(tenant)}


def _dists_for(tenant):
    """Build a cost_distributions dict for two types that the tenant actually
    contains, so the post-pass has a non-empty population to draw on."""
    present = _present_types(tenant)
    candidates = [
        resources.T_STORAGE,
        resources.T_VM,
        resources.T_DISK,
        resources.T_NIC,
        resources.T_KV,
    ]
    chosen = [t for t in candidates if t in present][:2]
    assert chosen, f"no candidate cost type present in tenant; present={present}"
    return {
        t: {"distribution": "lognormal", "mu": 3.0, "sigma": 1.0} for t in chosen
    }, set(chosen)


# --------------------------------------------------------------------------- #
# COST-01 — seeded byte-reproducibility
# --------------------------------------------------------------------------- #


def test_seeded_reproducible(generator_profile):
    """Two inject_cost runs over the same tenant with the same seed produce
    byte-identical cost_records — same order, same values (the COST-01 gate)."""
    tenant = _base_tenant(generator_profile)
    dists, _types = _dists_for(tenant)

    rows_a = cost.inject_cost(SeededContext(7), tenant, dists)
    rows_b = cost.inject_cost(SeededContext(7), tenant, dists)

    # Non-empty (otherwise the determinism claim is vacuous).
    assert rows_a, "expected cost rows for cost-bearing resource types"
    # Byte-identical row sequences.
    assert rows_a == rows_b
    # A different seed diverges (proves the draw actually depends on the RNG).
    rows_c = cost.inject_cost(SeededContext(8), tenant, dists)
    assert rows_c != rows_a

    # Row shape contract: resource_id, subscription_id, billing_period(date),
    # cost_amount(float>=0), currency "USD".
    sample = rows_a[0]
    assert set(sample) == {
        "resource_id",
        "subscription_id",
        "billing_period",
        "cost_amount",
        "currency",
    }
    assert isinstance(sample["billing_period"], _dt.date)
    assert isinstance(sample["cost_amount"], float)
    assert sample["cost_amount"] >= 0.0
    assert sample["currency"] == "USD"


# --------------------------------------------------------------------------- #
# COST-01 — granularity modes
# --------------------------------------------------------------------------- #


def test_granularity_modes(generator_profile):
    """monthly → 12 first-of-month periods per cost-bearing resource; daily → one
    row per day of the CURRENT month only (the D-09 short window)."""
    tenant = _base_tenant(generator_profile)
    dists, cost_types = _dists_for(tenant)

    cost_res_ids = [r.id for r in _all_res(tenant) if r.type in cost_types]
    assert cost_res_ids, "no cost-bearing resources to assert against"

    # --- monthly (default) -------------------------------------------------- #
    monthly = cost.inject_cost(SeededContext(7), tenant, dists, granularity="monthly")
    by_res: dict[str, list] = {}
    for row in monthly:
        by_res.setdefault(row["resource_id"], []).append(row)

    # Every cost-bearing resource gets exactly 12 periods, all first-of-month,
    # all distinct months.
    for rid in cost_res_ids:
        periods = sorted(r["billing_period"] for r in by_res[rid])
        assert len(periods) == 12
        assert all(p.day == 1 for p in periods)
        assert len({(p.year, p.month) for p in periods}) == 12
    # The newest period is the current month (first-of-month).
    today_fom = _dt.date.today().replace(day=1)
    newest = max(r["billing_period"] for r in monthly)
    assert newest == today_fom

    # --- daily (short window = current month only) -------------------------- #
    daily = cost.inject_cost(SeededContext(7), tenant, dists, granularity="daily")
    daily_by_res: dict[str, list] = {}
    for row in daily:
        daily_by_res.setdefault(row["resource_id"], []).append(row)

    days_in_month = calendar.monthrange(today_fom.year, today_fom.month)[1]
    for rid in cost_res_ids:
        periods = [r["billing_period"] for r in daily_by_res[rid]]
        assert len(periods) == days_in_month
        # All within the current month (the short window — never a 12-month blow-up).
        assert all(
            p.year == today_fom.year and p.month == today_fom.month for p in periods
        )


# --------------------------------------------------------------------------- #
# Back-compat — cost-less no-op keeps the Phase-8 baseline byte-identical
# --------------------------------------------------------------------------- #


def test_empty_distributions_is_noop(generator_profile):
    """An empty cost_distributions returns an empty list (no draws, no rows)."""
    tenant = _base_tenant(generator_profile)
    assert cost.inject_cost(SeededContext(7), tenant, {}) == []


def test_unmapped_type_yields_no_rows(generator_profile):
    """A distribution for a type absent from the tenant produces zero cost rows
    (the documented zero-cost fallback for unmapped/free types)."""
    tenant = _base_tenant(generator_profile)
    bogus = {"Microsoft.Nonexistent/widgets": {"distribution": "lognormal",
                                                "mu": 1.0, "sigma": 0.5}}
    assert cost.inject_cost(SeededContext(7), tenant, bogus) == []


def test_costless_generate_is_phase8_byte_identical(generator_profile):
    """A cost-less profile generated with inject_cost ON is byte-identical to the
    inject_cost OFF run: cost_records is empty and violations/dependencies are
    unchanged (the Phase-8 baseline is preserved)."""
    assert "cost_distributions" not in generator_profile  # the test-small profile is v1.x

    with_cost = generate_tenant(
        generator_profile, seed=42, n_subs=20, n_resources=2000, inject_cost=True
    )
    without_cost = generate_tenant(
        generator_profile, seed=42, n_subs=20, n_resources=2000, inject_cost=False
    )

    # No cost section → zero cost rows either way.
    assert with_cost.cost_records == ()
    assert without_cost.cost_records == ()
    # The other domains are byte-identical (cost was a true no-op — no RNG drawn).
    assert with_cost.violations == without_cost.violations
    assert with_cost.dependencies == without_cost.dependencies
    assert with_cost.clamp_notes == without_cost.clamp_notes
    ids_with = [r.id for rg in with_cost.tenant.resource_groups for r in rg.resources]
    ids_without = [
        r.id for rg in without_cost.tenant.resource_groups for r in rg.resources
    ]
    assert ids_with == ids_without


# --------------------------------------------------------------------------- #
# COST-01 — billing-period anchor (P1 fix: derive from cost_as_of, not today())
# --------------------------------------------------------------------------- #


def _cost_bearing_profile(generator_profile):
    """generator_profile augmented with a cost_distributions section for two types
    the tenant actually contains, so the pipeline's inject_cost path emits rows."""
    tenant = _base_tenant(generator_profile)
    dists, _types = _dists_for(tenant)
    return {**generator_profile, "cost_distributions": dists}


def test_cost_as_of_anchors_periods_not_today(generator_profile):
    """The pipeline threads --cost-as-of: billing periods derive EXCLUSIVELY from
    the given calendar date, never date.today(). So a fixed (profile, seed,
    cost_as_of) is byte-reproducible across calendar days — the P1 reproducibility
    fix. A past anchor proves periods do not track 'now'."""
    prof = _cost_bearing_profile(generator_profile)
    as_of = _dt.date(2020, 1, 15)

    res = generate_tenant(
        prof, seed=42, n_subs=20, n_resources=2000, cost_as_of=as_of
    )
    assert res.cost_records, "expected cost rows for a cost-bearing profile"
    periods = {c["billing_period"] for c in res.cost_records}
    # Monthly: 12 first-of-month periods ANCHORED to as_of (newest = its month),
    # independent of today — a 2020 anchor never yields a current-year period.
    assert max(periods) == _dt.date(2020, 1, 1)
    assert all(p.day == 1 for p in periods)
    assert len(periods) == 12

    # Byte-reproducible across runs with the same anchor.
    res2 = generate_tenant(
        prof, seed=42, n_subs=20, n_resources=2000, cost_as_of=as_of
    )
    assert res2.cost_records == res.cost_records

    # A different anchor shifts the periods (proving periods derive from as_of).
    other = generate_tenant(
        prof, seed=42, n_subs=20, n_resources=2000, cost_as_of=_dt.date(2021, 6, 10)
    )
    other_periods = {c["billing_period"] for c in other.cost_records}
    assert max(other_periods) == _dt.date(2021, 6, 1)
    assert other.cost_records != res.cost_records
