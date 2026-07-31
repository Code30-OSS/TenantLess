"""Two-layer k-anonymity privacy-floor tests (SEC-PRIV-FLOOR).

The min-aggregation threshold is only a real data boundary if it cannot be
lowered below the k=5 bar. This suite pins BOTH enforcement layers that share a
single ``MIN_BUCKET_FLOOR`` source of truth:

1. CLI layer -- ``analyze --min-bucket-size`` uses ``click.IntRange(min=5)`` so an
   under-floor value is rejected at parse time (exit 2) before any work runs and
   no profile file is written.
2. Programmatic layer -- ``build_profile(min_bucket_size < 5)`` raises
   ``PrivacyFloorError`` as its very first statement, before any read / extraction
   / write (defense in depth for in-process callers that bypass the CLI).

Both layers REJECT (never clamp): an under-floor value never silently proceeds
with a bumped bucket size. The default (5) path is unchanged and stays green.
"""

from __future__ import annotations

import pytest

from tenantless.analyzer.privacy import MIN_BUCKET_FLOOR, PrivacyFloorError
from tenantless.analyzer.profile import build_profile
from tenantless.cli import main

from click.testing import CliRunner


def test_floor_constant_is_five():
    """The single source of truth is the k=5 anonymity bar."""
    assert MIN_BUCKET_FLOOR == 5


# --------------------------------------------------------------------------- #
# Layer 1: CLI IntRange guard (parse-time rejection, exit 2, no file written).
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("under_floor", [4, 1, 0, -1])
def test_cli_rejects_under_floor(tmp_path, under_floor):
    """analyze --min-bucket-size <5 -> exit 2, range error, NO profile written."""
    out_path = tmp_path / "derived.json"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "analyze",
            "--source",
            "duckdb:/nonexistent.duckdb",
            "--out",
            str(out_path),
            "--min-bucket-size",
            str(under_floor),
            "--allow-no-denylist",
        ],
    )
    # IntRange rejection is a Click usage error -> exit code 2.
    assert result.exit_code == 2
    # The error names the offending option / the accepted range.
    assert "min-bucket-size" in result.output
    assert ("5" in result.output) or ("range" in result.output.lower())
    # Rejected at parse time, before build_profile: nothing is written.
    assert not out_path.exists()


def test_cli_accepts_floor_value(fixture_duckdb, tmp_path):
    """analyze --min-bucket-size 5 is accepted and fully succeeds (exit 0)."""
    out_path = tmp_path / "derived.json"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "analyze",
            "--source",
            f"duckdb:{fixture_duckdb}",
            "--out",
            str(out_path),
            "--min-bucket-size",
            "5",
            "--allow-no-denylist",
            "--non-interactive",
        ],
    )
    assert result.exit_code == 0, result.output
    assert out_path.exists()


def test_cli_accepts_above_floor_value(fixture_duckdb, tmp_path):
    """A value above the floor (6) is likewise accepted -- floor is a minimum."""
    out_path = tmp_path / "derived.json"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "analyze",
            "--source",
            f"duckdb:{fixture_duckdb}",
            "--out",
            str(out_path),
            "--min-bucket-size",
            "6",
            "--allow-no-denylist",
            "--non-interactive",
        ],
    )
    assert result.exit_code == 0, result.output
    assert out_path.exists()


# --------------------------------------------------------------------------- #
# Layer 2: build_profile top-of-function guard (defense in depth).
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("under_floor", [4, 1, 0])
def test_build_profile_rejects_under_floor(tmp_path, under_floor):
    """build_profile(<5) raises PrivacyFloorError BEFORE any read/write."""
    out_path = tmp_path / "derived.json"
    with pytest.raises(PrivacyFloorError):
        build_profile(
            source="duckdb:/nonexistent.duckdb",
            out=out_path,
            min_bucket_size=under_floor,
            allow_no_denylist=True,
        )
    # The guard fires before any extraction/write: no output file exists, proving
    # the value was REJECTED, not clamped-and-run.
    assert not out_path.exists()


def test_build_profile_reject_not_clamp(tmp_path):
    """Reject-not-clamp: an under-floor call never returns a profile.

    A silent clamp (bump 4 -> 5 and proceed) would make this pytest.raises fail.
    """
    out_path = tmp_path / "derived.json"
    with pytest.raises(PrivacyFloorError):
        build_profile(
            source=f"duckdb:{tmp_path / 'fixture.duckdb'}",
            out=out_path,
            min_bucket_size=4,
            allow_no_denylist=True,
        )


def test_build_profile_accepts_floor_value(fixture_duckdb, tmp_path):
    """build_profile(min_bucket_size=5) proceeds normally (happy-path regression)."""
    out_path = tmp_path / "derived.json"
    profile = build_profile(
        source=f"duckdb:{fixture_duckdb}",
        out=out_path,
        min_bucket_size=5,
        allow_no_denylist=True,
    )
    assert isinstance(profile, dict)
    assert out_path.exists()


# --------------------------------------------------------------------------- #
# Layer 2 hardening: NON-INTEGER inputs must fail closed (SEC-PRIV-FLOOR-2).
#
# A bare ``min_bucket_size < MIN_BUCKET_FLOOR`` is NOT a sufficient gate:
#   * ``float('nan') < 5`` is False (every NaN comparison is False), so NaN would
#     SAIL PAST the guard -- and downstream ``count < NaN`` is also False, so a
#     unique real bucket (count 1) survives into the "synthetic" profile.
#   * ``float('inf') < 5`` / ``5.0 < 5`` are False too (floats bypass).
#   * ``"5" < 5`` / ``None < 5`` raise TypeError -- an UNCONTROLLED failure, not a
#     fail-closed PrivacyFloorError.
# The guard must therefore require a GENUINE ``int`` (rejecting ``bool``, which is
# an int subclass) BEFORE the ordering comparison.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bad",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        5.0,  # right value, WRONG type
        6.0,
        "5",  # string
        None,
        True,  # bool is an int subclass -> must be rejected explicitly
        False,
    ],
)
def test_build_profile_rejects_non_integer(tmp_path, bad):
    """A non-integer min_bucket_size fails closed with PrivacyFloorError, no I/O.

    NaN is the sharp case the CLI IntRange never sees but an in-process caller can
    pass: it must be REJECTED, not admitted by a permissive ordering comparison.
    """
    out_path = tmp_path / "derived.json"
    with pytest.raises(PrivacyFloorError):
        build_profile(
            source="duckdb:/nonexistent.duckdb",
            out=out_path,
            min_bucket_size=bad,  # type: ignore[arg-type]
            allow_no_denylist=True,
        )
    # Rejected before any read/extraction/write.
    assert not out_path.exists()


def test_build_profile_rejects_int_subclass(tmp_path):
    """An int SUBCLASS that lies about ordering must NOT bypass the floor.

    ``isinstance(x, int)`` is True for subclasses, so a subclass overriding
    ``__lt__`` to return False would pass an isinstance-based gate AND the
    ``x < MIN_BUCKET_FLOOR`` comparison (its own ``__lt__`` runs), then survive
    as a count-1 bucket downstream. Exact-type identity (``type(x) is int``) is
    required to reject it fail-closed.
    """

    class SneakyInt(int):
        def __lt__(self, other):  # always claims "not below the floor"
            return False

    out_path = tmp_path / "derived.json"
    with pytest.raises(PrivacyFloorError):
        build_profile(
            source="duckdb:/nonexistent.duckdb",
            out=out_path,
            min_bucket_size=SneakyInt(1),  # type: ignore[arg-type]
            allow_no_denylist=True,
        )
    assert not out_path.exists()


def test_defaults_are_tied_to_the_floor_constant():
    """P3: both defaults ARE ``MIN_BUCKET_FLOOR`` -- not drifting literals.

    A literal ``5`` passes today but silently drifts (and starts failing) if the
    floor constant ever changes; binding the equality to the constant catches that.
    """
    import inspect

    assert (
        inspect.signature(build_profile).parameters["min_bucket_size"].default
        == MIN_BUCKET_FLOOR
    )
    analyze_cmd = main.commands["analyze"]
    opt = next(p for p in analyze_cmd.params if p.name == "min_bucket_size")
    assert opt.default == MIN_BUCKET_FLOOR
