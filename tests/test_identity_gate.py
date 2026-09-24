"""Structural anti-normalization gate for the canonical ARM-ID identity paths.

Every stateful identity comparison (equality / collision / lookup / dedup / ownership)
goes through ONE documented fold: ``synthetic.arm_id_key`` / ``synthetic.ascii_fold`` in
SQL, ``arm_id::arm_id_key`` / ``arm_id::ascii_fold`` in Rust, ``tenantless.identity`` in
Python — all ASCII ``A-Z -> a-z`` only, pinned byte-identical by the shared KAT corpus. A
locale-aware lowercase (SQL ``lower()``, Python ``str.lower()``, Rust ``to_lowercase()``)
folds non-ASCII letters the key keeps distinct, so reintroducing one in an identity path
silently splits identity between engines.

This gate scans EXACTLY the migrated identity-path source set for the forbidden tokens
``lower(``, ``.lower()``, ``.to_lowercase()`` and ``to_ascii_lowercase()``. A hit passes only
when it carries an explicit sanctioned marker on the same line or the line directly above::

    IDENTITY-ALLOW[<category>: <reason>]

with ``<category>`` from the closed set ``structural`` (an ARM path-segment keyword or
UUID-segment check, not an identity comparison) or ``protocol`` (an HTTP header value, not
an ARM id). The fold functions themselves never match (``arm_id_key(`` / ``ascii_fold(``).

Comments, Python docstrings and Rust ``#[cfg(test)]`` modules are blanked first (the
source-aware stripping shared with the reader-inventory gate), while executable string
literals — runtime SQL — stay in scope. Search / presentation / analyzer normalization
outside the identity set is deliberately NOT scanned (it may keep ``lower()``).

Non-vacuous: the positive-control fixtures under ``tests/fixtures/identity_gate/`` plant
bare, unmarked normalizations in each language and the scanner MUST flag them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# tests/ is on sys.path (conftest) — reuse the reader-inventory gate's source-aware stripping.
from test_reader_inventory_gate import _strip  # noqa: E402

_REPO = Path(__file__).resolve().parents[1]
_FIXTURES = _REPO / "tests" / "fixtures" / "identity_gate"

# The migrated identity-path source set (the stateful seams + the RG-name component
# predicates + the resolver / cutover migrations). Deliberately NOT the whole tree.
IDENTITY_FILES: tuple[str, ...] = (
    "mock-server/src/write_merge.rs",
    "mock-server/src/handlers/resource_write.rs",
    "mock-server/src/handlers/resource_detail.rs",
    "mock-server/src/handlers/resource_groups.rs",
    "mock-server/src/handlers/resources.rs",
    "mock-server/src/handlers/cost.rs",
    "src/tenantless/cli.py",
    "src/tenantless/generator/drift.py",
    "sql/010_arm_resolver.sql",
    "sql/011_arm_id_key.sql",
    "sql/012_arm_id_identity_cutover.sql",
)

_LANG_BY_SUFFIX = {".rs": "rust", ".py": "python", ".sql": "sql"}

# Locale-dependent normalization tokens. `lower(` must not be part of a longer identifier
# (so `id_lower` / `ascii_fold(` / `arm_id_key(` never match).
_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])lower\s*\(|\.to_lowercase\(\)|to_ascii_lowercase\(\)"
)
_MARKER_RE = re.compile(r"IDENTITY-ALLOW\[(structural|protocol): ([^\]\n]{3,})\]")

# The ONE sanctioned lower() in the identity set: the canonical fold body itself. With an
# explicit C collation PostgreSQL's lower() maps ASCII A-Z only and leaves every other byte
# untouched, independent of the database locale (pinned by the KAT corpus). It is allowed
# ONLY as this exact body line inside a `CREATE OR REPLACE FUNCTION synthetic.ascii_fold(` /
# `synthetic.arm_id_key(` definition (sql/011 and its sql/010 prelude copy) — the same token
# anywhere else, or any other lower() form inside those bodies, still trips the gate.
_FOLD_FN_HEAD_RE = re.compile(r"^\s*CREATE OR REPLACE FUNCTION synthetic\.(ascii_fold|arm_id_key)\(")
_SANCTIONED_FOLD_BODY = 'SELECT lower($1 COLLATE "C")'


def _sanctioned_fold_lines(stripped: list[str]) -> set[int]:
    """Indices of the exact sanctioned fold-body lines inside the two fold definitions."""
    sanctioned: set[int] = set()
    in_fn = in_body = False
    for idx, line in enumerate(stripped):
        text = line.strip()
        if not in_fn:
            in_fn = bool(_FOLD_FN_HEAD_RE.match(line))
            in_body = False
            continue
        if text == "$$;":
            in_fn = in_body = False
        elif not in_body:
            in_body = text.endswith("AS $$")
        elif text == _SANCTIONED_FOLD_BODY:
            sanctioned.add(idx)
    return sanctioned


def scan(text: str, lang: str) -> list[tuple[int, str]]:
    """Return ``(line_no, raw_line)`` for every unmarked forbidden token in ``text``."""
    stripped = _strip(text.replace("\r\n", "\n"), lang).split("\n")
    raw = text.replace("\r\n", "\n").split("\n")
    fold_body = _sanctioned_fold_lines(stripped) if lang == "sql" else set()
    hits: list[tuple[int, str]] = []
    for idx, code in enumerate(stripped):
        if not _TOKEN_RE.search(code) or idx in fold_body:
            continue
        marked = _MARKER_RE.search(raw[idx]) or (idx > 0 and _MARKER_RE.search(raw[idx - 1]))
        if not marked:
            hits.append((idx + 1, raw[idx].strip()))
    return hits


def _scan_file(rel: str) -> list[tuple[int, str]]:
    path = _REPO / rel
    return scan(path.read_text(encoding="utf-8"), _LANG_BY_SUFFIX[path.suffix])


# --------------------------------------------------------------------------- #
# Positive controls (non-vacuity) — the scanner MUST flag planted normalizations
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("fixture", "lang", "expected_lines"),
    [
        ("planted_bare_lower_sql.txt", "sql", [9]),
        ("planted_bare_lower_py.txt", "python", [7, 8]),
        ("planted_bare_lower_rs.txt", "rust", [7, 9]),
    ],
)
def test_positive_control_bare_normalization_is_flagged(fixture, lang, expected_lines):
    text = (_FIXTURES / fixture).read_text(encoding="utf-8")
    assert [line for line, _ in scan(text, lang)] == expected_lines


def test_bare_lower_id_line_trips_and_marker_needs_a_category_and_reason():
    assert scan("WHERE o.id_lower = lower(b.id)\n", "sql") == [
        (1, "WHERE o.id_lower = lower(b.id)")
    ]
    # An unknown category or an empty reason is not a sanctioned marker.
    assert scan("x = lower(y) -- IDENTITY-ALLOW[whatever: because]\n", "sql")
    assert scan("x = lower(y) -- IDENTITY-ALLOW[structural: ]\n", "sql")
    assert not scan("x = lower(y) -- IDENTITY-ALLOW[structural: segment keyword]\n", "sql")
    # The fold functions never match.
    assert not scan("WHERE id_lower = synthetic.arm_id_key($1) AND ascii_fold(rg)\n", "sql")


def _fold_fn(name: str, body: str) -> str:
    return (
        f"CREATE OR REPLACE FUNCTION synthetic.{name}(t text)\n"
        "    RETURNS text\n"
        "    LANGUAGE sql\n"
        "    IMMUTABLE STRICT PARALLEL SAFE\n"
        "    AS $$\n"
        f"    {body}\n"
        "$$;\n"
    )


def test_only_the_exact_fold_body_may_use_c_collation_lower():
    sanctioned = 'SELECT lower($1 COLLATE "C")'
    # The exact body inside either fold definition is the one sanctioned lower().
    assert not scan(_fold_fn("ascii_fold", sanctioned), "sql")
    assert not scan(_fold_fn("arm_id_key", sanctioned), "sql")
    # Any other lower() form inside a fold body still trips (locale lower, other operand,
    # a different collation, a composed expression).
    for body in (
        "SELECT lower($1)",
        'SELECT lower(t COLLATE "C")',
        'SELECT lower($1 COLLATE "tr-TR-x-icu")',
        'SELECT lower($1 COLLATE "C") || lower($1)',
    ):
        assert scan(_fold_fn("ascii_fold", body), "sql") == [(6, body)], body
    # The same body in any other function, or outside a function body, still trips.
    assert scan(_fold_fn("other_fold", sanctioned), "sql") == [(6, sanctioned)]
    assert scan(f"{sanctioned};\n", "sql") == [(1, f"{sanctioned};")]
    assert scan("WHERE o.id_lower = lower(b.id COLLATE \"C\")\n", "sql")
    # The allowance is SQL-only: the same text in Python / Rust sources still trips.
    assert scan(_fold_fn("ascii_fold", sanctioned), "python")
    assert scan(_fold_fn("ascii_fold", sanctioned), "rust")
    # Before the `AS $$` body opens, the exact text is not a body line and still trips.
    no_body = f"CREATE OR REPLACE FUNCTION synthetic.ascii_fold(t text)\n    {sanctioned}\n"
    assert scan(no_body, "sql") == [(2, sanctioned)]


def test_fold_definitions_use_the_sanctioned_body():
    """sql/011 and its sql/010 prelude copy each carry exactly one sanctioned fold body (the
    ascii_fold primitive); arm_id_key stays a wrapper over it."""
    for rel in ("sql/011_arm_id_key.sql", "sql/010_arm_resolver.sql"):
        text = (_REPO / rel).read_text(encoding="utf-8").replace("\r\n", "\n")
        stripped = _strip(text, "sql").split("\n")
        assert len(_sanctioned_fold_lines(stripped)) == 1, rel


# --------------------------------------------------------------------------- #
# The real identity-path tree is clean
# --------------------------------------------------------------------------- #


def test_identity_paths_use_only_the_canonical_fold():
    violations = {rel: _scan_file(rel) for rel in IDENTITY_FILES}
    violations = {rel: hits for rel, hits in violations.items() if hits}
    assert not violations, (
        "locale-dependent normalization in an identity path (use arm_id_key / ascii_fold, "
        "or mark a non-identity use with IDENTITY-ALLOW[structural|protocol: <reason>]):\n"
        + "\n".join(
            f"  {rel}:{line}: {src}" for rel, hits in violations.items() for line, src in hits
        )
    )


def test_identity_file_set_exists_and_is_scanned():
    for rel in IDENTITY_FILES:
        assert (_REPO / rel).is_file(), rel
    # The identity set is scoped: search / presentation / analyzer code is out of scope and
    # may legitimately keep case-insensitive lower() filters.
    assert not any("analyzer" in rel or rel.endswith("sim.rs") for rel in IDENTITY_FILES)


def test_structural_split_part_keyword_matches_are_marked_in_the_resolver():
    """The resolver's only remaining lower() calls are the structural segment checks."""
    text = (_REPO / "sql" / "010_arm_resolver.sql").read_text(encoding="utf-8")
    code = _strip(text.replace("\r\n", "\n"), "sql")
    assert "synthetic.arm_id_key(b.id)" in code and "synthetic.arm_id_key(g.id)" in code
    lower_lines = [
        ln
        for ln in text.splitlines()
        if _TOKEN_RE.search(ln.split("--")[0]) and ln.strip() != _SANCTIONED_FOLD_BODY
    ]
    assert lower_lines, "the structural split_part checks exist"
    assert all("split_part" in ln and "IDENTITY-ALLOW[structural:" in ln for ln in lower_lines)


# --------------------------------------------------------------------------- #
# The sql/010 fold prelude is byte-identical to the canonical sql/011 definitions
# --------------------------------------------------------------------------- #

_FN_RE = re.compile(r"CREATE OR REPLACE FUNCTION synthetic\.\w+\(.*?\n\$\$;", re.DOTALL)


def test_resolver_fold_prelude_matches_the_canonical_definitions():
    canon = _FN_RE.findall(
        (_REPO / "sql" / "011_arm_id_key.sql").read_text(encoding="utf-8").replace("\r\n", "\n")
    )
    prelude = _FN_RE.findall(
        (_REPO / "sql" / "010_arm_resolver.sql").read_text(encoding="utf-8").replace("\r\n", "\n")
    )
    assert len(canon) == 2, canon
    assert prelude == canon, "sql/010's fold prelude must be byte-identical to sql/011"
