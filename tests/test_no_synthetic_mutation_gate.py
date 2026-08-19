"""Structural gate — NO drift code path mutates ``synthetic.resources`` in place.

The overlay migration moved BOTH halves of configuration drift (apply and revert) off
in-place ``synthetic.resources`` mutation and onto the ``synthetic.arm_overlay``
copy-on-write plane. This is a SOURCE-LEVEL, machine-checkable gate proving the migration
is complete: it AST-extracts the ``apply_drift`` and ``revert_drift`` bodies from
``src/tenantless/cli.py``, strips comments AND the function docstring (so header prose can
never self-invalidate the gate), and asserts ZERO in-place
``UPDATE / INSERT INTO / DELETE FROM synthetic.resources`` statements remain. Baseline
READs (``SELECT ... FROM synthetic.resources``) are allowed — those are the immutable
source the overlay resolves against.

A regression that reintroduces an in-place mutation FAILS this gate with the offending
line. The gate's teeth are pinned by ``test_gate_flags_a_planted_in_place_mutation`` (a
planted mutation IS flagged; the same phrase inside a comment is NOT).

Pure static analysis — no DB, no server, safe to run natively (``python`` / ``ast``).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_CLI_PATH = (
    Path(__file__).resolve().parent.parent / "src" / "tenantless" / "cli.py"
)

# The three in-place mutations forbidden in a drift path (case-insensitive, whitespace
# flexible). ``\b`` after ``resources`` keeps ``synthetic.resource_groups`` out of scope —
# drift never touches RGs, and "resource_groups" has no "resources" substring anyway.
_FORBIDDEN = [
    (re.compile(r"UPDATE\s+synthetic\.resources\b", re.IGNORECASE), "UPDATE synthetic.resources"),
    (
        re.compile(r"INSERT\s+INTO\s+synthetic\.resources\b", re.IGNORECASE),
        "INSERT INTO synthetic.resources",
    ),
    (
        re.compile(r"DELETE\s+FROM\s+synthetic\.resources\b", re.IGNORECASE),
        "DELETE FROM synthetic.resources",
    ),
]


def _function_code(source: str, tree: ast.AST, name: str) -> str:
    """Return the executable source of the top-level function ``name`` with its leading
    docstring dropped (comments are stripped by the caller). Statements are re-joined from
    their own source segments, so the decorator lines + docstring are excluded by
    construction — only real code (incl. SQL string literals) remains to scan."""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            body = node.body
            start = 0
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                start = 1  # drop the docstring so its prose can't self-invalidate the gate
            segments = []
            for stmt in body[start:]:
                seg = ast.get_source_segment(source, stmt)
                if seg:
                    segments.append(seg)
            return "\n".join(segments)
    raise AssertionError(f"function {name!r} not found in {_CLI_PATH}")


def _strip_comment_lines(code: str) -> str:
    """Drop whole-line comments (the ``grep -v '^[[:space:]]*#'`` rule from the plan)."""
    return "\n".join(
        line for line in code.splitlines() if not line.lstrip().startswith("#")
    )


def _find_violations(code: str) -> list[tuple[int, str, str]]:
    """Return ``(lineno, label, line_text)`` for every forbidden in-place mutation found."""
    scannable = _strip_comment_lines(code)
    violations: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(scannable.splitlines(), start=1):
        for pattern, label in _FORBIDDEN:
            if pattern.search(line):
                violations.append((lineno, label, line.strip()))
    return violations


def _drift_function_code(name: str) -> str:
    source = _CLI_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    return _function_code(source, tree, name)


def test_apply_drift_has_no_in_place_synthetic_mutation():
    """``apply_drift`` issues ZERO in-place UPDATE/INSERT/DELETE against synthetic.resources
    (apply half; the migration lands drift on arm_overlay)."""
    violations = _find_violations(_drift_function_code("apply_drift"))
    assert not violations, (
        "apply_drift still mutates synthetic.resources in place:\n"
        + "\n".join(f"  line {n}: [{label}] {text}" for n, label, text in violations)
    )


def test_revert_drift_has_no_in_place_synthetic_mutation():
    """``revert_drift`` issues ZERO in-place UPDATE/INSERT/DELETE against synthetic.resources
    (revert half; recompute-from-ledger rebuilds the overlay, baseline is READ-only)."""
    violations = _find_violations(_drift_function_code("revert_drift"))
    assert not violations, (
        "revert_drift still mutates synthetic.resources in place:\n"
        + "\n".join(f"  line {n}: [{label}] {text}" for n, label, text in violations)
    )


def test_gate_flags_a_planted_in_place_mutation():
    """The gate has TEETH: a planted in-place mutation IS flagged, while the SAME phrase in a
    comment is NOT (proving both the detection and the comment-stripping work — the gate would
    fail if a regression reintroduced a real mutation, and cannot be defeated by a comment)."""
    planted = (
        'cur.execute(\n'
        '    "UPDATE synthetic.resources SET drift_deleted_at = now() WHERE id = %s",\n'
        '    (rid,),\n'
        ')'
    )
    flagged = _find_violations(planted)
    assert any(label == "UPDATE synthetic.resources" for _, label, _ in flagged), (
        "the gate must flag a planted in-place UPDATE synthetic.resources"
    )

    # A comment mentioning the phrase must NOT be flagged (comment-strip proof).
    commented = "# historically this issued UPDATE synthetic.resources — now overlay-only"
    assert _find_violations(commented) == [], (
        "a whole-line comment mentioning the phrase must not trip the gate"
    )

    # A baseline READ must NOT be flagged (reads are the allowed immutable source).
    read_only = 'cur.execute("SELECT id FROM synthetic.resources WHERE id = ANY(%s)", (ids,))'
    assert _find_violations(read_only) == [], (
        "a baseline SELECT read must not trip the gate"
    )
