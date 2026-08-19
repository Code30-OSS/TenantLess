"""Structural reader-inventory gate.

The enforcement teeth: a whole-tree structural scan that FAILS CI if any
resource-facing read of the raw ``synthetic.resources`` baseline lacks an explicit, narrow,
symbol-specific sanctioned-use marker. This is the ONLY thing that protects the
unified resolver boundary for future code no behavioral test happens to exercise — a new direct
read of ``synthetic.resources`` (bypassing ``synthetic.arm_resolved_resources``) would leak
tombstoned / stale-baseline data into the console / search / violations / dependency surfaces.

WHAT IS SCANNED
===============
Runtime/shipped source only — the positive enumeration below, NOT the whole tree:
  * ``mock-server/src/**/*.rs``   (Rust handlers/lib/job)
  * ``sql/**/*.sql``              (schema + resolver views)
  * ``src/tenantless/**/*.py``    (generator + CLI)
  * ``mock-server/tests/common/*.sql`` (the canonical hash SQL, read at runtime by product tests)
Test directories (``tests/``, ``mock-server/tests/*.rs``) and ``scripts/`` are OUT of the enforced
scan by construction — they are not runtime resolver consumers, and folding them in would flag the
many legitimate test/bench readers. Within the Rust source, ``#[cfg(test)]`` modules are blanked so
the two SQL-injection *test attack-string* literals (``"'; DROP TABLE synthetic.resources;--"``)
are out of scope (they are DDL-shaped anyway — see the DDL exemption below).

THE MARKER CONVENTION
============================
    SYNRES-ALLOW[<category>]: <non-empty reason>
A marker associates with EXACTLY ONE ``synthetic.resources`` occurrence: trailing on the SAME
line as that occurrence, OR on the single immediately-preceding line (K=1). Each occurrence needs
its OWN marker; one marker can NEVER cover two occurrences (a bypass placed next to a sanctioned
read does not inherit its marker). A well-formed marker with NO adjacent occurrence (floating) is
itself a violation. The category must come from the CLOSED set; the reason must be non-empty.

    Closed categories: schema/provisioning | reset | baseline-replay | drift-hashing | generation/writer

SOURCE-AWARE STRIPPING (before matching)
========================================
Comments/docstrings are blanked before OCCURRENCE matching (Rust ``//`` + ``/* */``; Python ``#``
+ triple-quoted docstrings; SQL ``--`` + ``/* */``), so a mention inside a comment is ignored. An
occurrence inside an EXECUTABLE (possibly multiline) SQL string literal is STILL scanned. MARKERS,
by contrast, are detected from the RAW lines (they live in comments by design), so a marker is
seen even though the comment carrying it is stripped for occurrence detection.

DDL EXEMPTION
=============
Naming the base table in its OWN schema definition is not a resolver-bypassing read: an occurrence
immediately preceded by ``TABLE`` / ``INDEX`` / ``REFERENCES`` (CREATE/ALTER TABLE, FK REFERENCES),
or by ``ON`` and followed by ``(`` (CREATE INDEX ... ON), is exempt. A ``FROM`` / ``JOIN`` read, a
``COPY`` write, or a bare string-literal table reference is NOT exempt and needs a marker. This is
what keeps ``sql/001`` .. ``sql/008`` (pure DDL) green while still flagging every real read.

Pure static analysis — DB-free, safe and fast to run natively.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_GOLDEN_DIR = _REPO_ROOT / "tests" / "fixtures" / "reader_inventory_golden"
_PLANTED_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "planted_raw_reader.txt"

# The CLOSED sanctioned-category set. Any token outside this set is not a valid marker.
CATEGORIES = frozenset(
    {"schema/provisioning", "reset", "baseline-replay", "drift-hashing", "generation/writer"}
)
_MIN_REASON_CHARS = 3

# The base-table read token. `(?![_a-z])` keeps `arm_resolved_resources`, `resource_groups`,
# and `resources_pkey` out: none of them contain the literal `synthetic.resources`
# followed by a non-`[_a-z]` boundary.
_OCC_RE = re.compile(r"synthetic\.resources(?![_a-z])")
_MARKER_RE = re.compile(r"SYNRES-ALLOW\[([^\]\n]*)\]:([^\n]*)")

_DDL_PRECEDING = {"TABLE", "INDEX", "REFERENCES"}
_EXCLUDED_DIR_PARTS = {"target", "__pycache__", ".git", ".venv", "node_modules", "dist", "coverage"}
_LANG_BY_SUFFIX = {".rs": "rust", ".py": "python", ".sql": "sql"}


# --------------------------------------------------------------------------- #
# Source-aware stripping
# --------------------------------------------------------------------------- #
def _blank_cfg_test(text: str) -> str:
    """Blank every ``#[cfg(test)] mod … { … }`` block (Rust), preserving newlines/length so line
    numbers stay aligned. Brace-matching skips string and comment content."""
    out = list(text)
    n = len(text)
    search = 0
    while True:
        start = text.find("#[cfg(test)]", search)
        if start == -1:
            break
        brace = text.find("{", start)
        if brace == -1:
            break
        i = brace
        depth = 0
        end = n
        while i < n:
            if text.startswith("//", i):
                while i < n and text[i] != "\n":
                    i += 1
                continue
            if text.startswith("/*", i):
                j = text.find("*/", i + 2)
                i = n if j == -1 else j + 2
                continue
            c = text[i]
            if c in ('"', "'"):
                i += 1
                while i < n:
                    if text[i] == "\\":
                        i += 2
                        continue
                    if text[i] == c:
                        i += 1
                        break
                    i += 1
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
            i += 1
        for k in range(start, min(end, n)):
            if out[k] != "\n":
                out[k] = " "
        search = end
    return "".join(out)


def _blank_comments(text: str, lang: str) -> str:
    """Blank line/block comments and (Python) triple-quoted docstrings, KEEPING executable string
    literals in scope. Output length == input length (newlines preserved) so line indices align."""
    line_tok = {"rust": "//", "sql": "--", "python": "#"}[lang]
    has_block = lang in ("rust", "sql")
    has_triple = lang == "python"
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text.startswith(line_tok, i):
            while i < n and text[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if has_block and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j == -1 else j + 2
            for k in range(i, j):
                out.append("\n" if text[k] == "\n" else " ")
            i = j
            continue
        c = text[i]
        if has_triple and (text.startswith('"""', i) or text.startswith("'''", i)):
            q = text[i : i + 3]
            j = text.find(q, i + 3)
            j = n if j == -1 else j + 3
            for k in range(i, j):
                out.append("\n" if text[k] == "\n" else " ")
            i = j
            continue
        if c in ('"', "'"):
            # Executable string literal: KEEP its contents (they may be runtime SQL). Consume to
            # the closing quote so a `//`/`#`/`--` inside the string is not mistaken for a comment.
            out.append(c)
            i += 1
            while i < n:
                d = text[i]
                if d == "\\" and lang in ("rust", "python"):
                    out.append(d)
                    if i + 1 < n:
                        out.append(text[i + 1])
                        i += 2
                        continue
                    i += 1
                    continue
                if lang == "sql" and d == c and i + 1 < n and text[i + 1] == c:
                    out.append(d)
                    out.append(text[i + 1])
                    i += 2
                    continue
                out.append(d)
                i += 1
                if d == c:
                    break
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _strip(text: str, lang: str) -> str:
    if lang == "rust":
        text = _blank_cfg_test(text)
    return _blank_comments(text, lang)


# --------------------------------------------------------------------------- #
# Marker + DDL predicates
# --------------------------------------------------------------------------- #
def _line_has_valid_marker(raw_line: str) -> bool:
    for m in _MARKER_RE.finditer(raw_line):
        cat = m.group(1).strip()
        reason = m.group(2).strip()
        if cat in CATEGORIES and len(reason) >= _MIN_REASON_CHARS:
            return True
    return False


def _is_ddl_definition(line: str, start: int, end: int) -> bool:
    """True when the occurrence is a pure schema-definition reference (exempt), i.e. immediately
    preceded by TABLE/INDEX/REFERENCES, or by ON with a following ``(`` (CREATE INDEX ... ON)."""
    pre = line[:start].rstrip()
    j = len(pre)
    while j > 0 and (pre[j - 1].isalpha() or pre[j - 1] == "_"):
        j -= 1
    word = pre[j:].upper()
    if not word:
        return False
    if word in _DDL_PRECEDING:
        return True
    if word == "ON":
        return line[end:].lstrip().startswith("(")
    return False


# --------------------------------------------------------------------------- #
# The scanner — pure function over a text blob
# --------------------------------------------------------------------------- #
def _find_violations(text: str, lang: str) -> list[tuple[int, str, str]]:
    """Return ``(line_no, kind, snippet)`` for every unsanctioned occurrence and every floating
    marker. ``kind`` is ``"unmarked"`` or ``"floating"``. Single-use K=1 association: a marker on
    the occurrence's own line or the single line above sanctions EXACTLY ONE occurrence."""
    raw_lines = text.split("\n")
    stripped_lines = _strip(text, lang).split("\n")

    marker_lines = {i for i, rl in enumerate(raw_lines) if _line_has_valid_marker(rl)}

    occurrences: list[tuple[int, str]] = []
    for i, sline in enumerate(stripped_lines):
        for m in _OCC_RE.finditer(sline):
            if _is_ddl_definition(sline, m.start(), m.end()):
                continue
            occurrences.append((i, sline.strip()))

    occ_lines = {li for li, _ in occurrences}
    consumed: set[int] = set()
    violations: list[tuple[int, str, str]] = []

    for li, snippet in occurrences:
        if li in marker_lines and li not in consumed:
            consumed.add(li)
            continue
        if (li - 1) in marker_lines and (li - 1) not in consumed:
            consumed.add(li - 1)
            continue
        violations.append((li + 1, "unmarked", snippet))

    for ml in sorted(marker_lines):
        if ml in occ_lines or (ml + 1) in occ_lines:
            continue
        violations.append((ml + 1, "floating", raw_lines[ml].strip()))

    return violations


def _candidate_files() -> list[Path]:
    seen: set[Path] = set()
    files: list[Path] = []

    def _add(p: Path) -> None:
        if not p.is_file():
            return
        if set(p.parts) & _EXCLUDED_DIR_PARTS:
            return
        rp = p.resolve()
        if rp in seen:
            return
        seen.add(rp)
        files.append(p)

    for rel, pattern in (
        ("mock-server/src", "*.rs"),
        ("sql", "*.sql"),
        ("src/tenantless", "*.py"),
    ):
        base = _REPO_ROOT / rel
        if base.exists():
            for p in base.rglob(pattern):
                _add(p)
    common = _REPO_ROOT / "mock-server" / "tests" / "common"
    if common.exists():
        for p in common.glob("*.sql"):
            _add(p)
    return files


def _load_manifest() -> list[tuple[str, str, int]]:
    entries: list[tuple[str, str, int]] = []
    text = (_GOLDEN_DIR / "expected_violations.tsv").read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, lang, count = line.split("\t")
        entries.append((name, lang, int(count)))
    return entries


# =========================================================================== #
# positive controls
# =========================================================================== #
def test_catches_planted_raw_reader():
    """A planted, unmarked raw reader (the fixture) yields exactly one violation."""
    content = _PLANTED_FIXTURE.read_text(encoding="utf-8")
    violations = _find_violations(content, "sql")
    assert len(violations) == 1, violations
    assert violations[0][1] == "unmarked"


@pytest.mark.parametrize(
    "marker, expected_violations",
    [
        ("SYNRES-ALLOW[reset]: a valid non-empty reason", 0),        # valid → accepted
        ("SYNRES-ALLOW[bogus]: some reason", 1),                     # unknown category → rejected
        ("SYNRES-ALLOW[reset]:", 1),                                 # missing reason → rejected
        ("SYNRES-ALLOW[reset]", 1),                                  # bare (no colon) → rejected
    ],
)
def test_marker_validation_table_driven(marker, expected_violations):
    text = f'cur.execute("SELECT id FROM synthetic.resources")  # {marker}'
    assert len(_find_violations(text, "python")) == expected_violations


def test_marker_validation_out_of_window_k_gt_1_rejected():
    """A marker two lines above the occurrence (K>1) does not sanction it."""
    text = (
        "# SYNRES-ALLOW[reset]: too far above the occurrence\n"
        "x = 1\n"
        'cur.execute("SELECT id FROM synthetic.resources")\n'
    )
    violations = _find_violations(text, "python")
    assert any(k == "unmarked" and ln == 3 for ln, k, _ in violations), violations


def test_single_use_one_marker_two_refs():
    text = (_GOLDEN_DIR / "single_use_one_marker_two_refs.sql").read_text(encoding="utf-8")
    v = _find_violations(text, "sql")
    assert len(v) == 1 and v[0][1] == "unmarked", v


def test_single_use_floating_marker_flagged():
    text = (_GOLDEN_DIR / "floating_marker.sql").read_text(encoding="utf-8")
    v = _find_violations(text, "sql")
    assert len(v) == 1 and v[0][1] == "floating", v


def test_single_use_two_consecutive_one_marker():
    text = (_GOLDEN_DIR / "two_consecutive_one_marker.sql").read_text(encoding="utf-8")
    v = _find_violations(text, "sql")
    assert len(v) == 1 and v[0][1] == "unmarked", v


@pytest.mark.parametrize("name, lang", [
    ("comment_rust.rs", "rust"),
    ("comment_python.py", "python"),
    ("comment_sql.sql", "sql"),
])
def test_comment_immunity_per_language(name, lang):
    text = (_GOLDEN_DIR / name).read_text(encoding="utf-8")
    assert _find_violations(text, lang) == []


def test_comment_runtime_sql_string_literal_still_flagged():
    text = (_GOLDEN_DIR / "runtime_sql_string_literal.py").read_text(encoding="utf-8")
    v = _find_violations(text, "python")
    assert len(v) == 1 and v[0][1] == "unmarked", v


def test_ddl_definition_is_exempt_but_reads_are_flagged():
    text = (_GOLDEN_DIR / "ddl_exempt.sql").read_text(encoding="utf-8")
    v = _find_violations(text, "sql")
    assert len(v) == 1 and v[0][1] == "unmarked", v


def test_regex_safety_does_not_match_lookalikes():
    """The resolved view and sibling relations must not match."""
    for probe in (
        "SELECT * FROM synthetic.arm_resolved_resources",
        "SELECT * FROM synthetic.resource_groups",
        "ALTER TABLE x DROP CONSTRAINT resources_pkey",
    ):
        assert _find_violations(probe, "sql") == [], probe
    # A genuine read still matches.
    assert len(_find_violations("SELECT * FROM synthetic.resources", "sql")) == 1


# =========================================================================== #
# Python <-> Rust parity over the shared golden fixtures
# =========================================================================== #
def test_parity_golden_fixtures_match_shared_manifest():
    manifest = _load_manifest()
    assert manifest, "the shared expected-violations manifest is empty — parity would be vacuous"
    for name, lang, expected in manifest:
        path = _GOLDEN_DIR / name
        assert path.is_file(), f"missing shared golden fixture: {name}"
        got = _find_violations(path.read_text(encoding="utf-8"), lang)
        assert len(got) == expected, (name, "expected", expected, "got", got)


# =========================================================================== #
# Non-vacuity floors
# =========================================================================== #
def test_closed_category_set_is_present_and_non_empty():
    assert CATEGORIES, "the closed category set is empty — the gate would accept nothing / be vacuous"
    assert "schema/provisioning" in CATEGORIES and "reset" in CATEGORIES


def test_scanner_covers_a_meaningful_number_of_files():
    """Non-vacuity floor: a path-resolution slip would empty the candidate list and turn the gate
    green while scanning nothing. The enforced runtime-source scope is ~84 files (mock-server/src +
    sql + src/tenantless + the one common hash SQL); >50 proves the sweep is finding the tree
    without pulling in the excluded test/script trees."""
    files = _candidate_files()
    assert len(files) > 50, f"only {len(files)} files scanned -- the sweep is not finding the tree"


# =========================================================================== #
# The real thing: the whole runtime tree is green
# =========================================================================== #
def test_real_tree_is_green():
    offenders: list[str] = []
    for path in _candidate_files():
        lang = _LANG_BY_SUFFIX.get(path.suffix.lower())
        if lang is None:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line_no, kind, snippet in _find_violations(text, lang):
            rel = path.relative_to(_REPO_ROOT)
            offenders.append(f"{rel}:{line_no}: [{kind}] {snippet[:100]}")

    assert not offenders, (
        "Unsanctioned synthetic.resources references (each direct reader needs its OWN single-use "
        "SYNRES-ALLOW[<category>]: <reason> marker; a floating marker has no adjacent occurrence):\n"
        + "\n".join(offenders)
    )
