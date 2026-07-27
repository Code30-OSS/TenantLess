"""Generator identity post-pass tests (Plan 10-01, IAM-01/IAM-02, D-01..D-07).

DB-free unit tests for ``generator.identity`` — the seeded,
``SeededContext``-driven synthetic-principal + role-assignment materializer that
is the FOURTH instance of the Phase-5 ``violations.inject`` / Phase-9
``cost.inject_cost`` post-pass idiom (D-01/D-03).

Given / When / Then BDD framing per task behavior:

- **Principals (IAM-01/D-02):** ~70 % User / 15 % Group / 15 % ServicePrincipal,
  ARM-opaque GUID ``oid`` (no real-identifier-shaped display strings — Pitfall 5),
  byte-reproducible for a fixed ``(tenant, seed)``, population scales with tenant
  size (a documented formula of subscription + resource counts).
- **Role assignments (IAM-02/D-04/D-07):** every row references a real principal
  ``oid`` + a built-in roleDefinition GUID (tenant-scoped) + a REAL scope
  (subscription / RG / resource id that exists in the tenant) — 0-dangling.
- **Over-privilege injection (D-05/D-06):** a SECOND ``ctx.bernoulli(rate)`` pass
  adds Owner-at-subscription rows at a configurable rate; ``rate=0`` adds zero;
  the injected count is RETURNED for the run summary, and the injected row carries
  NO "finding" marker column (the over-privilege IS the assignment, D-06).

The engine operates on the in-memory ``Tenant`` produced by ``generate_tenant``
before any COPY, so these tests need no Postgres.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

from tenantless.generator import identity
from tenantless.generator.pipeline import generate_tenant
from tenantless.generator.rng import SeededContext

# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def _base_tenant(profile, *, seed=42, n_subs=20, n_resources=2000):
    """A deterministic base tenant with every post-pass off (no identity yet)."""
    return generate_tenant(
        profile,
        seed=seed,
        n_subs=n_subs,
        n_resources=n_resources,
        inject_violations=False,
        inject_cross_sub=False,
        inject_cost=False,
    ).tenant


def _real_scope_ids(tenant):
    """The UNION of every real generated scope id (sub / RG / resource) — the only
    legal ``scope`` values (Pitfall 1: select, never invent)."""
    subs = {f"/subscriptions/{s.subscription_id}" for s in tenant.subscriptions}
    rgs = {rg.id for rg in tenant.resource_groups}
    res = {r.id for rg in tenant.resource_groups for r in rg.resources}
    return subs | rgs | res


def _role_def_ids():
    return {
        identity.ROLE_DEF_ID_PREFIX + guid
        for guid, _ in identity.BUILTIN_ROLE_DEFINITIONS
    }


_ASSIGNMENT_KEYS = {
    "assignment_id",
    "subscription_id",
    "principal_oid",
    "principal_type",
    "role_definition_id",
    "scope",
}


# --------------------------------------------------------------------------- #
# Principals (IAM-01 / D-02)
# --------------------------------------------------------------------------- #


def test_principals_reproducible(generator_profile):
    """Given a fixed (tenant, seed), When principals are generated twice, Then the
    oid+type rows are byte-identical and the type mix is ~70/15/15 (IAM-01/D-02)."""
    tenant = _base_tenant(generator_profile)
    first = identity.generate_principals(SeededContext(42), tenant)
    second = identity.generate_principals(SeededContext(42), tenant)

    assert [(p["oid"], p["principal_type"]) for p in first] == [
        (p["oid"], p["principal_type"]) for p in second
    ]

    # ARM-opaque (Pitfall 5): no display strings; app_id only for ServicePrincipal.
    for p in first:
        assert p["display_name"] is None
        if p["principal_type"] == "ServicePrincipal":
            assert p["app_id"] is not None
        else:
            assert p["app_id"] is None

    n = len(first)
    assert n >= 100, f"population too small for a stable mix: {n}"
    counts = Counter(p["principal_type"] for p in first)
    users = counts["User"] / n
    groups = counts["Group"] / n
    sps = counts["ServicePrincipal"] / n
    assert users > groups and users > sps
    assert 0.60 <= users <= 0.80, users
    assert 0.07 <= groups <= 0.25, groups
    assert 0.07 <= sps <= 0.25, sps


def test_principal_count_scales(generator_profile):
    """More subscriptions/resources → more principals (D-02 documented formula)."""
    small = _base_tenant(generator_profile, n_subs=10, n_resources=500)
    large = _base_tenant(generator_profile, n_subs=40, n_resources=4000)
    n_small = len(identity.generate_principals(SeededContext(1), small))
    n_large = len(identity.generate_principals(SeededContext(1), large))
    assert n_large > n_small


# --------------------------------------------------------------------------- #
# Role assignments (IAM-02 / D-04 / D-07)
# --------------------------------------------------------------------------- #


def test_assignments_reference_real_pools(generator_profile):
    """Every assignment references a real principal oid + a built-in roleDefinition
    (tenant-scoped) + a real scope — the 0-dangling three-way chain (IAM-02/D-07)."""
    tenant = _base_tenant(generator_profile)
    ctx = SeededContext(42)
    principals = identity.generate_principals(ctx, tenant)
    rows, _ = identity.assign_roles(
        ctx, tenant, principals, over_privilege_rate=0.05
    )

    oids = {p["oid"] for p in principals}
    scopes = _real_scope_ids(tenant)
    role_ids = _role_def_ids()
    assert rows, "expected a non-empty assignment pool"
    for a in rows:
        assert a["principal_oid"] in oids
        assert a["role_definition_id"] in role_ids
        assert a["role_definition_id"].startswith(identity.ROLE_DEF_ID_PREFIX)
        assert a["scope"] in scopes
        assert a["principal_type"] in {"User", "Group", "ServicePrincipal"}


# --------------------------------------------------------------------------- #
# Over-privilege injection (D-05 / D-06)
# --------------------------------------------------------------------------- #


def test_over_privilege_rate(generator_profile):
    """rate=0 → zero over-priv rows; a positive rate injects proportionally and the
    injected count is RETURNED for the run summary; no "finding" marker (D-05/D-06)."""
    tenant = _base_tenant(generator_profile)

    ctx0 = SeededContext(42)
    principals0 = identity.generate_principals(ctx0, tenant)
    rows0, n0 = identity.assign_roles(
        ctx0, tenant, principals0, over_privilege_rate=0.0
    )
    assert n0 == 0
    base_count = len(rows0)

    ctx1 = SeededContext(42)
    principals1 = identity.generate_principals(ctx1, tenant)
    rows1, n1 = identity.assign_roles(
        ctx1, tenant, principals1, over_privilege_rate=0.5
    )
    assert n1 > 0
    # over-priv rows are ADDED on top of the baseline mix.
    assert len(rows1) == base_count + n1

    ctx2 = SeededContext(42)
    principals2 = identity.generate_principals(ctx2, tenant)
    _, n2 = identity.assign_roles(
        ctx2, tenant, principals2, over_privilege_rate=1.0
    )
    # Higher rate → strictly more injected rows (proportional to the rate).
    assert n2 > n1
    assert n2 == len(principals2)  # rate=1.0 → one over-priv grant per principal

    # D-06: an injected over-privilege row is a REAL Owner assignment — no synthetic
    # "finding"/marker column; its key set is exactly the served assignment shape.
    owner_id = identity.ROLE_DEF_ID_PREFIX + identity.GUID_BY_ROLE["Owner"]
    injected = rows1[base_count:]
    for a in injected:
        assert set(a.keys()) == _ASSIGNMENT_KEYS
        assert "finding" not in a
        assert a["role_definition_id"] == owner_id
        assert a["scope"].startswith("/subscriptions/")


def test_baseline_no_identity_noop(generator_profile):
    """An empty principal pool is a clean no-op: zero assignments, zero RNG draws —
    the Phase-8 byte-identical baseline guard (no draw → no sequence shift)."""
    tenant = _base_tenant(generator_profile)
    ctx = SeededContext(7)
    probe = SeededContext(7)
    rows, n = identity.assign_roles(ctx, tenant, [], over_privilege_rate=0.5)
    assert rows == [] and n == 0
    # zero draws consumed: ctx stays byte-aligned with an untouched same-seed context.
    assert ctx.rng.random() == probe.rng.random()


# --------------------------------------------------------------------------- #
# Cross-language catalogue pin (Pitfall 3 / WR-02)
# --------------------------------------------------------------------------- #


def _authorization_rs_path() -> Path:
    """Locate mock-server/src/handlers/authorization.rs relative to the repo root.

    This test lives at ``<repo>/tests/test_generator_identity.py`` so the repo root
    is ``parents[1]``.
    """
    return (
        Path(__file__).resolve().parents[1]
        / "mock-server"
        / "src"
        / "handlers"
        / "authorization.rs"
    )


def test_rust_catalogue_pins_identity_py_across_languages():
    """Pitfall 3 (THE genuine cross-language pin): the Rust ``BUILTIN_ROLE_DEFINITIONS``
    const in ``authorization.rs`` must equal ``identity.BUILTIN_ROLE_DEFINITIONS``
    EXACTLY — same (guid, roleName) pairs, same order, same count (8).

    The Rust-side ``catalogue_pins_all_eight_guids`` only compares the const to a list
    hardcoded IN the Rust test, so it never reads identity.py — a Python-side GUID or
    roleName typo previously failed NO test. This test READS the Rust source and
    extracts the ``BuiltinRole {{ guid: "…", role_name: "…", … }}`` struct literals, so
    EITHER side drifting goes red here. Mutating any GUID/roleName on EITHER side fails.
    """
    rs_path = _authorization_rs_path()
    if not rs_path.is_file():
        pytest.skip(f"Rust source not present in this checkout: {rs_path}")

    text = rs_path.read_text(encoding="utf-8")
    # Each catalogue entry is a `BuiltinRole { guid: "…", role_name: "…", … }` literal;
    # the `guid:` field is immediately followed by `role_name:` (whitespace/newline
    # between). The const is the ONLY place this field syntax appears (the Rust test's
    # EXPECTED uses `("…", "…")` tuple syntax), so a whole-file scan is unambiguous.
    pairs = re.findall(
        r'guid:\s*"([^"]+)"\s*,\s*role_name:\s*"([^"]+)"',
        text,
    )
    rust_catalogue = [(guid, role_name) for guid, role_name in pairs]

    assert len(rust_catalogue) == 8, (
        "expected exactly 8 BuiltinRole entries extracted from authorization.rs, "
        f"got {len(rust_catalogue)}"
    )
    assert rust_catalogue == list(identity.BUILTIN_ROLE_DEFINITIONS), (
        "the Rust authorization.rs BUILTIN_ROLE_DEFINITIONS const must match "
        "identity.BUILTIN_ROLE_DEFINITIONS VERBATIM (guid + roleName, in order) — "
        "a drift on EITHER side is a cross-language coupling break (Pitfall 3 / WR-02)"
    )
