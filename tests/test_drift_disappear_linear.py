"""Temporal disappear eligibility must be linear-time and output-identical.

``drift.disappear_eligible`` used to decide "is this row a leaf?" by scanning
every OTHER row id for an ``id + "/"`` prefix, i.e. O(n^2) over the tenant, and a
temporal apply evaluated it twice. These DB-free tests pin:

* exact equivalence (same rows, same order) against the original quadratic
  algorithm with every id comparison made on the canonical identity key
  (``arm_id_key``), over seeded randomized inputs that exercise nesting,
  duplicates, mixed casing, trailing slashes, empty and single inputs;
* a differently-cased child keeps its parent (and a differently-cased reference
  protects its target), as the DELETE cascade's identity does;
* byte-identical output to the original raw-id algorithm on casing-uniform
  tenants (every generated tenant), so their drift output is unchanged;
* a scaling bound the quadratic version cannot meet;
* ``compute_lifecycle`` reusing a precomputed eligible list without recomputing it.
"""

from __future__ import annotations

import random
import sys
import time
import uuid
from types import SimpleNamespace

import pytest

from tenantless.generator import drift, resources
from tenantless.generator.rng import SeededContext
from tenantless.identity import arm_id_key


# --------------------------------------------------------------------------- #
# Oracles: the original (quadratic) algorithm on raw ids, kept verbatim, and the
# same algorithm with every id comparison on the canonical identity key.
# --------------------------------------------------------------------------- #


def _reference_folded_disappear_eligible(rows: list, refs: drift.DisappearRefs) -> list:
    referenced = {
        arm_id_key(i)
        for i in refs.role_scopes | refs.dependency_ids | refs.violation_ids | refs.managed_by_ids
    }
    keys = [arm_id_key(r.id) for r in rows]

    def _is_leaf(key: str) -> bool:
        prefix = key + "/"
        return not any(other != key and other.startswith(prefix) for other in keys)

    eligible = [
        r for r in rows if arm_id_key(r.id) not in referenced and _is_leaf(arm_id_key(r.id))
    ]
    return sorted(eligible, key=lambda r: r.id)


def _reference_raw_disappear_eligible(rows: list, refs: drift.DisappearRefs) -> list:
    referenced = (
        refs.role_scopes
        | refs.dependency_ids
        | refs.violation_ids
        | refs.managed_by_ids
    )
    ids = [r.id for r in rows]

    def _is_leaf(rid: str) -> bool:
        prefix = rid + "/"
        return not any(other != rid and other.startswith(prefix) for other in ids)

    eligible = [r for r in rows if r.id not in referenced and _is_leaf(r.id)]
    return sorted(eligible, key=lambda r: r.id)


# --------------------------------------------------------------------------- #
# Seeded random input generation
# --------------------------------------------------------------------------- #

_TYPES = [
    "Microsoft.Storage/storageAccounts",
    "Microsoft.Network/virtualNetworks",
    "Microsoft.Sql/servers",
    "Microsoft.KeyVault/vaults",
]
_CHILD_SEGS = ["subnets", "databases", "blobServices", "secrets"]


def _rand_case(rnd: random.Random, s: str) -> str:
    mode = rnd.random()
    if mode < 0.15:
        return s.upper()
    if mode < 0.30:
        return s.lower()
    return s


def _random_ids(rnd: random.Random, n: int) -> list[str]:
    ids: list[str] = []
    for _ in range(n):
        roll = rnd.random()
        if ids and roll < 0.25:
            # nested child of an existing id (arbitrary depth)
            base = rnd.choice(ids)
            ids.append(f"{base}/{rnd.choice(_CHILD_SEGS)}/c{rnd.randrange(4)}")
        elif ids and roll < 0.32:
            # a differently-cased copy of an existing id (one identity under arm_id_key)
            ids.append(_rand_case(rnd, rnd.choice(ids)))
        elif ids and roll < 0.36:
            # exact duplicate id
            ids.append(rnd.choice(ids))
        elif ids and roll < 0.39:
            # a string-prefix sibling that is NOT a path child ("/x/abc" vs "/x/ab")
            ids.append(rnd.choice(ids) + rnd.choice(["x", "-1", "0"]))
        elif roll < 0.41:
            # odd shapes: trailing slash, double slash, bare slash, empty
            ids.append(rnd.choice(["", "/", "//", "/a/", "/a//b", "a"]))
        else:
            sub = f"s{rnd.randrange(3)}"
            rg = _rand_case(rnd, f"rg-{rnd.randrange(5)}")
            typ = _rand_case(rnd, rnd.choice(_TYPES))
            ids.append(
                f"/subscriptions/{sub}/resourceGroups/{rg}/providers/{typ}/n{rnd.randrange(30)}"
            )
    rnd.shuffle(ids)
    return ids


def _random_refs(rnd: random.Random, ids: list[str]) -> drift.DisappearRefs:
    def pick(k: int) -> frozenset[str]:
        if not ids:
            return frozenset()
        chosen = {rnd.choice(ids) for _ in range(k)}
        # sprinkle case variants and ids absent from rows
        chosen |= {_rand_case(rnd, rnd.choice(ids)) for _ in range(k // 2)}
        chosen.add("/not/a/row")
        return frozenset(chosen)

    k = max(1, len(ids) // 10)
    return drift.DisappearRefs(
        role_scopes=pick(k),
        dependency_ids=pick(k),
        violation_ids=pick(k),
        managed_by_ids=pick(k),
    )


def _rows(ids: list[str]) -> list:
    # distinct objects even for duplicate ids, so identity-based comparison is exact
    return [SimpleNamespace(id=i, tag=idx) for idx, i in enumerate(ids)]


@pytest.mark.parametrize("seed", range(60))
def test_disappear_eligible_matches_reference(seed):
    rnd = random.Random(seed)
    n = rnd.choice([0, 1, 2, 3, 5, 10, 40, 150, 400])
    rows = _rows(_random_ids(rnd, n))
    refs = _random_refs(rnd, [r.id for r in rows]) if rnd.random() < 0.8 else drift.DisappearRefs()

    got = drift.disappear_eligible(rows, refs)
    want = _reference_folded_disappear_eligible(rows, refs)

    # exact same objects in exactly the same order
    assert [id(r) for r in got] == [id(r) for r in want]


@pytest.mark.parametrize(
    "ids",
    [
        [],
        ["/only"],
        ["/a", "/a/b"],
        ["/A", "/a/b"],  # one identity: /a/b is a child of /A
        ["/a", "/a"],  # duplicate ids are not each other's children
        ["/ab", "/a"],  # string prefix without a path separator is not a child
        ["/a/", "/a//b"],
        ["", "/x"],
        ["/a", "/a/b", "/a/b/c", "/a/b/c/d"],
    ],
)
def test_disappear_eligible_edge_cases_match_reference(ids):
    rows = _rows(ids)
    refs = drift.DisappearRefs()
    got = drift.disappear_eligible(rows, refs)
    want = _reference_folded_disappear_eligible(rows, refs)
    assert [id(r) for r in got] == [id(r) for r in want]


_SERVER = "/subscriptions/s1/resourceGroups/RG-Web/providers/Microsoft.Sql/servers/S1"
_CHILD = "/subscriptions/s1/resourceGroups/rg-web/providers/microsoft.sql/servers/s1/databases/d1"


def test_a_differently_cased_child_keeps_its_parent():
    # Given a baseline server and a child stored in a different (route) casing
    rows = _rows([_SERVER, _CHILD])
    # When eligibility is computed, Then only the child is a leaf: the parent never
    # disappears from under its live child (the DELETE cascade's identity)
    got = drift.disappear_eligible(rows, drift.DisappearRefs())
    assert [r.id for r in got] == [_CHILD]


def test_a_differently_cased_reference_protects_its_target():
    rows = _rows([_SERVER])
    refs = drift.DisappearRefs(role_scopes=frozenset({_SERVER.lower()}))
    assert drift.disappear_eligible(rows, refs) == []


@pytest.mark.parametrize("seed", range(20))
def test_casing_uniform_tenants_are_unchanged(seed, monkeypatch):
    # Every generated tenant writes each id, and each ancestor prefix, in one casing: the
    # folded rule must then pick exactly what the original raw-id rule picked.
    monkeypatch.setattr(sys.modules[__name__], "_rand_case", lambda _rnd, s: s)
    rnd = random.Random(1000 + seed)
    ids = _random_ids(rnd, rnd.choice([10, 40, 150, 400]))
    assert len({arm_id_key(i) for i in ids}) == len(set(ids)), "fixture must be uniform"
    rows = _rows(ids)
    refs = _random_refs(rnd, ids)
    got = drift.disappear_eligible(rows, refs)
    want = _reference_raw_disappear_eligible(rows, refs)
    assert [id(r) for r in got] == [id(r) for r in want]


def _tenant_ids(n: int) -> list[str]:
    """A realistic flat+nested ARM id population of ~n rows."""
    ids: list[str] = []
    i = 0
    while len(ids) < n:
        base = (
            f"/subscriptions/{uuid.UUID(int=i % 50)}/resourceGroups/rg-{i % 400}"
            f"/providers/Microsoft.Sql/servers/srv{i}"
        )
        ids.append(base)
        if i % 3 == 0:
            ids.append(f"{base}/databases/db{i}")
        i += 1
    return ids[:n]


def test_disappear_eligible_scales_linearly():
    """20K rows must finish well under a second-scale bound; the quadratic scan
    needs tens of seconds at this size."""
    rows = _rows(_tenant_ids(20_000))
    refs = drift.DisappearRefs(dependency_ids=frozenset({rows[0].id}))
    t0 = time.perf_counter()
    eligible = drift.disappear_eligible(rows, refs)
    elapsed = time.perf_counter() - t0
    assert eligible  # sanity: non-trivial result
    assert elapsed < 2.0, f"disappear_eligible took {elapsed:.2f}s for 20K rows"


# --------------------------------------------------------------------------- #
# compute_lifecycle reuses a precomputed eligible list
# --------------------------------------------------------------------------- #


def _mk(rid: str):
    return resources.Resource(
        id=rid,
        subscription_id=uuid.UUID(int=7),
        resource_group_name="rg",
        name=rid.rsplit("/", 1)[-1],
        type=resources.T_STORAGE,
        location="eastus",
        api_version="2023-01-01",
        tags={},
        properties={},
    )


def _rgs():
    ids = _tenant_ids(300)
    return [
        SimpleNamespace(
            subscription_id=uuid.UUID(int=7),
            name="rg-syn-001",
            location="eastus",
            resources=[_mk(i) for i in ids],
        )
    ]


def _ctx(seed: int = 42) -> SeededContext:
    return SeededContext(seed)


def test_compute_lifecycle_reuses_precomputed_eligible(monkeypatch):
    refs = drift.DisappearRefs()

    baseline_deltas, baseline_minted = drift.compute_lifecycle(
        _ctx(), _rgs(), refs, disappear_count=25, appear_count=3
    )

    rgs = _rgs()
    rows = [r for g in rgs for r in g.resources]
    pre = drift.disappear_eligible(rows, refs)

    def _boom(*_a, **_k):
        raise AssertionError("disappear_eligible recomputed despite eligible= being supplied")

    monkeypatch.setattr(drift, "disappear_eligible", _boom)
    deltas, minted = drift.compute_lifecycle(
        _ctx(), rgs, refs, disappear_count=25, appear_count=3, eligible=pre
    )

    assert deltas == baseline_deltas
    assert [m.id for m in minted] == [m.id for m in baseline_minted]
