"""Final OSS scrub gate (PLAT-02 / D-09 + D-09a).

Asserts that ZERO forbidden tokens survive anywhere in the shipped tree: a
case-insensitive scan of every shipped source/config/doc file must find none of
them.

TOKEN SOURCES (D-4)
===================
The gate reads its word list from two places:

1. ``tests/scrub-tokens.json`` -- COMMITTED and public. Generic
   "this must never ship" markers, tied to no organization or product. Always
   loaded, so public CI always runs a real gate.

2. ``tests/.scrub-tokens.private.json`` -- OPTIONAL and gitignored. Where a
   maintainer or fork puts their own internal product names, code names and
   customer names.

Splitting them this way ships the MECHANISM without shipping the word list.
Publishing a list of internal names in order to forbid those names would
disclose exactly what the gate exists to protect -- a reader of the public repo
would learn precisely what the project was carved out of. The private file keeps
working locally and in the maintainers' release verification; a fork gets a
functioning gate on day one and adds its own tokens.

Tokens are matched as WHOLE identifier tokens, not substrings, so ordinary
English words that merely contain a token do not trip the gate. A token matches
when it is not flanked by ASCII letters -- so ``foo_sim``, ``foo-sim``,
``Foo/Bar`` and ``FOO_ARM_ENDPOINT`` all match, while an English word that
happens to contain the letters does not.

Scanned paths: ``src mock-server tests scripts sql docs frontend/src`` (the last
is the shipped web-console UI source — .ts/.tsx/.css included) plus the manifests
/ config (``*.toml *.yml *.yaml *.json`` at root + nested, so
``profiles/schema.json``, ``pyproject.toml``, ``mock-server/Cargo.toml`` and
``docker-compose.yml`` are covered) plus the ``Dockerfile``.

Documented exclusions:
- ``.planning/`` — internal GSD planning artifacts, never shipped.
- ``Cargo.lock`` / ``uv.lock`` — generated dependency lockfiles; a forbidden
  token there would be a transitive dependency name, outside our control.
- ``profiles/.scan-denylist.json`` (and any stale pre-rename ``.*-denylist.json``
  dotfile) — gitignored dev-only privacy backstop (real-identifier denylist),
  not a shipped artifact. Excluded by pattern so a stale local copy is covered.
- ``__pycache__`` / ``*.pyc`` and ``target/`` — compiled bytecode / build output
  (text scan only; stale bytecode can carry tokens from deleted modules).
- ``node_modules/`` / ``frontend/dist`` / ``coverage`` — installed deps and
  generated frontend build/report output, not shipped source.
- the two scrub-token DATA files — they exist to hold the words.

This file is NOT exempt. It used to be, because it spelled the tokens in source;
it no longer spells any, so the gate scans itself. A gate that cannot see its own
source is where the last bypass lived.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import scrub_tokens

# Repo root: tests/test_scrub_gate.py -> parents[1].
_REPO_ROOT = Path(__file__).resolve().parents[1]

_PUBLIC_TOKENS_PATH = scrub_tokens.PUBLIC_TOKENS_PATH
_PRIVATE_TOKENS_PATH = scrub_tokens.PRIVATE_TOKENS_PATH

_PUBLIC_TOKENS = scrub_tokens.public_tokens()
_PRIVATE_TOKENS = scrub_tokens.private_tokens()
_FORBIDDEN_TOKENS = scrub_tokens.all_tokens()

# Whole-identifier match: a token is forbidden only when it is NOT embedded
# inside a larger run of ASCII letters. This admits identifier forms
# (``foo_sim``, ``foo-sim``, ``Foo/Bar``, ``FOO_ARM_ENDPOINT``) while rejecting
# English words that merely contain the letters.
_FORBIDDEN = (
    re.compile(
        r"(?<![a-z])(" + "|".join(re.escape(t) for t in _FORBIDDEN_TOKENS) + r")(?![a-z])",
        re.IGNORECASE,
    )
    if _FORBIDDEN_TOKENS
    else None
)

# Directory roots to scan in full. ``frontend/src`` is the shipped web-console UI
# source (the Vite build output ``frontend/dist`` and ``node_modules`` are excluded
# below / by the build-output guard); its .ts/.tsx/.css files carry the same
# brand-boundary decision (D-05) as the rest of the tree and MUST be scanned.
_SCAN_ROOTS = ("src", "mock-server", "tests", "scripts", "sql", "docs", "frontend/src")

# Extra manifest/config globs scanned across the whole tree.
_CONFIG_SUFFIXES = {".toml", ".yml", ".yaml", ".json"}
_EXTRA_NAMES = {"Dockerfile"}

# Text suffixes scanned inside the scan roots (text files only). Includes the
# frontend TypeScript/CSS suffixes so the shipped web-console source is covered.
_TEXT_SUFFIXES = {
    ".py",
    ".rs",
    ".json",
    ".html",
    ".md",
    ".sql",
    ".toml",
    ".yml",
    ".yaml",
    ".txt",
    ".sh",
    ".cfg",
    ".ini",
    ".ts",
    ".tsx",
    ".css",
    ".jsx",
}

# Path fragments / filenames that are never scanned (documented above). ``dist`` /
# ``coverage`` are frontend build/report output (generated, not shipped source).
_EXCLUDED_DIR_PARTS = {
    ".planning",
    "__pycache__",
    "target",
    ".git",
    ".venv",
    "node_modules",
    "dist",
    "coverage",
}
_EXCLUDED_NAMES = {"Cargo.lock", "uv.lock"}


def _is_denylist_backstop(name: str) -> bool:
    """True for the gitignored privacy denylist dotfile (any historical name).

    The denylist (``.scan-denylist.json``, or a stale pre-rename
    ``.*-denylist.json`` left on disk) is the dev-only privacy backstop: it
    DELIBERATELY contains the real identifiers the gate forbids elsewhere, and is
    gitignored — never a shipped artifact. Matched by pattern (not an exact name)
    so a stale local copy under the old name is excluded too.
    """
    return name.startswith(".") and name.endswith("-denylist.json")


def _is_excluded(path: Path) -> bool:
    # Only the two token DATA files are exempt -- they exist to hold the words.
    #
    # This gate file used to exempt ITSELF, because it spelled the forbidden
    # tokens in source. It no longer spells any: the words come from the data
    # files, and the examples in the docstrings describe the bypass in prose
    # rather than demonstrating it. So the gate now scans its own source, which
    # is the point -- a gate that cannot see itself is where the last bypass
    # lived.
    if path.resolve() in {_PUBLIC_TOKENS_PATH, _PRIVATE_TOKENS_PATH}:
        return True
    if path.name in _EXCLUDED_NAMES or _is_denylist_backstop(path.name):
        return True
    parts = set(path.parts)
    if parts & _EXCLUDED_DIR_PARTS:
        return True
    return False


def _candidate_files() -> list[Path]:
    seen: set[Path] = set()
    files: list[Path] = []

    def _add(p: Path) -> None:
        rp = p.resolve()
        if rp in seen:
            return
        seen.add(rp)
        files.append(p)

    # 1. Everything textual under the scan roots.
    for root in _SCAN_ROOTS:
        base = _REPO_ROOT / root
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if not p.is_file() or _is_excluded(p):
                continue
            if p.suffix.lower() in _TEXT_SUFFIXES or p.name in _EXTRA_NAMES:
                _add(p)

    # 2. Manifests/config across the whole tree (root + nested).
    for p in _REPO_ROOT.rglob("*"):
        if not p.is_file() or _is_excluded(p):
            continue
        if p.suffix.lower() in _CONFIG_SUFFIXES or p.name in _EXTRA_NAMES:
            _add(p)

    return files


# --------------------------------------------------------------------------- #
# Split-literal bypass gate
#
# The Stage 3 human review rejected an export in which seven public test files
# assembled the private tokens inline -- each one split across two adjacent
# string literals joined with `+` -- precisely so the whole-tree scan above
# would not see them. Deleting the `+` signs reconstructed the private word
# list from a public file, which is the exact disclosure the public/private
# token split exists to prevent.
#
# Two gates close it, because either alone has a hole:
#
#   1. RECONSTRUCT-AND-CHECK is semantic. It joins same-line literal
#      concatenations and runs the token matcher over the RESULT, so the bypass
#      fails no matter how the token is cut up. Its hole: a public runner has no
#      private token list, so it cannot catch a private name split in a fork.
#
#   2. The STRUCTURAL BAN needs no token list at all. Concatenating two short
#      alphabetic literals on one line has no legitimate use here (measured:
#      zero occurrences in the tree outside the bypass itself), and it is the
#      signature of exactly this trick.
# --------------------------------------------------------------------------- #

# A quoted literal, single or double, not spanning a line.
_LITERAL = r"""(?:"[^"\n\\]*"|'[^'\n\\]*')"""
# A chain of two or more literals joined by `+`.
_CONCAT_CHAIN = re.compile(rf"{_LITERAL}(?:\s*\+\s*{_LITERAL})+")
_LITERAL_ONLY = re.compile(_LITERAL)
# Short, purely alphabetic fragment -- the shape a split token has.
_SHORT_ALPHA = re.compile(r"^[A-Za-z]{1,6}$")


def _concat_chains(text: str):
    """Yield (line_no, raw_chain, reconstructed_value, [fragments])."""
    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in _CONCAT_CHAIN.finditer(line):
            raw = match.group(0)
            frags = [lit[1:-1] for lit in _LITERAL_ONLY.findall(raw)]
            yield lineno, raw, "".join(frags), frags


def test_no_forbidden_token_reconstructable_from_split_literals():
    """Joining same-line literal concatenation must not spell a forbidden token."""
    if _FORBIDDEN is None:
        pytest.fail("no scrub tokens configured -- refusing to report a vacuous pass")

    offenders: list[str] = []
    for path in _candidate_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, raw, joined, _frags in _concat_chains(text):
            match = _FORBIDDEN.search(joined)
            if match:
                rel = path.relative_to(_REPO_ROOT)
                offenders.append(
                    f"{rel}:{lineno}: {raw[:60]} reconstructs to a forbidden token"
                )

    assert not offenders, (
        "Forbidden tokens reconstructable from split string literals. Load tokens "
        "from tests/scrub-tokens.json (see tests/scrub_tokens.py) instead of "
        "spelling them in source:\n" + "\n".join(offenders)
    )


def test_no_short_alphabetic_literal_splitting():
    """Ban the SHAPE of the bypass, not just its known instances.

    Public CI has no private token list, so check 1 above cannot see a private
    name that a fork splits. This one needs no list: joining two short
    alphabetic literals on one line is the signature of token obfuscation, and
    there is no legitimate instance of it in this tree.

    If you hit this for a real reason, use a single literal, or an explicitly
    named constant, or move the value into a data file.
    """
    offenders: list[str] = []
    for path in _candidate_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, raw, joined, frags in _concat_chains(text):
            short_alpha = [f for f in frags if _SHORT_ALPHA.match(f)]
            # Two or more short all-alpha fragments joined into a word-like run.
            if len(short_alpha) >= 2 and joined.isalpha():
                rel = path.relative_to(_REPO_ROOT)
                offenders.append(f"{rel}:{lineno}: {raw[:60]}")

    assert not offenders, (
        "Short alphabetic string literals joined on one line -- this is how a "
        "forbidden token gets smuggled past the scan above. Use a single literal "
        "or a data file:\n" + "\n".join(offenders)
    )


def test_split_literal_gate_detects_a_planted_bypass(tmp_path):
    """Positive control: both gates above must actually fire.

    They assert an empty offender list, so a broken chain-finder would report a
    clean tree and be indistinguishable from success.
    """
    token = _PUBLIC_TOKENS[0]
    # Split the token the way the rejected export did.
    cut = max(1, len(token) // 2)
    planted = f'X = "{token[:cut]}" + "{token[cut:]}"\n'

    chains = list(_concat_chains(planted))
    assert chains, "chain-finder did not see a planted concatenation"
    _, _, joined, frags = chains[0]
    assert joined == token, f"reconstruction gave {joined!r}, expected {token!r}"
    assert _FORBIDDEN is not None and _FORBIDDEN.search(joined)

    # And the structural gate fires on a token-free split too. Built by format()
    # rather than written out, so this file does not itself contain the adjacent
    # literal concatenation it forbids -- the gate now scans itself (see below).
    sample = "Y = {} + {}\n".format('"foo"', '"bar"')
    generic = list(_concat_chains(sample))
    assert generic and generic[0][2] == "foobar"
    assert len([f for f in generic[0][3] if _SHORT_ALPHA.match(f)]) >= 2


def test_public_token_set_is_present_and_non_empty():
    """The committed set must exist and carry tokens.

    Non-vacuity floor: if the public file went missing or emptied, the gate below
    would scan the whole tree against zero patterns and pass unconditionally --
    a green result proving nothing. Public CI depends on this file alone, since
    the private supplement is absent there by construction.
    """
    assert _PUBLIC_TOKENS_PATH.is_file(), f"missing committed token set: {_PUBLIC_TOKENS_PATH}"
    assert _PUBLIC_TOKENS, "tests/scrub-tokens.json declares no tokens -- the gate would be vacuous"


def test_gate_actually_matches_a_forbidden_token():
    """The matcher detects a token, and does not fire on a word merely containing it.

    Proves the regex is live rather than trivially non-matching -- the failure
    mode a zero-match gate cannot otherwise distinguish from success.
    """
    assert _FORBIDDEN is not None
    token = _PUBLIC_TOKENS[0]
    assert _FORBIDDEN.search(f"a line with {token} in it"), f"gate missed {token!r}"
    assert not _FORBIDDEN.search(f"embedded{token.replace('-', '')}word")


def test_scanner_covers_a_meaningful_number_of_files():
    """Second non-vacuity floor: the file sweep must actually find the tree.

    A path-resolution slip (wrong root, over-broad exclusion) would empty the
    candidate list and turn the gate green while scanning nothing.
    """
    files = _candidate_files()
    assert len(files) > 100, f"only {len(files)} files scanned -- the sweep is not finding the tree"


def test_no_forbidden_tokens_in_shipped_tree():
    """Zero forbidden tokens across every shipped path.

    Runs against the committed set, plus the private supplement when present.
    """
    if _FORBIDDEN is None:
        pytest.fail("no scrub tokens configured -- refusing to report a vacuous pass")

    offenders: list[str] = []
    for path in _candidate_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            # Binary or unreadable: not a shipped text artifact -> skip.
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            match = _FORBIDDEN.search(line)
            if match:
                rel = path.relative_to(_REPO_ROOT)
                offenders.append(f"{rel}:{lineno}: {match.group(0)!r} :: {line.strip()[:100]}")

    # The message reports COUNTS, not the private word list: a CI log from a fork
    # or a public runner must not become a way to read the supplement.
    sources = f"{len(_PUBLIC_TOKENS)} public"
    if _PRIVATE_TOKENS:
        sources += f" + {len(_PRIVATE_TOKENS)} private"
    assert not offenders, (
        f"Forbidden tokens found in shipped paths ({sources}, whole-token, "
        f"case-insensitive):\n" + "\n".join(offenders)
    )
