"""Regression tests for scripts/audit_rg_coherence.py.

The audit itself is DB-backed, but its gate-decision logic is a pure function
(``evaluate_gates``) and its thresholds are module constants, so the valuable
safety properties -- the anti-vacuity floors and the over-claim gates -- can be
exercised with no live PostgreSQL. A subprocess ``--help`` smoke test rounds it
out and doubles as the guard that no internal project-management identifier
leaked into the shipped script.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "audit_rg_coherence.py"


def _load_module():
    """Import the audit script as a module (no DB, no argparse side effects)."""
    spec = importlib.util.spec_from_file_location("audit_rg_coherence", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load_module()


# --- thresholds: the exact values that must not silently drift or loosen ---

def test_threshold_constants_have_expected_values():
    assert mod.MAX_SEMANTIC_CROSS_PCT == 1.0
    assert mod.MIN_SEMANTIC_RGS_AUDITED == 1
    assert mod.MIN_ANCHOR_REQUIRED_RGS_AUDITED == 1
    assert mod.GENERIC == {"shared", "core"}


# --- pure gate-decision logic: a synthetic PASS and several synthetic FAILs ---

def _metrics(**over):
    """A fully-passing metric set; override one field to build a FAIL case."""
    base = dict(
        tot_ne=50,
        role_token_rgs_audited=10,
        unconfirmed_semantic_rgs=0,
        empty_semantic_rgs=0,
        anchorless_under_anchor_required_tokens=0,
        semantic_cross_pct=0.0,
        undeclared_ubiquitous_only_anchors=0,
    )
    base.update(over)
    return base


def test_synthetic_pass_case_all_gates_hold():
    gates = mod.evaluate_gates(**_metrics())
    assert all(ok for _, _, _, ok in gates)
    assert len(gates) == 7


def test_vacuous_empty_tenant_fails_evidence_floor():
    # No semantic RGs audited -> the "== 0" gates would be vacuously true, but
    # the non-vacuity floor (gate 1) must FAIL rather than certify nothing.
    gates = mod.evaluate_gates(**_metrics(tot_ne=0, role_token_rgs_audited=0))
    assert not all(ok for _, _, _, ok in gates)


def test_vacuous_role_gate_fails_role_evidence_floor():
    # Semantic RGs exist, but not one carries a role token -> the role gate is
    # vacuously satisfied, so its evidence floor (gate 2) must FAIL.
    gates = mod.evaluate_gates(**_metrics(role_token_rgs_audited=0))
    assert not all(ok for _, _, _, ok in gates)


@pytest.mark.parametrize(
    "override",
    [
        {"unconfirmed_semantic_rgs": 1},
        {"empty_semantic_rgs": 1},
        {"anchorless_under_anchor_required_tokens": 1},
        {"semantic_cross_pct": 1.1},
        {"undeclared_ubiquitous_only_anchors": 1},
    ],
)
def test_any_over_claim_makes_the_verdict_fail(override):
    gates = mod.evaluate_gates(**_metrics(**override))
    assert not all(ok for _, _, _, ok in gates)


def test_cross_pct_at_threshold_passes_but_above_fails():
    at = mod.evaluate_gates(**_metrics(semantic_cross_pct=mod.MAX_SEMANTIC_CROSS_PCT))
    over = mod.evaluate_gates(**_metrics(semantic_cross_pct=mod.MAX_SEMANTIC_CROSS_PCT + 0.1))
    assert all(ok for _, _, _, ok in at)
    assert not all(ok for _, _, _, ok in over)


def test_parse_token_extracts_the_archetype_token():
    assert mod.parse_token("rg-eng-uat-network-hub-56") == "network-hub"
    assert mod.parse_token("rg-eng-uat-db-1") == "db"
    assert mod.parse_token("not-a-managed-name") == "?"


# --- subprocess smoke test: --help works and leaks no internal identifiers ---

def test_help_exits_zero_and_is_clean_of_internal_ids():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    help_text = proc.stdout + proc.stderr
    assert "--database-url" in help_text
    # No internal project-management identifiers may reach a public reader.
    lowered = help_text.lower()
    for needle in (
        "d-14", "d-17", "d-19", "arch-gap", "t-19", "phase 1", "phase 19",
        "plan 19", "remedy", "operator rejected", "19-06", "19-09", "cr-01",
        "/gsd",
    ):
        assert needle not in lowered, f"internal identifier {needle!r} leaked into --help"
