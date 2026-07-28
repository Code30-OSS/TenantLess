#!/usr/bin/env python3
"""Assert the ACTUAL seed-42 demo estate — identity, counts, and served planes.

This is the native-amd64 end-to-end gate's assertion (Wave 3 / D-02 / D-14). It runs
AFTER the generator has seeded a demo estate and the mock-server is serving it, and it
FAILS the job on ANY mismatch. A fingerprint or count mismatch on a CI amd64 runner is a
DEFECT to investigate — NEVER a new expected value. Do not "update the baseline" to make
this pass; change it only if the fixture's derivation is deliberately, reviewably changed.

Two independent assertion groups:

  DB group (``--database-url``): the canonical estate identity + shape, read straight from
  ``synthetic.*``. This is the load-bearing, deterministic assertion —
      fingerprint = md5(string_agg(id, '|' ORDER BY id)) over synthetic.resources
  plus the exact seven-count vector, plus a non-vacuity floor on the RBAC over-privilege
  and drift-eligible planes (which are not among the seven counts).

  API group (``--api-base``): proves the SAME estate is non-vacuous THROUGH THE SERVED
  stack (the ``required:false`` generator wiring means a silent generation failure would
  otherwise let ``up`` come up green-but-empty — so we assert the served bytes, not just
  liveness). Cost is queried with a JANUARY 2026 timeframe on purpose: spend anchors at
  ``--cost-as-of 2026-01-01``, so a ``MonthToDate`` query reads 0 by design.

Dependencies: ``psycopg`` (a project dependency) + stdlib ``urllib`` only. Run it with
``uv run python scripts/assert_demo_estate.py`` locally, or on the CI runner with the same
invocation. Exit code 0 = every assertion passed; 1 = at least one failed (details on
stderr) or a connection/query error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

# --- Canonical seed-42 demo-estate baseline (D-14) --------------------------------
# The md5 over the '|'-joined, id-ordered synthetic.resources ids. Locked by Wave 2's
# live validation and reproduced by Wave 2's isolated-compose run. A mismatch is a
# defect to investigate, never a value to edit here.
EXPECTED_FINGERPRINT = "2f56d6d2ffc0d1ca41850820cf6a7c57"

# The exact seven-count vector for the seed-42 / cost-2026-01-01 demo estate.
EXPECTED_COUNTS: dict[str, int] = {
    "subscriptions": 50,
    "resource_groups": 828,
    "resources": 4960,
    "violations": 1238,
    "dependencies": 290,
    "role_assignments": 761,
    "cost_records": 36408,
}

# Owner built-in roleDefinition GUID — cross-language constant, byte-identical to
# generator/identity.py BUILTIN_ROLE_DEFINITIONS and the Rust authorization.rs catalogue.
# Over-privilege signal (D-05): Owner granted to a ServicePrincipal.
OWNER_ROLE_GUID = "8e3af657-bb00-4899-acbc-f0f7f5db61aa"

# Jan-2026 cost window (the spend anchor month). MonthToDate reads 0 by design.
COST_FROM = "2026-01-01"
COST_TO = "2026-01-31"


class Reporter:
    """Collects PASS/FAIL lines; a single FAIL flips the overall result to failed."""

    def __init__(self) -> None:
        self.ok = True

    def check(self, passed: bool, label: str, detail: str = "") -> None:
        mark = "PASS" if passed else "FAIL"
        line = f"[{mark}] {label}"
        if detail:
            line += f" — {detail}"
        print(line, file=sys.stderr)
        if not passed:
            self.ok = False

    def fatal(self, label: str, detail: str) -> None:
        print(f"[FAIL] {label} — {detail}", file=sys.stderr)
        self.ok = False


# --- DB assertions ----------------------------------------------------------------


def assert_db(dsn: str, expected_fp: str, counts: dict[str, int], rep: Reporter) -> None:
    import psycopg

    print("== DB assertions (synthetic.* identity + shape) ==", file=sys.stderr)
    with psycopg.connect(dsn, connect_timeout=10) as conn, conn.cursor() as cur:
        # 1) Canonical estate fingerprint.
        cur.execute(
            "SELECT md5(string_agg(id, '|' ORDER BY id)) FROM synthetic.resources"
        )
        actual_fp = cur.fetchone()[0]
        rep.check(
            actual_fp == expected_fp,
            "estate fingerprint",
            f"expected {expected_fp}, got {actual_fp}",
        )

        # 2) Exact seven-count vector. One query per table keeps a mismatch legible.
        count_sql = {
            "subscriptions": "SELECT count(*) FROM synthetic.subscriptions",
            "resource_groups": "SELECT count(*) FROM synthetic.resource_groups",
            # Live resources only (soft-deleted excluded) — matches /_sim/summary.
            "resources": "SELECT count(*) FROM synthetic.resources WHERE drift_deleted_at IS NULL",
            "violations": "SELECT count(*) FROM synthetic.violations",
            "dependencies": "SELECT count(*) FROM synthetic.dependencies",
            "role_assignments": "SELECT count(*) FROM synthetic.role_assignments",
            "cost_records": "SELECT count(*) FROM synthetic.cost_records",
        }
        for name, expected in counts.items():
            cur.execute(count_sql[name])
            actual = int(cur.fetchone()[0])
            rep.check(actual == expected, f"count {name}", f"expected {expected}, got {actual}")

        # 3) Non-vacuity floor for the two planes NOT in the seven-count vector.
        # RBAC over-privilege: Owner granted to a ServicePrincipal (D-05).
        cur.execute(
            "SELECT count(*) FROM synthetic.role_assignments "
            "WHERE role_definition_id LIKE %s AND principal_type = 'ServicePrincipal'",
            (f"%{OWNER_ROLE_GUID}",),
        )
        overpriv = int(cur.fetchone()[0])
        rep.check(overpriv > 0, "RBAC over-privilege (Owner→ServicePrincipal) > 0", f"got {overpriv}")

        # Drift-eligible: live (not soft-deleted) resources are the drift-apply surface.
        cur.execute(
            "SELECT count(*) FROM synthetic.resources WHERE drift_deleted_at IS NULL"
        )
        drift_eligible = int(cur.fetchone()[0])
        rep.check(drift_eligible > 0, "drift-eligible (live) resources > 0", f"got {drift_eligible}")


# --- API assertions ---------------------------------------------------------------


def _get_json(url: str, *, bearer: str | None = None, body: dict | None = None) -> object:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    # ARM routes sit inside the bearer gate: any non-empty Bearer → 200, none → 401.
    # /_sim routes are bearer-exempt but accept the header harmlessly.
    req.add_header("Authorization", f"Bearer {bearer or 'assert'}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def assert_api(base: str, counts: dict[str, int], rep: Reporter) -> None:
    base = base.rstrip("/")
    print("== API assertions (served planes through the running stack) ==", file=sys.stderr)

    # /_sim/summary — served totals for five of the seven counts (bearer-exempt). The
    # served envelope is camelCase (resourceGroups), so map each expected count key to its
    # served JSON key rather than assuming the DB column name.
    summary = _get_json(f"{base}/_sim/summary")
    totals = summary.get("totals", {}) if isinstance(summary, dict) else {}
    served_key = {
        "subscriptions": "subscriptions",
        "resource_groups": "resourceGroups",
        "resources": "resources",
        "violations": "violations",
        "dependencies": "dependencies",
    }
    for count_key, json_key in served_key.items():
        rep.check(
            int(totals.get(json_key, -1)) == counts[count_key],
            f"served /_sim/summary totals.{json_key}",
            f"expected {counts[count_key]}, got {totals.get(json_key)}",
        )

    # /subscriptions (ARM, bearer) — the subscription plane.
    subs = _get_json(f"{base}/subscriptions")
    sub_list = subs.get("value", []) if isinstance(subs, dict) else []
    rep.check(
        len(sub_list) == counts["subscriptions"],
        "served /subscriptions count",
        f"expected {counts['subscriptions']}, got {len(sub_list)}",
    )
    first_sub = sub_list[0].get("subscriptionId") if sub_list else None

    # Governance (topology twin) — served violation + dependency planes non-vacuous.
    viol = _get_json(f"{base}/_sim/violations")
    rep.check(_page_nonempty(viol), "served /_sim/violations non-vacuous (governance)")
    deps = _get_json(f"{base}/_sim/dependencies")
    rep.check(_page_nonempty(deps), "served /_sim/dependencies non-vacuous (topology)")

    if first_sub is None:
        rep.fatal("served RBAC + cost planes", "no subscription id available to scope the query")
        return

    # RBAC — served roleAssignments for a real subscription is non-empty.
    ra = _get_json(
        f"{base}/subscriptions/{first_sub}/providers/Microsoft.Authorization/roleAssignments"
    )
    ra_list = ra.get("value", []) if isinstance(ra, dict) else []
    rep.check(len(ra_list) > 0, "served roleAssignments non-vacuous (RBAC)", f"got {len(ra_list)} on sub[0]")

    # Cost (FinOps) — Jan-2026 Custom timeframe. total > 0 (MonthToDate would read 0).
    cost = _get_json(
        f"{base}/subscriptions/{first_sub}/providers/Microsoft.CostManagement/query",
        body={
            "type": "ActualCost",
            "timeframe": "Custom",
            "timePeriod": {"from": COST_FROM, "to": COST_TO},
        },
    )
    total = _cost_total(cost)
    rep.check(total > 0.0, "served cost query Jan-2026 total > 0 (FinOps)", f"got {total}")


def _page_nonempty(payload: object) -> bool:
    """A keyset page is non-vacuous if its value[] has rows (or a positive count)."""
    if not isinstance(payload, dict):
        return False
    if isinstance(payload.get("value"), list) and payload["value"]:
        return True
    return int(payload.get("count", 0) or 0) > 0


def _cost_total(payload: object) -> float:
    """Sum the first (Number) cell of every row in the positional cost envelope."""
    if not isinstance(payload, dict):
        return 0.0
    rows = payload.get("properties", {}).get("rows", [])
    total = 0.0
    for row in rows:
        if row and isinstance(row[0], (int, float)):
            total += float(row[0])
    return total


# --- entrypoint -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--database-url", default=os.environ.get("DATABASE_URL"),
                   help="Postgres DSN for the DB identity/count assertions (env DATABASE_URL).")
    p.add_argument("--api-base", default=os.environ.get("ASSERT_API_BASE"),
                   help="Base URL of the served mock-server for the plane assertions (env ASSERT_API_BASE).")
    p.add_argument("--expected-fingerprint", default=EXPECTED_FINGERPRINT,
                   help="Override the expected estate fingerprint (for the deliberate FAIL dry-run only).")
    p.add_argument("--skip-db", action="store_true", help="Skip the DB identity/count group.")
    p.add_argument("--skip-api", action="store_true", help="Skip the served-plane group.")
    args = p.parse_args(argv)

    rep = Reporter()

    if not args.skip_db:
        if not args.database_url:
            rep.fatal("DB group", "no --database-url / DATABASE_URL provided")
        else:
            try:
                assert_db(args.database_url, args.expected_fingerprint, EXPECTED_COUNTS, rep)
            except Exception as exc:  # noqa: BLE001 — surface any DB error as a hard failure
                rep.fatal("DB group", f"{type(exc).__name__}: {exc}")

    if not args.skip_api:
        if not args.api_base:
            rep.fatal("API group", "no --api-base / ASSERT_API_BASE provided")
        else:
            try:
                assert_api(args.api_base, EXPECTED_COUNTS, rep)
            except urllib.error.URLError as exc:
                rep.fatal("API group", f"request failed: {exc}")
            except Exception as exc:  # noqa: BLE001
                rep.fatal("API group", f"{type(exc).__name__}: {exc}")

    print(
        f"\n== RESULT: {'PASS — estate matches the seed-42 baseline' if rep.ok else 'FAIL — estate mismatch (investigate; do NOT edit the baseline)'} ==",
        file=sys.stderr,
    )
    return 0 if rep.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
