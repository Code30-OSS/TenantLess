//! Write-GATING proofs (WAUTH-01 / LIFE-09 / D-10 / D-13 / D-16 / D-18).
//!
//! Two postures through the real `build_router`:
//!   * writes DISABLED (default `enable_arm_writes: false`): every write method on a resource
//!     path returns `405 MethodNotAllowed` with `Allow: GET, HEAD` and the ARM envelope body;
//!     a DISABLED write with a MALFORMED body STILL returns `405` (gating precedes parse,
//!     D-16); GET is unaffected.
//!   * writes ENABLED (`enable_arm_writes: true`): a write on a bare `.../resourceGroups/{rg}`
//!     path (no `/providers/…`) STILL returns the ARM `405` envelope + `Allow: GET, HEAD`
//!     (D-13/D-18 — RG CRUD is Phase 24), never axum's implicit 405.
//!
//! DB-gated: requires Docker/PG16; validates on the Linux CI gate.

mod common;

use axum::{
    Router,
    body::Body,
    http::{HeaderMap, Request, StatusCode, header},
};
use serde_json::Value;
use sqlx::PgPool;
use tenantless_server::{build_router, metrics::Metrics, state::AppState};
use testcontainers_modules::{postgres, testcontainers::runners::AsyncRunner};
use tower::ServiceExt;

const SUB: &str = "11111111-1111-1111-1111-111111111111";

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

async fn seed_reads_first_boot(pool: &PgPool) {
    common::seed_overlay_first_boot(pool).await;
    tenantless_server::ensure_arm_resolver_schema(pool)
        .await
        .expect("ensure_arm_resolver_schema");
}

fn router(pool: PgPool, enable_arm_writes: bool) -> Router {
    build_router(AppState {
        pool,
        base_url: "http://test".to_string(),
        metrics: Metrics::new(),
        signer: common::test_signer(),
        enforce_auth: false,
        enable_arm_writes,
        control: None,
    })
}

fn res_id(name: &str) -> String {
    format!(
        "/subscriptions/{SUB}/resourceGroups/rg-1/providers/Microsoft.Storage/storageAccounts/{name}"
    )
}

async fn request(
    app: &Router,
    method: &str,
    uri: &str,
    ct: Option<&str>,
    body: Option<Vec<u8>>,
) -> (StatusCode, HeaderMap, Value) {
    let mut builder = Request::builder()
        .method(method)
        .uri(uri)
        .header("Authorization", "Bearer x");
    if let Some(ct) = ct {
        builder = builder.header("Content-Type", ct);
    }
    let req = builder
        .body(body.map(Body::from).unwrap_or_else(Body::empty))
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

fn allow_of(headers: &HeaderMap) -> Option<String> {
    headers
        .get(header::ALLOW)
        .map(|v| v.to_str().expect("ascii").to_string())
}

/// Assert the ARM 405 envelope + `Allow: GET, HEAD`.
fn assert_arm_405(status: StatusCode, headers: &HeaderMap, body: &Value) {
    assert_eq!(status, StatusCode::METHOD_NOT_ALLOWED, "expected 405");
    assert_eq!(
        allow_of(headers).as_deref(),
        Some("GET, HEAD"),
        "405 must advertise Allow: GET, HEAD"
    );
    assert_eq!(
        body["error"]["code"], "MethodNotAllowed",
        "the ARM envelope body carries code MethodNotAllowed"
    );
}

// --------------------------------------------------------------------------------------- //
// Writes DISABLED → 405 + Allow + ARM envelope on every write method; GET unaffected.
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn disabled_writes_return_arm_405_with_allow() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    let app = router(pool, false);
    let id = res_id("acct-a");
    let valid = serde_json::to_vec(&serde_json::json!({ "properties": {}, "tags": {} })).unwrap();

    for method in ["PUT", "PATCH"] {
        let (s, h, b) = request(
            &app,
            method,
            &id,
            Some("application/json"),
            Some(valid.clone()),
        )
        .await;
        assert_arm_405(s, &h, &b);
    }
    let (s, h, b) = request(&app, "DELETE", &id, None, None).await;
    assert_arm_405(s, &h, &b);
}

#[tokio::test]
async fn disabled_write_with_malformed_body_still_405_not_400() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    let app = router(pool, false);
    let id = res_id("acct-b");

    // A malformed body under DISABLED writes must STILL be a 405, proving the flag check
    // precedes the parse (D-16) — the Bytes body never triggers axum's default 400.
    let (s, h, b) = request(
        &app,
        "PUT",
        &id,
        Some("application/json"),
        Some(b"not json {".to_vec()),
    )
    .await;
    assert_arm_405(s, &h, &b);
    assert_ne!(
        s,
        StatusCode::BAD_REQUEST,
        "gating precedes parse (not a 400)"
    );
}

#[tokio::test]
async fn reads_are_unaffected_when_writes_disabled() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    let app = router(pool, false);

    // A GET on an absent id is a normal 404 (a read path, not a 405 gating response).
    let (s, _h, _b) = request(&app, "GET", &res_id("acct-c"), None, None).await;
    assert_eq!(
        s,
        StatusCode::NOT_FOUND,
        "GET still routes to the read handler"
    );

    // The subscription resource LIST still 200s.
    let (ls, _lh, _lb) = request(
        &app,
        "GET",
        &format!("/subscriptions/{SUB}/resources"),
        None,
        None,
    )
    .await;
    assert_eq!(ls, StatusCode::OK, "reads are unaffected by write gating");
}

// --------------------------------------------------------------------------------------- //
// D-13/D-18: a bare resourceGroups/{rg} write is the ARM 405 envelope even when ENABLED.
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn rg_path_writes_are_arm_405_even_when_writes_enabled() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    // Writes ENABLED — resource writes would be armed, but RG CRUD stays 405 (Phase 24).
    let app = router(pool, true);
    let rg_path = format!("/subscriptions/{SUB}/resourceGroups/rg-1");
    let valid =
        serde_json::to_vec(&serde_json::json!({ "location": "eastus", "tags": {} })).unwrap();

    for method in ["PUT", "PATCH"] {
        let (s, h, b) = request(
            &app,
            method,
            &rg_path,
            Some("application/json"),
            Some(valid.clone()),
        )
        .await;
        assert_arm_405(s, &h, &b);
    }
    let (s, h, b) = request(&app, "DELETE", &rg_path, None, None).await;
    assert_arm_405(s, &h, &b);
}

// --------------------------------------------------------------------------------------- //
// Request-validation hardening (ENABLED writes): a controlled ARM 400 for a sibling media
// type, a malformed conditional header, and a malformed path — never a silent
// write. Plus the guard that a `charset` parameter on `application/json` stays ACCEPTED.
// --------------------------------------------------------------------------------------- //

use sqlx::Executor;

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
             VALUES ($1, $2, 'rg-1', 'eastus', 'network', '{}'::jsonb, 'Succeeded') \
             ON CONFLICT DO NOTHING",
        )
        .bind(format!("/subscriptions/{SUB}/resourceGroups/rg-1"))
        .bind(sub),
    )
    .await
    .expect("resource group");
}

fn valid_put_body() -> Vec<u8> {
    serde_json::to_vec(&serde_json::json!({
        "location": "eastus", "properties": {}, "tags": {}
    }))
    .unwrap()
}

#[tokio::test]
async fn sibling_json_media_type_is_rejected_400() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    let app = router(pool, true);
    // `application/json-patch+json` is a DIFFERENT media type — a prefix match would wrongly
    // accept it. Exact comparison → a controlled 400 before any write.
    let (s, _h, b) = request(
        &app,
        "PUT",
        &res_id("acct-mt"),
        Some("application/json-patch+json"),
        Some(valid_put_body()),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_REQUEST, "sibling json media type → 400");
    assert_eq!(b["error"]["code"], "InvalidRequestContent");
}

#[tokio::test]
async fn json_with_charset_parameter_is_accepted() {
    // Guard against over-tightening: `application/json; charset=utf-8` MUST still be accepted.
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool).await;
    let app = router(pool, true);
    let (s, _h, _b) = request(
        &app,
        "PUT",
        &res_id("acct-cs"),
        Some("application/json; charset=utf-8"),
        Some(valid_put_body()),
    )
    .await;
    assert_eq!(
        s,
        StatusCode::CREATED,
        "a charset parameter on application/json stays accepted"
    );
}

#[tokio::test]
async fn trailing_slash_path_is_rejected_400() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    let app = router(pool, true);
    // A trailing slash yields an empty path segment — malformed, a 400, never normalized
    // away and persisted onto the slash-free id.
    let (s, _h, b) = request(
        &app,
        "PUT",
        &format!("{}/", res_id("acct-slash")),
        Some("application/json"),
        Some(valid_put_body()),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_REQUEST, "trailing-slash path → 400");
    assert_eq!(b["error"]["code"], "InvalidRequestContent");
}

#[tokio::test]
async fn malformed_conditional_header_is_rejected_400() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    let app = router(pool, true);
    // An If-Match whose bytes are not visible ASCII (`to_str` fails) is a malformed
    // conditional request → 400 — NEVER silently dropped to an unconditional overwrite.
    let req = Request::builder()
        .method("PUT")
        .uri(res_id("acct-badcond"))
        .header("Authorization", "Bearer x")
        .header("Content-Type", "application/json")
        .header(
            header::IF_MATCH,
            header::HeaderValue::from_bytes(&[0xFF, 0xFE]).unwrap(),
        )
        .body(Body::from(valid_put_body()))
        .expect("build request");
    let resp = app.clone().oneshot(req).await.expect("oneshot");
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .expect("collect body");
    let body: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    assert_eq!(
        status,
        StatusCode::BAD_REQUEST,
        "a non-ASCII If-Match is a malformed conditional request → 400"
    );
    assert_eq!(body["error"]["code"], "InvalidRequestContent");
}
