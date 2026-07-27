#!/usr/bin/env python3
"""Coherence audit: is every resource-group archetype name backed by its contents?

The generator names some resource groups after an architectural *archetype*
(``rg-<env>-<app>-<token>-<n>``, where ``<token>`` is a shape like
``network-hub`` or ``data-platform``). A name like that is a CLAIM about what
the group contains. This audit reads a generated estate straight out of
Postgres and asks, for every semantic name, whether the group's materialized
contents actually back the claim. It exits non-zero if any name over-claims.

WHY A SEPARATE AUDIT, AND WHY IT IS SHAPED THE WAY IT IS
-------------------------------------------------------
The tempting version of this check gates on one number: "does every semantic
name survive the generator's own confirmation rule". That question is
TAUTOLOGICAL. It asks whether the naming pass obeyed its own inputs, never
whether those inputs were honest. A group can pass that check while holding no
resource that defines the role its name claims. So the single "unconfirmed == 0"
number is kept here only as a FLOOR, and the audit adds the metrics that make
the verdict trustworthy:

  - Anchor-less semantic groups, BROKEN DOWN BY TOKEN: a name can survive
    confirmation yet still hold none of the anchor resources that would prove
    its archetype. That breakdown is the metric the naive check lacks.
  - An explicit ceiling on argmax "cross" residuals sitting under SEMANTIC
    names: a name that claims one archetype while its contents most resemble a
    DIFFERENT archetype is a real defect, and its rate is gated, not merely
    printed.

ROLE VS DOMAIN. Some tokens name an architectural ROLE (hub, platform,
cluster, workspace, db, app, workload). Only the resource that DEFINES that
role can prove it -- domain-adjacent support resources establish the DOMAIN,
never the ROLE. A group named ``network-hub`` that holds NSGs and route tables
but no VNet has proven "networking", not "hub", and its name over-claims. The
role-aware gate keys on the catalog's own ``anchor_required`` flag, so
tightening an archetype in the catalog automatically tightens this gate with no
edit here.

NON-VACUITY. Every gate below is a ``== 0`` or ``<= X%`` condition, so ALL of
them are VACUOUSLY TRUE against an EMPTY estate -- run the audit against a
tenant that was never generated (or one truncated by a test run) and it prints
a confident all-green banner over ZERO groups. A gate that cannot fail proves
nothing. The audit therefore asserts it is actually looking at a materialized
tenant, AND that it actually examined some anchor-required (role) claims,
before it is allowed to certify one. Those floors are on EVIDENCE VOLUME, not
precision: raising them cannot launder a defect; lowering them to zero re-opens
the vacuous pass.

NONE of the thresholds below may be loosened to obtain a pass, and no
``anchor_required`` flag may be cleared for one. A failing gate is a finding to
report, not a number to tune.

WHAT THE GATES CERTIFY
  - non-vacuous estate       : some semantic groups were actually audited
                               (>= MIN_SEMANTIC_RGS_AUDITED).
  - role-gate evidence floor : some role (anchor-required) claims were actually
                               audited (>= MIN_ANCHOR_REQUIRED_RGS_AUDITED), so
                               the role gate below is not vacuously satisfied.
  - unconfirmed_semantic_rgs : a semantic name whose materialized contents fail
                               the confirmation rule. Must be 0 (a FLOOR).
  - empty_semantic_rgs       : a semantic name over an empty group -- an
                               over-claim by definition. Must be 0.
  - anchorless (role tokens) : a group carrying a token whose archetype REQUIRES
                               its anchor, yet holding no anchor. Must be 0.
  - semantic_cross_pct       : argmax-cross share under SEMANTIC names.
                               Must be <= MAX_SEMANTIC_CROSS_PCT.
  - undeclared ubiquitous    : a group whose only anchor hit is a ubiquitous
                               type the archetype does not declare as its own.
                               Must be 0.

The confirmation rule is the shipped one (``archetypes.confirm_token``), not the
argmax view: a name is honest iff the child-credited materialized set hits that
archetype's anchor or a strong signal. The generator enforces this same rule at
naming time, so a cleanly generated tenant audits clean here by construction.

INFORMATIONAL (the argmax view, kept for continuity -- it asks "what do the
contents look MOST like", a strictly different question from "do the contents
back this claim"):
  - AGREE    : match_template(actual types) == name token
  - thin->gen: match_template(actual types) in {shared, core}
  - CROSS    : match_template(actual types) is a DIFFERENT semantic token
A residual argmax-cross under a GENERIC name is acceptable -- a generic name
never over-claims. Only a SEMANTIC name that fails confirmation is a defect.

Run:
    uv run python scripts/audit_rg_coherence.py
    uv run python scripts/audit_rg_coherence.py --database-url postgres://...
"""

from __future__ import annotations

import argparse
import collections
import os
import sys
from types import SimpleNamespace

import psycopg
from tenantless.generator import archetypes

DEFAULT_DSN = os.environ.get(
    "DATABASE_URL",
    "postgres://tenantless:tenantless_dev@localhost:5433/tenantless",
)
GENERIC = {"shared", "core"}

# EXPLICIT ceiling on argmax-cross residuals sitting under SEMANTIC names. A
# cross under a GENERIC name is never a defect -- a generic name claims nothing
# -- so only the semantic share is gated. DO NOT RAISE THIS TO MAKE A GATE PASS:
# it is the number a reviewer trusts, and laundering it silently re-creates the
# exact over-claim this audit exists to catch.
MAX_SEMANTIC_CROSS_PCT = 1.0

# NON-VACUITY FLOOR on evidence volume. Every gate is a "== 0" / "<= X%"
# condition, so all of them are VACUOUSLY TRUE against an EMPTY tenant: run this
# before a generate (or after a test run that truncated the dev database) and it
# prints a confident all-green banner over ZERO groups -- a meaningless verdict.
# The audit must assert it is looking at a materialized tenant before certifying
# one. Raising this cannot launder a defect; lowering it to 0 re-opens the
# vacuous pass.
MIN_SEMANTIC_RGS_AUDITED = 1

# EVIDENCE FLOOR for the role-aware anchor-less gate. "no anchor-less role
# tokens" is VACUOUSLY TRUE on a tenant that happens to contain no group under
# any anchor-required (role) token -- which re-creates the empty-tenant false
# pass one level down, inside the very gate meant to catch role over-claims. So
# the audit must prove it actually LOOKED at role claims before certifying that
# none of them are anchor-less. Floor on evidence volume, not a precision knob.
MIN_ANCHOR_REQUIRED_RGS_AUDITED = 1


def parse_token(name: str) -> str:
    """Extract the archetype token from ``rg-<env>-<app>-<token>-<n>``."""
    parts = name.split("-")
    return "-".join(parts[3:-1]) if len(parts) >= 4 and parts[0] == "rg" else "?"


def requires_anchor(tok: str) -> bool:
    """Can ``tok``'s archetype be confirmed ONLY by its anchor?

    DERIVED FROM THE LIVE CATALOG, never a hardcoded token list. Two clauses,
    both read off the shipped catalog:

    1. ``entry.anchor_required`` -- the token names an architectural ROLE (hub,
       platform, cluster, workspace, db, app, workload), so only the resource
       that DEFINES that role can prove it. Domain-adjacent support resources
       establish the DOMAIN, never the ROLE.
    2. ``len(entry.supporting) < MIN_SUPPORTING_SIGNALS`` -- the archetype's
       supporting tier is too thin to ever satisfy the supporting-signal path,
       so an anchor is the only route regardless of the flag.

    Clause 1 exists because clause 2 alone is not enough: a role token can
    declare several supporting signals (so it looks "signal-confirmable") while
    still being a role that only its anchor can prove. Reading the FLAG means
    this gate tracks the catalog it audits -- tighten an archetype, and the gate
    tightens with it, with zero edits here.
    """
    entry = archetypes._BY_TOKEN.get(tok)
    if entry is None:  # unknown token -- not classifiable, never silently "fine"
        return False
    return bool(entry.anchor_required) or len(entry.supporting) < archetypes.MIN_SUPPORTING_SIGNALS


def load_estate(dsn: str) -> tuple[dict, list]:
    """Read (rg -> materialized types) and the resource-group list from Postgres."""
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT subscription_id, resource_group_name, type FROM synthetic.resources")
        contents = collections.defaultdict(list)
        for sub, rg, t in cur.fetchall():
            contents[(sub, rg)].append(t)
        cur.execute("SELECT subscription_id, name FROM synthetic.resource_groups")
        rgs = cur.fetchall()
    return contents, rgs


def audit(contents: dict, rgs: list) -> SimpleNamespace:
    """Accumulate every metric and example the report and the gates need."""
    stat = collections.defaultdict(lambda: collections.Counter())
    cross_examples = collections.defaultdict(list)
    unconfirmed_examples = []
    empty_semantic_examples = []
    unconfirmed_semantic_rgs = 0
    empty_semantic_rgs = 0
    # Anchor-less semantic groups, by token, with offending examples.
    anchorless_examples = collections.defaultdict(list)
    anchorless_semantic_rgs = 0
    anchorless_under_anchor_required_tokens = 0
    # Non-vacuity evidence: how many non-empty, catalog-known semantic groups
    # carried an anchor-required token. The role-gate floor refuses to certify
    # at zero.
    anchor_required_rgs_audited = 0
    # The ROLE-ONLY evidence volume -- groups whose token carries
    # anchor_required=True. This, not the wider two-clause requires_anchor()
    # count, is what the role-gate floor may certify on: the wider predicate is
    # also true for non-role tokens with a thin supporting tier, and counting
    # those would inflate the floor and let a tenant whose role-token groups had
    # all collapsed to generic still satisfy the floor.
    role_token_rgs_audited = 0
    # Ubiquitous-only anchor hits, by token. A declared overlap (an archetype
    # owning its own ubiquitous anchor) is informational; an undeclared one is
    # gated.
    ubiquitous_only_anchor = collections.Counter()
    undeclared_ubiquitous_only_anchors = 0
    undeclared_ubiq_examples = []
    # A cross under a GENERIC name is informational (a generic name never
    # over-claims); only the SEMANTIC share is gated.
    cross_under_generic = 0

    for sub, name in rgs:
        tok = parse_token(name)
        types = contents.get((sub, name), [])
        if not types:
            stat[tok]["empty"] += 1
            # A semantic name over an empty group is an over-claim by definition.
            if tok not in GENERIC and tok != "?":
                empty_semantic_rgs += 1
                if len(empty_semantic_examples) < 5:
                    empty_semantic_examples.append((tok, name))
            continue
        stat[tok]["nonempty"] += 1
        uniq = list(set(types))
        inst = archetypes.match_template(uniq)  # informational: argmax view
        if tok in GENERIC:
            # honest-generic name: fine as long as contents don't SCREAM a single archetype
            if inst not in GENERIC:
                stat[tok]["contents_semantic"] += 1
                cross_under_generic += 1  # informational, NEVER a defect
            continue
        # ---- HEADLINE: the confirmation criterion (the shipped gate) ----
        confirmed_tok = archetypes.confirm_token(tok, uniq)
        if confirmed_tok != tok:
            unconfirmed_semantic_rgs += 1
            stat[tok]["unconfirmed"] += 1
            if len(unconfirmed_examples) < 5:
                unconfirmed_examples.append((tok, confirmed_tok, name, sorted(uniq)[:6]))
        # ---- does this group actually HOLD the anchor its name claims? ----
        # Child-credit first, exactly as the shipped gate does, so a group
        # holding only `sql/servers/databases` credits its parent `sql/servers`.
        _entry = archetypes._BY_TOKEN.get(tok)
        credited = archetypes._credit_children(archetypes._norm(uniq))
        _req_anchor = requires_anchor(tok)
        if _entry is not None and _req_anchor:
            anchor_required_rgs_audited += 1
        if _entry is not None and _entry.anchor_required:
            # The role-gate floor must count ROLE claims, and ONLY role claims.
            # requires_anchor() is deliberately wider (flag OR thin supporting
            # tier), so it is also true for non-role tokens; counting those would
            # inflate the floor. The floor reads the role flag alone; the gate
            # below keeps the wider predicate, where a superset is the safe
            # direction.
            role_token_rgs_audited += 1
        if _entry is not None and credited & _entry.anchors:
            stat[tok]["with_anchor"] += 1
            # An archetype MAY own a ubiquitous type as its own anchor (e.g. a
            # monitoring archetype owning action groups). So a ubiquitous-only
            # anchor hit is REPORTED, never failed, provided the overlap is
            # DECLARED in the catalog: ubiquity forbids BORROWED evidence, not
            # self-ownership. An UNdeclared one is a silent over-claim and is
            # gated below.
            _hit = credited & _entry.anchors
            if _hit and _hit <= archetypes._UBIQUITOUS:
                ubiquitous_only_anchor[tok] += 1
                _aid = _entry.archetype.id
                if any(
                    (_aid, _t) not in archetypes.DECLARED_UBIQUITOUS_ANCHORS
                    for _t in _hit
                ):
                    undeclared_ubiquitous_only_anchors += 1
                    if len(undeclared_ubiq_examples) < 5:
                        undeclared_ubiq_examples.append((tok, name, sorted(_hit)))
        elif _entry is not None:
            stat[tok]["anchorless"] += 1
            anchorless_semantic_rgs += 1
            if _req_anchor:
                anchorless_under_anchor_required_tokens += 1
            if len(anchorless_examples[tok]) < 5:
                anchorless_examples[tok].append((tok, name, sorted(uniq)[:6]))
        else:
            # A parsed token with no catalog entry: not a known claim, so it can
            # be neither anchored nor anchor-less. Counted so the table never
            # silently loses groups -- `unconfirmed` above already flags it if it
            # over-claims.
            stat[tok]["unknown_token"] += 1
        # ---- INFORMATIONAL: argmax columns ----
        if inst == tok:
            stat[tok]["agree"] += 1
        elif inst in GENERIC:
            stat[tok]["generic_thin"] += 1
        else:
            stat[tok]["cross"] += 1
            if len(cross_examples[tok]) < 3:
                cross_examples[tok].append((name, inst, sorted(set(types))[:6]))

    return SimpleNamespace(
        stat=stat,
        cross_examples=cross_examples,
        unconfirmed_examples=unconfirmed_examples,
        empty_semantic_examples=empty_semantic_examples,
        unconfirmed_semantic_rgs=unconfirmed_semantic_rgs,
        empty_semantic_rgs=empty_semantic_rgs,
        anchorless_examples=anchorless_examples,
        anchorless_semantic_rgs=anchorless_semantic_rgs,
        anchorless_under_anchor_required_tokens=anchorless_under_anchor_required_tokens,
        anchor_required_rgs_audited=anchor_required_rgs_audited,
        role_token_rgs_audited=role_token_rgs_audited,
        ubiquitous_only_anchor=ubiquitous_only_anchor,
        undeclared_ubiquitous_only_anchors=undeclared_ubiquitous_only_anchors,
        undeclared_ubiq_examples=undeclared_ubiq_examples,
        cross_under_generic=cross_under_generic,
    )


def evaluate_gates(
    *,
    tot_ne: int,
    role_token_rgs_audited: int,
    unconfirmed_semantic_rgs: int,
    empty_semantic_rgs: int,
    anchorless_under_anchor_required_tokens: int,
    semantic_cross_pct: float,
    undeclared_ubiquitous_only_anchors: int,
) -> list[tuple[str, object, str, bool]]:
    """Pure gate-decision logic: (label, measured, threshold, ok) per gate.

    All gates must hold. The evidence-floor gates are RETAINED alongside the
    over-claim gates, never replaced -- a floor was proven insufficient on its
    own, not wrong, and a green banner from a floor alone means nothing.
    """
    return [
        ("(1) semantic RGs audited     (non-vacuity evidence floor)",
         tot_ne, f">= {MIN_SEMANTIC_RGS_AUDITED}", tot_ne >= MIN_SEMANTIC_RGS_AUDITED),
        ("(2) role-token RGs audited   (role-gate evidence floor)",
         role_token_rgs_audited, f">= {MIN_ANCHOR_REQUIRED_RGS_AUDITED}",
         role_token_rgs_audited >= MIN_ANCHOR_REQUIRED_RGS_AUDITED),
        ("(3) unconfirmed_semantic_rgs (over-claim floor)",
         unconfirmed_semantic_rgs, "== 0", unconfirmed_semantic_rgs == 0),
        ("(4) empty_semantic_rgs       (empty-name over-claim)",
         empty_semantic_rgs, "== 0", empty_semantic_rgs == 0),
        ("(5) anchorless role tokens   (role-aware anchor gate)",
         anchorless_under_anchor_required_tokens, "== 0",
         anchorless_under_anchor_required_tokens == 0),
        ("(6) semantic_cross_pct       (argmax-cross under semantic names)",
         f"{semantic_cross_pct:.1f}%", f"<= {MAX_SEMANTIC_CROSS_PCT}%",
         semantic_cross_pct <= MAX_SEMANTIC_CROSS_PCT),
        ("(7) undeclared ubiquitous-only anchors",
         undeclared_ubiquitous_only_anchors, "== 0",
         undeclared_ubiquitous_only_anchors == 0),
    ]


def render(r: SimpleNamespace) -> bool:
    """Print the full report and the gate verdict. Returns True iff all gates hold."""
    stat = r.stat
    print(f"{'token':<15}{'#RGs':>6}{'empty':>7}{'nonE':>6}{'AGREE':>7}{'thin→gen':>9}{'CROSS':>7}")
    print("-" * 57)
    semantic = sorted((t for t in stat if t not in GENERIC and t != "?"),
                      key=lambda t: -(stat[t]["nonempty"] + stat[t]["empty"]))
    tot_agree = tot_thin = tot_cross = tot_ne = 0
    for tok in semantic:
        s = stat[tok]
        n = s["empty"] + s["nonempty"]
        ne, ag, th, cr = s["nonempty"], s["agree"], s["generic_thin"], s["cross"]
        tot_ne += ne
        tot_agree += ag
        tot_thin += th
        tot_cross += cr
        print(f"{tok:<15}{n:>6}{s['empty']:>7}{ne:>6}{ag:>7}{th:>9}{cr:>7}")
    print("-" * 57)
    print(f"{'TOTAL semantic':<15}{'':>6}{'':>7}{tot_ne:>6}{tot_agree:>7}{tot_thin:>9}{tot_cross:>7}")
    if tot_ne:
        print(f"\nOf non-empty semantic RGs: AGREE {100*tot_agree/tot_ne:.1f}%  "
              f"thin→generic {100*tot_thin/tot_ne:.1f}%  CROSS(real mismatch) {100*tot_cross/tot_ne:.1f}%")

    for tok in GENERIC:
        s = stat[tok]
        print(f"\n{tok}: {s['empty']+s['nonempty']} RGs, {s['contents_semantic']} whose CONTENTS "
              f"look strongly semantic (a generic name hiding a clear archetype)")

    # ============ anchor-less semantic RGs, BY TOKEN ============
    print("\n" + "=" * 70)
    print("== ANCHOR-LESS semantic RGs BY TOKEN ==")
    print("=" * 70)
    print("   ANCHOR-REQ   = the token names an architectural ROLE (anchor_required), or")
    print("                  its supporting tier < MIN_SUPPORTING_SIGNALS, so ONLY its")
    print("                  anchor can confirm it; anchor-less here is a HARD FAIL.")
    print("   signal-conf. = can legitimately confirm on >=2 supporting signals with a")
    print("                  margin; anchor-less here is LEGAL -- eyeball it.")
    print()
    print(f"{'token':<15}{'RGs':>6}{'w/anchor':>10}{'anchorless':>12}{'%':>7}  kind")
    print("-" * 70)
    for tok in semantic:
        s = stat[tok]
        n_claim = s["with_anchor"] + s["anchorless"]  # non-empty, catalog-known only
        if not n_claim:
            continue
        al = s["anchorless"]
        kind = "ANCHOR-REQ" if requires_anchor(tok) else "signal-conf."
        print(
            f"{tok:<15}{n_claim:>6}{s['with_anchor']:>10}{al:>12}"
            f"{100*al/n_claim:>6.0f}%  {kind}"
        )
    print("-" * 70)

    def _named_anchorless(tok: str) -> str:
        """`anchorless/claims (pct)` for one token, or `n/a` if it serves no RGs.

        A missing token is INFORMATION (the catalog pushed every group generic),
        not a crash -- never raise here, or a successful outcome would look like
        a broken audit.
        """
        s = stat.get(tok)
        if not s:
            return "n/a (token serves no RGs)"
        n_claim = s["with_anchor"] + s["anchorless"]
        if not n_claim:
            return "n/a (no non-empty catalog-known RGs)"
        return f"{s['anchorless']} of {n_claim} = {100*s['anchorless']/n_claim:.0f}%"

    # A quick spot-check of two representative role tokens, so a reviewer need
    # not scan the whole table to see the role gate is holding.
    print(f"  network-hub anchor-less   = {_named_anchorless('network-hub')}   [role token -- MUST be 0]")
    print(f"  data-platform anchor-less = {_named_anchorless('data-platform')}   [role token -- MUST be 0]")
    print("-" * 70)
    print(f"  anchorless_semantic_rgs                 = {r.anchorless_semantic_rgs}   (total, incl. LEGAL signal-only)")
    print(f"  anchorless_under_anchor_required_tokens = {r.anchorless_under_anchor_required_tokens}   (MUST be 0 -- role-aware)")
    print(f"  anchor_required_rgs_audited             = {r.anchor_required_rgs_audited}   (evidence floor >= {MIN_ANCHOR_REQUIRED_RGS_AUDITED})")

    if r.anchorless_examples:
        print("\n  Anchor-less examples (name claims the shape; no anchor in contents):")
        for tok in sorted(r.anchorless_examples):
            kind = "ANCHOR-REQ -> HARD FAIL" if requires_anchor(tok) else "signal-only -> judge it"
            print(f"    -- {tok}  [{kind}]")
            for _tok, name, types in r.anchorless_examples[tok]:
                print(f"       {name}: {types}")

    print("\n== CROSS mismatches (name says X, contents classify as a DIFFERENT archetype Y) ==")
    print("   [informational -- argmax view; a CROSS under a GENERIC name is not a defect]")
    if not any(r.cross_examples.values()):
        print("  NONE -- no RG's contents classify as a conflicting archetype")
    for tok in sorted(r.cross_examples):
        for name, inst, types in r.cross_examples[tok]:
            print(f"  name={tok:<13} contents→{inst:<13} {name}: {types}")

    # ===================== HEADLINE: the gate verdict =====================
    # `tot_cross` was accumulated ONLY under semantic tokens, so it is exactly
    # the gated numerator; `tot_ne` is non-empty semantic RGs.
    semantic_cross_pct = (100.0 * tot_cross / tot_ne) if tot_ne else 0.0

    print("\n" + "=" * 70)
    print("== the CROSS split (which NAMES do the residuals hide under?) ==")
    print("=" * 70)
    print(f"  CROSS under GENERIC  names = {r.cross_under_generic:<6} (informational -- a generic name never over-claims)")
    print(f"  CROSS under SEMANTIC names = {tot_cross:<6} of {tot_ne} non-empty semantic RGs "
          f"= {semantic_cross_pct:.1f}%  (GATED)")
    print(f"                               threshold MAX_SEMANTIC_CROSS_PCT = {MAX_SEMANTIC_CROSS_PCT}%")

    if r.ubiquitous_only_anchor:
        print("\n" + "=" * 70)
        print("== UBIQUITOUS-ONLY ANCHORS -- reported, not failed when declared ==")
        print("=" * 70)
        print("  An RG whose ONLY anchor hit is a ubiquitous type. Legitimate when the")
        print("  archetype OWNS that type (declared in the catalog with a rationale);")
        print("  ubiquity forbids BORROWED evidence, not self-ownership.")
        for _t, _n in sorted(r.ubiquitous_only_anchor.items(), key=lambda kv: -kv[1]):
            _e = archetypes._BY_TOKEN.get(_t)
            _aid = _e.archetype.id if _e is not None else "?"
            _declared = any(a == _aid for (a, _) in archetypes.DECLARED_UBIQUITOUS_ANCHORS)
            _total = stat[_t]["nonempty"]
            print(f"    {_t:<15}{_n:>5} of {_total:<5} non-empty  "
                  f"{'DECLARED (informational)' if _declared else 'UNDECLARED (GATED)'}")

    gates = evaluate_gates(
        tot_ne=tot_ne,
        role_token_rgs_audited=r.role_token_rgs_audited,
        unconfirmed_semantic_rgs=r.unconfirmed_semantic_rgs,
        empty_semantic_rgs=r.empty_semantic_rgs,
        anchorless_under_anchor_required_tokens=r.anchorless_under_anchor_required_tokens,
        semantic_cross_pct=semantic_cross_pct,
        undeclared_ubiquitous_only_anchors=r.undeclared_ubiquitous_only_anchors,
    )

    print("\n" + "=" * 70)
    print("== COHERENCE GATE VERDICT -- semantic honesty, not internal consistency ==")
    print("=" * 70)
    for label, measured, threshold, ok in gates:
        print(f"  [{'PASS' if ok else 'FAIL'}]  {label:<62} measured={measured!s:<8} threshold {threshold}")

    if r.unconfirmed_examples:
        print("\n  Semantic names whose contents FAIL confirmation (name over-claims):")
        for tok, got, name, types in r.unconfirmed_examples:
            print(f"    name={tok:<13} confirm→{got:<8} {name}: {types}")
    if r.empty_semantic_examples:
        print("\n  Semantic names with ZERO materialized contents:")
        for tok, name in r.empty_semantic_examples:
            print(f"    name={tok:<13} {name}: (empty)")

    # Total occupancy across EVERY token (generic included), so an all-generic
    # tenant is never misreported as an empty one.
    _all_nonempty_rgs = sum(s["nonempty"] for s in stat.values())
    ok = all(g[3] for g in gates)
    print()
    if ok:
        print("  ####  PASS -- every semantic RG name is backed by its served contents  ####")
    else:
        broke = [g[0] for g in gates if not g[3]]
        if tot_ne < MIN_SEMANTIC_RGS_AUDITED and _all_nonempty_rgs == 0:
            # Do not misreport the cause: there are no names here to over-claim.
            print("  ####  FAIL -- VACUOUS: no semantic RGs audited, nothing was proven  ####")
            print("  ####  This is NOT an RG-naming defect -- the tenant is EMPTY.")
            print("  ####  Generate an estate first, e.g.:")
            print("  ####    uv run tenantless generate --profile enterprise --seed 7 --force")
            print("  ####  (running the test suite truncates the local dev database.)")
        elif tot_ne < MIN_SEMANTIC_RGS_AUDITED:
            # `tot_ne` counts SEMANTIC RGs only, so a populated tenant whose names
            # ALL collapsed to shared/core lands here too -- and the branch above
            # would have told the reader to re-seed an already-full tenant while a
            # real naming regression went unreported. Distinguish on occupancy.
            print("  ####  FAIL -- TOTAL SEMANTIC COLLAPSE: the tenant is POPULATED  ####")
            print(f"  ####  ({_all_nonempty_rgs} non-empty RGs) but NOT ONE carries a semantic")
            print("  ####  name -- every RG degraded to shared/core. This IS a naming defect:")
            print("  ####  DO NOT re-seed and DO NOT read this as an empty tenant.")
            print("  ####  Suspect an over-tightened catalog or confirmation rule.")
        elif r.role_token_rgs_audited < MIN_ANCHOR_REQUIRED_RGS_AUDITED:
            # Distinct cause, distinct message: there ARE semantic RGs, but NONE
            # carries a role token, so the role gate proved nothing.
            print("  ####  FAIL -- VACUOUS ROLE GATE: semantic RGs exist, but NOT ONE carries")
            print("  ####  a role (anchor-required) token, so the anchor-less role gate is")
            print("  ####  vacuously true and the role over-claim is UNPROVEN on this tenant.")
            print("  ####  This is NOT an RG-naming defect -- it is missing EVIDENCE.")
            print("  ####  Check the tenant was regenerated, and that the catalog still")
            print("  ####  declares anchor_required entries.")
        else:
            print("  ####  FAIL -- at least one RG name over-claims vs its served contents  ####")
        print(f"  ####  gate(s) broken: {', '.join(broke)}")
        print("  ####  DO NOT tune a threshold to clear this -- report it.")
    print("=" * 70)
    return ok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Audit that every generated resource-group archetype name is backed "
            "by its materialized contents. Exits non-zero on any gate failure."
        )
    )
    ap.add_argument(
        "--database-url",
        default=DEFAULT_DSN,
        help="Postgres DSN of the estate to audit (default: $DATABASE_URL, else the local dev DSN)",
    )
    args = ap.parse_args(argv)

    # Windows consoles default to cp1252, which cannot encode this report's
    # box/arrow characters. Force UTF-8 so the audit runs from any shell;
    # `errors="replace"` keeps a degraded console from masking the verdict
    # behind a traceback.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    contents, rgs = load_estate(args.database_url)
    result = audit(contents, rgs)
    ok = render(result)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
