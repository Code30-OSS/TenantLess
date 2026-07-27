"""Drift determinism tests (Plan 11-03, DRIFT-01 / D-08).

Pins the byte-identical-drift contract: the same ``(seed, options,
parent-state)`` must yield identical selected resources AND identical deltas, and
the D-08 ``state_fingerprint`` must be a stable, order-independent SHA-256 over
the served-state tuple ``(id, tags, sku, kind, properties, drift_deleted_at)``.
DB-free — operates on in-memory :class:`resources.Resource` objects.
"""

from __future__ import annotations

import uuid

import orjson

from tenantless.generator import drift, resources
from tenantless.generator.rng import SeededContext


def _mk(type_key: str, rid: str, *, props=None, tags=None):
    return resources.Resource(
        id=rid,
        subscription_id=uuid.uuid4(),
        resource_group_name="rg",
        name=rid.rsplit("/", 1)[-1],
        type=type_key,
        location="eastus",
        api_version="2023-01-01",
        tags=dict(tags or {}),
        properties=dict(props or {}),
    )


def _fresh_storage_pop(n=20):
    """A deterministic population of storage accounts with stable ids."""
    return [
        _mk(resources.T_STORAGE, f"/r/stor{i:03d}", props={"minimumTlsVersion": "TLS1_2"})
        for i in range(n)
    ]


# --------------------------------------------------------------------------- #
# D-08 state fingerprint
# --------------------------------------------------------------------------- #


def test_fingerprint_stable_and_order_independent():
    rows = [
        {"id": "/r/b", "tags": {"a": "1"}, "sku": None, "kind": None,
         "properties": {"x": 1, "y": 2}, "drift_deleted_at": None},
        {"id": "/r/a", "tags": {}, "sku": {"name": "S0"}, "kind": "v1",
         "properties": {"z": 3}, "drift_deleted_at": None},
    ]
    fp1 = drift.state_fingerprint(rows)
    # Shuffled input order yields the SAME digest (sort-by-id + OPT_SORT_KEYS).
    fp2 = drift.state_fingerprint(list(reversed(rows)))
    assert fp1 == fp2
    assert isinstance(fp1, str) and len(fp1) == 64  # sha256 hexdigest


def test_fingerprint_sensitive_to_property_change():
    base = [
        {"id": "/r/a", "tags": {}, "sku": None, "kind": None,
         "properties": {"z": 3}, "drift_deleted_at": None},
    ]
    fp_base = drift.state_fingerprint(base)

    changed = [dict(base[0], properties={"z": 4})]
    assert drift.state_fingerprint(changed) != fp_base


def test_fingerprint_sensitive_to_drift_deleted_at():
    base = [
        {"id": "/r/a", "tags": {}, "sku": None, "kind": None,
         "properties": {}, "drift_deleted_at": None},
    ]
    fp_base = drift.state_fingerprint(base)

    hidden = [dict(base[0], drift_deleted_at="2026-06-27T00:00:00Z")]
    assert drift.state_fingerprint(hidden) != fp_base


def test_fingerprint_drift_deleted_at_is_boolean_presence():
    """drift_deleted_at enters the digest as a STABLE boolean presence flag, never
    its wall-clock timestamp value (P2a / D-08 determinism): two different deletion
    timestamps yield the SAME fingerprint — no wall-clock in the digest. Deleted vs
    active is still distinguished (presence is significant)."""
    base = {"id": "/r/a", "tags": {}, "sku": None, "kind": None,
            "properties": {}, "drift_deleted_at": None}
    hidden_t1 = [dict(base, drift_deleted_at="2026-06-27T00:00:00Z")]
    hidden_t2 = [dict(base, drift_deleted_at="2030-01-01T12:34:56Z")]
    # Distinct wall-clock timestamps must NOT change the digest (determinism).
    assert drift.state_fingerprint(hidden_t1) == drift.state_fingerprint(hidden_t2)
    # but deleted (present) still differs from active (absent).
    assert drift.state_fingerprint(hidden_t1) != drift.state_fingerprint([base])


# --------------------------------------------------------------------------- #
# DRIFT-01 reproducible chaos compute
# --------------------------------------------------------------------------- #


def _delta_blob(deltas: list[dict]) -> bytes:
    return orjson.dumps(deltas, option=orjson.OPT_SORT_KEYS)


def test_reproducible_chaos():
    """Two identical-seed compute_drift runs over identical parent rows produce
    identical selected-resource ids AND identical delta lists."""
    opts = dict(drift_type="chaos", codes=["DRIFT_STORAGE_OLD_TLS"], intensity=0.5)

    pop_a = _fresh_storage_pop()
    deltas_a = drift.compute_drift(SeededContext(7), pop_a, **opts)

    pop_b = _fresh_storage_pop()
    deltas_b = drift.compute_drift(SeededContext(7), pop_b, **opts)

    ids_a = [d["resource_id"] for d in deltas_a]
    ids_b = [d["resource_id"] for d in deltas_b]
    assert ids_a == ids_b
    assert len(ids_a) == 10  # 0.5 of 20 eligible
    assert _delta_blob(deltas_a) == _delta_blob(deltas_b)
    # All selected storage accounts had their served TLS key overwritten.
    assert all(r.properties["minimumTlsVersion"] == "TLS1_0"
               for r in pop_a if r.id in set(ids_a))


def test_different_seed_changes_selection():
    opts = dict(drift_type="chaos", codes=["DRIFT_STORAGE_OLD_TLS"], intensity=0.5)
    ids_7 = [d["resource_id"] for d in
             drift.compute_drift(SeededContext(7), _fresh_storage_pop(), **opts)]
    ids_99 = [d["resource_id"] for d in
              drift.compute_drift(SeededContext(99), _fresh_storage_pop(), **opts)]
    assert ids_7 != ids_99  # different seed -> different sampled subset


def test_compute_drift_respects_codes_and_types():
    """Only the requested codes fire, filtered to the chaos drift_type."""
    pop = _fresh_storage_pop(4) + [
        _mk(resources.T_KV, f"/r/kv{i}", props={"enableSoftDelete": True})
        for i in range(4)
    ]
    deltas = drift.compute_drift(
        SeededContext(1),
        pop,
        drift_type="chaos",
        codes=["DRIFT_KV_NO_SOFT_DELETE"],
        intensity=1.0,
    )
    assert {d["drift_code"] for d in deltas} == {"DRIFT_KV_NO_SOFT_DELETE"}
    assert all(d["field_path"] == "properties.enableSoftDelete" for d in deltas)
    assert len(deltas) == 4  # all KV accounts at intensity 1.0
