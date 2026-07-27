"""GEN-01/02/03 sampling assertions for the in-memory generator pipeline,
exercised against profiles/test-small.json (no DB).

- GEN-01: profile + targets produce a sampled tenant object with populated
  counts; targets default from source_stats when omitted (D-05).
- GEN-02: subscriptions split across archetypes in proportion to archetype
  weights, with Azure-shaped synthetic display names (never real identifiers).
- GEN-03: RG counts per subscription, template assignment, and location are
  sampled from the subscription's archetype.
"""

from __future__ import annotations

import copy
import inspect
import json
import re
import uuid
from collections import Counter
from dataclasses import FrozenInstanceError

import pytest

from tenantless.generator import archetypes, arm, naming, pipeline, sampling
from tenantless.generator.pipeline import (
    GenerationResult,
    ResourceGroup,
    Tenant,
    _confirm_and_rename,
    generate_tenant,
)
from tenantless.generator.resources import Resource

# Grammar: rg-{bu}-{env}-{token}-{nn}. bu/env are single lowercase words; the
# archetype token may contain hyphens (``web-app``), so the token segment is
# ``[a-z-]+``. ``_rg_token`` extracts the token verbatim from a name.
_RG_GRAMMAR = re.compile(r"^rg-[a-z]+-[a-z]+-[a-z-]+-\d{2}$")


def _rg_token(name: str) -> str:
    """Extract the workload token from an ``rg-{bu}-{env}-{token}-{nn}`` name.

    bu/env are single hyphen-free words, so after stripping the ``rg-`` prefix and
    the ``-NN`` suffix the remainder splits into ``bu``, ``env``, then the token
    (which may itself be hyphenated) as the rest.
    """
    core = re.sub(r"-\d{2}$", "", name[len("rg-") :])
    _bu, _env, token = core.split("-", 2)
    return token


# --------------------------------------------------------------------------- #
# PLAT-01 (D-01..D-04): GenerationResult shape, named access, immutability
# --------------------------------------------------------------------------- #


def test_generation_result_is_frozen_and_named(generator_profile):
    """generate_tenant returns a frozen+slotted GenerationResult (D-02/D-03):

    - the return value is a ``GenerationResult`` instance, not a 4-tuple;
    - the four fields are reachable by name (``.tenant`` / ``.violations`` /
      ``.dependencies`` / ``.clamp_notes``);
    - the collection fields are ``tuple`` (true content immutability, D-03);
    - rebinding an attribute raises ``FrozenInstanceError`` (D-02);
    - ``.tenant`` is the same populated Tenant graph as before.
    """
    result = generate_tenant(
        generator_profile, seed=42, n_subs=40, n_resources=3000
    )

    assert isinstance(result, GenerationResult)

    # Named access — every field reachable by attribute (D-04).
    assert result.tenant is not None
    assert result.violations is not None
    assert result.dependencies is not None
    assert result.clamp_notes is not None

    # Collection fields are tuples, not lists (D-03 — content immutability).
    assert isinstance(result.violations, tuple)
    assert isinstance(result.dependencies, tuple)
    assert isinstance(result.clamp_notes, tuple)

    # The tenant graph is intact (same shape the 4-tuple used to carry).
    assert result.tenant.tenant_id is not None
    assert len(result.tenant.subscriptions) == 40
    assert len(result.tenant.resource_groups) > 0

    # Frozen: rebinding any attribute raises (D-02).
    with pytest.raises(FrozenInstanceError):
        result.tenant = None


# --------------------------------------------------------------------------- #
# GEN-01: profile + targets → sampled tenant object
# --------------------------------------------------------------------------- #


def test_generates_from_profile(generator_profile):
    """A tenant object is produced with populated subscription + RG counts."""
    result = generate_tenant(
        generator_profile, seed=42, n_subs=40, n_resources=3000
    )
    tenant = result.tenant
    assert tenant.tenant_id is not None
    assert len(tenant.subscriptions) == 40
    assert len(tenant.resource_groups) > 0
    # Every RG belongs to a generated subscription.
    sub_ids = {s.subscription_id for s in tenant.subscriptions}
    assert all(rg.subscription_id in sub_ids for rg in tenant.resource_groups)
    # Each subscription has at least one RG (rg_count floored at 1).
    rgs_per_sub = Counter(rg.subscription_id for rg in tenant.resource_groups)
    assert all(rgs_per_sub[s.subscription_id] >= 1 for s in tenant.subscriptions)


def test_targets_default_from_source_stats(generator_profile):
    """Omitting targets defaults from source_stats (D-05): test-small → 50 subs."""
    result = generate_tenant(generator_profile, seed=42)
    tenant = result.tenant
    assert len(tenant.subscriptions) == (
        generator_profile["source_stats"]["total_subscriptions"]
    )


# --------------------------------------------------------------------------- #
# GEN-02: subscriptions per archetype weights; synthetic names
# --------------------------------------------------------------------------- #


def test_subscription_archetype_proportions(generator_profile):
    """Subscriptions split across archetypes roughly in proportion to weights."""
    n_subs = 400
    result = generate_tenant(
        generator_profile, seed=42, n_subs=n_subs, n_resources=10000
    )
    tenant = result.tenant
    counts = Counter(s.archetype for s in tenant.subscriptions)

    archetypes = generator_profile["subscription_archetypes"]
    total_weight = sum(a["weight"] for a in archetypes)
    # Every archetype id used is a real archetype id from the profile.
    valid_ids = {a["id"] for a in archetypes}
    assert set(counts) <= valid_ids
    # The dominant-weight archetype is the most-sampled one.
    dominant = max(archetypes, key=lambda a: a["weight"])["id"]
    assert counts.most_common(1)[0][0] == dominant
    # Each archetype's share is within a loose band of its normalized weight.
    for a in archetypes:
        expected = a["weight"] / total_weight
        observed = counts.get(a["id"], 0) / n_subs
        assert abs(observed - expected) < 0.12


def test_subscription_names_are_synthetic(generator_profile):
    """Display names are Azure-shaped synthetic strings, never real identifiers
    and never a profile archetype id echoed verbatim (D-11)."""
    result = generate_tenant(
        generator_profile, seed=42, n_subs=40, n_resources=3000
    )
    tenant = result.tenant
    archetype_ids = {a["id"] for a in generator_profile["subscription_archetypes"]}
    for s in tenant.subscriptions:
        assert s.display_name
        assert s.display_name not in archetype_ids
        # No profile sentinel ever leaks into a name.
        assert "__other__" not in s.display_name
        assert "__misc__" not in s.display_name


# --------------------------------------------------------------------------- #
# GEN-03: RG counts / templates / locations sampled per archetype
# --------------------------------------------------------------------------- #


def test_rg_counts_and_templates(generator_profile):
    """RG template assignment + locations are drawn from valid pools, and RG
    counts per sub stay within the archetype's [min, max] band."""
    result = generate_tenant(
        generator_profile, seed=42, n_subs=50, n_resources=5000
    )
    tenant = result.tenant
    valid_templates = {t["id"] for t in generator_profile["resource_group_templates"]}
    for rg in tenant.resource_groups:
        assert rg.template_type in valid_templates
        assert rg.location
        # Locations are never the aggregation sentinel.
        assert rg.location != "__other__"
        # RG ARM id is the canonical path.
        assert rg.id.startswith("/subscriptions/")
        assert "/resourceGroups/" in rg.id

    # RG count per sub respects the archetype band (min..max) for each sub.
    by_arch = {a["id"]: a for a in generator_profile["subscription_archetypes"]}
    rgs_per_sub = Counter(rg.subscription_id for rg in tenant.resource_groups)
    for s in tenant.subscriptions:
        band = by_arch[s.archetype]["resource_group_count"]
        n = rgs_per_sub[s.subscription_id]
        assert band["min"] <= n <= band["max"]


# --------------------------------------------------------------------------- #
# Regression (260622-dcq): RG-id uniqueness at high RGs/sub (PK safety)
# --------------------------------------------------------------------------- #


def test_resource_group_ids_are_unique_at_scale(generator_profile, monkeypatch):
    """Given many RGs per subscription, When the generator mints RG names,
    Then every resource-group id is unique (no duplicate primary key → no
    aborted binary COPY).

    D-12 update: the workload token is now the archetype label, NOT a random
    ``_WORKLOADS`` draw, so monkeypatching ``_WORKLOADS`` is a no-op for RG
    names. To make a within-sub collision certain DETERMINISTICALLY (so the
    re-mint loop is exercised) we collapse ``_BUSINESS_UNITS``/``_ENVIRONMENTS``
    to a single value each: the name space per token is then only the 1..99
    suffix. Forcing 80 RGs/sub (80 < 99 → the fixed re-mint always finds a free
    suffix, never spins) drives repeated (bu, env, token, suffix) draws → the
    re-mint fires. We keep the REAL ``naming.resource_group_name`` and a pinned
    seed.
    """
    monkeypatch.setattr(naming, "_BUSINESS_UNITS", ("a",))
    monkeypatch.setattr(naming, "_ENVIRONMENTS", ("b",))
    # Match the real arity of sample_rg_count(ctx, archetype) → 80 RGs per sub.
    monkeypatch.setattr(sampling, "sample_rg_count", lambda *a, **k: 80)

    result = generate_tenant(
        generator_profile, seed=42, n_subs=5, n_resources=5000
    )

    ids = [rg.id for rg in result.tenant.resource_groups]
    assert len(ids) == len(set(ids))  # zero duplicate RG ids (PK uniqueness)

    # Every name still matches the new rg-{bu}-{env}-{token}-{nn} grammar.
    names = [rg.name for rg in result.tenant.resource_groups]
    for name in names:
        assert _RG_GRAMMAR.match(name), name

    # Same RG NAME across DIFFERENT subscriptions is legal — uniqueness is
    # per-id (which embeds subscription_id), not per-name. With 80 RGs/sub over a
    # 1*1*ntokens*99 name space across 5 subs, the same name recurs; that (and the
    # re-mint re-drawing suffixes) means fewer distinct names than total RGs. This
    # must NOT be deduped away.
    assert len(set(names)) < len(names)


def test_rg_name_matches_template_archetype(generator_profile):
    """Every RG's workload-token segment equals the archetype label of its
    template's measured ``type_set`` (via build_label_map) — the RG name is a
    label of its contents, not a random word. At least one known-archetype
    template (e.g. ``web-app``) surfaces its token verbatim in a name."""
    label_map = archetypes.build_label_map(
        generator_profile["resource_group_templates"]
    )
    result = generate_tenant(
        generator_profile, seed=7, n_subs=40, n_resources=8000
    )
    rgs = result.tenant.resource_groups
    assert rgs

    for rg in rgs:
        assert _RG_GRAMMAR.match(rg.name), rg.name
        assert _rg_token(rg.name) == label_map[rg.template_type], rg.name

    # A concrete known-archetype token appears verbatim (the test profile's
    # ``web-app`` template → ``web-app`` token). Guards against a silent
    # generic-only fallback.
    web_rgs = [
        rg
        for rg in rgs
        if label_map[rg.template_type] == "web-app"
    ]
    assert web_rgs, "expected at least one web-app RG in a 40-sub tenant"
    assert all("-web-app-" in rg.name for rg in web_rgs)


def test_bu_env_vary_under_fixed_template(generator_profile):
    """RGs sharing one template id (one archetype token) still show varied
    bu/env segments — bu/env remain independent random draws."""
    label_map = archetypes.build_label_map(
        generator_profile["resource_group_templates"]
    )
    result = generate_tenant(
        generator_profile, seed=11, n_subs=40, n_resources=8000
    )
    # Group RG names by their (single) template id, keep the most common one.
    by_template: dict[str, list[str]] = {}
    for rg in result.tenant.resource_groups:
        by_template.setdefault(rg.template_type, []).append(rg.name)
    tid, names = max(by_template.items(), key=lambda kv: len(kv[1]))
    token = label_map[tid]

    bu_env = set()
    for name in names:
        core = re.sub(r"-\d{2}$", "", name[len("rg-") :])
        bu, env, name_token = core.split("-", 2)
        assert name_token == token  # all share the one template's token
        bu_env.add((bu, env))
    assert len(bu_env) > 1, bu_env


# --------------------------------------------------------------------------- #
# ARCH-GAP-01 (Plan 19-05, D-14/D-16/D-17): the deterministic post-materialization
# _confirm_and_rename pass. DB-free — small tenants are hand-built so the two-phase
# structural-rename + subscription-wide reference-sweep can be exercised directly.
# --------------------------------------------------------------------------- #

# A fixed subscription id keeps hand-built ids byte-stable across builds (used by
# the purity test that builds two independent identical tenants).
_FIXED_SUB = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


def _res(sub_id, rg_name, type_key, name, props=None):
    """A minimal in-memory Resource with a canonical ARM id for the given RG."""
    return Resource(
        id=arm.resource_id(sub_id, rg_name, type_key, name),
        subscription_id=sub_id,
        resource_group_name=rg_name,
        name=name,
        type=arm.canonical_type(type_key),
        location="eastus",
        api_version="2023-01-01",
        properties=props if props is not None else {},
    )


def _rg(sub_id, name, template_type, resources_list):
    rg = ResourceGroup(
        id=arm.rg_id(sub_id, name),
        subscription_id=sub_id,
        name=name,
        location="eastus",
        template_type=template_type,
    )
    rg.resources = resources_list
    return rg


def _tenant(rgs):
    t = Tenant(
        tenant_id=uuid.uuid4(),
        display_name="x-tenant",
        profile_version="1.0",
        scale_params={},
    )
    t.resource_groups = rgs
    return t


def test_confirm_empty_rg_generic():
    """D-17: an empty RG whose template token is semantic downgrades to a generic
    token (specifically ``shared`` — genuinely nothing materialized)."""
    sub = uuid.uuid4()
    rg = _rg(sub, "rg-fin-prod-web-app-01", "t-web", [])
    tenant = _tenant([rg])
    _confirm_and_rename(tenant, {"t-web": "web-app"}, [], [])
    assert _rg_token(rg.name) == archetypes.TOKEN_SHARED


def test_confirm_downgrade_not_relabel():
    """D-14: a web-app-labelled RG whose materialized contents are monitoring-only
    (no web-app anchor/strong-signal) downgrades to a GENERIC token — NEVER to a
    different semantic token (the rejected remedy B)."""
    sub = uuid.uuid4()
    r1 = _res(sub, "rg-fin-prod-web-app-01",
              "Microsoft.OperationalInsights/workspaces", "law1")
    r2 = _res(sub, "rg-fin-prod-web-app-01",
              "Microsoft.Insights/metricAlerts", "alert1")
    rg = _rg(sub, "rg-fin-prod-web-app-01", "t-web", [r1, r2])
    tenant = _tenant([rg])
    _confirm_and_rename(tenant, {"t-web": "web-app"}, [], [])
    token = _rg_token(rg.name)
    assert token in (archetypes.TOKEN_SHARED, archetypes.TOKEN_CORE)
    assert token == archetypes.TOKEN_CORE  # named-but-unbacked -> core (D-09)
    assert token != "monitoring"  # never relabel to the *materialized* archetype


def test_confirm_keep_coherent():
    """A template whose materialized instance carries its anchor keeps its token."""
    sub = uuid.uuid4()
    r = _res(sub, "rg-fin-prod-web-app-01", "Microsoft.Web/sites", "site1")
    rg = _rg(sub, "rg-fin-prod-web-app-01", "t-web", [r])
    tenant = _tenant([rg])
    _confirm_and_rename(tenant, {"t-web": "web-app"}, [], [])
    assert rg.name == "rg-fin-prod-web-app-01"  # unchanged
    assert rg.id.endswith("/resourceGroups/rg-fin-prod-web-app-01")


def test_confirm_own_rg_referential_integrity():
    """After a downgrade rename, every child id + resource_group_name and the
    rg.id itself point at the NEW name (no dangling own-RG id)."""
    sub = uuid.uuid4()
    r1 = _res(sub, "rg-fin-prod-web-app-01",
              "Microsoft.OperationalInsights/workspaces", "law1")
    r2 = _res(sub, "rg-fin-prod-web-app-01",
              "Microsoft.Insights/metricAlerts", "alert1")
    rg = _rg(sub, "rg-fin-prod-web-app-01", "t-web", [r1, r2])
    tenant = _tenant([rg])
    _confirm_and_rename(tenant, {"t-web": "web-app"}, [], [])
    assert rg.name != "rg-fin-prod-web-app-01"  # downgraded
    assert rg.id.endswith(f"/resourceGroups/{rg.name}")
    for r in rg.resources:
        assert f"/resourceGroups/{rg.name}/" in r.id
        assert r.resource_group_name == rg.name


def test_confirm_cross_rg_property_refs():
    """BLOCKER 1: a resource in RG A embeds an id living in RG B (subscription-wide
    subnet/pip pool). Renaming B must rewrite that reference even though it lives
    OUTSIDE B — the sweep is subscription-wide, not own-RG-only."""
    sub = uuid.uuid4()
    # RG B: web-app label, monitoring content -> downgrades (renamed).
    b_res = _res(sub, "rg-fin-prod-web-app-02",
                 "Microsoft.OperationalInsights/workspaces", "law")
    rg_b = _rg(sub, "rg-fin-prod-web-app-02", "t-web", [b_res])
    b_old_seg = "/resourceGroups/rg-fin-prod-web-app-02/"
    # RG A: a NIC referencing b_res.id in its properties; a VM anchors A so A stays.
    a_nic = _res(
        sub, "rg-eng-dev-vm-workload-01",
        "Microsoft.Network/networkInterfaces", "nic",
        props={"ipConfigurations": [{"properties": {"subnet": {"id": b_res.id}}}]},
    )
    a_vm = _res(sub, "rg-eng-dev-vm-workload-01",
                "Microsoft.Compute/virtualMachines", "vm")
    rg_a = _rg(sub, "rg-eng-dev-vm-workload-01", "t-vm", [a_nic, a_vm])
    tenant = _tenant([rg_a, rg_b])
    _confirm_and_rename(
        tenant, {"t-web": "web-app", "t-vm": "vm-workload"}, [], []
    )
    assert rg_b.name != "rg-fin-prod-web-app-02"  # B renamed
    assert rg_a.name == "rg-eng-dev-vm-workload-01"  # A confirmed (kept)
    # (a) NO resource in ANY RG retains the stale segment.
    for rg in tenant.resource_groups:
        for r in rg.resources:
            assert b_old_seg not in json.dumps(r.properties)
    # (b) A's subnet ref now resolves to B's NEW id.
    live_ids = {r.id for rg in tenant.resource_groups for r in rg.resources}
    ref = a_nic.properties["ipConfigurations"][0]["properties"]["subnet"]["id"]
    assert ref in live_ids
    assert f"/resourceGroups/{rg_b.name}/" in ref


def test_confirm_violation_detail_remap():
    """BLOCKER 2: a VM_PUBLIC_IP-style violation row's nested detail (nic_id /
    public_ip_id — full ARM ids served verbatim as JSONB) is remapped to the new
    RG segment; no stale segment survives anywhere in the detail payload."""
    sub = uuid.uuid4()
    nic = _res(sub, "rg-fin-prod-web-app-01",
               "Microsoft.Network/networkInterfaces", "nic")
    pip = _res(sub, "rg-fin-prod-web-app-01",
               "Microsoft.Network/publicIPAddresses", "pip")
    rg = _rg(sub, "rg-fin-prod-web-app-01", "t-web", [nic, pip])
    tenant = _tenant([rg])
    old_seg = "/resourceGroups/rg-fin-prod-web-app-01/"
    vrows = [{
        "resource_id": nic.id,
        "violation_type": "VM_PUBLIC_IP",
        "severity": "high",
        "detail": {"nic_id": nic.id, "public_ip_id": pip.id},
    }]
    _confirm_and_rename(tenant, {"t-web": "web-app"}, vrows, [])
    assert rg.name != "rg-fin-prod-web-app-01"  # network-only -> web-app unbacked
    new_seg = f"/resourceGroups/{rg.name}/"
    detail = vrows[0]["detail"]
    assert new_seg in detail["nic_id"] and old_seg not in detail["nic_id"]
    assert new_seg in detail["public_ip_id"] and old_seg not in detail["public_ip_id"]
    live_ids = {r.id for rg in tenant.resource_groups for r in rg.resources}
    assert vrows[0]["resource_id"] in live_ids
    assert detail["nic_id"] in live_ids
    assert detail["public_ip_id"] in live_ids


def test_confirm_managed_by_remap():
    """T-19-13: ``managed_by`` is a SEPARATE ``Resource`` field — it is not part of
    ``id`` and not inside ``properties`` — so NEITHER the Phase-A structural rename
    NOR the Phase-B property sweep reaches it, and ``id_remap`` was previously
    applied only to violation/dependency rows. A subnet minted with
    ``managed_by=vnet.id`` (``resources.py`` ``_materialize_subnets``) must still
    point at its VNet after the owning RG downgrades.

    Blast radius if it dangles: ``managed_by`` is persisted and feeds drift's
    protected-reference set, so a stale value leaves the real VNet unprotected
    against deletion while its subnets survive.
    """
    sub = uuid.uuid4()
    vnet = _res(sub, "rg-fin-prod-web-app-01",
                "Microsoft.Network/virtualNetworks", "vnet0")
    subnet = _res(sub, "rg-fin-prod-web-app-01",
                  "Microsoft.Network/virtualNetworks/subnets", "subnet-00")
    subnet.managed_by = vnet.id  # the parent link minted at materialization
    rg = _rg(sub, "rg-fin-prod-web-app-01", "t-web", [vnet, subnet])
    tenant = _tenant([rg])
    old_seg = "/resourceGroups/rg-fin-prod-web-app-01/"
    _confirm_and_rename(tenant, {"t-web": "web-app"}, [], [])
    assert rg.name != "rg-fin-prod-web-app-01"  # network-only -> web-app unbacked
    live_ids = {r.id for r in rg.resources}
    assert old_seg not in subnet.managed_by  # no vacated segment survives
    assert subnet.managed_by == vnet.id  # tracks the VNet's NEW id
    assert subnet.managed_by in live_ids  # and that id actually exists


def test_confirm_duplicate_rg_name_across_subscriptions():
    """T-19-14: RG names are unique only WITHIN a subscription, so a rename map keyed
    on the NAME ALONE and applied tenant-wide rewrites references belonging to a
    DIFFERENT subscription that merely shares the name.

    Sub A downgrades and is renamed; sub B carries the anchor and keeps its name.
    B's own self-reference must come out byte-identical — a rewrite there would point
    at an RG that does not exist in B.
    """
    sub_a, sub_b = uuid.uuid4(), uuid.uuid4()
    shared = "rg-fin-prod-web-app-01"  # SAME name in both subs (legal per-sub)
    # Sub A: monitoring-only content under a web-app token -> downgrades, renamed.
    a_res = _res(sub_a, shared, "Microsoft.OperationalInsights/workspaces", "law")
    rg_a = _rg(sub_a, shared, "t-web", [a_res])
    # Sub B: carries the web-app anchor -> confirms, must be left alone.
    b_site = _res(sub_b, shared, "Microsoft.Web/sites", "site")
    b_vnet = _res(sub_b, shared, "Microsoft.Network/virtualNetworks", "vnet0")
    b_nic = _res(
        sub_b, shared, "Microsoft.Network/networkInterfaces", "nic",
        props={"ipConfigurations": [{"properties": {"subnet": {"id": b_vnet.id}}}]},
    )
    rg_b = _rg(sub_b, shared, "t-web", [b_site, b_vnet, b_nic])
    tenant = _tenant([rg_a, rg_b])
    ref_before = b_nic.properties["ipConfigurations"][0]["properties"]["subnet"]["id"]
    _confirm_and_rename(tenant, {"t-web": "web-app"}, [], [])
    assert rg_a.name != shared  # A downgraded and renamed
    assert rg_b.name == shared  # B confirmed — name untouched
    ref_after = b_nic.properties["ipConfigurations"][0]["properties"]["subnet"]["id"]
    assert ref_after == ref_before  # B's reference NOT rewritten by A's rename
    # And every id in sub B still resolves inside sub B.
    b_ids = {r.id for r in rg_b.resources}
    assert ref_after in b_ids
    assert f"/resourceGroups/{shared}/" in ref_after


def test_confirm_no_collision():
    """BLOCKER 3: several semantic RGs converge onto the SAME generic token per
    bu/env (plus a pre-existing generic RG occupying a target nn). The taken-name
    set — seeded from EVERY existing name — forces distinct suffixes; zero
    duplicate rg.name / rg.id per subscription and the grammar/length hold."""
    sub = uuid.uuid4()
    rgs = []
    for nn in ("01", "02", "03"):
        r = _res(sub, f"rg-fin-prod-web-app-{nn}",
                 "Microsoft.OperationalInsights/workspaces", f"law{nn}")
        rgs.append(_rg(sub, f"rg-fin-prod-web-app-{nn}", "t-web", [r]))
    # A pre-existing generic RG that already occupies the first downgrade target.
    r0 = _res(sub, "rg-fin-prod-core-01",
              "Microsoft.OperationalInsights/workspaces", "law0")
    rgs.append(_rg(sub, "rg-fin-prod-core-01", "t-core", [r0]))
    tenant = _tenant(rgs)
    _confirm_and_rename(
        tenant, {"t-web": "web-app", "t-core": archetypes.TOKEN_CORE}, [], []
    )
    names = [rg.name for rg in tenant.resource_groups]
    ids = [rg.id for rg in tenant.resource_groups]
    assert len(names) == len(set(names)), names  # no duplicate rg.name
    assert len(ids) == len(set(ids)), ids  # no duplicate rg.id (== PK safety)
    for rg in tenant.resource_groups:
        assert _RG_GRAMMAR.match(rg.name), rg.name
        assert len(rg.name) <= 90


def test_confirm_rename_jobs_determinism(generator_profile):
    """Determinism: the rename pass keeps generate_tenant jobs=1 byte-identical to
    jobs=2 over RG names+ids and the violation/dependency id rows."""
    kw = dict(seed=42, n_subs=24, n_resources=4000)
    a = generate_tenant(generator_profile, jobs=1, **kw)
    b = generate_tenant(generator_profile, jobs=2, **kw)
    assert [(rg.name, rg.id) for rg in a.tenant.resource_groups] == \
           [(rg.name, rg.id) for rg in b.tenant.resource_groups]
    assert [v["resource_id"] for v in a.violations] == \
           [v["resource_id"] for v in b.violations]
    assert [d["source_resource_id"] for d in a.dependencies] == \
           [d["source_resource_id"] for d in b.dependencies]


def test_confirm_pass_is_rng_free_source():
    """The pass (and its helpers) draw NO RNG — grep-verifiable absence of any
    SeededContext draw in the source of the new functions."""
    banned = ("rng", "integers", "choice", "bernoulli", "SeededContext")
    for fn in (
        pipeline._confirm_and_rename,
        pipeline._rename_rg,
        pipeline._rewrite_refs,
        pipeline._next_free,
    ):
        src = inspect.getsource(fn)
        for tok in banned:
            assert tok not in src, (fn.__name__, tok)


def test_confirm_pass_is_pure_deterministic():
    """Two independent, byte-identical tenants (fixed sub id) produce identical
    renamed names+ids — the pass is a pure function of its inputs, no RNG."""
    def build():
        rgs = []
        for nn in ("01", "02", "03"):
            r = _res(_FIXED_SUB, f"rg-fin-prod-web-app-{nn}",
                     "Microsoft.OperationalInsights/workspaces", f"law{nn}")
            rgs.append(_rg(_FIXED_SUB, f"rg-fin-prod-web-app-{nn}", "t-web", [r]))
        r0 = _res(_FIXED_SUB, "rg-fin-prod-core-01",
                  "Microsoft.OperationalInsights/workspaces", "law0")
        rgs.append(_rg(_FIXED_SUB, "rg-fin-prod-core-01", "t-core", [r0]))
        return _tenant(rgs), {"t-web": "web-app", "t-core": archetypes.TOKEN_CORE}

    t1, lm1 = build()
    t2, lm2 = build()
    _confirm_and_rename(t1, lm1, [], [])
    _confirm_and_rename(t2, lm2, [], [])
    assert [rg.name for rg in t1.resource_groups] == \
           [rg.name for rg in t2.resource_groups]
    assert [rg.id for rg in t1.resource_groups] == \
           [rg.id for rg in t2.resource_groups]


def test_confirm_returns_metrics():
    """D-18: the pass returns the confirmed / downgraded_to_generic /
    child_credit_confirmed (+ already_generic) tally."""
    sub = uuid.uuid4()
    keep = _res(sub, "rg-fin-prod-web-app-01", "Microsoft.Web/sites", "site")
    drop = _res(sub, "rg-fin-prod-web-app-02",
                "Microsoft.OperationalInsights/workspaces", "law")
    generic = _res(sub, "rg-fin-prod-core-01",
                   "Microsoft.OperationalInsights/workspaces", "law2")
    tenant = _tenant([
        _rg(sub, "rg-fin-prod-web-app-01", "t-web", [keep]),
        _rg(sub, "rg-fin-prod-web-app-02", "t-web", [drop]),
        _rg(sub, "rg-fin-prod-core-01", "t-core", [generic]),
    ])
    metrics = _confirm_and_rename(
        tenant, {"t-web": "web-app", "t-core": archetypes.TOKEN_CORE}, [], []
    )
    assert metrics["confirmed"] == 1
    assert metrics["downgraded_to_generic"] == 1
    assert metrics["already_generic"] == 1
    assert metrics["child_credit_confirmed"] == 0


# --------------------------------------------------------------------------- #
# ARCH-GAP-02 remedy 7 (Plan 19-08): D-15 child crediting, exercised DECISIVELY
# end-to-end. The live seed-7 tenant reported child_credit_confirmed=0, i.e. D-15
# shipped unproven — whether the metric ever fires must not depend on whether a
# given seed happens to materialize a bare nested child. DB-free.
# --------------------------------------------------------------------------- #


def test_child_credit_decisive_increments_metric():
    """A VM/runCommands-only RG confirms ONLY via its credited parent anchor.

    ``virtualMachines/runCommands`` is not itself a catalog signal, so the raw set
    carries zero evidence; crediting ``Microsoft.Compute/virtualMachines`` is the
    sole path to confirmation — the definition of decisive (D-15/D-18).
    """
    sub = uuid.uuid4()
    r = _res(sub, "rg-fin-prod-vm-workload-01",
             "Microsoft.Compute/virtualMachines/runCommands", "rc1")
    rg = _rg(sub, "rg-fin-prod-vm-workload-01", "t-vm", [r])
    tenant = _tenant([rg])

    # The premise: WITHOUT crediting, the raw set proves nothing at all.
    raw = archetypes._norm({r.type})
    entry = archetypes._BY_TOKEN["vm-workload"]
    assert archetypes._confirms(entry, raw) is False

    metrics = _confirm_and_rename(tenant, {"t-vm": "vm-workload"}, [], [])
    assert metrics["confirmed"] >= 1
    assert metrics["child_credit_confirmed"] >= 1
    assert metrics["downgraded_to_generic"] == 0
    assert "-vm-workload-" in rg.name  # kept its semantic token — not renamed


def test_anchor_present_child_credit_not_decisive():
    """The contrast: when the RAW set already carries the anchor, crediting adds
    nothing, so ``child_credit_confirmed`` must NOT increment — pinning the D-18
    semantics so a future rule change cannot silently inflate the metric.
    """
    sub = uuid.uuid4()
    srv = _res(sub, "rg-fin-prod-sql-db-01", "Microsoft.Sql/servers", "srv1")
    db = _res(sub, "rg-fin-prod-sql-db-01", "Microsoft.Sql/servers/databases", "db1")
    rg = _rg(sub, "rg-fin-prod-sql-db-01", "t-sql", [srv, db])
    tenant = _tenant([rg])
    metrics = _confirm_and_rename(tenant, {"t-sql": "sql-db"}, [], [])
    assert metrics["confirmed"] >= 1
    assert metrics["child_credit_confirmed"] == 0
    assert "-sql-db-" in rg.name
