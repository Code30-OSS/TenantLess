"""SPEED-02 gate: ``generate_tenant(..., jobs=1)`` must be byte-identical to
``jobs=N`` for a fixed seed — including ``tenant_id`` — over the FULL
``GenerationResult`` content fingerprint.

RED until 13-03: ``generate_tenant`` has no ``jobs`` parameter yet, so every
assertion here raises ``TypeError`` today. That RED state is INTENTIONAL and
required — it proves the gate is wired to the real entrypoint before the
substream re-architecture lands. Per TDD the failing test is committed first
(``test(13-02):``); 13-03 turns it GREEN.

The test is a REAL multi-core gate, NOT a 2-job toy (13-RESEARCH "Determinism
Test Design"):
  * worker counts go up to ``os.cpu_count()`` (``generate_tenant`` clamps jobs to
    ``[1, cpu_count]`` — oversubscription is disallowed as a DoS-self guard, so
    completion order is scrambled by ``n_subs >> workers`` + ``chunksize``, NOT by
    running more workers than cores).
  * ``n_subs`` is many multiples of the worker count → workers finish out of
    index order.
  * compares a DB-free sha256 over the full result (never insertion/SERIAL
    order).
  * runs at TWO distinct worker counts to catch scheduling-dependent flakiness.

The contract is ``--jobs 1 == --jobs N`` internal consistency for a seed, NOT
preservation of the OLD single-threaded stream (13-RESEARCH A2): re-architecting
the RNG seam will change the actual byte values a seed produces, and that is
acceptable — only cross-job-count agreement is asserted here.
"""

from __future__ import annotations

import os

from tenantless.generator.pipeline import generate_tenant

from _fingerprint import fingerprint


def _worker_counts() -> tuple[int, int]:
    """Two distinct worker counts within the clamp ``[1, cpu_count]``.

    The high count is all cores; the low count is a different, smaller pool so the
    two runs schedule differently. On a 1-core host both collapse to the
    single-process path (no pool to exercise) — inherent, not a gap.
    """
    cpu = os.cpu_count() or 2
    high = cpu
    low = max(2, cpu // 2)
    if low >= high:  # tiny-core hosts: keep them distinct where possible
        low = max(1, high - 1)
    return high, low


def test_jobs_determinism(generator_profile):
    """jobs=1 fingerprint == jobs=N fingerprint (and equal tenant_id) at two
    distinct worker counts (both within the [1, cpu_count] clamp) — the SPEED-02
    multi-core determinism gate.

    RED until 13-03: ``generate_tenant`` accepts no ``jobs`` kwarg, so the first
    call raises ``TypeError``. 13-03 (per-subscription substream re-architecture)
    makes it GREEN.
    """
    high, low = _worker_counts()
    n_subs = max(4 * high, 40)  # n_subs >> workers → scrambled completion order
    n_resources = 4000

    serial = generate_tenant(
        generator_profile,
        seed=42,
        n_subs=n_subs,
        n_resources=n_resources,
        jobs=1,
    )
    parallel = generate_tenant(
        generator_profile,
        seed=42,
        n_subs=n_subs,
        n_resources=n_resources,
        jobs=high,
    )

    assert fingerprint(serial) == fingerprint(parallel)
    # SPEED-02 names tenant_id explicitly — assert it independently of the digest.
    assert serial.tenant.tenant_id == parallel.tenant.tenant_id

    # Not a 2-job toy: a SECOND, different worker count must also agree, so the
    # gate cannot pass by accident on one particular schedule.
    parallel2 = generate_tenant(
        generator_profile,
        seed=42,
        n_subs=n_subs,
        n_resources=n_resources,
        jobs=low,
    )
    assert fingerprint(serial) == fingerprint(parallel2)
    assert serial.tenant.tenant_id == parallel2.tenant.tenant_id
