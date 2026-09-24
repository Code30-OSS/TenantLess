//! Canonical ARM-ID identity on the read + write seams — the ASCII-only fold, end to end.
//!
//! The identity contract folds ASCII `A-Z -> a-z` and NOTHING else. Two ids that differ only
//! in ASCII casing are the SAME resource; two ids that differ in a NON-ASCII letter's case
//! (`À` vs `à`) are DIFFERENT resources — exactly as PostgreSQL `synthetic.arm_id_key`, Rust
//! `arm_id::arm_id_key` and Python `tenantless.identity.arm_id_key` agree on the shared KAT
//! corpus. A locale-aware `lower()` would silently merge the non-ASCII variants, so these
//! proofs pin that every stateful lookup went through the fold:
//!
//! * resource detail (`arm_id_key(id) = arm_id_key($1)`),
//! * resource-group detail (same, RG kind),
//! * the RG-scoped resource list (`ascii_fold(resource_group_name) = ascii_fold($4)`),
//! * the served `id` echoes the stored raw casing verbatim (never rewritten).
//!
//! Harness mirrors `write_lifecycle.rs`: ephemeral testcontainers Postgres, the bare first-boot
//! overlay substrate, the real `build_router`, `tower::ServiceExt::oneshot`. DB-gated.

mod common;

use axum::{
    Router,
    body::Body,
    http::{Request, StatusCode},
};
use serde_json::Value;
use sqlx::Executor;
use sqlx::PgPool;
use tenantless_server::{build_router, metrics::Metrics, state::AppState};
use testcontainers_modules::{postgres, testcontainers::runners::AsyncRunner};
use tower::ServiceExt;

const SUB: &str = "11111111-1111-1111-1111-111111111111";
/// A resource-group name carrying a NON-ASCII uppercase letter.
const RG_NON_ASCII: &str = "RG-Àlpha";

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

/// Percent-encode every non-unreserved byte of a path so a non-ASCII id survives the URI.
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

async fn get(app: &Router, uri: &str) -> (StatusCode, Value) {
    let req = Request::builder()
        .method("GET")
        .uri(pct(uri))
        .header("Authorization", "Bearer x")
        .body(Body::empty())
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

/// Boot-shaped provisioning (overlay substrate, identity fold + cutover, resolver) plus a
/// tenant / subscription / the non-ASCII resource group / one baseline resource in it.
async fn seed(pool: &PgPool) -> String {
    common::seed_overlay_first_boot(pool).await;
    tenantless_server::ensure_arm_resolver_schema(pool)
        .await
        .expect("ensure_arm_resolver_schema");
    let sub = uuid::Uuid::parse_str(SUB).unwrap();
    pool.execute(sqlx::query(
        "INSERT INTO synthetic.tenant (tenant_id, display_name, profile_version, scale_params) \
         VALUES ('00000000-0000-0000-0000-000000000000', 't', '1.0', '{}'::jsonb)",
    ))
    .await
    .expect("tenant");
    pool.execute(
        sqlx::query(
            "INSERT INTO synthetic.subscriptions \
                 (subscription_id, tenant_id, display_name, state, archetype, tags, \
                  authorization_source, spending_limit) \
             VALUES ($1, '00000000-0000-0000-0000-000000000000', 'sub', 'Enabled', 'prod', \
                     '{}'::jsonb, 'RoleBased', 'Off')",
        )
        .bind(sub),
    )
    .await
    .expect("subscription");
    pool.execute(
        sqlx::query(
            "INSERT INTO synthetic.resource_groups \
                 (id, subscription_id, name, location, template_type, tags, provisioning_state) \
             VALUES ($1, $2, $3, 'eastus', 'network', '{}'::jsonb, 'Succeeded')",
        )
        .bind(format!(
            "/subscriptions/{SUB}/resourceGroups/{RG_NON_ASCII}"
        ))
        .bind(sub)
        .bind(RG_NON_ASCII),
    )
    .await
    .expect("resource group");
    let id = format!(
        "/subscriptions/{SUB}/resourceGroups/{RG_NON_ASCII}/providers/\
         Microsoft.Storage/storageAccounts/ÀccountX"
    );
    pool.execute(
        sqlx::query(
            "INSERT INTO synthetic.resources \
                 (id, subscription_id, resource_group_name, name, type, location, tags, sku, \
                  kind, properties, provisioning_state, managed_by) \
             VALUES ($1, $2, $3, 'ÀccountX', 'Microsoft.Storage/storageAccounts', 'eastus', \
                     '{}'::jsonb, NULL, NULL, '{}'::jsonb, 'Succeeded', NULL)",
        )
        .bind(&id)
        .bind(sub)
        .bind(RG_NON_ASCII),
    )
    .await
    .expect("baseline resource");
    id
}

/// Precondition for every proof below: the database's own locale `lower()` DOES merge the
/// non-ASCII variants (otherwise the proofs could pass vacuously on a C-locale server).
async fn assert_locale_lower_merges_non_ascii(pool: &PgPool) {
    let merged: bool = sqlx::query_scalar("SELECT lower('À') = lower('à')")
        .fetch_one(pool)
        .await
        .expect("locale probe");
    assert!(
        merged,
        "precondition: the fixture DB's locale lower() must fold À/à together, so a \
         lower()-based lookup would (wrongly) merge them"
    );
}

#[tokio::test]
async fn resource_detail_folds_ascii_case_only_and_serves_raw_id() {
    let (pool, _c) = start_pg().await;
    let id = seed(&pool).await;
    assert_locale_lower_merges_non_ascii(&pool).await;
    let app = router(pool);

    // Given a baseline resource `.../storageAccounts/ÀccountX`
    // When it is fetched with ONLY the ASCII casing changed
    let ascii_variant = format!(
        "/subscriptions/{SUB}/resourceGroups/rg-Àlpha/providers/\
         microsoft.storage/STORAGEACCOUNTS/Àccountx"
    );
    let (status, body) = get(&app, &ascii_variant).await;
    // Then it resolves, and the served id is the stored raw casing verbatim
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

    // When it is fetched with the NON-ASCII letter's case changed (À -> à)
    let non_ascii_variant = format!(
        "/subscriptions/{SUB}/resourceGroups/{RG_NON_ASCII}/providers/\
         Microsoft.Storage/storageAccounts/àccountX"
    );
    let (status, body) = get(&app, &non_ascii_variant).await;
    // Then it is a DIFFERENT identity: 404, never the À resource
    assert_eq!(
        status,
        StatusCode::NOT_FOUND,
        "àccountX is a distinct identity from ÀccountX: {body}"
    );
}

#[tokio::test]
async fn resource_group_detail_folds_ascii_case_only() {
    let (pool, _c) = start_pg().await;
    seed(&pool).await;
    assert_locale_lower_merges_non_ascii(&pool).await;
    let app = router(pool);

    // Given the resource group `RG-Àlpha`
    // When fetched with only ASCII casing changed -> resolves, raw id served
    let (status, body) = get(
        &app,
        &format!("/subscriptions/{SUB}/resourceGroups/rg-Àlpha"),
    )
    .await;
    assert_eq!(
        status,
        StatusCode::OK,
        "ASCII-case RG variant resolves: {body}"
    );
    assert_eq!(
        body["id"],
        Value::String(format!(
            "/subscriptions/{SUB}/resourceGroups/{RG_NON_ASCII}"
        )),
        "served RG id is the raw casing"
    );

    // When fetched with the non-ASCII letter's case changed -> distinct identity, 404
    let (status, body) = get(
        &app,
        &format!("/subscriptions/{SUB}/resourceGroups/RG-àlpha"),
    )
    .await;
    assert_eq!(
        status,
        StatusCode::NOT_FOUND,
        "RG-àlpha is a distinct identity from RG-Àlpha: {body}"
    );
}

#[tokio::test]
async fn rg_scoped_list_folds_rg_name_ascii_case_only() {
    let (pool, _c) = start_pg().await;
    let id = seed(&pool).await;
    assert_locale_lower_merges_non_ascii(&pool).await;
    let app = router(pool);

    // Given one resource in `RG-Àlpha`
    // When the RG-scoped list is requested with only ASCII casing changed -> it is listed
    let (status, body) = get(
        &app,
        &format!("/subscriptions/{SUB}/resourceGroups/rg-ÀLPHA/resources"),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    let ids: Vec<&str> = body["value"]
        .as_array()
        .expect("value array")
        .iter()
        .map(|r| r["id"].as_str().unwrap())
        .collect();
    assert_eq!(
        ids,
        vec![id.as_str()],
        "ASCII-case RG variant lists the resource"
    );

    // When requested with the non-ASCII letter's case changed -> a different RG: empty
    let (status, body) = get(
        &app,
        &format!("/subscriptions/{SUB}/resourceGroups/RG-àlpha/resources"),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        body["value"].as_array().expect("value array").len(),
        0,
        "RG-àlpha is a distinct RG name from RG-Àlpha: {body}"
    );
}
