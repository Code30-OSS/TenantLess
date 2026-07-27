"""Opt-in full-scale benchmark (INFRA-05, CONTEXT D-04/D-05/D-06).

A single ``-m scale`` test that proves the pipeline at the real headline target —
~2000 subscriptions / ~500K resources — asserting all three bars:

  (a) correctness at scale: counts within +/-5% of target, 0 dangling references
      (the verbatim XSUB-06 anti-join), and full single-visit pagination over the
      ARM server (every resource walked exactly once);
  (b) generation throughput: ``generate_tenant`` wall-clock + peak Python memory
      under a GENEROUS ceiling (catch gross regressions only);
  (c) server latency: list / detail / ``$filter`` p95 under a generous threshold.

The raw numbers (gen_wall_s, parent_peak_bytes, n_res, n_dangling, the three p95 values)
are ALWAYS written to a report artifact in a ``finally`` block BEFORE any assertion
can fail — so the benchmark records data regardless of pass/fail (D-06).

Gated behind a dedicated ``scale`` marker (deselected by default like
``integration``) so everyday runs stay fast. This is the same generate -> COPY ->
serve -> HTTP shape as ``tests/test_e2e_pipeline.py``, just at 500K behind a
heavier marker.

Safety (T-07-07, Pitfall 4 / A4): truncating the shared :5433 synthetic schema is
gated by BOTH the ``scale`` marker AND an explicit env opt-in
(``TENANTLESS_SCALE_ALLOW_TRUNCATE=1``) so a dev dataset is NEVER silently wiped.
The ``pg_conn`` fixture (tests/conftest.py) skips cleanly when Postgres is down.

The launched server (T-07-09) uses the Plan 01 discovery seam
(``serve._discover_command``) as an argv-LIST child (never ``shell=True``), pinned
to a chosen free port, and ALWAYS terminated in a ``finally`` block.
"""

from __future__ import annotations

import json
import os
import socket
import statistics
import subprocess
import time
import tracemalloc
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlparse

import pytest

# Opt-in env gate for the destructive truncate of the shared :5433 schema (T-07-07).
_ALLOW_TRUNCATE_ENV = "TENANTLESS_SCALE_ALLOW_TRUNCATE"

# The headline target (CONTEXT D-04). N is overridable via env so the knob exists,
# but the DEFAULT stays at the full ~2000/500K production scale.
_N_SUBS = int(os.environ.get("TENANTLESS_SCALE_N_SUBS", "2000"))
_N_RESOURCES = int(os.environ.get("TENANTLESS_SCALE_N_RESOURCES", "500000"))
_SEED = 42

# --jobs worker count for the benchmarked run (SPEED-01/SPEED-03). The CLI/pipeline
# semantics are: 0 == all cores, otherwise the literal count (clamped internally to
# [1, cpu_count]). Default 0 so the benchmark records the multi-core headline number.
_JOBS = int(os.environ.get("TENANTLESS_SCALE_JOBS", "0"))
_EFFECTIVE_JOBS = (os.cpu_count() or 1) if _JOBS == 0 else _JOBS

# Data-derived SPEED-01 "materially faster" bar (13-baseline-profile.md). The honest
# demo baseline is 996.1 res/s PURE-TIMING (96.5s / 96,144 res, seed 42). The Target is
# >= 4x that (the Amdahl single-core ceiling from the 77.9% tag hotspot); the Rust+rayon
# contingency is justified only by a MEASURED miss below 3x. These are res/sec so they are
# scale-invariant (the demo and the full 2000/500K run gate against the same bar).
_BASELINE_RES_PER_SEC = 996.1  # 13-01 demo pure-timing baseline
_TARGET_MULTIPLIER = 4.0  # SPEED-01 Target (>= 4x => "materially faster")
_CONTINGENCY_MULTIPLIER = 3.0  # < 3x => Rust+rayon contingency justified (human-gated)
_TARGET_RES_PER_SEC = _BASELINE_RES_PER_SEC * _TARGET_MULTIPLIER  # ~3984 res/s
_CONTINGENCY_RES_PER_SEC = _BASELINE_RES_PER_SEC * _CONTINGENCY_MULTIPLIER  # ~2988 res/s

# Small page size so the busiest subscription guarantees MULTIPLE pages to walk.
# At ~2000 subs / ~500K resources the busiest single subscription holds only a few
# hundred resources, so the page size must be well below that to span >1 page.
_PAGE_TOP = 200

# Latency sample count per endpoint (D-05c: p95 over n>=200 requests each).
_LATENCY_SAMPLES = 200

# GENEROUS gates (D-06): chosen well above any reasonable observed value to catch
# only gross regressions / OOM, absorbing hardware variance.
# Observed baseline on dev hardware: single-threaded Python generation of ~500K
# resources runs ~13-15 min (774-849s measured). A GENEROUS ceiling sits well above
# that so only a gross (~2x) regression trips it — not the real baseline (D-06).
_GEN_WALL_CEILING_S = 1800.0  # generation wall-clock ceiling (30 min)
_GEN_PEAK_CEILING_BYTES = 8 * 1024**3  # 8 GiB peak Python allocation
_LIST_P95_CEILING_MS = 2000.0  # list p95 < 2s at 500K
_DETAIL_P95_CEILING_MS = 2000.0  # detail p95 < 2s
_FILTER_P95_CEILING_MS = 2000.0  # $filter p95 < 2s

_REPO_ROOT = Path(__file__).resolve().parents[1]
_REPORT_PATH = (
    _REPO_ROOT
    / ".planning"
    / "phases"
    / "07-integration-scale-testing"
    / "scale-report.json"
)
# Phase-13 committed throughput artifact (SPEED-03 source of truth). Carries the
# measured gen_wall_s / res_per_sec / parent_peak_bytes / n_jobs / cpu_count / scale that the
# landing-page generation-time claim must equal (no aspirational figure).
_THROUGHPUT_PATH = (
    _REPO_ROOT
    / ".planning"
    / "phases"
    / "13-generation-speed"
    / "13-throughput.json"
)
_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgres://tenantless:tenantless_dev@localhost:5433/tenantless",
)


def _free_port() -> int:
    """Bind to port 0 to let the OS hand back a currently-free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http_get(url: str, *, bearer: str | None = "Bearer scale-bench") -> tuple[int, dict]:
    """GET ``url`` with an optional Bearer header; return ``(status, json_body)``.

    A non-2xx status is surfaced via ``urllib``'s ``HTTPError`` whose ``.code`` and
    JSON body we return verbatim, so error shapes are asserted as data.
    """
    req = urllib.request.Request(url, method="GET")
    if bearer is not None:
        req.add_header("Authorization", bearer)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"_raw": raw}
        return exc.code, body


def _wait_ready(base_url: str, proc: subprocess.Popen, *, deadline_s: float = 30.0) -> None:
    """Poll ``GET /subscriptions`` until the server answers or the deadline elapses;
    fail fast if the child has already exited."""
    sub_url = f"{base_url}/subscriptions"
    end = time.monotonic() + deadline_s
    last_err: Exception | None = None
    while time.monotonic() < end:
        if proc.poll() is not None:
            raise AssertionError(
                f"server child exited early with code {proc.returncode} before readiness"
            )
        try:
            # A non-5xx response proves the listener is up AND serving; a 5xx
            # means it bound but is still warming up — keep polling (WR-02).
            status, _ = _http_get(sub_url)
            if status < 500:
                return
            last_err = AssertionError(f"server warming up: HTTP {status}")
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_err = exc
        time.sleep(0.25)
    raise AssertionError(f"server did not become ready within {deadline_s}s: {last_err}")


def _p95_latency_ms(url: str, *, n: int = _LATENCY_SAMPLES) -> float:
    """Sample ``GET url`` ``n`` times; return the p95 latency in milliseconds.

    Uses ``statistics.quantiles(samples, n=20, method="inclusive")[18]`` (RESEARCH
    Pattern 4): 19 cut points, index 18 is the p95 boundary.
    """
    samples: list[float] = []
    for _ in range(n):
        t = time.perf_counter()
        status, _ = _http_get(url)
        assert status == 200, f"latency sample GET failed ({status}): {url}"
        samples.append((time.perf_counter() - t) * 1000.0)
    return statistics.quantiles(samples, n=20, method="inclusive")[18]


def _write_report(report: dict) -> None:
    """Persist the raw benchmark numbers to BOTH report artifacts (D-06).

    Called from the ``finally`` block BEFORE any assertion can fail, so the numbers
    are recorded regardless of the test outcome. Writes the legacy Phase-7
    ``scale-report.json`` (back-compat) AND the Phase-13 ``13-throughput.json``
    (SPEED-03 source of truth for the landing-page claim).
    """
    payload = json.dumps(report, indent=2, sort_keys=True)
    for path in (_REPORT_PATH, _THROUGHPUT_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")


@pytest.mark.scale
def test_scale_benchmark_correctness_throughput_latency(generator_profile, pg_conn):
    """Full ~2000/500K benchmark: correctness + throughput + latency, report always written.

    (a) correctness: counts +/-5%, 0 dangling refs (verbatim XSUB-06 anti-join),
        full single-visit pagination over the ARM server;
    (b) throughput: generate_tenant wall-clock + peak Python memory (generous gate);
    (c) latency: list / detail / $filter p95 (generous gate).

    The raw numbers are ALWAYS written to ``scale-report.json`` in a ``finally``
    block BEFORE any assertion can fail (D-06). Cross-sub topology is NOT asserted
    over ARM (cross-sub-risk tooling only). The server child is ALWAYS torn down
    in ``finally``.
    """
    from tenantless.generator.pipeline import generate_tenant
    from tenantless.generator import writer
    from tenantless import serve

    # T-07-07: the destructive truncate must be explicitly opted into. The marker
    # already gates this off the default suite; the env gate is the second lock so a
    # real dev dataset on :5433 is NEVER silently wiped (Pitfall 4 / A4).
    if os.environ.get(_ALLOW_TRUNCATE_ENV) not in ("1", "true", "yes"):
        pytest.skip(
            f"set {_ALLOW_TRUNCATE_ENV}=1 to allow this benchmark to TRUNCATE + rewrite "
            "the synthetic schema on :5433 (regeneratable synthetic data only)"
        )

    # Report accumulator — every measured number lands here and is flushed in finally.
    report: dict = {
        "target": {"n_subs": _N_SUBS, "n_resources": _N_RESOURCES, "seed": _SEED},
        "scale": {"n_subs": _N_SUBS, "n_resources": _N_RESOURCES, "seed": _SEED},
        "n_jobs": _EFFECTIVE_JOBS,
        "jobs_env": _JOBS,
        "cpu_count": os.cpu_count(),
        "gen_wall_s": None,
        "res_per_sec": None,
        "parent_peak_bytes": None,
        "n_res": None,
        "n_dangling": None,
        "list_p95_ms": None,
        "detail_p95_ms": None,
        "filter_p95_ms": None,
        # SPEED-01 materially-faster gate inputs/outcome (data-derived, 13-01).
        "baseline_res_per_sec": _BASELINE_RES_PER_SEC,
        "target_res_per_sec": _TARGET_RES_PER_SEC,
        "contingency_threshold_res_per_sec": _CONTINGENCY_RES_PER_SEC,
        "target_met": None,
        "contingency_missed": None,
    }
    proc: subprocess.Popen | None = None
    try:
        # --- (b) throughput: PURE-TIMING generation (NO instrument) for the HONEST
        #     gen_wall_s / res_per_sec. tracemalloc inflates wall-clock ~3.7x and
        #     cProfile ~1.7x (13-baseline-profile.md), so the published landing-page
        #     number and the materially-faster gate MUST be pure-timing — otherwise
        #     the parallel win looks ~3.7x worse than it is. Peak memory is captured in
        #     a SEPARATE tracemalloc pass below (13-01 "separate timing/mem runs"). ---
        t0 = time.perf_counter()
        result = generate_tenant(
            generator_profile,
            seed=_SEED,
            n_subs=_N_SUBS,
            n_resources=_N_RESOURCES,
            inject_violations=True,
            inject_cross_sub=True,
            jobs=_JOBS,
        )
        gen_wall_s = time.perf_counter() - t0
        tenant = result.tenant
        violation_rows = result.violations
        dependency_rows = result.dependencies

        # --- (a) correctness: counts within +/-5% of target (D-05a) ------------------
        n_res = sum(len(rg.resources) for rg in tenant.resource_groups)
        res_per_sec = n_res / gen_wall_s if gen_wall_s > 0 else 0.0
        report["gen_wall_s"] = gen_wall_s
        report["n_res"] = n_res
        report["res_per_sec"] = res_per_sec
        report["target_met"] = res_per_sec >= _TARGET_RES_PER_SEC
        report["contingency_missed"] = res_per_sec < _CONTINGENCY_RES_PER_SEC

        # --- peak memory: a SEPARATE tracemalloc-wrapped generation at the SAME scale
        #     and job count; its WALL-CLOCK IS DISCARDED (tracemalloc pollutes timing).
        #     tracemalloc only sees THIS (parent) process's Python allocations — under
        #     jobs>1 the ProcessPoolExecutor workers each carry their own (un-measured)
        #     heap, so this is a PARENT-process figure, NOT the whole-run peak. It is a
        #     lower bound on true RSS, recorded honestly as `parent_peak_bytes`. Summing
        #     worker RSS would need psutil / OS RSS, which we deliberately avoid (no new
        #     dep; not portable on Windows). ------------------------------------------
        tracemalloc.start()
        _mem_result = generate_tenant(
            generator_profile,
            seed=_SEED,
            n_subs=_N_SUBS,
            n_resources=_N_RESOURCES,
            inject_violations=True,
            inject_cross_sub=True,
            jobs=_JOBS,
        )
        _, parent_peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        del _mem_result
        report["parent_peak_bytes"] = parent_peak_bytes

        # --- write into the throwaway DB (FK order), under the opt-in guard ----------
        writer.truncate_synthetic(pg_conn)
        writer.write_tenant(
            pg_conn, tenant, dependencies=dependency_rows, violations=violation_rows
        )
        pg_conn.commit()

        # --- (a) 0-dangling anti-join (VERBATIM from test_generator_copy.py:196-205) --
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM synthetic.dependencies d "
                "WHERE NOT EXISTS ("
                "    SELECT 1 FROM synthetic.resources r WHERE r.id = d.source_resource_id"
                ") OR NOT EXISTS ("
                "    SELECT 1 FROM synthetic.resources r WHERE r.id = d.target_resource_id"
                ")"
            )
            n_dangling = cur.fetchone()[0]
        report["n_dangling"] = n_dangling

        # Pick the busiest subscription + total resource count for the pagination walk,
        # and one concrete (id, type) for the detail + $filter latency probes.
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT subscription_id::text, count(*) AS n "
                "FROM synthetic.resources GROUP BY subscription_id "
                "ORDER BY n DESC LIMIT 1"
            )
            busiest_sub, busiest_count = cur.fetchone()
            cur.execute("SELECT count(*) FROM synthetic.resources")
            total_res = cur.fetchone()[0]
            # A known resource under the busiest sub for the detail probe + its type
            # for the $filter probe (a real id/type pair that the server serves).
            cur.execute(
                "SELECT id, type FROM synthetic.resources "
                "WHERE subscription_id = %s LIMIT 1",
                (busiest_sub,),
            )
            detail_id, detail_type = cur.fetchone()

        # Release the read locks before launching the server. The count/detail SELECTs
        # above leave pg_conn in an open transaction holding ACCESS SHARE on
        # synthetic.resources; the server's startup schema preflight runs
        # `ALTER TABLE resources ADD COLUMN IF NOT EXISTS drift_deleted_at` (sql/006),
        # which must take ACCESS EXCLUSIVE and would block forever behind that lock —
        # the server stays alive but never binds, so _wait_ready times out. Committing
        # ends the transaction and frees the lock so preflight can proceed.
        pg_conn.commit()

        # --- (c) launch the real server on a free port via the Plan 01 seam ----------
        port = _free_port()
        base_url = f"http://127.0.0.1:{port}"
        cmd = serve._discover_command(_REPO_ROOT) + [
            "--port",
            str(port),
            "--base-url",
            base_url,
            "--database-url",
            _DATABASE_URL,
        ]
        env = {
            **os.environ,
            "DATABASE_URL": _DATABASE_URL,
            "BASE_URL": base_url,
            "PORT": str(port),
        }
        # argv LIST, never shell=True (T-07-09). Bind to a chosen free port.
        proc = subprocess.Popen(cmd, env=env)  # noqa: S603 - argv list, trusted discovery
        _wait_ready(base_url, proc)

        # --- (a) full single-visit pagination across the WHOLE tenant ----------------
        # Walk every subscription's resources; every resource id must be seen exactly
        # once across all subs and the union must equal the DB resource count.
        seen_ids: set[str] = set()
        status, subs_body = _http_get(f"{base_url}/subscriptions")
        assert status == 200, f"GET /subscriptions failed: {status}"
        all_sub_ids = [s["subscriptionId"] for s in subs_body["value"]]
        assert busiest_sub in all_sub_ids, "busiest sub must be served by ARM"
        for sub_id in all_sub_ids:
            url = f"{base_url}/subscriptions/{sub_id}/resources?$top={_PAGE_TOP}"
            pages = 0
            while url is not None:
                status, body = _http_get(url)
                assert status == 200, f"page GET failed ({status}): {url}"
                assert isinstance(body["value"], list)
                for res in body["value"]:
                    assert res["id"] not in seen_ids, f"resource visited twice: {res['id']}"
                    seen_ids.add(res["id"])
                pages += 1
                next_link = body.get("nextLink")
                if next_link is not None:
                    parsed = urlparse(next_link)
                    assert parsed.scheme in ("http", "https"), f"nextLink not absolute: {next_link}"
                    assert parsed.netloc == f"127.0.0.1:{port}", next_link
                url = next_link
                assert pages <= 100_000, "pagination did not terminate (runaway nextLink)"

        # --- (c) p95 latency for list / detail / $filter (D-05c) ---------------------
        list_url = f"{base_url}/subscriptions/{busiest_sub}/resources?$top={_PAGE_TOP}"
        detail_url = f"{base_url}{detail_id}"
        filter_q = quote(f"resourceType eq '{detail_type}'")
        filter_url = (
            f"{base_url}/subscriptions/{busiest_sub}/resources?$filter={filter_q}"
        )
        report["list_p95_ms"] = _p95_latency_ms(list_url)
        report["detail_p95_ms"] = _p95_latency_ms(detail_url)
        report["filter_p95_ms"] = _p95_latency_ms(filter_url)

        # === ASSERTIONS (all numbers already recorded above) =========================
        # (a) correctness at scale.
        assert abs(n_res - _N_RESOURCES) / _N_RESOURCES <= 0.05, (
            f"generated {n_res} resources; target {_N_RESOURCES} (+/-5%)"
        )
        assert n_dangling == 0, f"{n_dangling} dangling dependency endpoints (XSUB-06)"
        assert len(seen_ids) == total_res, (
            f"pagination visited {len(seen_ids)} resources; DB has {total_res} "
            "(full single-visit pagination)"
        )
        assert busiest_count > _PAGE_TOP, (
            "the busiest subscription should span multiple pages for a meaningful walk "
            f"(had {busiest_count}, top={_PAGE_TOP})"
        )

        # (b) generation throughput — GENEROUS gates (catch gross regressions only).
        assert gen_wall_s < _GEN_WALL_CEILING_S, (
            f"generation took {gen_wall_s:.1f}s (ceiling {_GEN_WALL_CEILING_S}s)"
        )
        # Parent-process peak only (workers excluded under jobs>1) — a lower bound,
        # so this catches gross PARENT-side blowups; it is not the whole-run RSS.
        assert parent_peak_bytes < _GEN_PEAK_CEILING_BYTES, (
            f"parent peak memory {parent_peak_bytes / 1024**3:.2f} GiB exceeds the generous ceiling"
        )

        # (b') SPEED-01 MATERIALLY-FASTER gate — assert the --jobs N throughput clears
        #      the data-derived 13-01 Target (>= 4x the 996.1 res/s pure-timing
        #      baseline), NOT merely the generous absolute ceiling above. A miss BELOW
        #      the 3x contingency threshold is the MEASURED signal that justifies the
        #      deferred Rust+rayon generator (a human decision at the checkpoint, never
        #      auto-built). The report is already flushed in `finally`, so the measured
        #      numbers survive even when this assertion fails.
        assert res_per_sec >= _TARGET_RES_PER_SEC, (
            f"throughput {res_per_sec:.0f} res/s at jobs={_EFFECTIVE_JOBS} "
            f"(cpu_count={os.cpu_count()}) missed the materially-faster Target "
            f"{_TARGET_RES_PER_SEC:.0f} res/s (>= {_TARGET_MULTIPLIER:g}x the 13-01 "
            f"baseline {_BASELINE_RES_PER_SEC} res/s). Contingency threshold "
            f"{_CONTINGENCY_RES_PER_SEC:.0f} res/s: "
            + (
                "MISSED -> Rust+rayon contingency justified"
                if res_per_sec < _CONTINGENCY_RES_PER_SEC
                else "cleared (3x-4x band -> re-evaluate, not an auto Rust trigger)"
            )
        )

        # (c) server latency — GENEROUS p95 gates.
        assert report["list_p95_ms"] < _LIST_P95_CEILING_MS, report["list_p95_ms"]
        assert report["detail_p95_ms"] < _DETAIL_P95_CEILING_MS, report["detail_p95_ms"]
        assert report["filter_p95_ms"] < _FILTER_P95_CEILING_MS, report["filter_p95_ms"]

        # NOTE: cross-sub topology (synthetic.dependencies) is intentionally NOT
        # asserted over ARM — it is not ARM-visible (cross-sub-risk tooling only).
    finally:
        # D-06: ALWAYS record the raw numbers BEFORE any assertion can fail the test.
        _write_report(report)
        # T-07-09: ALWAYS tear the server child down deterministically.
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
