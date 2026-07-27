# ARM mock-server latency benchmarks

This directory holds the reproducible latency benchmark harness for the
ARM-compatible mock server and the committed result reports it produces.

The harness — [`scripts/bench_arm_latency.py`](../../scripts/bench_arm_latency.py) —
is a scale-parameterized measurement tool. For an arbitrary tenant size it:

1. (optionally) generates the synthetic tenant via the project's `generate` CLI,
2. launches the mock server on a free port via the project's discovery seam,
3. measures ARM list / `$filter` endpoint latency with a **keep-alive**
   methodology, and
4. emits a machine-readable JSON report, a human-readable Markdown table, and a
   self-contained HTML dashboard.

It complements the fixed-scale pytest gate
([`tests/test_scale_benchmark.py`](../../tests/test_scale_benchmark.py)): that one
is a pass/fail CI assertion; this is an ad-hoc tool whose canonical report is
committed here.

## Provenance of the committed report

The committed run measures an estate generated from the bundled `enterprise`
profile, which is itself synthetic — generated from a hand-authored bootstrap and
then analyzed, with no real Azure tenant anywhere in its derivation chain (see
[`docs/profiles.md`](../profiles.md)).

This matters for a benchmark specifically: a report names the dataset it ran
against, so a measurement taken over a real-derived profile would carry that
estate's shape — subscription count, resource-group count, type mix — into a
published document even if the profile itself were withheld.
`scripts/check_release_provenance.py` enforces that, checking shipped docs as well
as profiles.

Latency figures are machine-dependent. Treat them as a shape (which endpoints cost
what, and how that changes with scale), not as an absolute claim about your
hardware.

## Running it

```bash
# Full run against the bundled synthetic `enterprise` profile.
uv run python scripts/bench_arm_latency.py --subscriptions 300 --resources 150000

# Reuse data already in the DB and a server you started yourself.
uv run python scripts/bench_arm_latency.py \
    --skip-generate --no-serve --base-url http://127.0.0.1:8080

# A quick smoke run at small scale.
uv run python scripts/bench_arm_latency.py \
    --subscriptions 10 --resources 2000 --samples 50 --warmup 5
```

See `--help` for the full option set (`--seed`, `--profile`, `--samples`,
`--warmup`, `--api-version`, `--database-url`, `--port`, `--out`, `--timestamp`,
`--from-json`).

Each run writes three artifacts to `--out` (default this directory):

- `scale-<subs>sub-<resources>.json` — the full machine-readable report
  (dataset counts, generation throughput, per-endpoint percentiles, sample and
  warmup counts, timestamp).
- `scale-<subs>sub-<resources>.md` — the rendered Markdown table.
- `scale-<subs>sub-<resources>.html` — a **self-contained HTML dashboard**.

## The HTML dashboard

The `.html` artifact visualizes the run: a generation-throughput hero, a row of
dataset stat cards, and the centrepiece — a per-endpoint grouped bar chart of
**p50 / p95 / p99** latency on a shared millisecond scale — plus the full latency
table and a methodology footer. The raw JSON report is embedded inside the page
for reference.

It is **fully self-contained and offline**: all CSS is inline, the bar chart is a
server-rendered inline `<svg>` (no JavaScript, no charting library), and there are
no external requests, fonts, or CDN links. To view it, just **open the file** — for
example double-click it or run `open scale-300sub-150000.html` (macOS) /
`xdg-open …` (Linux) / `start …` (Windows). No server and no network are needed; it
renders correctly straight from `file://`.

## Re-rendering a saved report offline (`--from-json`)

Any saved JSON report can be re-visualized without a database or a running server:

```bash
# Re-emit the Markdown + HTML (and a normalized JSON copy) from a saved report.
uv run python scripts/bench_arm_latency.py \
    --from-json docs/benchmarks/scale-300sub-150000.json --out docs/benchmarks
```

`--from-json` short-circuits the whole pipeline — it performs **no generation, no
serving, and no DB access** — so it works anywhere, offline, against any committed
or archived report. The subscription/resource counts (and thus the output
filenames) are taken from the report's `dataset`. If `--from-json` is combined with
generate/serve flags, **`--from-json` wins** and those flags are ignored.

## Measurement methodology (keep-alive)

The whole point of this harness is an accurate measurement of **server
processing latency** rather than connection-setup overhead.

Each endpoint is measured over **one persistent
`http.client.HTTPConnection` reused across every warmup and timed request**. The
connection is opened once per endpoint; the response body is fully drained after
each request so the socket can carry the next one. Warmup requests prime the
connection (and the server's query plans / OS buffers) and are discarded; the
timed requests feed the percentile summary.

Percentiles use a documented, deterministic cut-point rule
(`statistics.quantiles(..., n=100, method="inclusive")`), so p50/p95/p99 are
reproducible and free of interpolation ambiguity. A single sample collapses every
percentile to that one value; an empty sample set yields `None` (a missing
measurement is never reported as a zero latency).

### Why per-request connection setup is excluded

- **A fresh TCP connection per request** (for example `urllib.request.urlopen`
  called once per sample) folds the connect handshake into every measurement.
  At high request rates the p95 then reflects connection churn, not the server.
- **Spawning a process per request** (for example `curl` in a loop) is worse
  still: on some platforms the process-spawn floor is tens to hundreds of
  milliseconds and dominates the figure entirely, masking real server time.

Both are deliberately avoided. The harness opens exactly one connection per
endpoint and reuses it, so the reported numbers are the server's own latency.

## Endpoints measured

For the **busiest subscription** in the dataset (and its most common resource
type, used as the `$filter` value), the harness probes:

| Label | Request |
| --- | --- |
| `list_subscriptions` | `GET /subscriptions` |
| `list_resource_groups` | `GET /subscriptions/{sub}/resourceGroups` |
| `list_resources` | `GET /subscriptions/{sub}/resources` |
| `filter_resource_type` | `GET /subscriptions/{sub}/resources?$filter=resourceType eq '<type>'` |

Route casing matches the server's ARM contract exactly — `resourceGroups` is
camelCase, and the `$filter` field is `resourceType` (which the server maps to
its `type` column), URL-encoded.

## Server lifecycle and safety

The server is launched as an **argv list** through the project's binary
discovery seam (never `shell=True`), bound to a free port, and **always torn down
in a `finally` block**. The report is likewise written in a `finally` block, so a
partial run still records whatever was measured. Database access for discovering
the busiest subscription is **read-only** (`SELECT` only).
