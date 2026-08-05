//! WAPI-04 (D-12) contract suite for the bearer-exempt `/_sim` read-only surface.
//!
//! These five tests pin the ARCHITECTURAL boundary of Phase 14 — they hold against the
//! Plan 14-01 STUB handlers because WAPI-04 is structural (it depends on router
//! composition, not on query logic). The real query/aggregate behavior (WAPI-01/02/03)
//! is pinned by Plans 14-02/14-03.
//!
//! Coverage (D-12):
//!   * `bearer_exempt`          — GET /_sim/* reachable with NO Authorization header → 200.
//!   * `method_not_allowed`     — POST/PUT/PATCH/DELETE /_sim/violations → 405 + `Allow: GET`.
//!   * `unknown_route_json_error` — GET /_sim/nope → `{error:{code,message}}` JSON (not HTML/empty).
//!   * `no_arm_shadow`          — build_router does not panic; an ARM path and a /_sim path
//!     each resolve to their OWN handler.
//!   * `arm_byte_identical`     — a representative ARM list AND detail response are byte- and
//!     header-identical after the /_sim merge.
//!
//! Harness mirrors `tests/drift_simulator.rs` (ephemeral testcontainers Postgres seeded by
//! `common::seed_fixture` + the SCOPED `common::seed_sim_rows`; `enforce_auth: false`).

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

/// Start an ephemeral Postgres container and return a connected pool plus the
/// container guard (kept alive for the test's duration).
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

/// Build a `/_sim`-seeded router (enforce OFF) over the given pool. Shares a single
/// signer so two independently-built routers accept the same any-Bearer identically.
fn build_app(pool: &PgPool, signer: tenantless_server::jwt::SharedSigner) -> Router {
    build_router(sim_state(pool, signer))
}

/// Build the GENUINE pre-merge ARM-only router (WAPI-04 test seam, D-17): the same
/// `AppState` as [`build_app`] but constructed via `build_router_without_sim`, which
/// composes `arm` + `/_console` + `/token` WITHOUT `.merge(sim::router)`. This is the
/// baseline `arm_byte_identical` compares against the merged router — it has NO `/_sim`
/// surface, so the comparison is non-tautological (a real merge regression is detectable).
fn build_app_without_sim(pool: &PgPool, signer: tenantless_server::jwt::SharedSigner) -> Router {
    build_router_without_sim(sim_state(pool, signer))
}

/// The shared `AppState` (enforce OFF, `http://test` base) used by both the merged and
/// the pre-merge routers so any-Bearer is accepted identically across them.
fn sim_state(pool: &PgPool, signer: tenantless_server::jwt::SharedSigner) -> AppState {
    AppState {
        pool: pool.clone(),
        base_url: "http://test".to_string(),
        metrics: Metrics::new(),
        signer,
        enforce_auth: false,
        control: None,
    }
}

/// Build the seeded app + return the container guard, a pool clone, and the ground truth.
async fn sim_app() -> (
    Router,
    testcontainers::ContainerAsync<postgres::Postgres>,
    PgPool,
    common::SimSeed,
) {
    let (pool, container) = start_pg().await;
    common::seed_fixture(&pool).await;
    let seed = common::seed_sim_rows(&pool).await;
    let app = build_app(&pool, common::test_signer());
    (app, container, pool, seed)
}

/// Drive the router in-process, returning status, the FULL header map, and the raw body
/// bytes — the header/byte fidelity the WAPI-04 contract needs (the shared `common::request`
/// helper parses+discards headers, which would lose the `Allow` header + byte identity).
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

// -------------------------------------------------------------------------------------
// D-12.2 — /_sim/* reachable with NO Authorization header (bearer-exempt).
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn bearer_exempt() {
    let (app, _pg, _pool, _seed) = sim_app().await;
    for uri in [
        "/_sim/violations",
        "/_sim/dependencies",
        "/_sim/summary",
        "/_sim/subscriptions",
        "/_sim/resources/search?q=res-",
    ] {
        // NO bearer — the ARM routes would 401 here; /_sim must return 200.
        let (status, _h, _b) = raw_request(app.clone(), "GET", uri, None).await;
        assert_eq!(
            status,
            StatusCode::OK,
            "{uri} must be reachable with no auth"
        );
    }
}

// -------------------------------------------------------------------------------------
// D-12.3 — only GET registered ⇒ MethodRouter returns 405 + `Allow: GET` for mutations.
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn method_not_allowed() {
    let (app, _pg, _pool, _seed) = sim_app().await;
    for method in ["POST", "PUT", "PATCH", "DELETE"] {
        let (status, headers, _b) =
            raw_request(app.clone(), method, "/_sim/violations", None).await;
        assert_eq!(
            status,
            StatusCode::METHOD_NOT_ALLOWED,
            "{method} /_sim/violations must be 405 (read-only, structural)"
        );
        let allow = headers
            .get("allow")
            .expect("405 must carry an Allow header")
            .to_str()
            .expect("Allow header is ascii");
        assert!(
            allow.contains("GET"),
            "Allow header must advertise GET, got {allow:?}"
        );
    }
}

// -------------------------------------------------------------------------------------
// D-12.5 — unknown /_sim route → the ARM CloudError JSON shape (not empty/HTML).
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn unknown_route_json_error() {
    let (app, _pg, _pool, _seed) = sim_app().await;
    let (status, headers, bytes) = raw_request(app, "GET", "/_sim/nope", None).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    // JSON content-type, not text/html or empty.
    let ct = headers
        .get("content-type")
        .expect("404 must carry a content-type")
        .to_str()
        .expect("content-type ascii");
    assert!(
        ct.contains("application/json"),
        "unknown /_sim route must return JSON, got {ct:?}"
    );
    let body: serde_json::Value = serde_json::from_slice(&bytes).expect("body is JSON");
    let err = body.get("error").expect("has an `error` object");
    assert!(err.get("code").is_some(), "error has a `code`");
    assert!(err.get("message").is_some(), "error has a `message`");
}

// -------------------------------------------------------------------------------------
// D-12.4 — build_router does not panic; an ARM path and a /_sim path each resolve to
// their OWN handler (no shadowing).
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn no_arm_shadow() {
    // Constructing the app already proves build_router does not panic (the merge of the
    // nested /_sim fallback into the fallback-less `arm` router is the panic risk).
    let (app, _pg, _pool, _seed) = sim_app().await;

    // ARM path resolves to the ARM handler (bearer-gated; any non-empty Bearer → 200 ARM
    // shape with a `value` array of subscriptions).
    let (arm_status, _h, arm_bytes) =
        raw_request(app.clone(), "GET", "/subscriptions", Some("any-token")).await;
    assert_eq!(arm_status, StatusCode::OK, "ARM /subscriptions resolves");
    let arm_json: serde_json::Value = serde_json::from_slice(&arm_bytes).expect("ARM JSON");
    assert!(
        arm_json.get("value").and_then(|v| v.as_array()).is_some(),
        "ARM /subscriptions returns its own `value` array shape"
    );

    // /_sim path resolves to the (distinct) stub handler — bearer-exempt, its own envelope.
    let (sim_status, _h, sim_bytes) = raw_request(app, "GET", "/_sim/violations", None).await;
    assert_eq!(sim_status, StatusCode::OK, "/_sim/violations resolves");
    let sim_json: serde_json::Value = serde_json::from_slice(&sim_bytes).expect("/_sim JSON");
    assert!(
        sim_json.get("value").and_then(|v| v.as_array()).is_some(),
        "/_sim/violations returns its own `value` envelope"
    );
    // The two responses come from different handlers over the SAME state — they are not
    // the same route (ARM subscriptions carries real subscription objects; the /_sim stub
    // is empty), proving no shadow/capture.
    assert_ne!(
        arm_bytes, sim_bytes,
        "ARM and /_sim resolve to DIFFERENT handlers"
    );
}

// -------------------------------------------------------------------------------------
// D-12.1 / D-17 — a representative ARM list AND detail response are byte- and
// header-identical between the GENUINE pre-merge ARM-only router
// (`build_router_without_sim`) and the merged router (`build_router`), over the SAME
// pool + signer. This is NON-TAUTOLOGICAL: the discriminating check below proves the two
// routers genuinely differ (the pre-merge one has NO `/_sim` surface), so if the `/_sim`
// merge ever altered ARM bytes/headers — or if the pre-merge seam accidentally included
// `/_sim` — this test FAILS. The full prior-phase suite is the broad byte-identical proof.
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn arm_byte_identical() {
    let (pool, _pg) = start_pg().await;
    common::seed_fixture(&pool).await;
    common::seed_sim_rows(&pool).await;

    // app_ref = the GENUINE pre-merge ARM baseline (no `.merge(sim::router)`); app_merged
    // = the full runtime router. Same pool + SAME signer so any-Bearer is accepted
    // identically. Any divergence in ARM response bytes/headers is a real merge regression.
    let signer = common::test_signer();
    let app_ref = build_app_without_sim(&pool, signer.clone());
    let app_merged = build_app(&pool, signer);

    // DISCRIMINATOR (makes the comparison non-tautological): the pre-merge router has NO
    // `/_sim` surface (404), while the merged router serves it (200). If this ever fails,
    // the two routers are not actually distinct and the byte-identity below proves nothing.
    let (ref_sim_status, _h, _b) =
        raw_request(app_ref.clone(), "GET", "/_sim/violations", None).await;
    assert_eq!(
        ref_sim_status,
        StatusCode::NOT_FOUND,
        "pre-merge build_router_without_sim must NOT expose /_sim (404)"
    );
    let (merged_sim_status, _h, _b) =
        raw_request(app_merged.clone(), "GET", "/_sim/violations", None).await;
    assert_eq!(
        merged_sim_status,
        StatusCode::OK,
        "merged build_router must expose /_sim/violations (200)"
    );

    // ARM LIST — /subscriptions (bearer-gated; any non-empty Bearer under enforce OFF).
    let (s1, h1, b1) = raw_request(app_ref.clone(), "GET", "/subscriptions", Some("t")).await;
    let (s2, h2, b2) = raw_request(app_merged.clone(), "GET", "/subscriptions", Some("t")).await;
    assert_eq!(s1, StatusCode::OK);
    assert_eq!(s1, s2, "ARM list status identical after merge");
    assert_eq!(b1, b2, "ARM list BODY BYTES identical after merge");
    assert_eq!(
        h1.get("content-type"),
        h2.get("content-type"),
        "ARM list content-type identical after merge"
    );

    // ARM DETAIL — a nested resource detail (the full ARM id resolves via the {*tail}
    // catch-all detail route). This is the route most at risk from a catch-all shadow.
    let detail_uri = common::NESTED_RESOURCE_ID;
    let (d1s, d1h, d1b) = raw_request(app_ref, "GET", detail_uri, Some("t")).await;
    let (d2s, d2h, d2b) = raw_request(app_merged, "GET", detail_uri, Some("t")).await;
    assert_eq!(d1s, StatusCode::OK, "ARM detail resolves (not shadowed)");
    assert_eq!(d1s, d2s, "ARM detail status identical after merge");
    assert_eq!(d1b, d2b, "ARM detail BODY BYTES identical after merge");
    assert_eq!(
        d1h.get("content-type"),
        d2h.get("content-type"),
        "ARM detail content-type identical after merge"
    );
    // Sanity: the detail body is the ARM resource shape (its own `id`), not a /_sim body.
    let detail: serde_json::Value = serde_json::from_slice(&d1b).expect("ARM detail JSON");
    assert_eq!(
        detail.get("id").and_then(|v| v.as_str()),
        Some(detail_uri),
        "ARM detail returns the requested resource id"
    );
}

// =====================================================================================
// WAPI-01 / WAPI-02 — the collection handler bodies (Plan 14-02).
// =====================================================================================

/// Parse a `/_sim` collection response into its `value` array and optional `nextLink`.
fn parse_collection(bytes: &Bytes) -> (Vec<serde_json::Value>, Option<String>) {
    let body: serde_json::Value = serde_json::from_slice(bytes).expect("collection JSON");
    let value = body
        .get("value")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    let next = body
        .get("nextLink")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string());
    (value, next)
}

// -------------------------------------------------------------------------------------
// WAPI-01 — ?subscription narrows violations via LEFT JOIN synthetic.resources; a
// malformed UUID returns a fixed 400 (never a 500/panic).
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn violations_subscription_filter() {
    let (app, _pg, _pool, seed) = sim_app().await;

    // ?subscription=<SUB_A> — every seeded violation's resource lives under SUB_A, so the
    // JOIN-narrowed result carries only SUB_A rows (subscriptionId == SUB_A).
    let uri = format!("/_sim/violations?subscription={}", seed.sub);
    let (status, _h, bytes) = raw_request(app.clone(), "GET", &uri, None).await;
    assert_eq!(status, StatusCode::OK);
    let (value, _n) = parse_collection(&bytes);
    assert!(!value.is_empty(), "SUB_A has seeded violations");
    let sub_a = seed.sub.to_string();
    for v in &value {
        assert_eq!(
            v.get("subscriptionId").and_then(|x| x.as_str()),
            Some(sub_a.as_str()),
            "subscription filter must return ONLY SUB_A violations (via r.subscription_id)"
        );
    }

    // ?subscription=<SUB_B> — no violation's resource lives under SUB_B → empty.
    let uri_b = format!("/_sim/violations?subscription={}", seed.cross_sub_target);
    let (sb, _h, bb) = raw_request(app.clone(), "GET", &uri_b, None).await;
    assert_eq!(sb, StatusCode::OK);
    let (vb, _n) = parse_collection(&bb);
    assert!(vb.is_empty(), "no violations under SUB_B");

    // ?subscription=not-a-uuid → fixed 400 (parsed to Uuid BEFORE SQL; D-09), never 500.
    let (sbad, _h, _b) =
        raw_request(app, "GET", "/_sim/violations?subscription=not-a-uuid", None).await;
    assert_eq!(
        sbad,
        StatusCode::BAD_REQUEST,
        "malformed subscription UUID → 400, never 500"
    );
}

// -------------------------------------------------------------------------------------
// D-09 / Pitfall 6 — code & severity match case-insensitively (lower()=lower()).
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn filter_case_behavior() {
    let (app, _pg, _pool, seed) = sim_app().await;

    // code: the stored value is UPPER_SNAKE; a lowercased query returns the SAME set.
    let lower = seed.sample_code.to_lowercase();
    let (s1, _h, b1) = raw_request(
        app.clone(),
        "GET",
        &format!("/_sim/violations?code={lower}"),
        None,
    )
    .await;
    let (s2, _h, b2) = raw_request(
        app.clone(),
        "GET",
        &format!("/_sim/violations?code={}", seed.sample_code),
        None,
    )
    .await;
    assert_eq!(s1, StatusCode::OK);
    assert_eq!(s2, StatusCode::OK);
    let (v1, _) = parse_collection(&b1);
    let (v2, _) = parse_collection(&b2);
    assert_eq!(
        v1.len() as i64,
        seed.sample_code_count,
        "code filter count matches ground truth"
    );
    assert_eq!(v1.len(), v2.len(), "code filter is case-insensitive");

    // severity: stored Title case; a lowercased query returns the same non-empty set.
    let sev_lower = seed.sample_severity.to_lowercase();
    let (_s, _h, bs1) = raw_request(
        app.clone(),
        "GET",
        &format!("/_sim/violations?severity={sev_lower}"),
        None,
    )
    .await;
    let (_s2, _h, bs2) = raw_request(
        app,
        "GET",
        &format!("/_sim/violations?severity={}", seed.sample_severity),
        None,
    )
    .await;
    let (vs1, _) = parse_collection(&bs1);
    let (vs2, _) = parse_collection(&bs2);
    assert!(!vs1.is_empty(), "severity filter returns rows");
    assert_eq!(vs1.len(), vs2.len(), "severity filter is case-insensitive");
}

// -------------------------------------------------------------------------------------
// D-06 — a malformed $skiptoken returns a fixed 400, never a 500/panic.
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn bad_cursor_is_400() {
    let (app, _pg, _pool, _seed) = sim_app().await;
    // Valid url-safe base64 ("aGVsbG8" = "hello") whose payload is not a base-10 i64.
    let (status, _h, _b) =
        raw_request(app, "GET", "/_sim/violations?$skiptoken=aGVsbG8", None).await;
    assert_eq!(
        status,
        StatusCode::BAD_REQUEST,
        "malformed $skiptoken → fixed 400, never 500"
    );
}

// -------------------------------------------------------------------------------------
// WAPI-02 — ?subscription matches source OR target (one bind, two uses); crossSubscription
// = (source != target); ?type is case-insensitive.
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn dependencies_source_or_target() {
    let (app, _pg, _pool, seed) = sim_app().await;

    // ?subscription=<SUB_B> — SUB_B appears ONLY as a cross-sub edge TARGET, never a
    // source. The source-OR-target filter must still return every such edge.
    let uri = format!("/_sim/dependencies?subscription={}", seed.cross_sub_target);
    let (status, _h, bytes) = raw_request(app.clone(), "GET", &uri, None).await;
    assert_eq!(status, StatusCode::OK);
    let (value, _n) = parse_collection(&bytes);
    assert_eq!(
        value.len() as i64,
        seed.cross_sub_dependency_count,
        "SUB_B matches every cross-sub edge via TARGET (source-OR-target)"
    );
    let sub_b = seed.cross_sub_target.to_string();
    for d in &value {
        // Nested spec shape (D-13): source/target are OBJECTS carrying subscriptionId.
        let src = d["source"]["subscriptionId"].as_str().unwrap();
        let tgt = d["target"]["subscriptionId"].as_str().unwrap();
        assert!(
            src == sub_b || tgt == sub_b,
            "each returned edge has SUB_B as source OR target"
        );
        // The flat DTO keys must be GONE (D-13 replaces them with the nested shape).
        assert!(
            d.get("sourceSubscriptionId").is_none() && d.get("dependencyType").is_none(),
            "flat dependency keys must not appear in the nested spec shape: {d:?}"
        );
        assert!(
            d["source"]["resourceId"].as_str().is_some(),
            "source object carries resourceId"
        );
        // All SUB_B-touching seeded edges are cross-sub (SUB_A → SUB_B).
        assert_eq!(
            d["crossSubscription"],
            serde_json::Value::Bool(true),
            "SUB_B edges are cross-subscription"
        );
    }

    // crossSubscription = (source != target) over the FULL set: exactly the seeded count.
    let (s_all, _h, b_all) =
        raw_request(app.clone(), "GET", "/_sim/dependencies?$top=1000", None).await;
    assert_eq!(s_all, StatusCode::OK);
    let (all, _n) = parse_collection(&b_all);
    assert_eq!(
        all.len() as i64,
        seed.dependency_count,
        "unfiltered returns every seeded dependency"
    );
    let cross = all
        .iter()
        .filter(|d| d["crossSubscription"] == serde_json::Value::Bool(true))
        .count() as i64;
    assert_eq!(
        cross, seed.cross_sub_dependency_count,
        "crossSubscription derived = (source_subscription != target_subscription)"
    );

    // ?type — the stored value is lower-hyphen; an UPPERCASED query returns the SAME set
    // (filter_case_behavior extension for dependencies).
    let ty = &seed.sample_dependency_type;
    let (t1, _h, tb1) = raw_request(
        app.clone(),
        "GET",
        &format!("/_sim/dependencies?type={ty}"),
        None,
    )
    .await;
    let (t2, _h, tb2) = raw_request(
        app,
        "GET",
        &format!("/_sim/dependencies?type={}", ty.to_uppercase()),
        None,
    )
    .await;
    assert_eq!(t1, StatusCode::OK);
    assert_eq!(t2, StatusCode::OK);
    let (tv1, _) = parse_collection(&tb1);
    let (tv2, _) = parse_collection(&tb2);
    assert_eq!(
        tv1.len() as i64,
        seed.sample_dependency_type_count,
        "type filter count matches ground truth"
    );
    assert_eq!(tv1.len(), tv2.len(), "type filter is case-insensitive");
}

/// Walk a `/_sim` collection from `first_uri` following `nextLink` to exhaustion, over the
/// `http://test` base. Returns every collected item plus the list of nextLinks seen.
async fn walk_all(app: &Router, first_uri: &str) -> (Vec<serde_json::Value>, Vec<String>) {
    let mut items = Vec::new();
    let mut links = Vec::new();
    let mut uri = first_uri.to_string();
    // Bound the loop defensively so a paging bug can't hang the suite.
    for _ in 0..100 {
        let (status, _h, bytes) = raw_request(app.clone(), "GET", &uri, None).await;
        assert_eq!(status, StatusCode::OK, "page {uri} must be 200");
        let (value, next) = parse_collection(&bytes);
        items.extend(value);
        match next {
            Some(link) => {
                let rel = link
                    .strip_prefix("http://test")
                    .expect("nextLink is absolute on the test base_url")
                    .to_string();
                links.push(link);
                uri = rel;
            }
            None => return (items, links),
        }
    }
    panic!("pagination did not terminate within 100 pages");
}

// -------------------------------------------------------------------------------------
// D-06 — keyset traversal across pages: no gaps, no dupes; nextLink preserves the active
// filter AND api-version (both collection endpoints).
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn pagination_traversal_no_gaps() {
    let (app, _pg, _pool, seed) = sim_app().await;

    // --- /_sim/violations: full traversal with a small $top (no filter) ---
    let (viol_items, _l) = walk_all(&app, "/_sim/violations?$top=2").await;
    // Dedup key = (resourceId, code) — unique per seeded violation.
    let mut vkeys: Vec<(String, String)> = viol_items
        .iter()
        .map(|v| {
            (
                v["resourceId"].as_str().unwrap().to_string(),
                v["code"].as_str().unwrap().to_string(),
            )
        })
        .collect();
    let visited_v = vkeys.len();
    vkeys.sort();
    vkeys.dedup();
    assert_eq!(vkeys.len(), visited_v, "no violation appears on two pages");
    assert_eq!(
        visited_v as i64, seed.violation_count,
        "every seeded violation visited exactly once (no gaps)"
    );

    // --- /_sim/dependencies: FILTERED traversal (small $top + subscription + api-version) ---
    // Every seeded dependency has source_subscription = SUB_A, so subscription=SUB_A
    // matches all of them via the source arm — enough rows to force multiple pages.
    let first = format!(
        "/_sim/dependencies?$top=2&subscription={}&api-version=2021-04-01",
        seed.sub
    );
    let (dep_items, dep_links) = walk_all(&app, &first).await;
    let mut dkeys: Vec<(String, String, String)> = dep_items
        .iter()
        .map(|d| {
            // Nested spec shape (D-13): source/target objects + `type` (not `dependencyType`).
            (
                d["source"]["resourceId"].as_str().unwrap().to_string(),
                d["target"]["resourceId"].as_str().unwrap().to_string(),
                d["type"].as_str().unwrap().to_string(),
            )
        })
        .collect();
    let visited_d = dkeys.len();
    dkeys.sort();
    dkeys.dedup();
    assert_eq!(dkeys.len(), visited_d, "no dependency appears on two pages");
    assert_eq!(
        visited_d as i64, seed.dependency_count,
        "every SUB_A dependency visited exactly once (no gaps)"
    );

    // page-2+ nextLink preserves BOTH the active discrete filter AND api-version (D-06).
    assert!(
        !dep_links.is_empty(),
        "a multi-page traversal must emit at least one nextLink"
    );
    let page2 = &dep_links[0];
    assert!(
        page2.contains(&format!("subscription={}", seed.sub)),
        "nextLink must preserve the subscription filter: {page2}"
    );
    assert!(
        page2.contains("api-version=2021-04-01"),
        "nextLink must preserve api-version: {page2}"
    );
}

// -------------------------------------------------------------------------------------
// D-13 — both collection envelopes carry `count` = the total rows matching the ACTIVE
// filter (a COUNT(*) over the SAME predicate), NOT the page size, NOT the table total.
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn collection_count_is_filtered_total() {
    let (app, _pg, pool, seed) = sim_app().await;

    // --- violations: unfiltered `count` == an independent COUNT(*) of the table ---
    let (status, _h, bytes) =
        raw_request(app.clone(), "GET", "/_sim/violations?$top=1000", None).await;
    assert_eq!(status, StatusCode::OK);
    let body: serde_json::Value = serde_json::from_slice(&bytes).expect("violations JSON");
    let total_viol: i64 = sqlx::query_scalar("SELECT count(*) FROM synthetic.violations")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(
        body["count"]
            .as_i64()
            .expect("violations envelope has a count"),
        total_viol,
        "unfiltered violations count == COUNT(*)"
    );
    assert_eq!(total_viol, seed.violation_count);

    // --- violations: ?code= → `count` is the COUNT(*) of THAT predicate (not the total) ---
    let uri = format!("/_sim/violations?code={}", seed.sample_code);
    let (s, _h, b) = raw_request(app.clone(), "GET", &uri, None).await;
    assert_eq!(s, StatusCode::OK);
    let body: serde_json::Value = serde_json::from_slice(&b).expect("violations JSON");
    assert_eq!(
        body["count"].as_i64().expect("filtered count present"),
        seed.sample_code_count,
        "filtered violations count == COUNT(*) of the code predicate, NOT the table total"
    );
    assert_ne!(
        seed.sample_code_count, seed.violation_count,
        "the code predicate must narrow (else the assertion above is vacuous)"
    );

    // --- violations: $top < result set → `count` is the WHOLE filtered set, not the page ---
    let (s, _h, b) = raw_request(app.clone(), "GET", "/_sim/violations?$top=1", None).await;
    assert_eq!(s, StatusCode::OK);
    let body: serde_json::Value = serde_json::from_slice(&b).expect("violations JSON");
    let page_len = body["value"].as_array().unwrap().len() as i64;
    assert_eq!(page_len, 1, "$top=1 bounds the page to one row");
    assert_eq!(
        body["count"].as_i64().expect("count present"),
        seed.violation_count,
        "count is the full filtered set, NOT the page size"
    );
    assert!(
        body["count"].as_i64().unwrap() > page_len,
        "count > value.len() when $top < result size"
    );

    // --- dependencies: unfiltered `count` == COUNT(*) ---
    let (s, _h, b) = raw_request(app.clone(), "GET", "/_sim/dependencies?$top=1000", None).await;
    assert_eq!(s, StatusCode::OK);
    let body: serde_json::Value = serde_json::from_slice(&b).expect("dependencies JSON");
    let total_dep: i64 = sqlx::query_scalar("SELECT count(*) FROM synthetic.dependencies")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(
        body["count"]
            .as_i64()
            .expect("dependencies envelope has a count"),
        total_dep,
        "unfiltered dependencies count == COUNT(*)"
    );
    assert_eq!(total_dep, seed.dependency_count);

    // --- dependencies: ?subscription= → `count` == the source-OR-target COUNT(*) ---
    let uri = format!("/_sim/dependencies?subscription={}", seed.cross_sub_target);
    let (s, _h, b) = raw_request(app, "GET", &uri, None).await;
    assert_eq!(s, StatusCode::OK);
    let body: serde_json::Value = serde_json::from_slice(&b).expect("dependencies JSON");
    let filtered_dep: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM synthetic.dependencies \
         WHERE source_subscription = $1 OR target_subscription = $1",
    )
    .bind(seed.cross_sub_target)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(
        body["count"].as_i64().expect("filtered count present"),
        filtered_dep,
        "filtered dependencies count == source-OR-target COUNT(*) of the SAME predicate"
    );
    assert_eq!(filtered_dep, seed.cross_sub_dependency_count);
}

// =====================================================================================
// WAPI-03 — the /_sim/summary aggregate (Plan 14-03).
//
// One unpaginated payload: `totals` + `subscriptions[]` + `byType[]` + `byLocation[]` +
// tenant metadata (tenantId / seed / profile). All computed in ONE read-only REPEATABLE
// READ snapshot so the sections cannot disagree under concurrent generation. Ground truth
// is taken from INDEPENDENT `COUNT(*)` queries (never hard-coded literals).
// =====================================================================================

// `use` items may appear at module scope anywhere; `Executor` powers the schema-only
// migration apply used by the empty-tenant test.
use sqlx::Executor as _;

/// Apply the synthetic schema migrations to a FRESH container WITHOUT seeding any rows —
/// the empty / schema-only tenant contract (D-11 / Pitfall 5). Mirrors the migration
/// ordering in `common::seed_fixture` (001 → 002 → 003 → 006, so `drift_deleted_at` exists)
/// plus 007 so `profile_name` exists) but inserts NOTHING, so `synthetic.tenant`
/// has zero rows.
async fn schema_only_pg() -> (PgPool, testcontainers::ContainerAsync<postgres::Postgres>) {
    let (pool, container) = start_pg().await;
    for sql in [
        include_str!("../../sql/001_synthetic_tenant.sql"),
        include_str!("../../sql/002_cross_sub_dependencies.sql"),
        include_str!("../../sql/003_integrity_and_index.sql"),
        include_str!("../../sql/006_drift.sql"),
        include_str!("../../sql/007_web_metadata.sql"),
    ] {
        (&pool).execute(sql).await.expect("apply schema migration");
    }
    (pool, container)
}

// -------------------------------------------------------------------------------------
// WAPI-03 — totals equal COUNT(*) ground truth on the seeded fixture; every per-sub
// rollup count also matches an independent COUNT(*).
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn summary_counts_ground_truth() {
    let (app, _pg, pool, _seed) = sim_app().await;
    let (status, _h, bytes) = raw_request(app, "GET", "/_sim/summary", None).await;
    assert_eq!(status, StatusCode::OK);
    let body: serde_json::Value = serde_json::from_slice(&bytes).expect("summary JSON");

    // Independent COUNT(*) ground truth (NOT hard-coded literals). Resource counts exclude
    // soft-deleted rows to match what ARM serves (A3).
    let subs: i64 = sqlx::query_scalar("SELECT count(*) FROM synthetic.subscriptions")
        .fetch_one(&pool)
        .await
        .unwrap();
    let rgs: i64 = sqlx::query_scalar("SELECT count(*) FROM synthetic.resource_groups")
        .fetch_one(&pool)
        .await
        .unwrap();
    let res: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM synthetic.resources WHERE drift_deleted_at IS NULL",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    let viol: i64 = sqlx::query_scalar("SELECT count(*) FROM synthetic.violations")
        .fetch_one(&pool)
        .await
        .unwrap();
    let deps: i64 = sqlx::query_scalar("SELECT count(*) FROM synthetic.dependencies")
        .fetch_one(&pool)
        .await
        .unwrap();

    let t = &body["totals"];
    assert_eq!(
        t["subscriptions"].as_i64().unwrap(),
        subs,
        "totals.subscriptions"
    );
    assert_eq!(
        t["resourceGroups"].as_i64().unwrap(),
        rgs,
        "totals.resourceGroups"
    );
    assert_eq!(t["resources"].as_i64().unwrap(), res, "totals.resources");
    assert_eq!(t["violations"].as_i64().unwrap(), viol, "totals.violations");
    assert_eq!(
        t["dependencies"].as_i64().unwrap(),
        deps,
        "totals.dependencies"
    );

    // subscriptions[]: one per subscription; each resourceCount matches an independent
    // COUNT(*) scoped to that subscription (proves the per-sub rollup is honest).
    let sub_arr = body["subscriptions"].as_array().expect("subscriptions[]");
    assert_eq!(
        sub_arr.len() as i64,
        subs,
        "one subscriptions[] entry per subscription"
    );
    for s in sub_arr {
        let sid = s["subscriptionId"].as_str().expect("subscriptionId");
        let rc: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM synthetic.resources \
             WHERE subscription_id = $1::uuid AND drift_deleted_at IS NULL",
        )
        .bind(sid)
        .fetch_one(&pool)
        .await
        .unwrap();
        assert_eq!(
            s["resourceCount"].as_i64().unwrap(),
            rc,
            "resourceCount matches COUNT(*) for {sid}"
        );
        assert!(s["name"].as_str().is_some(), "subscription has a name");
        assert!(
            s["archetype"].as_str().is_some(),
            "subscription has an archetype"
        );
        assert!(s["resourceGroupCount"].as_i64().is_some());
        assert!(s["violationCount"].as_i64().is_some());
    }
}

// -------------------------------------------------------------------------------------
// WAPI-03 — sum(subscriptions[].violationCount) == totals.violations (0-dangling
// reconciliation: the seeded violations all join to a resource under SUB_A).
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn summary_violation_reconciliation() {
    let (app, _pg, _pool, seed) = sim_app().await;
    let (status, _h, bytes) = raw_request(app, "GET", "/_sim/summary", None).await;
    assert_eq!(status, StatusCode::OK);
    let body: serde_json::Value = serde_json::from_slice(&bytes).expect("summary JSON");

    let total_viol = body["totals"]["violations"]
        .as_i64()
        .expect("totals.violations");
    let sum_per_sub: i64 = body["subscriptions"]
        .as_array()
        .expect("subscriptions[]")
        .iter()
        .map(|s| s["violationCount"].as_i64().expect("violationCount"))
        .sum();

    assert_eq!(
        sum_per_sub, total_viol,
        "sum(subscriptions[].violationCount) must reconcile to totals.violations (0-dangling)"
    );
    assert_eq!(
        total_viol, seed.violation_count,
        "totals.violations == the seeded violation count"
    );
}

// -------------------------------------------------------------------------------------
// WAPI-03 — an empty (schema-only, no tenant row) container returns 200 with zeros +
// empty arrays + null tenant metadata, never a 500 (fetch_optional, not fetch_one).
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn summary_empty_tenant() {
    let (pool, _pg) = schema_only_pg().await;
    let app = build_app(&pool, common::test_signer());
    let (status, _h, bytes) = raw_request(app, "GET", "/_sim/summary", None).await;
    assert_eq!(status, StatusCode::OK, "empty tenant → 200, never 500");
    let body: serde_json::Value = serde_json::from_slice(&bytes).expect("summary JSON");

    let t = &body["totals"];
    for k in [
        "subscriptions",
        "resourceGroups",
        "resources",
        "violations",
        "dependencies",
    ] {
        assert_eq!(t[k].as_i64().unwrap(), 0, "empty-tenant totals.{k} == 0");
    }
    assert!(
        body["subscriptions"].as_array().unwrap().is_empty(),
        "empty subscriptions[]"
    );
    assert!(
        body["byType"].as_array().unwrap().is_empty(),
        "empty byType[]"
    );
    assert!(
        body["byLocation"].as_array().unwrap().is_empty(),
        "empty byLocation[]"
    );
    assert!(body["tenantId"].is_null(), "empty-tenant tenantId is null");
    assert!(body["seed"].is_null(), "empty-tenant seed is null");
    assert!(body["profile"].is_null(), "empty-tenant profile is null");
}

// -------------------------------------------------------------------------------------
// WAPI-03 / D-14 — `summary.profile` returns the REAL generation profile NAME sourced
// from `synthetic.tenant.profile_name` (NOT `profile_version`). A seeded-but-un-updated
// tenant (seed_fixture never sets profile_name) yields `profile: null`; setting the
// column surfaces the NAME. The UPDATE goes through the test's OWN pool handle — NOT
// via seed_fixture (the fixture-coupling bar / FixtureCounts stay untouched).
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn summary_profile_name() {
    let (app, _pg, pool, _seed) = sim_app().await;

    // (a) seed_fixture inserts the tenant WITHOUT profile_name → NULL → profile: null.
    let (status, _h, bytes) = raw_request(app.clone(), "GET", "/_sim/summary", None).await;
    assert_eq!(status, StatusCode::OK);
    let body: serde_json::Value = serde_json::from_slice(&bytes).expect("summary JSON");
    assert!(
        body["profile"].is_null(),
        "un-updated tenant (profile_name NULL) → profile is null, got {:?}",
        body["profile"]
    );

    // (b) set the generation-profile NAME through the test's own pool handle, then the
    //     summary must return that NAME (D-14 supersedes the profile_version compromise).
    sqlx::query("UPDATE synthetic.tenant SET profile_name = 'enterprise-eu'")
        .execute(&pool)
        .await
        .expect("set profile_name");

    let (status, _h, bytes) = raw_request(app, "GET", "/_sim/summary", None).await;
    assert_eq!(status, StatusCode::OK);
    let body: serde_json::Value = serde_json::from_slice(&bytes).expect("summary JSON");
    assert_eq!(
        body["profile"].as_str(),
        Some("enterprise-eu"),
        "summary.profile is the profile NAME from synthetic.tenant.profile_name"
    );
}

// -------------------------------------------------------------------------------------
// WAPI-03 — arrays are deterministically ordered (two fetches are byte-identical) and
// byType `type` strings are canonicalized via casing::canonical_type.
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn summary_deterministic_order() {
    let (app, _pg, pool, seed) = sim_app().await;

    // Seed ONE resource whose stored `type` is NON-canonical (all-lowercase) and is a NEW
    // type not otherwise present in the fixture — so byType must surface its CANONICAL form
    // (proving canonicalization) without colliding with an existing canonical group.
    let vm_id = format!(
        "/subscriptions/{}/resourceGroups/{}/providers/microsoft.compute/virtualMachines/vm-canon-000",
        seed.sub,
        common::DENSE_RG_NAME
    );
    sqlx::query(
        r#"INSERT INTO synthetic.resources
               (id, subscription_id, resource_group_name, name, type, location,
                tags, sku, kind, properties, provisioning_state, managed_by)
           VALUES ($1, $2, $3, 'vm-canon-000', 'microsoft.compute/virtualmachines', 'northeurope',
                   '{}'::jsonb, NULL, NULL, '{}'::jsonb, 'Succeeded', NULL)"#,
    )
    .bind(&vm_id)
    .bind(seed.sub)
    .bind(common::DENSE_RG_NAME)
    .execute(&pool)
    .await
    .expect("insert non-canonical-typed resource");

    // Two fetches → byte-identical (deterministic ORDER BY, one read-only snapshot each).
    let (s1, _h, b1) = raw_request(app.clone(), "GET", "/_sim/summary", None).await;
    let (s2, _h, b2) = raw_request(app, "GET", "/_sim/summary", None).await;
    assert_eq!(s1, StatusCode::OK);
    assert_eq!(s2, StatusCode::OK);
    assert_eq!(
        b1, b2,
        "summary is byte-identical across repeat fetches (deterministic order)"
    );

    let body: serde_json::Value = serde_json::from_slice(&b1).expect("summary JSON");
    let by_type = body["byType"].as_array().expect("byType[]");
    let types: Vec<&str> = by_type
        .iter()
        .map(|e| e["type"].as_str().unwrap())
        .collect();
    assert!(
        types.contains(&"Microsoft.Compute/virtualMachines"),
        "byType type strings are canonicalized: {types:?}"
    );
    assert!(
        !types.contains(&"microsoft.compute/virtualmachines"),
        "the raw lowercase type must NOT appear (canonicalized): {types:?}"
    );

    // byType is ordered by count DESC (deterministic; key ASC is the tiebreak).
    let counts: Vec<i64> = by_type
        .iter()
        .map(|e| e["count"].as_i64().unwrap())
        .collect();
    let mut sorted = counts.clone();
    sorted.sort_by(|a, b| b.cmp(a));
    assert_eq!(counts, sorted, "byType ordered by count DESC");
}

// -------------------------------------------------------------------------------------
// WAPI-03 / D-15 (GAP-14-03) — the inline `summary.subscriptions[]` array is a BOUNDED
// preview: deterministically ordered by `resourceCount DESC, subscriptionId ASC` and
// capped at min(total_subscriptions, 500). Full enumeration is served by the paginated
// `GET /_sim/subscriptions` endpoint. This test seeds a LOW-subscription_id, ZERO-resource
// subscription so ordering-by-subscription_id (the PRE-cap behavior) and ordering-by-
// resourceCount-DESC produce a DIFFERENT first element — making the ordering genuinely
// assertable (not a fixture coincidence).
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn summary_subscriptions_capped_and_ordered() {
    let (app, _pg, pool, _seed) = sim_app().await;

    // A subscription whose id sorts BEFORE SUB_A (0x1111…) but has ZERO resources. Under the
    // old `ORDER BY subscription_id` this row would lead (resourceCount 0 ahead of SUB_A's
    // many); under `ORDER BY resourceCount DESC, subscription_id ASC` it is pushed after SUB_A.
    let low_sub = uuid::Uuid::from_u128(0x0000_0000_0000_0000_0000_0000_0000_0001);
    sqlx::query(
        r#"INSERT INTO synthetic.subscriptions
               (subscription_id, tenant_id, display_name, state, archetype,
                tags, authorization_source, spending_limit)
           VALUES ($1, $2, 'Zero-Resource-Sub', 'Enabled', 'prod', '{}'::jsonb, 'RoleBased', 'Off')"#,
    )
    .bind(low_sub)
    .bind(common::TENANT_ID)
    .execute(&pool)
    .await
    .expect("insert low-id zero-resource subscription");

    let (status, _h, bytes) = raw_request(app, "GET", "/_sim/summary", None).await;
    assert_eq!(status, StatusCode::OK);
    let body: serde_json::Value = serde_json::from_slice(&bytes).expect("summary JSON");
    let subs = body["subscriptions"].as_array().expect("subscriptions[]");

    // length == min(total_subscriptions, 500) — three subs (< 500) so all appear.
    let total: i64 = sqlx::query_scalar("SELECT count(*) FROM synthetic.subscriptions")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(
        subs.len() as i64,
        total.min(500),
        "subscriptions[] length == min(total, 500)"
    );

    // Ordered by resourceCount DESC, then subscriptionId ASC (deterministic).
    let ordered: Vec<(i64, String)> = subs
        .iter()
        .map(|s| {
            (
                s["resourceCount"].as_i64().unwrap(),
                s["subscriptionId"].as_str().unwrap().to_string(),
            )
        })
        .collect();
    let mut expected = ordered.clone();
    expected.sort_by(|a, b| b.0.cmp(&a.0).then(a.1.cmp(&b.1)));
    assert_eq!(
        ordered, expected,
        "subscriptions[] ordered by resourceCount DESC, then subscriptionId ASC"
    );

    // SUB_A (many resources) leads; the two zero-resource subs follow, subscriptionId ASC.
    assert_eq!(
        subs[0]["subscriptionId"].as_str().unwrap(),
        common::SUB_A.to_string(),
        "the highest-resourceCount subscription is first (NOT the lowest subscription_id)"
    );
}

// =====================================================================================
// D-16 (GAP-14-02) — STRICT fail-closed query validation on the `/_sim` collection
// endpoints. Unknown params, an out-of-domain `severity`, and a malformed `$top`/
// `$skiptoken` all return the SAME fixed JSON `ApiError` 400 (T-14-03) — NEVER axum's
// default `Query` plain-text rejection, and NEVER a misleading empty page.
// =====================================================================================

/// Assert a response is the FIXED JSON `ApiError` 400 (D-16 / T-14-03): status 400,
/// `content-type: application/json`, and body `{error:{code,message}}`. This is the SAME
/// shape every bad-input path emits — pointedly NOT axum's default plain-text `Query`
/// rejection (which is `text/plain` with no `error` object).
fn assert_fixed_json_400(status: StatusCode, headers: &HeaderMap, bytes: &Bytes) {
    assert_eq!(status, StatusCode::BAD_REQUEST, "bad input must be 400");
    let ct = headers
        .get("content-type")
        .expect("fixed 400 must carry a content-type")
        .to_str()
        .expect("content-type is ascii");
    assert!(
        ct.contains("application/json"),
        "fixed 400 must be JSON (not axum's plain-text Query rejection), got {ct:?}"
    );
    let body: serde_json::Value = serde_json::from_slice(bytes).expect("400 body is JSON");
    let err = body.get("error").expect("fixed 400 has an `error` object");
    assert!(err.get("code").is_some(), "error has a `code`");
    assert!(err.get("message").is_some(), "error has a `message`");
}

// -------------------------------------------------------------------------------------
// D-16 — an unknown query param on /_sim/violations → fixed JSON 400 (STRICT surface).
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn violations_unknown_param_is_400() {
    let (app, _pg, _pool, _seed) = sim_app().await;
    let (status, headers, bytes) = raw_request(app, "GET", "/_sim/violations?bogus=1", None).await;
    assert_fixed_json_400(status, &headers, &bytes);
}

// -------------------------------------------------------------------------------------
// D-16 — an out-of-domain `severity` → fixed JSON 400 (NOT an empty page); the in-domain
// values are accepted case-insensitively (high / HIGH / High) → 200.
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn violations_bad_severity_is_400() {
    let (app, _pg, _pool, _seed) = sim_app().await;

    // `Critical` is not in {High, Medium, Low} → fixed 400 (fail-closed, not empty page).
    let (s, h, b) = raw_request(
        app.clone(),
        "GET",
        "/_sim/violations?severity=Critical",
        None,
    )
    .await;
    assert_fixed_json_400(s, &h, &b);

    // in-domain, case-insensitive → 200 (never rejected on casing alone).
    for sev in ["high", "HIGH", "High"] {
        let (s, _h, _b) = raw_request(
            app.clone(),
            "GET",
            &format!("/_sim/violations?severity={sev}"),
            None,
        )
        .await;
        assert_eq!(
            s,
            StatusCode::OK,
            "in-domain severity `{sev}` (case-insensitive) → 200"
        );
    }
}

// -------------------------------------------------------------------------------------
// D-16 / T-14-03 — a malformed `$top` returns the SAME fixed JSON `ApiError` shape via
// the manual RawQuery parse, NOT axum's default plain-text `Query` rejection; a valid
// `$top` still works.
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn violations_bad_top_is_json_400() {
    let (app, _pg, _pool, _seed) = sim_app().await;

    // `$top=abc` is not an integer → the FIXED JSON 400 (content-type application/json).
    let (s, h, b) = raw_request(app.clone(), "GET", "/_sim/violations?$top=abc", None).await;
    assert_fixed_json_400(s, &h, &b);

    // a valid `$top` still serves a page.
    let (s, _h, _b) = raw_request(app, "GET", "/_sim/violations?$top=10", None).await;
    assert_eq!(s, StatusCode::OK, "$top=10 is a valid page size → 200");
}

// -------------------------------------------------------------------------------------
// D-16 — a valid FULL request (every documented param) is unaffected by the strict
// parse: subscription + code + severity + $top + $skiptoken-less + api-version → 200.
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn violations_valid_full_request_is_200() {
    let (app, _pg, _pool, seed) = sim_app().await;
    // STORAGE_NO_ENCRYPTION is High severity under SUB_A → this triple matches real rows.
    let uri = format!(
        "/_sim/violations?subscription={}&code={}&severity={}&$top=5&api-version=2021-04-01",
        seed.sub, seed.sample_code, seed.sample_severity
    );
    let (status, _h, bytes) = raw_request(app, "GET", &uri, None).await;
    assert_eq!(status, StatusCode::OK, "valid full request → 200");
    let (value, _n) = parse_collection(&bytes);
    assert!(
        !value.is_empty(),
        "the valid full request returns the expected (non-empty) page"
    );
}

// -------------------------------------------------------------------------------------
// D-16 — an unknown query param on /_sim/dependencies → fixed JSON 400. The documented
// set is `$top`/`$skiptoken`/`api-version`/`subscription`/`type`.
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn dependencies_unknown_param_is_400() {
    let (app, _pg, _pool, _seed) = sim_app().await;
    let (status, headers, bytes) =
        raw_request(app, "GET", "/_sim/dependencies?bogus=1", None).await;
    assert_fixed_json_400(status, &headers, &bytes);
}

// -------------------------------------------------------------------------------------
// D-16 / T-14-03 — a malformed `$top` AND a malformed `$skiptoken` on /_sim/dependencies
// both return the SAME fixed JSON `ApiError` 400 (not axum's plain-text Query rejection,
// not a 500).
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn dependencies_bad_pagination_is_json_400() {
    let (app, _pg, _pool, _seed) = sim_app().await;

    // `$top=abc` → the fixed JSON 400 via the manual RawQuery parse.
    let (s, h, b) = raw_request(app.clone(), "GET", "/_sim/dependencies?$top=abc", None).await;
    assert_fixed_json_400(s, &h, &b);

    // `$skiptoken=@@@` (not url-safe base64) → the SAME fixed JSON 400.
    let (s, h, b) = raw_request(app, "GET", "/_sim/dependencies?$skiptoken=@@@", None).await;
    assert_fixed_json_400(s, &h, &b);
}

// -------------------------------------------------------------------------------------
// WR-01 — a decoded ASCII control character (NUL `%00` and the rest of C0 + DEL `%7f`) in
// an OPEN-DOMAIN filter value (`code`/`resource` on violations, `type` on dependencies)
// must be the SAME fixed JSON 400 — NOT an HTTP 500 (Postgres rejecting a NUL after the
// value binds) and NOT a misleading empty 200 page. Proves the encoded control is stopped
// at the decode choke point and never reaches SQL, upholding the D-16 fail-closed contract.
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn control_char_in_open_domain_filter_is_json_400() {
    let (app, _pg, _pool, _seed) = sim_app().await;

    // NUL is the headline case; DEL and a mid-C0 control exercise the full class.
    for ctrl in ["%00", "%1f", "%7f"] {
        for uri in [
            format!("/_sim/violations?code=prod{ctrl}x"),
            format!("/_sim/violations?resource={ctrl}"),
            format!("/_sim/dependencies?type=peer{ctrl}"),
        ] {
            let (s, h, b) = raw_request(app.clone(), "GET", &uri, None).await;
            // assert_fixed_json_400 asserts status == 400 (so NOT the 500 WR-01 described,
            // and NOT a 200 empty page) AND the fixed `{error:{code,message}}` JSON shape.
            assert_fixed_json_400(s, &h, &b);
        }
    }
}

// -------------------------------------------------------------------------------------
// D-16 — a valid `?subscription=<uuid>&type=<t>&$top=2` request is unaffected by the
// strict parse: 200 with the nested dependency shape + `count`.
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn dependencies_valid_full_request_is_200() {
    let (app, _pg, _pool, seed) = sim_app().await;
    // SUB_A is the source of every seeded edge; vnet-peering exists → the page is non-empty.
    let uri = format!(
        "/_sim/dependencies?subscription={}&type={}&$top=2",
        seed.sub, seed.sample_dependency_type
    );
    let (status, _h, bytes) = raw_request(app, "GET", &uri, None).await;
    assert_eq!(status, StatusCode::OK, "valid full request → 200");
    let body: serde_json::Value = serde_json::from_slice(&bytes).expect("dependencies JSON");
    assert!(
        body["count"].as_i64().is_some(),
        "the nested envelope carries a count"
    );
    let value = body["value"].as_array().expect("value[]");
    assert!(!value.is_empty(), "vnet-peering under SUB_A is non-empty");
    // Nested spec shape (D-13) survives the strict parse.
    assert!(
        value[0]["source"]["subscriptionId"].as_str().is_some(),
        "nested source.subscriptionId present"
    );
}

// =====================================================================================
// WAPI-03 / D-15 (GAP-14-03) — the NEW keyset-paginated `GET /_sim/subscriptions` endpoint
// (the fourth /_sim GET route, superseding D-01). UUID keyset on `subscription_id`, D-13
// `count`, fail-closed D-16, read-only (405 on mutation). Closes the T-14-05 residual by
// serving FULL subscription enumeration (the inline summary preview is capped at 500).
// =====================================================================================

// -------------------------------------------------------------------------------------
// WAPI-03 — the envelope is `{ count, value:[{subscriptionId,name,archetype,resourceCount,
// resourceGroupCount,violationCount}], nextLink? }`, matching the summary per-sub rollup
// shape; `count` == the total subscription COUNT(*).
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn subscriptions_envelope_and_shape() {
    let (app, _pg, pool, _seed) = sim_app().await;
    let (status, _h, bytes) = raw_request(app, "GET", "/_sim/subscriptions", None).await;
    assert_eq!(status, StatusCode::OK);
    let body: serde_json::Value = serde_json::from_slice(&bytes).expect("subscriptions JSON");

    let total: i64 = sqlx::query_scalar("SELECT count(*) FROM synthetic.subscriptions")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(
        body["count"].as_i64().expect("envelope has a count"),
        total,
        "count == total subscription COUNT(*) (D-13)"
    );

    let value = body["value"].as_array().expect("value[]");
    assert_eq!(
        value.len() as i64,
        total,
        "the fixture's 2 subs fit in one default page (no cap ≤ 100)"
    );
    for s in value {
        // Per-sub rollup shape — the SAME fields as summary.subscriptions[].
        assert!(s["subscriptionId"].as_str().is_some(), "subscriptionId");
        assert!(s["name"].as_str().is_some(), "name");
        assert!(s["archetype"].as_str().is_some(), "archetype");
        assert!(s["resourceCount"].as_i64().is_some(), "resourceCount");
        assert!(
            s["resourceGroupCount"].as_i64().is_some(),
            "resourceGroupCount"
        );
        assert!(s["violationCount"].as_i64().is_some(), "violationCount");
    }
}

// -------------------------------------------------------------------------------------
// WAPI-03 / D-06 — keyset traversal with $top=1 across the 2 seeded subs visits every
// subscription exactly once (no gaps / no dupes); `count` == total; nextLink preserves
// api-version.
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn subscriptions_keyset_traversal_no_gaps() {
    let (app, _pg, pool, _seed) = sim_app().await;

    let (items, links) = walk_all(&app, "/_sim/subscriptions?$top=1&api-version=2021-04-01").await;
    let mut ids: Vec<String> = items
        .iter()
        .map(|s| s["subscriptionId"].as_str().unwrap().to_string())
        .collect();
    let visited = ids.len();
    ids.sort();
    ids.dedup();
    assert_eq!(ids.len(), visited, "no subscription appears on two pages");

    let total: i64 = sqlx::query_scalar("SELECT count(*) FROM synthetic.subscriptions")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(
        visited as i64, total,
        "every seeded subscription visited exactly once (no gaps)"
    );

    // A multi-page ($top=1 over 2 subs) traversal must emit ≥1 nextLink preserving api-version.
    assert!(!links.is_empty(), "$top=1 over 2 subs forces a nextLink");
    assert!(
        links[0].contains("api-version=2021-04-01"),
        "nextLink preserves api-version: {}",
        links[0]
    );
}

// -------------------------------------------------------------------------------------
// D-16 — fail-closed on the new endpoint: an unknown param and a malformed `$top` both
// return the fixed JSON `ApiError` 400 (the documented set is the pagination trio only).
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn subscriptions_fail_closed() {
    let (app, _pg, _pool, _seed) = sim_app().await;

    // Unknown key (no filters in v1) → fixed JSON 400.
    let (s, h, b) = raw_request(app.clone(), "GET", "/_sim/subscriptions?bogus=1", None).await;
    assert_fixed_json_400(s, &h, &b);

    // Malformed `$top` → the SAME fixed JSON 400 (not axum's plain-text Query rejection).
    let (s, h, b) = raw_request(app.clone(), "GET", "/_sim/subscriptions?$top=abc", None).await;
    assert_fixed_json_400(s, &h, &b);

    // Malformed `$skiptoken` (valid base64 "hello", not a UUID) → the SAME fixed JSON 400.
    let (s, h, b) = raw_request(app, "GET", "/_sim/subscriptions?$skiptoken=aGVsbG8", None).await;
    assert_fixed_json_400(s, &h, &b);
}

// -------------------------------------------------------------------------------------
// D-12.3 — the new route is read-only: POST /_sim/subscriptions → 405 + `Allow: GET`.
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn subscriptions_method_not_allowed() {
    let (app, _pg, _pool, _seed) = sim_app().await;
    for method in ["POST", "PUT", "PATCH", "DELETE"] {
        let (status, headers, _b) =
            raw_request(app.clone(), method, "/_sim/subscriptions", None).await;
        assert_eq!(
            status,
            StatusCode::METHOD_NOT_ALLOWED,
            "{method} /_sim/subscriptions must be 405 (read-only, structural)"
        );
        let allow = headers
            .get("allow")
            .expect("405 must carry an Allow header")
            .to_str()
            .expect("Allow header is ascii");
        assert!(
            allow.contains("GET"),
            "Allow must advertise GET, got {allow:?}"
        );
    }
}

// -------------------------------------------------------------------------------------
// WAPI-04 route-count contract (15-14 adds /resources/search to the D-15 four) — EXACTLY
// five /_sim GET routes exist: violations, dependencies, summary, subscriptions, and
// resources/search. No more (drift/identity stay deferred — D-02/D-03), and no other prefix
// resolves.
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn sim_route_count_is_five() {
    let (app, _pg, _pool, _seed) = sim_app().await;

    // The FIVE documented routes each resolve (bearer-exempt → 200). `resources/search`
    // requires a `q`, so the probe carries one.
    for uri in [
        "/_sim/violations",
        "/_sim/dependencies",
        "/_sim/summary",
        "/_sim/subscriptions",
        "/_sim/resources/search?q=res-",
    ] {
        let (status, _h, _b) = raw_request(app.clone(), "GET", uri, None).await;
        assert_eq!(
            status,
            StatusCode::OK,
            "{uri} is one of the five /_sim routes"
        );
    }

    // Deferred / non-existent /_sim routes must 404 (proving no extras; bare `/_sim/resources`
    // is NOT a route — only the `/resources/search` leaf is).
    for uri in [
        "/_sim/resources",
        "/_sim/drift",
        "/_sim/identity",
        "/_sim/roleAssignments",
    ] {
        let (status, _h, _b) = raw_request(app.clone(), "GET", uri, None).await;
        assert_eq!(
            status,
            StatusCode::NOT_FOUND,
            "{uri} must NOT be a /_sim route (drift/identity deferred D-02/D-03; bare /_sim/resources 404s)"
        );
    }
}

// =====================================================================================
// 15-14 (EXPL-01 / EXPL-05) — the NEW bearer-exempt `GET /_sim/resources/search` endpoint:
// tenant-wide name/type substring search, keyset-paginated on the TEXT `id` PK, optionally
// subscription-scoped, soft-delete-excluded, fail-closed (D-16). Ground truth comes from an
// independent live COUNT(*) over the SAME `name ILIKE OR type ILIKE` predicate — never a
// hardcoded literal (the seed_fixture rows are never mutated; the soft-delete test uses the
// test's OWN pool handle, mirroring `summary_profile_name`).
// =====================================================================================

/// Independent ground truth: the COUNT(*) of resources matching `name ILIKE '%q%' OR type
/// ILIKE '%q%'`, soft-deleted rows excluded — the exact predicate `search_resources` applies.
async fn search_ground_truth(pool: &PgPool, q: &str) -> i64 {
    sqlx::query_scalar(
        "SELECT count(*) FROM synthetic.resources \
         WHERE (name ILIKE '%' || $1 || '%' OR type ILIKE '%' || $1 || '%') \
           AND drift_deleted_at IS NULL",
    )
    .bind(q)
    .fetch_one(pool)
    .await
    .unwrap()
}

// -------------------------------------------------------------------------------------
// Shape (D-10) — a `?q=res-` hit returns rows whose keys are EXACTLY
// {id,name,type,subscriptionId,resourceGroupName} (camelCase); `count` is present and equals
// the independent ground truth.
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn search_shape_and_keys() {
    let (app, _pg, pool, _seed) = sim_app().await;
    let (status, _h, bytes) =
        raw_request(app, "GET", "/_sim/resources/search?q=res-&$top=1000", None).await;
    assert_eq!(status, StatusCode::OK);
    let body: serde_json::Value = serde_json::from_slice(&bytes).expect("search JSON");

    let ground = search_ground_truth(&pool, "res-").await;
    assert!(ground > 0, "the fixture has res- named resources");
    assert_eq!(
        body["count"].as_i64().expect("envelope has a count"),
        ground,
        "count == independent COUNT(*) of the name/type ILIKE predicate"
    );

    let value = body["value"].as_array().expect("value[]");
    assert_eq!(
        value.len() as i64,
        ground,
        "$top=1000 returns the whole result set on this small fixture"
    );
    for r in value {
        let obj = r.as_object().expect("row object");
        let mut keys: Vec<&str> = obj.keys().map(|k| k.as_str()).collect();
        keys.sort();
        assert_eq!(
            keys,
            vec!["id", "name", "resourceGroupName", "subscriptionId", "type"],
            "row carries EXACTLY the camelCase key set (D-10)"
        );
        assert!(r["id"].as_str().is_some());
        assert!(r["subscriptionId"].as_str().is_some());
    }
}

// -------------------------------------------------------------------------------------
// Substring match — a `q` that matches only NAMEs and a distinct `q` that matches only a
// TYPE segment each return rows == an independent COUNT(*) of the name/type ILIKE predicate.
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn search_matches_name_and_type() {
    let (app, _pg, pool, _seed) = sim_app().await;

    // NAME-only substring: "flt" is present in the flt-000x names, absent from every type.
    let name_ground = search_ground_truth(&pool, "flt").await;
    assert!(name_ground > 0, "flt-000x names exist");
    let (s, _h, b) = raw_request(app.clone(), "GET", "/_sim/resources/search?q=flt", None).await;
    assert_eq!(s, StatusCode::OK);
    let body: serde_json::Value = serde_json::from_slice(&b).expect("search JSON");
    assert_eq!(body["count"].as_i64().unwrap(), name_ground);
    for r in body["value"].as_array().unwrap() {
        assert!(
            r["name"].as_str().unwrap().to_lowercase().contains("flt"),
            "a name-substring hit's name contains the term"
        );
    }

    // TYPE-only substring: "virtualNetworks" is a type segment, absent from every name.
    let type_ground = search_ground_truth(&pool, "virtualNetworks").await;
    assert!(type_ground > 0, "vnet-typed rows exist");
    let (s, _h, b) =
        raw_request(app, "GET", "/_sim/resources/search?q=virtualNetworks", None).await;
    assert_eq!(s, StatusCode::OK);
    let body: serde_json::Value = serde_json::from_slice(&b).expect("search JSON");
    assert_eq!(body["count"].as_i64().unwrap(), type_ground);
    for r in body["value"].as_array().unwrap() {
        assert!(
            r["type"]
                .as_str()
                .unwrap()
                .to_lowercase()
                .contains("virtualnetworks"),
            "a type-substring hit's type contains the term"
        );
    }
    // The two predicates select DIFFERENT rows (else the name/type distinction is vacuous).
    assert_ne!(name_ground, type_ground);
}

// -------------------------------------------------------------------------------------
// RG-name search (live UAT) — a term that names RESOURCE GROUPS, not resources, surfaces in
// the bounded `resourceGroups[]` section even though it matches ZERO resource rows. This is
// the exact gap that made a `rg-corp-...` search look empty: resources are named unlike their
// RGs, so RG names only surface here.
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn search_matches_resource_group_name() {
    let (app, _pg, pool, _seed) = sim_app().await;

    // "rg-dense" names the >100 dense RGs but no resource (resources are res-/typed names).
    let resource_ground = search_ground_truth(&pool, "rg-dense").await;
    assert_eq!(
        resource_ground, 0,
        "no RESOURCE is named rg-dense (the gap)"
    );

    let (s, _h, b) = raw_request(app, "GET", "/_sim/resources/search?q=rg-dense", None).await;
    assert_eq!(s, StatusCode::OK);
    let body: serde_json::Value = serde_json::from_slice(&b).expect("search JSON");

    // Zero resource hits — but the RG section is populated (the whole point of this fix).
    assert_eq!(body["count"].as_i64().unwrap(), 0);
    assert!(body["value"].as_array().unwrap().is_empty());
    let rgs = body["resourceGroups"]
        .as_array()
        .expect("search envelope carries resourceGroups[]");
    assert!(!rgs.is_empty(), "rg-dense RGs surface in resourceGroups[]");
    assert!(
        rgs.len() as i64 <= 50,
        "resourceGroups[] is bounded at SEARCH_RESOURCE_GROUPS_CAP (50)"
    );
    for g in rgs {
        let mut keys: Vec<&str> = g.as_object().unwrap().keys().map(|k| k.as_str()).collect();
        keys.sort();
        assert_eq!(keys, vec!["name", "subscriptionId"], "RG hit key set");
        assert!(
            g["name"]
                .as_str()
                .unwrap()
                .to_lowercase()
                .contains("rg-dense"),
            "each RG hit's name contains the term"
        );
        assert!(
            g["subscriptionId"].as_str().is_some(),
            "RG hit carries its sub"
        );
    }
}

// -------------------------------------------------------------------------------------
// Subscription scope — `?q=<hit>&subscription=<SUB_A>` narrows to SUB_A; a malformed
// `subscription` is a fixed 400 (parsed to Uuid BEFORE SQL, never a 500).
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn search_subscription_scope() {
    let (app, _pg, pool, _seed) = sim_app().await;

    // Every fixture resource lives under SUB_A, so scoping to SUB_A is a no-op on the count
    // but proves the bound predicate holds; scoping to SUB_B narrows to zero.
    let ground = search_ground_truth(&pool, "storageAccounts").await;
    assert!(ground > 0);
    let uri_a = format!(
        "/_sim/resources/search?q=storageAccounts&subscription={}&$top=1000",
        common::SUB_A
    );
    let (s, _h, b) = raw_request(app.clone(), "GET", &uri_a, None).await;
    assert_eq!(s, StatusCode::OK);
    let body: serde_json::Value = serde_json::from_slice(&b).expect("search JSON");
    assert_eq!(
        body["count"].as_i64().unwrap(),
        ground,
        "SUB_A holds every hit"
    );
    let sub_a = common::SUB_A.to_string();
    for r in body["value"].as_array().unwrap() {
        assert_eq!(r["subscriptionId"].as_str(), Some(sub_a.as_str()));
    }

    // Scope to SUB_B (no matching resources) → zero.
    let uri_b = format!(
        "/_sim/resources/search?q=storageAccounts&subscription={}",
        common::SUB_B
    );
    let (s, _h, b) = raw_request(app.clone(), "GET", &uri_b, None).await;
    assert_eq!(s, StatusCode::OK);
    let body: serde_json::Value = serde_json::from_slice(&b).expect("search JSON");
    assert_eq!(
        body["count"].as_i64().unwrap(),
        0,
        "SUB_B has no matching rows"
    );
    assert!(body["value"].as_array().unwrap().is_empty());

    // Malformed subscription → fixed JSON 400 (never a 500).
    let (s, h, b) = raw_request(
        app,
        "GET",
        "/_sim/resources/search?q=res-&subscription=not-a-uuid",
        None,
    )
    .await;
    assert_fixed_json_400(s, &h, &b);
}

// -------------------------------------------------------------------------------------
// Keyset traversal — a small `$top` visits every match exactly once (no gaps/dupes); the
// page-2 nextLink preserves BOTH `q` and `subscription`.
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn search_keyset_traversal_no_gaps() {
    let (app, _pg, pool, _seed) = sim_app().await;

    // Unfiltered traversal with a small $top.
    let (items, _l) = walk_all(&app, "/_sim/resources/search?q=res-&$top=2").await;
    let mut ids: Vec<String> = items
        .iter()
        .map(|r| r["id"].as_str().unwrap().to_string())
        .collect();
    let visited = ids.len();
    ids.sort();
    ids.dedup();
    assert_eq!(ids.len(), visited, "no resource appears on two pages");
    assert_eq!(
        visited as i64,
        search_ground_truth(&pool, "res-").await,
        "every match visited exactly once (no gaps)"
    );

    // Filtered traversal: the page-2 nextLink preserves BOTH q and subscription.
    let first = format!(
        "/_sim/resources/search?q=storageAccounts&subscription={}&$top=2",
        common::SUB_A
    );
    let (_items, links) = walk_all(&app, &first).await;
    assert!(!links.is_empty(), "a multi-page traversal emits a nextLink");
    let page2 = &links[0];
    assert!(
        page2.contains("q=storageAccounts"),
        "nextLink preserves q: {page2}"
    );
    assert!(
        page2.contains(&format!("subscription={}", common::SUB_A)),
        "nextLink preserves subscription: {page2}"
    );
}

// -------------------------------------------------------------------------------------
// Fail-closed (D-16) — unknown param, bad `$top`, malformed `$skiptoken`, AND missing/empty
// `q` each return the fixed JSON `ApiError` 400.
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn search_fail_closed() {
    let (app, _pg, _pool, _seed) = sim_app().await;

    for uri in [
        "/_sim/resources/search?q=res-&bogus=1",  // unknown param
        "/_sim/resources/search?q=res-&$top=abc", // bad $top
        "/_sim/resources/search?q=res-&$skiptoken=@@@", // malformed $skiptoken
        "/_sim/resources/search",                 // missing q
        "/_sim/resources/search?q=",              // empty q
        "/_sim/resources/search?q=%20%20",        // all-whitespace q
    ] {
        let (s, h, b) = raw_request(app.clone(), "GET", uri, None).await;
        assert_fixed_json_400(s, &h, &b);
    }
}

// -------------------------------------------------------------------------------------
// Soft-delete — a matching row hidden via `drift_deleted_at` (through the TEST's own pool
// handle, NOT seed_fixture) disappears from BOTH the result page and `count`.
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn search_excludes_soft_deleted() {
    let (app, _pg, pool, _seed) = sim_app().await;

    let before = search_ground_truth(&pool, "flt").await;
    assert!(before > 1, "several flt-000x rows match");

    // Soft-delete ONE matching row (flt-0000) via the test's own pool handle.
    let flt_0000 = format!(
        "/subscriptions/{}/resourceGroups/{}/providers/{}/flt-0000",
        common::SUB_A,
        common::FILTER_RG_NAME,
        common::FILTER_TYPE_STORAGE
    );
    sqlx::query("UPDATE synthetic.resources SET drift_deleted_at = now() WHERE id = $1")
        .bind(&flt_0000)
        .execute(&pool)
        .await
        .expect("soft-delete a matching resource");

    let (s, _h, b) = raw_request(app, "GET", "/_sim/resources/search?q=flt&$top=1000", None).await;
    assert_eq!(s, StatusCode::OK);
    let body: serde_json::Value = serde_json::from_slice(&b).expect("search JSON");
    assert_eq!(
        body["count"].as_i64().unwrap(),
        before - 1,
        "count drops by one after the soft-delete"
    );
    for r in body["value"].as_array().unwrap() {
        assert_ne!(
            r["id"].as_str(),
            Some(flt_0000.as_str()),
            "the soft-deleted row is absent from the page"
        );
    }
}

// -------------------------------------------------------------------------------------
// Read-only (D-12.3) — POST/PUT/PATCH/DELETE /_sim/resources/search → 405 + `Allow: GET`.
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn search_method_not_allowed() {
    let (app, _pg, _pool, _seed) = sim_app().await;
    for method in ["POST", "PUT", "PATCH", "DELETE"] {
        let (status, headers, _b) =
            raw_request(app.clone(), method, "/_sim/resources/search?q=res-", None).await;
        assert_eq!(
            status,
            StatusCode::METHOD_NOT_ALLOWED,
            "{method} /_sim/resources/search must be 405 (read-only, structural)"
        );
        let allow = headers
            .get("allow")
            .expect("405 must carry an Allow header")
            .to_str()
            .expect("Allow header is ascii");
        assert!(
            allow.contains("GET"),
            "Allow must advertise GET, got {allow:?}"
        );
    }
}

// =====================================================================================
// 15-15 (EXPL-GAP-01) — subscription-NAME search: a term matching a subscription's name
// returns that subscription's resources AND a bounded `subscriptions` array of the matching
// subscriptions. Fixture: SUB_A "Contoso-Prod-A", SUB_B "Contoso-Dev-B" (seed_fixture). No
// resource name/type contains "contoso"/"dev", so those hits are reachable ONLY via the
// sub-name subquery — ground truth from an independent COUNT, never a hardcoded literal.
// =====================================================================================

/// The cap the handler applies to the `subscriptions` array (`SEARCH_SUBSCRIPTIONS_CAP`).
/// Duplicated here because the const is crate-internal; the bound is asserted structurally.
const SEARCH_SUBSCRIPTIONS_CAP: usize = 50;

/// `q=contoso` matches BOTH subscription names but NO resource name/type → every active
/// resource is returned (strictly more than the name/type-only predicate), and `subscriptions`
/// carries BOTH `{id,name}` entries ordered by name ASC, bounded by the cap.
#[tokio::test]
async fn search_matches_subscription_name() {
    let (app, _pg, pool, _seed) = sim_app().await;

    let name_type_ground = search_ground_truth(&pool, "contoso").await;
    assert_eq!(
        name_type_ground, 0,
        "no resource name/type contains 'contoso' — the hit is sub-name-only"
    );
    let all_active: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM synthetic.resources WHERE drift_deleted_at IS NULL",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert!(all_active > 0, "the fixture has active resources");

    let (s, _h, b) = raw_request(
        app,
        "GET",
        "/_sim/resources/search?q=contoso&$top=1000",
        None,
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    let body: serde_json::Value = serde_json::from_slice(&b).expect("search JSON");

    let count = body["count"].as_i64().expect("count");
    assert!(
        count > name_type_ground,
        "sub-name match returns resources beyond the name/type predicate ({count} > {name_type_ground})"
    );
    assert_eq!(
        count, all_active,
        "both contoso subs match → every active resource is returned"
    );

    let subs = body["subscriptions"].as_array().expect("subscriptions[]");
    assert!(
        subs.len() <= SEARCH_SUBSCRIPTIONS_CAP,
        "subscriptions array is bounded by the cap"
    );
    let names: Vec<&str> = subs.iter().map(|x| x["name"].as_str().unwrap()).collect();
    assert_eq!(
        names,
        vec!["Contoso-Dev-B", "Contoso-Prod-A"],
        "both Contoso subs, ordered by name ASC"
    );
    for x in subs {
        let obj = x.as_object().expect("subscription object");
        let mut keys: Vec<&str> = obj.keys().map(|k| k.as_str()).collect();
        keys.sort();
        assert_eq!(
            keys,
            vec!["id", "name"],
            "subscription row carries {{id,name}}"
        );
        assert!(
            uuid::Uuid::parse_str(x["id"].as_str().unwrap()).is_ok(),
            "id is a synthetic subscription UUID"
        );
    }
}

/// `q=dev` matches ONLY "Contoso-Dev-B" (SUB_B). A resource inserted under SUB_B (its name/type
/// contain no "dev") is reachable ONLY via the sub-name subquery, and `subscriptions` contains
/// exactly the Contoso-Dev-B entry. The row is inserted via the test's OWN pool handle (fresh
/// container per test) so the shared fixture / earlier count assertions are untouched.
#[tokio::test]
async fn search_reaches_resource_via_subscription_name() {
    let (app, _pg, pool, _seed) = sim_app().await;

    // Insert one SUB_B resource whose name/type contain neither "dev" nor "contoso".
    sqlx::query(
        r#"INSERT INTO synthetic.resources
               (id, subscription_id, resource_group_name, name, type, location,
                tags, sku, kind, properties, provisioning_state, managed_by)
           VALUES ($1, $2, 'rg-b-000', 'vnet-b-000',
                   'Microsoft.Network/virtualNetworks', 'eastus',
                   '{}'::jsonb, NULL, NULL, '{}'::jsonb, 'Succeeded', NULL)"#,
    )
    .bind(common::SIM_SUB_B_RESOURCE_ID)
    .bind(common::SUB_B)
    .execute(&pool)
    .await
    .expect("insert SUB_B resource");

    let (s, _h, b) = raw_request(app, "GET", "/_sim/resources/search?q=dev&$top=1000", None).await;
    assert_eq!(s, StatusCode::OK);
    let body: serde_json::Value = serde_json::from_slice(&b).expect("search JSON");

    let ids: Vec<&str> = body["value"]
        .as_array()
        .expect("value[]")
        .iter()
        .map(|r| r["id"].as_str().unwrap())
        .collect();
    assert!(
        ids.contains(&common::SIM_SUB_B_RESOURCE_ID),
        "the SUB_B resource is reachable via its subscription name (sub-name-only): {ids:?}"
    );

    let subs = body["subscriptions"].as_array().expect("subscriptions[]");
    let names: Vec<&str> = subs.iter().map(|x| x["name"].as_str().unwrap()).collect();
    assert_eq!(
        names,
        vec!["Contoso-Dev-B"],
        "only the Dev sub matches 'dev'"
    );
    assert_eq!(
        subs[0]["id"].as_str().unwrap(),
        common::SUB_B.to_string(),
        "the matching subscription id is SUB_B"
    );
}

/// A term normalized from `*` works end-to-end: `q=contoso*` behaves EXACTLY like `q=contoso`
/// (same count + same `subscriptions` array).
#[tokio::test]
async fn search_normalizes_literal_asterisk() {
    let (app, _pg, _pool, _seed) = sim_app().await;

    let (s1, _h, b1) = raw_request(
        app.clone(),
        "GET",
        "/_sim/resources/search?q=contoso&$top=1000",
        None,
    )
    .await;
    let (s2, _h, b2) = raw_request(
        app,
        "GET",
        "/_sim/resources/search?q=contoso*&$top=1000",
        None,
    )
    .await;
    assert_eq!(s1, StatusCode::OK);
    assert_eq!(s2, StatusCode::OK);
    let body1: serde_json::Value = serde_json::from_slice(&b1).expect("search JSON");
    let body2: serde_json::Value = serde_json::from_slice(&b2).expect("search JSON");

    assert_eq!(
        body1["count"], body2["count"],
        "contoso* behaves like contoso (count)"
    );
    assert_eq!(
        body1["subscriptions"], body2["subscriptions"],
        "contoso* behaves like contoso (subscriptions array)"
    );
}

/// WR-02: a term containing the ILIKE wildcard `%` is matched LITERALLY (via `ESCAPE '\'`), so
/// `q=%` does NOT collapse to `ILIKE '%%%'` and scan the whole tenant. It matches only rows whose
/// name/type/subscription-name literally contains `%` (none in the fixture), NOT every resource —
/// upholding the T-15-24 "no whole-table scan" intent that a bare-`%` term would otherwise bypass.
#[tokio::test]
async fn search_percent_is_literal_not_wildcard() {
    let (app, _pg, pool, _seed) = sim_app().await;

    let all_active: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM synthetic.resources WHERE drift_deleted_at IS NULL",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert!(all_active > 0, "the fixture has active resources");

    // No fixture name/type/sub-name literally contains '%', so a LITERAL '%' search matches nothing.
    let literal_pct_ground: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM synthetic.resources WHERE drift_deleted_at IS NULL \
           AND (name LIKE '%\\%%' OR type LIKE '%\\%%')",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(
        literal_pct_ground, 0,
        "no fixture resource name/type literally contains '%'"
    );

    // `q=%` (percent-encoded `%25`). With ESCAPE the `%` is a LITERAL, not a wildcard.
    let (s, _h, b) = raw_request(app, "GET", "/_sim/resources/search?q=%25&$top=1000", None).await;
    assert_eq!(s, StatusCode::OK);
    let body: serde_json::Value = serde_json::from_slice(&b).expect("search JSON");

    let count = body["count"].as_i64().expect("count");
    assert!(
        count < all_active,
        "q=% must NOT match the whole tenant — literal '%' is not a wildcard ({count} < {all_active})"
    );
    assert_eq!(
        count, literal_pct_ground,
        "q=% matches only rows whose name/type LITERALLY contain '%' (zero here), not all rows"
    );
    assert!(
        body["subscriptions"]
            .as_array()
            .expect("subscriptions[]")
            .is_empty(),
        "no subscription name literally contains '%', so the sub-name match is empty too"
    );
}

// =====================================================================================
// Phase 16 (CONS-01 / D-02 / D-03) — the bearer-exempt console `/history` series + the
// `/_console` → `/ui/console` 302 redirect. Both live on the uninstrumented console
// router (never inside `arm`), so `arm_byte_identical` (above) stays green.
// =====================================================================================

// -------------------------------------------------------------------------------------
// D-03 / CONS-01 — GET /_console/history returns the aggregate bucket series with NO
// Authorization header (bearer-exempt, same seam as /_console/stats): bucket_ms == 1000,
// exactly WINDOW_BUCKETS (300) buckets oldest→newest, and an untouched bucket carries
// count 0 + NULL percentiles (never Some(0) — absence is not zero latency, Pitfall 5).
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn console_history_shape() {
    let (app, _pg, _pool, _seed) = sim_app().await;

    // NO bearer — the console router is bearer-exempt.
    let (status, _h, bytes) = raw_request(app, "GET", "/_console/history", None).await;
    assert_eq!(
        status,
        StatusCode::OK,
        "/_console/history is reachable with no auth"
    );

    let body: serde_json::Value = serde_json::from_slice(&bytes).expect("history JSON");

    // Granularity + window contract (D-04): 1-second buckets, `server_now_ms` present so
    // the client can align its x-axis to "now".
    assert_eq!(body["bucket_ms"].as_u64(), Some(1000), "1-second buckets");
    assert!(
        body["server_now_ms"].as_u64().is_some(),
        "server_now_ms present for x-axis alignment"
    );

    let buckets = body["buckets"].as_array().expect("buckets[]");
    // WINDOW_BUCKETS is a private server const (300); assert both the literal AND the
    // internal window_ms/bucket_ms consistency so a retune of either is caught.
    assert_eq!(buckets.len(), 300, "exactly WINDOW_BUCKETS (300) buckets");
    let window_ms = body["window_ms"].as_u64().expect("window_ms");
    let bucket_ms = body["bucket_ms"].as_u64().expect("bucket_ms");
    assert_eq!(
        buckets.len() as u64,
        window_ms / bucket_ms,
        "buckets.len() == window_ms / bucket_ms (internally consistent)"
    );

    // Oldest → newest ordering (monotonic non-decreasing ts_ms).
    for pair in buckets.windows(2) {
        let a = pair[0]["ts_ms"].as_u64().expect("ts_ms");
        let b = pair[1]["ts_ms"].as_u64().expect("ts_ms");
        assert!(a <= b, "buckets are oldest→newest");
    }

    // No ARM traffic hit the (bearer-exempt, uninstrumented) console router, so EVERY
    // bucket is empty: count 0 with NULL percentiles — NEVER Some(0) (Pitfall 5).
    for b in buckets {
        assert_eq!(b["count"].as_u64(), Some(0), "untouched bucket count == 0");
        assert!(b["p50_ms"].is_null(), "empty bucket p50 is null, not 0");
        assert!(b["p95_ms"].is_null(), "empty bucket p95 is null, not 0");
        assert!(b["max_ms"].is_null(), "empty bucket max is null, not 0");
    }
}

// -------------------------------------------------------------------------------------
// D-02 — GET /_console returns an EXACT 302 Found to /ui/console (axum 0.8 `Redirect` has
// NO 302 constructor — the handler builds StatusCode::FOUND + a static Location literal,
// so no user input is reflected). The legacy embedded HTML page is gone: the body is NOT
// an HTML document.
// -------------------------------------------------------------------------------------
#[tokio::test]
async fn console_redirect_302() {
    let (app, _pg, _pool, _seed) = sim_app().await;

    // NO bearer — bearer-exempt console seam; oneshot does NOT follow the redirect.
    let (status, headers, bytes) = raw_request(app, "GET", "/_console", None).await;
    assert_eq!(
        status,
        StatusCode::FOUND,
        "/_console is an exact 302 Found (not 303/307/308, not a 200 HTML page)"
    );

    let location = headers
        .get("location")
        .expect("302 must carry a Location header")
        .to_str()
        .expect("Location is ascii");
    assert_eq!(
        location, "/ui/console",
        "302 redirects to the React console at /ui/console"
    );

    // The legacy console.html is gone — the response body is NOT an HTML document.
    let body = String::from_utf8_lossy(&bytes).to_lowercase();
    assert!(
        !body.contains("<html") && !body.contains("<!doctype"),
        "302 body must not be the legacy HTML page, got: {body:?}"
    );
}
