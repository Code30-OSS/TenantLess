"""Governance-violation injection engine (Plan 05-02, VIOL-01..10).

A table-driven registry of all 18 VIOL codes — the RESEARCH §Mutation Table is
the single source of truth for every ARM property/tag path, its real insecure
value, the operation (flip / insert / delete / record-only / wire), and the
severity. The registry mirrors the table-as-data idiom of
``arm._API_VERSION_BY_PROVIDER`` (one ``ViolationSpec`` per code) and the
per-record sampler idiom of ``resources._sample_field``.

Injection contract (D-02 / D-05 / D-06 / VIOL-09):

- Each violation independently samples its ELIGIBLE resource population via
  ``ctx.bernoulli(rate)`` per resource. Rates default to the REQUIREMENTS spec
  values (Pitfall 1: the real-source profile gov block is degenerate — never
  derive rates from the profile), overridable per-code via ``rates``.
- Per-type independent sampling means a single resource can accumulate several
  violation rows (VIOL-09 stacking).
- Each hit does BOTH: mutate ``Resource.properties`` / ``Resource.tags`` in place
  to the genuine insecure ARM shape (D-06, ARM-faithful) AND append one
  ``synthetic.violations`` row ``{resource_id, violation_type, severity, detail}``
  (D-05). ``detail`` is a plain dict — JSONB wrapping is the writer's job
  (Pitfall 5).

Determinism (VIOL-10, T-05-REPRO): every draw goes through the injected
``SeededContext`` (``ctx.bernoulli`` / ``ctx.choice`` / ``ctx.uuid4``); the
registry and each eligible population are SORTED before any draw (Pitfall 3), so
the same ``(profile, seed)`` yields identical violation rows.

MEDIUM-confidence "invent" rows (RESEARCH A4): rows 1/15/18
(STORAGE_NO_ENCRYPTION / SQL_NO_AUDIT / DISK_UNENCRYPTED) inject a synthetic
*flat* ARM shape rather than the full child-resource hierarchy — the contract is
governance-rule detection fidelity, not live-Azure create validity. Rows 1/8 are
"legacy insecure forms" (Pitfall 6): real Azure (since Feb 2025) rejects them on
create, but they are the canonical shapes every governance ruleset matches.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from . import arm, resources
from .rng import SeededContext


@dataclass(frozen=True)
class ViolationSpec:
    """One VIOL code as DATA (mirror of ``arm._API_VERSION_BY_PROVIDER``).

    ``mutate(ctx, resource) -> detail_dict`` mutates ``resource.properties`` /
    ``resource.tags`` in place and returns the audit ``detail`` payload.
    ``eligible_predicate`` (TAG_* only) further narrows the eligible population
    beyond ``eligible_type`` — for TAG_* it is "the key is currently present"
    (Pitfall 3 reading (a), the LOCKED denominator).
    """

    eligible_type: str
    default_rate: float
    severity: str
    mutate: Callable[[SeededContext, Any], dict]
    eligible_predicate: Callable[[Any], bool] | None = None


# --------------------------------------------------------------------------- #
# FLIP mutate-fns (rows 2,3,4,8,9,16,17) — overwrite an existing secure field
# emitted by resources.assemble_properties() with its insecure value.
# --------------------------------------------------------------------------- #


def _storage_public_access(ctx: SeededContext, r) -> dict:  # row 2
    before = r.properties.get("allowBlobPublicAccess")
    r.properties["allowBlobPublicAccess"] = True
    return {"path": "properties.allowBlobPublicAccess", "from": before, "to": True}


def _storage_http_allowed(ctx: SeededContext, r) -> dict:  # row 3
    before = r.properties.get("supportsHttpsTrafficOnly")
    r.properties["supportsHttpsTrafficOnly"] = False
    return {
        "path": "properties.supportsHttpsTrafficOnly",
        "from": before,
        "to": False,
    }


def _storage_old_tls(ctx: SeededContext, r) -> dict:  # row 4
    before = r.properties.get("minimumTlsVersion")
    r.properties["minimumTlsVersion"] = "TLS1_0"
    return {"path": "properties.minimumTlsVersion", "from": before, "to": "TLS1_0"}


def _kv_no_soft_delete(ctx: SeededContext, r) -> dict:  # row 8 (legacy form, Pitfall 6)
    before = r.properties.get("enableSoftDelete")
    r.properties["enableSoftDelete"] = False
    return {"path": "properties.enableSoftDelete", "from": before, "to": False}


def _kv_no_purge_protect(ctx: SeededContext, r) -> dict:  # row 9
    before = r.properties.get("enablePurgeProtection")
    r.properties["enablePurgeProtection"] = False
    return {"path": "properties.enablePurgeProtection", "from": before, "to": False}


def _sql_no_tde(ctx: SeededContext, r) -> dict:  # row 16
    before = r.properties.get("transparentDataEncryption")
    r.properties["transparentDataEncryption"] = "Disabled"
    return {
        "path": "properties.transparentDataEncryption",
        "from": before,
        "to": "Disabled",
    }


def _aks_rbac_disabled(ctx: SeededContext, r) -> dict:  # row 17
    before = r.properties.get("enableRBAC")
    r.properties["enableRBAC"] = False
    return {"path": "properties.enableRBAC", "from": before, "to": False}


# --------------------------------------------------------------------------- #
# DELETE mutate-fns (rows 12,13,14) — remove a tag key. The spec's
# eligible_predicate restricts the population to resources that HAVE the key
# (Pitfall 3 reading (a) — LOCKED denominator).
# --------------------------------------------------------------------------- #


def _delete_tag(key: str) -> Callable[[SeededContext, Any], dict]:
    def _mutate(ctx: SeededContext, r) -> dict:
        r.tags.pop(key, None)
        return {"tag_key": key}

    return _mutate


def _has_tag(key: str) -> Callable[[Any], bool]:
    return lambda r: key in r.tags


# --------------------------------------------------------------------------- #
# INSERT mutate-fns — STORAGE / SQL / DISK "invent" rows (1, 15, 18) and the NSG
# open-rule appends (5, 6, 7). Rows 1/15/18 inject a synthetic FLAT ARM shape
# (RESEARCH A4 — MEDIUM confidence; the contract is governance-rule detection
# fidelity, not live-Azure create validity). Row 1 is also a "legacy insecure
# form" (Pitfall 6).
# --------------------------------------------------------------------------- #


def _storage_no_encryption(ctx: SeededContext, r) -> dict:  # row 1 (A4 / Pitfall 6)
    r.properties["encryption"] = {
        "services": {"blob": {"enabled": False}},
        "keySource": "Microsoft.Storage",
    }
    return {"path": "properties.encryption.services.blob.enabled", "set": False}


def _sql_no_audit(ctx: SeededContext, r) -> dict:  # row 15 (A4)
    r.properties["auditingSettings"] = {"state": "Disabled"}
    return {"path": "properties.auditingSettings.state", "set": "Disabled"}


def _disk_unencrypted(ctx: SeededContext, r) -> dict:  # row 18 (A4)
    r.properties["encryption"] = {
        "type": "EncryptionAtRestWithPlatformKey",
        "enabled": False,
    }
    return {"path": "properties.encryption.enabled", "set": False}


def _vm_no_backup(ctx: SeededContext, r) -> dict:  # row 10 (record-only)
    r.properties["_noRecoveryServicesLink"] = True
    return {"rationale": "no recovery-services vault link"}


def _nsg_open_rule(
    name: str, port: str, protocol: str
) -> Callable[[SeededContext, Any], dict]:
    """Build an NSG INSERT mutate-fn (rows 5/6/7).

    Appends a genuinely-open inbound rule to ``securityRules[]`` with a distinct
    name and a priority OUTSIDE the generated ``100+i*10`` band (``max+10``,
    Pitfall 2) so it never collides with an existing ``rule-NN`` priority/name.
    """

    def _mutate(ctx: SeededContext, r) -> dict:
        rules = r.properties.setdefault("securityRules", [])
        used = [
            p
            for p in (x.get("properties", {}).get("priority") for x in rules)
            if p
        ]
        prio = (max(used) if used else 100) + 10
        rules.append(
            {
                "name": name,
                "properties": {
                    "access": "Allow",
                    "direction": "Inbound",
                    "protocol": protocol,
                    "destinationPortRange": port,
                    "sourceAddressPrefix": "*",
                    "destinationAddressPrefix": "*",
                    "priority": prio,
                },
            }
        )
        return {"rule_name": name, "port": port}

    return _mutate


def _vm_public_ip_placeholder(ctx: SeededContext, r) -> dict:  # row 11 (guard)
    """Guard: VM_PUBLIC_IP is wired specially by :func:`inject`.

    Resolving the VM's NIC and (when no PIP pool exists) minting a companion PIP
    require the whole-tenant index + a post-loop append, which the plain
    ``(ctx, r)`` mutate signature cannot reach. :func:`inject` therefore routes
    VM_PUBLIC_IP through :func:`_wire_vm_public_ip`; this guard only fires if the
    spec is invoked outside that path.
    """
    raise RuntimeError(
        "VM_PUBLIC_IP must be injected via violations.inject (tenant-context wiring)"
    )


# --------------------------------------------------------------------------- #
# The registry — all 18 codes as DATA (RESEARCH §Mutation Table is authority).
# --------------------------------------------------------------------------- #

VIOLATION_REGISTRY: dict[str, ViolationSpec] = {
    # Row 1 — INSERT (MEDIUM-confidence "invent" flat shape, A4; legacy form, Pitfall 6)
    "STORAGE_NO_ENCRYPTION": ViolationSpec(
        resources.T_STORAGE, 0.08, "High", _storage_no_encryption
    ),
    # Row 2 — FLIP
    "STORAGE_PUBLIC_ACCESS": ViolationSpec(
        resources.T_STORAGE, 0.05, "High", _storage_public_access
    ),
    # Row 3 — FLIP
    "STORAGE_HTTP_ALLOWED": ViolationSpec(
        resources.T_STORAGE, 0.03, "Medium", _storage_http_allowed
    ),
    # Row 4 — FLIP
    "STORAGE_OLD_TLS": ViolationSpec(
        resources.T_STORAGE, 0.04, "Medium", _storage_old_tls
    ),
    # Rows 5,6,7 — INSERT securityRules[] open inbound rule
    "NSG_OPEN_SSH": ViolationSpec(
        resources.T_NSG,
        0.06,
        "High",
        _nsg_open_rule("AllowSSH-Inbound", "22", "Tcp"),
    ),
    "NSG_OPEN_RDP": ViolationSpec(
        resources.T_NSG,
        0.04,
        "High",
        _nsg_open_rule("AllowRDP-Inbound", "3389", "Tcp"),
    ),
    "NSG_OPEN_ALL": ViolationSpec(
        resources.T_NSG,
        0.02,
        "High",
        _nsg_open_rule("AllowAll-Inbound", "*", "*"),
    ),
    # Row 8 — FLIP (legacy insecure form, Pitfall 6)
    "KV_NO_SOFT_DELETE": ViolationSpec(
        resources.T_KV, 0.07, "Medium", _kv_no_soft_delete
    ),
    # Row 9 — FLIP
    "KV_NO_PURGE_PROTECT": ViolationSpec(
        resources.T_KV, 0.10, "Medium", _kv_no_purge_protect
    ),
    # Row 10 — record-only
    "VM_NO_BACKUP": ViolationSpec(
        resources.T_VM, 0.15, "Low", _vm_no_backup
    ),
    # Row 11 — wire the VM's NIC publicIPAddress.id (handled specially by inject:
    # NIC resolution + post-loop PIP mint need tenant context, so the registered
    # mutate is a guard — _wire_vm_public_ip does the real work).
    "VM_PUBLIC_IP": ViolationSpec(
        resources.T_VM, 0.08, "Medium", _vm_public_ip_placeholder
    ),
    # Rows 12,13,14 — DELETE tag key (eligible = key present, reading (a))
    "TAG_MISSING_ENV": ViolationSpec(
        resources.T_STORAGE,  # placeholder; eligibility is type-agnostic (see below)
        0.12,
        "Low",
        _delete_tag("environment"),
        eligible_predicate=_has_tag("environment"),
    ),
    "TAG_MISSING_OWNER": ViolationSpec(
        resources.T_STORAGE,
        0.18,
        "Low",
        _delete_tag("owner"),
        eligible_predicate=_has_tag("owner"),
    ),
    "TAG_MISSING_COSTCENTER": ViolationSpec(
        resources.T_STORAGE,
        0.15,
        "Low",
        _delete_tag("costCenter"),
        eligible_predicate=_has_tag("costCenter"),
    ),
    # Row 15 — INSERT auditingSettings (MEDIUM-confidence flat shape, A4)
    "SQL_NO_AUDIT": ViolationSpec(
        resources.T_SQLSRV, 0.10, "Medium", _sql_no_audit
    ),
    # Row 16 — FLIP
    "SQL_NO_TDE": ViolationSpec(resources.T_SQLDB, 0.05, "High", _sql_no_tde),
    # Row 17 — FLIP
    "AKS_RBAC_DISABLED": ViolationSpec(
        resources.T_AKS, 0.08, "Medium", _aks_rbac_disabled
    ),
    # Row 18 — INSERT encryption (MEDIUM-confidence flat shape, A4)
    "DISK_UNENCRYPTED": ViolationSpec(
        resources.T_DISK, 0.06, "High", _disk_unencrypted
    ),
}

# TAG_* violations apply to ANY taggable resource that currently carries the key,
# not just one ``eligible_type``. The sentinel ``_TAG_ANY_TYPE`` marks specs whose
# population is "all resources" (filtered by ``eligible_predicate``) rather than a
# single ``r.type`` match.
_TAG_CODES = frozenset(
    {"TAG_MISSING_ENV", "TAG_MISSING_OWNER", "TAG_MISSING_COSTCENTER"}
)


def _eligible_population(all_res: list, code: str, spec: ViolationSpec) -> list:
    """The sorted eligible resource list for ``code`` (Pitfall 3 — sorted).

    For TAG_* codes the population is "any taggable resource that HAS the key"
    (reading (a)), so ``eligible_type`` is ignored and only the predicate filters.
    Otherwise the population is resources whose ``type`` matches ``eligible_type``.
    """
    if code in _TAG_CODES:
        pred = spec.eligible_predicate
        candidates = (r for r in all_res if pred is None or pred(r))
    else:
        pred = spec.eligible_predicate
        candidates = (
            r
            for r in all_res
            if r.type == spec.eligible_type and (pred is None or pred(r))
        )
    return sorted(candidates, key=lambda r: r.id)


_VM_PUBLIC_IP = "VM_PUBLIC_IP"


def _vm_nic(vm, by_id: dict) -> Any:
    """Resolve the VM's associated NIC resource from its networkProfile, or None."""
    refs = (vm.properties.get("networkProfile", {}) or {}).get(
        "networkInterfaces", []
    )
    for ref in refs:
        nic = by_id.get(ref.get("id"))
        if nic is not None and nic.type == resources.T_NIC:
            return nic
    return None


def _mint_pip(ctx: SeededContext, nic, rg, seen_ids: set[str]):
    """Mint a minimal PIP resource into ``rg`` (rtd-free fallback for VM_PUBLIC_IP).

    Used only when the tenant has NO existing public IP to attach. The id is built
    via :func:`arm.resource_id` (never string-concat, D-11) and de-duplicated
    against ``seen_ids`` so the PK stays unique before COPY.
    """
    name = f"pip-{ctx.faker.lexify('????').lower()}-{int(ctx.rng.integers(1, 10000)):04d}"
    pid = arm.resource_id(nic.subscription_id, nic.resource_group_name, resources.T_PIP, name)
    while pid in seen_ids:
        name = f"pip-{ctx.faker.lexify('????').lower()}-{int(ctx.rng.integers(1, 10000)):04d}"
        pid = arm.resource_id(
            nic.subscription_id, nic.resource_group_name, resources.T_PIP, name
        )
    seen_ids.add(pid)
    return resources.Resource(
        id=pid,
        subscription_id=nic.subscription_id,
        resource_group_name=nic.resource_group_name,
        name=name,
        type=resources.T_PIP,
        location=nic.location,
        api_version=arm.api_version_for(resources.T_PIP),
        properties={
            "publicIPAllocationMethod": "Static",
            "publicIPAddressVersion": "IPv4",
        },
    )


def _wire_vm_public_ip(ctx, vm, by_id, rg_of, pip_pool, seen_ids, minted) -> dict | None:
    """Ensure the VM's NIC carries a resolvable ``publicIPAddress.id`` (row 11).

    Attaches an existing PIP from ``pip_pool`` when one exists; otherwise mints a
    minimal companion PIP appended to the NIC's RG (deferred via ``minted`` so we
    never mutate a list being iterated). Returns the audit ``detail`` or ``None``
    when the VM has no resolvable NIC (skip — never fabricate a dangling ref).
    """
    nic = _vm_nic(vm, by_id)
    if nic is None:
        return None
    if pip_pool:
        pip_id = ctx.choice(pip_pool)
    else:
        host_rg = rg_of.get(nic.id) or rg_of.get(vm.id)
        if host_rg is None:
            return None
        pip = _mint_pip(ctx, nic, host_rg, seen_ids)
        minted.append((host_rg, pip))
        pip_id = pip.id
    for cfg in nic.properties.get("ipConfigurations", []):
        cfg.setdefault("properties", {})["publicIPAddress"] = {"id": pip_id}
    return {"nic_id": nic.id, "public_ip_id": pip_id}


def inject(
    ctx: SeededContext,
    tenant,
    registry: dict[str, ViolationSpec],
    rates: dict[str, float] | None = None,
) -> list[dict]:
    """Inject governance violations into ``tenant`` in place (D-02/D-05/VIOL-09).

    For each code (in sorted order, Pitfall 3) draw ``ctx.bernoulli(rate)``
    independently over the code's sorted eligible population; on a hit, mutate the
    resource in place and record one ``{resource_id, violation_type, severity,
    detail}`` row. Per-type independence means one resource can stack multiple
    rows (VIOL-09). Returns the full list of recorded rows.

    VM_PUBLIC_IP (row 11) is special-cased: it resolves the VM's NIC, attaches an
    existing public IP or mints a companion PIP appended to the NIC's RG AFTER the
    per-code loop (never mutate-during-iteration).
    """
    rates = rates or {}
    all_res = [r for rg in tenant.resource_groups for r in rg.resources]
    by_id = {r.id: r for r in all_res}
    rg_of = {r.id: rg for rg in tenant.resource_groups for r in rg.resources}
    pip_pool = sorted(r.id for r in all_res if r.type == resources.T_PIP)
    seen_ids = set(by_id)
    minted: list[tuple] = []  # (rg, pip) appended AFTER iteration

    rows: list[dict] = []
    for code, spec in sorted(registry.items()):
        eligible = _eligible_population(all_res, code, spec)
        rate = rates.get(code, spec.default_rate)
        for r in eligible:
            if not ctx.bernoulli(rate):
                continue
            if code == _VM_PUBLIC_IP:
                detail = _wire_vm_public_ip(
                    ctx, r, by_id, rg_of, pip_pool, seen_ids, minted
                )
                if detail is None:
                    continue  # no resolvable NIC — skip (never a dangling ref)
            else:
                detail = spec.mutate(ctx, r)
            rows.append(
                {
                    "resource_id": r.id,
                    "violation_type": code,
                    "severity": spec.severity,
                    "detail": detail,
                }
            )

    # Append any minted companion PIPs now (post-loop — Anti-pattern: never mutate
    # a .resources list while iterating the eligible population over it).
    for rg, pip in minted:
        rg.resources.append(pip)

    return rows
