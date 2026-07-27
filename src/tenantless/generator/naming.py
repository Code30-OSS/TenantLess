"""Azure-naming-convention-shaped synthetic names (D-11, planner discretion).

Names look like real Azure naming conventions (``{bu}-{env}-{workload}-{abbrev}
[-nn]``) but carry ZERO real identifiers: every component is drawn from a small
SEEDED synthetic vocabulary via the injected ``SeededContext`` (no profile string
value is ever echoed into a name). This mirrors the analyzer's "ids are synthetic"
rule (``archetype-N`` / ``template-N``), extended to an Azure-shaped grammar.

All draws flow from the one seed (D-03); Azure name constraints are enforced per
type (resource-group names: <=90 chars, allowed charset).
"""

from __future__ import annotations

import re

from .rng import SeededContext

# Small SEEDED synthetic vocabularies — NOT sourced from any profile/real data.
_BUSINESS_UNITS = (
    "fin", "hr", "ops", "eng", "data", "sec", "mktg", "sales",
    "infra", "plat", "corp", "retail", "logi", "support", "rnd",
)
_ENVIRONMENTS = ("prod", "stg", "dev", "test", "sbx", "uat")
_WORKLOADS = (
    "payments", "portal", "api", "etl", "billing", "auth", "catalog",
    "search", "ingest", "report", "gateway", "cache", "queue", "ml",
    "backup", "monitor", "identity", "web", "batch", "stream",
)

# RG name charset: Azure allows alphanumerics, underscore, parentheses, hyphen,
# period (no trailing period). We keep it to a safe lowercase alnum+hyphen set.
_RG_SAFE = re.compile(r"[^a-z0-9-]")
_RG_MAX_LEN = 90


def _word(ctx: SeededContext, vocab: tuple[str, ...]) -> str:
    """Deterministically pick one synthetic vocabulary word."""
    idx = int(ctx.rng.integers(0, len(vocab)))
    return vocab[idx]


def subscription_name(ctx: SeededContext) -> str:
    """An Azure-shaped synthetic subscription display name.

    Pattern: ``{bu}-{env}-{workload}-sub`` (e.g. ``fin-prod-payments-sub``).
    """
    bu = _word(ctx, _BUSINESS_UNITS)
    env = _word(ctx, _ENVIRONMENTS)
    workload = _word(ctx, _WORKLOADS)
    return f"{bu}-{env}-{workload}-sub"


def resource_group_name(ctx: SeededContext, *, workload: str) -> str:
    """An Azure-shaped synthetic resource-group name.

    Pattern: ``rg-{bu}-{env}-{workload}-{nn}`` (e.g. ``rg-eng-dev-web-app-07``).

    ARCH-03 / D-08: the ``workload`` token is INJECTED by the caller — it is the
    archetype label of the RG's measured contents (via
    :func:`archetypes.build_label_map`), NOT a random ``_WORKLOADS`` draw. This
    makes the RG name semantically coherent with what it holds while ``bu``/``env``
    remain independent random draws (any team/env owns any archetype).

    Determinism: removing the old ``_WORKLOADS`` draw is the ONLY RNG change — the
    token adds NO new draw (it is a pure function of the already-sampled template),
    so a fixed ``(ctx, workload)`` stays byte-reproducible. Enforces the RG charset
    and the 90-char limit (Azure constraint).
    """
    bu = _word(ctx, _BUSINESS_UNITS)
    env = _word(ctx, _ENVIRONMENTS)
    suffix = int(ctx.rng.integers(1, 100))
    name = f"rg-{bu}-{env}-{workload}-{suffix:02d}"
    name = _RG_SAFE.sub("-", name.lower())
    return name[:_RG_MAX_LEN].rstrip("-.")
