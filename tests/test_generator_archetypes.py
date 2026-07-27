"""Archetype catalog + type-signature matcher unit suite (Phase 19, ARCH-01/02).

DB-free, RNG-free. Mirrors the ``tests/test_generator_misc_types.py`` header:
``type_set`` inputs are built from ``arm.canonical_type(...)`` constants so the
casing rule under test is the one governing the whole tree. No SeededContext is
needed — the matcher is a pure function of the in-memory profile.

NOTE: named ``test_generator_archetypes.py`` (not the plan's ``test_archetypes``)
because ``tests/test_archetypes.py`` already exists for the ANALYZER archetype
extractor (``tenantless.analyzer.extractors.archetypes``) — a name collision.
This file is the GENERATOR archetype suite.

Covers:
- ARCH-01: catalog well-formedness, brand-token scrub, static-only token source
  (privacy leak guard T-19-01), and the load-time lowercased ``_NORMALIZED`` index.
- ARCH-02: the precision matrix, casing-invariance, honest margin-tie fallback,
  the coverage helper, and coverage over the REAL ``enterprise.json`` templates.
"""

from __future__ import annotations

import re

import pytest

import scrub_tokens

from tenantless.generator import arm, archetypes
from tenantless.generator.profile_input import load_profile, resolve_profile

# ARM type constants routed through the ONE governing casing rule (arm.canonical_type).
STOR = arm.canonical_type("Microsoft.Storage/storageAccounts")
KV = arm.canonical_type("Microsoft.KeyVault/vaults")
VM = arm.canonical_type("Microsoft.Compute/virtualMachines")
DISK = arm.canonical_type("Microsoft.Compute/disks")
NIC = arm.canonical_type("Microsoft.Network/networkInterfaces")
PE = arm.canonical_type("Microsoft.Network/privateEndpoints")
VNET = arm.canonical_type("Microsoft.Network/virtualNetworks")
ROUTES = arm.canonical_type("Microsoft.Network/routeTables")
NSG = arm.canonical_type("Microsoft.Network/networkSecurityGroups")
NIP = arm.canonical_type("Microsoft.Network/networkIntentPolicies")
RSV = arm.canonical_type("Microsoft.RecoveryServices/vaults")
RPC = arm.canonical_type("Microsoft.Compute/restorePointCollections")
WEB = arm.canonical_type("Microsoft.Web/sites")
COMPONENTS = arm.canonical_type("Microsoft.Insights/components")
UAI = arm.canonical_type("Microsoft.ManagedIdentity/userAssignedIdentities")
DBX = arm.canonical_type("Microsoft.Databricks/workspaces")
SYN = arm.canonical_type("Microsoft.Synapse/workspaces")
SQLSRV = arm.canonical_type("Microsoft.Sql/servers")
SBUS = arm.canonical_type("Microsoft.ServiceBus/namespaces")
# Nested (child) ARM types — the D-15 child-credit inputs (Task 1, Plan 04).
SQLDB = arm.canonical_type("Microsoft.Sql/servers/databases")
VMEXT = arm.canonical_type("Microsoft.Compute/virtualMachines/extensions")
# Monitoring types that are NOT devbox evidence — the D-19.3 downgrade input.
OPINS = arm.canonical_type("Microsoft.OperationalInsights/workspaces")
METRICALERT = arm.canonical_type("Microsoft.Insights/metricAlerts")
# A nested VM child that is NOT itself a catalog strong-signal, so child-credit
# is genuinely DECISIVE for it (its parent anchor is the ONLY evidence path).
VMRUN = arm.canonical_type("Microsoft.Compute/virtualMachines/runCommands")
# Plan 08 (ARCH-GAP-02) — the tier-aware confirmation inputs.
AVSET = arm.canonical_type("Microsoft.Compute/availabilitySets")
MAINT = arm.canonical_type("Microsoft.Maintenance/maintenanceConfigurations")
DEVCENTER = arm.canonical_type("Microsoft.DevCenter/devcenters")
ACTIONGRP = arm.canonical_type("Microsoft.Insights/actionGroups")
ACTLOGALERT = arm.canonical_type("Microsoft.Insights/activityLogAlerts")
# Plan 11 (ARCH-GAP-03) — monitoring's SECOND supporting signal (so the margin pair
# can be built on monitoring, and the Databricks access connector, demoted to
# data-platform's supporting tier by 19-10 remedy 3.
SCHEDQUERY = arm.canonical_type("Microsoft.Insights/scheduledQueryRules")
ACCESSCONN = arm.canonical_type("Microsoft.Databricks/accessConnectors")
AKS = arm.canonical_type("Microsoft.ContainerService/managedClusters")
ACR = arm.canonical_type("Microsoft.ContainerRegistry/registries")

# --------------------------------------------------------------------------- #
# Reference templates for the label-map ripple pins.
#
# These pins used to run against the BUNDLED `enterprise` profile. That coupled
# a catalog regression test to whichever profile happened to ship, so replacing
# the bundled profile broke them for reasons that had nothing to do with the
# catalog — and it meant the pinned literal was a fingerprint of that profile's
# real resource-group compositions.
#
# A hand-authored set does the same job better: the pin still fails loudly when
# a catalog edit shifts a label (which is the whole point), and a reviewer can
# now see WHICH composition produces which token instead of reading opaque
# `template-19` ids. Every case the old pins covered is represented below, and
# `test_reference_templates_cover_every_archetype` keeps the set honest.
# --------------------------------------------------------------------------- #
REFERENCE_TEMPLATES: list[dict] = [
    {"id": "t-vm-workload", "type_set": [VM, DISK, NIC]},
    # Child-credit cases: nested types whose ONLY evidence path is the parent.
    {"id": "t-vm-child-credit", "type_set": [VMEXT]},
    {"id": "t-vm-runcommand", "type_set": [VMRUN]},
    {"id": "t-sql-database", "type_set": [SQLSRV, SQLDB]},
    {"id": "t-sql-child-credit", "type_set": [SQLDB]},
    {"id": "t-web-app", "type_set": [WEB, COMPONENTS]},
    {"id": "t-network-hub", "type_set": [VNET, ROUTES, NSG]},
    {"id": "t-aks-platform", "type_set": [AKS, ACR]},
    {"id": "t-data-platform", "type_set": [DBX, STOR]},
    # 19-10 remedy 3: accessConnectors demoted out of data-platform's anchors, so
    # this carries no data-platform anchor. Lands on `core`, NOT `shared`, because
    # userAssignedIdentities IS identity's anchor -- identity then fails the score
    # gate (storageAccounts is its negative signal: 2.0 - 1.5 = 0.5 < MIN_SCORE).
    # That is the D-09 split: shared = no anchor anywhere, core = named-but-unbacked.
    {"id": "t-connector-only", "type_set": [ACCESSCONN, UAI, STOR]},
    {"id": "t-devbox", "type_set": [DEVCENTER, KV]},
    {"id": "t-identity", "type_set": [UAI]},
    {"id": "t-messaging", "type_set": [SBUS]},
    {"id": "t-backup", "type_set": [RSV, MAINT]},
    {"id": "t-monitoring", "type_set": [OPINS, SCHEDQUERY]},
    # Two archetypes within < MARGIN_THRESHOLD -> honest tie, never a coin flip.
    {"id": "t-margin-tie", "type_set": [SQLSRV, SBUS]},
    {"id": "t-no-anchor", "type_set": [STOR, KV]},
    {"id": "__misc__", "type_set": ["__misc__"]},
]
for _t in REFERENCE_TEMPLATES:
    _t.setdefault("weight", round(1 / len(REFERENCE_TEMPLATES), 6))
    _t.setdefault("resource_count", {"mean": 5, "std": 2, "min": 1, "max": 10})

# ARM-shaped string validator (2-segment min; allows the 3rd-party
# ``dynatrace.observability/monitors`` form and nested ``a/b/c`` tails).
_ARM_SHAPE = re.compile(r"^[A-Za-z][A-Za-z0-9.]*/[A-Za-z0-9/._-]+$")
# Loaded from data, never spelled in this source. These were previously assembled
# from string fragments so the file would not trip the whole-tree scrub gate --
# which defeated the public/private token split, since deleting the `+` signs
# reconstructed the private word list from a public file.
_BRAND_TOKENS = scrub_tokens.all_tokens()


# --------------------------------------------------------------------------- #
# Task 1 — ARCH-01: catalog well-formedness + privacy guards
# --------------------------------------------------------------------------- #


def test_catalog_wellformed() -> None:
    assert 10 <= len(archetypes.ARCHETYPES) <= 12
    seen_ids: set[str] = set()
    for a in archetypes.ARCHETYPES:
        assert a.id and isinstance(a.id, str)
        assert a.id not in seen_ids, f"duplicate archetype id {a.id!r}"
        seen_ids.add(a.id)
        assert isinstance(a.name_tokens, tuple) and a.name_tokens
        assert all(isinstance(tok, str) and tok for tok in a.name_tokens)
        assert a.required_any, f"{a.id} has no anchors"
        for t in (*a.required_any, *a.required_all, *a.strong_signals, *a.negative_signals):
            assert _ARM_SHAPE.match(t), f"{a.id}: {t!r} is not ARM-shaped"


def test_no_brand_tokens() -> None:
    assert _BRAND_TOKENS, "no scrub tokens configured -- refusing a vacuous pass"
    for a in archetypes.ARCHETYPES:
        blob = " ".join((a.id, *a.name_tokens)).lower()
        for brand in _BRAND_TOKENS:
            assert brand not in blob, f"brand token {brand!r} leaked into {a.id!r}"


def test_tokens_are_static_only() -> None:
    """The derivable workload-token universe is the static catalog + fallbacks ONLY.

    Privacy leak guard (T-19-01): a token can NEVER echo a ``type_set``/profile
    string. The universe of tokens the matcher may emit is, by construction, the
    catalog ``name_tokens`` joins plus the two fallback constants — and it is
    structurally disjoint from every raw ARM type string in the profile (ARM
    types always contain a ``/`` and a ``microsoft.`` namespace; tokens never do).
    """
    universe = {"-".join(a.name_tokens) for a in archetypes.ARCHETYPES} | {
        archetypes.TOKEN_SHARED,
        archetypes.TOKEN_CORE,
    }
    for tok in universe:
        assert "/" not in tok
        assert "microsoft." not in tok.lower()
    profile = load_profile(resolve_profile("enterprise"))
    all_type_strings = {
        t.lower()
        for tm in profile["resource_group_templates"]
        for t in (tm.get("type_set") or [])
    }
    assert universe.isdisjoint(all_type_strings)


def test_normalized_index_lowercased() -> None:
    strings: list[str] = []
    for entry in archetypes._NORMALIZED:
        strings.extend(entry.anchors)
        strings.extend(entry.required_all)
        strings.extend(entry.strong)
        strings.extend(entry.negative)
    assert strings  # non-empty
    for s in strings:
        assert s == s.lower(), f"{s!r} is not fully lowercased"
        assert "Microsoft." not in s


# --------------------------------------------------------------------------- #
# Plan 07 Task 1 — ARCH-GAP-02 remedy 1/2/3: the evidence tiers
# (anchor / supporting / generic) + the mechanical full-catalog tiering audit.
# --------------------------------------------------------------------------- #


def _other_anchors(entry: archetypes._Normalized) -> frozenset[str]:
    """Union of every OTHER catalog entry's normalized anchors."""
    return frozenset().union(
        *(n.anchors for n in archetypes._NORMALIZED if n.archetype.id != entry.archetype.id)
    )


def test_ubiquitous_signals_declared() -> None:
    """The five Azure types that carry no archetype-discriminating power."""
    assert len(archetypes.UBIQUITOUS_SIGNALS) == 5
    assert set(archetypes.UBIQUITOUS_SIGNALS) == {
        "Microsoft.Storage/storageAccounts",
        "Microsoft.KeyVault/vaults",
        "Microsoft.Insights/components",
        "Microsoft.Insights/actionGroups",
        "Microsoft.ManagedIdentity/userAssignedIdentities",
    }
    # load-time normalized view (the identity.py derived-index pattern)
    assert archetypes._UBIQUITOUS == archetypes._norm(archetypes.UBIQUITOUS_SIGNALS)
    assert isinstance(archetypes._UBIQUITOUS, frozenset)
    for s in archetypes._UBIQUITOUS:
        assert s == s.lower()


def test_tiering_invariant_full_catalog() -> None:
    """ARCH-GAP-02 remedy 3 — audit EVERY archetype, not a hand-listed subset.

    Bidirectional by design: a ubiquitous type (or another archetype's anchor)
    can never be *promoted* into ``supporting``, and a discriminative signal can
    never be *dumped* into ``generic`` to dodge the rule. A future archetype is
    audited automatically because this iterates the catalog itself.
    """
    for entry in archetypes._NORMALIZED:
        aid = entry.archetype.id
        others = _other_anchors(entry)

        # (a) no ubiquitous type may SUPPORT any claim (remedy 3)
        assert not (entry.supporting & archetypes._UBIQUITOUS), (
            f"{aid}: ubiquitous type in supporting tier: "
            f"{sorted(entry.supporting & archetypes._UBIQUITOUS)}"
        )
        # (b) another archetype's anchor may never SUPPORT this one
        assert not (entry.supporting & others), (
            f"{aid}: another archetype's anchor in supporting tier: "
            f"{sorted(entry.supporting & others)}"
        )
        # (c) the reverse forcing rule: everything in generic EARNED that tier
        for t in entry.generic:
            assert t in archetypes._UBIQUITOUS or t in others, (
                f"{aid}: {t!r} is discriminative but was dumped into generic"
            )
        # (d) the tiers are disjoint from each other and from own anchors
        assert not (entry.supporting & entry.generic), f"{aid}: tiers overlap"
        assert not (entry.supporting & entry.anchors), f"{aid}: anchor in supporting"
        assert not (entry.generic & entry.anchors), f"{aid}: anchor in generic"


def test_anchor_tier_ubiquitous_overlap_is_declared() -> None:
    """CR-03 — the anchor tier was the tiering invariant's blind spot.

    ``test_tiering_invariant_full_catalog`` audits the supporting and generic
    tiers only, so a ubiquitous type sitting in ``required_any`` slipped through
    entirely: ``confirm_token("monitoring", [Insights/actionGroups])`` returned
    ``"monitoring"`` on 85 of 108 live monitoring RGs, the verbatim ARCH-GAP-02
    shape the module docstring called structurally impossible.

    The operator's ruling (2026-07-20) is that this is NOT a false claim and must
    NOT be renamed: ubiquity means "not discriminative as BORROWED evidence", not
    "semantically meaningless". A lone action group is thin monitoring, but it is
    monitoring. ``actionGroups`` may therefore remain monitoring's OWN anchor.

    What was actually wrong is that the overlap was SILENT. This invariant makes
    it explicit and audited: every ``anchors & UBIQUITOUS`` pair must be declared
    in the catalog WITH a rationale, and every declaration must correspond to a
    real overlap. A future archetype that quietly anchors on a ubiquitous type
    fails here until its author writes down why.
    """
    declared = archetypes.DECLARED_UBIQUITOUS_ANCHORS
    actual: set[tuple[str, str]] = set()

    for entry in archetypes._NORMALIZED:
        aid = entry.archetype.id
        for t in sorted(entry.anchors & archetypes._UBIQUITOUS):
            actual.add((aid, t))
            assert (aid, t) in declared, (
                f"{aid}: anchor {t!r} is a UBIQUITOUS signal but is not declared "
                f"in DECLARED_UBIQUITOUS_ANCHORS. Ubiquity does not forbid an "
                f"archetype from owning the type as its OWN anchor — but the "
                f"overlap must be written down with a rationale, not silent."
            )

    # No stale declarations: a rationale for an overlap that no longer exists is
    # rot that would silently pre-authorize a future re-introduction.
    assert set(declared) == actual, (
        f"DECLARED_UBIQUITOUS_ANCHORS is stale: "
        f"declared-but-absent={sorted(set(declared) - actual)}, "
        f"present-but-undeclared={sorted(actual - set(declared))}"
    )

    # A declaration with an empty rationale is a rubber stamp, not a decision.
    for key, rationale in declared.items():
        assert isinstance(rationale, str) and len(rationale.strip()) >= 20, (
            f"{key}: rationale must be a real sentence explaining why this "
            f"archetype OWNS a ubiquitous type as its anchor, got {rationale!r}"
        )


def test_ubiquitous_anchor_never_confirms_a_borrowing_archetype() -> None:
    """The half of CR-03 that stays forbidden.

    A ubiquitous type may confirm the archetype that DECLARES it as an anchor
    (operator ruling), and nothing else. This pins the distinction so a future
    change cannot widen "monitoring owns actionGroups" into "actionGroups is
    evidence for anything monitoring-adjacent".
    """
    for entry in archetypes._NORMALIZED:
        aid = entry.archetype.id
        owned = {t for (a, t) in archetypes.DECLARED_UBIQUITOUS_ANCHORS if a == aid}
        for t in sorted(archetypes._UBIQUITOUS - owned):
            token = entry.archetype.name_tokens
            tok = "-".join(token) if isinstance(token, tuple) else token
            assert archetypes.confirm_token(tok, [t]) != tok, (
                f"{aid}: ubiquitous {t!r} confirmed {tok!r} without being that "
                f"archetype's declared anchor — borrowed-evidence path re-opened"
            )


def test_supporting_tiers_pairwise_disjoint() -> None:
    """Precondition for 19-08's runner-up margin arithmetic.

    A type sitting in two archetypes' supporting tiers would inflate an
    archetype's hit count AND its runner-up's simultaneously, making the margin
    meaningless. Holds for all 12 today. If a future archetype legitimately needs
    to share a supporting signal this test SHOULD fail — it is the forcing
    function that makes the author revisit the margin rule in the same change.
    """
    entries = list(archetypes._NORMALIZED)
    for i, a in enumerate(entries):
        for b in entries[i + 1 :]:
            shared = a.supporting & b.supporting
            assert not shared, (
                f"{a.archetype.id} and {b.archetype.id} share supporting signals: "
                f"{sorted(shared)}"
            )


def test_devbox_is_anchor_only() -> None:
    """ARCH-GAP-02 remedy 2 — the explicit, named pin.

    devbox-platform is structurally anchor-only: its entire former strong_signals
    set was ubiquitous, so the supporting tier is EMPTY. (The confirmation RULE
    that consumes this lands in 19-08; this plan pins the DATA.)
    """
    devbox = [a for a in archetypes.ARCHETYPES if a.id == "devbox-platform"][0]
    assert devbox.supporting_signals == ()
    assert set(devbox.generic_signals) == {
        "Microsoft.KeyVault/vaults",
        "Microsoft.Storage/storageAccounts",
        "Microsoft.Insights/actionGroups",
    }


def test_archetype_strong_signals_is_tier_union_property() -> None:
    """``strong_signals`` survives as a read-only property = supporting + generic,
    which is WHY the scorer's arithmetic cannot move (score neutrality)."""
    for a in archetypes.ARCHETYPES:
        assert a.strong_signals == a.supporting_signals + a.generic_signals


# --------------------------------------------------------------------------- #
# Plan 10 Task 1 — ARCH-GAP-03 remedy 1: the ROLE-ANCHOR invariant.
#
# A token naming an architectural ROLE (hub / platform / cluster / workspace /
# db / app / workload) claims a specific structure, and ONLY that structure's
# defining resource (the archetype's anchor) can prove it. Domain-adjacent
# support resources establish the DOMAIN but never the ROLE — the class-level
# statement of "109 of 213 network-hub RGs hold no virtualNetworks".
#
# Every assertion below iterates ``ARCHETYPES``: no hand-listed subset and no
# catalog-size literal, so a FUTURE role-token archetype inherits the rule
# automatically instead of relying on a code review to catch it.
# --------------------------------------------------------------------------- #


def _mk(**kw) -> archetypes.Archetype:
    """Build a throwaway Archetype for policy tests (never added to the catalog)."""
    base = dict(id="probe", name_tokens=("probe",), required_any=("Microsoft.X/probes",))
    base.update(kw)
    return archetypes.Archetype(**base)


def test_archetype_requires_explicit_confirmation_policy() -> None:
    """WR-01, THE CLASS FIX: there is NO default confirmation policy.

    The old guard was a 7-word ``ROLE_NOUNS`` vocabulary list, so an archetype
    named ``("network","gateway")`` / ``("security","firewall")`` — semantically
    the identical ARCH-GAP-03 failure — inherited a permissive default and shipped
    with every test green. A vocabulary list can only catch vocabulary it already
    knows, which is exactly the wrong shape for a catalog OSS contributors extend.

    Omission is now a construction-time TypeError: the semantic contract must be
    chosen consciously before an entry can exist at all.
    """
    with pytest.raises(TypeError):
        _mk()  # no confirmation= → cannot be built
    # ...and the same entry WITH a policy constructs fine.
    assert _mk(confirmation=archetypes.ConfirmationPolicy.ANCHOR_REQUIRED)


def test_catalog_every_entry_declares_a_policy() -> None:
    """Catalog validation ITERATES EVERY ENTRY — no token-vocabulary heuristic.

    Today's 11 entries and every future one are audited the same way, so a new
    archetype inherits the rule without relying on a code review to catch it.
    """
    assert archetypes.ARCHETYPES, "empty catalog — this gate would pass vacuously"
    for a in archetypes.ARCHETYPES:
        assert isinstance(a.confirmation, archetypes.ConfirmationPolicy), (
            f"{a.id!r} does not declare a ConfirmationPolicy"
        )
    # The validator itself must accept the shipped catalog.
    archetypes.validate_catalog()


def test_anchor_required_never_confirms_on_supporting_alone() -> None:
    """For EVERY ANCHOR_REQUIRED entry: all its supporting signals, no anchor,
    must NOT confirm. This is ARCH-GAP-03 audited catalog-wide by policy rather
    than by token spelling."""
    checked = 0
    for entry in archetypes._NORMALIZED:
        if entry.archetype.confirmation is not archetypes.ConfirmationPolicy.ANCHOR_REQUIRED:
            continue
        checked += 1
        materialized = entry.supporting  # every supporting signal, zero anchors
        assert not archetypes._confirms(entry, materialized), (
            f"{entry.archetype.id!r} is ANCHOR_REQUIRED but confirmed on supporting "
            "signals alone — the role gate is not closed"
        )
    assert checked, "no ANCHOR_REQUIRED entry in the catalog — gate is vacuous"


def test_supporting_allowed_set_is_pinned() -> None:
    """T-19-08 bound, expressed as an explicit set instead of a token heuristic.

    If every archetype became ANCHOR_REQUIRED the 19-08 margin rule would be
    unreachable dead code that "passes" because it never runs. Pin exactly which
    archetypes keep the signal-only path, and prove each still confirms on its own
    supporting evidence.

    ``messaging`` and ``identity`` were removed from this set on 2026-07-26: both
    declared SUPPORTING_ALLOWED with an EMPTY supporting tier, so the signal path
    could never run and the policy was advertising a capability the entry did not
    have. They are anchor-only in fact and now say so. Behaviour is unchanged —
    ``hits=0 < MIN_SUPPORTING_SIGNALS`` returned exactly what the role gate returns.
    """
    allowed = {
        a.id for a in archetypes.ARCHETYPES
        if a.confirmation is archetypes.ConfirmationPolicy.SUPPORTING_ALLOWED
    }
    assert allowed == {"backup", "monitoring"}, allowed
    for entry in archetypes._NORMALIZED:
        if entry.archetype.id not in allowed:
            continue
        # Its own anchor still confirms it (capability archetypes preserved).
        assert archetypes._confirms(entry, entry.anchors)


def test_supporting_allowed_requires_a_written_rationale() -> None:
    """SUPPORTING_ALLOWED is the one policy that lets a NAME stand without its
    defining resource — the exact shape of ARCH-GAP-03. It cannot be chosen
    silently: the entry must carry a written justification for why supporting
    signals make the claimed name honest without an anchor.

    This proves nothing about whether the justification is CORRECT — no test can.
    It forces the risky decision into the diff where a reviewer can challenge it.
    """
    with pytest.raises(ValueError, match="rationale"):
        _mk(
            supporting_signals=("Microsoft.X/a", "Microsoft.X/b"),
            confirmation=archetypes.ConfirmationPolicy.SUPPORTING_ALLOWED,
        )
    # Placeholder text is not a rationale.
    for junk in ("", "   ", "TODO", "n/a", "N/A.", "tbd", "see above"):
        with pytest.raises(ValueError, match="rationale"):
            _mk(
                supporting_signals=("Microsoft.X/a", "Microsoft.X/b"),
                confirmation=archetypes.ConfirmationPolicy.SUPPORTING_ALLOWED,
                supporting_allowed_rationale=junk,
            )


def test_supporting_allowed_requires_a_non_empty_supporting_tier() -> None:
    """A SUPPORTING_ALLOWED entry with NO supporting signals is mislabelled: the
    signal path it claims to allow cannot ever run, so the entry is anchor-only in
    fact while advertising otherwise. Forbid the lie rather than let a contributor
    write a rationale for a path that does not exist."""
    with pytest.raises(ValueError, match="supporting"):
        _mk(
            supporting_signals=(),
            confirmation=archetypes.ConfirmationPolicy.SUPPORTING_ALLOWED,
            supporting_allowed_rationale=(
                "A perfectly well-written rationale that cannot rescue an entry "
                "whose supporting tier is empty."
            ),
        )


def test_other_policies_reject_the_rationale_field() -> None:
    """The field is meaningful ONLY under SUPPORTING_ALLOWED. Allowing a stray
    rationale elsewhere would let it rot into decoration that reviewers stop
    reading."""
    for policy in (
        archetypes.ConfirmationPolicy.ANCHOR_REQUIRED,
        archetypes.ConfirmationPolicy.GENERIC,
    ):
        kw = dict(confirmation=policy,
                  supporting_allowed_rationale="Not applicable to this policy.")
        if policy is archetypes.ConfirmationPolicy.GENERIC:
            kw.update(name_tokens=(archetypes.TOKEN_CORE,), required_any=())
        with pytest.raises(ValueError, match="rationale"):
            _mk(**kw)


def test_every_supporting_allowed_catalog_entry_has_a_rationale() -> None:
    """Audited by ITERATING the catalog — today's entries and every future one."""
    checked = 0
    for a in archetypes.ARCHETYPES:
        if a.confirmation is not archetypes.ConfirmationPolicy.SUPPORTING_ALLOWED:
            assert a.supporting_allowed_rationale is None, a.id
            continue
        checked += 1
        assert a.supporting_allowed_rationale, a.id
        assert len(a.supporting_allowed_rationale.strip()) >= 60, (
            f"{a.id!r}: rationale is too thin to have been thought about"
        )
        assert a.supporting_signals, a.id
    assert checked, "no SUPPORTING_ALLOWED entry — this gate would pass vacuously"


def test_rationale_is_metadata_only_and_cannot_affect_generation() -> None:
    """The rationale documents a decision; it must never participate in one.

    Asserted structurally: no function on the generation path so much as mentions
    the field, and the load-time `_Normalized` view does not carry it. So no
    wording change can move a single generated name.
    """
    import inspect

    for fn in (
        archetypes._confirms,
        archetypes.confirm_token_detail,
        archetypes.match_template,
        archetypes.build_label_map,
        archetypes._normalize,
    ):
        src = inspect.getsource(fn)
        assert "supporting_allowed_rationale" not in src, fn.__name__
    assert "supporting_allowed_rationale" not in archetypes._Normalized._fields


def test_generic_policy_never_over_claims() -> None:
    """A GENERIC archetype claims nothing, so it can never over-claim.

    Enforced in both directions: ``_confirms`` refuses a GENERIC entry outright,
    and construction refuses a GENERIC entry that declares a semantic token or an
    anchor (which would be a claim wearing a generic label).
    """
    generic = _mk(
        id="generic-probe",
        name_tokens=(archetypes.TOKEN_CORE,),
        required_any=(),
        confirmation=archetypes.ConfirmationPolicy.GENERIC,
    )
    norm = archetypes._normalize(generic)
    assert not archetypes._confirms(norm, archetypes._norm(["Microsoft.X/probes"]))
    assert not archetypes._confirms(norm, frozenset())
    with pytest.raises(ValueError):  # semantic token under a GENERIC policy
        _mk(name_tokens=("web-app",), required_any=(),
            confirmation=archetypes.ConfirmationPolicy.GENERIC)
    with pytest.raises(ValueError):  # an anchor is evidence for a claim it cannot make
        _mk(name_tokens=(archetypes.TOKEN_SHARED,),
            required_any=("Microsoft.X/probes",),
            confirmation=archetypes.ConfirmationPolicy.GENERIC)


def test_network_hub_is_anchor_required() -> None:
    """Remedy 1, the named pin: a hub is defined by the VNet it hubs.

    ``rg-eng-uat-network-hub-56`` (1 network-intent policy, 2 NSGs, 6 route
    tables) proves *networking* — not *hub*. The 5 supporting signals stay (they
    are real, discriminative networking evidence and still feed the D-05 score),
    but they can no longer CONFIRM the hub claim on their own.
    """
    entry = archetypes._BY_TOKEN["network-hub"]
    assert entry.anchor_required is True
    assert archetypes._norm([VNET]) <= entry.anchors


def test_anchor_required_archetypes_declare_an_anchor() -> None:
    """An anchor-required archetype with no anchor is unconfirmable by
    construction — a silent dead token. Forbid it catalog-wide."""
    for entry in archetypes._NORMALIZED:
        if not entry.anchor_required:
            continue
        assert entry.anchors, (
            f"{entry.archetype.id!r} is anchor_required but declares no anchor — "
            "it could never be confirmed by anything"
        )


def test_anchor_required_is_derived_from_the_policy() -> None:
    """``anchor_required`` survives ONLY as a projection of the policy, so the
    19-12 audit's ``requires_anchor()`` predicate keeps reading the catalog
    directly (T-19-11) with zero audit edits."""
    for a in archetypes.ARCHETYPES:
        expected = a.confirmation is archetypes.ConfirmationPolicy.ANCHOR_REQUIRED
        assert a.anchor_required is expected, a.id
    for entry in archetypes._NORMALIZED:
        assert entry.anchor_required is entry.archetype.anchor_required


# --------------------------------------------------------------------------- #
# Plan 10 Task 2 — ARCH-GAP-03 remedy 3: a lone access CONNECTOR is not a
# data platform. 4 of the 5 surviving data-platform RGs were anchored only by
# Microsoft.Databricks/accessConnectors (rg-ops-prod-data-platform-72:
# accessconnectors + userassignedidentities).
# --------------------------------------------------------------------------- #


def test_access_connector_is_supporting_not_anchor() -> None:
    """Remedy 3: accessConnectors corroborates a data platform, never constitutes one."""
    entry = archetypes._BY_TOKEN["data-platform"]
    connector = arm.canonical_type("Microsoft.Databricks/accessConnectors").lower()
    assert connector not in entry.anchors, "an access connector still anchors data-platform"
    assert connector in entry.supporting
    # the anchors are exactly the three types that ARE a data platform
    assert entry.anchors == archetypes._norm(
        [
            "Microsoft.Databricks/workspaces",
            "Microsoft.Synapse/workspaces",
            "Microsoft.DataFactory/factories",
        ]
    )


# --------------------------------------------------------------------------- #
# Task 2 — ARCH-02: signature matcher + label map + coverage helper
# --------------------------------------------------------------------------- #


def test_precision_matrix() -> None:
    """Exact token per representative composition, asserted by type_set CONTENT.

    Biased to precision (D-07): an anchor MUST be present before a specialized
    claim; storage/App-Insights/restorePointCollections alone stay generic.
    """
    cases: list[tuple[list[str], str]] = [
        ([STOR], "shared"),  # storage-only → never web/app
        ([RPC], "shared"),  # poster-child: restorePointCollections-only ≠ backup
        ([COMPONENTS, STOR], "shared"),  # App Insights + storage ≠ web-app (D-07)
        ([WEB, COMPONENTS, NIC, PE], "web-app"),  # anchor present ⇒ confident
        ([RSV], "backup"),  # anchor-alone qualifies
        (
            [DBX, SYN, VNET, NSG, NIC, PE, NIP],
            "data-platform",  # network-hub negative-signal override
        ),
        ([VNET, ROUTES, NSG], "network-hub"),  # clean hub match
        ([UAI, STOR], "core"),  # identity anchor killed by storage negative → core
        (["__misc__"], "shared"),  # D-06 sentinel
    ]
    for type_set, expected in cases:
        got = archetypes.match_template(type_set)
        assert got == expected, f"{type_set} → {got!r}, expected {expected!r}"


def test_casing_invariance() -> None:
    """Profile casing (Microsoft.web/sites) and canonical (Microsoft.Web/sites)
    yield an identical token."""
    profile_case = ["Microsoft.web/sites", "Microsoft.insights/components"]
    canonical_case = ["Microsoft.Web/sites", "Microsoft.Insights/components"]
    assert (
        archetypes.match_template(profile_case)
        == archetypes.match_template(canonical_case)
        == "web-app"
    )


def test_margin_tie_to_generic() -> None:
    """Two archetypes within < MARGIN_THRESHOLD resolve to ``core``, never a
    coin-flip. sql-database (anchor sql/servers) and messaging (anchor
    servicebus) both score exactly ANCHOR_WEIGHT here → tie → core."""
    tie = [SQLSRV, SBUS]
    assert archetypes.match_template(tie) == "core"


def test_reference_templates_cover_every_archetype() -> None:
    """Non-vacuity floor for the pins below.

    If the reference set stopped exercising an archetype, the pinned map would
    still pass while silently testing less. Every catalog entry must be reachable
    from it, plus both generic outcomes.
    """
    label_map = archetypes.build_label_map(REFERENCE_TEMPLATES)
    produced = set(label_map.values())

    missing = {e.name_tokens[0] for e in archetypes.ARCHETYPES} - {
        tok.split("-")[0] for tok in produced
    }
    assert not missing, f"reference templates never produce: {sorted(missing)}"
    assert {"shared", "core"} <= produced, "both generic outcomes must be exercised"


def test_label_map_is_pinned_over_reference_templates() -> None:
    """Ripple bound: a catalog edit that shifts a label fails loudly here.

    The literal below is a PIN, not a recomputation. If a change to the catalog
    moves a template's token, this test must fail and the shift must be
    enumerated deliberately -- never re-baselined to whatever the code now emits.
    """
    expected = {
        "t-vm-workload": "vm-workload",
        "t-vm-child-credit": "vm-workload",
        "t-vm-runcommand": "vm-workload",
        "t-sql-database": "sql-db",
        "t-sql-child-credit": "sql-db",
        "t-web-app": "web-app",
        "t-network-hub": "network-hub",
        "t-aks-platform": "aks-platform",
        "t-data-platform": "data-platform",
        "t-connector-only": "core",
        "t-devbox": "devbox",
        "t-identity": "identity",
        "t-messaging": "messaging",
        "t-backup": "backup",
        "t-monitoring": "monitoring",
        "t-margin-tie": "core",
        "t-no-anchor": "shared",
        "__misc__": "shared",
    }
    assert archetypes.build_label_map(REFERENCE_TEMPLATES) == expected


def test_bundled_profile_labels_are_well_formed() -> None:
    """Profile-agnostic check over whatever profile actually ships.

    Deliberately asserts INVARIANTS rather than a pinned map: the bundled profile
    is data, and pinning its labels made a catalog test fail whenever the data
    changed. What must hold for any profile is that every template gets a label,
    every label is one the catalog can produce, and the sentinel stays generic.
    """
    profile = load_profile(resolve_profile("enterprise"))
    templates = profile["resource_group_templates"]
    label_map = archetypes.build_label_map(templates)

    assert set(label_map) == {t["id"] for t in templates}, "every template needs a label"

    legal = {tok.split("-")[0] for tok in archetypes.build_label_map(REFERENCE_TEMPLATES).values()}
    for tid, tok in label_map.items():
        assert tok.split("-")[0] in legal, f"{tid} got unknown token {tok!r}"

    if "__misc__" in label_map:
        assert label_map["__misc__"] in _GENERIC, "the misc sentinel must never claim a workload"

    # Non-vacuity: a profile whose templates ALL went generic would pass every
    # assertion above while proving the matcher never fires.
    semantic = {t for t in label_map.values() if t not in _GENERIC}
    assert semantic, "no template earned a semantic label -- the matcher is not firing"


def test_coverage_helper() -> None:
    label_map = {"t0": "backup", "t1": "backup", "t2": "shared", "t3": "web-app"}
    cov = archetypes.archetype_coverage(label_map, ["t0", "t1", "t2", "t3"])
    assert cov["backup"] == 2
    assert cov["shared"] == 1
    assert cov["web-app"] == 1
    # subset of ids also works
    assert archetypes.archetype_coverage(label_map, ["t0", "t3"]) == {
        "backup": 1,
        "web-app": 1,
    }


# --------------------------------------------------------------------------- #
# Plan 04 Task 1 — D-15 child-type crediting folded into matcher input
# --------------------------------------------------------------------------- #


def test_credit_children_adds_parent_type_level_ancestor() -> None:
    """A ``<parent>/<child>`` type credits its ``count('/')==1`` ancestor + itself."""
    credited = archetypes._credit_children(frozenset({SQLDB}))
    assert SQLSRV in credited  # microsoft.sql/servers (the count('/')==1 ancestor)
    assert SQLDB in credited  # the original child is retained


def test_credit_children_vm_extension() -> None:
    credited = archetypes._credit_children(frozenset({VMEXT}))
    assert VM in credited  # microsoft.compute/virtualmachines


def test_credit_children_stops_at_type_level_not_namespace() -> None:
    """Child-credit never adds the bare ``microsoft.<ns>`` (count('/')==0)."""
    credited = archetypes._credit_children(frozenset({SQLDB}))
    assert "microsoft.sql" not in credited
    # a deeper 3-segment tail credits BOTH intermediate levels, still not the ns
    deep = archetypes._credit_children(frozenset({"microsoft.compute/galleries/images/versions"}))
    assert "microsoft.compute/galleries/images" in deep
    assert "microsoft.compute/galleries" in deep
    assert "microsoft.compute" not in deep


def test_credit_children_is_pure_frozenset() -> None:
    out = archetypes._credit_children(frozenset({STOR}))
    assert isinstance(out, frozenset)
    assert out == frozenset({STOR})  # non-nested input unchanged


def test_match_template_child_credits_sql_db() -> None:
    """A SQL-child-only input confirms sql-db via parent credit (D-15/D-19.4)."""
    assert archetypes.match_template([SQLDB]) == "sql-db"


def test_match_template_child_credits_vm_workload() -> None:
    assert archetypes.match_template([VMEXT]) == "vm-workload"


def test_child_credit_resolves_nested_types_to_parent_token() -> None:
    """WARNING-2 ripple bound: child-credit wired into build_label_map.

    A nested type must resolve to its parent's anchored token. The child-only
    templates are the sharp cases -- their parent anchor is the ONLY evidence
    path, so if child-credit regressed they would fall to a generic token.
    """
    label_map = archetypes.build_label_map(REFERENCE_TEMPLATES)
    assert label_map["t-vm-child-credit"] == "vm-workload"
    assert label_map["t-vm-runcommand"] == "vm-workload"
    assert label_map["t-sql-child-credit"] == "sql-db"
    # And a template holding the parent alongside the child is unchanged by it.
    assert label_map["t-sql-database"] == "sql-db"
    assert label_map["t-vm-workload"] == "vm-workload"


# --------------------------------------------------------------------------- #
# Plan 07 Task 2 — the tier split is SCORE-NEUTRAL (zero template-label ripple)
# --------------------------------------------------------------------------- #


def test_tier_split_preserves_strong_union() -> None:
    """The structural reason the D-05 scorer cannot move.

    ``match_template`` scores with ``STRONG_SIGNAL_WEIGHT * len(s & entry.strong)``.
    Because ``strong`` is still the full union of the two new tiers (and the tiers
    are disjoint), the term is arithmetically identical to pre-tiering for EVERY
    possible input type_set — not merely for the cases the suite happens to sample.
    """
    for entry in archetypes._NORMALIZED:
        aid = entry.archetype.id
        assert entry.strong == entry.supporting | entry.generic, aid
        assert not (entry.supporting & entry.generic), f"{aid}: tiers overlap"


def test_tier_split_does_not_shift_any_label() -> None:
    """WARNING-2 ripple bound (the 19-04 precedent, repeated for the tier split).

    Companion to ``test_tier_split_preserves_strong_union``: that one proves the
    scorer is arithmetically unchanged for every possible input; this one pins
    the observable outcome so a future catalog edit that shifts a token fails
    loudly instead of silently re-baselining itself.

    The sharp case is ``t-connector-only``. After 19-10 remedy 3 demoted
    accessConnectors out of data-platform's ``required_any``, that template
    carries no data-platform anchor, and ``match_template`` requires one.

    Its destination is ``core``, NOT ``shared``: ``userAssignedIdentities`` IS
    the *identity* archetype's anchor, so an anchor DOES match -- identity then
    fails the score gate, because storageAccounts is its negative signal
    (2.0 - 1.5 = 0.5 < MIN_SCORE). That is exactly the D-09 split: ``shared``
    means no anchor anywhere, ``core`` means named-but-unbacked. ``core`` is the
    honest outcome and is pinned deliberately.

    ``t-data-platform`` keeps its Databricks *workspace* anchor and is unchanged.
    """
    label_map = archetypes.build_label_map(REFERENCE_TEMPLATES)

    assert label_map["t-connector-only"] == "core"
    assert label_map["t-data-platform"] == "data-platform"
    assert label_map["t-identity"] == "identity"
    assert label_map["t-no-anchor"] == "shared"
    assert label_map["t-margin-tie"] == "core"

# --------------------------------------------------------------------------- #
# Plan 04 Task 2 — D-14/D-17/D-18 confirmation gate (downgrade-only, never relabel)
# --------------------------------------------------------------------------- #

_GENERIC = (archetypes.TOKEN_SHARED, archetypes.TOKEN_CORE)


def test_confirm_downgrades_devbox_with_only_monitoring() -> None:
    """D-14/D-19.3: a devbox RG whose materialized contents are only monitoring
    bits (none of devbox's anchors/strong-signals) downgrades to a generic — it
    is NEVER relabeled to monitoring and NEVER keeps devbox."""
    tok = archetypes.confirm_token("devbox", [OPINS, METRICALERT])
    assert tok in _GENERIC
    assert tok != "devbox"
    assert tok != "monitoring"


def test_confirm_sql_db_via_child_credit() -> None:
    """D-19.4: SQL-child-only confirms sql-db via parent credit."""
    assert archetypes.confirm_token("sql-db", [SQLDB]) == "sql-db"


def test_confirm_vm_workload_via_extension() -> None:
    assert archetypes.confirm_token("vm-workload", [VMEXT]) == "vm-workload"


def test_confirm_empty_rg_is_generic() -> None:
    """D-17/D-19.2: an empty materialized set has no evidence -> generic."""
    assert archetypes.confirm_token("web-app", []) in _GENERIC
    assert archetypes.confirm_token("identity", []) in _GENERIC


def test_confirm_anchor_present() -> None:
    assert archetypes.confirm_token("backup", [RSV]) == "backup"


def test_generic_tier_signal_alone_never_confirms() -> None:
    """ARCH-GAP-02 — REPLACES ``test_confirm_strong_signal_only_confirms``.

    The old test asserted ``confirm_token("web-app", [STOR]) == "web-app"``: a lone
    storage account certifying a web app. That expectation WAS the tautology (an
    ubiquitous type is now web-app's GENERIC tier), and it is the exact shape that
    let 135/182 devbox RGs certify on ubiquitous contents. Storage may nudge a
    score; it may never CONFIRM a claim.
    """
    assert archetypes.confirm_token("web-app", [STOR]) == "core"


def test_confirm_downgrades_no_anchor_or_strong() -> None:
    """The unambiguous OR-rule negative: a devbox token whose contents carry
    none of devbox's anchors nor strong-signals downgrades (never keeps devbox)."""
    tok = archetypes.confirm_token("devbox", [VNET])
    assert tok in _GENERIC
    assert tok != "devbox"


def test_confirm_generic_passthrough_never_repromotes() -> None:
    assert archetypes.confirm_token(archetypes.TOKEN_SHARED, [WEB, STOR]) == archetypes.TOKEN_SHARED
    assert archetypes.confirm_token(archetypes.TOKEN_CORE, [WEB, STOR]) == archetypes.TOKEN_CORE


def test_confirm_detail_child_credit_decisive() -> None:
    """D-18: child_credit_decisive is True only when evidence appears ONLY after
    crediting. VM/runCommands is a nested child that is NOT itself a catalog
    strong-signal, so its parent-anchor credit is the sole evidence path -> True.
    A raw anchor (backup/RSV) confirms without crediting -> False."""
    d = archetypes.confirm_token_detail("vm-workload", [VMRUN])
    assert d.confirmed is True
    assert d.child_credit_decisive is True
    d2 = archetypes.confirm_token_detail("backup", [RSV])
    assert d2.confirmed is True
    assert d2.child_credit_decisive is False


def test_confirm_detail_lone_supporting_child_is_decisive() -> None:
    """Plan 08 SHIFT — supersedes ``test_confirm_detail_direct_signal_child_not_decisive``.

    Under 19-04's OR rule, ``Sql/servers/databases`` confirmed DIRECTLY (it is a
    catalog signal), so child-credit was not decisive. Under the tier-aware rule a
    LONE supporting signal is below ``MIN_SUPPORTING_SIGNALS`` and cannot confirm
    by itself — so crediting the ``Sql/servers`` parent anchor is now genuinely
    what tips it. The metric tracks the truth of the rule that is actually in
    force, and the truth changed with the rule.
    """
    d = archetypes.confirm_token_detail("sql-db", [SQLDB])
    assert d.confirmed is True
    assert d.child_credit_decisive is True


def test_confirm_detail_anchor_in_raw_set_not_decisive() -> None:
    """The contrast that pins the ``decisive`` semantics from the other side: when
    the RAW set already carries the anchor, crediting adds nothing, so the credit
    was not the tipping factor even though a nested child is present."""
    d = archetypes.confirm_token_detail("sql-db", [SQLSRV, SQLDB])
    assert d.confirmed is True
    assert d.child_credit_decisive is False


def test_confirm_never_relabels_to_different_archetype() -> None:
    """The core D-14 invariant across a sweep: confirm_token returns the SAME
    token or a static generic — NEVER a different semantic archetype token."""
    generic = set(_GENERIC)
    samples = [
        ("devbox", [OPINS, METRICALERT]),  # contents classify as monitoring
        ("identity", [VNET]),  # lone-vnet class (would match network-hub)
        ("web-app", [VM, DISK]),  # contents look like vm-workload
    ]
    for tok, types in samples:
        out = archetypes.confirm_token(tok, types)
        assert out == tok or out in generic, f"{tok} relabeled to {out!r}"


def test_confirm_is_deterministic() -> None:
    a = archetypes.confirm_token("sql-db", [SQLDB])
    b = archetypes.confirm_token("sql-db", [SQLDB])
    assert a == b == "sql-db"


# --------------------------------------------------------------------------- #
# Plan 08 Task 1 — ARCH-GAP-02 remedy 2/4: TIER-AWARE confirmation.
# An anchor confirms outright; the supporting tier confirms only with
# >= MIN_SUPPORTING_SIGNALS AND a >= CONFIRM_MARGIN lead over the runner-up;
# the generic tier never confirms anything.
# --------------------------------------------------------------------------- #


def test_confirm_thresholds_declared() -> None:
    """The two D-07 precision knobs exist as module constants (tunable, but pinned
    by this matrix — they are NEVER to be loosened to make a gate pass, T-19-06)."""
    assert archetypes.MIN_SUPPORTING_SIGNALS == 2
    assert archetypes.CONFIRM_MARGIN == 1


def test_confirm_devbox_requires_devcenter_anchor() -> None:
    """THE ARCH-GAP-02 HEADLINE REGRESSION — the ``rg-retail-test-devbox-29``
    offender, verbatim from the live seed-7 tenant.

    Its whole content is monitoring bits + a storage account: not one DevCenter
    type. Today it confirms as ``devbox`` (135/182 devbox RGs are this shape).
    It must downgrade to the honest generic — and never relabel to ``monitoring``,
    whose anchor it *does* carry (D-14).
    """
    tok = archetypes.confirm_token(
        "devbox",
        [
            "Microsoft.insights/actiongroups",
            "Microsoft.insights/activitylogalerts",
            "Microsoft.storage/storageaccounts",
        ],
    )
    assert tok == "core", f"devbox certified on ubiquitous contents -> {tok!r}"
    assert tok != "monitoring"


def test_confirm_devbox_lone_storage_downgrades() -> None:
    """The minimal statement of the same bug: storage may SUPPORT a match; it may
    never CONFIRM one. devbox's supporting tier is structurally empty (19-07)."""
    assert archetypes.confirm_token("devbox", [STOR]) == "core"


def test_confirm_devbox_anchor_confirms() -> None:
    """The anchor path is unchanged — a real DevCenter proves a DevCenter platform."""
    assert archetypes.confirm_token("devbox", [DEVCENTER]) == "devbox"


def test_confirm_multi_supporting_with_margin_confirms() -> None:
    """Signal-only confirmation IS possible: 2 ``backup`` supporting signals with a
    clear lead over every other archetype's supporting count is honest evidence even
    with no RecoveryServices anchor.

    RE-PINNED by Plan 19-11 (T-19-09). This assertion previously read
    ``confirm_token("network-hub", [ROUTES, NSG]) == "network-hub"`` — an RG holding
    route tables and NSGs but NO virtual network certifying as a *hub*. That
    expectation WAS ARCH-GAP-03 (109/213 network-hub RGs were exactly this shape), and
    it now lives INVERTED in ``test_confirm_network_hub_requires_vnet_anchor``.
    ``backup`` is one of the only two archetypes that can still confirm signal-only
    (with ``monitoring``), so re-pinning here keeps the margin arithmetic genuinely
    under test instead of passing via the new ``anchor_required`` short-circuit.
    """
    assert archetypes.confirm_token("backup", [MAINT, RPC]) == "backup"


def test_confirm_single_supporting_signal_downgrades() -> None:
    """One supporting signal is below MIN_SUPPORTING_SIGNALS — it cannot confirm.

    RE-PINNED by Plan 19-11 onto ``backup``: the shipped form used ``network-hub``,
    which is now ``anchor_required``, so it would have passed via the role gate
    without ever exercising the COUNT rule this test exists to prove.
    """
    assert archetypes.confirm_token("backup", [MAINT]) == "core"


def test_confirm_backup_supporting_signals_confirm() -> None:
    """Real backup-specific evidence (restore points + maintenance configs)
    confirms signal-only, with no RecoveryServices anchor."""
    assert archetypes.confirm_token("backup", [RPC, MAINT]) == "backup"


def test_confirm_backup_generic_tier_storage_downgrades() -> None:
    """Storage is backup's GENERIC tier — never evidence, however plausible."""
    assert archetypes.confirm_token("backup", [STOR]) == "core"


def test_margin_tie_between_archetypes_downgrades() -> None:
    """T-19-07 half 1 — the DELIBERATE precision-bias cost, constructible against
    the real catalog: an anchor-less "backed-up estate that is also alerted on" RG
    hits BOTH backup supporting signals (maintenanceConfigurations +
    restorePointCollections) AND both monitoring supporting signals
    (activityLogAlerts + scheduledQueryRules). hits=2, runner-up=2, margin=0: the
    evidence is genuinely ambiguous, so it resolves to the honest generic — never to
    a claim, never to a relabel. Symmetric with ``test_margin_tie_to_generic`` in the
    scorer. Do NOT "fix" this into a confirm.

    RE-PINNED by Plan 19-11 (T-19-09) off ``vm-workload``, which is now
    ``anchor_required`` and would have downgraded via the role gate — passing without
    ever reaching the runner-up scan. Verified by hand that NEITHER side's anchors
    appear in the input: backup's anchors are RecoveryServices/vaults and
    DataProtection/backupVaults; monitoring's are OperationalInsights/workspaces,
    Insights/actionGroups, AlertsManagement/actionRules, Insights/metricAlerts and
    dynatrace.observability/monitors. So this genuinely tests the MARGIN.
    """
    assert (
        archetypes.confirm_token("backup", [MAINT, RPC, ACTLOGALERT, SCHEDQUERY])
        == "core"
    )


def test_margin_dominant_evidence_still_confirms() -> None:
    """T-19-07 half 2 — the rule DISCRIMINATES rather than blanket-downgrading: the
    SAME collision minus one backup signal gives monitoring hits=2 vs backup
    runner-up=1, margin=1, and dominant evidence still wins.

    RE-PINNED by Plan 19-11 (T-19-09) off ``vm-workload`` for the same reason as the
    tie case above — and its old expectation would additionally have FLIPPED to
    ``core`` under the role gate, hiding the fact that the margin rule was no longer
    being exercised at all.
    """
    assert (
        archetypes.confirm_token("monitoring", [ACTLOGALERT, SCHEDQUERY, MAINT])
        == "monitoring"
    )


def test_confirms_respects_required_all() -> None:
    """Parity with ``match_template``'s disqualifier: an archetype can NEVER confirm
    while failing its own hard prerequisite — not even with an anchor present.

    Inert today (zero shipped archetypes declare ``required_all``), so the entry is
    FABRICATED and handed straight to ``_confirms`` — no catalog change, no
    monkeypatch. Without this gate a future entry would confirm while failing its
    own precondition: a silent tautology of exactly the ARCH-GAP-02 shape.
    """
    entry = archetypes._Normalized(
        archetype=archetypes.ARCHETYPES[0],
        anchors=frozenset({VM}),
        required_all=frozenset({KV}),  # NOT satisfied below
        strong=frozenset(),
        negative=frozenset(),
        supporting=frozenset(),
        generic=frozenset(),
    )
    assert archetypes._confirms(entry, frozenset({VM})) is False  # anchor present!
    assert archetypes._confirms(entry, frozenset({VM, KV})) is True


def test_confirm_generic_tier_never_consulted() -> None:
    """The structural statement of remedy 4: for EVERY archetype, a materialized
    set consisting solely of that archetype's generic tier never confirms it."""
    for entry in archetypes._NORMALIZED:
        if not entry.generic:
            continue
        token = "-".join(entry.archetype.name_tokens)
        got = archetypes.confirm_token(token, sorted(entry.generic))
        assert got != token, f"{token} confirmed on its generic tier alone"


def test_confirm_d19_acceptance_set_preserved() -> None:
    """D-19.2 (empty -> shared), D-19.3 (devbox+monitoring -> core), D-19.4 (child
    credit) all still hold under the tier-aware rule."""
    assert archetypes.confirm_token("web-app", []) == "shared"
    assert archetypes.confirm_token("devbox", [OPINS, METRICALERT]) == "core"
    assert archetypes.confirm_token("sql-db", [SQLDB]) == "sql-db"
    assert archetypes.confirm_token("vm-workload", [VMEXT]) == "vm-workload"


def test_confirm_never_relabels_under_tier_rule() -> None:
    """D-14 holds STRUCTURALLY under the new rule: the runner-up scan decides
    confirm-vs-downgrade ONLY — the runner-up's token is never returned."""
    generic = set(_GENERIC)
    samples = [
        ("devbox", [ACTIONGRP, ACTLOGALERT, STOR]),  # contents scream monitoring
        ("vm-workload", [DISK, NIC, RPC, MAINT]),  # runner-up backup ties
        ("backup", [ROUTES, NSG]),  # contents are network-hub's evidence
        ("web-app", [DEVCENTER]),
    ]
    for tok, types in samples:
        out = archetypes.confirm_token(tok, types)
        assert out == tok or out in generic, f"{tok} relabeled to {out!r}"


# --------------------------------------------------------------------------- #
# Plan 11 Task 1 — ARCH-GAP-03 remedy 1/3: the ROLE GATE.
# An ``anchor_required`` archetype names an architectural ROLE, and only its
# defining resource proves it — the supporting path is structurally closed, so a
# domain-correct/role-wrong claim can finally FAIL. The margin rule stays live for
# the capability archetypes that legitimately use it.
# --------------------------------------------------------------------------- #


def test_confirm_network_hub_requires_vnet_anchor() -> None:
    """THE ARCH-GAP-03 HEADLINE REGRESSION — the ``rg-eng-uat-network-hub-56``
    offender, verbatim from the live seed-7 tenant: 1 networkIntentPolicy + 2
    networkSecurityGroups + 6 routeTables, and NOT ONE virtual network.

    Today it confirms as ``network-hub``: its three supporting signals are real,
    discriminative NETWORKING evidence with a clear lead over every other archetype,
    so the 19-08 margin rule structurally cannot reject it. But networking support
    resources establish the DOMAIN, not the ROLE — a hub is defined by the VNet it
    hubs. 109 of 213 network-hub RGs (51%) held no virtual network and every gate
    returned PASS.

    The anchor path is untouched: a hub WITH a VNet is still a hub, with or without
    its supporting signals.
    """
    assert archetypes.confirm_token("network-hub", [NIP, NSG, ROUTES]) == "core"
    # the dominant anchor-less class — 94 of the 109 offending RGs
    assert archetypes.confirm_token("network-hub", [NSG, ROUTES]) == "core"
    assert archetypes.confirm_token("network-hub", [VNET]) == "network-hub"
    assert archetypes.confirm_token("network-hub", [VNET, ROUTES, NSG]) == "network-hub"


def test_confirm_data_platform_requires_workspace_anchor() -> None:
    """ARCH-GAP-03 remedy 3, rule half — the ``rg-ops-prod-data-platform-72`` shape:
    a lone ``Microsoft.Databricks/accessConnectors`` (plus a user-assigned identity)
    is NOT a data platform. A connector corroborates the claim; it cannot constitute
    it. A real Databricks or Synapse WORKSPACE anchor still confirms.

    Held by two independent mechanisms after 19-10: the connector is the entry's only
    supporting signal (below ``MIN_SUPPORTING_SIGNALS``) AND ``anchor_required=True``.
    """
    assert archetypes.confirm_token("data-platform", [ACCESSCONN, UAI]) == "core"
    assert archetypes.confirm_token("data-platform", [ACCESSCONN]) == "core"
    assert archetypes.confirm_token("data-platform", [DBX]) == "data-platform"
    assert archetypes.confirm_token("data-platform", [SYN]) == "data-platform"


def test_anchor_required_forbids_signal_only_confirmation() -> None:
    """The CLASS-level audit of the role gate (T-19-05).

    For EVERY ``anchor_required`` entry in the catalog — today's and every future
    one — presenting its ENTIRE supporting tier with no anchor confirms NOTHING,
    while any single anchor still confirms. Iterates ``_NORMALIZED``: no hand-listed
    token, no catalog-size literal, nothing skipped.
    """
    audited = 0
    for entry in archetypes._NORMALIZED:
        if not entry.anchor_required:
            continue
        audited += 1
        token = "-".join(entry.archetype.name_tokens)
        assert entry.anchors, f"{token} is anchor_required with no anchor"
        assert archetypes._confirms(entry, entry.supporting) is False, (
            f"{token} confirmed on its supporting tier alone despite anchor_required"
        )
        for anchor in sorted(entry.anchors):
            assert archetypes._confirms(entry, frozenset({anchor})) is True, (
                f"{token} failed to confirm on its own anchor {anchor!r}"
            )
    assert audited, "no anchor_required archetype in the catalog — the gate is vacuous"


def test_signal_only_path_still_live() -> None:
    """The anti-dead-code guard (T-19-08) — the ARCH-GAP-02 shape, one level up.

    If every archetype became ``anchor_required`` the supporting/margin arithmetic
    would be unreachable while every test in this file still passed. So: at least one
    NON-anchor-required entry must declare ``>= MIN_SUPPORTING_SIGNALS`` supporting
    signals, and it must actually CONFIRM on that tier alone.
    """
    live = [
        e
        for e in archetypes._NORMALIZED
        if not e.anchor_required
        and len(e.supporting) >= archetypes.MIN_SUPPORTING_SIGNALS
    ]
    assert live, "the signal-only confirmation path became dead code"
    for entry in live:
        token = "-".join(entry.archetype.name_tokens)
        assert archetypes._confirms(entry, entry.supporting) is True, (
            f"{token} should confirm signal-only but did not"
        )


# --------------------------------------------------------------------------- #
# Plan 11 Task 2 — preservation sweep. D-14 upgraded from a hand-listed sample to
# an EXHAUSTIVE pairwise catalog sweep (both forms kept).
# --------------------------------------------------------------------------- #


def test_confirm_never_relabels_full_catalog_sweep() -> None:
    """D-14 exhaustively: for EVERY ordered pair of archetypes, naming one while
    materializing the OTHER's anchors returns the input token or a static generic —
    never a different semantic token. The role gate opened no relabel path.

    Catalog-driven over ``_BY_TOKEN``: no hand-listed token, no catalog-size literal,
    so every future archetype pair is covered the day it is added.
    """
    generic = set(_GENERIC)
    tokens = sorted(archetypes._BY_TOKEN)
    swept = 0
    for token in tokens:
        for other in tokens:
            if other == token:
                continue
            materialized = sorted(archetypes._BY_TOKEN[other].anchors)
            out = archetypes.confirm_token(token, materialized)
            swept += 1
            assert out == token or out in generic, (
                f"{token} relabeled to {out!r} on {other}'s anchors"
            )
    assert swept, "the pairwise sweep covered nothing"


def test_confirm_token_output_is_static_only() -> None:
    """T-19-01 extended to confirm_token: output is always a static-catalog token
    or a generic — it can never echo a materialized ARM type string."""
    universe = {"-".join(a.name_tokens) for a in archetypes.ARCHETYPES} | set(_GENERIC)
    samples = [
        ("devbox", [OPINS, METRICALERT]),
        ("sql-db", [SQLDB]),
        ("web-app", []),
        ("backup", [RSV]),
        ("vm-workload", [VMEXT]),
        (archetypes.TOKEN_SHARED, [WEB]),
    ]
    for tok, types in samples:
        out = archetypes.confirm_token(tok, types)
        assert out in universe
        assert "/" not in out
        assert "microsoft." not in out.lower()
