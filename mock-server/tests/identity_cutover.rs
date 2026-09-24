//! Canonical ARM-ID identity CUTOVER — the boot-time migration that re-derives the overlay
//! identity CHECK onto `synthetic.arm_id_key`, gated by the fail-loud identity audit.
//!
//! Boot order under test: `ensure_arm_id_key_schema` (sql/011) → identity audit (only while
//! the cutover is still pending) → `ensure_arm_id_identity_cutover_schema` (sql/012,
//! pg_constraint-conditional) → `ensure_arm_resolver_schema` (sql/010). Proves:
//!   * the CHECK is re-derived from `lower(id)` to `arm_id_key(id)`, and a re-run is a NO-OP
//!     (the constraint is not dropped and re-added — its oid is unchanged);
//!   * the overlay structural inventory accepts the re-derived CHECK on the next boot;
//!   * the audit fails LOUD (naming only ids) on a divergent overlay row or baseline id, and the
//!     migration itself refuses to convert a divergent overlay (the CHECK stays untouched);
//!   * after the cutover a non-ASCII write round-trips under the ASCII-only identity: the
//!     served id is the raw request casing, an ASCII-case variant resolves, a non-ASCII-case
//!     variant is a distinct resource, and the DELETE cascade sweeps only true descendants.
//!
//! Harness mirrors `write_delete_cascade.rs` (ephemeral testcontainers Postgres, real router).

mod common;

use axum::{
    Router,
    body::Body,
    http::{Request, StatusCode},
};
use serde_json::{Value, json};
use sqlx::Executor;
use sqlx::PgPool;
use tenantless_server::{build_router, metrics::Metrics, state::AppState};
use testcontainers_modules::{postgres, testcontainers::runners::AsyncRunner};
use tower::ServiceExt;

const SUB: &str = "11111111-1111-1111-1111-111111111111";
const RG: &str = "rg-cutover";

async fn start_pg() -> (PgPool, testcontainers::ContainerAsync<postgres::Postgres>) {
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
    (pool, container)
}

/// A PRE-cutover volume: base schema + the overlay substrate with its original `lower(id)`
/// CHECK, and the fold functions — exactly what an upgraded volume looks like before the
/// cutover migration runs.
async fn pre_cutover_volume(pool: &PgPool) {
    common::seed_empty_tenant(pool).await;
    tenantless_server::ensure_arm_overlay_schema(pool)
        .await
        .expect("ensure_arm_overlay_schema");
    tenantless_server::ensure_arm_id_key_schema(pool)
        .await
        .expect("ensure_arm_id_key_schema");
}

async fn id_check(pool: &PgPool) -> (u32, String) {
    let (oid, def): (sqlx::postgres::types::Oid, String) = sqlx::query_as(
        "SELECT oid, pg_get_constraintdef(oid) FROM pg_constraint \
         WHERE conrelid = 'synthetic.arm_overlay'::regclass \
           AND contype = 'c' AND conname = 'ck_arm_overlay_id_lower'",
    )
    .fetch_one(pool)
    .await
    .expect("ck_arm_overlay_id_lower present");
    (oid.0, def)
}

async fn seed_scope(pool: &PgPool) {
    let sub = uuid::Uuid::parse_str(SUB).unwrap();
    pool.execute(sqlx::query(
        "INSERT INTO synthetic.tenant (tenant_id, display_name, profile_version, scale_params) \
         VALUES ('00000000-0000-0000-0000-000000000000', 't', '1.0', '{}'::jsonb) \
         ON CONFLICT DO NOTHING",
    ))
    .await
    .expect("tenant");
    pool.execute(
        sqlx::query(
            "INSERT INTO synthetic.subscriptions \
                 (subscription_id, tenant_id, display_name, state, archetype, tags, \
                  authorization_source, spending_limit) \
             VALUES ($1, '00000000-0000-0000-0000-000000000000', 'sub', 'Enabled', 'prod', \
                     '{}'::jsonb, 'RoleBased', 'Off') ON CONFLICT DO NOTHING",
        )
        .bind(sub),
    )
    .await
    .expect("subscription");
    pool.execute(
        sqlx::query(
            "INSERT INTO synthetic.resource_groups \
                 (id, subscription_id, name, location, template_type, tags, provisioning_state) \
             VALUES ($1, $2, $3, 'eastus', 'network', '{}'::jsonb, 'Succeeded') \
             ON CONFLICT DO NOTHING",
        )
        .bind(format!("/subscriptions/{SUB}/resourceGroups/{RG}"))
        .bind(sub)
        .bind(RG),
    )
    .await
    .expect("resource group");
}

fn id_for(tail: &str) -> String {
    format!("/subscriptions/{SUB}/resourceGroups/{RG}/providers/{tail}")
}

/// Percent-encode every non-unreserved byte so a non-ASCII id survives the URI.
fn pct(path: &str) -> String {
    let mut out = String::new();
    for b in path.bytes() {
        if b.is_ascii_alphanumeric() || b"-._~/".contains(&b) {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

async fn call(app: &Router, method: &str, id: &str, body: Option<Value>) -> (StatusCode, Value) {
    let mut builder = Request::builder()
        .method(method)
        .uri(pct(id))
        .header("Authorization", "Bearer x");
    if body.is_some() {
        builder = builder.header("Content-Type", "application/json");
    }
    let req = builder
        .body(
            body.map(|b| Body::from(serde_json::to_vec(&b).unwrap()))
                .unwrap_or_else(Body::empty),
        )
        .expect("build request");
    let resp = app.clone().oneshot(req).await.expect("oneshot");
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .expect("collect body");
    let json = if bytes.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(&bytes).unwrap_or(Value::Null)
    };
    (status, json)
}

fn router(pool: PgPool) -> Router {
    build_router(AppState {
        pool,
        base_url: "http://test".to_string(),
        metrics: Metrics::new(),
        signer: common::test_signer(),
        enforce_auth: false,
        enable_arm_writes: true,
        control: None,
    })
}

#[tokio::test]
async fn cutover_rederives_overlay_check_and_rerun_is_a_noop() {
    let (pool, _c) = start_pg().await;
    // Given an upgraded volume whose overlay CHECK still derives id_lower with lower()
    pre_cutover_volume(&pool).await;
    let (_, before) = id_check(&pool).await;
    assert!(before.contains("lower(id)"), "pre-cutover CHECK: {before}");
    assert!(
        tenantless_server::arm_id_identity_cutover_pending(&pool)
            .await
            .expect("pending probe"),
        "a lower()-derived CHECK means the cutover is pending"
    );

    // When the boot runs the audit and then the cutover migration
    tenantless_server::audit_arm_id_identity(&pool)
        .await
        .expect("clean estate passes the audit");
    tenantless_server::ensure_arm_id_identity_cutover_schema(&pool)
        .await
        .expect("cutover applies");

    // Then the CHECK derives from arm_id_key and the cutover is no longer pending
    let (oid1, after) = id_check(&pool).await;
    assert!(
        after.contains("arm_id_key(id)"),
        "post-cutover CHECK: {after}"
    );
    assert!(
        !tenantless_server::arm_id_identity_cutover_pending(&pool)
            .await
            .expect("pending probe"),
        "cutover applied"
    );

    // And a re-run (every later boot) is a NO-OP: the constraint is not dropped + re-added
    tenantless_server::ensure_arm_id_identity_cutover_schema(&pool)
        .await
        .expect("idempotent re-run");
    let (oid2, again) = id_check(&pool).await;
    assert_eq!(oid1, oid2, "re-run must not drop/re-add the CHECK");
    assert_eq!(after, again);

    // And the next boot's overlay inventory + resolver provisioning accept the new CHECK
    tenantless_server::ensure_arm_overlay_schema(&pool)
        .await
        .expect("overlay inventory accepts the arm_id_key-derived CHECK");
    tenantless_server::ensure_arm_resolver_schema(&pool)
        .await
        .expect("resolver provisions after the cutover");
}

#[tokio::test]
async fn fold_functions_are_parallel_safe_and_match_the_rust_fold() {
    let (pool, _c) = start_pg().await;
    // Given a volume whose boot ensure path applied the fold definitions
    pre_cutover_volume(&pool).await;

    // Then both functions are IMMUTABLE STRICT PARALLEL SAFE in schema `synthetic`
    let rows: Vec<(String, String, String, bool)> = sqlx::query_as(
        "SELECT p.proname::text, p.proparallel::text, p.provolatile::text, p.proisstrict \
         FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace \
         WHERE n.nspname = 'synthetic' AND p.proname IN ('ascii_fold', 'arm_id_key') \
         ORDER BY p.proname",
    )
    .fetch_all(&pool)
    .await
    .expect("pg_proc");
    assert_eq!(rows.len(), 2, "{rows:?}");
    for (name, parallel, volatile, strict) in &rows {
        assert_eq!(parallel, "s", "{name} must be PARALLEL SAFE");
        assert_eq!(volatile, "i", "{name} must be IMMUTABLE");
        assert!(*strict, "{name} must be STRICT");
    }

    // And the PG fold equals the Rust fold on every shared KAT row
    let corpus: Value = serde_json::from_str(include_str!("../../tests/kat/arm_id_kat.json"))
        .expect("KAT corpus is valid JSON");
    for row in corpus.as_array().expect("KAT corpus is an array") {
        let input = row["input"].as_str().expect("input");
        let (key, fold): (String, String) =
            sqlx::query_as("SELECT synthetic.arm_id_key($1), synthetic.ascii_fold($1)")
                .bind(input)
                .fetch_one(&pool)
                .await
                .expect("fold query");
        let rust = tenantless_server::arm_id::arm_id_key(input);
        assert_eq!(key, rust, "PG arm_id_key vs Rust on {input:?}");
        assert_eq!(fold, rust, "PG ascii_fold vs Rust on {input:?}");
        assert_eq!(key, row["key"].as_str().expect("key"), "{input:?}");
    }
}

#[tokio::test]
async fn audit_and_migration_refuse_a_divergent_overlay_row() {
    let (pool, _c) = start_pg().await;
    pre_cutover_volume(&pool).await;
    // Given a pre-cutover overlay row whose stored id_lower was derived by locale lower()
    // (valid under the OLD CHECK) and differs from the ASCII-only arm_id_key
    let divergent = id_for("Microsoft.Storage/storageAccounts/ÀccountZ");
    common::insert_present_overlay_row_with_id(&pool, &divergent, "ÀccountZ").await;

    // When the audit runs it fails LOUD naming the id
    let err = tenantless_server::audit_arm_id_identity(&pool)
        .await
        .expect_err("divergent overlay id must trip the audit");
    assert!(
        err.contains(&divergent),
        "audit names the offending id: {err}"
    );

    // And the migration itself refuses to convert (never silently changes identity)
    tenantless_server::ensure_arm_id_identity_cutover_schema(&pool)
        .await
        .expect_err("the cutover must refuse a divergent overlay");
    let (_, def) = id_check(&pool).await;
    assert!(def.contains("lower(id)"), "CHECK left untouched: {def}");
    let stored: String = sqlx::query_scalar("SELECT id_lower FROM synthetic.arm_overlay")
        .fetch_one(&pool)
        .await
        .expect("row kept");
    assert!(
        stored.contains("àccountz"),
        "stored id_lower never rewritten: {stored}"
    );
}

#[tokio::test]
async fn audit_names_a_divergent_baseline_id() {
    let (pool, _c) = start_pg().await;
    pre_cutover_volume(&pool).await;
    seed_scope(&pool).await;
    let divergent = id_for("Microsoft.Storage/storageAccounts/ÀccountB");
    let sub = uuid::Uuid::parse_str(SUB).unwrap();
    pool.execute(
        sqlx::query(
            "INSERT INTO synthetic.resources \
                 (id, subscription_id, resource_group_name, name, type, location) \
             VALUES ($1, $2, $3, 'ÀccountB', 'Microsoft.Storage/storageAccounts', 'eastus')",
        )
        .bind(&divergent)
        .bind(sub)
        .bind(RG),
    )
    .await
    .expect("baseline resource");
    let err = tenantless_server::audit_arm_id_identity(&pool)
        .await
        .expect_err("divergent baseline id must trip the audit");
    assert!(
        err.contains(&divergent),
        "audit names the offending id: {err}"
    );
}

#[tokio::test]
async fn non_ascii_write_round_trip_after_cutover() {
    let (pool, _c) = start_pg().await;
    // Given a first boot (the shared fixture models the full boot cutover order)
    common::seed_overlay_first_boot(&pool).await;
    tenantless_server::ensure_arm_resolver_schema(&pool)
        .await
        .expect("ensure_arm_resolver_schema");
    seed_scope(&pool).await;
    let app = router(pool.clone());

    // When a resource with a non-ASCII uppercase letter is PUT
    let id = id_for("Microsoft.Storage/storageAccounts/ÀccountW");
    let (status, body) = call(
        &app,
        "PUT",
        &id,
        Some(json!({"properties": {}, "tags": {}})),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED, "PUT creates: {body}");
    assert_eq!(
        body["id"],
        Value::String(id.clone()),
        "PUT echoes the raw casing"
    );

    // Then the overlay key is the ASCII-only fold (non-ASCII letter preserved)
    let key: String =
        sqlx::query_scalar("SELECT id_lower FROM synthetic.arm_overlay WHERE id = $1")
            .bind(&id)
            .fetch_one(&pool)
            .await
            .expect("overlay row");
    assert_eq!(key, tenantless_server::arm_id::arm_id_key(&id));
    assert!(
        key.ends_with("/Àccountw"),
        "ASCII folded, À preserved: {key}"
    );

    // And an ASCII-case variant resolves to the same resource with the raw id served
    let variant = format!(
        "/subscriptions/{SUB}/resourceGroups/RG-CUTOVER/providers/\
         microsoft.storage/storageaccounts/ÀCCOUNTW"
    );
    let (status, body) = call(&app, "GET", &variant, None).await;
    assert_eq!(
        status,
        StatusCode::OK,
        "ASCII-case variant resolves: {body}"
    );
    assert_eq!(
        body["id"],
        Value::String(id.clone()),
        "served id is the raw casing"
    );

    // And a non-ASCII-case variant is a DISTINCT resource
    let other = id_for("Microsoft.Storage/storageAccounts/àccountW");
    let (status, _) = call(&app, "GET", &other, None).await;
    assert_eq!(
        status,
        StatusCode::NOT_FOUND,
        "àccountW is a different identity"
    );

    // When a second PUT uses an ASCII-case variant, the first-write casing stays frozen
    let (status, body) = call(
        &app,
        "PUT",
        &variant,
        Some(json!({"properties": {}, "tags": {}})),
    )
    .await;
    assert_eq!(
        status,
        StatusCode::OK,
        "PUT to an existing identity replaces: {body}"
    );
    assert_eq!(
        body["id"],
        Value::String(id.clone()),
        "raw casing frozen at first write"
    );
}

#[tokio::test]
async fn non_ascii_delete_cascade_sweeps_only_true_descendants() {
    let (pool, _c) = start_pg().await;
    common::seed_overlay_first_boot(&pool).await;
    tenantless_server::ensure_arm_resolver_schema(&pool)
        .await
        .expect("ensure_arm_resolver_schema");
    seed_scope(&pool).await;
    let app = router(pool.clone());

    // Given a server `Àsrv` with a database, and a DIFFERENT server `àsrv` with a database
    let parent = id_for("Microsoft.Sql/servers/Àsrv");
    let child = id_for("Microsoft.Sql/servers/Àsrv/databases/d1");
    let other_parent = id_for("Microsoft.Sql/servers/àsrv");
    let other_child = id_for("Microsoft.Sql/servers/àsrv/databases/d2");
    for id in [&parent, &child, &other_parent, &other_child] {
        let (status, body) =
            call(&app, "PUT", id, Some(json!({"properties": {}, "tags": {}}))).await;
        assert_eq!(status, StatusCode::CREATED, "seed PUT {id}: {body}");
    }

    // When the parent is DELETEd through an ASCII-case variant of its id
    let (status, _) = call(&app, "DELETE", &id_for("microsoft.sql/SERVERS/ÀSRV"), None).await;
    assert_eq!(status, StatusCode::NO_CONTENT);

    // Then the parent and its true descendant are gone
    assert_eq!(
        call(&app, "GET", &parent, None).await.0,
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        call(&app, "GET", &child, None).await.0,
        StatusCode::NOT_FOUND
    );
    // And the non-ASCII-case sibling tree is untouched
    assert_eq!(
        call(&app, "GET", &other_parent, None).await.0,
        StatusCode::OK
    );
    assert_eq!(
        call(&app, "GET", &other_child, None).await.0,
        StatusCode::OK
    );
}

/// Hold a transaction that has redefined the fold functions the way every provisioning path
/// does (the shared fold advisory lock first, then `CREATE OR REPLACE`), and keep it open.
async fn hold_fold_redefinition(pool: &PgPool) -> sqlx::Transaction<'static, sqlx::Postgres> {
    let mut tx = pool.begin().await.expect("holder tx");
    sqlx::query("SELECT pg_advisory_xact_lock(hashtext('synthetic.arm_id_fold'))")
        .execute(&mut *tx)
        .await
        .expect("fold lock");
    sqlx::raw_sql(include_str!("../../sql/011_arm_id_key.sql"))
        .execute(&mut *tx)
        .await
        .expect("holder redefinition");
    tx
}

#[tokio::test]
async fn concurrent_fold_redefinitions_serialize_instead_of_failing() {
    let (pool, _c) = start_pg().await;
    pre_cutover_volume(&pool).await;
    tenantless_server::ensure_arm_resolver_schema(&pool)
        .await
        .expect("resolver provisioned");

    // Given another session mid-way through redefining the fold functions
    let holder = hold_fold_redefinition(&pool).await;

    // When the boot's 011 apply and 010 apply (whose prelude redefines them too) race it
    let (a, b, ()) = tokio::join!(
        tenantless_server::ensure_arm_id_key_schema(&pool),
        tenantless_server::ensure_arm_resolver_schema(&pool),
        async {
            tokio::time::sleep(std::time::Duration::from_millis(800)).await;
            holder.commit().await.expect("holder commits");
        },
    );

    // Then both wait for it and succeed (no "tuple concurrently updated")
    a.expect("011 apply must serialize behind the concurrent redefinition");
    b.expect("010 apply must serialize behind the concurrent redefinition");
}
