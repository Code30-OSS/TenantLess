#!/usr/bin/env python3
"""Release gate: no published artifact may carry real-tenant provenance.

Run against a TREE (an export directory, or the repo root):

    uv run python scripts/check_release_provenance.py --tree ../tenantless-public

Three independent checks, because a profile and the numbers computed *from* it
fail in different ways:

1. BUNDLED PROFILES must positively declare ``provenance.synthetic == true`` and
   ``derived_from_real_tenant == false``. Absence is a failure, not a pass --
   an unstamped profile is one whose provenance nobody recorded.

2. NO KNOWN REAL-DERIVED SHAPE may appear in any bundled profile's
   ``source_stats``. A profile that is real-derived is recognisable by the
   estate shape it was fitted from even after every identifier is stripped.

3. NO STALE DERIVED MEASUREMENT may appear in shipped docs. Benchmarks,
   dashboards and marketing figures computed from a real-derived profile carry
   that estate's shape onward even when the profile itself is withheld. Removing
   the profile and keeping its measurements does not resolve the concern.

The magic numbers below are deliberately hard-coded. They are the shapes this
project must never republish, and a gate that reads them from a file the export
could omit would not be a gate.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# The real-derived estate shape the private `enterprise` profile was fitted from.
FORBIDDEN_SOURCE_STATS = {
    "total_subscriptions": 399,
    "total_resource_groups": 6753,
    "total_resources": 96093,
}

# Measurements computed FROM that profile. Benchmarks name the dataset they ran
# against, so these travel independently of the profile file.
FORBIDDEN_MEASUREMENTS = {
    "399": "real-derived subscription count",
    "6753": "real-derived resource-group count",
    "96093": "real-derived resource count",
    "8383": "resource-group count of the real-derived benchmark dataset",
    "192138": "resource count of the real-derived benchmark dataset",
}

# Stale PERFORMANCE claims from runs against the real-derived profile, and the
# real source scan's file size. Unlike the counts above these are matched as
# PHRASES: the bare numbers ("832", "1.4") are far too common to gate on, but the
# phrasings below only occur when someone is restating a real-derived measurement.
#
# Kept separate from FORBIDDEN_MEASUREMENTS because these are scanned across
# CODE as well as docs -- the claims lived in a Rust doc-comment and a Python
# docstring, neither of which a docs-only sweep would have seen.
FORBIDDEN_CLAIMS: dict[str, str] = {
    r"520\s*[Kk]\b": "the 520K-resource figure from the real-derived Phase 13 run",
    r"\b520[,_]?009\b": "the exact resource count of the real-derived Phase 13 run",
    r"\b832\s*s\b": "the 832-second generation time of the real-derived Phase 13 run",
    r"\b26\s*[-–]\s*48\s*ms\b": "the p95 latency range measured on the real-derived profile",
    r"\b1\.4\s*GB\b": "the file size of the real customer scan",
}

DOC_SUFFIXES = {".md", ".json", ".html", ".txt", ".csv"}
# Claims hide in code comments too, so the claim sweep covers source as well.
CODE_SUFFIXES = {".py", ".rs", ".ts", ".tsx", ".sql", ".toml", ".yml", ".yaml"}
SKIP_DIRS = {
    ".git", "node_modules", "target", ".venv", "__pycache__", ".planning",
    ".pytest_cache", "dist", "build", ".codegraph", ".mypy_cache", ".ruff_cache",
    # Local tooling artifacts. Never exported, but present in a maintainer's
    # working tree, where they would otherwise bury the real findings.
    ".playwright-mcp", ".remember", "scratchpad", ".claude",
}

# This file necessarily contains the very numbers and phrases it forbids -- they
# ARE the pattern table. Unlike the scrub gate (which was able to move its words
# into a data file and now scans itself), a regex like `520\s*[Kk]` cannot be
# written without the digits, so the exemption is unavoidable here. It is safe
# for the same reason it is unavoidable: the file contains patterns, not claims.
_SELF = Path(__file__).resolve()


def _iter_files(tree: Path, suffixes: set[str]):
    for path in tree.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.resolve() == _SELF:
            continue
        yield path


def check_bundled_profiles(tree: Path) -> list[str]:
    """Checks 1 and 2 -- every bundled profile declares synthetic provenance."""
    errors: list[str] = []
    profile_dir = tree / "src" / "tenantless" / "profiles"
    if not profile_dir.is_dir():
        return [f"no bundled-profile directory at {profile_dir}"]

    profiles = sorted(profile_dir.glob("*.json"))
    if not profiles:
        return [f"{profile_dir} contains no profiles -- nothing to certify"]

    for path in profiles:
        rel = path.relative_to(tree).as_posix()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{rel}: unreadable ({exc})")
            continue

        prov = data.get("provenance") or {}
        if prov.get("synthetic") is not True:
            errors.append(
                f"{rel}: provenance.synthetic is {prov.get('synthetic')!r}, must be true. "
                "A bundled profile has to state that it has no real-tenant ancestor."
            )
        if prov.get("derived_from_real_tenant") is not False:
            errors.append(
                f"{rel}: provenance.derived_from_real_tenant is "
                f"{prov.get('derived_from_real_tenant')!r}, must be false."
            )

        stats = data.get("source_stats") or {}
        if stats == FORBIDDEN_SOURCE_STATS:
            errors.append(
                f"{rel}: source_stats matches the known real-derived estate shape "
                f"{FORBIDDEN_SOURCE_STATS} -- this is the private profile, STOP."
            )
    return errors


def check_derived_measurements(tree: Path) -> list[str]:
    """Check 3 -- no shipped doc cites a measurement from the real-derived run."""
    errors: list[str] = []
    for path in _iter_files(tree, DOC_SUFFIXES):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for number, what in FORBIDDEN_MEASUREMENTS.items():
            # Word-boundary match so 96093 fires but 960931 does not.
            if re.search(rf"(?<!\d){re.escape(number)}(?!\d)", text):
                errors.append(
                    f"{path.relative_to(tree).as_posix()}: contains {number} "
                    f"({what}) -- regenerate this artifact from the synthetic "
                    f"profile or exclude it."
                )
    return errors


def check_stale_performance_claims(tree: Path) -> list[str]:
    """Check 4 -- no shipped file restates a measurement from the real-derived run.

    Separate from check 3 because these are phrases rather than bare counts, and
    because they must be looked for in CODE as well as docs: the Stage 3 review
    found surviving claims in a Rust doc-comment and a Python docstring, which a
    docs-only sweep had walked straight past.
    """
    errors: list[str] = []
    compiled = [(re.compile(p), why) for p, why in FORBIDDEN_CLAIMS.items()]
    for path in _iter_files(tree, DOC_SUFFIXES | CODE_SUFFIXES):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for rx, why in compiled:
                if rx.search(line):
                    errors.append(
                        f"{path.relative_to(tree).as_posix()}:{lineno}: restates "
                        f"{why} -- regenerate it from the synthetic profile or remove it."
                    )
    return errors


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--tree", type=Path, default=Path("."), help="Tree to check (default: cwd)"
    )
    args = ap.parse_args()
    tree = args.tree.resolve()

    errors = (
        check_bundled_profiles(tree)
        + check_derived_measurements(tree)
        + check_stale_performance_claims(tree)
    )

    if errors:
        print(f"PROVENANCE GATE FAILED ({len(errors)} finding(s)) in {tree}:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print(f"Provenance gate PASSED for {tree}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
