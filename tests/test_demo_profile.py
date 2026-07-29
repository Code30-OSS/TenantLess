"""Lock the bundled synthetic ``demo`` profile (Wave 1, D-03).

Two layers:

* UNIT (default suite, DB-free): the ``demo`` alias resolves + schema-validates,
  its provenance is synthetic + carries a reproducible derivation recipe, its
  ``source_stats`` sit at ~50/~5000 (and clear the forbidden real-derived shape),
  its cost / governance / topology planes are non-vacuous, the minimum-aggregation
  floor holds, and the COMMITTED bytes match the fingerprint the build driver
  recorded (deterministic-rebuild lock).

* INTEGRATION (``-m integration``, opt-in, DISPOSABLE DB per D-10): generating
  from ``--profile demo`` exercises EVERY plane the demo must show -- costs, RBAC
  (with over-privilege), topology, governance violations and drift eligibility --
  all non-zero, and the generated estate matches ``demo.json``'s ``source_stats``.

The ``demo`` profile is derived (bootstrap -> generate -> export -> analyze ->
stamp) by ``scripts/build_demo_profile.py``; it is NOT a hand-edited copy of
``small.json`` with cost fields injected.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_DEMO_PATH = _REPO / "src" / "tenantless" / "profiles" / "demo.json"
_SIDECAR = _REPO / "tests" / "demo_profile.sha256"

# The forbidden real-derived estate shape (must never appear in a bundled profile).
_FORBIDDEN_SOURCE_STATS = {
    "total_subscriptions": 399,
    "total_resource_groups": 6753,
    "total_resources": 96093,
}

# The demo command the compose demo (D-04) runs; the integration test mirrors it.
_DEMO_SEED = 42
_DEMO_COST_AS_OF = "2026-01-01"
_MIN_BUCKET_SIZE = 5  # analyzer privacy floor (analyze --min-bucket-size default)


@pytest.fixture(scope="module")
def demo() -> dict:
    return json.loads(_DEMO_PATH.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# UNIT -- alias + provenance + non-vacuity + fingerprint (no DB)
# --------------------------------------------------------------------------- #
def test_demo_registered_in_bundled_names() -> None:
    from tenantless.generator.profile_input import _BUNDLED_NAMES

    assert _BUNDLED_NAMES == ("enterprise", "small", "demo")


def test_resolve_and_load_validates_demo() -> None:
    """resolve_profile('demo') -> bundled resource; load_profile schema-validates."""
    from tenantless.generator.profile_input import load_profile, resolve_profile

    profile = load_profile(resolve_profile("demo"))  # raises on schema drift
    assert profile["source_stats"]["total_subscriptions"] == 50


def test_demo_provenance_is_synthetic_with_recipe(demo) -> None:
    prov = demo["provenance"]
    assert prov["synthetic"] is True
    assert prov["derived_from_real_tenant"] is False
    deriv = prov["derivation"]
    assert deriv["bootstrap_profile"] == "profiles/oss-bootstrap.json"
    assert deriv["generator_seed"] == 424242  # the FIXED derivation seed (D-03)
    assert deriv["cost_as_of"] == "2026-01-01"
    assert len(deriv["bootstrap_profile_sha256"]) == 64


def test_demo_source_stats_band_and_not_forbidden(demo) -> None:
    stats = demo["source_stats"]
    # ~50 subscriptions / ~5000 resources, asserted within a tolerance band.
    assert 45 <= stats["total_subscriptions"] <= 55
    assert 4000 <= stats["total_resources"] <= 6000
    assert stats["total_resource_groups"] > 0
    # Must clear the forbidden real-derived shape by a wide margin.
    assert {k: stats.get(k) for k in _FORBIDDEN_SOURCE_STATS} != _FORBIDDEN_SOURCE_STATS


def test_demo_planes_are_non_vacuous(demo) -> None:
    """The reason demo exists: unlike small.json, every plane carries signal."""
    # Cost -- small.json ships ZERO here; the demo FinOps plane must be populated.
    assert len(demo["cost_distributions"]) > 0
    # Governance violations.
    assert len(demo["governance_violations"]["type_frequencies"]) > 0
    # Topology (cross-subscription dependencies).
    assert demo.get("cross_subscription_dependencies")
    assert len(demo["cross_subscription_dependencies"]) > 0


def test_demo_min_aggregation_floor_holds(demo) -> None:
    """No surviving resource-type bucket sits below the analyzer's privacy floor.

    Frequencies are shares of the estate; ``frequency * total_resources`` is the
    bucket's member count, which must be >= min_bucket_size (merge_min_buckets
    folds anything smaller away before the profile is written)."""
    total = demo["source_stats"]["total_resources"]
    freqs = [v["frequency"] for v in demo["resource_type_distributions"].values()]
    assert freqs, "demo must carry resource-type distributions"
    smallest_count = min(freqs) * total
    assert smallest_count >= _MIN_BUCKET_SIZE - 0.5  # rounding slack


def test_committed_demo_matches_recorded_fingerprint() -> None:
    """The committed bytes match the sha256 the build driver recorded -- the
    deterministic-rebuild lock (a byte drift here means the profile was hand-edited
    or rebuilt outside the pinned canonical builder)."""
    recorded = _SIDECAR.read_text(encoding="utf-8").strip()
    actual = hashlib.sha256(_DEMO_PATH.read_bytes()).hexdigest()
    assert actual == recorded, (
        "committed demo.json sha256 does not match tests/demo_profile.sha256 -- "
        "re-derive via scripts/build_demo_profile.py in the canonical builder."
    )


# --------------------------------------------------------------------------- #
# INTEGRATION -- five non-zero planes for the demo fixture (DISPOSABLE DB, D-10)
# --------------------------------------------------------------------------- #
_ALLOW_TRUNCATE_ENV = "TENANTLESS_E2E_ALLOW_TRUNCATE"


@pytest.mark.integration
def test_demo_estate_exercises_every_plane(pg_conn) -> None:
    """generate --profile demo -> non-zero cost / RBAC(+over-privilege) / topology /
    governance / drift-eligible, and the estate matches demo.json source_stats.

    D-10: this TRUNCATES + rewrites the synthetic schema of whatever ``DATABASE_URL``
    points at. Point it at a DISPOSABLE Postgres (a throwaway postgres:16-alpine on a
    non-5433 port) -- NEVER the :5433 dev tenant. Gated by both the ``integration``
    marker AND ``TENANTLESS_E2E_ALLOW_TRUNCATE=1`` (mirrors test_e2e_pipeline.py)."""
    from tenantless.generator import writer
    from tenantless.generator.pipeline import generate_tenant
    from tenantless.generator.profile_input import (
        load_profile,
        resolve_profile,
        resolve_targets,
    )

    if os.environ.get(_ALLOW_TRUNCATE_ENV) not in ("1", "true", "yes"):
        pytest.skip(
            f"set {_ALLOW_TRUNCATE_ENV}=1 to allow this test to TRUNCATE + rewrite the "
            "synthetic schema (DISPOSABLE DB only, never :5433 -- D-10)"
        )

    db_url = os.environ.get("DATABASE_URL", "")
    # Disposable IDENTITY, not a port heuristic (Wave1 #1). Combined with the
    # TENANTLESS_E2E_ALLOW_TRUNCATE gate above (explicit destructive authorization),
    # this mirrors the build driver's guard: refuse unless the target holds NO
    # synthetic estate, so the truncate/rewrite can never erase a populated database
    # on ANY host or port. The :5433 string check is kept only as a redundant backstop.
    assert ":5433/" not in db_url, "never the :5433 dev tenant (D-10)"
    assert writer.estate_is_empty(pg_conn), (
        "refusing to run the destructive demo-estate integration test against a "
        "database that already holds a synthetic estate -- point DATABASE_URL at a "
        "fresh DISPOSABLE Postgres (Wave1 #1 / D-10)"
    )

    profile = load_profile(resolve_profile("demo"))
    n_subs, n_resources = resolve_targets(profile)

    import datetime as _dt

    result = generate_tenant(
        profile,
        seed=_DEMO_SEED,
        n_subs=n_subs,
        n_resources=n_resources,
        inject_violations=True,
        inject_cross_sub=True,
        cost_granularity="monthly",
        cost_as_of=_dt.date.fromisoformat(_DEMO_COST_AS_OF),
        inject_identity=True,
        over_privilege_rate=0.05,
        jobs=1,
    )

    # --- In-memory plane assertions (exact, from the pipeline result) -----------
    assert len(result.cost_records) > 0, "FinOps plane empty"
    assert len(result.role_assignments) > 0, "RBAC plane empty"
    assert result.over_privilege_count > 0, "no over-privilege injected (RBAC signal)"
    assert len(result.dependencies) > 0, "topology plane empty"
    assert len(result.violations) > 0, "governance plane empty"
    assert len(result.principals) > 0

    # --- Persist to the DISPOSABLE DB, then assert estate shape -----------------
    writer.ensure_base_schema(pg_conn)
    writer.ensure_cost_schema(pg_conn)
    writer.ensure_identity_schema(pg_conn)
    writer.ensure_drift_schema(pg_conn)
    writer.ensure_web_metadata_schema(pg_conn)
    writer.truncate_synthetic(pg_conn)
    writer.write_tenant(
        pg_conn,
        result.tenant,
        dependencies=result.dependencies,
        violations=result.violations,
        cost_records=result.cost_records,
        principals=result.principals,
        role_assignments=result.role_assignments,
    )
    pg_conn.commit()

    stats = profile["source_stats"]
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM synthetic.subscriptions")
        assert cur.fetchone()[0] == stats["total_subscriptions"]
        cur.execute("SELECT count(*) FROM synthetic.resources")
        n_res = cur.fetchone()[0]
    # Targets are approximate (per-RG rounding); the estate tracks source_stats
    # within ~10% -- byte-reproducibility of the fixture at the shape level.
    assert abs(n_res - stats["total_resources"]) / stats["total_resources"] < 0.10

    # Release the read txn before apply-drift's schema preflight takes its lock.
    pg_conn.rollback()

    # --- Drift-eligible plane: apply-drift --dry-run reports a non-zero plan -----
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from tenantless.cli import main; sys.argv[0]='tenantless'; main()",
            "apply-drift",
            "--type",
            "chaos",
            "--seed",
            "42",
            "--dry-run",
            "--database-url",
            db_url,
        ],
        cwd=str(_REPO),
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, f"apply-drift dry-run failed: {proc.stderr}"
    combined = proc.stdout + proc.stderr
    import re

    m = re.search(r"planned\s+(\d+)\s+drift records", combined)
    assert m, f"could not parse planned drift count from: {combined}"
    assert int(m.group(1)) > 0, "drift-eligible plane empty"
