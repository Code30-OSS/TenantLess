#!/usr/bin/env python3
"""Deterministic build driver for the bundled synthetic ``demo`` profile (D-03).

WHY THIS FILE EXISTS
====================
The public adoption demo must open with EVERY plane populated -- costs, RBAC,
topology, governance violations and drift eligibility. The bundled ``small``
profile carries ZERO cost distributions, so a demo on ``small`` shows an empty
FinOps plane. This driver derives a NEW bundled ``demo`` profile through the
same reproducible chain the ``enterprise`` profile rests on -- it is NOT a
hand-edited copy of ``small.json`` with cost fields injected. Credibility is in
the chain:

    profiles/oss-bootstrap.json        (hand-authored, synthetic)
      -> tenantless init-db            (provision synthetic schema 001..007)
      -> tenantless generate           (a synthetic estate in a DISPOSABLE PG)
      -> export_estate_duckdb.py       (a DuckDB view of that estate)
      -> tenantless analyze            (fit build/demo.json from the estate)
      -> stamp_synthetic_provenance.py (record synthetic provenance + recipe)
      -> src/tenantless/profiles/demo.json   (+ a sha256 fingerprint sidecar)

Determinism (D-11)
------------------
Run inside the PINNED canonical builder image with single-thread numerical
libraries (``OMP_NUM_THREADS=OPENBLAS_NUM_THREADS=MKL_NUM_THREADS=1``):

    Byte-identical rebuilds are guaranteed in the pinned canonical builder image;
    cross-platform builds must remain schema-valid and semantically equivalent
    but are not promised byte-identical.

Disposable database (D-10)
--------------------------
Every run targets a DISPOSABLE Postgres via ``DATABASE_URL`` (a throwaway
``postgres:16-alpine`` on a non-5433 port, or a supplied
``TENANTLESS_BUILD_DATABASE_URL``). The persistent ``:5433`` development tenant
is NEVER touched -- this driver refuses to run against port 5433.

Run:
    DATABASE_URL=postgres://tenantless:tenantless_dev@localhost:55432/tenantless \
    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 \
    uv run python scripts/build_demo_profile.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

# --- Fixed derivation constants (D-03). Pinning these is what makes the
#     (profile, seed, cost-as-of) fingerprint stable and rebuildable. ---
DEMO_SEED = 424242
DEMO_COST_AS_OF = "2026-01-01"
DEMO_SUBSCRIPTIONS = 50
DEMO_RESOURCES = 5000
DEMO_K = 5
# The analyzer stamps ``extracted_at`` with wall-clock time, which alone would
# defeat a byte-reproducible rebuild. Pin it to a fixed derivation anchor so the
# fingerprint depends ONLY on (profile, seed, cost-as-of) -- an honest, constant
# derivation timestamp, never a moving now().
DEMO_EXTRACTED_AT = "2026-01-01T00:00:00Z"

REPO = Path(__file__).resolve().parents[1]

# All chain artifacts are addressed with REPO-RELATIVE paths (subprocesses run
# with cwd=REPO). This keeps recorded provenance portable and machine-path-clean
# -- an absolute path here would leak the builder's filesystem into demo.json's
# provenance and trip the local-path scrub gate.
BOOTSTRAP_REL = "profiles/oss-bootstrap.json"
ESTATE_DUCKDB_REL = "build/demo-estate.duckdb"
BUILD_PROFILE_REL = "build/demo.json"

BUILD_DIR = REPO / "build"
BUILD_PROFILE = REPO / BUILD_PROFILE_REL
BUNDLED_PROFILE = REPO / "src" / "tenantless" / "profiles" / "demo.json"
FINGERPRINT_SIDECAR = REPO / "tests" / "demo_profile.sha256"

EXPORT_SCRIPT = REPO / "scripts" / "export_estate_duckdb.py"
STAMP_SCRIPT = REPO / "scripts" / "stamp_synthetic_provenance.py"

# The forbidden real-derived estate shape (must never appear in a bundled profile).
FORBIDDEN_SOURCE_STATS = {
    "total_subscriptions": 399,
    "total_resource_groups": 6753,
    "total_resources": 96093,
}


def _require_disposable_db() -> str:
    """Return the disposable ``DATABASE_URL``, refusing the :5433 dev tenant (D-10)."""
    db_url = os.environ.get("TENANTLESS_BUILD_DATABASE_URL") or os.environ.get(
        "DATABASE_URL"
    )
    if not db_url:
        sys.exit(
            "DATABASE_URL (or TENANTLESS_BUILD_DATABASE_URL) is not set. Point it at a "
            "DISPOSABLE Postgres (a throwaway postgres:16-alpine on a non-5433 port). "
            "The persistent :5433 dev tenant must never be used for derivation (D-10)."
        )
    port = urlsplit(db_url).port
    if port == 5433:
        sys.exit(
            "Refusing to derive against port 5433 -- that is the persistent dev "
            "tenant, which generate/truncate would destroy (D-10). Use a disposable DB."
        )
    return db_url


def _cli(env: dict, *args: str) -> None:
    """Invoke the tenantless CLI in a child interpreter (no __main__ guard on the
    module, so bootstrap the click group directly). Reuses the shipped commands
    verbatim -- nothing is reimplemented here."""
    cmd = [
        sys.executable,
        "-c",
        "import sys; from tenantless.cli import main; sys.argv[0]='tenantless'; main()",
        *args,
    ]
    subprocess.run(cmd, check=True, cwd=str(REPO), env=env)


def _script(env: dict, script: Path, *args: str) -> None:
    subprocess.run(
        [sys.executable, str(script), *args], check=True, cwd=str(REPO), env=env
    )


def _finalize_profile(seed: int, cost_as_of: str) -> None:
    """Make the stamped profile byte-reproducible and its recipe honest:

    * pin ``extracted_at`` to the fixed derivation anchor (the analyzer stamps
      wall-clock time, which would otherwise change every rebuild);
    * rewrite ``derivation.steps`` to the EXACT demo commands this driver ran (the
      shared stamp script emits a generic enterprise-flavoured recipe).

    Re-serialised byte-for-byte the same way the stamp script writes, preserving
    determinism."""
    profile = json.loads(BUILD_PROFILE.read_text(encoding="utf-8"))
    profile["extracted_at"] = DEMO_EXTRACTED_AT
    profile["provenance"]["derivation"]["steps"] = [
        "uv run python scripts/build_demo_profile.py",
        "  (1) tenantless init-db",
        f"  (2) tenantless generate --profile profiles/oss-bootstrap.json "
        f"--subscriptions {DEMO_SUBSCRIPTIONS} --resources {DEMO_RESOURCES} "
        f"--seed {seed} --cost-as-of {cost_as_of} --jobs 1 --force",
        "  (3) python scripts/export_estate_duckdb.py --out build/demo-estate.duckdb --force",
        "  (4) tenantless analyze --source duckdb:build/demo-estate.duckdb "
        f"--out build/demo.json --allow-no-denylist --non-interactive --k {DEMO_K}",
        "  (5) python scripts/stamp_synthetic_provenance.py --profile build/demo.json "
        "--bootstrap profiles/oss-bootstrap.json "
        f"--seed {seed} --cost-as-of {cost_as_of}",
    ]
    # Canonicalise with sort_keys: the analyzer builds some maps (e.g.
    # tag_distributions) in a dict-insertion order that varies run-to-run even
    # though the VALUES are byte-identical. Sorting keys at the single
    # serialization point makes the committed bytes depend only on the data, so
    # two canonical-builder rebuilds are byte-identical. Key order is semantically
    # irrelevant to every consumer (load_profile -> schema-validate -> generate).
    BUILD_PROFILE.write_text(
        json.dumps(profile, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build() -> int:
    db_url = _require_disposable_db()
    host = urlsplit(db_url).hostname
    port = urlsplit(db_url).port
    print(f"[demo-build] disposable DB: {host}:{port} (never :5433)")

    # Child processes inherit a copy of the environment with the disposable DSN
    # and single-thread numerical settings pinned (D-11).
    env = dict(os.environ)
    env["DATABASE_URL"] = db_url
    env.setdefault("OMP_NUM_THREADS", "1")
    env.setdefault("OPENBLAS_NUM_THREADS", "1")
    env.setdefault("MKL_NUM_THREADS", "1")
    env["PYTHONHASHSEED"] = "0"

    BUILD_DIR.mkdir(parents=True, exist_ok=True)

    # (1) Provision synthetic schema 001..007 (idempotent). init-db is the ONLY
    # path that ensures 006_drift (the drift_deleted_at column export.py filters
    # on); a bare generate provisions 001..005,007 but not 006.
    print("[demo-build] (1) init-db -- provision synthetic schema 001..007")
    _cli(env, "init-db")

    # (2) Generate the synthetic estate into the disposable DB.
    print(
        f"[demo-build] (2) generate {DEMO_SUBSCRIPTIONS} subs / {DEMO_RESOURCES} "
        f"resources, seed={DEMO_SEED}, cost-as-of={DEMO_COST_AS_OF}, jobs=1"
    )
    _cli(
        env,
        "generate",
        "--profile",
        BOOTSTRAP_REL,
        "--subscriptions",
        str(DEMO_SUBSCRIPTIONS),
        "--resources",
        str(DEMO_RESOURCES),
        "--seed",
        str(DEMO_SEED),
        "--cost-as-of",
        DEMO_COST_AS_OF,
        "--jobs",
        "1",
        "--force",
    )

    # (3) Export the estate to a DuckDB scan.
    print("[demo-build] (3) export estate -> build/demo-estate.duckdb")
    _script(env, EXPORT_SCRIPT, "--out", ESTATE_DUCKDB_REL, "--force")

    # (4) Analyze the scan back into a statistical profile.
    print("[demo-build] (4) analyze scan -> build/demo.json")
    _cli(
        env,
        "analyze",
        "--source",
        f"duckdb:{ESTATE_DUCKDB_REL}",
        "--out",
        BUILD_PROFILE_REL,
        "--allow-no-denylist",
        "--non-interactive",
        "--k",
        str(DEMO_K),
    )

    # (5) Stamp synthetic provenance + the reproducible recipe.
    print("[demo-build] (5) stamp synthetic provenance")
    _script(
        env,
        STAMP_SCRIPT,
        "--profile",
        BUILD_PROFILE_REL,
        "--bootstrap",
        BOOTSTRAP_REL,
        "--seed",
        str(DEMO_SEED),
        "--cost-as-of",
        DEMO_COST_AS_OF,
    )
    _finalize_profile(DEMO_SEED, DEMO_COST_AS_OF)

    # Guard: the derived shape must clear the forbidden real-derived estate shape.
    derived = json.loads(BUILD_PROFILE.read_text(encoding="utf-8"))
    if derived.get("source_stats") == FORBIDDEN_SOURCE_STATS:
        sys.exit("Derived source_stats matches the forbidden real-derived shape -- STOP.")

    # (6) Publish into the bundled profiles dir + emit the fingerprint.
    BUNDLED_PROFILE.write_bytes(BUILD_PROFILE.read_bytes())
    digest = _sha256(BUNDLED_PROFILE)
    FINGERPRINT_SIDECAR.write_text(digest + "\n", encoding="utf-8")

    stats = derived["source_stats"]
    print("[demo-build] DONE")
    print(
        f"[demo-build]   subscriptions={stats['total_subscriptions']} "
        f"resource_groups={stats['total_resource_groups']} "
        f"resources={stats['total_resources']}"
    )
    print(f"[demo-build]   bundled profile: {BUNDLED_PROFILE.relative_to(REPO).as_posix()}")
    print(f"[demo-build]   sha256 fingerprint: {digest}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Deterministically derive the bundled synthetic `demo` profile "
            "(bootstrap -> generate -> export -> analyze -> stamp) against a "
            "DISPOSABLE Postgres (D-10), inside the pinned canonical builder (D-11)."
        )
    )
    ap.add_argument(
        "--print-fingerprint",
        action="store_true",
        help="Print the sha256 of the CURRENTLY committed demo.json and exit (no build).",
    )
    args = ap.parse_args()

    if args.print_fingerprint:
        if not BUNDLED_PROFILE.is_file():
            sys.exit(f"{BUNDLED_PROFILE} does not exist yet -- run the build first.")
        print(_sha256(BUNDLED_PROFILE))
        return 0

    return build()


if __name__ == "__main__":
    raise SystemExit(main())
