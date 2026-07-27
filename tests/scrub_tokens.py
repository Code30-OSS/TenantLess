"""Shared loader for the scrub-token word list (D-4).

NOT a test module -- a helper imported by every test that needs to assert some
artifact carries no forbidden token.

WHY THIS EXISTS
===============
Several tests used to assemble the forbidden tokens inline, splitting each one
across two adjacent string literals joined with ``+``, so that the file
checking for a token would not itself trip the whole-tree scrub gate. That
trick defeats the entire point of the public/private token split: a reader of
the public repository could reconstruct the private word list by deleting the
``+`` signs. The Stage 3 human review rejected the export for exactly this.

The tokens now live in data, not in source:

* ``tests/scrub-tokens.json``          -- committed, public, generic sentinels
* ``tests/.scrub-tokens.private.json`` -- gitignored, the real internal names

Both are excluded from the scan, so no source file needs to obfuscate anything.
A public checkout gets a real, non-vacuous gate over the generic set; a
maintainer's checkout additionally covers the private list.

``tests/test_scrub_gate.py`` adds a regression gate that reconstructs
same-line literal concatenation and re-checks it, so this bypass cannot return
by any spelling.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PUBLIC_TOKENS_PATH = TESTS_DIR / "scrub-tokens.json"
PRIVATE_TOKENS_PATH = TESTS_DIR / ".scrub-tokens.private.json"


def read_tokens(path: Path) -> list[str]:
    """Read a token list from a scrub-token file; ``[]`` when the file is absent."""
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    tokens = data.get("tokens", [])
    if not isinstance(tokens, list) or not all(isinstance(t, str) for t in tokens):
        raise ValueError(f"{path}: 'tokens' must be a list of strings")
    return [t.strip().lower() for t in tokens if t.strip()]


def public_tokens() -> list[str]:
    return read_tokens(PUBLIC_TOKENS_PATH)


def private_tokens() -> list[str]:
    return read_tokens(PRIVATE_TOKENS_PATH)


def all_tokens() -> tuple[str, ...]:
    """Public tokens, plus the private supplement when this checkout has one."""
    return tuple(dict.fromkeys(public_tokens() + private_tokens()))


def forbidden_pattern(tokens: "tuple[str, ...] | None" = None) -> "re.Pattern[str] | None":
    """Whole-identifier, case-insensitive matcher over ``tokens``.

    A token matches only when it is NOT embedded inside a longer run of ASCII
    letters, so ordinary English that merely contains the letters does not trip
    the gate. Returns ``None`` for an empty token set -- callers must treat that
    as "cannot check", never as "clean".
    """
    toks = all_tokens() if tokens is None else tokens
    if not toks:
        return None
    return re.compile(
        r"(?<![a-z])(" + "|".join(re.escape(t) for t in toks) + r")(?![a-z])",
        re.IGNORECASE,
    )
