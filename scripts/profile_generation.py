"""cProfile + tracemalloc harness over ``generate_tenant`` (Phase 13, profile-first).

This is the MANDATORY first step of the generation-speed phase: it MEASURES where
the existing sequential generator spends its time, so the "materially faster"
target and the Rust-contingency threshold are set from data, never assumed
(13-RESEARCH "Profiling Protocol", Pitfall 5 — optimizing before profiling).

Stdlib only — ``cProfile`` / ``pstats`` (function-level CPU attribution),
``tracemalloc`` (peak Python allocation), ``time.perf_counter`` (wall clock). NO
``py-spy``, NO new runtime dependency, NO ``pip install`` (13-RESEARCH Package
Legitimacy Audit: the legitimacy surface is empty for the primary path). cProfile
alone is sufficient to attribute the hot loop.

THREE measurements, deliberately kept in SEPARATE runs because each instrument
inflates wall-clock and must NOT pollute the others (measured here: tracemalloc
roughly DOUBLES wall-time, and cProfile's per-call overhead inflates the
millions-of-tiny-calls hot loop most):

  * TIME run (``time.perf_counter`` only, no instrumentation) — the HONEST
    throughput baseline (``gen_wall_s`` / ``res_per_sec``). Downstream plans
    13-03/04/05/06 compare their optimized runs against THIS number, so it must
    carry NEITHER tracemalloc NOR cProfile inflation.
  * MEM run (``tracemalloc``) — ``peak_bytes`` peak Python allocation. Its own
    wall-clock is tracemalloc-inflated and is NOT reported as throughput.
  * PROFILE run (``cProfile``) — function-level attribution (top-N by cumulative
    AND by tottime). Its wall-clock (``prof_wall_s``) is profiling-inflated and is
    NEVER the throughput number; only the relative function costs matter.

DB-FREE: ``generate_tenant`` returns an in-memory frozen ``GenerationResult`` and
never opens a Postgres connection (pipeline.py docstring), so this harness needs
no database, no Docker, and touches no external/untrusted input.

Scales (``--scale``):
  * ``demo`` — 400 subs / 100,000 resources (the landing-page demo scale).
  * ``full`` — 2000 subs / 500,000 resources (the committed scale baseline).
    Run it to get throughput and peak memory for YOUR machine; the committed
    benchmark under ``docs/benchmarks/`` records a reproducible reference run
    against the bundled synthetic profile.

Modes (``--mode``):
  * ``all``     — time, then mem, then profile (default; one invocation prints
    honest throughput + peak memory + both attribution tables, per the plan
    acceptance criteria — three generations, so it is the slow/complete mode).
  * ``time``    — honest throughput only (fastest; no instrumentation).
  * ``mem``     — tracemalloc peak only.
  * ``profile`` — cProfile attribution tables only (wall-clock inflated).

Usage:
    uv run python scripts/profile_generation.py --scale demo
    uv run python scripts/profile_generation.py --scale demo --out scratchpad/baseline-demo.txt
    uv run python scripts/profile_generation.py --scale demo --mode time
    uv run python scripts/profile_generation.py --scale full --top 30 --mode profile
"""

from __future__ import annotations

import cProfile
import io
import pstats
import sys
import time
import tracemalloc
from pathlib import Path

import click

# Bootstrap the in-repo package the same way scripts/bench_arm_latency.py does,
# so this runs from a plain checkout without an editable install.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from tenantless.generator.pipeline import generate_tenant  # noqa: E402
from tenantless.generator.profile_input import (  # noqa: E402
    load_profile,
    resolve_profile,
)

# Fixed, named presets. Index 0 = subs, index 1 = resources.
_SCALES: dict[str, tuple[int, int]] = {
    "demo": (400, 100_000),
    "full": (2000, 500_000),
}

_SEED = 42


def _render_stats(pr: cProfile.Profile, sort: str, top: int) -> str:
    """Return the top-``top`` ``pstats`` table sorted by ``sort`` as text."""
    buf = io.StringIO()
    stats = pstats.Stats(pr, stream=buf)
    stats.sort_stats(sort).print_stats(top)
    return buf.getvalue()


def _count_resources(result) -> int:
    return sum(len(rg.resources) for rg in result.tenant.resource_groups)


def _time_pass(profile, seed: int, n_subs: int, n_resources: int) -> str:
    """Honest throughput: perf_counter ONLY — no tracemalloc, no cProfile."""
    t0 = time.perf_counter()
    result = generate_tenant(profile, seed=seed, n_subs=n_subs, n_resources=n_resources)
    gen_wall_s = time.perf_counter() - t0

    n_res = _count_resources(result)
    res_per_sec = n_res / gen_wall_s if gen_wall_s > 0 else float("nan")
    return (
        "\n===== THROUGHPUT (pure timing — no instrumentation) =====\n"
        f"gen_wall_s   = {gen_wall_s:.3f}\n"
        f"n_res        = {n_res}\n"
        f"res_per_sec  = {res_per_sec:.1f}\n"
        f"n_subs       = {len(result.tenant.subscriptions)}\n"
        f"n_violations = {len(result.violations)}\n"
        f"n_deps       = {len(result.dependencies)}\n"
        f"n_cost_rows  = {len(result.cost_records)}\n"
        f"n_principals = {len(result.principals)}\n"
    )


def _mem_pass(profile, seed: int, n_subs: int, n_resources: int) -> str:
    """Peak allocation via tracemalloc. Its wall-clock is tracemalloc-inflated."""
    tracemalloc.start()
    t0 = time.perf_counter()
    generate_tenant(profile, seed=seed, n_subs=n_subs, n_resources=n_resources)
    mem_wall_s = time.perf_counter() - t0
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return (
        "\n===== PEAK MEMORY (tracemalloc — wall-clock here is inflated) =====\n"
        f"peak_bytes   = {peak_bytes} ({peak_bytes / 1024 / 1024:.1f} MiB)\n"
        f"mem_wall_s   = {mem_wall_s:.3f}  (tracemalloc-inflated, NOT throughput)\n"
    )


def _profile_pass(profile, seed: int, n_subs: int, n_resources: int, top: int) -> str:
    """Function attribution: cProfile only. Wall-clock here is inflated."""
    pr = cProfile.Profile()
    t0 = time.perf_counter()
    pr.enable()
    generate_tenant(profile, seed=seed, n_subs=n_subs, n_resources=n_resources)
    pr.disable()
    prof_wall_s = time.perf_counter() - t0
    return (
        f"\n===== PROFILE WALL (cProfile-inflated — NOT the throughput number) =====\n"
        f"prof_wall_s  = {prof_wall_s:.3f}\n"
        f"\n===== TOP {top} BY CUMULATIVE TIME =====\n"
        + _render_stats(pr, "cumulative", top)
        + f"\n===== TOP {top} BY TOTTIME =====\n"
        + _render_stats(pr, "tottime", top)
    )


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--scale",
    type=click.Choice(sorted(_SCALES)),
    default="demo",
    show_default=True,
    help="Generation scale preset (demo = landing-page scale, full = Phase-7 baseline).",
)
@click.option(
    "--mode",
    type=click.Choice(["all", "time", "mem", "profile"]),
    default="all",
    show_default=True,
    help="time = throughput; mem = tracemalloc peak; profile = cProfile tables; all = every run.",
)
@click.option(
    "--seed",
    default=_SEED,
    show_default=True,
    type=int,
    help="Single seed driving all sampling + Faker (reproducible).",
)
@click.option(
    "--profile",
    "profile_name",
    default="enterprise",
    show_default=True,
    help="Bundled generator profile name (or a path to a profile JSON).",
)
@click.option(
    "--top",
    default=30,
    show_default=True,
    type=click.IntRange(1, None),
    help="Rows per pstats table (top-N by cumulative AND by tottime).",
)
@click.option(
    "--out",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help="Also dump the rendered report (throughput header + both tables) to this file.",
)
def main(
    scale: str, mode: str, seed: int, profile_name: str, top: int, out: str | None
) -> None:
    """Profile a single DB-free ``generate_tenant`` run; print the attribution tables."""
    n_subs, n_resources = _SCALES[scale]
    profile = load_profile(resolve_profile(profile_name))

    click.echo(
        f"# profiling generate_tenant — scale={scale} "
        f"(n_subs={n_subs}, n_resources={n_resources}), seed={seed}, "
        f"profile={profile_name}, mode={mode}",
        err=True,
    )
    click.echo("# DB-free CPU path; cProfile + tracemalloc + perf_counter (stdlib).", err=True)

    report_parts: list[str] = [
        f"# generate_tenant profile — scale={scale} "
        f"(n_subs={n_subs}, n_resources={n_resources}), seed={seed}, profile={profile_name}\n"
    ]

    if mode in ("all", "time"):
        click.echo("# pure-timing throughput run (no instrumentation) ...", err=True)
        report_parts.append(_time_pass(profile, seed, n_subs, n_resources))
    if mode in ("all", "mem"):
        click.echo("# tracemalloc peak-memory run (wall-clock inflated) ...", err=True)
        report_parts.append(_mem_pass(profile, seed, n_subs, n_resources))
    if mode in ("all", "profile"):
        click.echo("# cProfile attribution run (wall-clock inflated) ...", err=True)
        report_parts.append(_profile_pass(profile, seed, n_subs, n_resources, top))

    report = "".join(report_parts)
    click.echo(report)

    if out is not None:
        out_path = Path(out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report, encoding="utf-8")
        click.echo(f"Wrote: {out_path}", err=True)


if __name__ == "__main__":
    main()
