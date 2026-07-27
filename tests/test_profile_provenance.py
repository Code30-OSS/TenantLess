"""Tests for the release provenance gate (scripts/check_release_provenance.py).

These test the GATE, against synthetic trees built in tmp_path -- deliberately
not against the repo's own tree. The private repository intentionally holds a
real-derived `enterprise` profile, so a test that asserted directly on
`src/tenantless/profiles/` would have to fail here to be correct there, and a
gate that only runs where it passes is not a gate.

The public export runs the gate for real, against the export directory, as step
4b of the clean-export procedure.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
GATE = REPO / "scripts" / "check_release_provenance.py"

SYNTHETIC_PROVENANCE = {
    "reviewed": True,
    "synthetic": True,
    "derived_from_real_tenant": False,
    "derivation": {
        "bootstrap_profile": "profiles/oss-bootstrap.json",
        "bootstrap_profile_sha256": "0" * 64,
        "generator_seed": 1,
        "cost_as_of": "2026-07-01",
        "estate": {"subscriptions": 10, "resource_groups": 20, "resources": 300},
        "steps": ["..."],
    },
}


def _tree(tmp_path: Path, profiles: dict[str, dict], docs: dict[str, str] | None = None) -> Path:
    """Build a minimal tree with the bundled-profile layout the gate expects."""
    pdir = tmp_path / "src" / "tenantless" / "profiles"
    pdir.mkdir(parents=True)
    for name, body in profiles.items():
        (pdir / f"{name}.json").write_text(json.dumps(body), encoding="utf-8")
    for rel, text in (docs or {}).items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return tmp_path


def _run(tree: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GATE), "--tree", str(tree)],
        capture_output=True,
        text=True,
    )


def _profile(**over) -> dict:
    body = {
        "version": "1.2",
        "source_stats": {
            "total_subscriptions": 250,
            "total_resource_groups": 4301,
            "total_resources": 59280,
        },
        "provenance": dict(SYNTHETIC_PROVENANCE),
    }
    body.update(over)
    return body


def test_synthetic_profile_passes(tmp_path):
    result = _run(_tree(tmp_path, {"enterprise": _profile()}))
    assert result.returncode == 0, result.stderr
    assert "PASSED" in result.stdout


def test_real_derived_source_stats_is_rejected(tmp_path):
    """The exact estate shape the private profile was fitted from must never ship."""
    body = _profile(
        source_stats={
            "total_subscriptions": 399,
            "total_resource_groups": 6753,
            "total_resources": 96093,
        }
    )
    result = _run(_tree(tmp_path, {"enterprise": body}))
    assert result.returncode == 1
    assert "real-derived estate shape" in result.stderr


def test_unstamped_profile_is_rejected(tmp_path):
    """Absence of a provenance declaration is a failure, not a default-pass."""
    body = _profile()
    del body["provenance"]
    result = _run(_tree(tmp_path, {"enterprise": body}))
    assert result.returncode == 1
    assert "provenance.synthetic" in result.stderr


def test_profile_declaring_real_ancestry_is_rejected(tmp_path):
    body = _profile()
    body["provenance"] = dict(SYNTHETIC_PROVENANCE, synthetic=False, derived_from_real_tenant=True)
    result = _run(_tree(tmp_path, {"enterprise": body}))
    assert result.returncode == 1
    assert "derived_from_real_tenant" in result.stderr


def test_every_bundled_profile_is_checked_not_just_enterprise(tmp_path):
    """A second profile must not ride along uncertified."""
    body = _profile()
    del body["provenance"]
    result = _run(_tree(tmp_path, {"enterprise": _profile(), "small": body}))
    assert result.returncode == 1
    assert "small.json" in result.stderr


def test_empty_profile_dir_is_rejected(tmp_path):
    """A vacuous pass over zero profiles would certify nothing (non-vacuity floor)."""
    (tmp_path / "src" / "tenantless" / "profiles").mkdir(parents=True)
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "no profiles" in result.stderr


@pytest.mark.parametrize(
    ("number", "label"),
    [("96093", "source resource count"), ("192138", "benchmark resource count")],
)
def test_stale_derived_measurement_in_docs_is_rejected(tmp_path, number, label):
    """Withholding the profile but shipping its measurements is still a leak."""
    tree = _tree(
        tmp_path,
        {"enterprise": _profile()},
        docs={"docs/benchmarks/scale.md": f"- Dataset: {number} resources\n"},
    )
    result = _run(tree)
    assert result.returncode == 1, f"{label} should have been rejected"
    assert number in result.stderr


def test_measurement_check_is_word_bounded(tmp_path):
    """A longer number that merely CONTAINS a forbidden one must not trip the gate."""
    tree = _tree(
        tmp_path,
        {"enterprise": _profile()},
        docs={"docs/benchmarks/scale.md": "- Dataset: 960931 resources, run 3991\n"},
    )
    result = _run(tree)
    assert result.returncode == 0, result.stderr


def test_the_actual_replacement_profile_passes_the_gate(tmp_path):
    """Guards the real artifact when it is present (skips in the private tree).

    The private repo ships the real-derived profile by design, so this asserts on
    the built replacement only when the build directory exists.
    """
    built = REPO / ".planning" / "oss-release" / "build" / "enterprise.json"
    if not built.is_file():
        pytest.skip("replacement profile not built in this tree")
    body = json.loads(built.read_text(encoding="utf-8"))
    result = _run(_tree(tmp_path, {"enterprise": body}))
    assert result.returncode == 0, result.stderr
