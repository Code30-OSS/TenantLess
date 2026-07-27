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
