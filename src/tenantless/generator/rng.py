"""The single seeded source of randomness for the generator (D-01/D-03).

``SeededContext`` wraps a numpy ``Generator(PCG64(seed))`` and a Faker instance
seeded via ``seed_instance`` (instance-level, never the global). Every other
generator module draws from an injected ``SeededContext`` — there is no bare
``random`` / ``np.random`` / ``Faker()`` anywhere else in the package, which
makes the determinism contract auditable by grep (mirror of the analyzer's
"only reader imports duckdb" seam rule).

Determinism rule, identical to the analyzer's locked contract: **sort keys
before building any probability vector** (Pitfall 3), then **renormalize** the
vector (Pitfall 2) before ``rng.choice``. The same ``seed`` therefore yields
byte-identical draws, including UUIDs (minted from ``rng.bytes(16)`` — Pitfall 4).

Parallel fan-out (Phase 13 / SPEED-02): :meth:`SeededContext.from_seed_sequence`
builds a context from a numpy ``SeedSequence`` substream instead of a bare int,
so the pipeline can ``SeedSequence(seed).spawn(...)`` per-subscription and
per-post-pass independent streams keyed by INDEX (13-RESEARCH "Determinism
Architecture"). The spawn/Faker-seeding logic lives ONLY here — there is still no
bare ``random`` / ``np.random`` / ``Faker()`` anywhere else in the package, so the
determinism seam stays grep-auditable even under concurrency.
"""

from __future__ import annotations

import uuid

import numpy as np
from faker import Faker
from numpy.random import Generator, PCG64, SeedSequence


class SeededContext:
    """One seed (or one ``SeedSequence`` substream) → numpy Generator + seeded
    Faker + sampling helpers."""

    def __init__(self, seed: int):
        self.seed = seed
        self.rng: Generator = Generator(PCG64(seed))
        self.faker = Faker()
        self.faker.seed_instance(seed)  # instance-level, not global (D-03)

    @classmethod
    def from_seed_sequence(cls, ss: SeedSequence) -> "SeededContext":
        """Build a context from a spawned ``SeedSequence`` substream (SPEED-02).

        Two contexts built from the *same* ``ss`` draw an identical stream (so a
        worker that re-receives its spawned child reproduces subscription *i*
        exactly, in any process); contexts from *different* spawned children are
        independent and non-correlated (13-RESEARCH rule 1 / Pitfall 3).

        The Faker instance is seeded from a STABLE reduction of the substream —
        ``int.from_bytes(ss.generate_state(2).tobytes(), "little")`` (rule 5) —
        never a shared int and never the global ``Faker()``. ``uuid4()`` keeps
        minting from ``self.rng.bytes(16)`` (rule 6) so PKs stay reproducible
        per-substream. ``SeedSequence.generate_state`` is a pure function of
        ``ss`` (immutable), so building the ``Generator`` and reducing the Faker
        seed from the same ``ss`` is deterministic and side-effect-free.
        """
        self = cls.__new__(cls)
        faker_seed = int.from_bytes(ss.generate_state(2).tobytes(), "little")
        self.seed = faker_seed
        self.rng = Generator(PCG64(ss))
        self.faker = Faker()
        self.faker.seed_instance(faker_seed)  # instance-level, not global (D-03)
        return self

    def categorical(self, value_to_prob: dict[str, float]) -> str:
        """Draw one key from a ``{value: prob}`` map.

        Sorts items (Pitfall 3: deterministic order) and renormalizes the
        probability vector (Pitfall 2: real archetype weights sum to ~0.998).
        """
        items = sorted(value_to_prob.items())
        vals = [k for k, _ in items]
        probs = np.array([p for _, p in items], dtype=float)
        probs = probs / probs.sum()
        return str(self.rng.choice(vals, p=probs))

    def choice(self, items: list, probs: list[float] | None = None):
        """Draw one element from ``items``.

        Callers are responsible for ordering ``items`` deterministically. When
        ``probs`` is given it is renormalized before the draw (Pitfall 2).
        """
        if probs is None:
            idx = int(self.rng.integers(0, len(items)))
            return items[idx]
        p = np.asarray(probs, dtype=float)
        p = p / p.sum()
        idx = int(self.rng.choice(len(items), p=p))
        return items[idx]

    def trunc_normal(
        self,
        mean: float,
        std: float,
        lo: int | None = None,
        hi: int | None = None,
    ) -> int:
        """Truncated-normal integer sample for ``{mean,std,min,max}`` shapes."""
        x = self.rng.normal(mean, max(std, 1e-9))
        x = round(x)
        if lo is not None:
            x = max(lo, x)
        if hi is not None:
            x = min(hi, x)
        return int(max(0, x))

    def trunc_lognormal(
        self,
        mean: float,
        std: float,
        lo: int | None = None,
        hi: int | None = None,
    ) -> int:
        """Mean-preserving, non-negative, right-skewed integer sample.

        The correct model for a HEAVY-TAILED ``{mean,std,min,max}`` count (a bucket
        whose ``std`` exceeds its ``mean`` — e.g. the ``__misc__`` RG-size bucket,
        std 242 / mean 28.7, a few giant RGs among many small ones). A plain
        ``trunc_normal`` there is wrong twice over: a symmetric normal with that std
        puts ~half its mass below zero (clamped up to ``lo``, which INFLATES the
        realized mean far above ``mean``) and the other half spreads to implausibly
        large values — so the generated total overshoots the target massively and
        the calibrate trim then empties the small RGs. A lognormal matched to the
        target arithmetic ``(mean, std)`` preserves the mean (so the total lands
        near target) while keeping the realistic right skew (most RGs small, a few
        large).
        """
        m = max(float(mean), 1e-9)
        var = max(float(std), 0.0) ** 2
        sigma_sq = float(np.log(1.0 + var / (m * m)))
        sigma = float(np.sqrt(sigma_sq))
        mu = float(np.log(m) - sigma_sq / 2.0)
        x = round(float(self.rng.lognormal(mu, sigma)))
        if lo is not None:
            x = max(lo, x)
        if hi is not None:
            x = min(hi, x)
        return int(max(0, x))

    def bernoulli(self, p: float) -> bool:
        """``True`` with probability ``p`` (clamped to [0, 1])."""
        p = min(1.0, max(0.0, p))
        return bool(self.rng.random() < p)

    def uuid4(self) -> uuid.UUID:
        """Deterministic v4 UUID from the seeded RNG (Pitfall 4).

        The same seed yields the same UUID sequence, so tenant/subscription PKs
        are reproducible (any v4-shaped UUID satisfies the mock).
        """
        b = bytearray(self.rng.bytes(16))
        b[6] = (b[6] & 0x0F) | 0x40  # version 4
        b[8] = (b[8] & 0x3F) | 0x80  # variant
        return uuid.UUID(bytes=bytes(b))
