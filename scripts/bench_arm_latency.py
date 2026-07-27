"""ARM mock-server latency benchmark harness.

A standalone, scale-parameterized measurement tool for the ARM-compatible mock
server. For an arbitrary tenant scale it (1) optionally generates the synthetic
tenant, (2) launches the mock server via the project's discovery seam, (3)
measures ARM list / ``$filter`` endpoint latency with a CORRECT keep-alive
methodology (one persistent ``http.client.HTTPConnection`` reused across all
warmup + sample requests per endpoint), and (4) emits a structured JSON +
Markdown report.

This complements the fixed-scale pytest gate (``tests/test_scale_benchmark.py``,
a CI assertion): this harness is an ad-hoc, parameterized measurement tool with a
committed results report.

Why keep-alive: opening a fresh TCP connection per sample folds per-request
connect overhead into the measured latency, masking real server processing time.
A single reused connection measures server time accurately. (Spawning ``curl``
per request is worse still — on some platforms the process-spawn floor dominates
— so it is never used here.)

The pure helpers (``percentiles``, ``build_endpoints``, ``render_markdown``) are
free of network/process side effects so they unit-test fast without a DB or a
running server (``tests/test_bench_arm_latency.py``).
"""

from __future__ import annotations

import html as _html
import http.client
import json
import os
import shutil
import socket
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

import click

# Reuse the project's process-orchestration seam (binary discovery, the dev DB
# URL default) rather than reimplementing shell logic.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from tenantless import serve  # noqa: E402  (after sys.path bootstrap)

DEFAULT_DATABASE_URL = serve.DEFAULT_DATABASE_URL


# ---------------------------------------------------------------------------
# Pure helpers (no network / process side effects) — unit-tested in isolation.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Endpoint:
    """A labelled ARM endpoint probe (label + request path)."""

    label: str
    path: str


def percentiles(samples: list[float]) -> dict:
    """Summarize latency ``samples`` (ms) → p50/p95/p99/min/max/mean.

    Cut-point convention (deterministic, documented): percentiles use
    ``statistics.quantiles(samples, n=100, method="inclusive")`` — the
    inclusive method places the i-th of 99 cut points at the value that
    linearly interpolates the rank, matching the convention already used by
    ``tests/test_scale_benchmark.py`` (which reads the p95 boundary off an
    inclusive-quantile cut). Index ``k - 1`` is the k-th percentile boundary
    (so p50 → ``[49]``, p95 → ``[94]``, p99 → ``[98]``).

    Edge cases:
      * ``n == 0`` → every field is ``None`` (the caller treats a missing
        measurement as "not collected", never as a zero latency).
      * ``n == 1`` → ``quantiles`` requires at least two points, so every
        percentile collapses to the single observed value.
    """
    keys = ("p50", "p95", "p99", "min", "max", "mean")
    if not samples:
        return {k: None for k in keys}

    lo = min(samples)
    hi = max(samples)
    mean = statistics.fmean(samples)

    if len(samples) == 1:
        only = samples[0]
        return {"p50": only, "p95": only, "p99": only, "min": lo, "max": hi, "mean": mean}

    cuts = statistics.quantiles(samples, n=100, method="inclusive")
    return {
        "p50": cuts[49],
        "p95": cuts[94],
        "p99": cuts[98],
        "min": lo,
        "max": hi,
        "mean": mean,
    }


def build_endpoints(sub_id: str, present_type: str, api_version: str) -> list[Endpoint]:
    """Build the labelled ARM probe URL set for one subscription.

    Azure-correct casing is non-negotiable: the resource-group list path is
    ``/subscriptions/{sub}/resourceGroups`` (camelCase ``G``), matching the Rust
    route table. The ``$filter`` probe uses the OData ``resourceType`` field
    (which maps to the server's ``type`` column), URL-encoded.

    The four probes:
      * ``list_subscriptions``      — ``GET /subscriptions``
      * ``list_resource_groups``    — ``GET /subscriptions/{sub}/resourceGroups``
      * ``list_resources``          — ``GET /subscriptions/{sub}/resources`` (paginated)
      * ``filter_resource_type``    — ``GET …/resources?$filter=resourceType eq '<type>'``
    """
    av = quote(api_version, safe="")
    filter_expr = quote(f"resourceType eq '{present_type}'", safe="")
    return [
        Endpoint("list_subscriptions", f"/subscriptions?api-version={av}"),
        Endpoint(
            "list_resource_groups",
            f"/subscriptions/{sub_id}/resourceGroups?api-version={av}",
        ),
        Endpoint(
            "list_resources",
            f"/subscriptions/{sub_id}/resources?api-version={av}",
        ),
        Endpoint(
            "filter_resource_type",
            f"/subscriptions/{sub_id}/resources?api-version={av}&$filter={filter_expr}",
        ),
    ]


def _fmt_ms(value: float | None) -> str:
    """Render a millisecond figure for the report table (or ``-`` when absent)."""
    return "-" if value is None else f"{value:.2f}"


def _esc(value: object) -> str:
    """HTML-escape any value injected into the dashboard markup (incl. quotes)."""
    return _html.escape(str(value), quote=True)


def render_markdown(report: dict) -> str:
    """Render a deterministic, human-readable Markdown report from ``report``.

    The output is a stable table of per-endpoint p50/p95/p99 plus dataset and
    generation context. Ordering is taken verbatim from ``report["endpoints"]``
    (the harness builds that list in a fixed order), so the same report renders
    byte-identically every time. The prose is generic — no product names.
    """
    ds = report.get("dataset", {})
    gen = report.get("generation", {})
    meas = report.get("measurement", {})

    lines: list[str] = []
    lines.append("# ARM mock-server latency benchmark")
    lines.append("")
    lines.append(f"- Timestamp: {report.get('timestamp', '-')}")
    lines.append(f"- Profile: {ds.get('profile', '-')}")
    lines.append(f"- Seed: {ds.get('seed', '-')}")
    lines.append(
        f"- Dataset: {ds.get('subscriptions', '-')} subscriptions, "
        f"{ds.get('resource_groups', '-')} resource groups, "
        f"{ds.get('resources', '-')} resources "
        f"({ds.get('violations', '-')} violations, "
        f"{ds.get('dependencies', '-')} dependencies)"
    )
    lines.append(f"- Busiest subscription: {report.get('busiest_subscription', '-')}")
    lines.append(f"- Present resource type ($filter probe): {report.get('present_type', '-')}")
    lines.append(
        f"- Measurement: {meas.get('samples', '-')} keep-alive samples per endpoint "
        f"(+{meas.get('warmup', '-')} warmup), api-version {meas.get('api_version', '-')}"
    )
    wall = gen.get("wall_s")
    rps = gen.get("resources_per_sec")
    if wall is not None or rps is not None:
        wall_s = "-" if wall is None else f"{wall:.1f}"
        rps_s = "-" if rps is None else f"{rps:.1f}"
        lines.append(f"- Generation: {wall_s}s wall, {rps_s} resources/sec")
    else:
        lines.append("- Generation: skipped (reused existing data)")
    lines.append("")
    lines.append("## Endpoint latency (keep-alive, milliseconds)")
    lines.append("")
    lines.append("| Endpoint | Status | Body bytes | p50 | p95 | p99 | min | max | mean |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for ep in report.get("endpoints", []):
        pct = ep.get("percentiles", {})
        lines.append(
            "| {label} | {status} | {bytes} | {p50} | {p95} | {p99} | {mn} | {mx} | {mean} |".format(
                label=ep.get("label", "-"),
                status=ep.get("status", "-"),
                bytes=ep.get("body_bytes", "-"),
                p50=_fmt_ms(pct.get("p50")),
                p95=_fmt_ms(pct.get("p95")),
                p99=_fmt_ms(pct.get("p99")),
                mn=_fmt_ms(pct.get("min")),
                mx=_fmt_ms(pct.get("max")),
                mean=_fmt_ms(pct.get("mean")),
            )
        )
    lines.append("")
    return "\n".join(lines)


def _nice_ceiling(value: float) -> float:
    """Round ``value`` UP to a friendly axis maximum (1/2/2.5/5 × 10^k).

    Used for the shared millisecond x-domain of the latency chart so bar widths
    and axis ticks land on readable round numbers. A non-positive value yields a
    sensible floor of ``1.0`` so the chart never collapses to a zero domain.
    """
    if value <= 0:
        return 1.0
    import math

    exp = math.floor(math.log10(value))
    base = 10.0**exp
    for mult in (1.0, 2.0, 2.5, 5.0, 10.0):
        if value <= mult * base:
            return mult * base
    return 10.0 * base


def _svg_latency_chart(endpoints: list[dict]) -> str:
    """Server-render a grouped horizontal bar chart (p50/p95/p99) as inline ``<svg>``.

    All geometry is computed in Python from a SHARED 0→nice-max millisecond
    x-domain (no JS, no chart library, no external request). Each endpoint is one
    labelled row of three bars — p50 (--gold-light), p95 (--gold), p99
    (--gold-dark) — every bar value-labelled in Space Mono, over light gridlines
    with an axis tick row in ms. Returns an empty string when there are no
    measured percentiles to plot.
    """
    rows = [
        (ep.get("label", "-"), ep.get("percentiles", {}))
        for ep in endpoints
        if ep.get("percentiles", {}).get("p99") is not None
    ]
    if not rows:
        return ""

    max_p99 = max(pct["p99"] for _, pct in rows)
    domain = _nice_ceiling(max_p99)

    # Layout constants (px). A single fixed coordinate space keeps the SVG
    # deterministic and self-describing.
    label_w = 168
    chart_w = 560
    value_w = 64
    pad_l = 16
    pad_r = 16
    pad_t = 32  # top band reserved for the legend (clear of the plot)
    axis_h = 40  # bottom band: tick labels + the "milliseconds" axis title
    row_h = 64
    bar_h = 14
    bar_gap = 4
    plot_x = pad_l + label_w
    plot_w = chart_w
    width = pad_l + label_w + chart_w + value_w + pad_r
    height = pad_t + len(rows) * row_h + axis_h

    series = (("p50", "--gold-light"), ("p95", "--gold"), ("p99", "--gold-dark"))

    def x_of(ms: float) -> float:
        return plot_x + (max(0.0, ms) / domain) * plot_w

    parts: list[str] = []
    parts.append(
        f'<svg class="chart" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img" '
        f'aria-label="Per-endpoint latency percentiles in milliseconds">'
    )

    # Vertical gridlines + axis ticks (5 divisions across the domain).
    n_ticks = 5
    grid_bottom = pad_t + len(rows) * row_h
    for i in range(n_ticks + 1):
        ms = domain * i / n_ticks
        gx = plot_x + (i / n_ticks) * plot_w
        parts.append(
            f'<line class="grid" x1="{gx:.1f}" y1="{pad_t}" '
            f'x2="{gx:.1f}" y2="{grid_bottom}" />'
        )
        parts.append(
            f'<text class="tick" x="{gx:.1f}" y="{grid_bottom + 18}" '
            f'text-anchor="middle">{_fmt_ms(ms)}</text>'
        )
    parts.append(
        f'<text class="axis-title" x="{plot_x + plot_w / 2:.1f}" '
        f'y="{grid_bottom + 34}" text-anchor="middle">milliseconds</text>'
    )

    # One labelled row per endpoint; three stacked bars (p50/p95/p99).
    for ri, (label, pct) in enumerate(rows):
        row_top = pad_t + ri * row_h
        group_h = len(series) * bar_h + (len(series) - 1) * bar_gap
        group_top = row_top + (row_h - group_h) / 2
        parts.append(
            f'<text class="ep-label" x="{pad_l}" '
            f'y="{row_top + row_h / 2 + 4:.1f}">{_esc(label)}</text>'
        )
        for si, (key, colour) in enumerate(series):
            ms = pct.get(key)
            if ms is None:
                continue
            by = group_top + si * (bar_h + bar_gap)
            bw = max(1.0, x_of(ms) - plot_x)
            parts.append(
                f'<rect class="bar" x="{plot_x}" y="{by:.1f}" '
                f'width="{bw:.1f}" height="{bar_h}" '
                f'fill="var({colour})" rx="1.5" />'
            )
            parts.append(
                f'<text class="bar-val" x="{plot_x + bw + 6:.1f}" '
                f'y="{by + bar_h - 2:.1f}">{_esc(key)} {_fmt_ms(ms)}</text>'
            )

    # Legend (p50/p95/p99 swatches) — top band, clear of the bottom axis ticks.
    legend_y = 16
    lx = plot_x
    for key, colour in series:
        parts.append(
            f'<rect class="swatch" x="{lx}" y="{legend_y - 9}" '
            f'width="11" height="11" fill="var({colour})" rx="1.5" />'
        )
        parts.append(
            f'<text class="legend" x="{lx + 16}" y="{legend_y}">{_esc(key)}</text>'
        )
        lx += 64

    parts.append("</svg>")
    return "".join(parts)


def render_html(report: dict) -> str:
    """Render a SELF-CONTAINED, offline HTML benchmark dashboard from ``report``.

    The output is a single document with inline CSS (the landing-page design
    tokens), a dark generation hero, a dataset stat-card row, a server-rendered
    inline-SVG grouped latency chart (p50/p95/p99 per endpoint on a shared ms
    scale), a latency table, a methodology footer, and the raw report embedded as
    an ``application/json`` script block. It references NO external URL and uses
    only system-font fallbacks, so it opens from ``file://`` with no network.

    Pure: reads only the documented report keys, performs no I/O, and renders
    byte-identically for the same input.
    """
    ds = report.get("dataset", {})
    gen = report.get("generation", {})
    meas = report.get("measurement", {})
    endpoints = report.get("endpoints", [])

    def _int(value: object, default: int = 0) -> int:
        return value if isinstance(value, int) else default

    def _num(value: object) -> str:
        """Thousands-separated integer string, or ``-`` when absent."""
        return f"{value:,}" if isinstance(value, (int, float)) else "-"

    subs = _int(ds.get("subscriptions"))
    n_res = _int(ds.get("resources"))
    rps = gen.get("resources_per_sec")
    wall = gen.get("wall_s")

    # --- Header subline ----------------------------------------------------
    subline_bits = [
        _esc(str(report.get("timestamp", "-"))),
        f"profile {_esc(str(ds.get('profile', '-')))}",
        f"seed {_esc(str(ds.get('seed', '-')))}",
        f"api-version {_esc(str(meas.get('api_version', '-')))}",
    ]
    subline = " · ".join(subline_bits)

    # --- Generation hero ---------------------------------------------------
    if rps is not None:
        hero_big = f"{round(rps):,}"
        wall_s = "-" if wall is None else f"{wall:.1f}"
        hero_sub = f"{n_res:,} resources generated in {wall_s}s"
    else:
        hero_big = "—"
        hero_sub = "generation skipped (reused existing data)"

    # --- Dataset cards -----------------------------------------------------
    cards = [
        ("subscriptions", ds.get("subscriptions")),
        ("resource groups", ds.get("resource_groups")),
        ("resources", ds.get("resources")),
        ("violations", ds.get("violations")),
        ("dependencies", ds.get("dependencies")),
    ]
    card_html = "".join(
        f'<div class="card"><div class="card-num">{_num(val)}</div>'
        f'<div class="card-label">{_esc(label)}</div></div>'
        for label, val in cards
    )

    # --- Latency chart (centrepiece) --------------------------------------
    chart_svg = _svg_latency_chart(endpoints)

    # --- Latency table -----------------------------------------------------
    table_rows: list[str] = []
    for ep in endpoints:
        pct = ep.get("percentiles", {})
        cells = [
            _esc(str(ep.get("label", "-"))),
            _esc(str(ep.get("status", "-"))),
            _num(ep.get("body_bytes")),
            _fmt_ms(pct.get("p50")),
            _fmt_ms(pct.get("p95")),
            _fmt_ms(pct.get("p99")),
            _fmt_ms(pct.get("min")),
            _fmt_ms(pct.get("max")),
            _fmt_ms(pct.get("mean")),
        ]
        td_label = f'<td class="mono">{cells[0]}</td>'
        td_rest = "".join(f'<td class="mono num">{c}</td>' for c in cells[1:])
        table_rows.append(f"<tr>{td_label}{td_rest}</tr>")
    table_body = "".join(table_rows)

    # --- Methodology footer + takeaway ------------------------------------
    p95s = [
        ep["percentiles"]["p95"]
        for ep in endpoints
        if ep.get("percentiles", {}).get("p95") is not None
    ]
    if p95s:
        import math

        takeaway = (
            f"all endpoints p95 &lt; {math.ceil(max(p95s))} ms on the "
            f"{n_res:,}-resource tenant"
        )
    else:
        takeaway = "no endpoint latencies were measured"
    methodology = (
        f"keep-alive (one reused connection) · "
        f"{_esc(str(meas.get('samples', '-')))} samples "
        f"+ {_esc(str(meas.get('warmup', '-')))} warmup per endpoint · "
        f"api-version {_esc(str(meas.get('api_version', '-')))}"
    )

    # --- Embedded raw report ----------------------------------------------
    # Escape the closing-tag sequence so the JSON cannot break out of <script>.
    embedded = json.dumps(report, indent=2, sort_keys=True).replace("</", "<\\/")

    css = """
  :root {
    --bg: #ffffff; --bg-secondary: #f8f7f5; --text: #0a0a0a; --text-muted: #57534e;
    --gold: #c9943a; --gold-dark: #a67a2d; --gold-light: #e8d4a8;
    --border: #e5e3df; --border-strong: #d0cdc6;
    --term-bg: #16140f; --term-text: #e8e4da; --term-gold: #d9a849; --term-green: #9bbf6e;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: system-ui, -apple-system, 'Segoe UI', sans-serif;
    background: var(--bg); color: var(--text); line-height: 1.6;
    -webkit-font-smoothing: antialiased;
  }
  .mono { font-family: 'Space Mono', ui-monospace, 'SF Mono', Consolas, monospace; }
  .wrap { max-width: 960px; margin: 0 auto; padding: 56px 32px 80px; }
  .label {
    font-family: 'Space Mono', ui-monospace, 'SF Mono', Consolas, monospace;
    font-size: 0.72rem; letter-spacing: 0.14em; text-transform: uppercase;
    color: var(--gold-dark); font-weight: 700;
  }
  .label::before { content: "\\25C6 "; color: var(--gold); }
  h1 {
    font-size: clamp(1.9rem, 4vw, 2.8rem); line-height: 1.08;
    letter-spacing: -0.02em; margin: 14px 0 10px;
  }
  .subline {
    font-family: 'Space Mono', ui-monospace, 'SF Mono', Consolas, monospace;
    font-size: 0.82rem; color: var(--text-muted);
  }
  section { margin-top: 44px; }
  section > .label { display: block; margin-bottom: 16px; }
  .hero {
    background: var(--term-bg); color: var(--term-text);
    border-radius: 6px; padding: 36px 32px; margin-top: 40px;
  }
  .hero .hero-num {
    font-family: 'Space Mono', ui-monospace, 'SF Mono', Consolas, monospace;
    font-size: clamp(2.6rem, 7vw, 4.2rem); font-weight: 700;
    color: var(--term-gold); line-height: 1; letter-spacing: -0.02em;
  }
  .hero .hero-unit { color: var(--term-text); font-size: 1.05rem; margin-left: 8px; }
  .hero .hero-sub { color: var(--term-green); margin-top: 12px; font-size: 0.95rem;
    font-family: 'Space Mono', ui-monospace, 'SF Mono', Consolas, monospace; }
  .cards { display: grid; grid-template-columns: repeat(5, 1fr); gap: 14px; }
  @media (max-width: 720px) { .cards { grid-template-columns: repeat(2, 1fr); } }
  .card {
    border: 1px solid var(--border); border-radius: 5px; padding: 18px 16px;
    background: var(--bg-secondary);
  }
  .card-num {
    font-family: 'Space Mono', ui-monospace, 'SF Mono', Consolas, monospace;
    font-size: 1.6rem; font-weight: 700; letter-spacing: -0.01em;
  }
  .card-label {
    font-size: 0.74rem; text-transform: uppercase; letter-spacing: 0.06em;
    color: var(--text-muted); margin-top: 4px;
  }
  .chart-wrap {
    border: 1px solid var(--border); border-radius: 6px; padding: 20px;
    overflow-x: auto;
  }
  svg.chart { display: block; max-width: 100%; height: auto; }
  svg.chart .grid { stroke: var(--border); stroke-width: 1; }
  svg.chart .tick, svg.chart .axis-title, svg.chart .legend {
    font-family: 'Space Mono', ui-monospace, 'SF Mono', Consolas, monospace;
    fill: var(--text-muted); font-size: 11px;
  }
  svg.chart .axis-title { fill: var(--text-muted); font-size: 11px; letter-spacing: 0.04em; }
  svg.chart .ep-label {
    font-family: 'Space Mono', ui-monospace, 'SF Mono', Consolas, monospace;
    fill: var(--text); font-size: 12px; font-weight: 700;
  }
  svg.chart .bar-val {
    font-family: 'Space Mono', ui-monospace, 'SF Mono', Consolas, monospace;
    fill: var(--text-muted); font-size: 10px;
  }
  table { width: 100%; border-collapse: collapse; font-size: 0.86rem; }
  th, td { text-align: right; padding: 9px 10px; border-bottom: 1px solid var(--border); }
  th { color: var(--text-muted); font-weight: 600; font-size: 0.74rem;
    text-transform: uppercase; letter-spacing: 0.05em; }
  th:first-child, td:first-child { text-align: left; }
  tbody tr:hover { background: var(--bg-secondary); }
  .num { font-variant-numeric: tabular-nums; }
  .footer {
    margin-top: 44px; padding-top: 24px; border-top: 1px solid var(--border);
    color: var(--text-muted); font-size: 0.85rem;
  }
  .footer .takeaway { color: var(--gold-dark); font-weight: 600; margin-top: 8px; }
"""

    columns = (
        "endpoint", "status", "body bytes", "p50", "p95", "p99", "min", "max", "mean"
    )
    thead = "".join(f"<th>{_esc(c)}</th>" for c in columns)

    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        "<title>ARM mock-server latency benchmark</title>\n"
        f"<style>{css}</style>\n"
        "</head>\n<body>\n"
        '<div class="wrap">\n'
        '<span class="label">benchmark</span>\n'
        "<h1>ARM mock-server latency</h1>\n"
        f'<div class="subline">{subline}</div>\n'
        f'<div class="hero">\n'
        f'<div><span class="hero-num">{_esc(hero_big)}</span>'
        f'<span class="hero-unit">resources/sec</span></div>\n'
        f'<div class="hero-sub">{_esc(hero_sub)}</div>\n'
        "</div>\n"
        '<section>\n<span class="label">dataset</span>\n'
        f'<div class="cards">{card_html}</div>\n</section>\n'
        '<section>\n<span class="label">endpoint latency</span>\n'
        f'<div class="chart-wrap">{chart_svg}</div>\n</section>\n'
        '<section>\n<span class="label">latency table (ms)</span>\n'
        f"<table><thead><tr>{thead}</tr></thead>"
        f"<tbody>{table_body}</tbody></table>\n</section>\n"
        f'<div class="footer">{methodology}'
        f'<div class="takeaway">{takeaway}</div></div>\n'
        f'<script type="application/json" id="report-data">{embedded}</script>\n'
        "</div>\n</body>\n</html>\n"
    )


# ---------------------------------------------------------------------------
# I/O orchestration (Task 2): generate / serve / keep-alive measure / report.
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Bind to port 0 to let the OS hand back a currently-free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _parse_generate_summary(stdout: str) -> dict:
    """Parse the ``tenantless generate`` STDOUT summary into a counts dict.

    The generator prints, e.g.:
      ``Generated tenant <id>: 410 subscriptions, 1230 resource groups,
        200000 resources, 50 violations, 99 dependencies
        (seed=7, target_resources=200000, elapsed=1234ms).``
    Missing fields are simply absent from the returned dict.
    """
    import re

    out: dict = {}
    patterns = {
        "subscriptions": r"(\d+)\s+subscriptions",
        "resource_groups": r"(\d+)\s+resource groups",
        "resources": r"(\d+)\s+resources",
        "violations": r"(\d+)\s+violations",
        "dependencies": r"(\d+)\s+dependencies",
        "seed": r"seed=(\d+)",
        "elapsed_ms": r"elapsed=(\d+)ms",
    }
    for key, pat in patterns.items():
        m = re.search(pat, stdout)
        if m:
            out[key] = int(m.group(1))
    return out


def _run_generate(
    *, profile: str, subscriptions: int, resources: int, seed: int, database_url: str
) -> tuple[dict, float]:
    """Run ``tenantless generate`` as an argv-LIST subprocess (never shell=True).

    Returns the parsed summary counts and the wall-clock seconds. Raises a
    ``click.ClickException`` if the generator exits non-zero.
    """
    # Resolve the `tenantless` console script (the package has no __main__, so
    # `python -m tenantless` does not work). Prefer PATH (the active venv's
    # Scripts/bin under `uv run`), fall back to the running interpreter's dir.
    exe = shutil.which("tenantless")
    if exe is None:
        _bin = Path(sys.executable).parent / (
            "tenantless.exe" if os.name == "nt" else "tenantless"
        )
        if not _bin.exists():
            raise click.ClickException(
                "could not locate the 'tenantless' console script; "
                "install with `uv pip install -e .`"
            )
        exe = str(_bin)
    cmd = [
        exe,
        "generate",
        "--profile",
        profile,
        "--subscriptions",
        str(subscriptions),
        "--resources",
        str(resources),
        "--seed",
        str(seed),
        "--force",
    ]
    env = {**os.environ, "DATABASE_URL": database_url}
    t0 = time.perf_counter()
    completed = subprocess.run(  # noqa: S603 - argv list, trusted module entrypoint
        cmd,
        env=env,
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
    )
    wall_s = time.perf_counter() - t0
    if completed.returncode != 0:
        raise click.ClickException(
            "tenantless generate failed "
            f"(exit {completed.returncode}):\n{completed.stderr[-2000:]}"
        )
    summary = _parse_generate_summary(completed.stdout)
    return summary, wall_s


def _discover_present_type(database_url: str) -> tuple[str, str]:
    """Query the DB read-only for the busiest subscription + its commonest type.

    Returns ``(busiest_subscription_id, present_resource_type)`` — a real
    ``(sub, type)`` pair that the server is guaranteed to serve, used for the
    ``$filter`` probe. Read-only: a single ``SELECT`` each, no writes.
    """
    import psycopg

    with psycopg.connect(database_url, connect_timeout=5) as conn:  # read-only
        with conn.cursor() as cur:
            cur.execute(
                "SELECT subscription_id::text, count(*) AS n "
                "FROM synthetic.resources GROUP BY subscription_id "
                "ORDER BY n DESC LIMIT 1"
            )
            row = cur.fetchone()
            if row is None:
                raise click.ClickException(
                    "no resources found in synthetic.resources — generate first "
                    "(omit --skip-generate)."
                )
            busiest_sub = row[0]
            cur.execute(
                "SELECT type, count(*) AS n FROM synthetic.resources "
                "WHERE subscription_id = %s GROUP BY type ORDER BY n DESC LIMIT 1",
                (busiest_sub,),
            )
            present_type = cur.fetchone()[0]
    return busiest_sub, present_type


def _wait_ready(host: str, port: int, proc: subprocess.Popen, *, deadline_s: float = 30.0) -> None:
    """Poll ``GET /subscriptions`` (keep-alive) until ready or the deadline elapses.

    Fails fast if the server child has already exited. A sub-500 status proves the
    listener is up and serving; a 5xx means it bound but is still warming up.
    """
    end = time.monotonic() + deadline_s
    last_err: Exception | None = None
    while time.monotonic() < end:
        if proc.poll() is not None:
            raise click.ClickException(
                f"server child exited early with code {proc.returncode} before readiness"
            )
        conn = http.client.HTTPConnection(host, port, timeout=5)
        try:
            conn.request("GET", "/subscriptions", headers={"Authorization": "Bearer bench"})
            resp = conn.getresponse()
            resp.read()
            if resp.status < 500:
                return
            last_err = RuntimeError(f"server warming up: HTTP {resp.status}")
        except (ConnectionError, OSError) as exc:
            last_err = exc
        finally:
            conn.close()
        time.sleep(0.25)
    raise click.ClickException(f"server did not become ready within {deadline_s}s: {last_err}")


def _measure_endpoint(
    host: str, port: int, ep: Endpoint, *, samples: int, warmup: int
) -> dict:
    """Measure one endpoint over a SINGLE persistent keep-alive connection.

    One ``http.client.HTTPConnection`` is opened and reused across every warmup
    and timed request (NOT a new connection per request) so the measured latency
    reflects server processing time, not TCP setup. Warmup requests are discarded;
    the timed requests' latencies feed ``percentiles``. Records the last observed
    status and body size.
    """
    conn = http.client.HTTPConnection(host, port, timeout=30)
    headers = {"Authorization": "Bearer bench"}
    status = None
    body_bytes = 0
    latencies: list[float] = []
    try:
        for _ in range(warmup):
            conn.request("GET", ep.path, headers=headers)
            resp = conn.getresponse()
            resp.read()  # MUST drain so the connection is reusable for the next request.
        for _ in range(samples):
            t = time.perf_counter()
            conn.request("GET", ep.path, headers=headers)
            resp = conn.getresponse()
            body = resp.read()
            latencies.append((time.perf_counter() - t) * 1000.0)
            status = resp.status
            body_bytes = len(body)
    finally:
        conn.close()
    return {
        "label": ep.label,
        "path": ep.path,
        "status": status,
        "body_bytes": body_bytes,
        "percentiles": percentiles(latencies),
    }


def _write_reports(
    report: dict, out_path: Path, *, subscriptions: int, resources: int, stem: str | None = None
) -> dict:
    """Write the ``.json`` / ``.md`` / ``.html`` triad for ``report`` to ``out_path``.

    Returns the three written ``Path`` objects keyed ``json`` / ``md`` / ``html``.
    The JSON is normalized (sorted keys, 2-space indent) so a re-render of a saved
    report is byte-stable. The HTML is the self-contained offline dashboard.
    """
    out_path.mkdir(parents=True, exist_ok=True)
    stem = stem or f"scale-{subscriptions}sub-{resources}"
    json_path = out_path / f"{stem}.json"
    md_path = out_path / f"{stem}.md"
    html_path = out_path / f"{stem}.html"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    html_path.write_text(render_html(report), encoding="utf-8")
    return {"json": json_path, "md": md_path, "html": html_path}


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--subscriptions", default=410, show_default=True, help="Subscription count to generate.")
@click.option("--resources", default=200_000, show_default=True, help="Resource count to generate.")
@click.option("--seed", default=7, show_default=True, help="Deterministic generation seed.")
@click.option("--profile", default="enterprise", show_default=True, help="Generator profile name.")
@click.option("--samples", default=200, show_default=True, help="Timed requests per endpoint.")
@click.option("--warmup", default=10, show_default=True, help="Discarded priming requests per endpoint.")
@click.option("--api-version", default="2022-12-01", show_default=True, help="ARM api-version query value.")
@click.option(
    "--database-url",
    default=DEFAULT_DATABASE_URL,
    show_default=True,
    help="Postgres URL for generation + read-only discovery.",
)
@click.option("--port", default=0, show_default=True, help="Server port (0 = pick a free port).")
@click.option(
    "--base-url",
    default=None,
    help="Base URL the server advertises (defaults to http://127.0.0.1:<port>).",
)
@click.option("--skip-generate", is_flag=True, help="Reuse existing DB data; do not regenerate.")
@click.option("--no-serve", is_flag=True, help="Assume a server is already running at --base-url.")
@click.option(
    "--out",
    "out_dir",
    default="docs/benchmarks",
    show_default=True,
    help="Output directory for the JSON + Markdown report.",
)
@click.option(
    "--timestamp",
    default=None,
    help="Explicit ISO timestamp to stamp the report (defaults to wall-clock UTC).",
)
@click.option(
    "--from-json",
    "from_json",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help=(
        "Offline re-render mode: load a saved JSON report and re-emit its "
        "Markdown + HTML (and a normalized JSON copy) to --out, with NO DB and "
        "NO server. Wins over generate/serve flags when combined."
    ),
)
def main(
    subscriptions: int,
    resources: int,
    seed: int,
    profile: str,
    samples: int,
    warmup: int,
    api_version: str,
    database_url: str,
    port: int,
    base_url: str | None,
    skip_generate: bool,
    no_serve: bool,
    out_dir: str,
    timestamp: str | None,
    from_json: str | None,
) -> None:
    """Run the ARM latency benchmark and emit a JSON + Markdown report.

    Flow: (optionally) generate the tenant, discover the busiest subscription and
    its commonest resource type, (optionally) launch the server on a free port,
    measure every endpoint over a persistent keep-alive connection, then write the
    report. The server child is ALWAYS torn down in a ``finally`` block, and the
    report is written even when a later step fails.

    ``--from-json PATH`` short-circuits the whole pipeline: it loads a saved report
    and re-emits the Markdown + HTML (and a normalized JSON copy) with NO database
    and NO server, so any past run can be re-visualized offline. When combined with
    generate/serve flags, ``--from-json`` wins and those flags are ignored.
    """
    out_path = Path(out_dir)
    if not out_path.is_absolute():
        out_path = _REPO_ROOT / out_path

    # --- Offline re-render mode (no DB, no server) ------------------------
    if from_json is not None:
        if not skip_generate or no_serve:
            click.echo(
                "# --from-json: ignoring generate/serve flags (offline re-render)",
                err=True,
            )
        report = json.loads(Path(from_json).read_text(encoding="utf-8"))
        ds = report.get("dataset", {})
        subs = ds.get("subscriptions", subscriptions)
        n_res = ds.get("resources", resources)
        # Name the re-rendered outputs after the SOURCE json's stem, so
        # `--from-json scale-410sub-200000.json` writes scale-410sub-200000.{md,html}
        # alongside it (not a divergent actual-count name).
        src_stem = Path(from_json).stem
        written = _write_reports(
            report, out_path, subscriptions=subs, resources=n_res, stem=src_stem
        )
        click.echo(render_markdown(report))
        for key in ("json", "md", "html"):
            click.echo(f"Wrote: {written[key]}", err=True)
        return

    if timestamp is None:
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    dataset: dict = {"profile": profile, "seed": seed}
    generation: dict = {}

    # 1. Generation (unless skipped).
    if not skip_generate:
        summary, wall_s = _run_generate(
            profile=profile,
            subscriptions=subscriptions,
            resources=resources,
            seed=seed,
            database_url=database_url,
        )
        dataset.update(summary)
        n_res = summary.get("resources", resources)
        generation = {
            "wall_s": wall_s,
            "resources_per_sec": (n_res / wall_s) if wall_s > 0 else None,
        }
    else:
        click.echo("# skip-generate: reusing existing synthetic data", err=True)

    # 2. Read-only discovery of the (busiest sub, present type) probe pair.
    busiest_sub, present_type = _discover_present_type(database_url)
    if "resources" not in dataset:
        # Fill dataset counts from the DB when generation was skipped.
        import psycopg

        with psycopg.connect(database_url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(DISTINCT subscription_id) FROM synthetic.resources")
                dataset["subscriptions"] = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM synthetic.resources")
                dataset["resources"] = cur.fetchone()[0]

    endpoints = build_endpoints(busiest_sub, present_type, api_version)

    report: dict = {
        "timestamp": timestamp,
        "dataset": dataset,
        "generation": generation,
        "measurement": {"samples": samples, "warmup": warmup, "api_version": api_version},
        "present_type": present_type,
        "busiest_subscription": busiest_sub,
        "endpoints": [],
    }

    # 3. Server lifecycle (argv-list launch; ALWAYS torn down in finally).
    proc: subprocess.Popen | None = None
    out_path.mkdir(parents=True, exist_ok=True)

    try:
        if no_serve:
            if base_url is None:
                raise click.ClickException("--no-serve requires --base-url")
            parts = urlsplit(base_url)
            host = parts.hostname or "127.0.0.1"
            srv_port = parts.port or 80
        else:
            srv_port = port or _free_port()
            host = "127.0.0.1"
            adv = base_url or f"http://127.0.0.1:{srv_port}"
            cmd = serve._discover_command(_REPO_ROOT) + [
                "--port",
                str(srv_port),
                "--base-url",
                adv,
                "--database-url",
                database_url,
            ]
            env = {
                **os.environ,
                "DATABASE_URL": database_url,
                "BASE_URL": adv,
                "PORT": str(srv_port),
            }
            proc = subprocess.Popen(cmd, env=env)  # noqa: S603 - argv list, trusted discovery
            _wait_ready(host, srv_port, proc)

        # 4. Keep-alive measurement per endpoint.
        for ep in endpoints:
            report["endpoints"].append(
                _measure_endpoint(host, srv_port, ep, samples=samples, warmup=warmup)
            )
    finally:
        # 5. Report is ALWAYS written (even on partial measurement) as the
        #    .json / .md / .html triad, and the server child is ALWAYS torn down
        #    deterministically.
        markdown = render_markdown(report)
        written = _write_reports(
            report, out_path, subscriptions=subscriptions, resources=resources
        )
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)

    click.echo(markdown)
    for key in ("json", "md", "html"):
        click.echo(f"Wrote: {written[key]}", err=True)


if __name__ == "__main__":
    main()
