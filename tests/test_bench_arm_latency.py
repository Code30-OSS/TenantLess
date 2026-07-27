"""Unit tests for the pure helpers of the ARM latency benchmark harness.

These cover the three side-effect-free building blocks of
``scripts/bench_arm_latency.py`` — ``percentiles``, ``build_endpoints``, and
``render_markdown`` — so the statistical, URL-construction, and report-rendering
logic is verifiable WITHOUT a database or a running server (fast, deterministic).

The I/O orchestration (generate / serve / keep-alive measurement) is exercised by
the harness end-to-end at scale and is intentionally NOT unit-tested here.

Scrub note: this file, the harness, and the emitted report carry zero forbidden
product tokens — the rendered Markdown is asserted clean below.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

import scrub_tokens

# The harness lives under ``scripts/`` (not an installed package), so load it by
# path. This keeps it runnable as a standalone script while still importable here.
# It is registered in ``sys.modules`` before execution so module-level
# ``@dataclass`` resolution (which looks the module up by name) works.
_HARNESS_PATH = Path(__file__).resolve().parents[1] / "scripts" / "bench_arm_latency.py"
_spec = importlib.util.spec_from_file_location("bench_arm_latency", _HARNESS_PATH)
assert _spec is not None and _spec.loader is not None
bench = importlib.util.module_from_spec(_spec)
sys.modules["bench_arm_latency"] = bench
_spec.loader.exec_module(bench)


# --- percentiles ------------------------------------------------------------


def test_percentiles_basic_distribution():
    """p50/p95/p99/min/max/mean over a known 1..100 sample set."""
    samples = [float(x) for x in range(1, 101)]  # 1.0 .. 100.0
    stats = bench.percentiles(samples)
    assert stats["min"] == 1.0
    assert stats["max"] == 100.0
    assert stats["mean"] == pytest.approx(50.5)
    # Inclusive-quantile cut points over 100 points: documented, deterministic.
    assert stats["p50"] == pytest.approx(50.5, abs=1.0)
    assert stats["p95"] == pytest.approx(95.0, abs=1.0)
    assert stats["p99"] == pytest.approx(99.0, abs=1.0)


def test_percentiles_single_sample():
    """A single sample: every percentile collapses to that one value."""
    stats = bench.percentiles([42.0])
    for key in ("p50", "p95", "p99", "min", "max", "mean"):
        assert stats[key] == 42.0, key


def test_percentiles_empty_returns_none():
    """Empty input yields all-None (documented choice — never raises)."""
    stats = bench.percentiles([])
    for key in ("p50", "p95", "p99", "min", "max", "mean"):
        assert stats[key] is None, key


def test_percentiles_keys_are_stable():
    """The result dict exposes exactly the documented keys."""
    stats = bench.percentiles([1.0, 2.0, 3.0])
    assert set(stats) == {"p50", "p95", "p99", "min", "max", "mean"}


# --- build_endpoints --------------------------------------------------------


def test_build_endpoints_uses_azure_correct_casing():
    """resourceGroups must use Azure camelCase (capital G)."""
    endpoints = bench.build_endpoints(
        "sub-123", "Microsoft.Storage/storageAccounts", "2022-12-01"
    )
    paths = [e.path for e in endpoints]
    rg = [p for p in paths if "/resourcegroups" in p.lower()]
    assert rg, "a resourceGroups endpoint must be present"
    for p in rg:
        assert "/resourceGroups" in p, f"must be camelCase resourceGroups: {p}"


def test_build_endpoints_covers_the_documented_set():
    """The four labelled probes: subscriptions, resourceGroups, resources, $filter."""
    endpoints = bench.build_endpoints("sub-9", "Microsoft.Compute/virtualMachines", "2022-12-01")
    labels = {e.label for e in endpoints}
    assert "list_subscriptions" in labels
    assert "list_resource_groups" in labels
    assert "list_resources" in labels
    assert "filter_resource_type" in labels


def test_build_endpoints_filter_uses_resourcetype_field_urlencoded():
    """The $filter probe uses the `resourceType eq '<type>'` field, URL-encoded."""
    present = "Microsoft.Sql/servers"
    endpoints = bench.build_endpoints("sub-7", present, "2022-12-01")
    flt = next(e for e in endpoints if e.label == "filter_resource_type")
    # The OData field is `resourceType` (maps to the `type` column server-side).
    assert "resourceType" in flt.path
    # The literal is URL-encoded: a space becomes %20, a single quote %27.
    assert " " not in flt.path, "filter must be URL-encoded (no raw spaces)"
    assert "%27" in flt.path or "%20" in flt.path, "filter value must be percent-encoded"
    # The present type appears in encoded form (slash safe; spaces/quotes encoded).
    assert present.replace("/", "%2F") in flt.path or present in flt.path


def test_build_endpoints_embeds_subscription_and_api_version():
    """Every endpoint carries the subscription id; list probes carry the api-version."""
    endpoints = bench.build_endpoints("sub-abc", "Microsoft.Network/virtualNetworks", "2021-04-01")
    for e in endpoints:
        if e.label != "list_subscriptions":
            assert "sub-abc" in e.path, f"{e.label} must scope to the subscription"
    assert any("2021-04-01" in e.path for e in endpoints), "api-version must appear"


# --- render_markdown --------------------------------------------------------


def _sample_report() -> dict:
    return {
        "dataset": {
            "subscriptions": 410,
            "resource_groups": 1230,
            "resources": 200000,
            "violations": 50,
            "dependencies": 99,
            "seed": 7,
            "profile": "enterprise",
        },
        "generation": {"wall_s": 120.0, "resources_per_sec": 1666.7},
        "measurement": {"samples": 200, "warmup": 10, "api_version": "2022-12-01"},
        "present_type": "Microsoft.Storage/storageAccounts",
        "busiest_subscription": "sub-xyz",
        "timestamp": "2026-06-22T00:00:00Z",
        "endpoints": [
            {
                "label": "list_resources",
                "path": "/subscriptions/sub-xyz/resources",
                "status": 200,
                "body_bytes": 4096,
                "percentiles": {
                    "p50": 12.3,
                    "p95": 25.6,
                    "p99": 40.1,
                    "min": 8.0,
                    "max": 55.0,
                    "mean": 14.2,
                },
            },
        ],
    }


def test_render_markdown_is_a_stable_table_with_dataset_context():
    md = bench.render_markdown(_sample_report())
    # Dataset counts present.
    assert "410" in md
    assert "200000" in md or "200,000" in md
    # Per-endpoint percentile columns present.
    assert "p50" in md
    assert "p95" in md
    assert "list_resources" in md
    # Markdown table structure (a header separator row of dashes).
    assert "|" in md
    assert re.search(r"\|\s*-{2,}", md), "expected a Markdown table separator row"


def test_render_markdown_carries_zero_forbidden_tokens():
    md = bench.render_markdown(_sample_report())
    forbidden = scrub_tokens.forbidden_pattern()
    assert forbidden is not None, "no scrub tokens configured -- refusing a vacuous pass"
    assert not forbidden.search(md), "rendered report must carry no forbidden tokens"


def test_render_markdown_is_deterministic():
    """Same report in → byte-identical Markdown out (stable ordering)."""
    report = _sample_report()
    assert bench.render_markdown(report) == bench.render_markdown(report)


# --- render_html ------------------------------------------------------------

# The committed canonical report is the realistic fixture input for the HTML
# dashboard renderer. Resolved by GLOB rather than by a hardcoded filename: the
# report is named after the scale it ran at (`scale-<subs>sub-<resources>.json`),
# so pinning the name coupled this suite to one particular benchmark run and
# broke it the moment the canonical report was regenerated at a different scale.
_BENCH_DIR = Path(__file__).resolve().parents[1] / "docs" / "benchmarks"


def _fixture_path() -> Path:
    reports = sorted(_BENCH_DIR.glob("scale-*.json"))
    assert reports, f"no committed benchmark report in {_BENCH_DIR}"
    return reports[0]


def _fixture_report() -> dict:
    return json.loads(_fixture_path().read_text(encoding="utf-8"))


def _forbidden_token_re() -> re.Pattern[str]:
    """The scrub-gate forbidden-token matcher, loaded from data.

    This used to assemble the tokens from split string fragments so the file
    would not trip the whole-tree scrub gate. That defeated the public/private
    split -- deleting the ``+`` signs reconstructed the private word list. The
    tokens now come from ``tests/scrub-tokens.json`` plus the gitignored private
    supplement, so no source file has to obfuscate anything.
    """
    pattern = scrub_tokens.forbidden_pattern()
    assert pattern is not None, "no scrub tokens configured -- refusing a vacuous pass"
    return pattern


def test_render_html_is_a_self_contained_document():
    """Doctype + inline <style> + at least one inline <svg> chart are present."""
    html = bench.render_html(_fixture_report())
    assert "<!doctype html>" in html.lower()
    assert "<style" in html.lower()
    assert "<svg" in html.lower(), "an inline SVG bar chart must be server-rendered"


def test_render_html_shows_every_endpoint_label_and_p95():
    """Every endpoint label and its p95 value appear in the rendered dashboard."""
    report = _fixture_report()
    html = bench.render_html(report)
    for ep in report["endpoints"]:
        assert ep["label"] in html, f"missing endpoint label: {ep['label']}"
        p95 = ep["percentiles"]["p95"]
        assert f"{p95:.2f}" in html, f"missing p95 for {ep['label']}"


def test_render_html_shows_dataset_and_generation_stats():
    """Dataset counts (subscriptions, resources) and generation rate are shown."""
    report = _fixture_report()
    html = bench.render_html(report)
    ds = report["dataset"]
    # Big numbers render with thousands separators.
    assert f"{ds['subscriptions']:,}" in html
    assert f"{ds['resources']:,}" in html
    # Generation throughput (resources/sec, rounded) appears.
    rps = round(report["generation"]["resources_per_sec"])
    assert f"{rps:,}" in html


def test_render_html_embeds_the_report_json():
    """The raw report is embedded as an application/json <script> block."""
    report = _fixture_report()
    html = bench.render_html(report)
    assert 'type="application/json"' in html
    assert 'id="report-data"' in html
    # The busiest-subscription id is part of the embedded payload.
    assert report["busiest_subscription"] in html


def test_render_html_is_fully_offline_no_external_url():
    """No external URL anywhere — must open from file:// with no network."""
    html = bench.render_html(_fixture_report())
    lowered = html.lower()
    assert "http://" not in lowered
    assert "https://" not in lowered
    assert "//cdn" not in lowered
    assert "@import" not in lowered, "no external font/style @import"


def test_render_html_carries_zero_forbidden_tokens():
    """The dashboard must carry no forbidden product tokens (scrub gate)."""
    html = bench.render_html(_fixture_report())
    forbidden = _forbidden_token_re()
    match = forbidden.search(html)
    assert match is None, f"forbidden token in HTML: {match.group(0) if match else ''!r}"


def test_render_html_is_deterministic():
    """Same report in → byte-identical HTML out."""
    report = _fixture_report()
    assert bench.render_html(report) == bench.render_html(report)
