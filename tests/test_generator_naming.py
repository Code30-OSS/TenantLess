"""ARCH-03 RG-naming behavior: the workload token is the injected archetype
label (not a random ``_WORKLOADS`` draw), while ``bu``/``env`` stay random and a
fixed ``(ctx, workload)`` is byte-reproducible.

DB-free/RNG-seeded: every ``SeededContext`` is built from a plain ``SeedSequence``
(mirrors ``tests/test_generator_misc_types.py::_ctx``), so no Postgres is touched.
"""

from __future__ import annotations

import re

from numpy.random import SeedSequence

from collections import Counter

from tenantless.generator import archetypes, naming
from tenantless.generator.rng import SeededContext

# Grammar: rg-{bu}-{env}-{token}-{nn}. bu/env are single lowercase words; the
# archetype token may itself contain hyphens (e.g. ``web-app``), so the workload
# segment is ``[a-z-]+``.
_RG_GRAMMAR = re.compile(r"^rg-[a-z]+-[a-z]+-[a-z-]+-\d{2}$")


def _ctx(seed: int = 0) -> SeededContext:
    return SeededContext.from_seed_sequence(SeedSequence(seed))


def test_grammar():
    """A generated name matches the rg grammar and ends in ``-{token}-NN``
    with the hyphenated token preserved and stays under the 90-char limit."""
    name = naming.resource_group_name(_ctx(1), workload="data-platform")
    assert _RG_GRAMMAR.match(name), name
    assert re.search(r"-data-platform-\d{2}$", name), name
    assert len(name) < 90


def test_token_injected():
    """The passed workload token is used verbatim as the workload segment."""
    name = naming.resource_group_name(_ctx(7), workload="backup")
    # Strip the ``rg-`` prefix and ``-NN`` suffix; bu/env are single words, so
    # the remainder after the first two segments is the token verbatim.
    core = name[len("rg-") :]
    core = re.sub(r"-\d{2}$", "", core)
    _bu, _env, token = core.split("-", 2)
    assert token == "backup", name


def test_bu_env_still_vary():
    """Across many seeds with a FIXED workload, the bu and env segments each
    take multiple distinct values — they remain independent random draws."""
    bus: set[str] = set()
    envs: set[str] = set()
    for seed in range(60):
        name = naming.resource_group_name(_ctx(seed), workload="web-app")
        core = re.sub(r"-\d{2}$", "", name[len("rg-") :])
        bu, env, _token = core.split("-", 2)
        bus.add(bu)
        envs.add(env)
    assert len(bus) > 1, bus
    assert len(envs) > 1, envs


def test_deterministic():
    """Two calls with the same seed + same workload return the identical name."""
    a = naming.resource_group_name(_ctx(42), workload="monitoring")
    b = naming.resource_group_name(_ctx(42), workload="monitoring")
    assert a == b


def test_no_workloads_draw(monkeypatch):
    """With ``workload`` supplied, the ``_WORKLOADS`` vocab is never consulted —
    a sentinel value patched into ``_WORKLOADS`` never appears in the output."""
    monkeypatch.setattr(naming, "_WORKLOADS", ("zzsentinelzz",))
    name = naming.resource_group_name(_ctx(3), workload="identity")
    assert "zzsentinelzz" not in name
    assert "-identity-" in name


# --------------------------------------------------------------------------- #
# D-13: archetype→RG-count coverage line rendering (pure, DB-free helper).
# --------------------------------------------------------------------------- #


def test_render_coverage_line_sorted():
    """token=count pairs render sorted by count DESCENDING, including the
    generic shared/core tokens, prefixed ``archetypes:``."""
    counter = Counter(
        {"shared": 1130, "monitoring": 214, "backup": 91, "core": 260}
    )
    line = archetypes.render_coverage_line(counter)
    assert line == "archetypes: shared=1130 core=260 monitoring=214 backup=91"
    # generic tokens are included, not filtered out.
    assert "shared=1130" in line
    assert "core=260" in line


def test_render_coverage_line_deterministic():
    """The same counter renders byte-identically on repeat calls; ties are
    broken by token name so the order is stable."""
    counter = Counter({"backup": 5, "web-app": 5, "shared": 5})
    a = archetypes.render_coverage_line(counter)
    b = archetypes.render_coverage_line(counter)
    assert a == b
    # equal counts → ascending token-name tiebreak (backup, shared, web-app).
    assert a == "archetypes: backup=5 shared=5 web-app=5"


def test_render_coverage_line_empty():
    """An empty counter renders a well-formed line and never raises."""
    line = archetypes.render_coverage_line(Counter())
    assert line == "archetypes: (none)"


def test_render_rg_naming_line_reports_d18_metrics():
    """D-18: the confirm-and-rename tally renders as ONE compact line carrying
    confirmed / downgraded_to_generic / child_credit_confirmed integer counts."""
    metrics = {
        "confirmed": 412,
        "downgraded_to_generic": 87,
        "child_credit_confirmed": 33,
        "already_generic": 260,
    }
    line = archetypes.render_rg_naming_line(metrics)
    assert line == (
        "rg-naming: confirmed=412 downgraded_to_generic=87 "
        "child_credit_confirmed=33"
    )
    assert "confirmed=" in line
    assert "downgraded_to_generic=" in line
    assert "child_credit_confirmed=" in line


def test_render_rg_naming_line_missing_keys_default_zero():
    """A partial/empty tally renders zeros rather than raising — the summary
    line must never break a generate run."""
    assert archetypes.render_rg_naming_line({}) == (
        "rg-naming: confirmed=0 downgraded_to_generic=0 child_credit_confirmed=0"
    )
    assert "downgraded_to_generic=0" in archetypes.render_rg_naming_line(
        {"confirmed": 5}
    )


def test_render_rg_naming_line_deterministic():
    """The same tally renders byte-identically on repeat calls (fixed field
    order — never dict-iteration order)."""
    metrics = {"child_credit_confirmed": 1, "confirmed": 2, "downgraded_to_generic": 3}
    a = archetypes.render_rg_naming_line(metrics)
    b = archetypes.render_rg_naming_line(metrics)
    assert a == b
    assert a == (
        "rg-naming: confirmed=2 downgraded_to_generic=3 child_credit_confirmed=1"
    )
