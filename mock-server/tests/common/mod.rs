//! Shared integration-test helpers: a self-seeding testcontainers fixture and an
//! in-process `oneshot` request driver.
//!
//! The dev `:5433` Postgres is EMPTY until the generator runs (RESEARCH Pitfall 2),
//! so tests NEVER depend on it — every test spins an ephemeral container and seeds
//! a known fixture: 1 tenant, 2 subscriptions (A, B), >100 resource groups under
//! sub A, and >100 resources under one RG of sub A, plus a resource with empty
//! `properties = '{}'` so MOCK-13 is assertable. Column lists mirror the
//! `writer.py` COPY contracts exactly.
//!
//! **Phase-4 additions** (this file extends the Phase-3 fixture without altering
//! the dense-RG loop or `FixtureCounts`): under two dedicated filter RGs
//! (`rg-filter-000` and `Rg-Filter-Mixed`) we seed a NESTED-type resource
//! (`Microsoft.Sql/servers/sql-srv-000/databases/db-000`) for MOCK-05
//! arbitrary-depth detail resolution, four FILTER-selectivity resources spanning
//! distinct `type`/`location`/`tags` for MOCK-06 `$filter`, and a MIXED-CASE-id
//! resource for MOCK-07 case-insensitive `{rg}`/`{name}` matching.
//!
//! All rows use the same 12-column parameterized `INSERT` idiom (bound, never
//! string-built — T-04-01). The exported `pub const`s below are the verbatim
//! names/ids Plan 03/04 tests reference.

#![allow(dead_code)]

use axum::{
    Router,
    body::Body,
    http::{Request, StatusCode},
};
use sqlx::PgPool;
use tower::ServiceExt; // for `oneshot`
use uuid::Uuid;

/// The fixed tenant id used by the seeded fixture.
pub const TENANT_ID: Uuid = Uuid::from_u128(0x0000_0000_0000_0000_0000_0000_0000_0000);
/// Subscription A — the densely populated one (>100 RGs, >100 resources in one RG).
pub const SUB_A: Uuid = Uuid::from_u128(0x1111_1111_1111_1111_1111_1111_1111_1111);
/// Subscription B — minimal, proves multi-sub listing.
pub const SUB_B: Uuid = Uuid::from_u128(0x2222_2222_2222_2222_2222_2222_2222_2222);

/// The RG (under sub A) that holds >100 resources.
pub const DENSE_RG_NAME: &str = "rg-dense-000";

// ---------------------------------------------------------------------------
// Phase-4 fixture constants (under sub A). These name the explicit detail /
// `$filter` / case-insensitivity rows so Plan 03/04 tests avoid magic strings.
// ---------------------------------------------------------------------------

/// RG (under sub A) holding the Phase-4 nested-type + filter-selectivity rows.
pub const FILTER_RG_NAME: &str = "rg-filter-000";

/// Stored-mixed-case RG holding the mixed-case-id resource (MOCK-07 / D-08).
pub const FILTER_MIXED_RG_NAME: &str = "Rg-Filter-Mixed";

/// Full id of the arbitrarily-nested resource (MOCK-05). Resolvable verbatim.
pub const NESTED_RESOURCE_ID: &str = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-filter-000/providers/Microsoft.Sql/servers/sql-srv-000/databases/db-000";
/// Canonical type of the nested resource (MOCK-12 — returned exactly as stored).
pub const NESTED_RESOURCE_TYPE: &str = "Microsoft.Sql/servers/databases";

/// Full stored id of the mixed-case resource (MOCK-07). A case-insensitive
/// lookup of a lower/upper variant of `{rg}`/`{name}` must resolve to this row.
pub const MIXED_CASE_RESOURCE_ID: &str = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/Rg-Filter-Mixed/providers/Microsoft.Storage/storageAccounts/Res-MixedCase-000";

/// Discriminating values the `$filter` selectivity assertions key on (MOCK-06).
pub const FILTER_TYPE_STORAGE: &str = "Microsoft.Storage/storageAccounts";
pub const FILTER_TYPE_VNET: &str = "Microsoft.Network/virtualNetworks";
pub const FILTER_LOCATION_EAST: &str = "eastus";
pub const FILTER_LOCATION_WEST: &str = "westus";
pub const FILTER_TAG_KEY: &str = "env";
pub const FILTER_TAG_VALUE_PROD: &str = "prod";
pub const FILTER_TAG_VALUE_DEV: &str = "dev";

/// Counts the fixture guarantees, so tests can assert the harness is sound.
pub struct FixtureCounts {
    pub subscriptions: i64,
    pub resource_groups_sub_a: i64,
    pub resources_dense_rg: i64,
    /// Phase-4 additive count: rows seeded under `FILTER_RG_NAME` (nested + 4 filter).
    pub resources_filter_rg: i64,
    /// Phase-4 additive count: rows seeded under `FILTER_MIXED_RG_NAME` (mixed-case).
    pub resources_filter_mixed_rg: i64,
}

/// Provision the FULL `synthetic` schema (sql/001+002+003+005+006+007) but insert NO
/// tenant row — the Phase-17 D-09 empty-tenant fixture. Mirrors `seed_fixture`'s migration
/// application MINUS every INSERT, so an ARM read over this pool returns empty envelopes
/// (`{value:[]}`) / a detail 404 and the server's startup path tolerates zero tenant rows
/// (RESEARCH Pitfall 3). Applies the identity/drift/web-metadata migrations too so every
/// handler that references `principals`/`drift_deleted_at`/`profile_name` reads cleanly
/// (empty), never 500s on a missing relation/column.
pub async fn seed_empty_tenant(pool: &PgPool) {
    let sql_001 = include_str!("../../../sql/001_synthetic_tenant.sql");
    let sql_002 = include_str!("../../../sql/002_cross_sub_dependencies.sql");
    let sql_003 = include_str!("../../../sql/003_integrity_and_index.sql");
    let sql_005 = include_str!("../../../sql/005_identity.sql");
    let sql_006 = include_str!("../../../sql/006_drift.sql");
    let sql_007 = include_str!("../../../sql/007_web_metadata.sql");
    pool.execute_unchecked(sql_001).await;
    pool.execute_unchecked(sql_002).await;
    pool.execute_unchecked(sql_003).await;
    pool.execute_unchecked(sql_005).await;
    pool.execute_unchecked(sql_006).await;
    pool.execute_unchecked(sql_007).await;
}

/// Build an ARMED `ControlPlane` over `pool` with `token` as the control secret, using a
/// fresh unique tempdir for the three control-data subdirs (`profiles`/`sources`/`snapshots`).
/// For control-plane integration tests (token gate, validation, armed byte-identical): the
/// registry starts empty and the write-gate is a `Semaphore(1)` — the same shape
/// `ControlPlane::arm` builds, minus the CLI fail-closed logic (tested DB-free elsewhere).
/// The `database_url` is a placeholder (analyze/generate jobs in tests spawn the stub below,
/// not a real Postgres write).
///
/// **Runner seam (17-02):** `pipeline_cmd` is a deterministic `python` stub instead of the
/// production `uv run tenantless` — so a control roundtrip (generate/analyze → job →
/// registry → profile_allowed) is fast and hermetic, never depending on a runnable Python
/// CLI/uv env. The stub creates the `--out` file (analyze) if the flag is present and is a
/// harmless exit-0 no-op otherwise (generate has no `--out`), so both job kinds finalize
/// `Succeeded` without touching Postgres.
pub fn armed_control_plane(pool: &PgPool, token: &str) -> tenantless_server::job::ControlPlane {
    use tenantless_server::job::{ControlDirs, ControlPlane, digest};
    let base = std::env::temp_dir().join(format!("tenantless-ctl-test-{}", Uuid::new_v4()));
    let dirs = ControlDirs {
        profiles: base.join("profiles"),
        sources: base.join("sources"),
        snapshots: base.join("snapshots"),
    };
    for d in [&dirs.profiles, &dirs.sources, &dirs.snapshots] {
        std::fs::create_dir_all(d).expect("create control-data test dir");
    }
    // Stub: if `--out <path>` is present in argv, write `{}` there (mirrors what `analyze`
    // produces in the profiles dir); otherwise a no-op. Always exits 0.
    const STUB: &str = "import sys,pathlib\na=sys.argv\nif '--out' in a:\n pathlib.Path(a[a.index('--out')+1]).write_text('{}')\n";
    ControlPlane {
        token_digest: digest(token),
        database_url: "postgres://unused-in-tests".to_string(),
        repo_root: base.clone(),
        dirs,
        registry: std::sync::Arc::new(std::sync::Mutex::new(std::collections::HashMap::new())),
        write_gate: std::sync::Arc::new(tokio::sync::Semaphore::new(1)),
        pipeline_cmd: vec!["python".to_string(), "-c".to_string(), STUB.to_string()],
        pool: pool.clone(),
    }
}

/// An armed `ControlPlane` (as [`armed_control_plane`]) but with a REAL `database_url` —
/// the snapshot round-trip (17-04) needs `pg_dump`/`pg_restore` to connect to the SAME
/// testcontainers Postgres the pool talks to (the default builder uses a placeholder DSN,
/// since generate/analyze in tests run the python stub, not a real DB write). The snapshot
/// ops derive `PG*` env from this DSN; `pipeline_cmd` is irrelevant to them (they build their
/// own `pg_dump`/`pg_restore` commands), so the stub carried over is harmless.
pub fn armed_control_plane_with_dsn(
    pool: &PgPool,
    token: &str,
    database_url: &str,
) -> tenantless_server::job::ControlPlane {
    let mut cp = armed_control_plane(pool, token);
    cp.database_url = database_url.to_string();
    cp
}

/// Apply `sql/001` + `sql/002`, then INSERT the known fixture.
///
/// Returns the counts the fixture established so the smoke test can assert the
/// harness populated what later pagination waves rely on.
pub async fn seed_fixture(pool: &PgPool) -> FixtureCounts {
    // Schema: the same migration files the generator/dev DB use. sql/003 adds the
    // lower(id) functional index + safe FKs (SEC-MED-3) — applied here so the
    // harness mirrors the docker-entrypoint-initdb.d ordering (001 -> 002 -> 003).
    let sql_001 = include_str!("../../../sql/001_synthetic_tenant.sql");
    let sql_002 = include_str!("../../../sql/002_cross_sub_dependencies.sql");
    let sql_003 = include_str!("../../../sql/003_integrity_and_index.sql");
    pool.execute_unchecked(sql_001).await;
    pool.execute_unchecked(sql_002).await;
    pool.execute_unchecked(sql_003).await;

    // Phase 11: apply sql/006 so the additive `synthetic.resources.drift_deleted_at`
    // soft-delete column (+ drift tables) exists for EVERY existing list/detail test —
    // without it the `AND drift_deleted_at IS NULL` filter added to the three serving
    // queries 500s on the missing column (RESEARCH Pitfall 2 / fixture coupling). The
    // ALTER is nullable-no-default, so existing seeded rows get NULL and all existing
    // counts are unchanged. No fixture rows are mutated.
    let sql_006 = include_str!("../../../sql/006_drift.sql");
    pool.execute_unchecked(sql_006).await;

    // v1.1.10: apply sql/008 so the case-insensitive resource-group functional index
    // exists in the fixture DB, mirroring docker-entrypoint-initdb.d (which applies every
    // sql/*.sql on a fresh volume). Index-only DDL — no fixture rows are mutated and all
    // existing counts are unchanged.
    let sql_008 = include_str!("../../../sql/008_rg_lower_index.sql");
    pool.execute_unchecked(sql_008).await;

    // 1 tenant.
    sqlx::query(
        r#"INSERT INTO synthetic.tenant
               (tenant_id, display_name, profile_version, scale_params)
           VALUES ($1, $2, $3, '{}'::jsonb)"#,
    )
    .bind(TENANT_ID)
    .bind("Contoso-Synthetic")
    .bind("test-1.0")
    .execute(pool)
    .await
    .expect("insert tenant");

    // 2 subscriptions (writer.py copy_subscriptions column contract).
    for (id, name) in [(SUB_A, "Contoso-Prod-A"), (SUB_B, "Contoso-Dev-B")] {
        sqlx::query(
            r#"INSERT INTO synthetic.subscriptions
                   (subscription_id, tenant_id, display_name, state, archetype,
                    tags, authorization_source, spending_limit)
               VALUES ($1, $2, $3, 'Enabled', 'prod', '{}'::jsonb, 'RoleBased', 'Off')"#,
        )
        .bind(id)
        .bind(TENANT_ID)
        .bind(name)
        .execute(pool)
        .await
        .expect("insert subscription");
    }

    // >100 resource groups under sub A (writer.py copy_resource_groups contract).
    // The first RG (rg-dense-000) becomes the dense one for resource pagination.
    let rg_count: i64 = 105;
    for i in 0..rg_count {
        let name = format!("rg-dense-{i:03}");
        let id = format!("/subscriptions/{SUB_A}/resourceGroups/{name}");
        sqlx::query(
            r#"INSERT INTO synthetic.resource_groups
                   (id, subscription_id, name, location, template_type, tags, provisioning_state)
               VALUES ($1, $2, $3, 'eastus', 'network', '{}'::jsonb, 'Succeeded')"#,
        )
        .bind(&id)
        .bind(SUB_A)
        .bind(&name)
        .execute(pool)
        .await
        .expect("insert resource group");
    }

    // >100 resources under the dense RG (writer.py copy_resources contract).
    // One resource intentionally keeps properties = '{}' (MOCK-13 assertable).
    let res_count: i64 = 110;
    for i in 0..res_count {
        let name = format!("res-{i:04}");
        let id = format!(
            "/subscriptions/{SUB_A}/resourceGroups/{DENSE_RG_NAME}/providers/Microsoft.Storage/storageAccounts/{name}"
        );
        // First resource: empty properties; the rest: a populated object.
        let properties = if i == 0 {
            "{}"
        } else {
            r#"{"provisioningState":"Succeeded"}"#
        };
        sqlx::query(
            r#"INSERT INTO synthetic.resources
                   (id, subscription_id, resource_group_name, name, type, location,
                    tags, sku, kind, properties, provisioning_state, managed_by)
               VALUES ($1, $2, $3, $4, 'Microsoft.Storage/storageAccounts', 'eastus',
                       '{}'::jsonb, NULL, NULL, $5::jsonb, 'Succeeded', NULL)"#,
        )
        .bind(&id)
        .bind(SUB_A)
        .bind(DENSE_RG_NAME)
        .bind(&name)
        .bind(properties)
        .execute(pool)
        .await
        .expect("insert resource");
    }

    // -----------------------------------------------------------------------
    // Phase-4 fixture rows: nested-type, filter-selectivity, and mixed-case
    // resources under dedicated filter RGs. Same 12-column parameterized INSERT
    // idiom as the dense-RG loop above (bound, never string-built — T-04-01).
    // These RGs are NOT the dense RG, so the Phase-3 `resources_dense_rg` count
    // is unaffected (its smoke test scopes on `resource_group_name`).
    // -----------------------------------------------------------------------

    // Create the two filter RGs first (FK/scope validity), via the same
    // resource_groups INSERT idiom used by the dense-RG loop.
    for (rg_name, location) in [
        (FILTER_RG_NAME, FILTER_LOCATION_EAST),
        (FILTER_MIXED_RG_NAME, FILTER_LOCATION_EAST),
    ] {
        let rg_id = format!("/subscriptions/{SUB_A}/resourceGroups/{rg_name}");
        sqlx::query(
            r#"INSERT INTO synthetic.resource_groups
                   (id, subscription_id, name, location, template_type, tags, provisioning_state)
               VALUES ($1, $2, $3, $4, 'network', '{}'::jsonb, 'Succeeded')"#,
        )
        .bind(&rg_id)
        .bind(SUB_A)
        .bind(rg_name)
        .bind(location)
        .execute(pool)
        .await
        .expect("insert filter resource group");
    }

    // One Phase-4 fixture row, bound (never spliced) into the canonical INSERT.
    struct Phase4Row<'a> {
        id: &'a str,
        rg_name: &'a str,
        name: &'a str,
        ty: &'a str,
        location: &'a str,
        tags_json: &'a str,
        properties_json: &'a str,
    }

    // Helper: insert one resource via the canonical 12-column bound INSERT.
    // `tags` is bound as a JSON string cast to jsonb — never spliced into SQL.
    async fn insert_resource(pool: &PgPool, row: Phase4Row<'_>) {
        sqlx::query(
            r#"INSERT INTO synthetic.resources
                   (id, subscription_id, resource_group_name, name, type, location,
                    tags, sku, kind, properties, provisioning_state, managed_by)
               VALUES ($1, $2, $3, $4, $5, $6,
                       $7::jsonb, NULL, NULL, $8::jsonb, 'Succeeded', NULL)"#,
        )
        .bind(row.id)
        .bind(SUB_A)
        .bind(row.rg_name)
        .bind(row.name)
        .bind(row.ty)
        .bind(row.location)
        .bind(row.tags_json)
        .bind(row.properties_json)
        .execute(pool)
        .await
        .expect("insert phase-4 resource");
    }

    // 1) NESTED resource (MOCK-05 arbitrary depth, MOCK-12 canonical casing).
    insert_resource(
        pool,
        Phase4Row {
            id: NESTED_RESOURCE_ID,
            rg_name: FILTER_RG_NAME,
            name: "db-000",
            ty: NESTED_RESOURCE_TYPE,
            location: FILTER_LOCATION_WEST,
            tags_json: r#"{"env":"prod"}"#,
            properties_json: r#"{"status":"Online"}"#,
        },
    )
    .await;

    // 2) FILTER selectivity rows (MOCK-06): distinct (type, location, tags)
    //    combinations so each predicate selects a provable subset.
    //    flt-0000: storage / eastus / env=prod
    //    flt-0001: storage / westus / env=dev
    //    flt-0002: vnet    / eastus / env=prod
    //    flt-0003: vnet    / westus / env=dev,team=core
    let filter_rows: [(&str, &str, &str, &str); 4] = [
        (
            "flt-0000",
            FILTER_TYPE_STORAGE,
            FILTER_LOCATION_EAST,
            r#"{"env":"prod"}"#,
        ),
        (
            "flt-0001",
            FILTER_TYPE_STORAGE,
            FILTER_LOCATION_WEST,
            r#"{"env":"dev"}"#,
        ),
        (
            "flt-0002",
            FILTER_TYPE_VNET,
            FILTER_LOCATION_EAST,
            r#"{"env":"prod"}"#,
        ),
        (
            "flt-0003",
            FILTER_TYPE_VNET,
            FILTER_LOCATION_WEST,
            r#"{"env":"dev","team":"core"}"#,
        ),
    ];
    for (name, ty, location, tags_json) in filter_rows {
        let id =
            format!("/subscriptions/{SUB_A}/resourceGroups/{FILTER_RG_NAME}/providers/{ty}/{name}");
        insert_resource(
            pool,
            Phase4Row {
                id: &id,
                rg_name: FILTER_RG_NAME,
                name,
                ty,
                location,
                tags_json,
                properties_json: r#"{"provisioningState":"Succeeded"}"#,
            },
        )
        .await;
    }

    // 3) MIXED-CASE resource (MOCK-07 / D-08): stored id carries mixed-case
    //    `{rg}` and `{name}`; the case-insensitive test requests a lower/upper
    //    variant and compares against MIXED_CASE_RESOURCE_ID.
    insert_resource(
        pool,
        Phase4Row {
            id: MIXED_CASE_RESOURCE_ID,
            rg_name: FILTER_MIXED_RG_NAME,
            name: "Res-MixedCase-000",
            ty: FILTER_TYPE_STORAGE,
            location: FILTER_LOCATION_EAST,
            tags_json: r#"{"env":"prod"}"#,
            properties_json: r#"{"provisioningState":"Succeeded"}"#,
        },
    )
    .await;

    FixtureCounts {
        subscriptions: 2,
        resource_groups_sub_a: rg_count,
        resources_dense_rg: res_count,
        // 1 nested + 4 filter rows under FILTER_RG_NAME.
        resources_filter_rg: 5,
        // 1 mixed-case row under FILTER_MIXED_RG_NAME.
        resources_filter_mixed_rg: 1,
    }
}

// ---------------------------------------------------------------------------
// Cost fixture (Plan 09-05) — a SCOPED, deterministic cost-row seed applied ON
// TOP of the shared fixture WITHOUT touching `seed_fixture` (project memory:
// fixture coupling — earlier phases' hardcoded counts stay zero-cost). The cost
// rows reference EXISTING Phase-4 filter-RG resources (so the FK fk_cost_resource
// holds and the 0-dangling anti-join is 0). Amounts are exact-in-f64 so the
// reconciliation total is byte-deterministic.
// ---------------------------------------------------------------------------

/// The deterministic billing period the cost rows are seeded at (first-of-month,
/// monthly grain) and a Custom timeframe window that brackets it inclusively. Using
/// an explicit Custom window makes the cost queries independent of the wall clock.
pub const COST_BILLING_PERIOD: &str = "2026-01-01";
pub const COST_FROM: &str = "2026-01-01";
pub const COST_TO: &str = "2026-01-31";

/// What `seed_cost_rows` established, so the reconciliation/both-scopes assertions
/// have a known ground truth.
pub struct CostSeed {
    /// SUM over every cost row under SUB_A (sub-scope total).
    pub sub_total: f64,
    /// SUM over the cost rows whose resource lives in `FILTER_RG_NAME` (RG-scope total).
    pub rg_filter_total: f64,
    /// How many cost rows were inserted (all under SUB_A).
    pub row_count: i64,
}

/// Apply `sql/004` (the cost fact table + FK), then INSERT a small deterministic
/// cost set against EXISTING fixture resources. MUST be called AFTER `seed_fixture`
/// (the FK references `synthetic.resources`). Returns the ground-truth totals.
///
/// The six cost-bearing resources are the Phase-4 filter-RG rows (known type / RG /
/// `env` tag) plus the mixed-case row in a DIFFERENT RG, so the RG-scope total is a
/// strict subset of the sub-scope total:
///   nested  (Sql/servers/databases, rg-filter-000, env=prod)   100.0
///   flt-0000(storage,                rg-filter-000, env=prod)   200.0
///   flt-0001(storage,                rg-filter-000, env=dev)     50.0
///   flt-0002(vnet,                   rg-filter-000, env=prod)    25.0
///   flt-0003(vnet,                   rg-filter-000, env=dev)     75.0
///   mixed   (storage,                Rg-Filter-Mixed, env=prod) 300.0
/// sub-scope SUM = 750.0; rg-filter-000 SUM = 450.0.
pub async fn seed_cost_rows(pool: &PgPool) -> CostSeed {
    let sql_004 = include_str!("../../../sql/004_cost.sql");
    pool.execute_unchecked(sql_004).await;

    // (resource_id, amount). Every id matches a row seed_fixture already inserted.
    let flt = |ty: &str, name: &str| {
        format!("/subscriptions/{SUB_A}/resourceGroups/{FILTER_RG_NAME}/providers/{ty}/{name}")
    };
    let rows: [(String, f64); 6] = [
        (NESTED_RESOURCE_ID.to_string(), 100.0),
        (flt(FILTER_TYPE_STORAGE, "flt-0000"), 200.0),
        (flt(FILTER_TYPE_STORAGE, "flt-0001"), 50.0),
        (flt(FILTER_TYPE_VNET, "flt-0002"), 25.0),
        (flt(FILTER_TYPE_VNET, "flt-0003"), 75.0),
        (MIXED_CASE_RESOURCE_ID.to_string(), 300.0),
    ];

    for (resource_id, amount) in &rows {
        sqlx::query(
            r#"INSERT INTO synthetic.cost_records
                   (resource_id, subscription_id, billing_period, cost_amount, currency)
               VALUES ($1, $2, $3::date, $4, 'USD')"#,
        )
        .bind(resource_id)
        .bind(SUB_A)
        .bind(COST_BILLING_PERIOD)
        .bind(amount)
        .execute(pool)
        .await
        .expect("insert cost row");
    }

    let sub_total: f64 = rows.iter().map(|(_, a)| a).sum();
    let rg_filter_total: f64 = 100.0 + 200.0 + 50.0 + 25.0 + 75.0; // the 5 rg-filter-000 rows
    CostSeed {
        sub_total,
        rg_filter_total,
        row_count: rows.len() as i64,
    }
}

// ---------------------------------------------------------------------------
// Identity / RBAC fixture (Plan 10-03) — a SCOPED, deterministic principals +
// role_assignments seed applied ON TOP of the shared fixture WITHOUT touching
// `seed_fixture` (project memory: fixture coupling). Every assignment references an
// EXISTING fixture subscription/RG/resource scope + a real seeded principal + a
// built-in roleDefinition GUID that the served `authorization.rs` catalogue ships
// (so all three legs of the 0-dangling anti-join return 0 — D-07/Pitfall 1/3).
// ---------------------------------------------------------------------------

/// Built-in role GUIDs (a subset) the identity seed draws from. These MUST be present
/// in the served `authorization.rs` BUILTIN_ROLE_DEFINITIONS catalogue (Pitfall 3,
/// pinned by `role_def_catalogue_agrees`). Byte-identical to `identity.py`.
pub const ROLE_OWNER_GUID: &str = "8e3af657-bb00-4899-acbc-f0f7f5db61aa";
pub const ROLE_CONTRIBUTOR_GUID: &str = "b24988ac-6180-42a0-ab88-20f7382dd24c";
pub const ROLE_READER_GUID: &str = "acdd72a7-3385-48ef-bd42-f606fba81ae7";

/// The three seeded principal oids, exported so the `$filter=principalId eq` tests can
/// target a known principal. Values are byte-identical to the original inline literals
/// `seed_identity_rows` used — the seed now references these consts (single source).
/// `PRINCIPAL_USER` holds 2 assignments (Reader@resource, Contributor@RG); `PRINCIPAL_GROUP`
/// holds 2 (Reader@RG, Owner@subscription); `PRINCIPAL_SP` holds 2 (Reader@resource,
/// Owner@subscription).
pub const PRINCIPAL_USER: Uuid = Uuid::from_u128(0x0a0a_0a0a_0a0a_0a0a_0a0a_0a0a_0a0a_0a0a);
pub const PRINCIPAL_GROUP: Uuid = Uuid::from_u128(0x0b0b_0b0b_0b0b_0b0b_0b0b_0b0b_0b0b_0b0b);
pub const PRINCIPAL_SP: Uuid = Uuid::from_u128(0x0c0c_0c0c_0c0c_0c0c_0c0c_0c0c_0c0c_0c0c);

/// What `seed_identity_rows` established, so the integration assertions have a known
/// ground truth. `role_definition_ids` is the DISTINCT set of tenant-scoped
/// roleDefinition ids seeded (for the catalogue-agreement assertion).
pub struct IdentitySeed {
    /// How many principals were inserted (User + Group + ServicePrincipal).
    pub principal_count: i64,
    /// How many role_assignments were inserted (all under SUB_A).
    pub assignment_count: i64,
    /// The DISTINCT tenant-scoped roleDefinition ids referenced by the seeded rows.
    pub role_definition_ids: Vec<String>,
    /// The subscription every seeded assignment lives under (SUB_A).
    pub sub: Uuid,
}

/// Apply `sql/005_identity.sql`, then INSERT a small deterministic principal directory +
/// role_assignments against EXISTING fixture scopes. MUST be called AFTER `seed_fixture`
/// (the assignments reference its subscription/RG/resource ids; the FK references the
/// seeded principals). Returns the ground-truth [`IdentitySeed`].
///
/// The seed is a spread of Reader/Contributor at sub/RG/resource scopes plus two
/// over-privilege rows (Owner-at-subscription, one of them granted to a
/// ServicePrincipal — D-05). All INSERT literals are bound as `$N` (SQL bar / project
/// memory); `seed_fixture` is left untouched (Pitfall 4).
pub async fn seed_identity_rows(pool: &PgPool) -> IdentitySeed {
    let sql_005 = include_str!("../../../sql/005_identity.sql");
    pool.execute_unchecked(sql_005).await;

    // 3 principals: a User, a Group, and a ServicePrincipal (the SP carries an app_id;
    // display_name stays NULL — principals are ARM-opaque GUIDs, IAM-01/Pitfall 5). The
    // oids are the exported `PRINCIPAL_*` consts (single source — the `$filter` tests
    // target the same values).
    let p_user = PRINCIPAL_USER;
    let p_group = PRINCIPAL_GROUP;
    let p_sp = PRINCIPAL_SP;
    let sp_app_id = Uuid::from_u128(0x0d0d_0d0d_0d0d_0d0d_0d0d_0d0d_0d0d_0d0d);

    let principals: [(Uuid, &str, Option<Uuid>); 3] = [
        (p_user, "User", None),
        (p_group, "Group", None),
        (p_sp, "ServicePrincipal", Some(sp_app_id)),
    ];
    for (oid, principal_type, app_id) in principals {
        sqlx::query(
            r#"INSERT INTO synthetic.principals (oid, principal_type, display_name, app_id)
               VALUES ($1, $2, NULL, $3)"#,
        )
        .bind(oid)
        .bind(principal_type)
        .bind(app_id)
        .execute(pool)
        .await
        .expect("insert principal");
    }

    // The three REAL scope tiers, all present in the seed_fixture (so the scope
    // anti-join returns 0): the subscription, an existing RG, an existing resource.
    let sub_scope = format!("/subscriptions/{SUB_A}");
    let rg_scope = format!("/subscriptions/{SUB_A}/resourceGroups/{FILTER_RG_NAME}");
    let res_scope = NESTED_RESOURCE_ID.to_string();
    let role_def_id =
        |guid: &str| format!("/providers/Microsoft.Authorization/roleDefinitions/{guid}");

    // (assignment_id, principal, principal_type, role guid, scope). The last two rows
    // are the over-privilege injection: Owner at SUBSCRIPTION scope, one of them on a
    // ServicePrincipal (the spicy SP-granted-Owner signal — D-05).
    struct RaSeed {
        assignment_id: Uuid,
        principal: Uuid,
        principal_type: &'static str,
        role_guid: &'static str,
        scope: String,
    }
    let assignments = vec![
        RaSeed {
            assignment_id: Uuid::from_u128(0x1001_0000_0000_0000_0000_0000_0000_0001),
            principal: p_user,
            principal_type: "User",
            role_guid: ROLE_READER_GUID,
            scope: res_scope.clone(),
        },
        RaSeed {
            assignment_id: Uuid::from_u128(0x1001_0000_0000_0000_0000_0000_0000_0002),
            principal: p_user,
            principal_type: "User",
            role_guid: ROLE_CONTRIBUTOR_GUID,
            scope: rg_scope.clone(),
        },
        RaSeed {
            assignment_id: Uuid::from_u128(0x1001_0000_0000_0000_0000_0000_0000_0003),
            principal: p_group,
            principal_type: "Group",
            role_guid: ROLE_READER_GUID,
            scope: rg_scope.clone(),
        },
        RaSeed {
            assignment_id: Uuid::from_u128(0x1001_0000_0000_0000_0000_0000_0000_0004),
            principal: p_sp,
            principal_type: "ServicePrincipal",
            role_guid: ROLE_READER_GUID,
            scope: res_scope.clone(),
        },
        // Over-privilege #1: a ServicePrincipal granted Owner at the subscription.
        RaSeed {
            assignment_id: Uuid::from_u128(0x1001_0000_0000_0000_0000_0000_0000_0005),
            principal: p_sp,
            principal_type: "ServicePrincipal",
            role_guid: ROLE_OWNER_GUID,
            scope: sub_scope.clone(),
        },
        // Over-privilege #2: a Group granted Owner at the subscription.
        RaSeed {
            assignment_id: Uuid::from_u128(0x1001_0000_0000_0000_0000_0000_0000_0006),
            principal: p_group,
            principal_type: "Group",
            role_guid: ROLE_OWNER_GUID,
            scope: sub_scope.clone(),
        },
    ];

    for a in &assignments {
        sqlx::query(
            r#"INSERT INTO synthetic.role_assignments
                   (assignment_id, subscription_id, principal_oid, principal_type,
                    role_definition_id, scope)
               VALUES ($1, $2, $3, $4, $5, $6)"#,
        )
        .bind(a.assignment_id)
        .bind(SUB_A)
        .bind(a.principal)
        .bind(a.principal_type)
        .bind(role_def_id(a.role_guid))
        .bind(&a.scope)
        .execute(pool)
        .await
        .expect("insert role assignment");
    }

    let mut role_definition_ids: Vec<String> = assignments
        .iter()
        .map(|a| role_def_id(a.role_guid))
        .collect();
    role_definition_ids.sort();
    role_definition_ids.dedup();

    IdentitySeed {
        principal_count: principals.len() as i64,
        assignment_count: assignments.len() as i64,
        role_definition_ids,
        sub: SUB_A,
    }
}

// ---------------------------------------------------------------------------
// Drift audit fixture (Plan 11-07) — a SCOPED, deterministic drift_batches +
// drift_records seed (plus ONE soft-deleted fixture resource) applied ON TOP of
// the shared fixture WITHOUT touching `seed_fixture` (project memory: fixture
// coupling — earlier phases' hardcoded counts stay drift-free). Every drift_record
// references an EXISTING `seed_fixture` resource id; the soft-deleted row is an
// existing dense-RG resource hidden via `drift_deleted_at` (D-09/D-11). All INSERT
// literals are bound as `$N` (SQL bar / project memory).
// ---------------------------------------------------------------------------

/// What `seed_drift_rows` established, so the audit-read / soft-delete assertions have
/// a known ground truth.
pub struct DriftSeed {
    /// The single seeded batch's id (the get-batch / list-drift ground truth).
    pub batch_id: Uuid,
    /// The batch's drift_type (`chaos`).
    pub drift_type: String,
    /// Total drift_records inserted in the batch.
    pub record_count: i64,
    /// A resource id carrying multiple drift records (the by-resource ground truth).
    pub drifted_resource_id: String,
    /// How many drift records reference `drifted_resource_id`.
    pub drifted_resource_record_count: i64,
    /// An EXISTING fixture resource hidden via `drift_deleted_at` (soft-delete; D-11).
    pub soft_deleted_resource_id: String,
    /// Total number of `drift_batches` rows seeded (the list_drift continuation
    /// ground truth — MUST exceed a small `$top` so paging requires >1 page).
    pub batch_count: i64,
    /// A batch carrying MORE than one small-`$top` page of records (the get_batch
    /// records-continuation ground truth).
    pub cont_batch_id: Uuid,
    /// How many drift records `cont_batch_id` carries.
    pub cont_record_count: i64,
    /// A resource id carrying MORE than one small-`$top` page of records, ALL within
    /// `cont_batch_id` (the by-resource continuation ground truth). Distinct from
    /// `drifted_resource_id` so the primary by-resource count stays 2.
    pub cont_resource_id: String,
    /// How many drift records reference `cont_resource_id` (== `cont_record_count`).
    pub cont_resource_record_count: i64,
}

/// Apply `sql/006_drift.sql` (idempotent — `seed_fixture` already applied it), then
/// INSERT a small deterministic `drift_batches` + `drift_records` set against EXISTING
/// fixture resource ids and soft-delete ONE existing dense-RG resource. MUST be called
/// AFTER `seed_fixture`. Returns the ground-truth [`DriftSeed`]. Leaves `seed_fixture`'s
/// counts untouched (the scoped-helper contract — project memory).
pub async fn seed_drift_rows(pool: &PgPool) -> DriftSeed {
    let sql_006 = include_str!("../../../sql/006_drift.sql");
    pool.execute_unchecked(sql_006).await;

    let batch_id = Uuid::from_u128(0x2001_0000_0000_0000_0000_0000_0000_0001);

    sqlx::query(
        r#"INSERT INTO synthetic.drift_batches
               (batch_id, drift_type, seed, options, parent_fingerprint, result_fingerprint)
           VALUES ($1, $2, $3, $4::jsonb, $5, $6)"#,
    )
    .bind(batch_id)
    .bind("chaos")
    .bind(7_i64)
    .bind(r#"{"intensity":0.1,"type":"chaos"}"#)
    .bind("parent-fp-0000000000000000")
    .bind("result-fp-1111111111111111")
    .execute(pool)
    .await
    .expect("insert drift batch");

    // The existing dense-RG resource (res-0001) we hide via drift_deleted_at — a leaf
    // storage account seeded by `seed_fixture`.
    let soft_deleted_resource_id = format!(
        "/subscriptions/{SUB_A}/resourceGroups/{DENSE_RG_NAME}/providers/Microsoft.Storage/storageAccounts/res-0001"
    );

    // before/after are bound as JSON text cast to ::jsonb — never spliced.
    // `drift_code` / `metadata` (sql/006 audit columns, persisted by remediation 2) are
    // seeded on every record so the audit-read surface assertion has ground truth.
    struct RecSeed {
        resource_id: String,
        field_path: &'static str,
        before: &'static str,
        after: &'static str,
        drift_code: &'static str,
        metadata: &'static str,
    }
    let records = vec![
        // Two records against the NESTED resource (the by-resource ground truth = 2).
        RecSeed {
            resource_id: NESTED_RESOURCE_ID.to_string(),
            field_path: "properties.allowBlobPublicAccess",
            before: "false",
            after: "true",
            drift_code: "CHAOS_PUBLIC_ACCESS",
            metadata: r#"{"mutation":"public_access","intensity":0.1}"#,
        },
        RecSeed {
            resource_id: NESTED_RESOURCE_ID.to_string(),
            field_path: "tags.env",
            before: r#""prod""#,
            after: r#""dev""#,
            drift_code: "CHAOS_TAG_FLIP",
            metadata: r#"{"mutation":"tag_flip","key":"env"}"#,
        },
        // The disappear record for the soft-deleted resource (full-column before/after).
        RecSeed {
            resource_id: soft_deleted_resource_id.clone(),
            field_path: "drift_deleted_at",
            before: "null",
            after: r#""2026-06-27T00:00:00+00:00""#,
            drift_code: "CHAOS_DISAPPEAR",
            metadata: r#"{"mutation":"disappear"}"#,
        },
    ];

    for r in &records {
        sqlx::query(
            r#"INSERT INTO synthetic.drift_records
                   (batch_id, resource_id, subscription_id, field_path, before, after,
                    drift_code, metadata)
               VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7, $8::jsonb)"#,
        )
        .bind(batch_id)
        .bind(&r.resource_id)
        .bind(SUB_A)
        .bind(r.field_path)
        .bind(r.before)
        .bind(r.after)
        .bind(r.drift_code)
        .bind(r.metadata)
        .execute(pool)
        .await
        .expect("insert drift record");
    }

    // Soft-delete (disappear, D-09) ONE existing fixture resource so the ARM list/detail
    // soft-delete filter is exercised; the row STAYS in the DB (only hidden).
    sqlx::query("UPDATE synthetic.resources SET drift_deleted_at = now() WHERE id = $1")
        .bind(&soft_deleted_resource_id)
        .execute(pool)
        .await
        .expect("soft-delete fixture resource");

    let drifted_resource_record_count = records
        .iter()
        .filter(|r| r.resource_id.as_str() == NESTED_RESOURCE_ID)
        .count() as i64;

    // ----------------------------------------------------------------------------
    // Continuation ground truth (Plan 11-11): the primary batch above is too small
    // to exercise keyset continuation, so seed > one small-`$top` page of batches
    // and of records. A `$top=2` traversal must require ≥3 pages.
    //   * cont_batch_id: one batch carrying `CONT_RECORDS` records, ALL on a single
    //     distinct resource (cont_resource_id) → drives BOTH the get_batch records
    //     continuation AND the by_resource continuation.
    //   * BARE_BATCHES extra batches (no records) → pushes the total batch_count
    //     past `$top` so list_drift continuation needs multiple pages.
    // None of this touches synthetic.resources, so the soft-delete/ARM counts
    // (project memory: fixture coupling) are unchanged.
    // ----------------------------------------------------------------------------
    const CONT_RECORDS: i64 = 5;
    const BARE_BATCHES: i64 = 3;

    let cont_batch_id = Uuid::from_u128(0x2001_0000_0000_0000_0000_0000_0000_0002);
    let cont_resource_id = format!(
        "/subscriptions/{SUB_A}/resourceGroups/{DENSE_RG_NAME}/providers/Microsoft.Storage/storageAccounts/res-0002"
    );

    sqlx::query(
        r#"INSERT INTO synthetic.drift_batches
               (batch_id, drift_type, seed, options, parent_fingerprint, result_fingerprint)
           VALUES ($1, $2, $3, $4::jsonb, $5, $6)"#,
    )
    .bind(cont_batch_id)
    .bind("chaos")
    .bind(11_i64)
    .bind(r#"{"intensity":0.2,"type":"chaos"}"#)
    .bind("parent-fp-2222222222222222")
    .bind("result-fp-3333333333333333")
    .execute(pool)
    .await
    .expect("insert continuation drift batch");

    for i in 0..CONT_RECORDS {
        sqlx::query(
            r#"INSERT INTO synthetic.drift_records
                   (batch_id, resource_id, subscription_id, field_path, before, after,
                    drift_code, metadata)
               VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7, $8::jsonb)"#,
        )
        .bind(cont_batch_id)
        .bind(&cont_resource_id)
        .bind(SUB_A)
        .bind(format!("properties.field{i}"))
        .bind("false")
        .bind("true")
        .bind("CHAOS_CONT")
        .bind(r#"{"mutation":"cont"}"#)
        .execute(pool)
        .await
        .expect("insert continuation drift record");
    }

    // Bare batches (record-free) to push batch_count past a small `$top`.
    for i in 0..BARE_BATCHES {
        let bare_id = Uuid::from_u128(0x2001_0000_0000_0000_0000_0000_0000_0010 + i as u128);
        sqlx::query(
            r#"INSERT INTO synthetic.drift_batches
                   (batch_id, drift_type, seed, options, parent_fingerprint, result_fingerprint)
               VALUES ($1, $2, $3, $4::jsonb, $5, $6)"#,
        )
        .bind(bare_id)
        .bind("temporal")
        .bind(20_i64 + i)
        .bind(r#"{"type":"temporal"}"#)
        .bind("parent-fp-bare")
        .bind("result-fp-bare")
        .execute(pool)
        .await
        .expect("insert bare drift batch");
    }

    // 2 explicit + 1 continuation + BARE_BATCHES.
    let batch_count = 2 + BARE_BATCHES;

    DriftSeed {
        batch_id,
        drift_type: "chaos".to_string(),
        record_count: records.len() as i64,
        drifted_resource_id: NESTED_RESOURCE_ID.to_string(),
        drifted_resource_record_count,
        soft_deleted_resource_id,
        batch_count,
        cont_batch_id,
        cont_record_count: CONT_RECORDS,
        cont_resource_id,
        cont_resource_record_count: CONT_RECORDS,
    }
}

// ---------------------------------------------------------------------------
// `/_sim` fixture (Phase 14, WAPI-01/02/03) — a SCOPED, deterministic
// violations + dependencies seed applied ON TOP of the shared fixture WITHOUT
// touching `seed_fixture` or `FixtureCounts` (project memory: fixture coupling).
// Every violation references an EXISTING `seed_fixture` resource id under SUB_A
// (0-dangling → `totals.violations` reconciles with the per-sub sum, Pitfall 3);
// stored casing mirrors the generator domains — codes UPPER_SNAKE, severities
// Title, dependency types lower-hyphen (Pitfall 6 / D-09). Enough rows are seeded
// to exceed a small `$top` so Plans 14-02/14-03 pagination traversal needs >1 page
// (mirrors `seed_drift_rows`' surplus trick). All INSERT literals are bound `$N`.
// ---------------------------------------------------------------------------

/// The subscription B resource id a cross-sub dependency edge targets. `synthetic.dependencies`
/// carries no FK (cross-sub FKs are deferred — sql/003), so a SUB_B target id that has no
/// resource row is valid: `crossSubscription` keys on the SUBSCRIPTION ids, not the resource rows.
pub const SIM_SUB_B_RESOURCE_ID: &str = "/subscriptions/22222222-2222-2222-2222-222222222222/resourceGroups/rg-b-000/providers/Microsoft.Network/virtualNetworks/vnet-b-000";

/// What `seed_sim_rows` established, so the Plan 14-02/14-03 assertions have a known
/// ground truth. All violations live under SUB_A (their resources do); the cross-sub
/// dependency edges connect SUB_A → SUB_B.
pub struct SimSeed {
    /// Total violations inserted (== `sum(per-sub violationCount)` given 0-dangling).
    pub violation_count: i64,
    /// Total dependencies inserted.
    pub dependency_count: i64,
    /// How many dependencies have `source_subscription <> target_subscription`.
    pub cross_sub_dependency_count: i64,
    /// The subscription every seeded violation's resource lives under (SUB_A).
    pub sub: Uuid,
    /// A resource id carrying MORE than one violation (the `?resource=` filter ground truth).
    pub multi_violation_resource_id: String,
    /// How many violations reference `multi_violation_resource_id`.
    pub multi_violation_resource_count: i64,
    /// A violation code present with a known count (UPPER_SNAKE — the `?code=` ground truth).
    pub sample_code: String,
    /// How many violations carry `sample_code`.
    pub sample_code_count: i64,
    /// A severity present (Title case — the `?severity=` ground truth).
    pub sample_severity: String,
    /// A dependency type present (lower-hyphen — the `?type=` ground truth).
    pub sample_dependency_type: String,
    /// How many dependencies carry `sample_dependency_type`.
    pub sample_dependency_type_count: i64,
    /// The subscription the cross-sub edges target (SUB_B).
    pub cross_sub_target: Uuid,
}

/// INSERT a small deterministic `synthetic.violations` + `synthetic.dependencies` set
/// against EXISTING `seed_fixture` resource ids/subscriptions. MUST be called AFTER
/// `seed_fixture` (the violations reference its resource ids). Applies `sql/002` only via
/// `seed_fixture` (already run); this helper INSERTs rows only. Returns the ground-truth
/// [`SimSeed`]. Leaves `seed_fixture` / `FixtureCounts` untouched (scoped-helper contract).
pub async fn seed_sim_rows(pool: &PgPool) -> SimSeed {
    // Plan 14-05 (D-14): apply sql/007 idempotently so `synthetic.tenant.profile_name`
    // exists for the summary handler read (same pattern as `seed_drift_rows` re-applying
    // sql/006). `seed_fixture` inserts the tenant WITHOUT profile_name (it stays NULL) —
    // this helper only provisions the column, it never edits `seed_fixture`/`FixtureCounts`.
    let sql_007 = include_str!("../../../sql/007_web_metadata.sql");
    pool.execute_unchecked(sql_007).await;

    // Existing dense-RG resource ids (seed_fixture inserts res-0000..res-0109 under SUB_A).
    let dense = |name: &str| {
        format!(
            "/subscriptions/{SUB_A}/resourceGroups/{DENSE_RG_NAME}/providers/Microsoft.Storage/storageAccounts/{name}"
        )
    };
    let flt = |ty: &str, name: &str| {
        format!("/subscriptions/{SUB_A}/resourceGroups/{FILTER_RG_NAME}/providers/{ty}/{name}")
    };

    // Violations: 6 rows. STORAGE_NO_ENCRYPTION appears twice (code ground truth = 2);
    // NESTED_RESOURCE_ID carries two violations (resource ground truth = 2). Codes are
    // UPPER_SNAKE, severities Title case, detail is a {field, observed} JSONB passthrough.
    struct ViolSeed {
        resource_id: String,
        code: &'static str,
        severity: &'static str,
        detail: &'static str,
    }
    let violations = vec![
        ViolSeed {
            resource_id: NESTED_RESOURCE_ID.to_string(),
            code: "STORAGE_NO_ENCRYPTION",
            severity: "High",
            detail: r#"{"field":"encryption.services.blob.enabled","observed":false}"#,
        },
        ViolSeed {
            resource_id: NESTED_RESOURCE_ID.to_string(),
            code: "TAG_MISSING_OWNER",
            severity: "Low",
            detail: r#"{"field":"tags.owner","observed":null}"#,
        },
        ViolSeed {
            resource_id: dense("res-0000"),
            code: "PUBLIC_IP_EXPOSED",
            severity: "Medium",
            detail: r#"{"field":"publicNetworkAccess","observed":"Enabled"}"#,
        },
        ViolSeed {
            resource_id: flt(FILTER_TYPE_STORAGE, "flt-0000"),
            code: "STORAGE_NO_ENCRYPTION",
            severity: "High",
            detail: r#"{"field":"encryption.services.blob.enabled","observed":false}"#,
        },
        ViolSeed {
            resource_id: flt(FILTER_TYPE_VNET, "flt-0002"),
            code: "NSG_ANY_ANY_INBOUND",
            severity: "High",
            detail: r#"{"field":"securityRules[0].sourceAddressPrefix","observed":"*"}"#,
        },
        ViolSeed {
            resource_id: MIXED_CASE_RESOURCE_ID.to_string(),
            code: "KEYVAULT_NO_PURGE_PROTECTION",
            severity: "Medium",
            detail: r#"{"field":"properties.enablePurgeProtection","observed":null}"#,
        },
    ];
    for v in &violations {
        sqlx::query(
            r#"INSERT INTO synthetic.violations (resource_id, violation_type, severity, detail)
               VALUES ($1, $2, $3, $4::jsonb)"#,
        )
        .bind(&v.resource_id)
        .bind(v.code)
        .bind(v.severity)
        .bind(v.detail)
        .execute(pool)
        .await
        .expect("insert violation");
    }

    // Dependencies: 6 rows. Three cross-sub edges (SUB_A → SUB_B) so `crossSubscription`
    // and the source-OR-target subscription filter are provable; vnet-peering appears
    // twice (type ground truth = 2). Types are lower-hyphen.
    struct DepSeed {
        dep_type: &'static str,
        source_resource_id: String,
        target_resource_id: String,
        source_sub: Uuid,
        target_sub: Uuid,
    }
    let dependencies = vec![
        // cross-sub
        DepSeed {
            dep_type: "vnet-peering",
            source_resource_id: dense("res-0000"),
            target_resource_id: SIM_SUB_B_RESOURCE_ID.to_string(),
            source_sub: SUB_A,
            target_sub: SUB_B,
        },
        // intra-sub
        DepSeed {
            dep_type: "shared-keyvault",
            source_resource_id: flt(FILTER_TYPE_STORAGE, "flt-0000"),
            target_resource_id: dense("res-0002"),
            source_sub: SUB_A,
            target_sub: SUB_A,
        },
        // cross-sub
        DepSeed {
            dep_type: "log-analytics",
            source_resource_id: dense("res-0003"),
            target_resource_id: SIM_SUB_B_RESOURCE_ID.to_string(),
            source_sub: SUB_A,
            target_sub: SUB_B,
        },
        // intra-sub
        DepSeed {
            dep_type: "private-endpoint",
            source_resource_id: flt(FILTER_TYPE_VNET, "flt-0002"),
            target_resource_id: dense("res-0004"),
            source_sub: SUB_A,
            target_sub: SUB_A,
        },
        // cross-sub
        DepSeed {
            dep_type: "shared-acr",
            source_resource_id: dense("res-0005"),
            target_resource_id: SIM_SUB_B_RESOURCE_ID.to_string(),
            source_sub: SUB_A,
            target_sub: SUB_B,
        },
        // intra-sub (second vnet-peering → type count = 2)
        DepSeed {
            dep_type: "vnet-peering",
            source_resource_id: NESTED_RESOURCE_ID.to_string(),
            target_resource_id: dense("res-0002"),
            source_sub: SUB_A,
            target_sub: SUB_A,
        },
    ];
    for d in &dependencies {
        sqlx::query(
            r#"INSERT INTO synthetic.dependencies
                   (dependency_type, source_resource_id, target_resource_id,
                    source_subscription, target_subscription)
               VALUES ($1, $2, $3, $4, $5)"#,
        )
        .bind(d.dep_type)
        .bind(&d.source_resource_id)
        .bind(&d.target_resource_id)
        .bind(d.source_sub)
        .bind(d.target_sub)
        .execute(pool)
        .await
        .expect("insert dependency");
    }

    let cross_sub_dependency_count = dependencies
        .iter()
        .filter(|d| d.source_sub != d.target_sub)
        .count() as i64;
    let sample_code_count = violations
        .iter()
        .filter(|v| v.code == "STORAGE_NO_ENCRYPTION")
        .count() as i64;
    let multi_violation_resource_count = violations
        .iter()
        .filter(|v| v.resource_id.as_str() == NESTED_RESOURCE_ID)
        .count() as i64;
    let sample_dependency_type_count = dependencies
        .iter()
        .filter(|d| d.dep_type == "vnet-peering")
        .count() as i64;

    SimSeed {
        violation_count: violations.len() as i64,
        dependency_count: dependencies.len() as i64,
        cross_sub_dependency_count,
        sub: SUB_A,
        multi_violation_resource_id: NESTED_RESOURCE_ID.to_string(),
        multi_violation_resource_count,
        sample_code: "STORAGE_NO_ENCRYPTION".to_string(),
        sample_code_count,
        sample_severity: "High".to_string(),
        sample_dependency_type: "vnet-peering".to_string(),
        sample_dependency_type_count,
        cross_sub_target: SUB_B,
    }
}

/// Build a fresh ephemeral RS256 signer for the fixture tenant, wrapped in the
/// `Arc` that `AppState.signer` expects. Each call mints a new in-memory key
/// (D-08); tests that only exercise the any-Bearer/OFF path don't depend on the
/// key, so a throwaway per-builder signer is correct and cheap enough.
pub fn test_signer() -> std::sync::Arc<tenantless_server::jwt::JwtSigner> {
    std::sync::Arc::new(
        tenantless_server::jwt::JwtSigner::ephemeral(&TENANT_ID).expect("build test signer"),
    )
}

/// Drive the router in-process via `tower::ServiceExt::oneshot`, returning the
/// status and parsed JSON body. `bearer` injects an `Authorization: Bearer <tok>`
/// header when `Some`.
pub async fn request(
    app: Router,
    method: &str,
    uri: &str,
    bearer: Option<&str>,
) -> (StatusCode, serde_json::Value) {
    let mut builder = Request::builder().method(method).uri(uri);
    if let Some(tok) = bearer {
        builder = builder.header("Authorization", format!("Bearer {tok}"));
    }
    let req = builder.body(Body::empty()).expect("build request");

    let resp = app.oneshot(req).await.expect("oneshot");
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .expect("collect body");
    let json: serde_json::Value = if bytes.is_empty() {
        serde_json::Value::Null
    } else {
        serde_json::from_slice(&bytes).expect("parse json body")
    };
    (status, json)
}

/// Drive the router with a JSON request body (the Cost Management Query is a POST).
/// Sets `content-type: application/json` and injects an `Authorization: Bearer <tok>`
/// header when `bearer` is `Some`. Returns the status and parsed JSON body.
pub async fn request_json(
    app: Router,
    method: &str,
    uri: &str,
    bearer: Option<&str>,
    body: &serde_json::Value,
) -> (StatusCode, serde_json::Value) {
    let mut builder = Request::builder()
        .method(method)
        .uri(uri)
        .header("content-type", "application/json");
    if let Some(tok) = bearer {
        builder = builder.header("Authorization", format!("Bearer {tok}"));
    }
    let payload = serde_json::to_vec(body).expect("serialize request body");
    let req = builder.body(Body::from(payload)).expect("build request");

    let resp = app.oneshot(req).await.expect("oneshot");
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .expect("collect body");
    let json: serde_json::Value = if bytes.is_empty() {
        serde_json::Value::Null
    } else {
        serde_json::from_slice(&bytes).expect("parse json body")
    };
    (status, json)
}

/// Small extension so `seed_fixture` can run multi-statement migration files.
trait ExecuteUnchecked {
    async fn execute_unchecked(&self, sql: &str);
}

impl ExecuteUnchecked for PgPool {
    async fn execute_unchecked(&self, sql: &str) {
        use sqlx::Executor;
        self.execute(sql).await.expect("apply migration sql");
    }
}
