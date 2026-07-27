#!/usr/bin/env python3
"""End-to-end validation of the synthetic tenant schema.

Connects to Postgres and validates:
1. Schema existence (6 tables in synthetic schema)
2. Write test (INSERT into tenant + subscriptions)
3. Read test (SELECT back the inserted subscription)
4. Cleanup (DELETE inserted rows)

Exits 0 on success, 1 on any failure.
"""

import os
import sys
import uuid

EXPECTED_TABLES = sorted([
    "tenant",
    "subscriptions",
    "resource_groups",
    "resources",
    "dependencies",
    "violations",
])

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgres://tenantless:tenantless_dev@localhost:5433/tenantless",
)


def main() -> int:
    # Import psycopg (v3), fall back to psycopg2
    try:
        import psycopg

        conn = psycopg.connect(DATABASE_URL)
        using = "psycopg (v3)"
    except ImportError:
        try:
            import psycopg2

            conn = psycopg2.connect(DATABASE_URL)
            using = "psycopg2"
        except ImportError:
            print("FAIL: Neither psycopg nor psycopg2 is installed")
            return 1

    print(f"Connected using {using}")
    conn.autocommit = False
    passed = True

    try:
        cur = conn.cursor()

        # Step 1: Schema existence
        print("\n--- Step 1: Schema existence ---")
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'synthetic' ORDER BY table_name;"
        )
        tables = sorted([row[0] for row in cur.fetchall()])
        for table in tables:
            print(f"  Found table: synthetic.{table}")

        if tables == EXPECTED_TABLES:
            print(f"PASS: Found all {len(tables)} expected tables")
        else:
            print(f"FAIL: Expected {EXPECTED_TABLES}, got {tables}")
            passed = False

        # Step 2: Write test
        print("\n--- Step 2: Write test ---")
        tenant_id = uuid.uuid4()
        subscription_id = uuid.uuid4()

        cur.execute(
            "INSERT INTO synthetic.tenant "
            "(tenant_id, display_name, profile_version, scale_params) "
            "VALUES (%s, %s, %s, %s)",
            (str(tenant_id), "validation-tenant", "test", '{"test": true}'),
        )
        print(f"  INSERT synthetic.tenant: OK (tenant_id={tenant_id})")

        cur.execute(
            "INSERT INTO synthetic.subscriptions "
            "(subscription_id, tenant_id, display_name, state, archetype) "
            "VALUES (%s, %s, %s, %s, %s)",
            (str(subscription_id), str(tenant_id), "validation-sub", "Enabled", "test"),
        )
        print(f"  INSERT synthetic.subscriptions: OK (subscription_id={subscription_id})")

        conn.commit()

        # Step 3: Read test
        print("\n--- Step 3: Read test ---")
        cur.execute(
            "SELECT subscription_id, display_name FROM synthetic.subscriptions "
            "WHERE display_name = 'validation-sub';"
        )
        rows = cur.fetchall()
        if len(rows) == 1:
            row_sub_id, row_name = rows[0]
            print(f"  SELECT: subscription_id={row_sub_id}, display_name={row_name}")
            print("PASS: Read back inserted subscription")
        else:
            print(f"FAIL: Expected 1 row, got {len(rows)}")
            passed = False

        # Step 4: Cleanup
        print("\n--- Step 4: Cleanup ---")
        cur.execute(
            "DELETE FROM synthetic.subscriptions WHERE subscription_id = %s",
            (str(subscription_id),),
        )
        print(f"  DELETE synthetic.subscriptions: OK")
        cur.execute(
            "DELETE FROM synthetic.tenant WHERE tenant_id = %s",
            (str(tenant_id),),
        )
        print(f"  DELETE synthetic.tenant: OK")
        conn.commit()
        print("PASS: Cleanup complete")

    except Exception as e:
        print(f"\nFAIL: {e}")
        conn.rollback()
        passed = False
    finally:
        conn.close()

    if passed:
        print("\n=== ALL STEPS PASSED ===")
        return 0
    else:
        print("\n=== SOME STEPS FAILED ===")
        return 1


if __name__ == "__main__":
    sys.exit(main())
