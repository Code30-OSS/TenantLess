#!/usr/bin/env python3
"""Reproducibly build the LOCAL privacy denylist from a real DuckDB scan.

The denylist is the gitignored backstop the analyzer's ``--denylist`` scan
checks the assembled profile against (see ``src/tenantless/analyzer/privacy.py``).
It must contain the real tenant identifiers that must NEVER appear in a
committed profile: subscription ids + display names, resource-group ids +
names, resource ids + names, and the raw tag VALUES across all three levels.

The analyzer's STRUCTURAL controls (field allow-lists + tag key/value guards)
are the primary defense and already keep these out of the profile; this
denylist is defense-in-depth so a regression is caught loudly.

Output is ``{"terms": [...]}`` written to ``--out`` (default the gitignored
``profiles/.scan-denylist.json``). The DuckDB is opened READ-ONLY and is
never copied into the repo.

Usage:
    uv run python scripts/build-denylist.py \
        --source duckdb:/path/to/your-scan.duckdb

Exits 0 on success, 1 on failure. After running, confirm the output stays
ignored: ``git check-ignore profiles/.scan-denylist.json``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Tokens too generic to be a tenant identifier; including them only causes
# false-positive collisions with required Azure vocabulary in the profile.
_BOOLEANS = {"true", "false", "null", "none", "yes", "no"}
# Minimum length for a term to be a plausible identifier (drops "BU", "01", ...).
_MIN_LEN = 3

# Identifier-SHAPE predicates. Tag VALUES are noisy -- they include enums,
# version strings (``1.0``), region codes (``eastus2``) and sizes that are NOT
# tenant identifiers and would only cause false-positive collisions with
# required Azure vocabulary in the profile. So tag values are kept ONLY when
# structurally identifier-shaped (matching the curate-to-identifier-shaped
# policy). The id/name COLUMNS are kept as-is (they are real identifiers by
# definition); the boundary-aware scan tolerates short names.
_UUIDISH = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}")
_LONG_HEX = re.compile(r"[0-9a-fA-F]{16,}")
_PATH_MARKERS = ("/subscriptions/", "/resourcegroups/", "/providers/")


def _keep(term: str | None) -> bool:
    """True if ``term`` is a plausible real identifier worth denylisting."""
    if term is None:
        return False
    t = term.strip()
    if len(t) < _MIN_LEN:
        return False
    if t.lower() in _BOOLEANS:
        return False
    if t.isdigit():  # bare counts / ports / years are not identifiers
        return False
    return True


def _identifier_shaped(value: str) -> bool:
    """True if a tag VALUE is structurally a real identifier (UUID/hex/path)."""
    if any(m in value.lower() for m in _PATH_MARKERS):
        return True
    return bool(_UUIDISH.search(value) or _LONG_HEX.search(value))


def _column(conn, sql: str) -> list[str]:
    """Run ``sql`` (single string column) and return the non-null values."""
    return [r[0] for r in conn.execute(sql).fetchall() if r[0] is not None]


def _tag_values(conn, table: str) -> list[str]:
    """Distinct raw tag VALUES across all tag keys of ``table``."""
    return _column(
        conn,
        f"""
        SELECT DISTINCT json_extract_string(tags, kv.key) AS v
        FROM {table}, (SELECT unnest(json_keys(tags)) AS key) AS kv
        WHERE tags IS NOT NULL
        """,
    )


def _boundary_hit(term: str, s: str) -> bool:
    """True if ``term`` appears as a whole identifier token in ``s``.

    Mirrors the boundary-aware match in ``tenantless.analyzer.privacy`` so the
    built denylist is filtered by exactly the rule the scan applies.
    """
    n = len(term)
    start = s.find(term)
    while start != -1:
        before = s[start - 1] if start > 0 else ""
        after = s[start + n] if start + n < len(s) else ""
        if not (before.isalnum() or before == "_") and not (
            after.isalnum() or after == "_"
        ):
            return True
        start = s.find(term, start + 1)
    return False


def _profile_strings(profile_path: Path) -> list[str]:
    """All unique key/value strings in a reference profile JSON."""
    data = json.loads(profile_path.read_text(encoding="utf-8"))
    seen: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(k, str):
                    seen.add(k)
                walk(v)
        elif isinstance(node, (list, tuple)):
            for x in node:
                walk(x)
        elif isinstance(node, str):
            seen.add(node)

    walk(data)
    return list(seen)


def build_terms(conn, reference_profile: Path | None = None) -> list[str]:
    """Collect all real-identifier terms from the scan tables.

    Identifier columns (ids/names) are kept as real identifiers by definition.
    Tag VALUES are kept only when structurally identifier-shaped (UUID/hex/path),
    since generic enum/version/region tag values are not identifiers.

    If ``reference_profile`` is given, any term that collides (as a whole token)
    with that profile is dropped: the committed profile is structurally
    identifier-free, so a colliding term is provably generic Azure vocabulary
    (``1.0``, a region code, a name that doubles as an enum) and would only
    false-positive the scan -- never a real-identifier leak.
    """
    terms: set[str] = set()

    # Identifier columns (ids are resource-path / UUID shaped; names are real).
    terms.update(_column(conn, "SELECT subscription_id FROM subscriptions"))
    terms.update(_column(conn, "SELECT display_name FROM subscriptions"))
    terms.update(_column(conn, "SELECT resource_group_id FROM resource_groups"))
    terms.update(_column(conn, "SELECT name FROM resource_groups"))
    terms.update(_column(conn, "SELECT resource_id FROM resources"))
    terms.update(_column(conn, "SELECT name FROM resources"))

    # Raw tag values at every level, but only the identifier-SHAPED ones.
    for table in ("subscriptions", "resource_groups", "resources"):
        terms.update(v for v in _tag_values(conn, table) if _identifier_shaped(v))

    kept = {t.strip() for t in terms if _keep(t)}

    if reference_profile is not None and reference_profile.exists():
        strings = _profile_strings(reference_profile)
        before = len(kept)
        kept = {
            t for t in kept if not any(_boundary_hit(t, s) for s in strings if t in s)
        }
        dropped = before - len(kept)
        print(
            f"Curated against {reference_profile.name}: dropped {dropped} "
            f"generic terms that collide with clean profile vocabulary."
        )

    return sorted(kept)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        required=True,
        help="Path to the real DuckDB scan (opened read-only).",
    )
    parser.add_argument(
        "--out",
        default="profiles/.scan-denylist.json",
        help="Output path for the denylist JSON (default: gitignored local file).",
    )
    parser.add_argument(
        "--profile",
        default="profiles/derived.json",
        help=(
            "Reference clean profile to curate against: terms that collide "
            "(whole-token) with it are dropped as provably-generic vocabulary. "
            "Pass an empty string to skip curation."
        ),
    )
    args = parser.parse_args()

    src = Path(args.source)
    if not src.exists():
        print(f"FAIL: source DuckDB not found: {src}")
        return 1

    try:
        import duckdb
    except ImportError:
        print("FAIL: duckdb is not installed (uv sync)")
        return 1

    reference = Path(args.profile) if args.profile else None

    conn = duckdb.connect(str(src), read_only=True)
    try:
        terms = build_terms(conn, reference_profile=reference)
    finally:
        conn.close()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"terms": terms}, ensure_ascii=False, indent=0),
        encoding="utf-8",
    )

    print(f"Wrote {out}: {len(terms)} identifier terms.")
    print("Reminder: confirm it stays ignored ->")
    print(f"  git check-ignore {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
