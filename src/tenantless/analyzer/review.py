"""ANLZ-10 human-review dump of a derived profile (report-only, D-04).

The analyzer always writes a ``<profile>_review.txt`` companion next to the
profile and -- in interactive mode -- also prints it to stdout, so a human can
eyeball coverage / skips / the unique string surface BEFORE the profile feeds the
generator. This is a *report*, never a gate: the real privacy guardrails are the
min-aggregation + denylist scan in :func:`profile.build_profile`. The review
functions therefore NEVER raise/block on a valid profile (D-04) -- a malformed
section is rendered as best it can be, never an exception.

Trust boundary (T-06-14): the review runs AFTER the privacy layer, so every
string it surfaces is already min-bucketed + denylist-clean. It only re-presents
already-safe strings grouped by category; it introduces no new disclosure.

Mirrors the ``profile.py`` output-path + ``write_bytes`` pattern for the file
write, and the ``cli.py`` ``sys.stdin.isatty()`` interactive check upstream.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Container/grouping categories surfaced in the review, in display order. Each
# entry is a (heading, collector) pair; the collector pulls the unique strings
# for that category out of the profile dict. A category that yields nothing is
# simply omitted from the dump (never an empty noisy block).
_NEVER_RAISES = (
    "ANLZ-10 review is report-only (D-04): it must never raise on a valid "
    "profile; the privacy/denylist scan in build_profile is the real gate."
)


def _as_str_keys(value: Any) -> list[str]:
    """Return the dict keys of ``value`` as strings, or ``[]`` if not a dict."""
    if isinstance(value, dict):
        return [str(k) for k in value.keys()]
    return []


def _collect_grouped(profile: dict[str, Any]) -> dict[str, list[str]]:
    """Collect ALL unique string values in the profile GROUPED BY category.

    Report-only: tolerant of missing / oddly-shaped sections (returns whatever it
    can, never raises). The categories mirror the human-meaningful surfaces a
    reviewer eyeballs: resource types, tag keys, tag values, locations, RG
    template ids, naming pattern classes, archetype ids, governance types, and
    the provenance coverage verdicts.
    """
    groups: dict[str, set[str]] = {}

    def add(category: str, values: list[str]) -> None:
        if not values:
            return
        groups.setdefault(category, set()).update(values)

    # source_stats -- shown as labelled scalars so "total_resources" surfaces.
    stats = profile.get("source_stats")
    if isinstance(stats, dict):
        add(
            "source_stats",
            [f"{k}={v}" for k, v in stats.items()],
        )

    # Resource types (the distribution keys).
    add("resource_types", _as_str_keys(profile.get("resource_type_distributions")))

    # Tag keys + tag values (already bucketed/denylist-clean).
    tags = profile.get("tag_distributions")
    if isinstance(tags, dict):
        add("tag_keys", _as_str_keys(tags.get("key_frequencies")))
        value_dists = tags.get("value_distributions")
        if isinstance(value_dists, dict):
            tag_values: list[str] = []
            for per_key in value_dists.values():
                tag_values.extend(_as_str_keys(per_key))
            add("tag_values", tag_values)

    # Locations (per-archetype location distributions) + archetype ids.
    archetypes = profile.get("subscription_archetypes")
    if isinstance(archetypes, list):
        arch_ids: list[str] = []
        locations: list[str] = []
        for arch in archetypes:
            if not isinstance(arch, dict):
                continue
            if "id" in arch:
                arch_ids.append(str(arch["id"]))
            locations.extend(_as_str_keys(arch.get("location_distribution")))
        add("archetype_ids", arch_ids)
        add("locations", locations)

    # RG template ids + their type-set members.
    rg_templates = profile.get("resource_group_templates")
    if isinstance(rg_templates, list):
        rg_ids: list[str] = []
        for tmpl in rg_templates:
            if not isinstance(tmpl, dict):
                continue
            if "id" in tmpl:
                rg_ids.append(str(tmpl["id"]))
        add("rg_template_ids", rg_ids)

    # Governance violation types.
    gov = profile.get("governance_violations")
    if isinstance(gov, dict):
        add("governance_types", _as_str_keys(gov.get("type_frequencies")))

    # Naming structural pattern classes (never verbatim names -- the extractor
    # already tokenized these into structural classes).
    naming = profile.get("naming_conventions")
    if isinstance(naming, dict):
        add("naming_patterns", _as_str_keys(naming.get("pattern_frequencies")))

    # Provenance coverage verdicts (derived / insufficient_coverage / no_source).
    prov = profile.get("provenance")
    if isinstance(prov, dict):
        coverage = prov.get("coverage")
        if isinstance(coverage, dict):
            add(
                "provenance_coverage",
                [f"{field}: {verdict}" for field, verdict in coverage.items()],
            )

    # Freeze to sorted lists for a stable, human-readable dump.
    return {category: sorted(values) for category, values in groups.items()}


def render(profile: dict[str, Any]) -> str:
    """Return the human-readable grouped-unique-string review as text.

    Report-only (D-04): never raises on a valid profile. The same text is what
    :func:`write_review` writes to ``<out>_review.txt`` and what the CLI prints in
    interactive mode.
    """
    groups = _collect_grouped(profile)
    lines: list[str] = ["# Profile review (report-only -- never blocks)", ""]
    if not groups:
        lines.append("(no reviewable string surface found)")
        return "\n".join(lines) + "\n"
    for category in sorted(groups):
        values = groups[category]
        lines.append(f"## {category} ({len(values)})")
        for value in values:
            lines.append(f"  - {value}")
        lines.append("")
    return "\n".join(lines) + "\n"


# The plan names this ``format_review``; the Wave-0 test scaffold names it
# ``render``. Keep both pointing at the one implementation.
format_review = render


def _review_path(out: str | Path) -> Path:
    """Derive ``<out-stem>_review.txt`` next to the profile (mirror profile.py)."""
    out_path = Path(out)
    return out_path.with_name(f"{out_path.stem}_review.txt")


def write_review(profile: dict[str, Any], out: str | Path) -> str:
    """Always write ``<out>_review.txt`` (grouped unique strings); return the text.

    Report-only (D-04): never raises on a valid profile -- a sparse/odd section is
    rendered best-effort, never an exception. Mirrors the ``profile.py`` output
    path + ``write_bytes`` pattern. The returned text lets the CLI print it in
    interactive mode without re-rendering.
    """
    text = render(profile)
    review_path = _review_path(out)
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(text, encoding="utf-8")
    return text
