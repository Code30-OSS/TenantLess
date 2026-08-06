//! WEBUI-03 (D-06) contract suite for the bearer-exempt `/ui` embedded-SPA nest.
//!
//! The Web Console's built `frontend/dist` is embedded into the binary (`include_dir`) and
//! served under a FRESH `/ui` nest that mirrors the proven `/_sim` router discipline
//! (`sim.rs`): a fresh prefix with a fallback scoped INSIDE the nest, merged onto the SAME
//! bearer-exempt `arm.merge(...)` seam. These tests pin the STRUCTURAL WEBUI-03 contract —
//! they depend on router composition + the `include_dir` embed, not on any DB query:
//!
//!   * `ui_index_is_html`            — GET /ui → 200 + `text/html` (the SPA index).
//!   * `ui_hashed_asset_js_mime`     — GET /ui/assets/<hashed>.js → 200 + a JavaScript MIME.
//!     Discovers the hash DYNAMICALLY from the served index.html (a Vite hash changes on
//!     every rebuild — never hardcode it). This is the one behavior to pin: axum 0.8 strips
//!     the `/ui` nest prefix from the `Uri` the fallback handler sees (RESEARCH A5).
//!   * `ui_spa_fallback_extensionless` — GET /ui/explorer/resources (a client-side nav route,
//!     no extension) → the index.html bytes (scoped SPA fallback).
//!   * `ui_asset_miss_is_404`        — GET /ui/missing.js (asset-looking miss) → 404, NOT
//!     index.html (avoids the MIME / "Unexpected token '<'" trap — Pitfall 5 anti-pattern).
//!   * `ui_no_arm_shadow`            — ARM routes keep precedence: /subscriptions resolves to
//!     the ARM handler, and a genuine ARM resource-detail MISS returns the ARM CloudError
//!     `{error:{code,message}}` 404 JSON — never the SPA HTML (T-15-05 no-shadow).
//!   * `arm_byte_identical_with_ui`  — the `/ui` discriminator (404 on the pre-merge baseline,
//!     200 on the merged router) PLUS a representative ARM list + detail that stay byte- and
//!     header-identical after `/ui` merges (D-06 / mirrors `tests/sim.rs::arm_byte_identical`).
//!
//! Harness mirrors `tests/sim.rs` verbatim (`start_pg`, `raw_request` — the header/byte
//! fidelity the MIME + byte-identity assertions need; the shared `common::request` helper
//! discards headers). The `/ui` router needs NO `AppState` (assets are static), but the
//! shared app factory (`build_router`) still requires a live pool for the ARM assertions.

mod common;

use axum::Router;
use axum::body::{Body, Bytes};
use axum::http::{HeaderMap, StatusCode};
use sqlx::PgPool;
use tenantless_server::{
    build_router, build_router_without_sim, metrics::Metrics, state::AppState,
};
use testcontainers_modules::{postgres, testcontainers::runners::AsyncRunner};
use tower::ServiceExt; // for `oneshot`

/// Start an ephemeral Postgres container and return a connected pool plus the container
/// guard (kept alive for the test's duration). Mirrors `tests/sim.rs::start_pg`.
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

/// The shared `AppState` (enforce OFF, `http://test` base) — mirrors `tests/sim.rs::sim_state`
/// so any-Bearer is accepted identically across the merged and pre-merge routers.
fn ui_state(pool: &PgPool, signer: tenantless_server::jwt::SharedSigner) -> AppState {
    AppState {
        pool: pool.clone(),
        base_url: "http://test".to_string(),
        metrics: Metrics::new(),
        signer,
        enforce_auth: false,
        control: None,
    }
}

/// The full merged runtime router (ARM + /_console + /token + /_sim + /ui).
fn build_app(pool: &PgPool, signer: tenantless_server::jwt::SharedSigner) -> Router {
    build_router(ui_state(pool, signer))
}

/// The GENUINE pre-merge baseline (no `/_sim`, no `/ui`) — the byte-identity reference.
fn build_app_without_sim(pool: &PgPool, signer: tenantless_server::jwt::SharedSigner) -> Router {
    build_router_without_sim(ui_state(pool, signer))
}

/// Build the seeded `/ui` app + return the container guard and a pool clone. Static assets
/// need no seeding, but the shared factory needs a live pool for the ARM no-shadow checks.
async fn ui_app() -> (
    Router,
    testcontainers::ContainerAsync<postgres::Postgres>,
    PgPool,
) {
    let (pool, container) = start_pg().await;
    common::seed_fixture(&pool).await;
    let app = build_app(&pool, common::test_signer());
    (app, container, pool)
}

/// Drive the router in-process, returning status, the FULL header map, and the raw body
/// bytes — the header/byte fidelity the MIME + byte-identity assertions need.
async fn raw_request(
    app: Router,
    method: &str,
    uri: &str,
    bearer: Option<&str>,
) -> (StatusCode, HeaderMap, Bytes) {
    let mut builder = axum::http::Request::builder().method(method).uri(uri);
    if let Some(tok) = bearer {
        builder = builder.header("Authorization", format!("Bearer {tok}"));
    }
    let req = builder.body(Body::empty()).expect("build request");
    let resp = app.oneshot(req).await.expect("oneshot");
    let status = resp.status();
    let headers = resp.headers().clone();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .expect("collect body");
    (status, headers, bytes)
}

/// Extract the FIRST hashed `/ui/assets/*.js` path referenced by the served index.html.
/// The Vite content-hash changes on every rebuild, so the asset name MUST be discovered
/// dynamically — never hardcoded (RESEARCH: `tests/ui.rs` DISCOVER the hashed asset).
fn find_hashed_js(html: &str) -> String {
    let start = html
        .find("/ui/assets/")
        .expect("index.html must reference a hashed /ui/assets/*.js bundle");
    let rest = &html[start..];
    let end = rest
        .find(".js")
        .expect("the referenced bundle must be a .js asset")
        + ".js".len();
    rest[..end].to_string()
}

// -------------------------------------------------------------------------------------
// WEBUI-03 — GET /ui returns the SPA index as 200 text/html from the embedded dist.
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn ui_index_is_html() {
    let (app, _pg, _pool) = ui_app().await;
    let (status, headers, bytes) = raw_request(app, "GET", "/ui", None).await;
    assert_eq!(
        status,
        StatusCode::OK,
        "GET /ui must serve the SPA index (200)"
    );
    let ct = headers
        .get("content-type")
        .expect("index must carry a content-type")
        .to_str()
        .expect("content-type ascii");
    assert!(
        ct.contains("text/html"),
        "GET /ui must be served as text/html, got {ct:?}"
    );
    let body = std::str::from_utf8(&bytes).expect("index is utf-8");
    assert!(
        body.contains("<div id=\"root\">"),
        "GET /ui must return the SPA index (with the React root div)"
    );
}

// -------------------------------------------------------------------------------------
// WEBUI-03 / A5 — a hashed asset resolves with the correct JS MIME. Pins the axum-0.8
// nest prefix-strip: the `/ui` prefix is stripped from the Uri the fallback handler sees,
// so `/ui/assets/<hash>.js` looks up `assets/<hash>.js` in the embedded dir. The hash is
// discovered from the served index.html (never hardcoded — it changes on every rebuild).
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn ui_hashed_asset_js_mime() {
    let (app, _pg, _pool) = ui_app().await;

    // Discover the hashed bundle from the served index.
    let (_s, _h, index_bytes) = raw_request(app.clone(), "GET", "/ui", None).await;
    let index = std::str::from_utf8(&index_bytes).expect("index utf-8");
    let js_path = find_hashed_js(index); // e.g. "/ui/assets/index-DHPVZjFP.js"

    let (status, headers, bytes) = raw_request(app, "GET", &js_path, None).await;
    assert_eq!(
        status,
        StatusCode::OK,
        "the hashed bundle {js_path} must resolve (200) — pins the /ui nest prefix-strip (A5)"
    );
    let ct = headers
        .get("content-type")
        .expect("asset must carry a content-type")
        .to_str()
        .expect("content-type ascii");
    assert!(
        ct.contains("javascript"),
        "the .js bundle must carry a JavaScript MIME (mime_guess), got {ct:?}"
    );
    assert!(!bytes.is_empty(), "the served bundle must not be empty");
}

// -------------------------------------------------------------------------------------
// WEBUI-03 — a client-side nav route (extensionless) falls back to index.html (scoped SPA
// fallback) so a deep-linked React route reloads correctly.
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn ui_spa_fallback_extensionless() {
    let (app, _pg, _pool) = ui_app().await;

    // The canonical index (GET /ui) …
    let (si, hi, index_bytes) = raw_request(app.clone(), "GET", "/ui", None).await;
    assert_eq!(si, StatusCode::OK);

    // … an extensionless nav route must return the SAME index bytes (SPA fallback).
    let (status, headers, bytes) = raw_request(app, "GET", "/ui/explorer/resources", None).await;
    assert_eq!(
        status,
        StatusCode::OK,
        "an extensionless /ui nav route must serve the SPA index (SPA fallback)"
    );
    let ct = headers
        .get("content-type")
        .expect("fallback must carry a content-type")
        .to_str()
        .expect("content-type ascii");
    assert!(
        ct.contains("text/html"),
        "the SPA fallback must be text/html, got {ct:?}"
    );
    assert_eq!(
        bytes, index_bytes,
        "the SPA fallback must serve the SAME index.html bytes as GET /ui"
    );
    // sanity: `_hi` is the index headers — the fallback content-type must also be html.
    assert!(
        hi.get("content-type")
            .and_then(|v| v.to_str().ok())
            .is_some_and(|c| c.contains("text/html")),
        "GET /ui itself is html"
    );
}

// -------------------------------------------------------------------------------------
// WEBUI-03 — an asset-looking miss (has an extension) returns a real 404, NOT index.html.
// Returning index.html for a missing `.js`/`.css` causes the browser MIME / "Unexpected
// token '<'" failure (Pitfall 5 anti-pattern).
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn ui_asset_miss_is_404() {
    let (app, _pg, _pool) = ui_app().await;

    // Grab the index bytes so we can prove the miss did NOT return them.
    let (_si, _hi, index_bytes) = raw_request(app.clone(), "GET", "/ui", None).await;

    let (status, _headers, bytes) = raw_request(app, "GET", "/ui/missing.js", None).await;
    assert_eq!(
        status,
        StatusCode::NOT_FOUND,
        "an asset-looking miss (/ui/missing.js) must be 404, NOT the SPA index"
    );
    assert_ne!(
        bytes, index_bytes,
        "an asset-looking miss must NOT return index.html (MIME/Unexpected-token trap)"
    );
    let body = std::str::from_utf8(&bytes).unwrap_or("");
    assert!(
        !body.contains("<div id=\"root\">"),
        "the 404 body must not be the SPA index HTML"
    );
}

// -------------------------------------------------------------------------------------
// T-15-05 — the `/ui` SPA fallback must NOT shadow ARM routes: ARM keeps precedence.
//   (a) GET /subscriptions (+Bearer) resolves to the ARM handler (its own `value` array).
//   (b) a genuine ARM resource-detail MISS returns the ARM CloudError `{error:{code,message}}`
//       404 JSON — never the SPA HTML. (The bare unmatched path /subscriptions/nope has no
//       route/handler, so it yields axum's default empty 404; to assert the CloudError-JSON
//       no-shadow contract we drive a real detail-route miss, which routes through
//       `get_resource_detail` → `ApiError::NotFound`.)
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn ui_no_arm_shadow() {
    let (app, _pg, _pool) = ui_app().await;

    // (a) ARM list resolves to the ARM handler (bearer-gated; any non-empty Bearer → 200).
    let (arm_status, _h, arm_bytes) =
        raw_request(app.clone(), "GET", "/subscriptions", Some("any-token")).await;
    assert_eq!(
        arm_status,
        StatusCode::OK,
        "ARM /subscriptions resolves (not shadowed by /ui)"
    );
    let arm_json: serde_json::Value = serde_json::from_slice(&arm_bytes).expect("ARM JSON");
    assert!(
        arm_json.get("value").and_then(|v| v.as_array()).is_some(),
        "ARM /subscriptions returns its own `value` array shape, not the SPA HTML"
    );

    // (b) a genuine ARM detail MISS → ARM CloudError 404 JSON, never HTML. This path matches
    // the `{*tail}` detail route (servers/{name}/databases/{name}) with a non-existent id.
    let miss_uri = format!(
        "/subscriptions/{}/resourceGroups/{}/providers/Microsoft.Sql/servers/no-such-srv/databases/no-such-db",
        common::SUB_A,
        common::FILTER_RG_NAME
    );
    let (status, headers, bytes) = raw_request(app, "GET", &miss_uri, Some("any-token")).await;
    assert_eq!(
        status,
        StatusCode::NOT_FOUND,
        "an unknown ARM resource is a 404, not a 200 SPA page"
    );
    let ct = headers
        .get("content-type")
        .expect("ARM 404 must carry a content-type")
        .to_str()
        .expect("content-type ascii");
    assert!(
        ct.contains("application/json"),
        "an ARM 404 must be the CloudError JSON, never HTML — got {ct:?}"
    );
    let body: serde_json::Value = serde_json::from_slice(&bytes).expect("ARM 404 body is JSON");
    let err = body.get("error").expect("ARM 404 has an `error` object");
    assert!(err.get("code").is_some(), "CloudError has a `code`");
    assert!(err.get("message").is_some(), "CloudError has a `message`");
}

// -------------------------------------------------------------------------------------
// D-06 — the `/ui` discriminator + ARM byte-identity. Mirrors `tests/sim.rs::arm_byte_identical`
// with a `/ui` discriminator: the pre-merge baseline (`build_router_without_sim`) exposes NO
// `/ui` (404) while the merged router (`build_router`) serves it (200), proving `/ui` was
// actually merged. A representative ARM list AND detail must stay byte- and header-identical
// across the two routers — the `/ui` overlay must not touch the ARM contract.
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn arm_byte_identical_with_ui() {
    let (pool, _pg) = start_pg().await;
    common::seed_fixture(&pool).await;

    let signer = common::test_signer();
    let app_ref = build_app_without_sim(&pool, signer.clone());
    let app_merged = build_app(&pool, signer);

    // DISCRIMINATOR — the pre-merge router has NO /ui (404); the merged router serves it (200).
    let (ref_ui_status, _h, _b) = raw_request(app_ref.clone(), "GET", "/ui", None).await;
    assert_eq!(
        ref_ui_status,
        StatusCode::NOT_FOUND,
        "pre-merge build_router_without_sim must NOT expose /ui (404)"
    );
    let (merged_ui_status, _h, _b) = raw_request(app_merged.clone(), "GET", "/ui", None).await;
    assert_eq!(
        merged_ui_status,
        StatusCode::OK,
        "merged build_router must expose /ui (200)"
    );

    // ARM LIST — /subscriptions (bearer-gated; any non-empty Bearer under enforce OFF).
    let (s1, h1, b1) = raw_request(app_ref.clone(), "GET", "/subscriptions", Some("t")).await;
    let (s2, h2, b2) = raw_request(app_merged.clone(), "GET", "/subscriptions", Some("t")).await;
    assert_eq!(s1, StatusCode::OK);
    assert_eq!(s1, s2, "ARM list status identical after /ui merge");
    assert_eq!(b1, b2, "ARM list BODY BYTES identical after /ui merge");
    assert_eq!(
        h1.get("content-type"),
        h2.get("content-type"),
        "ARM list content-type identical after /ui merge"
    );

    // ARM DETAIL — a nested resource detail (the route most at risk from a catch-all shadow).
    let detail_uri = common::NESTED_RESOURCE_ID;
    let (d1s, d1h, d1b) = raw_request(app_ref, "GET", detail_uri, Some("t")).await;
    let (d2s, d2h, d2b) = raw_request(app_merged, "GET", detail_uri, Some("t")).await;
    assert_eq!(
        d1s,
        StatusCode::OK,
        "ARM detail resolves (not shadowed by /ui)"
    );
    assert_eq!(d1s, d2s, "ARM detail status identical after /ui merge");
    assert_eq!(d1b, d2b, "ARM detail BODY BYTES identical after /ui merge");
    assert_eq!(
        d1h.get("content-type"),
        d2h.get("content-type"),
        "ARM detail content-type identical after /ui merge"
    );
    let detail: serde_json::Value = serde_json::from_slice(&d1b).expect("ARM detail JSON");
    assert_eq!(
        detail.get("id").and_then(|v| v.as_str()),
        Some(detail_uri),
        "ARM detail returns the requested resource id (its own shape, not the SPA HTML)"
    );
}
