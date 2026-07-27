"""ANLZ-10 unit scaffold: human-review dump of the derived profile.

Drives the (not-yet-existing) review module on a small in-memory profile dict.
ANLZ-10 requires the analyzer to emit a human-readable review of the derived
profile both to stdout (interactive) and ALWAYS to a ``<out>_review.txt`` file,
so a human can eyeball coverage / skips / distributions before the profile feeds
the generator.

Wave-0 status: ``tenantless.analyzer.review`` does not exist yet. ``importorskip``
makes this file COLLECT but SKIP cleanly; a later plan turns it green.
``uv run pytest tests/test_review.py`` resolves to these real tests.
"""

from __future__ import annotations

import pytest

# A tiny profile-like dict the review module formats for human eyeballing.
_PROFILE = {
    "source_stats": {
        "total_subscriptions": 2,
        "total_resource_groups": 3,
        "total_resources": 10,
    },
    "provenance": {"reviewed": False, "coverage": 0.97, "skipped_fields": ["apiVersion"]},
    "resource_type_distribution": {"microsoft.compute/virtualmachines": 0.6},
}


def test_review_writes_review_file(tmp_path):
    """The review module always writes a ``<out>_review.txt`` companion file.

    Skips until ``analyzer.review`` exists (Wave-0 scaffold).
    """
    review = pytest.importorskip(
        "tenantless.analyzer.review",
        reason="review dump (ANLZ-10) lands in a later Phase-6 plan.",
    )
    out = tmp_path / "profile.json"
    review.write_review(_PROFILE, out)  # later plan owns the exact signature
    assert (tmp_path / "profile_review.txt").exists()


def test_review_renders_to_stdout(capsys):
    """The review module also renders a human-readable dump to stdout.

    Skips until ``analyzer.review`` exists (Wave-0 scaffold).
    """
    review = pytest.importorskip(
        "tenantless.analyzer.review",
        reason="review dump (ANLZ-10) lands in a later Phase-6 plan.",
    )
    text = review.render(_PROFILE)
    assert isinstance(text, str)
    assert "total_resources" in text or "total resources" in text.lower()
