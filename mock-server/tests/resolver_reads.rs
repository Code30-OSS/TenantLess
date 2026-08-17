//! Read-plane proofs: every ARM read handler
//! resolves through `synthetic.arm_resolved_*` — the drift `drift_deleted_at` oracle is
//! retired and liveness is decided INSIDE the view.
//!
//! These are FULL HTTP proofs: each test seeds a bare base + overlay via
//! `common::seed_overlay_first_boot`, applies `ensure_arm_resolver_schema`, seeds baseline
//! rows + overlay rows of each kind (replace / appear / tombstone / cursor-interleave), then
//! drives the REAL `build_router` seam (the same router `main` serves). The assertions are
//! on the served ARM bodies, so a handler that still read raw `synthetic.resources` would
//! FAIL (it would return the baseline body for a replaced id, still serve a tombstoned id,
//! and honour `drift_deleted_at` instead of the overlay).
//!
//! Companion to `resolver_golden.rs` (the empty-overlay byte-identity proof, which must stay
//! green through the same view swap).

mod common;

use axum::{
    body::Body,
    http::{Request, StatusCode},
};
use serde_json::Value;
use sqlx::Executor;
use sqlx::PgPool;
use tenantless_server::{build_router, metrics::Metrics, state::AppState};
use testcontainers_modules::{postgres, testcontainers::runners::AsyncRunner};
use tower::ServiceExt;

/// Start an ephemeral Postgres container and return a connected pool plus the container
/// guard (kept alive for the test's duration). Mirrors the other suites' `start_pg`.
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

/// Provision a valid first boot for the read plane: base + overlay substrate + resolver views.
async fn seed_reads_first_boot(pool: &PgPool) {
    common::seed_overlay_first_boot(pool).await;
    tenantless_server::ensure_arm_resolver_schema(pool)
        .await
        .expect("ensure_arm_resolver_schema");
}

/// Build the real router over the seeded pool (the SAME `build_router` seam `main` serves).
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

/// Insert the tenant/subscription/RG FK chain once (idempotent via ON CONFLICT DO NOTHING).
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
    let rg_id = format!("/subscriptions/{SUB}/resourceGroups/{rg}");
    pool.execute(
        sqlx::query(
            "INSERT INTO synthetic.resource_groups \
                 (id, subscription_id, name, location, template_type, tags, provisioning_state) \
             VALUES ($1, $2, $3, 'eastus', 'network', '{}'::jsonb, 'Succeeded') \
             ON CONFLICT DO NOTHING",
        )
        .bind(&rg_id)
        .bind(sub)
        .bind(rg),
    )
    .await
    .expect("resource group");
}

/// Insert a baseline resource with an explicit `env` tag and an optional `drift_deleted_at`.
async fn seed_baseline_resource(
    pool: &PgPool,
    rg: &str,
    name: &str,
    env_tag: &str,
    drift_deleted: bool,
) -> String {
    let sub = uuid::Uuid::parse_str(SUB).unwrap();
    let id = res_id(rg, name);
    let deleted = if drift_deleted { "now()" } else { "NULL" };
    pool.execute(
        sqlx::query(&format!(
            "INSERT INTO synthetic.resources \
                 (id, subscription_id, resource_group_name, name, type, location, tags, sku, \
                  kind, properties, provisioning_state, managed_by, drift_deleted_at) \
             VALUES ($1, $2, $3, $4, 'Microsoft.Storage/storageAccounts', 'eastus', \
                     $5::jsonb, NULL, NULL, '{{\"provisioningState\":\"Succeeded\"}}'::jsonb, \
                     'Succeeded', NULL, {deleted})"
        ))
        .bind(&id)
        .bind(sub)
        .bind(rg)
        .bind(name)
        .bind(format!("{{\"env\":\"{env_tag}\"}}")),
    )
    .await
    .expect("baseline resource");
    id
}

/// A complete, CHECK-valid overlay resource body with a distinguishing `env` tag.
fn overlay_body(id: &str, name: &str, env_tag: &str) -> Value {
    serde_json::json!({
        "id": id,
        "name": name,
        "type": "Microsoft.Storage/storageAccounts",
        "location": "westus",
        "tags": { "env": env_tag },
        "properties": { "provisioningState": "Succeeded" }
    })
}

/// Insert an overlay row (the BEFORE trigger assigns `revision`).
async fn insert_overlay(pool: &PgPool, id: &str, present: bool, body: Option<Value>) {
    pool.execute(
        sqlx::query(
            "INSERT INTO synthetic.arm_overlay (id_lower, id, target_kind, source, present, body) \
             VALUES ($1, $2, 'resource', 'drift', $3, $4)",
        )
        .bind(id.to_lowercase())
        .bind(id)
        .bind(present)
        .bind(body),
    )
    .await
    .expect("overlay row");
}

/// GET through the real router, returning the status + parsed JSON body (empty body → Null).
async fn get_json(app: &axum::Router, uri: &str) -> (StatusCode, Value) {
    let req = Request::builder()
        .method("GET")
        .uri(uri)
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

/// Extract the `value[].id` list from an ARM list envelope.
fn ids(list: &Value) -> Vec<String> {
    list.get("value")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .map(|r| r["id"].as_str().unwrap().to_string())
                .collect()
        })
        .unwrap_or_default()
}

/// Walk a keyset-paginated ARM list to exhaustion, following `nextLink`, and return every id
/// visited in order. `base_uri` carries the query string (`?$top=…`).
async fn walk_all_ids(app: &axum::Router, base_uri: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut uri = base_uri.to_string();
    loop {
        let (status, body) = get_json(app, &uri).await;
        assert_eq!(status, StatusCode::OK, "list page must 200 ({uri})");
        out.extend(ids(&body));
        match body.get("nextLink").and_then(|v| v.as_str()) {
            Some(link) => {
                // nextLink is absolute (http://test/...) — strip the base_url to re-request.
                uri = link
                    .strip_prefix("http://test")
                    .expect("nextLink carries the test base_url")
                    .to_string();
            }
            None => break,
        }
    }
    out
}

// --------------------------------------------------------------------------------------- //
// Tombstone (present=false): absent from lists, 404 on detail; drift_deleted_at NOT consulted.
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn tombstone_absent_from_list_and_404_on_detail() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-t").await;

    let live = seed_baseline_resource(&pool, "rg-t", "sa-live", "base", false).await;
    let gone = seed_baseline_resource(&pool, "rg-t", "sa-gone", "base", false).await;
    // Tombstone `gone` (present=false). NOTE: drift_deleted_at is NOT set — the resolver must
    // hide it purely via the overlay tombstone, proving the view (not the oracle) is authority.
    insert_overlay(&pool, &gone, false, None).await;

    let app = seeded_router(pool);

    // rg-scoped list omits the tombstoned id, keeps the live one.
    let (_s, list) = get_json(
        &app,
        &format!("/subscriptions/{SUB}/resourceGroups/rg-t/resources"),
    )
    .await;
    let seen = ids(&list);
    assert!(seen.contains(&live), "live id present: {seen:?}");
    assert!(!seen.contains(&gone), "tombstoned id omitted: {seen:?}");

    // detail on the tombstoned id → 404.
    let (status, _b) = get_json(&app, &gone).await;
    assert_eq!(status, StatusCode::NOT_FOUND, "tombstoned detail is 404");

    // detail on the live id → 200.
    let (status, _b) = get_json(&app, &live).await;
    assert_eq!(status, StatusCode::OK, "live detail is 200");
}

// --------------------------------------------------------------------------------------- //
// Replace (overlay present shadowing a baseline id): list + detail return the OVERLAY body.
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn overlay_replace_returns_overlay_body() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-r").await;

    let id = seed_baseline_resource(&pool, "rg-r", "sa-x", "base", false).await;
    insert_overlay(&pool, &id, true, Some(overlay_body(&id, "sa-x", "drifted"))).await;

    let app = seeded_router(pool);

    // detail returns the overlay representation (drifted tag + westus location), not baseline.
    let (status, body) = get_json(&app, &id).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["tags"]["env"], "drifted", "overlay tags win on detail");
    assert_eq!(
        body["location"], "westus",
        "overlay location wins on detail"
    );

    // rg-scoped list returns the overlay body for the same id (exactly once).
    let (_s, list) = get_json(
        &app,
        &format!("/subscriptions/{SUB}/resourceGroups/rg-r/resources"),
    )
    .await;
    let arr = list["value"].as_array().unwrap();
    let matches: Vec<&Value> = arr
        .iter()
        .filter(|r| r["id"] == Value::String(id.clone()))
        .collect();
    assert_eq!(matches.len(), 1, "the replaced id appears exactly once");
    assert_eq!(
        matches[0]["tags"]["env"], "drifted",
        "overlay tags win in list"
    );
}

// --------------------------------------------------------------------------------------- //
// Appear (overlay-only present, no baseline twin): present in scoped/sub list + detail.
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn overlay_appear_present_in_scoped_list_and_detail() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-appear").await;

    // No baseline row for this id — the overlay is the ONLY source (create-forward shape).
    let id = res_id("rg-appear", "sa-new");
    insert_overlay(
        &pool,
        &id,
        true,
        Some(overlay_body(&id, "sa-new", "appeared")),
    )
    .await;

    let app = seeded_router(pool);

    // detail resolves via the overlay branch (scope derived by the view).
    let (status, body) = get_json(&app, &id).await;
    assert_eq!(status, StatusCode::OK, "overlay-only detail is 200");
    assert_eq!(body["tags"]["env"], "appeared");

    // rg-scoped list includes it (resource_group_name derived from the id).
    let (_s, list) = get_json(
        &app,
        &format!("/subscriptions/{SUB}/resourceGroups/rg-appear/resources"),
    )
    .await;
    assert!(ids(&list).contains(&id), "appear id in rg-scoped list");

    // sub-scoped list includes it (subscription_id derived from the id).
    let all = walk_all_ids(&app, &format!("/subscriptions/{SUB}/resources?$top=100")).await;
    assert!(all.contains(&id), "appear id in sub-scoped list");
}

// --------------------------------------------------------------------------------------- //
// drift_deleted_at is NOT consulted — a baseline row soft-deleted the LEGACY way,
// with NO overlay tombstone, stays LIVE (the old handler would 404/omit it).
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn drift_deleted_at_is_not_consulted() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-legacy").await;

    // drift_deleted_at set, but NO overlay row exists → the resolver still serves it live.
    let id = seed_baseline_resource(&pool, "rg-legacy", "sa-legacy", "base", true).await;

    let app = seeded_router(pool);

    let (status, _b) = get_json(&app, &id).await;
    assert_eq!(
        status,
        StatusCode::OK,
        "a drift_deleted_at row with no overlay tombstone is LIVE (oracle retired)"
    );
    let (_s, list) = get_json(
        &app,
        &format!("/subscriptions/{SUB}/resourceGroups/rg-legacy/resources"),
    )
    .await;
    assert!(
        ids(&list).contains(&id),
        "drift_deleted_at row still listed (drift_deleted_at not consulted)"
    );
}

// --------------------------------------------------------------------------------------- //
// Full-visit pagination with ids interleaving around the cursor across replace/appear/tombstone.
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn full_visit_pagination_interleaved_returns_each_live_id_once() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-page").await;

    // Baseline ids sort by name suffix: sa-a < sa-b < sa-c < sa-d.
    let a = seed_baseline_resource(&pool, "rg-page", "sa-a", "base", false).await;
    let b = seed_baseline_resource(&pool, "rg-page", "sa-b", "base", false).await;
    let c = seed_baseline_resource(&pool, "rg-page", "sa-c", "base", false).await;
    let d = seed_baseline_resource(&pool, "rg-page", "sa-d", "base", false).await;

    // Tombstone b, replace c, appear e (overlay-only, sorts after d).
    insert_overlay(&pool, &b, false, None).await;
    insert_overlay(&pool, &c, true, Some(overlay_body(&c, "sa-c", "drifted"))).await;
    let e = res_id("rg-page", "sa-e");
    insert_overlay(&pool, &e, true, Some(overlay_body(&e, "sa-e", "appeared"))).await;

    let app = seeded_router(pool);

    // Walk with a small page size so the cursor interleaves around the mutations.
    let walked = walk_all_ids(
        &app,
        &format!("/subscriptions/{SUB}/resourceGroups/rg-page/resources?$top=2"),
    )
    .await;

    // Each LIVE id appears exactly once, tombstoned b is absent, order is ascending by id.
    let expected = vec![a.clone(), c.clone(), d.clone(), e.clone()];
    assert_eq!(
        walked, expected,
        "full-visit returns each live id once, ORDER BY id"
    );
    assert!(
        !walked.contains(&b),
        "tombstoned id never appears in a full walk"
    );
}
