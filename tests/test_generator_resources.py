"""GEN-04 / GEN-07 / GEN-08 resource-generation contract.

These behaviors (per-RG resource type mix, ARM-valid property coercion, and
intra-resource reference resolution) are implemented in Plan 02-02. The earlier
skip-marked stubs are replaced here with real, DB-free assertions against
``profiles/test-small.json`` (the ``generator_profile`` fixture).
"""

from __future__ import annotations

import re

from tenantless.generator import arm, cross_sub
from tenantless.generator.pipeline import generate_tenant

# ARM id path: /subscriptions/{uuid}/resourceGroups/{rg}/providers/{ns}/{type}/{name}
_ARM_ID_RE = re.compile(
    r"^/subscriptions/[0-9a-fA-F-]{36}/resourceGroups/[^/]+/providers/"
    r"Microsoft\.[A-Za-z0-9]+/.+$"
)


# --------------------------------------------------------------------------- #
# Task 1 — arm.py: ARM id synthesis, casing, api-version (no DB)
# --------------------------------------------------------------------------- #


def test_arm_resource_id_format():
    """resource_id builds a canonical ARM path for a simple (non-nested) type."""
    sub = "11111111-1111-4111-8111-111111111111"
    rid = arm.resource_id(sub, "rg-eng-dev-api-01", "Microsoft.Storage/storageAccounts", "stengdev01")
    assert rid == (
        f"/subscriptions/{sub}/resourceGroups/rg-eng-dev-api-01"
        "/providers/Microsoft.Storage/storageAccounts/stengdev01"
    )
    assert _ARM_ID_RE.match(rid)


def test_arm_resource_id_nested():
    """A nested type embeds the parent name between the two type segments."""
    sub = "22222222-2222-4222-8222-222222222222"
    rid = arm.resource_id(
        sub,
        "rg-data-prod-sql-02",
        "Microsoft.Sql/servers/databases",
        "appdb",
        parent_name="sqlsrv01",
    )
    assert rid == (
        f"/subscriptions/{sub}/resourceGroups/rg-data-prod-sql-02"
        "/providers/Microsoft.Sql/servers/sqlsrv01/databases/appdb"
    )


def test_arm_rg_id_matches_pipeline():
    """rg_id() produces exactly the RG id the pipeline already wrote (02-01)."""
    sub = "33333333-3333-4333-8333-333333333333"
    assert arm.rg_id(sub, "rg-fin-prod-payments-07") == (
        f"/subscriptions/{sub}/resourceGroups/rg-fin-prod-payments-07"
    )


def test_arm_canonical_type_single_casing():
    """canonical_type is idempotent and preserves the profile's Microsoft.-leading key."""
    t = "Microsoft.Compute/virtualMachines"
    assert arm.canonical_type(t) == t
    # lowercase-namespace source canonicalizes the leading token only
    assert arm.canonical_type("microsoft.compute/virtualmachines") == (
        "Microsoft.compute/virtualmachines"
    )
    # idempotent
    assert arm.canonical_type(arm.canonical_type(t)) == t


def test_arm_api_version_for():
    """api_version_for returns a plausible recent api-version per provider."""
    v = arm.api_version_for("Microsoft.Compute/virtualMachines")
    assert re.match(r"^\d{4}-\d{2}-\d{2}", v)
    # unknown provider still returns a plausible default, never empty
    assert re.match(r"^\d{4}-\d{2}-\d{2}", arm.api_version_for("Microsoft.Foo/bars"))


# --------------------------------------------------------------------------- #
# Task 2 — resources.py: type mix (GEN-04) + ARM coercion (GEN-07)
# --------------------------------------------------------------------------- #


def test_resource_type_mix_and_props(generator_profile):
    """GEN-04: resources per RG follow the template type mix and carry sampled
    sku / kind / location / provisioning state."""
    tenant = generate_tenant(generator_profile, seed=42).tenant

    all_resources = [r for rg in tenant.resource_groups for r in rg.resources]
    assert all_resources, "expected resources to be generated"

    # the synthetic __misc__ type-set is never emitted as a resource type
    assert all(r.type != "__misc__" for r in all_resources)
    assert all("__misc__" not in r.type for r in all_resources)

    # every resource type is drawn from some template's type_set (canonicalized),
    # except wired child resources (subnets) minted during reference resolution.
    template_types = set()
    for tmpl in generator_profile["resource_group_templates"]:
        for t in tmpl.get("type_set", []):
            if t != "__misc__":
                template_types.add(arm.canonical_type(t))
    # Excluded from the template-type invariant: subnet child rows minted during
    # reference resolution, and the cross-sub host/companion anchors minted by the
    # default-on Phase-5 cross_sub pass (private endpoints + shared ACR registries
    # are not part of any template type_set — they are topology infrastructure).
    minted_types = {
        "Microsoft.Network/virtualNetworks/subnets",
        cross_sub.T_PE,
        cross_sub.T_ACR,
    }
    sampled_types = {r.type for r in all_resources} - minted_types
    assert sampled_types <= template_types

    # each resource inherits its RG location + has a provisioning state + ARM id
    for rg in tenant.resource_groups:
        for r in rg.resources:
            assert r.location == rg.location
            assert r.provisioning_state
            assert r.id.startswith(
                f"/subscriptions/{rg.subscription_id}/resourceGroups/{rg.name}/providers/"
            )

    # the vm-deployment template's types should appear in the mix
    vm_types = {
        r.type for r in all_resources if r.type == "Microsoft.Compute/virtualMachines"
    }
    assert vm_types, "expected at least one VM from the vm-deployment template"

    # a VM carries a sampled sku name from the profile distribution
    vms = [r for r in all_resources if r.type == "Microsoft.Compute/virtualMachines"]
    sku_names = generator_profile["resource_type_distributions"][
        "Microsoft.Compute/virtualMachines"
    ]["sku_distributions"]["name"]
    assert any(v.sku and v.sku.get("name") in sku_names for v in vms)


def test_property_coercion_arm_valid(generator_profile):
    """GEN-07: per-type properties JSONB is ARM-valid — string profile values
    coerced back to bool/int/null, and no sentinel is ever echoed."""
    tenant = generate_tenant(generator_profile, seed=7).tenant
    all_resources = [r for rg in tenant.resource_groups for r in rg.resources]

    def walk(obj):
        """Yield every scalar value in a nested dict/list."""
        if isinstance(obj, dict):
            for v in obj.values():
                yield from walk(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from walk(v)
        else:
            yield obj

    saw_bool = False
    saw_int = False
    for r in all_resources:
        for blob in (r.properties, r.sku or {}):
            for val in walk(blob):
                # No sentinel / stringified-null ever leaks into data
                assert val not in ("__other__", "__misc__", "null")
                if isinstance(val, str):
                    assert val not in ("true", "false"), (
                        f"boolean left as string in {r.type}: {blob}"
                    )
                if isinstance(val, bool):
                    saw_bool = True
                elif isinstance(val, int):
                    saw_int = True

    # storage account booleans are real JSON booleans
    storage = [
        r for r in all_resources if r.type == "Microsoft.Storage/storageAccounts"
    ]
    assert storage, "expected storage accounts in the mix"
    for s in storage:
        assert isinstance(s.properties["supportsHttpsTrafficOnly"], bool)
        assert isinstance(s.properties["allowBlobPublicAccess"], bool)
        assert s.properties["minimumTlsVersion"] in ("TLS1_2", "TLS1_0", "TLS1_1")

    # disk sizes are ints (a count/size field coerced from "127"-style strings)
    disks = [r for r in all_resources if r.type == "Microsoft.Compute/disks"]
    for d in disks:
        assert isinstance(d.properties["diskSizeGB"], int)

    assert saw_bool, "expected at least one coerced boolean property"
    assert saw_int, "expected at least one coerced integer property"


# --------------------------------------------------------------------------- #
# Task 3 — resources.py: reference resolution (GEN-08)
# --------------------------------------------------------------------------- #


def test_references_resolve(generator_profile):
    """GEN-08: every intra-property resource-ID reference resolves to a real
    generated resource id in the same tenant."""
    tenant = generate_tenant(generator_profile, seed=42).tenant
    all_resources = [r for rg in tenant.resource_groups for r in rg.resources]
    all_ids = {r.id for r in all_resources}

    def collect_ref_ids(obj):
        """Yield every value stored under an 'id' key in nested ARM properties."""
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "id" and isinstance(v, str):
                    yield v
                else:
                    yield from collect_ref_ids(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from collect_ref_ids(v)

    ref_ids = []
    for r in all_resources:
        ref_ids.extend(collect_ref_ids(r.properties))

    assert ref_ids, "expected at least one intra-resource reference (VM→NIC etc.)"
    unresolved = [rid for rid in ref_ids if rid not in all_ids]
    assert not unresolved, f"references do not resolve: {unresolved[:5]}"

    # a VM (when present) should reference a real NIC
    vms = [r for r in all_resources if r.type == "Microsoft.Compute/virtualMachines"]
    for vm in vms:
        nics = vm.properties["networkProfile"]["networkInterfaces"]
        assert nics, "VM has no NIC reference"
        for nic_ref in nics:
            assert nic_ref["id"] in all_ids
