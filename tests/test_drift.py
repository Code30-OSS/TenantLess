"""Drift mutation engine core tests (Plan 11-03, DRIFT-01/02).

DB-free unit tests over the seeded chaos catalogue in
``tenantless.generator.drift`` — the conceptual 4th inject twin of
``violations.py``. Every mutator must overwrite the EXACT served JSONB key
(`tags`/`sku`/`kind`/`properties`) the ARM server returns, so drift is
ARM-visible (DRIFT-03). Codes live in a FRESH ``DRIFT_*`` namespace that never
collides with ``VIOLATION_REGISTRY`` (RESEARCH Open Q1).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from tenantless.generator import drift, resources, violations
from tenantless.generator.rng import SeededContext


# --------------------------------------------------------------------------- #
# Helpers — build a minimal in-memory Resource of a given type (DB-free).
# --------------------------------------------------------------------------- #


def _mk(type_key: str, *, rid: str | None = None, props=None, tags=None):
    return resources.Resource(
        id=rid or f"/subscriptions/s/resourceGroups/rg/providers/{type_key}/n",
        subscription_id=uuid.uuid4(),
        resource_group_name="rg",
        name="n",
        type=type_key,
        location="eastus",
        api_version="2023-01-01",
        tags=dict(tags or {}),
        properties=dict(props or {}),
    )


def _ctx():
    return SeededContext(42)


# code -> (eligible_type, expected field_path, post-mutation assertion on r)
_CHAOS_CASES = {
    "DRIFT_STORAGE_PUBLIC_ACCESS": (
        resources.T_STORAGE,
        "properties.allowBlobPublicAccess",
        lambda r: r.properties["allowBlobPublicAccess"] is True,
    ),
    "DRIFT_STORAGE_HTTP_ALLOWED": (
        resources.T_STORAGE,
        "properties.supportsHttpsTrafficOnly",
        lambda r: r.properties["supportsHttpsTrafficOnly"] is False,
    ),
    "DRIFT_STORAGE_OLD_TLS": (
        resources.T_STORAGE,
        "properties.minimumTlsVersion",
        lambda r: r.properties["minimumTlsVersion"] == "TLS1_0",
    ),
    "DRIFT_KV_NO_PURGE_PROTECT": (
        resources.T_KV,
        "properties.enablePurgeProtection",
        lambda r: r.properties["enablePurgeProtection"] is False,
    ),
    "DRIFT_KV_NO_SOFT_DELETE": (
        resources.T_KV,
        "properties.enableSoftDelete",
        lambda r: r.properties["enableSoftDelete"] is False,
    ),
    "DRIFT_AKS_NO_RBAC": (
        resources.T_AKS,
        "properties.enableRBAC",
        lambda r: r.properties["enableRBAC"] is False,
    ),
}


def test_mutation_catalogue():
    """Each chaos mutator overwrites its mapped served key and returns a
    {field_path, before, after} delta whose field_path matches the served map."""
    ctx = _ctx()
    for code, (type_key, field_path, check) in _CHAOS_CASES.items():
        spec = drift.DRIFT_REGISTRY[code]
        assert spec.eligible_type == type_key
        r = _mk(type_key)
        delta = spec.mutate(ctx, r)
        assert set(delta.keys()) == {"field_path", "before", "after"}
        assert delta["field_path"] == field_path
        assert check(r), f"{code} did not overwrite the served key"

    # NSG appends an open inbound rule to the served securityRules[] array.
    nsg_spec = drift.DRIFT_REGISTRY["DRIFT_NSG_OPEN_INBOUND"]
    assert nsg_spec.eligible_type == resources.T_NSG
    nsg = _mk(
        resources.T_NSG,
        props={
            "securityRules": [
                {"name": "rule-00", "properties": {"priority": 100}},
                {"name": "rule-01", "properties": {"priority": 110}},
            ]
        },
    )
    before_len = len(nsg.properties["securityRules"])
    delta = nsg_spec.mutate(ctx, nsg)
    assert set(delta.keys()) == {"field_path", "before", "after"}
    assert delta["field_path"] == "properties.securityRules[]"
    rules = nsg.properties["securityRules"]
    assert len(rules) == before_len + 1
    added = rules[-1]
    assert added["properties"]["access"] == "Allow"
    assert added["properties"]["direction"] == "Inbound"
    # priority is max existing (110) + 10.
    assert added["properties"]["priority"] == 120
    # ARM NSG schema requires exactly one of sourcePortRange/sourcePortRanges on
    # every security rule; without it a strict scanner rejects the drifted NSG and
    # the chaos drift is invisible (WR-02, DRIFT-03).
    assert added["properties"]["sourcePortRange"] == "*"


def test_chaos_codes_are_fresh_namespace():
    """Every DRIFT_REGISTRY key uses the DRIFT_ namespace and none reuses a
    VIOLATION_REGISTRY code (no collision with the violations engine).

    Plan 11-04 adds temporal codes to the same registry, so the per-spec assertion
    relaxes from ``== "chaos"`` to "a known drift_type"; the chaos floor (>= 8) and
    the namespace/collision guarantees still hold for every entry."""
    assert len(drift.DRIFT_REGISTRY) >= 8
    viol_codes = set(violations.VIOLATION_REGISTRY)
    chaos = [c for c, s in drift.DRIFT_REGISTRY.items() if s.drift_type == "chaos"]
    assert len(chaos) >= 8
    for code, spec in drift.DRIFT_REGISTRY.items():
        assert code.startswith("DRIFT_"), code
        assert code not in viol_codes, f"{code} collides with a violation code"
        assert spec.drift_type in ("chaos", "temporal")


def test_eligible_population_sorted():
    """The sampler returns candidates filtered to the spec's eligible_type
    (and predicate) and sorted by id before any draw (Pitfall 3)."""
    all_res = [
        _mk(resources.T_STORAGE, rid="/r/zzz"),
        _mk(resources.T_KV, rid="/r/kkk"),
        _mk(resources.T_STORAGE, rid="/r/aaa"),
        _mk(resources.T_STORAGE, rid="/r/mmm"),
    ]
    code = "DRIFT_STORAGE_PUBLIC_ACCESS"
    spec = drift.DRIFT_REGISTRY[code]
    pop = drift._eligible_population(all_res, code, spec)
    assert [r.id for r in pop] == ["/r/aaa", "/r/mmm", "/r/zzz"]
    assert all(r.type == resources.T_STORAGE for r in pop)

    # eligible_predicate: tag-removal only targets resources that HAVE the key.
    tagged = [
        _mk(resources.T_STORAGE, rid="/r/b", tags={"environment": "prod"}),
        _mk(resources.T_KV, rid="/r/a"),  # no tag -> excluded
        _mk(resources.T_KV, rid="/r/c", tags={"environment": "dev"}),
    ]
    tcode = "DRIFT_TAGS_REMOVED"
    tspec = drift.DRIFT_REGISTRY[tcode]
    tpop = drift._eligible_population(tagged, tcode, tspec)
    assert [r.id for r in tpop] == ["/r/b", "/r/c"]


def test_tags_removed_chaos():
    """The tag-removal mutator deletes a present tag key and records
    before=old value, after=None (key absent)."""
    ctx = _ctx()
    spec = drift.DRIFT_REGISTRY["DRIFT_TAGS_REMOVED"]
    r = _mk(resources.T_STORAGE, tags={"environment": "prod", "owner": "x"})
    delta = spec.mutate(ctx, r)
    assert set(delta.keys()) == {"field_path", "before", "after"}
    assert delta["field_path"] == "tags.environment"
    assert delta["before"] == "prod"
    assert delta["after"] is None
    assert "environment" not in r.tags
    assert "owner" in r.tags  # other tags untouched


def test_intensity_clamp():
    """planned_count maps a fraction to round(I*n) and an int>1 to a count, then
    clamps to eligibility and emits a non-None note when clamped (D-14)."""
    ten = list(range(10))
    three = list(range(3))

    assert drift.planned_count(0.5, ten) == (5, None)
    assert drift.planned_count(0.0, ten) == (0, None)

    # exact integer count within eligibility -> no clamp, no note.
    count, note = drift.planned_count(2.0, ten)
    assert count == 2 and note is None

    # integer count exceeding eligibility -> clamp to n and a non-None note.
    count, note = drift.planned_count(5.0, three)
    assert count == 3
    assert note is not None and note != ""


# --------------------------------------------------------------------------- #
# Plan 11-04 — TEMPORAL catalogue (provisioningState in JSONB, sku, tags, TLS/policy)
# --------------------------------------------------------------------------- #


def test_temporal_provisioning_in_properties():
    """The provisioning mutator writes properties.provisioningState (the served
    JSONB) and NEVER the unserved provisioning column/attribute (Pitfall 1)."""
    ctx = _ctx()
    spec = drift.DRIFT_REGISTRY["DRIFT_PROVISIONING_STATE"]
    assert spec.drift_type == "temporal"
    r = _mk(resources.T_STORAGE, props={"provisioningState": "Succeeded"})
    column_before = r.provisioning_state  # the unserved attribute (must stay put)
    delta = spec.mutate(ctx, r)
    assert set(delta.keys()) == {"field_path", "before", "after"}
    assert delta["field_path"] == "properties.provisioningState"
    assert delta["before"] == "Succeeded"
    assert r.properties["provisioningState"] in ("Updating", "Failed")
    assert delta["after"] == r.properties["provisioningState"]
    # the unserved column attribute is never written by the mutator (Pitfall 1).
    assert r.provisioning_state == column_before


def test_temporal_sku_shift():
    """The sku mutator shifts sku.name/tier to an adjacent tier and records the
    full before/after sku object."""
    ctx = _ctx()
    spec = drift.DRIFT_REGISTRY["DRIFT_SKU_TIER_SHIFT"]
    assert spec.drift_type == "temporal"
    r = _mk(resources.T_STORAGE)
    r.sku = {"name": "Standard_LRS", "tier": "Standard"}
    delta = spec.mutate(ctx, r)
    assert set(delta.keys()) == {"field_path", "before", "after"}
    assert delta["field_path"] == "sku"
    assert delta["before"] == {"name": "Standard_LRS", "tier": "Standard"}
    # tier shifted to an adjacent ladder tier and the name's tier prefix tracked.
    assert r.sku["tier"] != "Standard"
    assert delta["after"]["tier"] == r.sku["tier"]
    assert delta["after"]["name"].split("_", 1)[0] == r.sku["tier"]
    assert delta["after"] != delta["before"]


def test_temporal_tag_churn():
    """The tag-churn mutator changes a present tag VALUE (not removed) and records
    before/after; other tags are untouched."""
    ctx = _ctx()
    spec = drift.DRIFT_REGISTRY["DRIFT_TAG_CHURN"]
    assert spec.drift_type == "temporal"
    r = _mk(resources.T_STORAGE, tags={"environment": "prod", "owner": "x"})
    delta = spec.mutate(ctx, r)
    assert set(delta.keys()) == {"field_path", "before", "after"}
    assert delta["field_path"] == "tags.environment"
    assert delta["before"] == "prod"
    assert delta["after"] != "prod"
    assert delta["after"] is not None  # churned (changed), never removed
    assert r.tags["environment"] == delta["after"]
    assert "owner" in r.tags  # other tags untouched


def test_temporal_tls_downgrade():
    """SQL/Web temporal mutators downgrade/disable the mapped served properties
    (minimalTlsVersion / publicNetworkAccess / transparentDataEncryption /
    httpsOnly / state)."""
    ctx = _ctx()
    cases = {
        "DRIFT_SQL_TLS_DOWNGRADE": (
            resources.T_SQLSRV,
            "properties.minimalTlsVersion",
            {"minimalTlsVersion": "1.2"},
        ),
        "DRIFT_SQL_PUBLIC_NETWORK": (
            resources.T_SQLSRV,
            "properties.publicNetworkAccess",
            {"publicNetworkAccess": "Disabled"},
        ),
        "DRIFT_SQLDB_TDE_DISABLED": (
            resources.T_SQLDB,
            "properties.transparentDataEncryption",
            {"transparentDataEncryption": "Enabled"},
        ),
        "DRIFT_WEBSITE_HTTPS_OFF": (
            resources.T_WEBSITE,
            "properties.httpsOnly",
            {"httpsOnly": True},
        ),
        "DRIFT_WEBSITE_STOPPED": (
            resources.T_WEBSITE,
            "properties.state",
            {"state": "Running"},
        ),
    }
    for code, (type_key, field_path, props) in cases.items():
        spec = drift.DRIFT_REGISTRY[code]
        assert spec.drift_type == "temporal", code
        assert spec.eligible_type == type_key, code
        r = _mk(type_key, props=dict(props))
        key = field_path.split(".", 1)[1]
        before_val = r.properties.get(key)
        delta = spec.mutate(ctx, r)
        assert set(delta.keys()) == {"field_path", "before", "after"}
        assert delta["field_path"] == field_path, code
        assert delta["before"] == before_val, code
        assert r.properties[key] == delta["after"], code
        assert delta["after"] != before_val, code


def test_temporal_codes_drift_type():
    """Every Plan-11-04 temporal code is registered with drift_type=="temporal"."""
    expected = {
        "DRIFT_PROVISIONING_STATE",
        "DRIFT_SKU_TIER_SHIFT",
        "DRIFT_TAG_CHURN",
        "DRIFT_SQL_TLS_DOWNGRADE",
        "DRIFT_SQL_PUBLIC_NETWORK",
        "DRIFT_SQLDB_TDE_DISABLED",
        "DRIFT_WEBSITE_HTTPS_OFF",
        "DRIFT_WEBSITE_STOPPED",
    }
    temporal = {
        c: s for c, s in drift.DRIFT_REGISTRY.items() if s.drift_type == "temporal"
    }
    assert expected <= set(temporal)
    for code, spec in temporal.items():
        assert code.startswith("DRIFT_"), code
        assert spec.drift_type == "temporal"


# --------------------------------------------------------------------------- #
# Plan 11-04 Task 2 — disappear eligibility (D-10) + safe appear mint (D-12)
# --------------------------------------------------------------------------- #


def _rg(name="rg-syn-001", *, sub=None, location="eastus", resources_list=None):
    return SimpleNamespace(
        subscription_id=sub or uuid.UUID(int=7),
        name=name,
        location=location,
        resources=list(resources_list or []),
    )


def test_disappear_eligibility():
    """disappear_eligible selects ONLY leaf resources with no role-assignment
    scope, no dependency, no violation, no child id, and no managed_by reference
    (Pitfall 6 / D-10); every referenced resource is excluded."""
    leaf_ok = _mk(resources.T_STORAGE, rid="/r/leaf-ok")
    parent = _mk(resources.T_VNET, rid="/r/vnet")
    child = _mk(f"{resources.T_VNET}/subnets", rid="/r/vnet/subnets/s0")
    role_ref = _mk(resources.T_STORAGE, rid="/r/role-ref")
    dep_ref = _mk(resources.T_STORAGE, rid="/r/dep-ref")
    viol_ref = _mk(resources.T_STORAGE, rid="/r/viol-ref")
    managed_ref = _mk(resources.T_STORAGE, rid="/r/managed-ref")

    rows = [leaf_ok, parent, child, role_ref, dep_ref, viol_ref, managed_ref]
    refs = drift.DisappearRefs(
        role_scopes=frozenset({"/r/role-ref"}),
        dependency_ids=frozenset({"/r/dep-ref"}),
        violation_ids=frozenset({"/r/viol-ref"}),
        managed_by_ids=frozenset({"/r/managed-ref"}),
    )
    ids = [r.id for r in drift.disappear_eligible(rows, refs)]

    assert "/r/leaf-ok" in ids
    assert "/r/vnet" not in ids          # has a child id -> not a leaf
    assert "/r/role-ref" not in ids      # role-assignment scope
    assert "/r/dep-ref" not in ids       # dependency source/target
    assert "/r/viol-ref" not in ids      # violation resource_id
    assert "/r/managed-ref" not in ids   # managed_by reference
    assert ids == sorted(ids)            # Pitfall 3 — sorted before any draw


def test_appear_deferred_append():
    """Appear-minted leaves are appended to their RG AFTER iteration completes —
    the iterated .resources list is never mutated mid-loop (violations.inject
    `minted` idiom)."""
    ctx = _ctx()
    rg_a = _rg(
        "rg-a",
        resources_list=[
            _mk(resources.T_STORAGE, rid="/r/a0"),
            _mk(resources.T_STORAGE, rid="/r/a1"),
        ],
    )
    rg_b = _rg("rg-b", resources_list=[_mk(resources.T_KV, rid="/r/b0")])
    before_total = sum(len(g.resources) for g in (rg_a, rg_b))

    deltas, minted = drift.compute_lifecycle(
        ctx, [rg_a, rg_b], drift.DisappearRefs(), appear_count=2
    )

    assert len(minted) == 2
    after_total = sum(len(g.resources) for g in (rg_a, rg_b))
    # exactly the minted count was appended (no runaway mid-iteration duplication).
    assert after_total == before_total + 2
    for leaf in minted:
        assert any(leaf in g.resources for g in (rg_a, rg_b))
    appear_deltas = [d for d in deltas if d["drift_code"] == "DRIFT_APPEAR"]
    assert len(appear_deltas) == 2
    assert all(d["before"] is None for d in appear_deltas)  # marker so revert DELETEs


def test_appear_no_real_ids():
    """An appear-minted leaf carries zero real identifiers: a planted fake-real
    token never appears, the leaf has no real-derived tags/properties, and the
    seeded synthetic name passes the analyzer identifier-shape guard (memory: new
    string-emitting paths reapply the privacy guard + ship a leak test)."""
    from tenantless.analyzer.extractors.tags import _is_identifier_shaped_value

    fake_real = "AcmeCorpRealTenant-PII-9f8e7d6c5b4a"
    leaf = drift.mint_appear_leaf(_ctx(), _rg("rg-syn-001"))

    blob = f"{leaf.id}|{leaf.name}|{leaf.tags}|{leaf.properties}"
    assert fake_real not in blob
    assert fake_real.lower() not in blob.lower()
    # the new string-emitting path carries no real-derived data.
    assert leaf.tags == {}
    assert leaf.properties == {}
    assert leaf.type == resources.T_STORAGE
    assert not _is_identifier_shaped_value(leaf.name)


def test_appear_seeded_deterministic():
    """The same seed mints a byte-identical appear-leaf id/name (D-12)."""
    leaf1 = drift.mint_appear_leaf(SeededContext(123), _rg("rg-syn-001"))
    leaf2 = drift.mint_appear_leaf(SeededContext(123), _rg("rg-syn-001"))
    assert leaf1.id == leaf2.id
    assert leaf1.name == leaf2.name
    # a different seed mints a different id.
    leaf3 = drift.mint_appear_leaf(SeededContext(999), _rg("rg-syn-001"))
    assert leaf3.id != leaf1.id
