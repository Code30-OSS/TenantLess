"""Guards for the parallel-generation seams (13 post-review fixes):

* the in-process path must NOT mutate the module-level worker globals in the
  parent — otherwise two concurrent in-process ``generate_tenant`` calls clobber
  each other's ``_WORKER_PROFILE`` / ``_WORKER_TENANT_ID`` (concurrent serial-run
  contamination);
* ``generate_tenant`` must clamp ``jobs`` to ``[1, cpu_count]`` itself, so a
  direct caller (test, benchmark env, public API) can never spawn an unbounded
  process pool (Security V5 DoS-self, T-13-05-DOS).
"""

from __future__ import annotations

import concurrent.futures
import os
import threading

from tenantless.generator import pipeline
from tenantless.generator.pipeline import generate_tenant


def test_inprocess_run_does_not_pollute_parent_globals(generator_profile):
    """A jobs<=1 (in-process) run leaves the parent's worker globals untouched.

    The globals exist ONLY for the ProcessPoolExecutor initializer to seed worker
    PROCESSES; the in-process path passes profile/tenant_id explicitly. If the
    parent globals get set here, concurrent in-process runs would share — and
    corrupt — that state.
    """
    pipeline._init_worker  # noqa: B018 - referenced for clarity
    # Pre-state: not set (or whatever a prior pool run left — force a known base).
    pipeline._WORKER_PROFILE = None
    pipeline._WORKER_TENANT_ID = None

    generate_tenant(generator_profile, seed=7, n_subs=6, n_resources=300, jobs=1)

    assert pipeline._WORKER_PROFILE is None, (
        "in-process generate_tenant mutated the parent _WORKER_PROFILE global"
    )
    assert pipeline._WORKER_TENANT_ID is None, (
        "in-process generate_tenant mutated the parent _WORKER_TENANT_ID global"
    )


def test_concurrent_inprocess_runs_are_internally_consistent(generator_profile):
    """Two in-process runs racing in threads each stay self-consistent.

    With the old shared-global design, thread A's worker could read thread B's
    ``_WORKER_TENANT_ID`` mid-flight and stamp A's subscriptions with B's tenant.
    The fix (explicit args, no parent-global mutation) makes each run independent:
    every subscription a run produces must carry THAT run's tenant_id.
    """
    results: dict[int, object] = {}
    barrier = threading.Barrier(2)

    def run(seed: int) -> None:
        barrier.wait()  # maximise overlap
        results[seed] = generate_tenant(
            generator_profile, seed=seed, n_subs=12, n_resources=600, jobs=1
        )

    threads = [threading.Thread(target=run, args=(s,)) for s in (101, 202)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for seed, res in results.items():
        tid = res.tenant.tenant_id
        assert all(s.tenant_id == tid for s in res.tenant.subscriptions), (
            f"seed {seed}: a subscription carries a foreign tenant_id "
            "(concurrent in-process contamination)"
        )
    # The two seeds must still produce distinct tenants (no collapse to one run).
    assert results[101].tenant.tenant_id != results[202].tenant.tenant_id


class _RecordingExecutor:
    """Stand-in for ProcessPoolExecutor that records max_workers and runs the
    mapped tasks inline (no real processes), so the clamp can be asserted cheaply.
    """

    captured_max_workers: int | None = None

    def __init__(self, *, max_workers, initializer=None, initargs=()):
        type(self).captured_max_workers = max_workers
        if initializer is not None:
            initializer(*initargs)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def map(self, fn, *iterables, chunksize=1):
        return [fn(*args) for args in zip(*iterables)]


def test_jobs_clamped_to_cpu_count(generator_profile, monkeypatch):
    """A pathological ``jobs`` never reaches ProcessPoolExecutor unclamped."""
    cpu = os.cpu_count() or 1
    monkeypatch.setattr(pipeline, "ProcessPoolExecutor", _RecordingExecutor)

    # Huge request must be pinned to cpu_count, never spawned as-is.
    _RecordingExecutor.captured_max_workers = None
    generate_tenant(generator_profile, seed=1, n_subs=8, n_resources=400, jobs=10_000)
    assert _RecordingExecutor.captured_max_workers == cpu

    # jobs=0 means "all cores" — also exactly cpu_count.
    _RecordingExecutor.captured_max_workers = None
    generate_tenant(generator_profile, seed=1, n_subs=8, n_resources=400, jobs=0)
    assert _RecordingExecutor.captured_max_workers == cpu


def test_negative_jobs_collapse_to_single_process(generator_profile, monkeypatch):
    """A negative ``jobs`` must NOT open a pool — it falls to the in-process path."""
    monkeypatch.setattr(pipeline, "ProcessPoolExecutor", _RecordingExecutor)
    _RecordingExecutor.captured_max_workers = None
    generate_tenant(generator_profile, seed=1, n_subs=8, n_resources=400, jobs=-4)
    assert _RecordingExecutor.captured_max_workers is None  # pool never constructed
