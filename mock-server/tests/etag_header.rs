//! Detail-GET `ETag` header proofs: single-resource AND
//! single-resource_group detail GETs emit the correct `b-`/`o-` ETag header; collection
//! bodies stay untouched (no per-item `etag` field, no ETag header).
//!
//! Full HTTP proofs through the real `build_router`: a baseline (not-overlaid) id emits
//! `"b-<64-hex>"` (the served-DTO hash), an overlay-present id emits `"o-<revision>"` (the
//! `arm_overlay.revision`), and a tombstoned / not-found detail is a normal 404 with no ETag.
//! The read-only RG detail route `GET /subscriptions/{sub}/resourceGroups/{rg}` mirrors it.
//! Runs alongside `resolver_golden.rs` (list bodies stay byte-identical).

mod common;

use axum::{
    body::Body,
    http::{HeaderMap, Request, StatusCode, header},
};
use serde_json::Value;
use sqlx::Executor;
use sqlx::PgPool;
use tenantless_server::{build_router, metrics::Metrics, state::AppState};
use testcontainers_modules::{postgres, testcontainers::runners::AsyncRunner};
use tower::ServiceExt;

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

const SUB: &str = "11111111-1111-1111-1111-111111111111";

async fn seed_reads_first_boot(pool: &PgPool) {
    common::seed_overlay_first_boot(pool).await;
    tenantless_server::ensure_arm_resolver_schema(pool)
        .await
        .expect("ensure_arm_resolver_schema");
}

fn seeded_router(pool: PgPool) -> axum::Router {
    build_router(AppState {
        pool,
        base_url: "http://test".to_string(),
        metrics: Metrics::new(),
        signer: common::test_signer(),
        enforce_auth: false,
        control: None,
    })
}

fn res_id(rg: &str, name: &str) -> String {
    format!(
        "/subscriptions/{SUB}/resourceGroups/{rg}/providers/Microsoft.Storage/storageAccounts/{name}"
    )
}

fn rg_id(rg: &str) -> String {
    format!("/subscriptions/{SUB}/resourceGroups/{rg}")
}

async fn seed_scope(pool: &PgPool, rg: &str) {
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
        .bind(rg_id(rg))
        .bind(sub)
        .bind(rg),
    )
    .await
    .expect("resource group");
}

async fn seed_baseline_resource(pool: &PgPool, rg: &str, name: &str) -> String {
    let sub = uuid::Uuid::parse_str(SUB).unwrap();
    let id = res_id(rg, name);
    pool.execute(
        sqlx::query(
            "INSERT INTO synthetic.resources \
                 (id, subscription_id, resource_group_name, name, type, location, tags, sku, \
                  kind, properties, provisioning_state, managed_by) \
             VALUES ($1, $2, $3, $4, 'Microsoft.Storage/storageAccounts', 'eastus', \
                     '{\"env\":\"base\"}'::jsonb, NULL, NULL, \
                     '{\"provisioningState\":\"Succeeded\"}'::jsonb, 'Succeeded', NULL)",
        )
        .bind(&id)
        .bind(sub)
        .bind(rg)
        .bind(name),
    )
    .await
    .expect("baseline resource");
    id
}

async fn insert_overlay(pool: &PgPool, id: &str, kind: &str, present: bool, body: Option<Value>) {
    pool.execute(
        sqlx::query(
            "INSERT INTO synthetic.arm_overlay (id_lower, id, target_kind, source, present, body) \
             VALUES ($1, $2, $3, 'drift', $4, $5)",
        )
        .bind(id.to_lowercase())
        .bind(id)
        .bind(kind)
        .bind(present)
        .bind(body),
    )
    .await
    .expect("overlay row");
}

/// The `arm_overlay.revision` assigned to `id` by the BEFORE trigger.
async fn overlay_revision(pool: &PgPool, id: &str) -> i64 {
    sqlx::query_scalar("SELECT revision FROM synthetic.arm_overlay WHERE id_lower = lower($1)")
        .bind(id)
        .fetch_one(pool)
        .await
        .expect("overlay revision")
}

fn resource_overlay_body(id: &str, name: &str) -> Value {
    serde_json::json!({
        "id": id, "name": name, "type": "Microsoft.Storage/storageAccounts",
        "location": "westus", "tags": { "env": "drifted" },
        "properties": { "provisioningState": "Succeeded" }
    })
}

fn rg_overlay_body(id: &str, name: &str) -> Value {
    serde_json::json!({
        "id": id, "name": name, "type": "Microsoft.Resources/resourceGroups",
        "location": "westus", "tags": { "env": "drifted" },
        "properties": { "provisioningState": "Updating" }
    })
}

/// GET through the real router, returning status + response headers + parsed body.
async fn get_full(app: &axum::Router, uri: &str) -> (StatusCode, HeaderMap, Value) {
    let req = Request::builder()
        .method("GET")
        .uri(uri)
        .header("Authorization", "Bearer x")
        .body(Body::empty())
        .expect("build request");
    let resp = app.clone().oneshot(req).await.expect("oneshot");
    let status = resp.status();
    let headers = resp.headers().clone();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .expect("collect body");
    let json = if bytes.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(&bytes).unwrap_or(Value::Null)
    };
    (status, headers, json)
}

fn etag_of(headers: &HeaderMap) -> Option<String> {
    headers
        .get(header::ETAG)
        .map(|v| v.to_str().expect("etag is valid ascii").to_string())
}

/// Assert a strong, quoted `"b-<64 lowercase hex>"` token.
fn assert_b_token(tok: &str) {
    assert!(
        tok.starts_with("\"b-"),
        "not b-prefixed inside quotes: {tok}"
    );
    assert!(tok.ends_with('"'), "not quoted: {tok}");
    assert!(!tok.starts_with("W/"), "must never be weak: {tok}");
    let hex = &tok[3..tok.len() - 1];
    assert_eq!(hex.len(), 64, "b- hash must be 64 hex chars: {tok}");
    assert!(
        hex.chars()
            .all(|c| c.is_ascii_digit() || ('a'..='f').contains(&c)),
        "b- hash must be lowercase hex: {tok}"
    );
}

// --------------------------------------------------------------------------------------- //
// Resource detail ETag: baseline → b-, overlay present → o-.
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn resource_detail_baseline_emits_b_etag() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-e").await;
    let id = seed_baseline_resource(&pool, "rg-e", "sa-base").await;

    let app = seeded_router(pool);
    let (status, headers, _b) = get_full(&app, &id).await;
    assert_eq!(status, StatusCode::OK);
    let tok = etag_of(&headers).expect("baseline resource detail emits an ETag header");
    assert_b_token(&tok);
}

#[tokio::test]
async fn resource_detail_overlay_emits_o_etag() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-e").await;
    let id = seed_baseline_resource(&pool, "rg-e", "sa-drift").await;
    insert_overlay(
        &pool,
        &id,
        "resource",
        true,
        Some(resource_overlay_body(&id, "sa-drift")),
    )
    .await;
    let rev = overlay_revision(&pool, &id).await;

    let app = seeded_router(pool);
    let (status, headers, body) = get_full(&app, &id).await;
    assert_eq!(status, StatusCode::OK);
    let tok = etag_of(&headers).expect("overlay resource detail emits an ETag header");
    assert_eq!(tok, format!("\"o-{rev}\""), "overlay id → o-<revision>");
    // And the served body is the overlay representation (sanity: the o- branch served overlay).
    assert_eq!(body["tags"]["env"], "drifted");
}

#[tokio::test]
async fn tombstoned_resource_detail_is_404_with_no_etag() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-e").await;
    let id = seed_baseline_resource(&pool, "rg-e", "sa-gone").await;
    insert_overlay(&pool, &id, "resource", false, None).await;

    let app = seeded_router(pool);
    let (status, headers, _b) = get_full(&app, &id).await;
    assert_eq!(status, StatusCode::NOT_FOUND, "tombstone → 404");
    assert!(etag_of(&headers).is_none(), "a 404 carries no ETag header");
}

// --------------------------------------------------------------------------------------- //
// Read-only RG detail route: baseline → b-, overlay present → o-, miss → 404 no ETag.
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn rg_detail_baseline_emits_b_etag() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-base").await;

    let app = seeded_router(pool);
    let (status, headers, body) = get_full(&app, &rg_id("rg-base")).await;
    assert_eq!(status, StatusCode::OK, "RG detail route exists and 200s");
    // Single-object RG body (not a list envelope).
    assert_eq!(body["type"], "Microsoft.Resources/resourceGroups");
    assert_eq!(body["name"], "rg-base");
    let tok = etag_of(&headers).expect("baseline RG detail emits an ETag header");
    assert_b_token(&tok);
}

#[tokio::test]
async fn rg_detail_overlay_emits_o_etag() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-over").await;
    let id = rg_id("rg-over");
    // Exercise the RG overlay branch directly (no product RG writer in P21 — test-only).
    insert_overlay(
        &pool,
        &id,
        "resource_group",
        true,
        Some(rg_overlay_body(&id, "rg-over")),
    )
    .await;
    let rev = overlay_revision(&pool, &id).await;

    let app = seeded_router(pool);
    let (status, headers, body) = get_full(&app, &id).await;
    assert_eq!(status, StatusCode::OK);
    let tok = etag_of(&headers).expect("overlay RG detail emits an ETag header");
    assert_eq!(tok, format!("\"o-{rev}\""), "overlay RG → o-<revision>");
    assert_eq!(body["properties"]["provisioningState"], "Updating");
}

#[tokio::test]
async fn rg_detail_miss_is_404_with_no_etag() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-real").await;

    let app = seeded_router(pool);
    let (status, headers, _b) = get_full(&app, &rg_id("rg-nonexistent")).await;
    assert_eq!(status, StatusCode::NOT_FOUND, "unknown RG detail → 404");
    assert!(etag_of(&headers).is_none(), "a 404 carries no ETag header");
}

// --------------------------------------------------------------------------------------- //
// Collection bodies untouched: no per-item `etag` field, no ETag header on lists.
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn list_responses_carry_no_etag_field_or_header() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-list").await;
    seed_baseline_resource(&pool, "rg-list", "sa-1").await;
    seed_baseline_resource(&pool, "rg-list", "sa-2").await;

    let app = seeded_router(pool);

    // Resource list (sub-scoped): no ETag header, no per-item etag field.
    let (status, headers, body) = get_full(&app, &format!("/subscriptions/{SUB}/resources")).await;
    assert_eq!(status, StatusCode::OK);
    assert!(
        etag_of(&headers).is_none(),
        "resource list carries no ETag header"
    );
    for item in body["value"].as_array().unwrap() {
        assert!(
            item.get("etag").is_none(),
            "no per-item etag field in the resource list"
        );
    }

    // RG list: same.
    let (status, headers, body) =
        get_full(&app, &format!("/subscriptions/{SUB}/resourceGroups")).await;
    assert_eq!(status, StatusCode::OK);
    assert!(
        etag_of(&headers).is_none(),
        "rg list carries no ETag header"
    );
    for item in body["value"].as_array().unwrap() {
        assert!(
            item.get("etag").is_none(),
            "no per-item etag field in the rg list"
        );
    }
}
