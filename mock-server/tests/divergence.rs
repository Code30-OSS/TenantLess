//! ARM-vs-console CONVERGENCE regression (the CLOSED state).
//!
//! An earlier phase migrated ONLY the ARM read plane onto the unified resolver
//! (`synthetic.arm_resolved_resources` = `baseline ∪ overlay(present) − tombstones`),
//! leaving the non-ARM console/search/summary reads in `handlers::sim` on the raw baseline
//! (`synthetic.resources WHERE drift_deleted_at IS NULL`) — a DELIBERATE, one-phase
//! divergence. **This phase CLOSES it**: every `handlers::sim` resource-facing read
//! now resolves through the SAME view, so console presence == resolver liveness on every
//! surface. The drift write plane writes overlay rows/tombstones and NEVER sets
//! `drift_deleted_at` (baseline immutability), so a drifted resource now reads
//! IDENTICALLY on both planes:
//!
//!   * an ARM read (resolved)    → sees the DRIFT (overlay present body / tombstone gone)
//!   * a `/_sim` read (resolved) → sees the SAME DRIFT (no longer the stale baseline)
//!
//! This suite now PINS the CONVERGED state on BOTH sides simultaneously, so a regression in
//! EITHER direction is caught:
//!   * if a `handlers::sim` reader regressed back to raw `synthetic.resources`, the "console
//!     also hides the tombstone / reflects the resolved field" assertions would FAIL (a
//!     re-opened divergence / resolver bypass — which the reader-inventory gate also forbids);
//!   * if an ARM reader regressed to the raw baseline, the "ARM sees the drift" assertions
//!     would FAIL.
//!
//! The raw `synthetic.resources` baseline row stays byte-unmutated under drift — it
//! is simply no longer what the console READS.

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

/// Start an ephemeral Postgres container and return a connected pool + the container guard
/// (kept alive for the test's duration). Mirrors `resolver_reads.rs::start_pg`.
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

/// Provision a valid first boot: base + overlay substrate + resolver views (same as the
/// read-plane suite, so the divergence is measured against the REAL resolved view + the REAL
/// `/_sim` baseline reads).
async fn seed_first_boot(pool: &PgPool) {
    common::seed_overlay_first_boot(pool).await;
    tenantless_server::ensure_arm_resolver_schema(pool)
        .await
        .expect("ensure_arm_resolver_schema");
}

/// Build the FULL runtime router (the SAME `build_router` seam `main` serves) — this INCLUDES
/// the bearer-exempt `/_sim` merge, so the console-plane reads are exercised end-to-end.
fn seeded_router(pool: PgPool) -> axum::Router {
    build_router(AppState {
        pool,
        base_url: "http://test".to_string(),
        metrics: Metrics::new(),
        signer: common::test_signer(),
        enforce_auth: false,
        enable_arm_writes: false,
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

/// Insert a baseline resource with an explicit `env` tag. `drift_deleted_at` is ALWAYS NULL —
/// the whole point of the divergence is that the drift write plane NO LONGER sets it, so `/_sim`'s
/// `drift_deleted_at IS NULL` baseline reads stay stale.
async fn seed_baseline_resource(pool: &PgPool, rg: &str, name: &str, env_tag: &str) -> String {
    let sub = uuid::Uuid::parse_str(SUB).unwrap();
    let id = res_id(rg, name);
    pool.execute(
        sqlx::query(
            "INSERT INTO synthetic.resources \
                 (id, subscription_id, resource_group_name, name, type, location, tags, sku, \
                  kind, properties, provisioning_state, managed_by, drift_deleted_at) \
             VALUES ($1, $2, $3, $4, 'Microsoft.Storage/storageAccounts', 'eastus', \
                     $5::jsonb, NULL, NULL, '{\"provisioningState\":\"Succeeded\"}'::jsonb, \
                     'Succeeded', NULL, NULL)",
        )
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

/// Insert an overlay row (the sql/009 BEFORE trigger assigns `revision`). `present=false`
/// (body None) is a tombstone; `present=true` (body Some) is a present drift snapshot.
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
/// `/_sim` is bearer-exempt, so the `Authorization` header is harmless there and required by
/// the ARM plane.
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

/// The `value[].id` list from an ARM list envelope OR a `/_sim` search envelope (both use the
/// `{ "value": [ { "id": ... } ] }` shape).
fn ids(list: &Value) -> Vec<String> {
    list.get("value")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|r| r["id"].as_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_default()
}

// --------------------------------------------------------------------------------------- //
// CORE (CLOSED): a disappeared (tombstoned) resource is GONE via ARM (resolved) AND via
// `/_sim` (also resolved) — the divergence is closed. Both planes asserted for the SAME id.
// --------------------------------------------------------------------------------------- //

/// The canonical case, now CONVERGED: a drift DISAPPEAR writes a `present=false` overlay
/// tombstone and does NOT set `drift_deleted_at`. BOTH the ARM resolved plane AND the `/_sim`
/// console plane (this phase migrated `handlers::sim` onto the SAME view) hide the id.
#[tokio::test]
async fn tombstoned_resource_is_gone_via_both_arm_and_sim() {
    let (pool, _c) = start_pg().await;
    seed_first_boot(&pool).await;
    seed_scope(&pool, "rg-div").await;

    // Two baseline resources; tombstone one of them (drift DISAPPEAR shape).
    let live = seed_baseline_resource(&pool, "rg-div", "sa-live", "base").await;
    let ghost = seed_baseline_resource(&pool, "rg-div", "sa-ghost", "base").await;
    insert_overlay(&pool, &ghost, false, None).await;

    let app = seeded_router(pool);

    // --- ARM plane (RESOLVED): the tombstoned id is GONE. ---
    // rg-scoped list omits it; detail 404s.
    let (_s, arm_list) = get_json(
        &app,
        &format!("/subscriptions/{SUB}/resourceGroups/rg-div/resources"),
    )
    .await;
    let arm_ids = ids(&arm_list);
    assert!(
        arm_ids.contains(&live),
        "ARM lists the live id: {arm_ids:?}"
    );
    assert!(
        !arm_ids.contains(&ghost),
        "ARM (resolved) HIDES the tombstoned id — the drift is visible: {arm_ids:?}"
    );
    let (arm_detail_status, _b) = get_json(&app, &ghost).await;
    assert_eq!(
        arm_detail_status,
        StatusCode::NOT_FOUND,
        "ARM detail on the tombstoned id is 404 (resolver sees the drift)"
    );

    // --- `/_sim` plane (RESOLVED): the SAME id is ALSO gone. ---
    // search NO LONGER returns it (the console now reads through the resolver)...
    let (sim_search_status, sim_search) = get_json(&app, "/_sim/resources/search?q=sa-ghost").await;
    assert_eq!(sim_search_status, StatusCode::OK, "/_sim search 200s");
    assert!(
        !ids(&sim_search).contains(&ghost),
        "/_sim search NO LONGER returns the tombstoned id — the divergence is CLOSED. \
         If this fails, handlers::sim regressed back to the raw baseline."
    );
    // the live id is still findable on the console.
    let (_s, sim_live) = get_json(&app, "/_sim/resources/search?q=sa-live").await;
    assert!(
        ids(&sim_live).contains(&live),
        "/_sim search still returns the LIVE id"
    );

    // ...and the summary totals count ONLY the live resource (the tombstoned one is excluded).
    let (sim_summary_status, sim_summary) = get_json(&app, "/_sim/summary").await;
    assert_eq!(sim_summary_status, StatusCode::OK, "/_sim summary 200s");
    assert_eq!(
        sim_summary["totals"]["resources"].as_i64(),
        Some(1),
        "/_sim summary counts ONLY the live baseline resource (the tombstone is excluded) — \
         the divergence is CLOSED (console == resolver liveness)"
    );
}

// --------------------------------------------------------------------------------------- //
// Present-drift facet (CLOSED): ARM serves the DRIFTED body, the raw baseline table
// stays byte-STALE, and `/_sim` now reads the DRIFTED value THROUGH the resolver —
// so the console reflects the resolved field, not the stale baseline.
// --------------------------------------------------------------------------------------- //

/// A present drift (replace) overlay shadows a baseline id with a new `env` tag AND a new
/// `location` (westus vs the baseline eastus). ARM detail returns the DRIFTED value; the raw
/// `synthetic.resources` row is byte-unchanged (immutability) but is NO LONGER what the
/// console reads. This phase: `/_sim` resolves through the same view, so the console
/// byLocation reflects the RESOLVED (drifted) location — proving the divergence is closed.
#[tokio::test]
async fn drifted_body_is_reflected_on_both_arm_and_sim() {
    let (pool, _c) = start_pg().await;
    seed_first_boot(&pool).await;
    seed_scope(&pool, "rg-shadow").await;

    let id = seed_baseline_resource(&pool, "rg-shadow", "sa-shadow", "base").await;
    // overlay_body sets location = "westus" (the baseline is "eastus").
    insert_overlay(
        &pool,
        &id,
        true,
        Some(overlay_body(&id, "sa-shadow", "drifted")),
    )
    .await;

    // The raw baseline row is STILL `env=base` / `location=eastus` (never mutated in place).
    let (baseline_env, baseline_loc): (String, String) =
        sqlx::query_as("SELECT tags ->> 'env', location FROM synthetic.resources WHERE id = $1")
            .bind(&id)
            .fetch_one(&pool)
            .await
            .expect("baseline row read");
    assert_eq!(
        baseline_env, "base",
        "the raw synthetic.resources row is byte-stale/unmutated"
    );
    assert_eq!(baseline_loc, "eastus", "baseline location is unmutated");

    let app = seeded_router(pool);

    // --- ARM plane (RESOLVED): serves the DRIFTED value. ---
    let (arm_status, arm_detail) = get_json(&app, &id).await;
    assert_eq!(
        arm_status,
        StatusCode::OK,
        "ARM detail 200s for the drifted id"
    );
    assert_eq!(
        arm_detail["tags"]["env"], "drifted",
        "ARM (resolved) serves the overlay/DRIFTED tag — if this becomes 'base', an ARM \
         reader regressed to raw synthetic.resources (an accidental resolver bypass)"
    );

    // --- `/_sim` plane (RESOLVED): reflects the RESOLVED (drifted) location. ---
    let (sim_status, sim_search) = get_json(&app, "/_sim/resources/search?q=sa-shadow").await;
    assert_eq!(sim_status, StatusCode::OK, "/_sim search 200s");
    assert!(
        ids(&sim_search).contains(&id),
        "/_sim search still returns the (present-drift) id — it is live"
    );
    // The summary byLocation now shows the RESOLVED westus, NOT the stale baseline eastus.
    let (_s, sim_summary) = get_json(&app, "/_sim/summary").await;
    let by_loc = sim_summary["byLocation"].as_array().expect("byLocation[]");
    let loc = |name: &str| {
        by_loc
            .iter()
            .find(|b| b["location"].as_str() == Some(name))
            .and_then(|b| b["count"].as_i64())
    };
    assert_eq!(
        loc("westus"),
        Some(1),
        "/_sim byLocation reflects the RESOLVED (drifted) location — the divergence is CLOSED"
    );
    assert_eq!(
        loc("eastus"),
        None,
        "the stale baseline location is NOT what the console reports (console reads the resolver)"
    );
}
