"""Centerpiece acceptance test: schema conformance.

build_profile over the synthetic fixture DuckDB produces JSON that validates
against profiles/schema.json with ZERO errors -- including that every
resource_type_distributions entry carries a property_distributions object and
extracted_at is a valid RFC3339 date-time. Runs in CI with NO dependency on the
real DB.
"""

from __future__ import annotations

import json
from pathlib import Path

import orjson

from tenantless.analyzer import schema_validate
from tenantless.analyzer.profile import build_profile

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_build_profile_over_fixture_validates_against_schema(fixture_duckdb, tmp_path):
    out = tmp_path / "derived.json"
    profile = build_profile(
        source=f"duckdb:{fixture_duckdb}",
        out=out,
        min_bucket_size=5,
        allow_no_denylist=True,
    )

    # In-memory dict validates.
    schema_validate.validate_profile(profile)

    # The written file validates too (round-trip through orjson on disk).
    on_disk = orjson.loads(out.read_bytes())
    schema_validate.validate_profile(on_disk)

    # Every type entry carries property_distributions; extracted_at is a date-time.
    rtd = on_disk["resource_type_distributions"]
    assert rtd
    assert all("property_distributions" in v for v in rtd.values())
    assert on_disk["extracted_at"].endswith("Z")


def test_committed_sample_profile_validates():
    """The committed synthetic profiles/sample-profile.json validates."""
    path = REPO_ROOT / "profiles" / "sample-profile.json"
    assert path.exists(), "profiles/sample-profile.json must be committed"
    with path.open("r", encoding="utf-8") as fh:
        profile = json.load(fh)
    schema_validate.validate_profile(profile)


# --------------------------------------------------------------------------- #
# cost_distributions parameter guards: a schema-valid profile must never crash
# the generator. `generate` validates the profile (via load_profile) before
# generating, so a cost distribution whose parameters would make numpy's
# rng.gamma()/lognormal() raise must be rejected up front here, not at draw time.
# --------------------------------------------------------------------------- #

import copy  # noqa: E402

import pytest  # noqa: E402
from jsonschema.exceptions import ValidationError  # noqa: E402


def _valid_profile_with_cost(entry: dict) -> dict:
    """A known-valid committed profile with one cost_distributions entry added."""
    with (REPO_ROOT / "profiles" / "sample-profile.json").open(encoding="utf-8") as fh:
        profile = json.load(fh)
    profile = copy.deepcopy(profile)
    profile["version"] = "1.2"  # cost_distributions is the v1.2 additive section
    profile["cost_distributions"] = {"Microsoft.Compute/virtualMachines": entry}
    return profile


def test_gamma_cost_distribution_accepts_positive_shape_and_scale():
    """A well-formed gamma fit (strictly-positive shape/scale) still validates."""
    schema_validate.validate_profile(
        _valid_profile_with_cost(
            {"distribution": "gamma", "shape": 2.0, "scale": 1.5, "sample_count": 42}
        )
    )


def test_lognormal_cost_distribution_accepts_valid_params():
    schema_validate.validate_profile(
        _valid_profile_with_cost(
            {"distribution": "lognormal", "mu": 3.0, "sigma": 1.2, "sample_count": 42}
        )
    )


@pytest.mark.parametrize("bad", [-1.0, 0.0])
def test_gamma_shape_must_be_strictly_positive(bad):
    """numpy rng.gamma(shape<=0) raises ValueError; the schema rejects it first."""
    with pytest.raises(ValidationError):
        schema_validate.validate_profile(
            _valid_profile_with_cost({"distribution": "gamma", "shape": bad, "scale": 1.5})
        )


@pytest.mark.parametrize("bad", [-2.0, 0.0])
def test_gamma_scale_must_be_strictly_positive(bad):
    with pytest.raises(ValidationError):
        schema_validate.validate_profile(
            _valid_profile_with_cost({"distribution": "gamma", "shape": 2.0, "scale": bad})
        )


def test_gamma_requires_shape_and_scale():
    """A gamma distribution missing shape/scale would silently draw with defaults."""
    with pytest.raises(ValidationError):
        schema_validate.validate_profile(
            _valid_profile_with_cost({"distribution": "gamma", "mu": 1.0})
        )


def test_lognormal_requires_mu_and_sigma():
    with pytest.raises(ValidationError):
        schema_validate.validate_profile(
            _valid_profile_with_cost({"distribution": "lognormal", "shape": 2.0})
        )
