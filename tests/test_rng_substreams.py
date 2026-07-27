"""SPEED-02 / 13-03 Task 1: the ``SeededContext.from_seed_sequence`` substream
factory — the grep-auditable RNG seam that fans a single seed out into
per-subscription / per-post-pass independent substreams via numpy
``SeedSequence.spawn``.

These pin the four behaviors the parallel pipeline relies on:

* two contexts built from the **same** ``SeedSequence`` draw an identical stream
  (so a worker that re-receives its spawned child reproduces sub *i* exactly,
  regardless of which process runs it);
* contexts built from **different** spawned children are non-correlated (no
  shared stream — 13-RESEARCH Pitfall 3 / "Determinism Architecture" rule 1);
* every legacy method (uuid4 / bernoulli / categorical) behaves identically
  through the factory path;
* the legacy ``SeededContext(seed)`` int constructor is byte-for-byte unchanged
  (the ~9 direct-construction unit tests + the apply-drift CLI path must not move).

RED until rng.py grows ``from_seed_sequence`` (it raises ``AttributeError`` today).
"""

from __future__ import annotations

from numpy.random import SeedSequence

from tenantless.generator.rng import SeededContext


def test_same_seed_sequence_yields_identical_draws():
    """Two contexts from the SAME SeedSequence object → identical draw streams.

    This is the worker-reproducibility guarantee: the spawned child for sub *i*
    is deterministic, so rebuilding a SeededContext from it (in any process)
    reproduces the same uuid4 / bernoulli / categorical sequence.
    """
    ss = SeedSequence(12345)
    a = SeededContext.from_seed_sequence(ss)
    b = SeededContext.from_seed_sequence(ss)

    assert [a.uuid4() for _ in range(8)] == [b.uuid4() for _ in range(8)]
    assert [a.bernoulli(0.5) for _ in range(32)] == [
        b.bernoulli(0.5) for _ in range(32)
    ]
    dist = {"x": 0.5, "y": 0.3, "z": 0.2}
    assert [a.categorical(dist) for _ in range(16)] == [
        b.categorical(dist) for _ in range(16)
    ]
    assert a.faker.name() == b.faker.name()


def test_different_children_are_non_correlated():
    """Two DIFFERENT spawned children of one root → divergent draws (no shared
    stream — 13-RESEARCH Pitfall 3)."""
    c0, c1 = SeedSequence(999).spawn(2)
    a = SeededContext.from_seed_sequence(c0)
    b = SeededContext.from_seed_sequence(c1)

    assert [a.uuid4() for _ in range(8)] != [b.uuid4() for _ in range(8)]
    # Faker is seeded from a STABLE reduction of the substream (rule 5), never a
    # shared int — so the two children carry distinct Faker seeds.
    assert a.seed != b.seed


def test_factory_uuid4_is_v4_shaped():
    """uuid4 still mints a v4-variant UUID from ``rng.bytes(16)`` (rule 6)."""
    u = SeededContext.from_seed_sequence(SeedSequence(7)).uuid4()
    assert u.version == 4
    assert (u.bytes[8] & 0xC0) == 0x80  # RFC-4122 variant


def test_legacy_int_constructor_unchanged():
    """The legacy ``SeededContext(seed)`` path stays byte-identical (back-compat
    for the ~9 direct-construction unit tests + the apply-drift CLI path)."""
    a = SeededContext(42)
    b = SeededContext(42)
    assert [a.uuid4() for _ in range(5)] == [b.uuid4() for _ in range(5)]
    assert a.seed == 42 and b.seed == 42
    # A different int seed still diverges.
    c = SeededContext(43)
    assert SeededContext(42).uuid4() != c.uuid4()
