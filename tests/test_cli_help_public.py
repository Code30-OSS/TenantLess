"""User-visible CLI help must not leak internal planning vocabulary.

The Click command docstrings and option help render into ``--help`` output that ships
to end users. They must read as timeless public documentation, free of the private
phase / decision / threat / requirement / quick-task identifiers used internally.

The forbidden patterns are deliberately narrow — each requires a digit after a known
prefix, or is a distinctive multi-word phrase — so ordinary vocabulary that legitimately
appears in help ("cost", "identity", "cost-as-of", the user-facing ``DRIFT_*`` code
family, the word "phase") is never rejected. See ``test_patterns_do_not_reject_ordinary``.
"""

from __future__ import annotations

import re

import pytest
from click.testing import CliRunner

from tenantless.cli import main

# Every user-visible help surface: the group and each subcommand.
HELP_TARGETS = [
    [],
    ["generate"],
    ["serve"],
    ["apply-drift"],
    ["revert-drift"],
    ["init-db"],
    ["analyze"],
]

# Internal-vocabulary patterns. Prefix IDs require a hyphen + digit (uppercase), so
# "cost" / "identity" / "cost-as-of" / "DRIFT_*" cannot match; phrase patterns are
# distinctive enough not to appear in ordinary prose.
FORBIDDEN = [
    r"\bD-\d",                                                    # D-01, D-14, ...
    r"\bT-\d",                                                    # T-11-13, ...
    r"\b(?:CTRL|WEBUI|IAM|DRIFT|COST|PLAT|ANLZ|VIOL|XSUB|SPEED|ARCH)-\d",
    r"\bPhase[ -]\d",                                             # Phase-2, Phase 2
    r"\bPitfall \d",                                              # Pitfall 5
    r"DOCUMENTED DECISIONS",
    r"/gsd",
    r"\.planning",
    r"\b\d{6}-[a-z]{3}\b",                                        # quick-task id, e.g. 260709-blf
]
FORBIDDEN_RE = re.compile("|".join(FORBIDDEN))


def _help_text(args: list[str]) -> str:
    result = CliRunner().invoke(main, args + ["--help"])
    assert result.exit_code == 0, f"`--help` failed for {args!r}:\n{result.output}"
    return result.output


@pytest.mark.parametrize("args", HELP_TARGETS, ids=lambda a: " ".join(a) or "<root>")
def test_help_has_no_internal_vocabulary(args: list[str]) -> None:
    text = _help_text(args)
    hits = sorted(set(FORBIDDEN_RE.findall(text)))
    label = "tenantless " + (" ".join(args) or "").strip()
    assert not hits, f"internal planning vocabulary in `{label} --help`: {hits}"


def test_patterns_do_not_reject_ordinary() -> None:
    """Guard the guard: legitimate help vocabulary must not trip the patterns."""
    for ok in [
        "cost", "identity", "cost-as-of", "the cost fact table grain",
        "over-privilege role assignments", "DRIFT_* code allowlist",
        "resource-group", "the analysis phase", "reproducible by default",
    ]:
        assert not FORBIDDEN_RE.search(ok), f"false positive on {ok!r}"
