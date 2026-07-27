"""Static archetype catalog + pure type-signature matcher (Phase 19, ARCH-01/02).

The labeling brain of Phase 19. It classifies each profile RG template's measured
``type_set`` into a named Azure solution shape (``web-app`` / ``backup`` /
``network-hub`` / ...) or an honest generic fallback (``shared`` / ``core``), so
the RG-name workload token can be a *label of the RG's contents* instead of a
random draw (ARCH-03, Plan 02). This module mirrors the ``identity.py`` shape:
a docstring stating the invariants, a top-level immutable catalogue constant, and
a derived index built ONCE at module load.

Privacy invariant (D-01 / T-19-01)
----------------------------------
Every catalog string is a PUBLIC ARM resource-type name or a generic English
token — ZERO tenant-derived strings. There is therefore no data-boundary
(denylist) gate here, exactly mirroring why ``type_weights`` carries no
aggregation gate: the token is emitted from the static catalog keyed by the
matched archetype id and can never echo a ``type_set``/profile value.

Determinism invariant (D-02 / T-19-02)
--------------------------------------
The token is a PURE function of the template's ``type_set`` — the matcher adds NO
RNG, no DB read, and no wall-clock. It is a subtractive-plus-lookup refactor: a
static dict + a ~30-line scorer. A fixed ``(profile, seed, targets)`` therefore
stays byte-reproducible (the SPEED-02 ``jobs=1 == jobs=N`` fingerprint gate stays
green — this module never draws).

Casing rule (VERIFIED — reuse, do NOT hand-roll)
------------------------------------------------
``enterprise.json`` stores type keys as ``Microsoft.<ns>/<tail-verbatim>`` where
the tail is lowercase for the real-derived profile (``Microsoft.web/sites``). The
catalog below is written in canonical ARM casing for readability and normalized
to a fully-lowercased compare key ONCE at module load, routing every anchor/signal
and every input ``type_set`` element through :func:`arm.canonical_type` then
``.lower()`` — the one casing rule that already governs the whole tree.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Mapping, NamedTuple, Sequence

from . import arm
from .resources import _MISC  # the "__misc__" sentinel (D-06) — single source of truth

# --------------------------------------------------------------------------- #
# Fallback tokens (D-09 split) + matcher weights/thresholds (D-07 — tunable).
# Exposed as module constants so a precision re-tune is a one-line edit pinned by
# the precision matrix in tests/test_generator_archetypes.py.
# --------------------------------------------------------------------------- #
TOKEN_SHARED = "shared"
TOKEN_CORE = "core"

ANCHOR_WEIGHT = 2.0
STRONG_SIGNAL_WEIGHT = 0.5
NEGATIVE_PENALTY = 1.5
MIN_SCORE = 2.0
MARGIN_THRESHOLD = 0.5

# ARCH-GAP-02 remedy 4 — the CONFIRMATION knobs (distinct from the scorer's above).
# An anchor always confirms; signal-only confirmation additionally requires
# MULTIPLE discriminative supporting signals AND a clear lead over the runner-up
# archetype's supporting count. Tunable and unit-tested (D-07 precision bias) by
# the confirmation matrix in tests/test_generator_archetypes.py — but NEVER to be
# loosened to make a gate pass: doing so is how ARCH-GAP-02 shipped.
MIN_SUPPORTING_SIGNALS = 2
CONFIRM_MARGIN = 1

# --------------------------------------------------------------------------- #
# ARCH-GAP-02 remedy 1/3 — the ubiquity denylist for the SUPPORTING tier.
#
# These five Azure types are among the most common in any real tenant, so their
# presence in an RG says nothing about which solution shape that RG is. They may
# only ever sit in an archetype's ``generic_signals`` tier: they can nudge a
# score, but (from 19-08) they must never CONFIRM a claim on their own. This is
# the constant that makes "an RG holding a lone storage account certifies as a
# DevCenter platform" structurally impossible to re-introduce — the full-catalog
# invariant test audits every entry (today's 12 and every future one) against it.
#
# SCOPE CORRECTION (CR-03, operator ruling 2026-07-20). The paragraph above is
# about BORROWED evidence and only ever audited the supporting/generic tiers.
# It said nothing about the ANCHOR tier, and the claim of structural
# impossibility was therefore overstated: ``monitoring`` anchors on
# ``Insights/actionGroups`` and ``identity`` anchors on
# ``userAssignedIdentities``, both ubiquitous, so both could confirm on a lone
# ubiquitous resource (85 of 108 live monitoring RGs did).
#
# The ruling is that this is CORRECT and must not be renamed: ubiquity means
# "not discriminative as evidence BORROWED by some other archetype", not
# "semantically meaningless". A lone action group is thin monitoring — but it is
# monitoring, not a false claim. An archetype may OWN a ubiquitous type as its
# own anchor. What was actually wrong is that the overlap was SILENT, so it is
# now declared below with a rationale and audited in both directions by
# ``test_anchor_tier_ubiquitous_overlap_is_declared`` and
# ``test_ubiquitous_anchor_never_confirms_a_borrowing_archetype``.
# --------------------------------------------------------------------------- #
UBIQUITOUS_SIGNALS: tuple[str, ...] = (
    "Microsoft.Storage/storageAccounts",
    "Microsoft.KeyVault/vaults",
    "Microsoft.Insights/components",
    "Microsoft.Insights/actionGroups",
    "Microsoft.ManagedIdentity/userAssignedIdentities",
)

# --------------------------------------------------------------------------- #
# CR-03 — the DECLARED anchor-tier ubiquity overlaps.
#
# Keyed ``(archetype id, ARM type)``; the value is the rationale for why this
# archetype OWNS a type that is otherwise non-discriminative. Adding an entry is
# a deliberate act with a written justification, and the invariant refuses both
# an undeclared overlap and a stale declaration — so this cannot rot into a
# blanket pre-authorization for a future archetype.
# --------------------------------------------------------------------------- #
_DECLARED_UBIQUITOUS_ANCHORS_RAW: dict[tuple[str, str], str] = {
    ("monitoring", "Microsoft.Insights/actionGroups"): (
        "An action group is the alert-notification target of the monitoring "
        "plane itself — it exists to serve monitoring and belongs to no other "
        "solution shape. Ubiquitous as BORROWED evidence (its presence must "
        "never help certify a devbox or a data platform), but it is monitoring's "
        "own defining resource, so a lone action group is thin monitoring rather "
        "than a false monitoring claim."
    ),
    ("identity", "Microsoft.ManagedIdentity/userAssignedIdentities"): (
        "A user-assigned managed identity IS the identity plane's primary "
        "object; identity declares no other anchor. Ubiquitous because it is "
        "attached to workloads across every domain, which is exactly why it may "
        "not certify those workloads' archetypes — but an RG holding managed "
        "identities is an identity RG."
    ),
}

# --------------------------------------------------------------------------- #
# WR-01 — the CONFIRMATION POLICY (supersedes the ROLE_NOUNS vocabulary list).
#
# ARCH-GAP-03's remedy was `anchor_required=True` on every archetype whose name
# token appeared in a 7-word ROLE_NOUNS set {hub, platform, cluster, workspace,
# db, app, workload}. That guard could only ever catch vocabulary it already
# knew: an archetype named ("network","gateway"), ("security","firewall") or
# ("edge","frontend") — semantically the IDENTICAL failure — inherited the
# permissive `anchor_required=False` default and shipped with every test green.
# For a catalog that OSS contributors extend, a default-permissive heuristic is
# precisely the wrong shape.
#
# So the default is removed entirely. Every archetype MUST declare which kind of
# evidence is allowed to prove its claim, and the catalog is validated by
# ITERATING ENTRIES — never by inspecting how a token is spelled.
#
# HONEST LIMIT: a mandatory policy makes the semantic contract an explicit,
# reviewable decision; it does not make that decision automatically correct. A
# contributor may still declare a role-shaped archetype SUPPORTING_ALLOWED. What
# changed is that doing so is now a conscious catalog edit visible in review
# rather than an omission inheriting a permissive default.
# --------------------------------------------------------------------------- #
# The rationale bar. Deliberately crude: it can only reject text nobody wrote, not
# reasoning nobody thought through. Anything stronger would pretend to adjudicate a
# semantic judgement that only a human reviewer can make — see
# docs/archetype-catalog-checklist.md.
_MIN_RATIONALE_CHARS = 60
_RATIONALE_PLACEHOLDERS: frozenset[str] = frozenset(
    {"", "todo", "tbd", "n/a", "na", "none", "see above", "obvious", "fixme", "xxx"}
)


class ConfirmationPolicy(Enum):
    """Which evidence may CONFIRM an archetype's claim (mandatory, no default)."""

    #: Only the archetype's own anchor — the resource that DEFINES the shape —
    #: proves the claim. Supporting signals stay real, discriminative evidence and
    #: still feed the D-05 score, but they establish the DOMAIN, never the ROLE:
    #: route tables and NSGs prove "networking", not "hub"; a hub is defined by
    #: the VNet it hubs. Choose this whenever the name asserts a STRUCTURE.
    ANCHOR_REQUIRED = "anchor_required"

    #: An anchor confirms, and so may >= MIN_SUPPORTING_SIGNALS discriminative
    #: supporting signals with a >= CONFIRM_MARGIN lead over the runner-up.
    #: Appropriate for CAPABILITY names (backup, monitoring), where a coherent
    #: bundle of discriminative types genuinely evidences the capability.
    SUPPORTING_ALLOWED = "supporting_allowed"

    #: Claims nothing, so it can never over-claim: an entry under this policy may
    #: only carry a generic token and never confirms. Its evidence tiers are not
    #: consulted at all.
    GENERIC = "generic"


@dataclass(frozen=True, slots=True)
class Archetype:
    """One named Azure solution shape (D-02 per-entry schema).

    Evidence is classified into THREE explicit tiers (ARCH-GAP-02 remedy 1) so
    the catalog knows which evidence actually *proves* a claim:

    - ``required_any`` = **anchor** tier — at least one MUST be present for any
      match; this is the type that *defines* the shape (``DevCenter/devcenters``
      for devbox). Never re-tiered.
    - ``required_all`` = optional hard prerequisites (every one must be present).
    - ``supporting_signals`` = **supporting** tier — discriminative corroborating
      types (``Network/routeTables`` for network-hub). Guaranteed by the catalog
      invariant to be neither ubiquitous nor another archetype's anchor, and
      pairwise disjoint across archetypes.
    - ``generic_signals`` = **generic** tier — types that co-occur with the shape
      but prove nothing on their own: every element is in
      :data:`UBIQUITOUS_SIGNALS` or is another archetype's anchor.
    - ``negative_signals`` = disqualifying-context types (penalty, -each).
    - ``confirmation`` = **mandatory** :class:`ConfirmationPolicy` (WR-01). Keyword-only
      with NO default, so an entry that does not state its semantic contract cannot be
      constructed at all. Per-entry invariants are checked in ``__post_init__`` and the
      whole catalog is re-checked by :func:`validate_catalog` at import.

    ``strong_signals`` remains available as a read-only property equal to
    ``supporting_signals + generic_signals`` — the scorer consumes that union, so
    the tier split is score-neutral by construction (zero template-label ripple).
    ``anchor_required`` likewise remains as a read-only PROJECTION of the policy, so
    ``_Normalized`` and the 19-12 audit's ``requires_anchor()`` keep working unchanged
    (T-19-11: tightening the catalog tightens the gate with zero audit edits).
    """

    id: str
    name_tokens: tuple[str, ...]
    required_any: tuple[str, ...]
    required_all: tuple[str, ...] = ()
    supporting_signals: tuple[str, ...] = ()
    generic_signals: tuple[str, ...] = ()
    negative_signals: tuple[str, ...] = ()
    # Keyword-only + no default: omission is a TypeError, never a permissive default.
    confirmation: ConfirmationPolicy = field(kw_only=True)
    #: REQUIRED under SUPPORTING_ALLOWED, forbidden otherwise. Prose explaining why
    #: this archetype's supporting signals make its claimed NAME honest without the
    #: resource that defines the shape. Metadata only — see
    #: ``test_rationale_is_metadata_only_and_cannot_affect_generation``.
    supporting_allowed_rationale: str | None = field(kw_only=True, default=None)

    def __post_init__(self) -> None:
        """Construction-time semantic invariants — a bad entry cannot exist.

        Deliberately here rather than in a test: a contributor adding an archetype
        finds out while writing it, not when someone runs the suite.
        """
        if not isinstance(self.confirmation, ConfirmationPolicy):
            raise TypeError(
                f"{self.id!r}: confirmation must be a ConfirmationPolicy, "
                f"got {type(self.confirmation).__name__}"
            )
        if self.confirmation is ConfirmationPolicy.ANCHOR_REQUIRED and not self.required_any:
            raise ValueError(
                f"{self.id!r} is ANCHOR_REQUIRED but declares no anchor — it could "
                "never be confirmed by anything (a silent dead token)"
            )
        if self.confirmation is ConfirmationPolicy.SUPPORTING_ALLOWED:
            # This is the ONE policy that lets a name stand without the resource
            # that defines it — the exact shape of ARCH-GAP-03. It may not be
            # chosen silently.
            if not self.supporting_signals:
                raise ValueError(
                    f"{self.id!r} is SUPPORTING_ALLOWED but declares no supporting "
                    "signals — the signal path it claims to allow can never run, so "
                    "the entry is anchor-only in fact. Declare ANCHOR_REQUIRED."
                )
            rationale = (self.supporting_allowed_rationale or "").strip()
            if len(rationale) < _MIN_RATIONALE_CHARS or rationale.lower().rstrip(
                ".!"
            ) in _RATIONALE_PLACEHOLDERS:
                raise ValueError(
                    f"{self.id!r} is SUPPORTING_ALLOWED and must carry a written "
                    "supporting_allowed_rationale explaining why its supporting "
                    "signals make the claimed NAME honest without an anchor. This "
                    "check cannot tell whether the reasoning is right — it exists so "
                    "the judgement lands in the diff where a reviewer can challenge it."
                )
        elif self.supporting_allowed_rationale is not None:
            raise ValueError(
                f"{self.id!r} declares a supporting_allowed_rationale under "
                f"{self.confirmation.name} — the field is meaningful only under "
                "SUPPORTING_ALLOWED and must be omitted elsewhere"
            )
        if self.confirmation is ConfirmationPolicy.GENERIC:
            semantic = [t for t in self.name_tokens if t not in (TOKEN_SHARED, TOKEN_CORE)]
            if semantic:
                raise ValueError(
                    f"{self.id!r} is GENERIC but claims the semantic token(s) {semantic} — "
                    "a generic policy may only carry a generic token"
                )
            if self.required_any or self.supporting_signals:
                raise ValueError(
                    f"{self.id!r} is GENERIC but declares evidence — a generic entry "
                    "claims nothing, so evidence for it is meaningless"
                )

    @property
    def anchor_required(self) -> bool:
        """Signal-only confirmation is unavailable — projection of the policy.

        Kept as a property so every existing consumer (``_Normalized``, the 19-12
        audit's ``requires_anchor()``) reads the catalog exactly as before.
        """
        return self.confirmation is ConfirmationPolicy.ANCHOR_REQUIRED

    @property
    def strong_signals(self) -> tuple[str, ...]:
        """The full weighted-signal union the D-05 scorer sees (both tiers).

        A property (class attribute), not a field — compatible with ``slots=True``
        because it does not collide with a field name. Keeping this seam means
        ``match_template``'s ``len(s & entry.strong)`` term is arithmetically
        identical to pre-tiering.
        """
        return self.supporting_signals + self.generic_signals


# --------------------------------------------------------------------------- #
# ARCH-01 catalog — 11 high-confidence Azure solution archetypes (19-RESEARCH
# § Archetype Catalog). Types written in canonical ARM casing; normalized at load.
# Extensible over time (D-03): start 10-12, grow from observed unmatched shapes.
# --------------------------------------------------------------------------- #
ARCHETYPES: tuple[Archetype, ...] = (
    Archetype(
        id="vm-workload",
        name_tokens=("vm", "workload"),
        required_any=("Microsoft.Compute/virtualMachines",),
        supporting_signals=(
            "Microsoft.Compute/disks",
            "Microsoft.Network/networkInterfaces",
            "Microsoft.Compute/availabilitySets",
            "Microsoft.Compute/virtualMachines/extensions",
        ),
        negative_signals=("Microsoft.ContainerService/managedClusters",),
        confirmation=ConfirmationPolicy.ANCHOR_REQUIRED,
    ),
    Archetype(
        id="web-app",
        name_tokens=("web", "app"),
        required_any=("Microsoft.Web/sites",),
        supporting_signals=("Microsoft.Web/serverfarms",),
        generic_signals=(
            "Microsoft.Insights/components",
            "Microsoft.Storage/storageAccounts",
            "Microsoft.KeyVault/vaults",
            "Microsoft.Sql/servers",  # sql-database's anchor
        ),
        negative_signals=(
            "Microsoft.ContainerService/managedClusters",
            "Microsoft.Network/azureFirewalls",
        ),
        confirmation=ConfirmationPolicy.ANCHOR_REQUIRED,
    ),
    Archetype(
        id="aks-platform",
        name_tokens=("aks", "platform"),
        required_any=("Microsoft.ContainerService/managedClusters",),
        supporting_signals=("Microsoft.ContainerRegistry/registries",),
        generic_signals=(
            "Microsoft.ManagedIdentity/userAssignedIdentities",
            "Microsoft.Network/virtualNetworks",  # network-hub's anchor
            "Microsoft.Insights/components",
        ),
        confirmation=ConfirmationPolicy.ANCHOR_REQUIRED,
    ),
    # ARCH-GAP-03 remedy 1 — the operator ruling, recorded verbatim in substance:
    # "Networking support resources establish the domain but not a hub." 109 of 213
    # network-hub RGs held no virtualNetworks — 51% is catalog behaviour, not noise.
    # The 5 supporting signals STAY (they are real, discriminative networking
    # evidence and they still feed the D-05 SCORE), but they can no longer CONFIRM
    # the hub claim on their own: a hub is defined by the VNet it hubs.
    Archetype(
        id="network-hub",
        name_tokens=("network", "hub"),
        required_any=("Microsoft.Network/virtualNetworks",),
        supporting_signals=(
            "Microsoft.Network/routeTables",
            "Microsoft.Network/azureFirewalls",
            "Microsoft.Network/networkSecurityGroups",
            "Microsoft.Network/networkIntentPolicies",
            "Microsoft.Network/publicIPAddresses",
        ),
        negative_signals=(
            "Microsoft.Databricks/workspaces",
            "Microsoft.Synapse/workspaces",
            "Microsoft.Web/sites",
            "Microsoft.Sql/servers",
            "Microsoft.Compute/virtualMachines",
        ),
        confirmation=ConfirmationPolicy.ANCHOR_REQUIRED,
    ),
    Archetype(
        id="backup",
        name_tokens=("backup",),
        required_any=(
            "Microsoft.RecoveryServices/vaults",
            "Microsoft.DataProtection/backupVaults",
        ),
        supporting_signals=(
            "Microsoft.Maintenance/maintenanceConfigurations",
            "Microsoft.Compute/restorePointCollections",
        ),
        generic_signals=("Microsoft.Storage/storageAccounts",),
        confirmation=ConfirmationPolicy.SUPPORTING_ALLOWED,
        supporting_allowed_rationale=(
            "'backup' names a FUNCTION being performed, not a structure that a "
            "particular resource defines, so there is no single object whose absence "
            "makes the name false. Both supporting signals are themselves backup "
            "artifacts: a restorePointCollection IS a set of taken restore points, and "
            "a maintenanceConfiguration schedules the protection window. Neither is "
            "ubiquitous and neither belongs to another archetype, so an RG holding "
            "them is doing backup work whether or not the vault object resource "
            "happens to live in the same RG — a common real-world layout, since "
            "vaults are frequently centralized while restore points accumulate beside "
            "the workload they protect."
        ),
    ),
    Archetype(
        id="data-platform",
        name_tokens=("data", "platform"),
        required_any=(
            "Microsoft.Databricks/workspaces",
            "Microsoft.Synapse/workspaces",
            "Microsoft.DataFactory/factories",
        ),
        # ARCH-GAP-03 second finding (remedy 3): 4 of the 5 surviving data-platform
        # RGs were anchored ONLY by an access connector — e.g.
        # rg-ops-prod-data-platform-72 held accessconnectors +
        # userassignedidentities. A lone access *connector* is not a data platform,
        # so it is demoted from the anchor tier to SUPPORTING: it corroborates the
        # claim, it cannot constitute it. That leaves data-platform with ONE
        # supporting signal — below MIN_SUPPORTING_SIGNALS — so it stays
        # unconfirmable without an anchor by two INDEPENDENT mechanisms (the count
        # AND anchor_required=True). Belt and braces, deliberately.
        supporting_signals=("Microsoft.Databricks/accessConnectors",),
        generic_signals=(
            "Microsoft.Storage/storageAccounts",
            "Microsoft.ManagedIdentity/userAssignedIdentities",
            "Microsoft.KeyVault/vaults",
        ),
        confirmation=ConfirmationPolicy.ANCHOR_REQUIRED,
    ),
    Archetype(
        id="sql-database",
        name_tokens=("sql", "db"),
        required_any=("Microsoft.Sql/servers",),
        supporting_signals=("Microsoft.Sql/servers/databases",),
        generic_signals=(
            "Microsoft.Storage/storageAccounts",
            "Microsoft.KeyVault/vaults",
        ),
        negative_signals=("Microsoft.Web/sites",),
        confirmation=ConfirmationPolicy.ANCHOR_REQUIRED,
    ),
    Archetype(
        id="monitoring",
        name_tokens=("monitoring",),
        required_any=(
            "Microsoft.OperationalInsights/workspaces",
            "Microsoft.Insights/actionGroups",
            "Microsoft.AlertsManagement/actionRules",
            "Microsoft.Insights/metricAlerts",
            "dynatrace.observability/monitors",
        ),
        supporting_signals=(
            "Microsoft.Insights/activityLogAlerts",
            "Microsoft.Insights/scheduledQueryRules",
        ),
        generic_signals=("Microsoft.Insights/components",),
        negative_signals=(
            "Microsoft.Web/sites",
            "Microsoft.DevCenter/devcenters",
        ),
        confirmation=ConfirmationPolicy.SUPPORTING_ALLOWED,
        supporting_allowed_rationale=(
            "'monitoring' names a FUNCTION, not a structure, so no single resource "
            "defines it — which is why the anchor tier already lists five alternative "
            "objects rather than one. The two supporting signals are alert RULES "
            "(activityLogAlerts, scheduledQueryRules): they are pure observability "
            "instruments that exist for no other purpose, are not ubiquitous, and are "
            "not any other archetype's anchor. An RG holding several alert rules is "
            "monitoring something even when the workspace they query lives in a "
            "central RG — the normal Azure layout, where a shared Log Analytics "
            "workspace is targeted by rules deployed next to each workload."
        ),
    ),
    Archetype(
        id="messaging",
        name_tokens=("messaging",),
        required_any=(
            "Microsoft.ServiceBus/namespaces",
            "Microsoft.EventHub/namespaces",
            "Microsoft.EventGrid/topics",
        ),
        # supporting tier EMPTY — both former strong-signals are ubiquitous.
        generic_signals=(
            "Microsoft.Storage/storageAccounts",
            "Microsoft.ManagedIdentity/userAssignedIdentities",
        ),
        # Its supporting tier is empty, so the signal path could never run: this
        # entry was already anchor-only in fact while advertising otherwise. Now it
        # says so. Behaviour is unchanged — `hits=0 < MIN_SUPPORTING_SIGNALS` gave
        # exactly the same answer the role gate now gives directly.
        confirmation=ConfirmationPolicy.ANCHOR_REQUIRED,
    ),
    Archetype(
        id="identity",
        name_tokens=("identity",),
        required_any=("Microsoft.ManagedIdentity/userAssignedIdentities",),
        negative_signals=(
            "Microsoft.Storage/storageAccounts",
            "Microsoft.Compute/virtualMachines",
            "Microsoft.Databricks/workspaces",
            "Microsoft.Web/sites",
            "Microsoft.Sql/servers",
            "Microsoft.Network/virtualNetworks",
        ),
        # Empty supporting tier — anchor-only in fact (see `messaging`). Its single
        # anchor, userAssignedIdentities, is a DECLARED ubiquity overlap (CR-03).
        confirmation=ConfirmationPolicy.ANCHOR_REQUIRED,
    ),
    Archetype(
        id="devbox-platform",
        name_tokens=("devbox",),
        required_any=(
            "Microsoft.DevCenter/devcenters",
            "Microsoft.DevCenter/projects",
        ),
        # ARCH-GAP-02 remedy 2 — devbox is STRUCTURALLY ANCHOR-ONLY. All three of
        # its former strong_signals are ubiquitous (actionGroups is also
        # monitoring's own anchor), which is exactly why 135/182 devbox RGs
        # certified on a lone storage account. The supporting tier is EMPTY: only
        # a DevCenter anchor can prove a DevCenter platform (rule lands in 19-08).
        generic_signals=(
            "Microsoft.KeyVault/vaults",
            "Microsoft.Storage/storageAccounts",
            "Microsoft.Insights/actionGroups",
        ),
        confirmation=ConfirmationPolicy.ANCHOR_REQUIRED,
    ),
)


class _Normalized(NamedTuple):
    """A load-time lowercased view of one :class:`Archetype` (frozensets)."""

    archetype: Archetype
    anchors: frozenset[str]
    required_all: frozenset[str]
    strong: frozenset[str]
    negative: frozenset[str]
    supporting: frozenset[str] = frozenset()
    generic: frozenset[str] = frozenset()
    # ARCH-GAP-03: projected from Archetype.anchor_required; consumed by the
    # tightened confirmation rule (Plan 19-11). Trailing + defaulted so existing
    # in-test _Normalized fabrications keep constructing unchanged.
    anchor_required: bool = False


def _norm(types: Iterable[str]) -> frozenset[str]:
    """Fully-lowercased compare key via the ONE governing casing rule."""
    return frozenset(arm.canonical_type(t).lower() for t in types if t)


# Derived indexes built ONCE at module load (identity.py::GUID_BY_ROLE analog):
# every anchor/signal lowercased so the casing rule is applied exactly once.
_UBIQUITOUS: frozenset[str] = _norm(UBIQUITOUS_SIGNALS)

# CR-03: the declaration re-keyed through the ONE governing casing rule, so it
# compares directly against `_Normalized.anchors` without a casing bug of its own.
DECLARED_UBIQUITOUS_ANCHORS: dict[tuple[str, str], str] = {
    (aid, arm.canonical_type(t).lower()): why
    for (aid, t), why in _DECLARED_UBIQUITOUS_ANCHORS_RAW.items()
}

def _normalize(a: Archetype) -> _Normalized:
    """Load-time lowercased view of one archetype (the ONE construction seam)."""
    return _Normalized(
        archetype=a,
        anchors=_norm(a.required_any),
        required_all=_norm(a.required_all),
        # `strong` is the UNION of both tiers, so the D-05 scorer's
        # `len(s & entry.strong)` term is arithmetically identical to
        # pre-tiering — the tier split cannot move a single template label.
        strong=_norm(a.supporting_signals) | _norm(a.generic_signals),
        negative=_norm(a.negative_signals),
        supporting=_norm(a.supporting_signals),
        generic=_norm(a.generic_signals),
        anchor_required=a.anchor_required,
    )


_NORMALIZED: tuple[_Normalized, ...] = tuple(_normalize(a) for a in ARCHETYPES)


def validate_catalog(entries: Sequence[Archetype] = ARCHETYPES) -> None:
    """Re-assert every entry's confirmation contract by ITERATING the catalog.

    WR-01: this deliberately inspects each entry's declared
    :class:`ConfirmationPolicy` — never how its ``name_tokens`` are spelled. A
    vocabulary heuristic can only recognise the roles someone already thought of,
    which is how an archetype named ``("network","gateway")`` would have shipped
    with the signal-only path open. Adding an entry therefore cannot bypass this
    check by inventing new words.

    ``Archetype.__post_init__`` already enforces the per-entry invariants at
    construction; running them again here means an invalid catalog cannot even be
    IMPORTED, so a contributor sees the failure immediately rather than as a
    downstream test error.
    """
    if not entries:
        raise ValueError("archetype catalog is empty — every gate over it is vacuous")
    seen: set[str] = set()
    for a in entries:
        if not isinstance(a.confirmation, ConfirmationPolicy):
            raise TypeError(f"{a.id!r} does not declare a ConfirmationPolicy")
        if a.id in seen:
            raise ValueError(f"duplicate archetype id {a.id!r}")
        seen.add(a.id)
        # Re-run the construction-time invariants (defends against a mutated or
        # unpickled entry that never went through __post_init__).
        a.__post_init__()


validate_catalog()


# --------------------------------------------------------------------------- #
# D-15 child-type crediting (Plan 04, remedy C) — matcher input normalization.
# --------------------------------------------------------------------------- #


def _credit_children(s: frozenset[str]) -> frozenset[str]:
    """Credit every ``<parent>/<child>`` ARM type toward its parent (D-15).

    For each element with ``count("/") >= 2`` (a nested type such as
    ``microsoft.sql/servers/databases``), add every ancestor obtained by repeated
    ``rsplit("/", 1)[0]`` DOWN TO — and including — the ``count("/") == 1``
    namespace/type level (``microsoft.sql/servers``), and never below it (never a
    bare ``microsoft.<ns>``, which carries no anchor meaning). Returns the union of
    the input set and all credited ancestors.

    Pure set arithmetic — no RNG/DB/wall-clock. Elements are already lowercased
    (callers pass a :func:`_norm` frozenset), so this operates verbatim. Applied
    ONLY to the matcher's INPUT compare set — never to the catalog anchors/signals
    in :data:`_NORMALIZED`, which are authored at the correct type level already.
    """
    credited = set(s)
    for t in s:
        parent = t
        while parent.count("/") >= 2:
            parent = parent.rsplit("/", 1)[0]
            credited.add(parent)
    return frozenset(credited)


# --------------------------------------------------------------------------- #
# ARCH-02 — the pure, RNG-free, DB-free matcher (D-05 scoring rule).
# --------------------------------------------------------------------------- #


def match_template(type_set: Iterable[str]) -> str:
    """Classify a template's measured ``type_set`` into a workload token.

    Returns a catalog ``name_tokens`` join (e.g. ``"data-platform"``) for a
    confident, unambiguous match, else an honest generic fallback (D-09 split):

    - ``__misc__`` sentinel, or no anchor matched at all → :data:`TOKEN_SHARED`
    - an anchor matched but the score/margin gate failed (killed by negatives or
      an ambiguous tie) → :data:`TOKEN_CORE`

    Scoring (D-05): for the normalized set ``S`` and each archetype ``A`` whose
    ``required_all`` ⊆ ``S`` and ``S ∩ anchors(A) ≠ ∅``::

        score = ANCHOR_WEIGHT
              + STRONG_SIGNAL_WEIGHT * |S ∩ strong(A)|
              - NEGATIVE_PENALTY     * |S ∩ negative(A)|

    Label iff ``best.score >= MIN_SCORE`` AND
    ``best.score - second.score >= MARGIN_THRESHOLD``.
    """
    s = _norm(type_set)
    if not s or _MISC in s:  # empty/sentinel short-circuit BEFORE crediting
        return TOKEN_SHARED
    s = _credit_children(s)  # D-15: credit nested children toward parents (input only)

    scored: list[tuple[float, Archetype]] = []
    for entry in _NORMALIZED:
        if entry.required_all and not entry.required_all <= s:
            continue
        if not (s & entry.anchors):  # anchor missing → no match (score -inf)
            continue
        score = (
            ANCHOR_WEIGHT
            + STRONG_SIGNAL_WEIGHT * len(s & entry.strong)
            - NEGATIVE_PENALTY * len(s & entry.negative)
        )
        scored.append((score, entry.archetype))

    if not scored:  # no anchor matched anywhere → genuinely generic
        return TOKEN_SHARED

    scored.sort(key=lambda pair: pair[0], reverse=True)
    best_score, best = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else float("-inf")

    if best_score >= MIN_SCORE and (best_score - second_score) >= MARGIN_THRESHOLD:
        return "-".join(best.name_tokens)
    return TOKEN_CORE


# --------------------------------------------------------------------------- #
# D-14 confirmation gate (Plan 04, remedy D) — downgrade-only, NEVER relabel.
# Consumed by the pipeline rename pass (19-05) and the live audit (19-06).
# --------------------------------------------------------------------------- #

# Reverse index token -> its _Normalized signature, built once at load beside
# _NORMALIZED (the confirmation gate looks up ONLY the NAMED archetype's evidence).
_BY_TOKEN: dict[str, _Normalized] = {
    "-".join(n.archetype.name_tokens): n for n in _NORMALIZED
}


def _confirms(entry: _Normalized, s: frozenset[str]) -> bool:
    """Does the compare set ``s`` actually PROVE ``entry``'s claim? (remedy 4)

    Tier-aware, and the whole point of ARCH-GAP-02/03. Four rules, in order:

    1. ``required_all`` is a hard prerequisite — mirrors ``match_template``'s
       disqualifier. Inert today (no shipped archetype declares one), but without
       it a future entry could confirm while failing its own precondition: the
       same silent tautology this function exists to kill.
    2. **Anchor path** — any anchor present ⇒ confirmed. The anchor is the type
       that *defines* the shape, so one is definitive evidence.
    3. **Role gate** (ARCH-GAP-03) — an ``anchor_required`` archetype names an
       architectural ROLE (hub / platform / workload / db / app / cluster), and only
       its defining resource proves it. Its supporting signals remain real,
       discriminative evidence and still feed the D-05 SCORE, but they establish the
       DOMAIN, not the ROLE: 109 of 213 network-hub RGs held nsg+routeTables and no
       VNet, and every gate passed. A domain-correct, role-wrong claim is the one
       shape the 19-08 margin rule structurally cannot filter. The gate sits AFTER
       rule 2 deliberately — such an archetype WITH its anchor still confirms
       normally; only the signal-only path is closed, and it is closed BEFORE any
       hit/runner-up arithmetic so no margin can ever resurrect the claim.
    4. **Supporting path** — with no anchor, confirm only on
       ``>= MIN_SUPPORTING_SIGNALS`` discriminative supporting signals AND a
       ``>= CONFIRM_MARGIN`` lead over the best OTHER archetype's supporting
       count. An exact tie is genuinely ambiguous and confirms nothing.

    :attr:`_Normalized.generic` is deliberately never consulted: ubiquitous types
    (and other archetypes' anchors) may nudge the D-05 *score*, but they can never
    CONFIRM a claim. That single exclusion is why an RG holding a lone storage
    account can no longer certify as a DevCenter platform.

    The runner-up scan decides confirm-vs-downgrade ONLY — the runner-up's token is
    never returned, so D-14 (never relabel) holds structurally: the caller looks up
    only the NAMED archetype via :data:`_BY_TOKEN`. Pure set arithmetic over the
    fixed-order :data:`_NORMALIZED` — no RNG, no DB, no wall-clock (T-19-02).
    """
    policy = entry.archetype.confirmation
    if policy is ConfirmationPolicy.GENERIC:
        # Claims nothing, so nothing can confirm it — and nothing needs to. Checked
        # FIRST so a generic entry's evidence tiers are never consulted at all.
        return False
    if entry.required_all and not entry.required_all <= s:
        return False
    if s & entry.anchors:  # anchor path — definitive
        return True
    if policy is ConfirmationPolicy.ANCHOR_REQUIRED:  # role gate — supporting path CLOSED
        return False
    hits = len(s & entry.supporting)
    if hits < MIN_SUPPORTING_SIGNALS:
        return False
    runner_up = max(
        (len(s & other.supporting) for other in _NORMALIZED if other is not entry),
        default=0,  # a single-entry catalog has no runner-up
    )
    return (hits - runner_up) >= CONFIRM_MARGIN


class ConfirmResult(NamedTuple):
    """Outcome of the confirmation gate (D-18 metrics carrier).

    ``token`` = the confirmed template token OR a downgraded generic;
    ``confirmed`` = the materialized set carried the archetype's evidence;
    ``child_credit_decisive`` = evidence appeared ONLY after child-crediting.
    """

    token: str
    confirmed: bool
    child_credit_decisive: bool


def confirm_token_detail(
    template_token: str, materialized_types: Iterable[str]
) -> ConfirmResult:
    """Confirm (or honestly downgrade) a template token against MATERIALIZED types.

    Truth contract (D-14): keep ``template_token`` iff the RG's materialized type
    set actually PROVES that archetype's claim under the tier-aware rule in
    :func:`_confirms` (an anchor, OR multiple supporting signals with a margin —
    never a generic-tier type), after child-credit normalization. Otherwise
    DOWNGRADE to a static generic (``shared`` for empty, ``core`` for
    named-but-unbacked, the D-09 split). It checks ONLY the named archetype — it
    NEVER argmax/relabels to a DIFFERENT archetype (that is the rejected remedy B).
    Pure / RNG-free / DB-free.

    - already-generic ``template_token`` (``shared``/``core``): passthrough,
      never re-promote (``confirmed=False``).
    - unknown token (defensive): returned unchanged, unconfirmed.
    - empty materialized set (D-17): ``TOKEN_SHARED``, unconfirmed.
    """
    if template_token in (TOKEN_SHARED, TOKEN_CORE):
        return ConfirmResult(template_token, False, False)  # generic passthrough
    entry = _BY_TOKEN.get(template_token)
    if entry is None:  # defensive — unknown token, never invent evidence
        return ConfirmResult(template_token, False, False)

    raw = _norm(materialized_types)
    if not raw:  # empty RG (D-17) — no evidence possible
        return ConfirmResult(TOKEN_SHARED, False, False)

    hit_raw = _confirms(entry, raw)  # tier-aware (remedy 4) — NOT anchors|strong
    credited = _credit_children(raw)  # D-15 applied BEFORE the confirmation check
    hit_credited = _confirms(entry, credited)

    if hit_credited:
        return ConfirmResult(template_token, True, hit_credited and not hit_raw)
    # named something, but the contents don't back it -> honest generic (D-09 core)
    return ConfirmResult(TOKEN_CORE, False, False)


def confirm_token(template_token: str, materialized_types: Iterable[str]) -> str:
    """Thin wrapper returning only the confirmed/downgraded token (D-14)."""
    return confirm_token_detail(template_token, materialized_types).token


def build_label_map(templates: Sequence[Mapping[str, object]]) -> dict[str, str]:
    """Map each ``template["id"]`` to its workload token (computed once, D-04).

    Pure function of the read-only profile templates — safe to precompute in the
    parent and thread through, or memoize; both are byte-identical.
    """
    label_map: dict[str, str] = {}
    for tmpl in templates:
        tid = tmpl["id"]
        type_set = tmpl.get("type_set") or []
        label_map[str(tid)] = match_template(type_set)  # type: ignore[arg-type]
    return label_map


def archetype_coverage(
    label_map: Mapping[str, str], template_ids: Iterable[str]
) -> dict[str, int]:
    """Token → RG/template count for the coverage report (D-13).

    Counts each id's token via :class:`collections.Counter`; ids absent from
    ``label_map`` are skipped (never a KeyError).
    """
    counts: Counter[str] = Counter(
        label_map[tid] for tid in template_ids if tid in label_map
    )
    return dict(counts)


def render_coverage_line(coverage: Mapping[str, int]) -> str:
    """Render the D-13 archetype→RG-count coverage summary as ONE compact line.

    ``token=count`` pairs are sorted by count DESCENDING, ties broken by token
    name ascending, so the output is byte-deterministic for a given mapping. The
    generic ``shared``/``core`` tokens are included (never filtered) so the line
    is an honest picture of coverage. An empty mapping renders ``archetypes:
    (none)`` and never raises.

    Pure (no RNG/DB/wall-clock) so it is unit-testable without a DB round-trip.
    """
    if not coverage:
        return "archetypes: (none)"
    pairs = sorted(coverage.items(), key=lambda kv: (-kv[1], kv[0]))
    return "archetypes: " + " ".join(f"{token}={count}" for token, count in pairs)


# The D-18 metric fields, in the FIXED render order — never dict-iteration order,
# so the line is byte-deterministic for a given tally. `already_generic` is tallied
# by the pass but is not a gate outcome, so it stays off the line (D-18 names three).
_RG_NAMING_FIELDS = ("confirmed", "downgraded_to_generic", "child_credit_confirmed")


def render_rg_naming_line(metrics: Mapping[str, int]) -> str:
    """Render the D-18 confirm-and-rename gap metrics as ONE compact line.

    Reports the outcome of the post-materialization confirmation gate:
    ``confirmed`` (RG kept its semantic token), ``downgraded_to_generic`` (the
    name over-claimed and was renamed, D-14), and ``child_credit_confirmed`` (the
    token survived only because child-type crediting was decisive, D-15). A
    missing key renders ``0`` rather than raising — a summary line must never
    break a completed generate run.

    Pure (no RNG/DB/wall-clock) so it is unit-testable without a DB round-trip;
    mirrors :func:`render_coverage_line`'s placement and contract.
    """
    return "rg-naming: " + " ".join(
        f"{field}={metrics.get(field, 0)}" for field in _RG_NAMING_FIELDS
    )
