#!/usr/bin/env python3
"""Stamp machine-readable synthetic provenance onto a derived profile.

Final step of the bundled-profile chain:

    build_oss_bootstrap_profile.py   ->  profiles/oss-bootstrap.json   (hand-authored)
    tenantless generate --profile .. ->  a synthetic estate in Postgres
    export_estate_duckdb.py          ->  a DuckDB view of that estate
    tenantless analyze duckdb:..     ->  a derived profile
    stamp_synthetic_provenance.py    ->  the same profile + provenance   <-- THIS FILE

The analyzer cannot know whether the estate it just read was real or generated --
it only sees a scan. This step records that answer explicitly, so downstream
consumers (and the release gate in ``tests/test_profile_provenance.py``) can
check it mechanically instead of trusting a filename or a changelog entry.

Records the bootstrap profile's SHA-256, so a later claim that the published
profile came from the committed bootstrap is verifiable rather than asserted.

Run:
    uv run python scripts/stamp_synthetic_provenance.py \
        --profile build/enterprise.json \
        --bootstrap profiles/oss-bootstrap.json \
        --seed 20260726 --cost-as-of 2026-07-01
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


def _write(profile: dict, out: Path) -> str:
    try:
        from tenantless.analyzer.schema_validate import validate_profile

        validate_profile(profile)
        note = "validated against profiles/schema.json"
    except ImportError:
        note = "SCHEMA NOT VALIDATED (tenantless not importable)"
    out.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    return note


def _stamp_hand_authored(profile: dict, args) -> int:
    """Stamp a profile that was written by hand rather than fitted from an estate."""
    prov = dict(profile.get("provenance") or {})
    prov.update(
        {
            "reviewed": True,
            "source": "hand-authored",
            "synthetic": True,
            "derived_from_real_tenant": False,
        }
    )
    profile["provenance"] = prov
    out = args.out or args.profile
    note = _write(profile, out)
    print(f"Stamped {out}: hand-authored, synthetic=True -- {note}.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", required=True, type=Path, help="Derived profile to stamp")
    ap.add_argument("--bootstrap", type=Path, help="Hand-authored bootstrap it was derived from")
    ap.add_argument("--seed", type=int, help="Generator seed used")
    ap.add_argument("--cost-as-of", help="--cost-as-of used (YYYY-MM-DD)")
    ap.add_argument(
        "--hand-authored",
        action="store_true",
        help=(
            "The profile is itself hand-authored, not derived from an estate. "
            "Stamps synthetic provenance with no derivation recipe (there is no "
            "chain to reproduce -- the file IS the root)."
        ),
    )
    ap.add_argument("--out", type=Path, default=None, help="Output (default: in place)")
    args = ap.parse_args()

    profile = json.loads(args.profile.read_text(encoding="utf-8"))

    if args.hand_authored:
        return _stamp_hand_authored(profile, args)

    missing = [
        flag
        for flag, value in (
            ("--bootstrap", args.bootstrap),
            ("--seed", args.seed),
            ("--cost-as-of", args.cost_as_of),
        )
        if value is None
    ]
    if missing:
        print(
            f"Missing {', '.join(missing)} (required unless --hand-authored). "
            "A derived profile without its recipe cannot be certified.",
            file=sys.stderr,
        )
        return 1
    boot_bytes = args.bootstrap.read_bytes()
    boot_sha = hashlib.sha256(boot_bytes).hexdigest()

    boot = json.loads(boot_bytes)
    boot_prov = boot.get("provenance", {})
    if boot_prov.get("source") != "hand-authored":
        print(
            f"{args.bootstrap} does not declare provenance.source == 'hand-authored'. "
            "Refusing to certify a chain whose root is not an authored profile.",
            file=sys.stderr,
        )
        return 1

    stats = profile.get("source_stats", {})
    prov = dict(profile.get("provenance") or {})
    prov.update(
        {
            "reviewed": True,
            "synthetic": True,
            "derived_from_real_tenant": False,
            "derivation": {
                "bootstrap_profile": args.bootstrap.as_posix(),
                "bootstrap_profile_sha256": boot_sha,
                "generator_seed": args.seed,
                "cost_as_of": args.cost_as_of,
                "estate": {
                    "subscriptions": stats.get("total_subscriptions", 0),
                    "resource_groups": stats.get("total_resource_groups", 0),
                    "resources": stats.get("total_resources", 0),
                },
                "steps": [
                    "uv run python scripts/build_oss_bootstrap_profile.py",
                    f"uv run tenantless generate --profile {args.bootstrap.as_posix()} "
                    f"--seed {args.seed} --cost-as-of {args.cost_as_of} --jobs 0 --force",
                    "uv run python scripts/export_estate_duckdb.py --out build/estate.duckdb --force",
                    "uv run tenantless analyze --source duckdb:build/estate.duckdb "
                    "--out build/enterprise.json --allow-no-denylist --non-interactive --k 5",
                    "uv run python scripts/stamp_synthetic_provenance.py ...",
                ],
            },
        }
    )
    profile["provenance"] = prov

    out = args.out or args.profile
    note = _write(profile, out)
    print(
        f"Stamped {out}: synthetic=True, derived_from_real_tenant=False, "
        f"bootstrap={args.bootstrap.as_posix()} sha256={boot_sha[:16]}... -- {note}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
