//! DB-backed proofs for the ARM overlay/tombstone/revision substrate
//! (`sql/009_arm_overlay.sql` + `ensure_arm_overlay_schema`). Every test spins its OWN
//! ephemeral testcontainers Postgres and provisions a bare base schema via the dedicated
//! `common::seed_overlay_first_boot` harness (NOT `seed_fixture` — project memory: fixture
//! coupling), modelling a valid first boot.
//!
//! Coverage (substrate):
//!   * `overlay_schema_idempotent` — a repeated `ensure_arm_overlay_schema` is a no-op.
//!   * `overlay_first_boot_from_bare` — provision base WITHOUT overlay, ensure (real) then
//!     ensure (idempotent), then an overlay INSERT succeeds.
//!   * `overlay_upgrade_from_008_preserves_data` — a live 001..008 -> 009 upgrade preserves
//!     every pre-existing row (operator requirement: no data loss).
//!   * `overlay_concurrent_boot_idempotent` — >=4 racing `ensure_arm_overlay_schema` tasks
//!     all return Ok and leave EXACTLY ONE of each object (operator requirement).
//!   * `revision_trigger_advances_every_write` — INSERT/UPDATE/delete-marker/resurrect each
//!     get a fresh strictly-greater revision (the BEFORE trigger fires on every write).
//!   * `revision_ignores_caller_supplied_value` — a caller-supplied `revision` on INSERT and
//!     on UPDATE is overwritten by `nextval()` (revision cannot be forged/frozen).
//!   * `revision_survives_truncate_restart_identity` — `TRUNCATE ... RESTART IDENTITY` does
//!     NOT rewind the unowned sequence (forward-constraint proof for later reset/restore).
//!   * `overlay_rowmodel_checks_parameterized` — each NAMED CHECK rejects its own violation
//!     independently, in BOTH invalid directions where applicable.
//!
//! Coverage (structural inventory):
//!   * `overlay_structural_inventory_deep` — the deep structural inventory fails loudly on
//!     each independently-damaged element.

mod common;

use serde_json::json;
use sqlx::PgPool;
use testcontainers_modules::{postgres, testcontainers::runners::AsyncRunner};

/// Start an ephemeral Postgres container and return a connected pool plus the container
/// guard (kept alive for the test's duration). Mirrors the `start_pg` in the other suites.
async fn start_pg() -> (
    PgPool,
    testcontainers::ContainerAsync<postgres::Postgres>,
    String,
) {
    let container = postgres::Postgres::default()
        .start()
        .await
        .expect("start postgres container");
    let host = container.get_host().await.expect("container host");
    let port = container
        .get_host_port_ipv4(5432)
        .await
        .expect("container port");
    let url = format!("postgres://postgres:postgres@{host}:{port}/postgres");
    let pool = PgPool::connect(&url).await.expect("connect pool");
    (pool, container, url)
}

/// A complete, CHECK-valid ARM body for the given canonical `id` and `kind`. A `resource`
/// body carries a non-RG `type`; a `resource_group` body carries the ARM RG constant `type`.
fn valid_body(id: &str, kind: &str) -> serde_json::Value {
    // An RG's served `properties` carries `provisioningState` (ck_..._rg_provisioning_state);
    // a resource requires no provisioningState in the overlay body (it is not part of the served ETag).
    let (type_val, properties) = if kind == "resource_group" {
        (
            "Microsoft.Resources/resourceGroups",
            json!({ "provisioningState": "Succeeded" }),
        )
    } else {
        ("Microsoft.Storage/storageAccounts", json!({}))
    };
    json!({
        "id": id,
        "name": "res-1",
        "type": type_val,
        "location": "eastus",
        "tags": {},
        "properties": properties
    })
}

/// Insert an overlay row letting the BEFORE trigger assign `revision`. Returns the raw
/// `Result` so rejection tests can assert a DB error.
async fn insert_overlay(
    pool: &PgPool,
    id: &str,
    kind: &str,
    source: &str,
    present: bool,
    body: Option<serde_json::Value>,
) -> Result<sqlx::postgres::PgQueryResult, sqlx::Error> {
    sqlx::query(
        "INSERT INTO synthetic.arm_overlay (id_lower, id, target_kind, source, present, body) \
         VALUES ($1, $2, $3, $4, $5, $6)",
    )
    .bind(id.to_lowercase())
    .bind(id)
    .bind(kind)
    .bind(source)
    .bind(present)
    .bind(body)
    .execute(pool)
    .await
}

/// Read the stored revision for an id.
async fn revision_of(pool: &PgPool, id: &str) -> i64 {
    sqlx::query_scalar("SELECT revision FROM synthetic.arm_overlay WHERE id_lower = $1")
        .bind(id.to_lowercase())
        .fetch_one(pool)
        .await
        .expect("read revision")
}

const RES_ID: &str = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/res-1";

// --------------------------------------------------------------------------------------- //
// Idempotency + first boot
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn overlay_schema_idempotent() {
    let (pool, _c, _url) = start_pg().await;
    // The harness itself already calls ensure_arm_overlay_schema twice (real + idempotent).
    common::seed_overlay_first_boot(&pool).await;
    // A further explicit re-apply on the already-provisioned DB is still a clean no-op.
    tenantless_server::ensure_arm_overlay_schema(&pool)
        .await
        .expect("third idempotent ensure_arm_overlay_schema apply");
    // Exactly one table exists.
    let n: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM pg_class WHERE relname = 'arm_overlay' \
         AND relnamespace = 'synthetic'::regnamespace",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(n, 1, "exactly one arm_overlay table after repeated applies");
}

#[tokio::test]
async fn overlay_first_boot_from_bare() {
    let (pool, _c, _url) = start_pg().await;
    common::seed_overlay_first_boot(&pool).await;
    // THEN insert an overlay row — succeeds against the freshly-created substrate.
    insert_overlay(
        &pool,
        RES_ID,
        "resource",
        "user",
        true,
        Some(valid_body(RES_ID, "resource")),
    )
    .await
    .expect("insert overlay row after first boot");
    let count: i64 = sqlx::query_scalar("SELECT count(*) FROM synthetic.arm_overlay")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(count, 1);
}

// --------------------------------------------------------------------------------------- //
// Live 001..008 -> 009 upgrade preserves data (operator requirement)
// --------------------------------------------------------------------------------------- //

async fn apply_raw(pool: &PgPool, sql: &str) {
    sqlx::raw_sql(sql)
        .execute(pool)
        .await
        .expect("apply base sql");
}

#[tokio::test]
async fn overlay_upgrade_from_008_preserves_data() {
    let (pool, _c, _url) = start_pg().await;
    // Provision the FULL base chain 001..008 (INCLUDING 004 cost + 008 rg_lower_index) —
    // a database provisioned before the overlay existed.
    for sql in [
        include_str!("../../sql/001_synthetic_tenant.sql"),
        include_str!("../../sql/002_cross_sub_dependencies.sql"),
        include_str!("../../sql/003_integrity_and_index.sql"),
        include_str!("../../sql/004_cost.sql"),
        include_str!("../../sql/005_identity.sql"),
        include_str!("../../sql/006_drift.sql"),
        include_str!("../../sql/007_web_metadata.sql"),
        include_str!("../../sql/008_rg_lower_index.sql"),
    ] {
        apply_raw(&pool, sql).await;
    }

    // Seed representative rows: a tenant, a subscription, a resource_group, a resource.
    let tenant = uuid::Uuid::from_u128(0xABCD);
    let sub = uuid::Uuid::from_u128(0x1111_2222);
    sqlx::query(
        "INSERT INTO synthetic.tenant (tenant_id, display_name, profile_version, scale_params) \
         VALUES ($1, 'upgrade-test', '1.0', '{}'::jsonb)",
    )
    .bind(tenant)
    .execute(&pool)
    .await
    .expect("insert tenant");
    sqlx::query(
        "INSERT INTO synthetic.subscriptions \
             (subscription_id, tenant_id, display_name, state, archetype, tags, \
              authorization_source, spending_limit) \
         VALUES ($1, $2, 'sub', 'Enabled', 'prod', '{}'::jsonb, 'RoleBased', 'Off')",
    )
    .bind(sub)
    .bind(tenant)
    .execute(&pool)
    .await
    .expect("insert subscription");
    let rg_id = format!("/subscriptions/{sub}/resourceGroups/Rg-Upgrade");
    sqlx::query(
        "INSERT INTO synthetic.resource_groups \
             (id, subscription_id, name, location, template_type, tags, provisioning_state) \
         VALUES ($1, $2, 'Rg-Upgrade', 'eastus', 'network', '{}'::jsonb, 'Succeeded')",
    )
    .bind(&rg_id)
    .bind(sub)
    .execute(&pool)
    .await
    .expect("insert resource group");
    let res_id = format!(
        "/subscriptions/{sub}/resourceGroups/Rg-Upgrade/providers/Microsoft.Storage/storageAccounts/res-up"
    );
    sqlx::query(
        "INSERT INTO synthetic.resources \
             (id, subscription_id, resource_group_name, name, type, location, tags, sku, \
              kind, properties, provisioning_state, managed_by) \
         VALUES ($1, $2, 'Rg-Upgrade', 'res-up', 'Microsoft.Storage/storageAccounts', 'eastus', \
                 '{}'::jsonb, NULL, NULL, '{}'::jsonb, 'Succeeded', NULL)",
    )
    .bind(&res_id)
    .bind(sub)
    .execute(&pool)
    .await
    .expect("insert resource");

    // Capture pre-upgrade state: counts + byte-for-byte (all-column) row snapshots via
    // to_jsonb(row) so a change to ANY column would be detected.
    let counts_before: (i64, i64, i64) = sqlx::query_as(
        "SELECT (SELECT count(*) FROM synthetic.resources), \
                (SELECT count(*) FROM synthetic.resource_groups), \
                (SELECT count(*) FROM synthetic.subscriptions)",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    let res_row_before: serde_json::Value =
        sqlx::query_scalar("SELECT to_jsonb(r.*) FROM synthetic.resources r WHERE id = $1")
            .bind(&res_id)
            .fetch_one(&pool)
            .await
            .unwrap();
    let rg_row_before: serde_json::Value =
        sqlx::query_scalar("SELECT to_jsonb(g.*) FROM synthetic.resource_groups g WHERE id = $1")
            .bind(&rg_id)
            .fetch_one(&pool)
            .await
            .unwrap();

    // The live upgrade.
    tenantless_server::ensure_arm_overlay_schema(&pool)
        .await
        .expect("live 008 -> 009 upgrade");

    // (a) Every pre-existing row is intact — counts unchanged and sampled rows identical.
    let counts_after: (i64, i64, i64) = sqlx::query_as(
        "SELECT (SELECT count(*) FROM synthetic.resources), \
                (SELECT count(*) FROM synthetic.resource_groups), \
                (SELECT count(*) FROM synthetic.subscriptions)",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(
        counts_before, counts_after,
        "table counts must be unchanged by 009"
    );
    let res_row_after: serde_json::Value =
        sqlx::query_scalar("SELECT to_jsonb(r.*) FROM synthetic.resources r WHERE id = $1")
            .bind(&res_id)
            .fetch_one(&pool)
            .await
            .unwrap();
    let rg_row_after: serde_json::Value =
        sqlx::query_scalar("SELECT to_jsonb(g.*) FROM synthetic.resource_groups g WHERE id = $1")
            .bind(&rg_id)
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(
        res_row_before, res_row_after,
        "resource row must be byte-identical after 009"
    );
    assert_eq!(
        rg_row_before, rg_row_after,
        "resource_group row must be byte-identical after 009"
    );

    // (b) The overlay now exists and accepts an insert.
    insert_overlay(
        &pool,
        RES_ID,
        "resource",
        "user",
        true,
        Some(valid_body(RES_ID, "resource")),
    )
    .await
    .expect("arm_overlay accepts an insert after upgrade");
}

// --------------------------------------------------------------------------------------- //
// Concurrent boot idempotency (operator requirement: >=4 racing tasks)
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn overlay_concurrent_boot_idempotent() {
    let (pool, _c, url) = start_pg().await;
    // Provision the base schema WITHOUT the overlay (so the concurrent tasks race the FIRST
    // create), reusing the six base files the harness applies.
    for sql in [
        include_str!("../../sql/001_synthetic_tenant.sql"),
        include_str!("../../sql/002_cross_sub_dependencies.sql"),
        include_str!("../../sql/003_integrity_and_index.sql"),
        include_str!("../../sql/005_identity.sql"),
        include_str!("../../sql/006_drift.sql"),
        include_str!("../../sql/007_web_metadata.sql"),
    ] {
        apply_raw(&pool, sql).await;
    }

    // 6 genuinely-racing OS threads, each with its OWN current-thread runtime AND its OWN
    // independent pool to the same database, all calling ensure_arm_overlay_schema at once.
    // Independent pools avoid cross-runtime pool sharing (the main runtime is blocked on
    // `join`, so it cannot drive a shared pool's background tasks -> PoolTimedOut). OS
    // threads also sidestep the sqlx-transaction `tokio::spawn` HRTB "Executor is not general
    // enough" limitation (rust-lang/rust#100013) — `block_on` imposes no `Send + 'static`
    // bound on the borrowing future. The in-SQL advisory lock serializes the racing applies.
    let mut threads = Vec::new();
    for _ in 0..6 {
        let u = url.clone();
        threads.push(std::thread::spawn(move || {
            let rt = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .expect("build per-thread runtime");
            rt.block_on(async move {
                let p = PgPool::connect(&u).await.expect("connect per-thread pool");
                tenantless_server::ensure_arm_overlay_schema(&p).await
            })
        }));
    }
    for t in threads {
        t.join()
            .expect("thread join")
            .expect("every concurrent ensure_arm_overlay_schema must be Ok");
    }

    // Afterwards EXACTLY ONE of each object exists.
    let tables: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM pg_class WHERE relname = 'arm_overlay' \
         AND relnamespace = 'synthetic'::regnamespace",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(tables, 1, "exactly one arm_overlay table");
    let seqs: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM pg_class WHERE relname = 'arm_overlay_revision_seq' \
         AND relnamespace = 'synthetic'::regnamespace",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(seqs, 1, "exactly one revision sequence");
    let trigs: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_arm_overlay_revision' \
         AND tgrelid = 'synthetic.arm_overlay'::regclass AND NOT tgisinternal",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(trigs, 1, "exactly one revision trigger");
    let checks: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM pg_constraint \
         WHERE conrelid = 'synthetic.arm_overlay'::regclass AND contype = 'c' \
           AND conname LIKE 'ck_arm_overlay_%'",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(checks, 12, "exactly twelve ck_arm_overlay_* constraints");
}

/// `sql/009` must contain NO transaction-scoped statement, so it is honest when
/// Docker's `docker-entrypoint-initdb.d` runs it statement-by-statement under autocommit. The
/// bounded `lock_timeout` + serializing advisory lock live in the provisioning paths instead.
#[test]
fn overlay_sql_file_is_initdb_honest() {
    let sql = include_str!("../../sql/009_arm_overlay.sql");
    // Inspect ACTIVE SQL only — strip `--` comments (the relocation is DOCUMENTED in comments
    // that necessarily name the relocated statements). No `--` appears inside a DDL literal here.
    let active: String = sql
        .lines()
        .map(|l| match l.find("--") {
            Some(i) => &l[..i],
            None => l,
        })
        .collect::<Vec<_>>()
        .join("\n");
    assert!(
        !active.contains("SET LOCAL"),
        "sql/009 active DDL must not contain a transaction-scoped SET LOCAL (relocated to the provisioning paths)"
    );
    assert!(
        !active.contains("pg_advisory_xact_lock"),
        "sql/009 active DDL must not contain a transaction-scoped advisory lock (relocated to the provisioning paths)"
    );
}

/// the trigger is created CONDITIONALLY, so a re-apply must NOT DROP/CREATE it
/// (which would take an ACCESS EXCLUSIVE lock every boot). Identity proof: the trigger's oid
/// is stable across a second `ensure_arm_overlay_schema`.
#[tokio::test]
async fn overlay_trigger_not_recreated_on_reapply() {
    let (pool, _c, _url) = start_pg().await;
    common::seed_overlay_first_boot(&pool).await;
    let oid_before: i64 = sqlx::query_scalar(
        "SELECT oid::int8 FROM pg_trigger WHERE tgrelid = 'synthetic.arm_overlay'::regclass \
         AND tgname = 'trg_arm_overlay_revision' AND NOT tgisinternal",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    tenantless_server::ensure_arm_overlay_schema(&pool)
        .await
        .expect("re-apply ensure_arm_overlay_schema");
    let oid_after: i64 = sqlx::query_scalar(
        "SELECT oid::int8 FROM pg_trigger WHERE tgrelid = 'synthetic.arm_overlay'::regclass \
         AND tgname = 'trg_arm_overlay_revision' AND NOT tgisinternal",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(
        oid_before, oid_after,
        "trigger must not be dropped/recreated on re-apply (conditional CREATE)"
    );
}

// --------------------------------------------------------------------------------------- //
// Revision trigger behaviour
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn revision_trigger_advances_every_write() {
    let (pool, _c, _url) = start_pg().await;
    common::seed_overlay_first_boot(&pool).await;

    // INSERT -> r1 > 0.
    insert_overlay(
        &pool,
        RES_ID,
        "resource",
        "user",
        true,
        Some(valid_body(RES_ID, "resource")),
    )
    .await
    .expect("insert");
    let r1 = revision_of(&pool, RES_ID).await;
    assert!(r1 > 0, "insert revision must be > 0 (got {r1})");

    // UPDATE of the same id_lower -> r2 > r1.
    sqlx::query("UPDATE synthetic.arm_overlay SET source = 'drift' WHERE id_lower = $1")
        .bind(RES_ID.to_lowercase())
        .execute(&pool)
        .await
        .expect("update");
    let r2 = revision_of(&pool, RES_ID).await;
    assert!(
        r2 > r1,
        "update revision {r2} must exceed insert revision {r1}"
    );

    // delete-marker via ON CONFLICT DO UPDATE (present=false, body NULL) -> r3 > r2.
    sqlx::query(
        "INSERT INTO synthetic.arm_overlay (id_lower, id, target_kind, source, present, body) \
         VALUES ($1, $2, 'resource', 'user', false, NULL) \
         ON CONFLICT (id_lower) DO UPDATE SET present = EXCLUDED.present, body = EXCLUDED.body",
    )
    .bind(RES_ID.to_lowercase())
    .bind(RES_ID)
    .execute(&pool)
    .await
    .expect("delete-marker upsert");
    let r3 = revision_of(&pool, RES_ID).await;
    assert!(
        r3 > r2,
        "delete-marker revision {r3} must exceed update revision {r2}"
    );

    // resurrect via ON CONFLICT DO UPDATE (present=true, complete body) -> r4 > r3.
    sqlx::query(
        "INSERT INTO synthetic.arm_overlay (id_lower, id, target_kind, source, present, body) \
         VALUES ($1, $2, 'resource', 'user', true, $3) \
         ON CONFLICT (id_lower) DO UPDATE SET present = EXCLUDED.present, body = EXCLUDED.body",
    )
    .bind(RES_ID.to_lowercase())
    .bind(RES_ID)
    .bind(valid_body(RES_ID, "resource"))
    .execute(&pool)
    .await
    .expect("resurrect upsert");
    let r4 = revision_of(&pool, RES_ID).await;
    assert!(
        r4 > r3,
        "resurrect revision {r4} must exceed delete-marker revision {r3}"
    );
}

#[tokio::test]
async fn revision_ignores_caller_supplied_value() {
    let (pool, _c, _url) = start_pg().await;
    common::seed_overlay_first_boot(&pool).await;

    // INSERT explicitly supplying a sentinel revision — the trigger overwrites it.
    sqlx::query(
        "INSERT INTO synthetic.arm_overlay \
             (id_lower, id, target_kind, source, present, body, revision) \
         VALUES ($1, $2, 'resource', 'user', true, $3, 999999)",
    )
    .bind(RES_ID.to_lowercase())
    .bind(RES_ID)
    .bind(valid_body(RES_ID, "resource"))
    .execute(&pool)
    .await
    .expect("insert with caller-supplied revision");
    let r1 = revision_of(&pool, RES_ID).await;
    assert_ne!(
        r1, 999_999,
        "trigger must overwrite the caller-supplied INSERT revision"
    );
    assert!(
        r1 > 0 && r1 < 999_999,
        "insert revision is a fresh nextval (got {r1})"
    );

    // UPDATE explicitly setting revision := sentinel — the trigger overwrites it too.
    sqlx::query("UPDATE synthetic.arm_overlay SET revision = 999999 WHERE id_lower = $1")
        .bind(RES_ID.to_lowercase())
        .execute(&pool)
        .await
        .expect("update setting caller-supplied revision");
    let r2 = revision_of(&pool, RES_ID).await;
    assert_ne!(
        r2, 999_999,
        "trigger must overwrite the caller-supplied UPDATE revision"
    );
    assert!(
        r2 > r1,
        "update revision {r2} must be a fresh nextval exceeding {r1}"
    );
}

#[tokio::test]
async fn revision_survives_truncate_restart_identity() {
    let (pool, _c, _url) = start_pg().await;
    common::seed_overlay_first_boot(&pool).await;

    // Seed a couple of rows so the sequence has advanced.
    insert_overlay(
        &pool,
        RES_ID,
        "resource",
        "user",
        true,
        Some(valid_body(RES_ID, "resource")),
    )
    .await
    .expect("insert 1");
    let id2 = format!("{RES_ID}-2");
    insert_overlay(
        &pool,
        &id2,
        "resource",
        "user",
        true,
        Some(valid_body(&id2, "resource")),
    )
    .await
    .expect("insert 2");
    let max_before: i64 = sqlx::query_scalar("SELECT max(revision) FROM synthetic.arm_overlay")
        .fetch_one(&pool)
        .await
        .unwrap();

    // RESTART IDENTITY: an UNOWNED sequence is NOT rewound (it only restarts sequences owned
    // by the table's identity/serial columns). Plain TRUNCATE would be a vacuous proof.
    sqlx::query("TRUNCATE synthetic.arm_overlay RESTART IDENTITY")
        .execute(&pool)
        .await
        .expect("truncate restart identity");

    insert_overlay(
        &pool,
        RES_ID,
        "resource",
        "user",
        true,
        Some(valid_body(RES_ID, "resource")),
    )
    .await
    .expect("insert after truncate");
    let rev_after = revision_of(&pool, RES_ID).await;
    assert!(
        rev_after > max_before,
        "revision after TRUNCATE RESTART IDENTITY ({rev_after}) must strictly exceed the \
         pre-truncate max ({max_before}) — the unowned sequence is not rewound"
    );
}

// --------------------------------------------------------------------------------------- //
// Parameterized row-model CHECK rejections (each rejects its own violation independently)
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn overlay_rowmodel_checks_parameterized() {
    let (pool, _c, _url) = start_pg().await;
    common::seed_overlay_first_boot(&pool).await;

    // A fully-valid row inserts cleanly (baseline).
    insert_overlay(
        &pool,
        RES_ID,
        "resource",
        "user",
        true,
        Some(valid_body(RES_ID, "resource")),
    )
    .await
    .expect("a valid row inserts cleanly");

    // Helper: an insert that MUST be rejected by the DB. Uses a distinct id per case so the
    // PK never collides with the baseline row (isolating the CHECK under test).
    async fn must_reject(
        pool: &PgPool,
        label: &str,
        id: &str,
        kind: &str,
        source: &str,
        present: bool,
        body: Option<serde_json::Value>,
    ) {
        let r = insert_overlay(pool, id, kind, source, present, body).await;
        assert!(r.is_err(), "expected rejection for case: {label}");
    }

    // ck_arm_overlay_id_lower: id_lower <> lower(id) — force a mismatched id_lower directly.
    let r = sqlx::query(
        "INSERT INTO synthetic.arm_overlay (id_lower, id, target_kind, source, present, body) \
         VALUES ('not-lower', $1, 'resource', 'user', true, $2)",
    )
    .bind("/Sub/Res-A")
    .bind(valid_body("/Sub/Res-A", "resource"))
    .execute(&pool)
    .await;
    assert!(
        r.is_err(),
        "ck_arm_overlay_id_lower must reject id_lower <> lower(id)"
    );

    // ck_arm_overlay_present_body — direction 1: present=true + NULL body.
    must_reject(
        &pool,
        "present=true+null body",
        "/sub/p1",
        "resource",
        "user",
        true,
        None,
    )
    .await;
    // ck_arm_overlay_present_body — direction 2: present=false + non-NULL body.
    must_reject(
        &pool,
        "present=false+body",
        "/sub/p2",
        "resource",
        "user",
        false,
        Some(valid_body("/sub/p2", "resource")),
    )
    .await;

    // ck_arm_overlay_body_nonempty: body = '{}'.
    must_reject(
        &pool,
        "empty {} body",
        "/sub/e1",
        "resource",
        "user",
        true,
        Some(json!({})),
    )
    .await;

    // ck_arm_overlay_body_id_agree: body->>'id' <> id.
    {
        let mut b = valid_body("/sub/x1", "resource");
        b["id"] = json!("/sub/DIFFERENT");
        must_reject(
            &pool,
            "body id mismatch",
            "/sub/x1",
            "resource",
            "user",
            true,
            Some(b),
        )
        .await;
    }

    // ck_arm_overlay_envelope: present row missing a required field (drop 'location').
    {
        let mut b = valid_body("/sub/env1", "resource");
        b.as_object_mut().unwrap().remove("location");
        must_reject(
            &pool,
            "missing envelope field",
            "/sub/env1",
            "resource",
            "user",
            true,
            Some(b),
        )
        .await;
    }

    // ck_arm_overlay_kind_shape — direction 1: a resource row whose body.type IS the RG const.
    {
        let mut b = valid_body("/sub/k1", "resource");
        b["type"] = json!("Microsoft.Resources/resourceGroups");
        must_reject(
            &pool,
            "resource body with RG type",
            "/sub/k1",
            "resource",
            "user",
            true,
            Some(b),
        )
        .await;
    }
    // ck_arm_overlay_kind_shape — direction 2: a resource_group row whose body.type is NOT the
    // RG const (proves the RG shape bidirectionally, even though RG rows are never written yet).
    {
        let b = valid_body("/sub/k2", "resource"); // non-RG type on a resource_group row
        must_reject(
            &pool,
            "RG row with non-RG type",
            "/sub/k2",
            "resource_group",
            "user",
            true,
            Some(b),
        )
        .await;
    }
    // ...and a resource_group row WITH the RG const type is accepted (the valid RG direction).
    insert_overlay(
        &pool,
        "/sub/k3",
        "resource_group",
        "user",
        true,
        Some(valid_body("/sub/k3", "resource_group")),
    )
    .await
    .expect("a valid resource_group row is accepted (RG shape defined)");

    // ck_arm_overlay_source: source not in {user,drift}.
    must_reject(
        &pool,
        "bad source",
        "/sub/s1",
        "resource",
        "system",
        true,
        Some(valid_body("/sub/s1", "resource")),
    )
    .await;

    // ck_arm_overlay_kind: target_kind not in {resource,resource_group}.
    must_reject(
        &pool,
        "bad target_kind",
        "/sub/tk1",
        "widget",
        "user",
        true,
        Some(valid_body("/sub/tk1", "resource")),
    )
    .await;

    // ---- minimum-complete-snapshot invariants -------------------------------
    // ck_arm_overlay_tags: a present body missing the always-served `tags`.
    {
        let mut b = valid_body("/sub/tg1", "resource");
        b.as_object_mut().unwrap().remove("tags");
        must_reject(
            &pool,
            "missing tags",
            "/sub/tg1",
            "resource",
            "user",
            true,
            Some(b),
        )
        .await;
    }
    // ck_arm_overlay_tags: `tags` present but not an object.
    {
        let mut b = valid_body("/sub/tg2", "resource");
        b["tags"] = json!("nope");
        must_reject(
            &pool,
            "non-object tags",
            "/sub/tg2",
            "resource",
            "user",
            true,
            Some(b),
        )
        .await;
    }
    // ck_arm_overlay_optional_types: `sku` present but not an object.
    {
        let mut b = valid_body("/sub/sk1", "resource");
        b["sku"] = json!("nope");
        must_reject(
            &pool,
            "non-object sku",
            "/sub/sk1",
            "resource",
            "user",
            true,
            Some(b),
        )
        .await;
    }
    // ck_arm_overlay_optional_types: `kind` present but not a string.
    {
        let mut b = valid_body("/sub/kd1", "resource");
        b["kind"] = json!(5);
        must_reject(
            &pool,
            "non-string kind",
            "/sub/kd1",
            "resource",
            "user",
            true,
            Some(b),
        )
        .await;
    }
    // ...but a resource carrying well-typed optional `sku` (object) + `kind` (string) is accepted.
    {
        let mut b = valid_body("/sub/opt-ok", "resource");
        b["sku"] = json!({ "name": "Standard_LRS" });
        b["kind"] = json!("StorageV2");
        insert_overlay(&pool, "/sub/opt-ok", "resource", "user", true, Some(b))
            .await
            .expect("well-typed optional sku/kind is accepted");
    }
    // ck_arm_overlay_rg_provisioning_state: an RG body without properties.provisioningState.
    {
        let mut b = valid_body("/sub/rg-nops", "resource_group");
        b["properties"] = json!({});
        must_reject(
            &pool,
            "RG missing provisioningState",
            "/sub/rg-nops",
            "resource_group",
            "user",
            true,
            Some(b),
        )
        .await;
    }
    // ck_arm_overlay_rg_provisioning_state: provisioningState present but not a string.
    {
        let mut b = valid_body("/sub/rg-badps", "resource_group");
        b["properties"] = json!({ "provisioningState": 7 });
        must_reject(
            &pool,
            "RG non-string provisioningState",
            "/sub/rg-badps",
            "resource_group",
            "user",
            true,
            Some(b),
        )
        .await;
    }

    // ck_arm_overlay_revision_pos: revision <= 0 — disable the trigger to FORCE a 0 through,
    // proving the CHECK is an independent guard behind the trigger.
    sqlx::query("ALTER TABLE synthetic.arm_overlay DISABLE TRIGGER trg_arm_overlay_revision")
        .execute(&pool)
        .await
        .expect("disable trigger");
    let r = sqlx::query(
        "INSERT INTO synthetic.arm_overlay \
             (id_lower, id, target_kind, source, present, body, revision) \
         VALUES ('/sub/rev0', '/sub/rev0', 'resource', 'user', true, $1, 0)",
    )
    .bind(valid_body("/sub/rev0", "resource"))
    .execute(&pool)
    .await;
    assert!(
        r.is_err(),
        "ck_arm_overlay_revision_pos must reject revision <= 0"
    );
    sqlx::query("ALTER TABLE synthetic.arm_overlay ENABLE TRIGGER trg_arm_overlay_revision")
        .execute(&pool)
        .await
        .expect("re-enable trigger");
}

// --------------------------------------------------------------------------------------- //
// deep structural-completeness inventory
// --------------------------------------------------------------------------------------- //

/// Provision a correct overlay schema, assert the inventory passes (no false-positive), then
/// apply `damage` DDL to ONE element and assert the inventory now fails with an error message
/// mentioning `needle`. Each case spins its own container for full independence.
async fn inventory_damage_case(damage: &[&str], needle: &str) {
    let (pool, _c, _url) = start_pg().await;
    common::seed_overlay_first_boot(&pool).await;
    // Baseline: a correctly-provisioned table passes the inventory.
    tenantless_server::arm_overlay_inventory(&pool)
        .await
        .expect("baseline: correctly-provisioned table passes the inventory");
    for sql in damage {
        sqlx::raw_sql(sql)
            .execute(&pool)
            .await
            .unwrap_or_else(|e| panic!("apply damage {sql:?}: {e}"));
    }
    let err = tenantless_server::arm_overlay_inventory(&pool)
        .await
        .expect_err("a damaged element must fail the inventory");
    assert!(
        err.contains(needle),
        "inventory error {err:?} must name the damaged element ({needle:?})"
    );
}

#[tokio::test]
async fn overlay_structural_inventory_deep() {
    // A same-named `CHECK (true)` stub is caught (definitions inspected, not just names).
    inventory_damage_case(
        &[
            "ALTER TABLE synthetic.arm_overlay DROP CONSTRAINT ck_arm_overlay_id_lower",
            "ALTER TABLE synthetic.arm_overlay ADD CONSTRAINT ck_arm_overlay_id_lower CHECK (true)",
        ],
        "ck_arm_overlay_id_lower",
    )
    .await;

    // A wrong column nullability is caught.
    inventory_damage_case(
        &["ALTER TABLE synthetic.arm_overlay ALTER COLUMN revision DROP NOT NULL"],
        "revision",
    )
    .await;

    // A wrong column type is caught (text -> character varying). `source` is chosen because
    // its CHECK definition still contains the 'drift' needle after the cast (so the constraint
    // pass, then the column-type check fires) — proving type detection independently.
    inventory_damage_case(
        &["ALTER TABLE synthetic.arm_overlay ALTER COLUMN source TYPE varchar(512)"],
        "character varying",
    )
    .await;

    // A cycling sequence is caught.
    inventory_damage_case(
        &["ALTER SEQUENCE synthetic.arm_overlay_revision_seq CYCLE"],
        "CYCLE",
    )
    .await;

    // An OWNED sequence is caught (would be rewound by TRUNCATE ... RESTART IDENTITY).
    inventory_damage_case(
        &["ALTER SEQUENCE synthetic.arm_overlay_revision_seq OWNED BY synthetic.arm_overlay.revision"],
        "OWNED",
    )
    .await;

    // A missing revision trigger is caught.
    inventory_damage_case(
        &["DROP TRIGGER trg_arm_overlay_revision ON synthetic.arm_overlay"],
        "trg_arm_overlay_revision",
    )
    .await;

    // A missing id_lower primary key is caught.
    inventory_damage_case(
        &["ALTER TABLE synthetic.arm_overlay DROP CONSTRAINT arm_overlay_pkey"],
        "PRIMARY KEY",
    )
    .await;

    // A STATEMENT-level trigger (row bit clear) is caught — the old check only
    // verified BEFORE/INSERT/UPDATE, so a statement trigger certified as valid.
    inventory_damage_case(
        &[
            "DROP TRIGGER trg_arm_overlay_revision ON synthetic.arm_overlay",
            "CREATE TRIGGER trg_arm_overlay_revision BEFORE INSERT OR UPDATE ON \
             synthetic.arm_overlay FOR EACH STATEMENT \
             EXECUTE FUNCTION synthetic.arm_overlay_set_revision()",
        ],
        "ROW-level",
    )
    .await;

    // A non-positive sequence increment (would not advance revisions) is caught.
    inventory_damage_case(
        &["ALTER SEQUENCE synthetic.arm_overlay_revision_seq INCREMENT BY -1"],
        "increment",
    )
    .await;

    // A COMPOSITE primary key containing id_lower is caught (exact-column check).
    inventory_damage_case(
        &[
            "ALTER TABLE synthetic.arm_overlay DROP CONSTRAINT arm_overlay_pkey",
            "ALTER TABLE synthetic.arm_overlay ADD PRIMARY KEY (id_lower, id)",
        ],
        "exactly (id_lower)",
    )
    .await;

    // THE vacuity defense: a same-named CHECK whose definition still CONTAINS the
    // characteristic needle (`lower(id)`) but is logically vacuous (`true OR ...`). The
    // definition-substring pass is FOOLED; the behavioural probe catches it at write time.
    inventory_damage_case(
        &[
            "ALTER TABLE synthetic.arm_overlay DROP CONSTRAINT ck_arm_overlay_id_lower",
            "ALTER TABLE synthetic.arm_overlay ADD CONSTRAINT ck_arm_overlay_id_lower \
             CHECK (true OR id_lower = lower(id))",
        ],
        "behavioural probe",
    )
    .await;

    // A vacuous revision_pos — the trigger overwrites revision so no probe can
    // force <= 0 — is caught by the EXACT-definition check (the substring "revision > 0" survives).
    inventory_damage_case(
        &[
            "ALTER TABLE synthetic.arm_overlay DROP CONSTRAINT ck_arm_overlay_revision_pos",
            "ALTER TABLE synthetic.arm_overlay ADD CONSTRAINT ck_arm_overlay_revision_pos \
             CHECK (true OR revision > 0)",
        ],
        "ck_arm_overlay_revision_pos",
    )
    .await;

    // A vacuous body_nonempty — an empty `{}` body is also rejected by the
    // envelope/tags CHECKs so no probe isolates it — is caught by the EXACT-definition check.
    inventory_damage_case(
        &[
            "ALTER TABLE synthetic.arm_overlay DROP CONSTRAINT ck_arm_overlay_body_nonempty",
            "ALTER TABLE synthetic.arm_overlay ADD CONSTRAINT ck_arm_overlay_body_nonempty \
             CHECK (true OR body <> '{}'::jsonb)",
        ],
        "ck_arm_overlay_body_nonempty",
    )
    .await;

    // A vacuous optional_types (needle "sku" retained so it passes the
    // substring pass) is caught by the non-string-kind / non-object-sku behavioural probe.
    inventory_damage_case(
        &[
            "ALTER TABLE synthetic.arm_overlay DROP CONSTRAINT ck_arm_overlay_optional_types",
            "ALTER TABLE synthetic.arm_overlay ADD CONSTRAINT ck_arm_overlay_optional_types \
             CHECK (true OR jsonb_typeof(body -> 'sku') = 'object')",
        ],
        "behavioural probe",
    )
    .await;

    // A same-shape trigger (right name, right ROW BEFORE INSERT OR UPDATE flags)
    // wired to a DIFFERENT function is caught by the `tgfoid` identity check — even one that STILL
    // advances the revision via nextval(), so the monotonic probe alone would pass it. This
    // isolates the trigger-FUNCTION verification.
    inventory_damage_case(
        &[
            "CREATE FUNCTION synthetic.arm_overlay_evil_advance() RETURNS trigger AS $evil$ \
             BEGIN NEW.revision := nextval('synthetic.arm_overlay_revision_seq'); RETURN NEW; \
             END $evil$ LANGUAGE plpgsql",
            "DROP TRIGGER trg_arm_overlay_revision ON synthetic.arm_overlay",
            "CREATE TRIGGER trg_arm_overlay_revision BEFORE INSERT OR UPDATE ON \
             synthetic.arm_overlay FOR EACH ROW \
             EXECUTE FUNCTION synthetic.arm_overlay_evil_advance()",
        ],
        "wrong trigger function",
    )
    .await;

    // The trigger still wired to arm_overlay_set_revision() (so the `tgfoid`
    // check PASSES) but whose function body is REPLACED to assign a CONSTANT revision is caught by
    // the monotonic behavioural probe (two writes to one row: the revision does not advance). This
    // isolates the strictly-increasing-revision verification.
    inventory_damage_case(
        &[
            "CREATE OR REPLACE FUNCTION synthetic.arm_overlay_set_revision() RETURNS trigger \
             AS $c$ BEGIN NEW.revision := 1; RETURN NEW; END $c$ LANGUAGE plpgsql",
        ],
        "revision did not advance",
    )
    .await;

    // EXISTENCE-only weakenings that keep the section-1 substring needle but
    // drop the TYPE predicate. Each is caught by a present-but-wrong-type behavioural probe.
    // ck_arm_overlay_tags weakened to `body ? 'tags'` (accepts `tags: []`) — the finding's example.
    inventory_damage_case(
        &[
            "ALTER TABLE synthetic.arm_overlay DROP CONSTRAINT ck_arm_overlay_tags",
            "ALTER TABLE synthetic.arm_overlay ADD CONSTRAINT ck_arm_overlay_tags \
             CHECK (body IS NULL OR body ? 'tags')",
        ],
        "behavioural probe",
    )
    .await;

    // ck_arm_overlay_rg_provisioning_state weakened to key-existence only (accepts a NUMERIC
    // provisioningState) — caught by the RG-provisioningState-as-number probe.
    inventory_damage_case(
        &[
            "ALTER TABLE synthetic.arm_overlay DROP CONSTRAINT ck_arm_overlay_rg_provisioning_state",
            "ALTER TABLE synthetic.arm_overlay ADD CONSTRAINT ck_arm_overlay_rg_provisioning_state \
             CHECK (body IS NULL OR target_kind <> 'resource_group' \
                    OR body -> 'properties' ? 'provisioningState')",
        ],
        "behavioural probe",
    )
    .await;

    // ck_arm_overlay_envelope weakened to check ONLY name=string (keeps the `jsonb_typeof` needle
    // so the substring pass is fooled) — accepts a wrong-typed id/type/location or non-object
    // properties; caught by a present-but-wrong-type envelope probe.
    inventory_damage_case(
        &[
            "ALTER TABLE synthetic.arm_overlay DROP CONSTRAINT ck_arm_overlay_envelope",
            "ALTER TABLE synthetic.arm_overlay ADD CONSTRAINT ck_arm_overlay_envelope \
             CHECK (body IS NULL OR coalesce(jsonb_typeof(body -> 'name') = 'string', false))",
        ],
        "behavioural probe",
    )
    .await;

    // An envelope weakened to make `properties` OPTIONAL — it still rejects a
    // wrong-TYPED properties (so the earlier non-object-properties probe passes) but ACCEPTS a body
    // with NO properties key. Caught by the dedicated missing-properties probe.
    inventory_damage_case(
        &[
            "ALTER TABLE synthetic.arm_overlay DROP CONSTRAINT ck_arm_overlay_envelope",
            "ALTER TABLE synthetic.arm_overlay ADD CONSTRAINT ck_arm_overlay_envelope \
             CHECK (body IS NULL OR ( \
                 coalesce(jsonb_typeof(body -> 'id') = 'string', false) \
                 AND coalesce(jsonb_typeof(body -> 'name') = 'string', false) \
                 AND coalesce(jsonb_typeof(body -> 'type') = 'string', false) \
                 AND coalesce(jsonb_typeof(body -> 'location') = 'string', false) \
                 AND (NOT jsonb_exists(body, 'properties') \
                      OR jsonb_typeof(body -> 'properties') = 'object') \
             ))",
        ],
        "behavioural probe",
    )
    .await;
}
