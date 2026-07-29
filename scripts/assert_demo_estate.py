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
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

# --- Canonical seed-42 demo-estate baseline (D-14) --------------------------------
# The md5 over the '|'-joined, id-ordered synthetic.resources ids. This is only a
# NECESSARY condition — it pins the resource id SET, but properties/tags/skus/costs/
# violations/principals could all mutate while it stays green (W3-3). It is retained
# as a fast, legible first check; EXPECTED_CONTENT_FINGERPRINT below is the SUFFICIENT
# one. A mismatch on either is a defect to investigate, never a value to edit here.
EXPECTED_FINGERPRINT = "2f56d6d2ffc0d1ca41850820cf6a7c57"

# Full-CONTENT estate digest (W3-3). Unlike the id-only md5, this hashes every
# reproducible business column of every content table — so a change to any property,
# tag, sku, location, cost, violation, dependency, principal or role assignment
# flips it even when the seven counts and the id set are unchanged. It is computed
# the same way as the repo's genuine tests/_fingerprint.py: each row canonicalized
# to sort-keyed JSON, the rows SORTED (DB row order is undefined), tables separated,
# sha256. COPY-time surrogate keys (dependencies.id / violations.id) and wall-clock
# columns (tenant.generated_at, drift *_at) are excluded, and the one float column
# (cost_amount) is cents-quantized (see _ROUND2_COLUMNS), so the digest is byte-stable
# across regenerations AND platforms — verified IDENTICAL on the Windows host and the
# linux/amd64 generator image. A mismatch is a defect, never a value to edit here.
EXPECTED_CONTENT_FINGERPRINT = "e4b2f67c4dc5574d7f5fd6cb2193b66d4b927e6446c6252b46770a0f81fef34f"

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


# --- Full-content estate digest (W3-3) --------------------------------------------
# ASCII separators mirror tests/_fingerprint.py so table blocks / rows / tables never
# collide with JSON content in a serialized row.
_FIELD_SEP = "\x1f"
_ROW_SEP = "\x1e"
_TABLE_SEP = b"\x00"

# Ordered (table, [reproducible business columns]) contract. Deliberately EXCLUDES:
#   * COPY-time SERIAL surrogate keys: synthetic.dependencies.id, synthetic.violations.id
#   * wall-clock columns: tenant.generated_at (+ the drift *_at columns, whose tables
#     are empty in a freshly generated demo estate and so contribute nothing anyway)
# so two same-seed regenerations produce the SAME digest. Column lists match the sql/
# DDL (001/002/004/005/006) and the writer COPY contracts.
_CONTENT_TABLES: list[tuple[str, list[str]]] = [
    # profile_name (migration 007) is the deterministic, API-visible generation-profile
    # identity the generator writes (writer.copy_tenant) and /_sim/summary serves as
    # `profile`. It MUST be fingerprinted — omitting it let a profile swap slip past both
    # the digest and every API assertion (W3-round2 #4). generated_at is excluded (wall
    # clock); profile_name is self-provisioned by `generate`, so it is present in both
    # the bare-generate and fully-migrated schemas.
    ("tenant", ["tenant_id", "display_name", "profile_version", "profile_name",
                "scale_params"]),
    ("subscriptions", ["subscription_id", "tenant_id", "display_name", "state",
                       "archetype", "tags", "authorization_source", "spending_limit"]),
    ("resource_groups", ["id", "subscription_id", "name", "location",
                         "template_type", "tags", "provisioning_state"]),
    # drift_deleted_at (migration 006) is deliberately EXCLUDED: it is server-runtime
    # drift state, always NULL in a freshly generated estate, absent from the in-memory
    # generation content model, and not provisioned by `generate` on a bare DB — so
    # including it would add nothing yet make the digest depend on migration order.
    ("resources", ["id", "subscription_id", "resource_group_name", "name", "type",
                   "location", "tags", "sku", "kind", "properties",
                   "provisioning_state", "managed_by"]),
    ("dependencies", ["dependency_type", "source_resource_id", "target_resource_id",
                      "source_subscription", "target_subscription"]),
    ("violations", ["resource_id", "violation_type", "severity", "detail"]),
    ("cost_records", ["resource_id", "subscription_id", "billing_period",
                      "cost_amount", "currency"]),
    ("principals", ["oid", "principal_type", "display_name", "app_id"]),
    ("role_assignments", ["assignment_id", "subscription_id", "principal_oid",
                          "principal_type", "role_definition_id", "scope"]),
]


# Float columns QUANTIZED to cents before hashing. cost_amount is computed with numpy
# float ops whose last ULPs differ across platforms/CPUs (Windows host vs the linux/
# amd64 generator image) — identical to >10 decimals, but Python's full-precision repr
# (what the digest hashes) diverges, and exact per-row float hashing can't be made
# reproducible across arbitrary amd64 runners (thread + SIMD dispatch). The noise is
# ~1e-11, so rounding to cents (unit 1e-2) collapses it — boundary-flip risk ~3e-5
# across the whole table — while still flipping the digest on any real cost change ≥ 1¢
# (the reviewer's cost-mutation coverage is preserved). Rounded in SQL to a numeric so
# the value is byte-identical regardless of the source float's low bits.
_ROUND2_COLUMNS = {"cost_amount"}


def _select_expr(col: str) -> str:
    """SELECT expression for a content column — cents-quantized for float columns."""
    return f"round({col}::numeric, 2)" if col in _ROUND2_COLUMNS else col


def _canon_row(cols: list[str], values: tuple) -> str:
    """Stable JSON for one row: keys sorted, uuid/date/datetime/Decimal via str."""
    return json.dumps(dict(zip(cols, values)), sort_keys=True, default=str,
                      separators=(",", ":"))


def content_fingerprint(cur) -> str:
    """sha256 over every content table's rows, canonicalized then SORTED by content.

    A pure function of the reproducible estate content, invariant to Postgres row
    order and to SERIAL assignment order (surrogate keys are not selected). Mirrors
    tests/_fingerprint.py's algorithm against the live DB rather than an in-memory
    GenerationResult. Float columns (``cost_amount``) are cents-quantized so the digest
    is byte-stable across platforms while still catching real value changes (see
    ``_ROUND2_COLUMNS``).
    """
    h = hashlib.sha256()
    for table, cols in _CONTENT_TABLES:
        exprs = ", ".join(_select_expr(c) for c in cols)
        cur.execute(f"SELECT {exprs} FROM synthetic.{table}")
        rows = sorted(_canon_row(cols, v) for v in cur.fetchall())
        h.update((table + _FIELD_SEP + _ROW_SEP.join(rows)).encode("utf-8"))
        h.update(_TABLE_SEP)
    return h.hexdigest()


# --- DB assertions ----------------------------------------------------------------


def assert_db(dsn: str, expected_fp: str, expected_content_fp: str,
              counts: dict[str, int], rep: Reporter) -> None:
    import psycopg

    print("== DB assertions (synthetic.* identity + shape) ==", file=sys.stderr)
    with psycopg.connect(dsn, connect_timeout=10) as conn, conn.cursor() as cur:
        # 1a) id-set md5 — fast NECESSARY check (pins the resource id set only).
        cur.execute(
            "SELECT md5(string_agg(id, '|' ORDER BY id)) FROM synthetic.resources"
        )
        actual_fp = cur.fetchone()[0]
        rep.check(
            actual_fp == expected_fp,
            "estate id-set fingerprint",
            f"expected {expected_fp}, got {actual_fp}",
        )

        # 1b) full-content digest — the SUFFICIENT identity check (W3-3). Catches any
        # mutation of properties/tags/skus/costs/violations/deps/principals/RBAC that
        # leaves counts + id-set intact.
        actual_content_fp = content_fingerprint(cur)
        rep.check(
            actual_content_fp == expected_content_fp,
            "estate full-content fingerprint",
            f"expected {expected_content_fp}, got {actual_content_fp}",
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
                   help="Override the expected id-set fingerprint (for the deliberate FAIL dry-run only).")
    p.add_argument("--expected-content-fingerprint", default=EXPECTED_CONTENT_FINGERPRINT,
                   help="Override the expected full-content fingerprint (deliberate FAIL dry-run only).")
    p.add_argument("--print-content-fingerprint", action="store_true",
                   help="Compute and print the full-content digest of the estate at "
                        "--database-url, then exit 0. Baseline-locking helper — asserts nothing.")
    p.add_argument("--skip-db", action="store_true", help="Skip the DB identity/count group.")
    p.add_argument("--skip-api", action="store_true", help="Skip the served-plane group.")
    args = p.parse_args(argv)

    # Baseline-locking mode: compute the content digest and print it, nothing else.
    if args.print_content_fingerprint:
        if not args.database_url:
            p.error("--print-content-fingerprint requires --database-url / DATABASE_URL")
        import psycopg

        with psycopg.connect(args.database_url, connect_timeout=10) as conn, conn.cursor() as cur:
            print(content_fingerprint(cur))
        return 0

    # W3-5: --skip-db AND --skip-api together asserts NOTHING yet exits 0 — a silent
    # green. The publication gate never passes either, but reject the combination hard
    # so no invocation can pass while checking nothing.
    if args.skip_db and args.skip_api:
        p.error("--skip-db and --skip-api cannot be combined — that would assert nothing")

    # W3-3: refuse to run the gate against an unlocked baseline (a placeholder must
    # never silently 'pass' by never being compared).
    if not args.skip_db and args.expected_content_fingerprint == "__PENDING_DOUBLE_GENERATION__":
        p.error("EXPECTED_CONTENT_FINGERPRINT is not locked yet — run "
                "--print-content-fingerprint against a canonical seed-42 estate and set it")

    rep = Reporter()

    if not args.skip_db:
        if not args.database_url:
            rep.fatal("DB group", "no --database-url / DATABASE_URL provided")
        else:
            try:
                assert_db(args.database_url, args.expected_fingerprint,
                          args.expected_content_fingerprint, EXPECTED_COUNTS, rep)
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
