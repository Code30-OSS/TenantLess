"""D-01 reproducibility: two in-memory generation runs with the same
(profile, seed, targets) must produce byte-identical subscription + RG row
lists.

Mirrors ``test_archetypes.py::test_archetypes_are_reproducible``'s
``assert first == second`` idiom. No DB — pure in-memory sampler.
"""

from __future__ import annotations

from tenantless.generator.pipeline import generate_tenant

from _fingerprint import fingerprint


def test_full_result_reproducible(generator_profile):
    """Same (profile, seed) → byte-identical FULL GenerationResult across two
    runs, via the canonical fingerprint — not just subs + RGs + identity but
    cost / violations / dependencies / cross-sub too (13-02, SPEED-02).

    GREEN on current code; this is the seam the 13-03 RNG re-architecture must
    keep GREEN (run1 == run2 holds independently of the substream restructure).
    """
    first = generate_tenant(
        generator_profile, seed=42, n_subs=30, n_resources=2000
    )
    second = generate_tenant(
        generator_profile, seed=42, n_subs=30, n_resources=2000
    )

    # Sanity: the identity post-pass the legacy test only partly covered is
    # populated, and the fingerprint is non-degenerate — a DIFFERENT seed yields
    # a different digest — so equality below is real content agreement, not an
    # equal-because-empty artifact. (The test-small profile carries no cost
    # distributions, so cost_records may be empty; the fingerprint covers it
    # structurally either way.)
    assert first.principals, "identity post-pass should be populated"
    other = generate_tenant(
        generator_profile, seed=7, n_subs=30, n_resources=2000
    )
    assert fingerprint(first) != fingerprint(other)

    fp = fingerprint(first)
    assert len(fp) == 64
    assert fp == fingerprint(second)


def test_two_runs_identical(generator_profile):
    """Same (profile, seed, targets) → identical tenant (D-01)."""
    first = generate_tenant(
        generator_profile, seed=42, n_subs=30, n_resources=2000
    ).tenant
    second = generate_tenant(
        generator_profile, seed=42, n_subs=30, n_resources=2000
    ).tenant

    # Deterministic UUIDs (Pitfall 4) → identical tenant id and sub ids.
    assert first.tenant_id == second.tenant_id
    assert [s.subscription_id for s in first.subscriptions] == [
        s.subscription_id for s in second.subscriptions
    ]
    assert [s.display_name for s in first.subscriptions] == [
        s.display_name for s in second.subscriptions
    ]
    assert [s.archetype for s in first.subscriptions] == [
        s.archetype for s in second.subscriptions
    ]
    # Resource groups: full row-list equality (id, name, location, template).
    first_rgs = [
        (rg.id, rg.name, rg.location, rg.template_type)
        for rg in first.resource_groups
    ]
    second_rgs = [
        (rg.id, rg.name, rg.location, rg.template_type)
        for rg in second.resource_groups
    ]
    assert first_rgs == second_rgs


def test_different_seed_differs(generator_profile):
    """A different seed yields a different (but valid) tenant (D-02)."""
    a = generate_tenant(generator_profile, seed=42, n_subs=30, n_resources=2000).tenant
    b = generate_tenant(generator_profile, seed=7, n_subs=30, n_resources=2000).tenant
    assert a.tenant_id != b.tenant_id
    assert [s.display_name for s in a.subscriptions] != [
        s.display_name for s in b.subscriptions
    ]


# --------------------------------------------------------------------------- #
# Identity (Phase 10, IAM-01/IAM-02) — selectable via `pytest -k identity`.
# --------------------------------------------------------------------------- #


def _identity_rows(result):
    """Comparable (hashable-tuple) view of principals + role_assignments."""
    principals = [
        (p["oid"], p["principal_type"], p["display_name"], p["app_id"])
        for p in result.principals
    ]
    assignments = [
        (
            a["assignment_id"],
            a["subscription_id"],
            a["principal_oid"],
            a["principal_type"],
            a["role_definition_id"],
            a["scope"],
        )
        for a in result.role_assignments
    ]
    return principals, assignments


def test_identity_reproducible(generator_profile):
    """Same (profile, seed) → byte-identical principals + role_assignments across
    two generate_tenant runs (IAM-01/IAM-02 / D-01 / T-10-03)."""
    first = generate_tenant(
        generator_profile, seed=42, n_subs=30, n_resources=2000,
        over_privilege_rate=0.1,
    )
    second = generate_tenant(
        generator_profile, seed=42, n_subs=30, n_resources=2000,
        over_privilege_rate=0.1,
    )

    assert first.principals  # non-empty (identity on by default)
    assert first.role_assignments
    assert _identity_rows(first) == _identity_rows(second)
    assert first.over_privilege_count == second.over_privilege_count


def test_identity_disabled_is_baseline(generator_profile):
    """--no-identity is a clean no-op: empty principals/role_assignments AND the
    rest of the run is byte-identical to the same-seed identity-on tenant (Phase-8
    baseline guard — identity is the LAST post-pass, so disabling it draws no RNG
    before the tenant is built)."""
    on = generate_tenant(generator_profile, seed=42, n_subs=20, n_resources=2000)
    off = generate_tenant(
        generator_profile, seed=42, n_subs=20, n_resources=2000,
        inject_identity=False,
    )

    assert off.principals == ()
    assert off.role_assignments == ()
    assert off.over_privilege_count == 0
    # The tenant + every earlier post-pass is unaffected by the identity toggle.
    assert on.tenant.tenant_id == off.tenant.tenant_id
    assert [s.subscription_id for s in on.tenant.subscriptions] == [
        s.subscription_id for s in off.tenant.subscriptions
    ]
    assert on.violations == off.violations
    assert on.dependencies == off.dependencies
    assert on.cost_records == off.cost_records
