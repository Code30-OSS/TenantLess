#!/usr/bin/env python3
"""Export a generated synthetic estate from Postgres into a DuckDB scan file.

WHY THIS FILE EXISTS
====================
The analyzer reads a *scan* (``duckdb:<path>``), while the generator writes an
*estate* (Postgres ``synthetic.*``). Closing that loop -- generate an estate,
then analyze it back into a profile -- is what makes the bundled ``enterprise``
profile provably synthetic:

    build_oss_bootstrap_profile.py   ->  profiles/oss-bootstrap.json   (hand-authored)
    tenantless generate --profile .. ->  a synthetic estate in Postgres
    export_estate_duckdb.py          ->  a DuckDB view of that estate   <-- THIS FILE
    tenantless analyze duckdb:..     ->  src/tenantless/profiles/enterprise.json

The output schema is exactly what ``analyzer/reader.py`` expects, and matches
``analyzer/azure/materialize.py``'s in-memory DDL so both scan paths agree.

It is also useful on its own: anyone can re-derive the bundled profile from a
tenant they generated themselves, which is the reproducibility claim the public
profile rests on.

The copy runs entirely inside DuckDB via its ``postgres`` extension -- no estate
rows pass through Python, so a 60K-resource / 400K-cost-row estate exports in
seconds. The extension is downloaded once on first use and then cached, so the
first run on a fresh machine needs network access.

Run:
    uv run python scripts/export_estate_duckdb.py --out build/estate.duckdb
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import duckdb

DEFAULT_DSN = os.environ.get(
    "DATABASE_URL",
    "postgres://tenantless:tenantless_dev@localhost:5433/tenantless",
)

# One CREATE TABLE AS per target table. Column names/order mirror
# analyzer/azure/materialize.py so the two scan paths are interchangeable.
#
# Two normalizations matter for fidelity with a real DuckDB scan file:
#   * '{}' -> NULL, so `properties IS NOT NULL` / `tags IS NULL` predicates in
#     reader.py behave identically (the same thing materialize.py does).
#   * drift tombstones are excluded -- a drift-deleted resource is not part of
#     the estate a scanner would see, and counting it would skew every rate.
#
# NOTE on the cost column name: the reader's contract predates this script and
# calls the amount `amortized_cost_eur`. The generator's amounts are plain
# synthetic figures with no currency semantics; the name is kept verbatim so the
# reader's SQL works unmodified. Nothing downstream reads a currency from it.
TABLES: dict[str, str] = {
    "subscriptions": """
        SELECT CAST(subscription_id AS VARCHAR) AS subscription_id
        FROM pg.synthetic.subscriptions
    """,
    "resource_groups": """
        SELECT CAST(subscription_id AS VARCHAR) AS subscription_id, name
        FROM pg.synthetic.resource_groups
    """,
    "resources": """
        SELECT
            $scan_id                                 AS scan_id,
            id                                       AS resource_id,
            name, type, location,
            resource_group_name                      AS resource_group,
            CAST(subscription_id AS VARCHAR)         AS subscription_id,
            NULLIF(properties, '{}')                 AS properties,
            NULLIF(COALESCE(sku, '{}'), '{}')        AS sku,
            NULLIF(tags, '{}')                       AS tags,
            kind
        FROM pg.synthetic.resources
        WHERE drift_deleted_at IS NULL
    """,
    "resource_costs": """
        SELECT
            resource_id,
            strftime(billing_period, '%Y-%m') AS billing_month,
            cost_amount                       AS amortized_cost_eur
        FROM pg.synthetic.cost_records
    """,
    "findings": """
        SELECT violation_type AS finding_type, resource_id
        FROM pg.synthetic.violations
    """,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True, type=Path, help="Output .duckdb path")
    ap.add_argument("--dsn", default=DEFAULT_DSN, help="Postgres DSN")
    ap.add_argument(
        "--force", action="store_true", help="Overwrite the output file if it exists"
    )
    args = ap.parse_args()

    if args.out.exists():
        if not args.force:
            print(f"{args.out} exists (use --force to overwrite)", file=sys.stderr)
            return 1
        args.out.unlink()
        args.out.with_suffix(args.out.suffix + ".wal").unlink(missing_ok=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)

    ddb = duckdb.connect(str(args.out))
    try:
        ddb.execute("INSTALL postgres; LOAD postgres;")
    except duckdb.Error as exc:
        print(
            f"Could not load DuckDB's postgres extension: {exc}\n"
            "The first run needs network access to download it.",
            file=sys.stderr,
        )
        return 1

    # ATTACH is parsed before binding, so DuckDB will not accept a placeholder
    # here -- the DSN has to be spliced. It is operator-supplied (--dsn or
    # DATABASE_URL), not untrusted input, and the single quote is the only
    # character that could terminate the literal, so doubling it is sufficient.
    dsn_literal = args.dsn.replace("'", "''")
    ddb.execute(f"ATTACH '{dsn_literal}' AS pg (TYPE postgres, READ_ONLY)")

    row = ddb.execute("SELECT CAST(tenant_id AS VARCHAR) FROM pg.synthetic.tenant LIMIT 1").fetchone()
    if row is None:
        print(
            "No tenant found in synthetic.tenant -- generate an estate first.",
            file=sys.stderr,
        )
        return 1
    scan_id = row[0]

    counts: dict[str, int] = {}
    for table, select in TABLES.items():
        # Only the resources SELECT carries $scan_id; DuckDB rejects a bind dict
        # holding parameters the statement does not reference.
        params = {"scan_id": scan_id} if "$scan_id" in select else {}
        ddb.execute(f"CREATE TABLE {table} AS {select}", params)
        counts[table] = ddb.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    ddb.execute("DETACH pg")
    ddb.close()

    if counts["resources"] == 0:
        print(
            "Exported 0 resources -- refusing to present an empty estate as a "
            "scan (a profile fitted from it would be vacuous).",
            file=sys.stderr,
        )
        return 1

    size_mb = args.out.stat().st_size / 1e6
    print(f"Wrote {args.out} ({size_mb:.1f} MB) from tenant {scan_id}:")
    for table, n in counts.items():
        print(f"  {table:16s} {n:>9,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
