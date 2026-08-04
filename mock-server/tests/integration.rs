//! Integration tests driving the real router against an ephemeral testcontainers
//! Postgres seeded by `common::seed_fixture`. The endpoint behavior tests
//! (subscriptions ARM shape, Bearer gate, CloudError, api-version) land in Task 3;
//! Wave 1 ships the harness + a smoke test that proves the fixture itself.

mod common;

use sqlx::PgPool;
use tenantless_server::{build_router, metrics::Metrics, state::AppState};
use testcontainers_modules::{postgres, testcontainers::runners::AsyncRunner};

/// Start an ephemeral Postgres container and return its connection URL plus the container
/// guard (kept alive for the test's duration). Lets a test build a CUSTOM pool (a tight
/// `statement_timeout`, a single-connection pool, …) against the same container.
async fn start_pg_url() -> (String, testcontainers::ContainerAsync<postgres::Postgres>) {
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
    (url, container)
}

/// Start an ephemeral Postgres container and return a connected pool plus the
/// container guard (kept alive for the test's duration).
async fn start_pg() -> (PgPool, testcontainers::ContainerAsync<postgres::Postgres>) {
    let (url, container) = start_pg_url().await;
    let pool = PgPool::connect(&url).await.expect("connect pool");
    (pool, container)
}

/// Smoke test: the harness starts a container, applies sql/001+002, seeds the
/// fixture, builds the router, and confirms the expected row counts so later
/// pagination waves have a known, exercisable dataset.
#[tokio::test]
async fn harness_seeds_known_fixture() {
    let (pool, _container) = start_pg().await;
    let counts = common::seed_fixture(&pool).await;

    // The fixture guarantees: 2 subs, >100 RGs in sub A, >100 resources in one RG.
    assert_eq!(counts.subscriptions, 2);
    assert!(
        counts.resource_groups_sub_a > 100,
        "expected >100 RGs in sub A, got {}",
        counts.resource_groups_sub_a
    );
    assert!(
        counts.resources_dense_rg > 100,
        "expected >100 resources in the dense RG, got {}",
        counts.resources_dense_rg
    );

    // Cross-check the counts actually landed in Postgres.
    let sub_count: i64 = sqlx::query_scalar("SELECT count(*) FROM synthetic.subscriptions")
        .fetch_one(&pool)
        .await
        .expect("count subscriptions");
    assert_eq!(sub_count, 2);

    let rg_count: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM synthetic.resource_groups WHERE subscription_id = $1",
    )
    .bind(common::SUB_A)
    .fetch_one(&pool)
    .await
    .expect("count resource groups");
    assert!(rg_count > 100, "RG count in DB = {rg_count}");

    let res_count: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM synthetic.resources WHERE subscription_id = $1 AND resource_group_name = $2",
    )
    .bind(common::SUB_A)
    .bind(common::DENSE_RG_NAME)
    .fetch_one(&pool)
    .await
    .expect("count resources");
    assert!(res_count > 100, "resource count in DB = {res_count}");

    // The router builds from the seeded pool (proves build_router is usable here).
    let _app = build_router(AppState {
        pool,
        base_url: "http://test".to_string(),
        metrics: Metrics::new(),
        signer: common::test_signer(),
        enforce_auth: false,
        control: None,
    });
}

/// Build a seeded router for endpoint tests.
async fn seeded_app() -> (
    axum::Router,
    testcontainers::ContainerAsync<postgres::Postgres>,
) {
    let (pool, container) = start_pg().await;
    common::seed_fixture(&pool).await;
    let app = build_router(AppState {
        pool,
        base_url: "http://test".to_string(),
        metrics: Metrics::new(),
        signer: common::test_signer(),
        enforce_auth: false,
        control: None,
    });
    (app, container)
}

/// MOCK-01: GET /subscriptions returns the ARM subscription envelope.
#[tokio::test]
async fn lists_subscriptions_arm_shape() {
    let (app, _c) = seeded_app().await;
    let (status, body) = common::request(app, "GET", "/subscriptions", Some("x")).await;

    assert_eq!(status, 200);
    let value = body["value"].as_array().expect("value array");
    assert!(!value.is_empty(), "expected non-empty subscription list");

    let first = &value[0];
    for key in [
        "id",
        "subscriptionId",
        "displayName",
        "state",
        "tenantId",
        "subscriptionPolicies",
    ] {
        assert!(first.get(key).is_some(), "missing key: {key}");
    }

    // id is synthesized as /subscriptions/{subscriptionId} (MOCK-01).
    let sub_id = first["subscriptionId"]
        .as_str()
        .expect("subscriptionId string");
    assert_eq!(
        first["id"].as_str().unwrap(),
        format!("/subscriptions/{sub_id}")
    );

    // subscriptionPolicies is an object carrying spendingLimit (A3); never null (MOCK-13).
    let policies = &first["subscriptionPolicies"];
    assert!(
        policies.is_object(),
        "subscriptionPolicies must be an object, got {policies}"
    );
    assert!(policies.get("spendingLimit").is_some());
}

/// MOCK-09 / SEC-HIGH-3: missing Bearer -> 401; empty Bearer token -> 401;
/// any NON-empty Bearer -> 200 (live localhost Path-A scan / healthcheck contract).
#[tokio::test]
async fn bearer_required() {
    let (app, _c) = seeded_app().await;
    let (status_no_auth, _) = common::request(app.clone(), "GET", "/subscriptions", None).await;
    assert_eq!(status_no_auth, 401);

    // SEC-HIGH-3: an `Authorization: Bearer ` header with an empty token -> 401.
    let (status_empty, _) = common::request(app.clone(), "GET", "/subscriptions", Some("")).await;
    assert_eq!(status_empty, 401, "empty Bearer token must be rejected");

    // SEC-HIGH-3: whitespace-only token is still empty -> 401.
    let (status_ws, _) = common::request(app.clone(), "GET", "/subscriptions", Some("   ")).await;
    assert_eq!(
        status_ws, 401,
        "whitespace-only Bearer token must be rejected"
    );

    let (status_auth, _) = common::request(app, "GET", "/subscriptions", Some("any-token")).await;
    assert_eq!(status_auth, 200);
}

/// MOCK-10: the 401 AND 400 bodies are ARM CloudError `{ error: { code, message } }`.
/// The 404 ResourceNotFound shape is proven DB-free in `not_found_cloud_error_shape`,
/// completing the 401/400/404 CloudError contract Phase 4's detail endpoint depends on.
#[tokio::test]
async fn arm_cloud_error_shapes() {
    let (app, _c) = seeded_app().await;

    // 401 — missing Bearer → MissingAuthenticationToken.
    let (status, body) = common::request(app.clone(), "GET", "/subscriptions", None).await;
    assert_eq!(status, 401);
    assert_eq!(body["error"]["code"], "MissingAuthenticationToken");
    let message = body["error"]["message"].as_str().expect("message string");
    assert!(
        !message.is_empty(),
        "401 CloudError message must be non-empty"
    );

    // 400 — malformed $skiptoken → InvalidRequestContent (same { error: { code, message } } shape).
    let bad = format!(
        "/subscriptions/{}/resources?$skiptoken=not!base64!!",
        common::SUB_A
    );
    let (status, body) = common::request(app, "GET", &bad, Some("x")).await;
    assert_eq!(status, 400);
    assert_eq!(body["error"]["code"], "InvalidRequestContent");
    let message = body["error"]["message"].as_str().expect("message string");
    assert!(
        !message.is_empty(),
        "400 CloudError message must be non-empty"
    );
}

/// MOCK-11: any api-version query param is accepted without validation (still 200) on
/// EVERY list endpoint — subscriptions, RGs, resources, and RG-scoped resources.
#[tokio::test]
async fn api_version_ignored() {
    let (app, _c) = seeded_app().await;
    let garbage = "api-version=2099-13-99-garbage";
    let endpoints = [
        format!("/subscriptions?{garbage}"),
        format!("/subscriptions/{}/resourceGroups?{garbage}", common::SUB_A),
        format!("/subscriptions/{}/resources?{garbage}", common::SUB_A),
        format!(
            "/subscriptions/{}/resourceGroups/{}/resources?{garbage}",
            common::SUB_A,
            common::DENSE_RG_NAME
        ),
    ];
    for ep in endpoints {
        let (status, body) = common::request(app.clone(), "GET", &ep, Some("x")).await;
        assert_eq!(status, 200, "garbage api-version must be accepted on {ep}");
        assert!(
            body["value"]
                .as_array()
                .map(|v| !v.is_empty())
                .unwrap_or(false),
            "{ep} should still return a non-empty list under a garbage api-version"
        );
    }
}

/// MOCK-10: a malformed `$skiptoken` on any list endpoint returns 400 with the ARM
/// CloudError code `InvalidRequestContent` (and a non-empty message).
#[tokio::test]
async fn bad_skiptoken_400() {
    let (app, _c) = seeded_app().await;
    let bad = "$skiptoken=not-valid-base64!!";
    let endpoints = [
        format!("/subscriptions/{}/resourceGroups?{bad}", common::SUB_A),
        format!("/subscriptions/{}/resources?{bad}", common::SUB_A),
        format!(
            "/subscriptions/{}/resourceGroups/{}/resources?{bad}",
            common::SUB_A,
            common::DENSE_RG_NAME
        ),
    ];
    for ep in endpoints {
        let (status, body) = common::request(app.clone(), "GET", &ep, Some("x")).await;
        assert_eq!(status, 400, "malformed $skiptoken must 400 on {ep}");
        assert_eq!(
            body["error"]["code"], "InvalidRequestContent",
            "400 body must carry the ARM code on {ep}"
        );
        assert!(
            body["error"]["message"]
                .as_str()
                .map(|m| !m.is_empty())
                .unwrap_or(false),
            "400 CloudError message must be non-empty on {ep}"
        );
    }
}

/// MOCK-10: the ApiError::NotFound IntoResponse produces a 404 with the ARM CloudError
/// `{ error: { code: "ResourceNotFound", message } }` shape. No Phase-3 route returns
/// 404, so this DB-free unit check locks the contract that Phase 4's detail endpoint
/// relies on.
#[tokio::test]
async fn not_found_cloud_error_shape() {
    use axum::response::IntoResponse;
    use tenantless_server::error::ApiError;

    let resp = ApiError::NotFound {
        what: "/subscriptions/x/resourceGroups/y/providers/Microsoft.Sql/servers/z".to_string(),
    }
    .into_response();

    assert_eq!(resp.status(), 404, "NotFound must map to HTTP 404");

    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .expect("collect 404 body");
    let body: serde_json::Value = serde_json::from_slice(&bytes).expect("parse 404 body");
    assert_eq!(body["error"]["code"], "ResourceNotFound");
    assert!(
        body["error"]["message"]
            .as_str()
            .map(|m| !m.is_empty())
            .unwrap_or(false),
        "404 CloudError message must be non-empty"
    );
}

/// The nextLink path component for sub A's resourceGroups list.
fn rg_path(sub: &uuid::Uuid) -> String {
    format!("/subscriptions/{sub}/resourceGroups")
}

/// Strip the absolute base_url prefix off a nextLink so it can be replayed via the
/// in-process `oneshot` request driver (which takes a path+query, not an absolute URL).
fn to_relative(next_link: &str, base_url: &str) -> String {
    next_link
        .strip_prefix(base_url)
        .expect("nextLink must be absolute (start with base_url)")
        .to_string()
}

/// MOCK-02: GET /subscriptions/{sub}/resourceGroups paginates >100 RGs; a full
/// traversal yields every RG in sub A exactly once; the last page omits nextLink.
#[tokio::test]
async fn paginates_resource_groups() {
    let (app, _c) = seeded_app().await;
    let base = "http://test";
    let path = rg_path(&common::SUB_A);

    // First page: default $top=100 → exactly 100 items + a nextLink.
    let (status, body) = common::request(app.clone(), "GET", &path, Some("x")).await;
    assert_eq!(status, 200);
    let first = body["value"].as_array().expect("value array");
    assert_eq!(first.len(), 100, "default $top should yield 100 RGs");
    assert!(
        body.get("nextLink").is_some(),
        "first page must carry nextLink"
    );

    // Each item: ARM RG shape (type const + properties object w/ provisioningState).
    let item = &first[0];
    assert_eq!(item["type"], "Microsoft.Resources/resourceGroups");
    let props = &item["properties"];
    assert!(
        props.is_object(),
        "RG properties must be an object, got {props}"
    );
    assert!(props.get("provisioningState").is_some());

    // Collect ids across the full traversal.
    let mut seen: Vec<String> = first
        .iter()
        .map(|i| i["id"].as_str().unwrap().to_string())
        .collect();
    let mut next = body["nextLink"].as_str().map(|l| to_relative(l, base));
    let mut pages = 1;
    while let Some(rel) = next {
        let (st, b) = common::request(app.clone(), "GET", &rel, Some("x")).await;
        assert_eq!(st, 200);
        for i in b["value"].as_array().unwrap() {
            seen.push(i["id"].as_str().unwrap().to_string());
        }
        next = b["nextLink"].as_str().map(|l| to_relative(l, base));
        pages += 1;
        assert!(pages < 100, "pagination did not terminate");
    }

    // Exactly once: set size == total, no duplicates.
    let total = seen.len();
    let unique: std::collections::HashSet<&String> = seen.iter().collect();
    assert_eq!(unique.len(), total, "RG traversal had duplicates");
    // 107 = 105 dense RGs + 2 Phase-4 filter RGs (rg-filter-000, Rg-Filter-Mixed)
    // added to SUB_A by the Plan 04-01 fixture extension.
    assert_eq!(total, 107, "all 107 RGs in sub A returned exactly once");

    // Last page (second page here: 107 = 100 + 7) omits nextLink — asserted by the
    // loop terminating naturally above (next became None).
}

/// Locked behavior (research Open Question 2): unknown {sub} → 200 with empty value.
#[tokio::test]
async fn unknown_sub_returns_empty_rg_list() {
    let (app, _c) = seeded_app().await;
    let unknown = uuid::Uuid::from_u128(0x9999_9999_9999_9999_9999_9999_9999_9999);
    let path = rg_path(&unknown);
    let (status, body) = common::request(app, "GET", &path, Some("x")).await;
    assert_eq!(status, 200);
    assert_eq!(body["value"].as_array().expect("value array").len(), 0);
    assert!(
        body.get("nextLink").is_none(),
        "empty list must omit nextLink"
    );
}

/// The resources list path for a subscription.
fn res_path(sub: &uuid::Uuid) -> String {
    format!("/subscriptions/{sub}/resources")
}

/// MOCK-03: $top default 100 + clamp to [1,1000]; the full $skiptoken chain
/// traverses every resource in sub A exactly once (no gaps, no duplicates).
#[tokio::test]
async fn resources_top_and_skiptoken() {
    let (app, _c) = seeded_app().await;
    let base = "http://test";
    let path = res_path(&common::SUB_A);

    // SUB_A now holds 116 resources: 110 in the dense RG + 5 in rg-filter-000
    // (1 nested-type + 4 filter-selectivity) + 1 mixed-case — the Plan 04-01 rows.
    // Default $top → exactly 100 items + nextLink (fixture has 116 resources).
    let (status, body) = common::request(app.clone(), "GET", &path, Some("x")).await;
    assert_eq!(status, 200);
    assert_eq!(
        body["value"].as_array().unwrap().len(),
        100,
        "default $top=100"
    );
    assert!(
        body.get("nextLink").is_some(),
        "first page must carry nextLink"
    );

    // $top=1500 → clamped to 1000 (max). Fixture has 116, so all 116 fit on
    // one page with no nextLink; assert the page is NOT capped at some lower value.
    let (st, b) =
        common::request(app.clone(), "GET", &format!("{path}?$top=1500"), Some("x")).await;
    assert_eq!(st, 200);
    assert_eq!(
        b["value"].as_array().unwrap().len(),
        116,
        "$top=1500 clamps to 1000 ≥ 116 total"
    );
    assert!(
        b.get("nextLink").is_none(),
        "all 116 fit within the clamped 1000"
    );

    // $top=0 → clamped to 1 (min): exactly one item + nextLink.
    let (st, b) = common::request(app.clone(), "GET", &format!("{path}?$top=0"), Some("x")).await;
    assert_eq!(st, 200);
    assert_eq!(
        b["value"].as_array().unwrap().len(),
        1,
        "$top=0 clamps to 1"
    );
    assert!(
        b.get("nextLink").is_some(),
        "more pages remain after 1 item"
    );

    // Full traversal via the default-page nextLink chain → each resource exactly once.
    let mut seen: Vec<String> = body["value"]
        .as_array()
        .unwrap()
        .iter()
        .map(|i| i["id"].as_str().unwrap().to_string())
        .collect();
    let mut next = body["nextLink"].as_str().map(|l| to_relative(l, base));
    let mut pages = 1;
    while let Some(rel) = next {
        let (s, b) = common::request(app.clone(), "GET", &rel, Some("x")).await;
        assert_eq!(s, 200);
        for i in b["value"].as_array().unwrap() {
            seen.push(i["id"].as_str().unwrap().to_string());
        }
        next = b["nextLink"].as_str().map(|l| to_relative(l, base));
        pages += 1;
        assert!(pages < 1000, "pagination did not terminate");
    }
    let total = seen.len();
    let unique: std::collections::HashSet<&String> = seen.iter().collect();
    assert_eq!(unique.len(), total, "resource traversal had duplicates");
    assert_eq!(total, 116, "all 116 resources returned exactly once");
}

/// MOCK-08: nextLink is absolute (starts with base_url) and carries $skiptoken + $top.
#[tokio::test]
async fn nextlink_is_absolute() {
    let (app, _c) = seeded_app().await;
    let path = res_path(&common::SUB_A);
    let (status, body) = common::request(app, "GET", &path, Some("x")).await;
    assert_eq!(status, 200);
    let link = body["nextLink"].as_str().expect("nextLink present");
    assert!(
        link.starts_with("http://test"),
        "nextLink must be absolute: {link}"
    );
    assert!(
        link.contains("$skiptoken="),
        "nextLink must carry $skiptoken: {link}"
    );
    assert!(link.contains("$top="), "nextLink must carry $top: {link}");
}

/// The RG-scoped resources list path for a subscription + resource group.
fn rg_scoped_res_path(sub: &uuid::Uuid, rg: &str) -> String {
    format!("/subscriptions/{sub}/resourceGroups/{rg}/resources")
}

/// MOCK-04: GET /subscriptions/{sub}/resourceGroups/{rg}/resources returns only that
/// RG's resources, keyset-paginated identically to the unscoped endpoint; an unknown
/// {rg} yields 200 + empty value (no 404 — locked); a resource living in a different
/// RG is absent from the scoped response.
#[tokio::test]
async fn rg_scoped_resources() {
    let (app, _c) = seeded_app().await;
    let base = "http://test";
    // All 110 fixture resources live in DENSE_RG_NAME; the other 104 RGs are empty.
    let path = rg_scoped_res_path(&common::SUB_A, common::DENSE_RG_NAME);

    // Default $top=100 over the >100-resource RG → 100 items + nextLink.
    let (status, body) = common::request(app.clone(), "GET", &path, Some("x")).await;
    assert_eq!(status, 200);
    assert_eq!(
        body["value"].as_array().unwrap().len(),
        100,
        "default $top=100 over the dense RG"
    );
    assert!(
        body.get("nextLink").is_some(),
        "first page must carry nextLink"
    );

    // Each scoped item belongs to DENSE_RG_NAME (its id contains the RG path segment).
    let rg_marker = format!("/resourceGroups/{}/", common::DENSE_RG_NAME);
    for item in body["value"].as_array().unwrap() {
        let id = item["id"].as_str().unwrap();
        assert!(
            id.contains(&rg_marker),
            "scoped resource {id} not in RG {}",
            common::DENSE_RG_NAME
        );
        assert!(
            item["properties"].is_object(),
            "properties must be an object"
        );
    }

    // Full traversal of the scoped chain → every resource of that RG exactly once.
    let mut seen: Vec<String> = body["value"]
        .as_array()
        .unwrap()
        .iter()
        .map(|i| i["id"].as_str().unwrap().to_string())
        .collect();
    let mut next = body["nextLink"].as_str().map(|l| to_relative(l, base));
    let mut pages = 1;
    while let Some(rel) = next {
        let (s, b) = common::request(app.clone(), "GET", &rel, Some("x")).await;
        assert_eq!(s, 200);
        for i in b["value"].as_array().unwrap() {
            seen.push(i["id"].as_str().unwrap().to_string());
        }
        next = b["nextLink"].as_str().map(|l| to_relative(l, base));
        pages += 1;
        assert!(pages < 1000, "pagination did not terminate");
    }
    let total = seen.len();
    let unique: std::collections::HashSet<&String> = seen.iter().collect();
    assert_eq!(unique.len(), total, "scoped traversal had duplicates");
    assert_eq!(
        total, 110,
        "all 110 resources of the dense RG returned exactly once"
    );

    // A resource that exists under DENSE_RG_NAME is ABSENT when scoping to a DIFFERENT
    // (empty) RG of sub A: the other RG returns 200 + empty value (no cross-RG leak).
    let other_rg = "rg-dense-001";
    let other_path = rg_scoped_res_path(&common::SUB_A, other_rg);
    let (st, b) = common::request(app.clone(), "GET", &other_path, Some("x")).await;
    assert_eq!(st, 200);
    assert_eq!(
        b["value"].as_array().unwrap().len(),
        0,
        "a different RG of sub A has no resources in the fixture"
    );
    assert!(
        b.get("nextLink").is_none(),
        "empty scoped list omits nextLink"
    );
    // And none of the dense RG's ids appear under the other RG (proven by len==0 above).
    assert!(
        !seen.is_empty() && seen.iter().all(|id| id.contains(&rg_marker)),
        "every seen id belongs to the dense RG, so none could appear under {other_rg}"
    );

    // Unknown {rg} (never created) → 200 + empty value: [] (NOT 404 — locked).
    let unknown_path = rg_scoped_res_path(&common::SUB_A, "rg-does-not-exist");
    let (st, b) = common::request(app, "GET", &unknown_path, Some("x")).await;
    assert_eq!(st, 200);
    assert_eq!(
        b["value"].as_array().unwrap().len(),
        0,
        "unknown {{rg}} → empty list"
    );
    assert!(b.get("nextLink").is_none());
}

/// P2 (ARM contract): the RG-scoped resource list matches `{rg}` case-insensitively — a
/// lowercased request for a mixed-case stored RG returns the SAME resources as the
/// canonical-case request, mirroring the case-insensitive resource-detail id lookup.
/// Before the fix the exact `resource_group_name = $4` compare read an empty list for
/// any casing but the one stored, so the same ARM path resolved in `.../providers/...`
/// (detail) yet 200-empty in `.../resources` (list).
#[tokio::test]
async fn rg_scoped_resources_case_insensitive() {
    let (app, _c) = seeded_app().await;

    // The fixture stores exactly one resource under the mixed-case RG `Rg-Filter-Mixed`.
    let canonical = rg_scoped_res_path(&common::SUB_A, common::FILTER_MIXED_RG_NAME);
    let flipped = rg_scoped_res_path(&common::SUB_A, &common::FILTER_MIXED_RG_NAME.to_lowercase());
    assert_ne!(
        canonical, flipped,
        "the fixture RG must be mixed-case for this test to be meaningful"
    );

    // Canonical-case request → the mixed-case RG's resource(s).
    let (st_c, body_c) = common::request(app.clone(), "GET", &canonical, Some("x")).await;
    assert_eq!(st_c, 200);
    let canon_ids: Vec<String> = body_c["value"]
        .as_array()
        .unwrap()
        .iter()
        .map(|i| i["id"].as_str().unwrap().to_string())
        .collect();
    assert!(
        canon_ids.contains(&common::MIXED_CASE_RESOURCE_ID.to_string()),
        "canonical-case request must return the mixed-case RG's resource"
    );

    // Lowercased `{rg}` → the SAME resources (case-insensitive match).
    let (st_f, body_f) = common::request(app, "GET", &flipped, Some("x")).await;
    assert_eq!(st_f, 200, "a case-mismatched {{rg}} must still resolve");
    let flipped_ids: Vec<String> = body_f["value"]
        .as_array()
        .unwrap()
        .iter()
        .map(|i| i["id"].as_str().unwrap().to_string())
        .collect();
    assert_eq!(
        flipped_ids, canon_ids,
        "a lowercased {{rg}} must return the SAME resources as the canonical case"
    );
    assert!(
        !flipped_ids.is_empty(),
        "the case-insensitive scope must not read empty"
    );
}

/// The resource-detail (catch-all) path: provider-onward tail under {sub}/{rg}.
fn detail_path(sub: &uuid::Uuid, rg: &str, tail: &str) -> String {
    format!("/subscriptions/{sub}/resourceGroups/{rg}/providers/{tail}")
}

/// Split a stored ARM resource id into (sub, rg, tail) so a detail request can be
/// reconstructed (and optionally case-flipped) from the canonical id.
fn split_resource_id(id: &str) -> (String, String, String) {
    // /subscriptions/{sub}/resourceGroups/{rg}/providers/{tail}
    let rest = id
        .strip_prefix("/subscriptions/")
        .expect("id starts with /subscriptions/");
    let (sub, rest) = rest
        .split_once("/resourceGroups/")
        .expect("has resourceGroups");
    let (rg, tail) = rest.split_once("/providers/").expect("has providers");
    (sub.to_string(), rg.to_string(), tail.to_string())
}

/// MOCK-05: a nested-depth id resolves to a single ARM Resource object (no `value`
/// envelope), with the canonical `type` echoed verbatim (MOCK-12).
#[tokio::test]
async fn resource_detail_nested() {
    let (app, _c) = seeded_app().await;
    let (_sub, rg, tail) = split_resource_id(common::NESTED_RESOURCE_ID);
    let path = detail_path(&common::SUB_A, &rg, &tail);
    let (status, body) = common::request(app, "GET", &path, Some("x")).await;

    assert_eq!(status, 200);
    // Single object, NOT a {value: []} envelope.
    assert!(
        body.get("value").is_none(),
        "detail must be a single object, not a list"
    );
    assert_eq!(
        body["id"].as_str().unwrap(),
        common::NESTED_RESOURCE_ID,
        "detail id must equal the stored nested id"
    );
    assert_eq!(
        body["type"].as_str().unwrap(),
        common::NESTED_RESOURCE_TYPE,
        "type must be canonical (echoed verbatim, MOCK-12)"
    );
    assert!(
        body["properties"].is_object(),
        "properties must be an object"
    );
}

/// D-06: an unknown (never-seeded) provider path under a real sub returns 404 with the
/// ARM CloudError `{ error: { code: "ResourceNotFound", message } }` shape.
#[tokio::test]
async fn resource_detail_404() {
    let (app, _c) = seeded_app().await;
    let path = detail_path(
        &common::SUB_A,
        common::FILTER_RG_NAME,
        "Microsoft.Sql/servers/does-not-exist/databases/nope",
    );
    let (status, body) = common::request(app, "GET", &path, Some("x")).await;

    assert_eq!(
        status, 404,
        "an unknown resource id must 404 (not empty value)"
    );
    assert_eq!(body["error"]["code"], "ResourceNotFound");
    assert!(
        body["error"]["message"]
            .as_str()
            .map(|m| !m.is_empty())
            .unwrap_or(false),
        "404 CloudError message must be non-empty"
    );
}

/// MOCK-07 / D-08: a case-flipped `{rg}`/`{name}` in the request still resolves to the
/// canonical stored resource (case-insensitive id match).
#[tokio::test]
async fn detail_case_insensitive() {
    let (app, _c) = seeded_app().await;
    let (_sub, rg, tail) = split_resource_id(common::MIXED_CASE_RESOURCE_ID);
    // Lowercase the request's {rg} and {name} segments; the stored id is mixed-case.
    let flipped = detail_path(&common::SUB_A, &rg.to_lowercase(), &tail.to_lowercase());
    // Sanity: the flipped path differs from the canonical (so the test is meaningful).
    assert_ne!(
        flipped,
        detail_path(&common::SUB_A, &rg, &tail),
        "case-flip must produce a different request path than the canonical id"
    );

    let (status, body) = common::request(app, "GET", &flipped, Some("x")).await;
    assert_eq!(status, 200, "case-mismatched request must still resolve");
    assert_eq!(
        body["id"].as_str().unwrap(),
        common::MIXED_CASE_RESOURCE_ID,
        "returned id must be the canonical stored id, not the case-flipped request"
    );
}

/// MOCK-12 / D-09: the returned `type` is canonical (echoed verbatim from storage), NOT
/// lowercased, even though the lookup is case-insensitive.
#[tokio::test]
async fn canonical_type_casing() {
    let (app, _c) = seeded_app().await;
    let (_sub, rg, tail) = split_resource_id(common::NESTED_RESOURCE_ID);
    let path = detail_path(&common::SUB_A, &rg, &tail);
    let (status, body) = common::request(app, "GET", &path, Some("x")).await;

    assert_eq!(status, 200);
    let ty = body["type"].as_str().expect("type string");
    assert_eq!(ty, common::NESTED_RESOURCE_TYPE, "type must be canonical");
    // Canonical casing is preserved: the stored `Microsoft.` prefix is not lowercased.
    assert!(
        ty.starts_with("Microsoft."),
        "canonical type retains Microsoft. casing: {ty}"
    );
    assert_ne!(ty, ty.to_lowercase(), "type must not be lowercased");
}

/// T-04-07: the detail route is behind the Bearer gate — a missing Bearer header → 401.
#[tokio::test]
async fn detail_requires_bearer() {
    let (app, _c) = seeded_app().await;
    let (_sub, rg, tail) = split_resource_id(common::NESTED_RESOURCE_ID);
    let path = detail_path(&common::SUB_A, &rg, &tail);
    let (status, _body) = common::request(app, "GET", &path, None).await;
    assert_eq!(
        status, 401,
        "detail route must require Bearer (gated like every route)"
    );
}

/// The catch-all `{*tail}` detail route must NOT shadow the static `/resources` and
/// `/resourceGroups/{rg}/resources` list routes — both must still return a `value`
/// array (matchit gives the static `resources` siblings priority over the catch-all).
#[tokio::test]
async fn detail_route_does_not_shadow_list() {
    let (app, _c) = seeded_app().await;

    let sub_path = res_path(&common::SUB_A);
    let (status, body) = common::request(app.clone(), "GET", &sub_path, Some("x")).await;
    assert_eq!(status, 200, "sub-scoped list must still resolve");
    assert!(
        body["value"]
            .as_array()
            .map(|v| !v.is_empty())
            .unwrap_or(false),
        "sub-scoped /resources must still return a non-empty value array"
    );

    let rg_path = rg_scoped_res_path(&common::SUB_A, common::DENSE_RG_NAME);
    let (status, body) = common::request(app, "GET", &rg_path, Some("x")).await;
    assert_eq!(status, 200, "RG-scoped list must still resolve");
    assert!(
        body["value"]
            .as_array()
            .map(|v| !v.is_empty())
            .unwrap_or(false),
        "RG-scoped /resources must still return a non-empty value array"
    );
}

// ===========================================================================
// MOCK-06: OData `$filter` on BOTH resource list endpoints (D-03).
//
// Selectivity counts are derived from the Plan 04-01 fixture under SUB_A
// (116 resources total): 110 dense (storage/eastus/no-tags) + 5 under
// rg-filter-000 [nested(Sql/servers/databases, westus, env=prod),
// flt-0000(storage,eastus,env=prod), flt-0001(storage,westus,env=dev),
// flt-0002(vnet,eastus,env=prod), flt-0003(vnet,westus,env=dev+team)] +
// 1 mixed-case(storage,eastus,env=prod).
// ===========================================================================

/// Percent-encode a `$filter` value so it survives the query string (spaces, quotes).
/// Mirrors the server-side `percent_encode_query` rule (RFC 3986 unreserved pass-through).
fn enc(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for &b in s.as_bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(b as char)
            }
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

/// Build `/subscriptions/{SUB_A}/resources?$filter=<encoded>`.
fn filtered_res_path(sub: &uuid::Uuid, filter: &str) -> String {
    format!("/subscriptions/{sub}/resources?$filter={}", enc(filter))
}

/// MOCK-06: `resourceType eq 'X'` returns only resources of type X (and the count
/// matches the fixture's vnet rows: flt-0002, flt-0003 = 2).
#[tokio::test]
async fn filter_resource_type() {
    let (app, _c) = seeded_app().await;
    let path = filtered_res_path(
        &common::SUB_A,
        &format!("resourceType eq '{}'", common::FILTER_TYPE_VNET),
    );
    let (status, body) = common::request(app, "GET", &path, Some("x")).await;
    assert_eq!(status, 200);
    let items = body["value"].as_array().expect("value array");
    assert_eq!(items.len(), 2, "exactly the 2 vnet resources under SUB_A");
    for item in items {
        assert_eq!(
            item["type"].as_str().unwrap(),
            common::FILTER_TYPE_VNET,
            "every returned item must have the filtered type"
        );
    }
}

/// MOCK-06: `location eq 'Y'` returns only resources in location Y (westus rows:
/// nested, flt-0001, flt-0003 = 3).
#[tokio::test]
async fn filter_location() {
    let (app, _c) = seeded_app().await;
    let path = filtered_res_path(
        &common::SUB_A,
        &format!("location eq '{}'", common::FILTER_LOCATION_WEST),
    );
    let (status, body) = common::request(app, "GET", &path, Some("x")).await;
    assert_eq!(status, 200);
    let items = body["value"].as_array().expect("value array");
    assert_eq!(items.len(), 3, "exactly the 3 westus resources under SUB_A");
    for item in items {
        assert_eq!(
            item["location"].as_str().unwrap(),
            common::FILTER_LOCATION_WEST,
            "every returned item must be in the filtered location"
        );
    }
}

/// MOCK-06 / D-01: `tagName eq 'env' and tagValue eq 'prod'` is a SINGLE paired tag
/// predicate (not two independent matches): env=prod rows are nested, flt-0000,
/// flt-0002, mixed-case = 4.
#[tokio::test]
async fn filter_tag_pair() {
    let (app, _c) = seeded_app().await;
    let path = filtered_res_path(
        &common::SUB_A,
        &format!(
            "tagName eq '{}' and tagValue eq '{}'",
            common::FILTER_TAG_KEY,
            common::FILTER_TAG_VALUE_PROD
        ),
    );
    let (status, body) = common::request(app, "GET", &path, Some("x")).await;
    assert_eq!(status, 200);
    let items = body["value"].as_array().expect("value array");
    assert_eq!(
        items.len(),
        4,
        "exactly the 4 resources tagged env=prod under SUB_A (paired predicate)"
    );
    for item in items {
        assert_eq!(
            item["tags"][common::FILTER_TAG_KEY].as_str(),
            Some(common::FILTER_TAG_VALUE_PROD),
            "every returned item must carry the paired tag env=prod"
        );
    }
}

/// MOCK-06: a lone `tagName eq 'env'` is a tag-key PRESENCE filter (not a 400) — it
/// returns every SUB_A resource carrying the `env` tag key regardless of value. The six
/// Phase-4 fixture rows all carry `env`; the dense-RG resources have empty `{}` tags.
#[tokio::test]
async fn filter_tag_presence() {
    let (app, _c) = seeded_app().await;
    let path = filtered_res_path(
        &common::SUB_A,
        &format!("tagName eq '{}'", common::FILTER_TAG_KEY),
    );
    let (status, body) = common::request(app, "GET", &path, Some("x")).await;
    assert_eq!(status, 200, "lone tagName must be accepted, not 400");
    let items = body["value"].as_array().expect("value array");
    assert_eq!(
        items.len(),
        6,
        "the 6 Phase-4 rows carry an `env` tag; dense-RG resources have empty tags"
    );
    for item in items {
        assert!(
            item["tags"].get(common::FILTER_TAG_KEY).is_some(),
            "every returned item must carry the `env` tag key"
        );
    }
}

/// MOCK-06 / D-02: a conjunction (`location eq 'westus' and resourceType eq '<vnet>'`)
/// selects the single matching row (flt-0003).
#[tokio::test]
async fn filter_and() {
    let (app, _c) = seeded_app().await;
    let path = filtered_res_path(
        &common::SUB_A,
        &format!(
            "location eq '{}' and resourceType eq '{}'",
            common::FILTER_LOCATION_WEST,
            common::FILTER_TYPE_VNET
        ),
    );
    let (status, body) = common::request(app, "GET", &path, Some("x")).await;
    assert_eq!(status, 200);
    let items = body["value"].as_array().expect("value array");
    assert_eq!(items.len(), 1, "only flt-0003 is both westus AND vnet");
    let item = &items[0];
    assert_eq!(
        item["location"].as_str().unwrap(),
        common::FILTER_LOCATION_WEST
    );
    assert_eq!(item["type"].as_str().unwrap(), common::FILTER_TYPE_VNET);
}

/// MOCK-06 / D-02: a disjunction (`location eq 'eastus' or location eq 'westus'`)
/// selects every resource (all 116 are one or the other).
#[tokio::test]
async fn filter_or() {
    let (app, _c) = seeded_app().await;
    let path = format!(
        "{}&$top=1500",
        filtered_res_path(
            &common::SUB_A,
            &format!(
                "location eq '{}' or location eq '{}'",
                common::FILTER_LOCATION_EAST,
                common::FILTER_LOCATION_WEST
            ),
        )
    );
    let (status, body) = common::request(app, "GET", &path, Some("x")).await;
    assert_eq!(status, 200);
    let items = body["value"].as_array().expect("value array");
    assert_eq!(items.len(), 116, "every SUB_A resource is eastus or westus");
    for item in items {
        let loc = item["location"].as_str().unwrap();
        assert!(
            loc == common::FILTER_LOCATION_EAST || loc == common::FILTER_LOCATION_WEST,
            "every returned item matches one disjunct, got {loc}"
        );
    }
}

/// MOCK-06 / D-03: the SAME `resourceType eq` filter applies to the RG-scoped endpoint,
/// filtering WITHIN the RG scope (rg-filter-000 holds flt-0002, flt-0003 = 2 vnets).
#[tokio::test]
async fn filter_applies_to_rg_scoped_endpoint() {
    let (app, _c) = seeded_app().await;
    let path = format!(
        "/subscriptions/{}/resourceGroups/{}/resources?$filter={}",
        common::SUB_A,
        common::FILTER_RG_NAME,
        enc(&format!("resourceType eq '{}'", common::FILTER_TYPE_VNET))
    );
    let (status, body) = common::request(app, "GET", &path, Some("x")).await;
    assert_eq!(status, 200);
    let items = body["value"].as_array().expect("value array");
    assert_eq!(items.len(), 2, "the 2 vnet resources within rg-filter-000");
    let rg_marker = format!("/resourceGroups/{}/", common::FILTER_RG_NAME);
    for item in items {
        assert_eq!(item["type"].as_str().unwrap(), common::FILTER_TYPE_VNET);
        assert!(
            item["id"].as_str().unwrap().contains(&rg_marker),
            "scoped filter must stay within the RG"
        );
    }
}

/// MOCK-06 / D-04: a malformed `$filter` (unknown field OR unknown operator) returns
/// 400 with the ARM CloudError code `InvalidRequestContent`, BEFORE any SQL runs
/// (mirrors `bad_skiptoken_400`).
#[tokio::test]
async fn filter_malformed_400() {
    let (app, _c) = seeded_app().await;
    let bad_filters = [
        "foo eq 'x'",          // unknown field
        "resourceType ne 'x'", // unknown operator
    ];
    for bad in bad_filters {
        let path = filtered_res_path(&common::SUB_A, bad);
        let (status, body) = common::request(app.clone(), "GET", &path, Some("x")).await;
        assert_eq!(status, 400, "malformed $filter `{bad}` must 400");
        assert_eq!(
            body["error"]["code"], "InvalidRequestContent",
            "400 body must carry the ARM code for `{bad}`"
        );
        assert!(
            body["error"]["message"]
                .as_str()
                .map(|m| !m.is_empty())
                .unwrap_or(false),
            "400 CloudError message must be non-empty for `{bad}`"
        );
    }
}

/// MOCK-06 / Research Q2: a filtered list with a small `$top` emits a `nextLink` that
/// echoes `$filter`; following it applies the SAME predicate on page 2 (every row on
/// the followed page still matches).
#[tokio::test]
async fn filtered_nextlink_echoes_filter() {
    let (app, _c) = seeded_app().await;
    let base = "http://test";
    // `location eq 'eastus'` → 113 rows under SUB_A (110 dense + flt-0000 + flt-0002 +
    // mixed-case); with $top=50 page 1 has 50 + a nextLink, so a page 2 exists.
    let filter = format!("location eq '{}'", common::FILTER_LOCATION_EAST);
    let path = format!("{}&$top=50", filtered_res_path(&common::SUB_A, &filter));

    let (status, body) = common::request(app.clone(), "GET", &path, Some("x")).await;
    assert_eq!(status, 200);
    let first = body["value"].as_array().expect("value array");
    assert_eq!(first.len(), 50, "$top=50 fills page 1");
    for item in first {
        assert_eq!(
            item["location"].as_str().unwrap(),
            common::FILTER_LOCATION_EAST,
            "page 1 must already be filtered"
        );
    }

    // The nextLink must carry the $filter so page 2 re-applies the same predicate.
    let link = body["nextLink"]
        .as_str()
        .expect("filtered first page must emit nextLink");
    assert!(
        link.contains("$filter="),
        "nextLink must echo $filter so paging stays self-consistent: {link}"
    );

    // Follow the nextLink (relative) and assert page 2 is still fully filtered.
    let rel = to_relative(link, base);
    let (st, b) = common::request(app, "GET", &rel, Some("x")).await;
    assert_eq!(st, 200);
    let page2 = b["value"].as_array().expect("page 2 value array");
    assert!(
        !page2.is_empty(),
        "page 2 must contain the remaining eastus rows"
    );
    for item in page2 {
        assert_eq!(
            item["location"].as_str().unwrap(),
            common::FILTER_LOCATION_EAST,
            "the followed page must apply the SAME $filter predicate"
        );
    }
}

/// MOCK-13: a resource whose properties column is '{}' serializes `"properties": {}`
/// (an object, never null); NULL sku/kind are omitted.
#[tokio::test]
async fn properties_always_object() {
    let (app, _c) = seeded_app().await;
    // $top=1500 (clamped to 1000) returns all 110 on one page incl. the empty-props one.
    let path = format!("{}?$top=1500", res_path(&common::SUB_A));
    let (status, body) = common::request(app.clone(), "GET", &path, Some("x")).await;
    assert_eq!(status, 200);

    let items = body["value"].as_array().unwrap();
    // Every resource serializes properties as an object.
    for item in items {
        assert!(
            item["properties"].is_object(),
            "properties must be an object, got {} for {}",
            item["properties"],
            item["id"]
        );
        // sku/kind are NULL in the fixture → omitted, not null.
        assert!(item.get("sku").is_none(), "NULL sku must be omitted");
        assert!(item.get("kind").is_none(), "NULL kind must be omitted");
    }
    // The fixture's res-0000 has empty properties → `{}` exactly.
    let empty = items
        .iter()
        .find(|i| i["id"].as_str().unwrap().ends_with("/res-0000"))
        .expect("empty-properties resource present");
    assert_eq!(
        empty["properties"],
        serde_json::json!({}),
        "empty props → {{}}"
    );

    // MOCK-13 reconfirmed on the RG-scoped endpoint: the same empty-props resource
    // serializes `"properties": {}` there too (res-0000 lives in DENSE_RG_NAME).
    let scoped_path = format!(
        "/subscriptions/{}/resourceGroups/{}/resources?$top=1500",
        common::SUB_A,
        common::DENSE_RG_NAME
    );
    let (st, b) = common::request(app, "GET", &scoped_path, Some("x")).await;
    assert_eq!(st, 200);
    let scoped_items = b["value"].as_array().unwrap();
    for item in scoped_items {
        assert!(
            item["properties"].is_object(),
            "scoped properties must be an object, got {} for {}",
            item["properties"],
            item["id"]
        );
    }
    let scoped_empty = scoped_items
        .iter()
        .find(|i| i["id"].as_str().unwrap().ends_with("/res-0000"))
        .expect("empty-properties resource present on scoped endpoint");
    assert_eq!(
        scoped_empty["properties"],
        serde_json::json!({}),
        "empty props → {{}} on scoped endpoint too"
    );
}

// ===========================================================================
// SEC-MED-3: sql/003 referential integrity + read-path index.
//
// The harness applies sql/001 + sql/002 + sql/003, then seeds the fixture; these
// tests assert the migration's index/FKs landed AND that the shared fixture still
// loads cleanly (the row-count assertions in `harness_seeds_known_fixture` and the
// endpoint tests above continue to pass — they all run the same seed_fixture).
// ===========================================================================

/// The case-insensitive resource-detail read path (lower(id) = lower($1)) is now
/// backed by a functional index on lower(id).
#[tokio::test]
async fn lower_id_functional_index_exists() {
    let (pool, _container) = start_pg().await;
    common::seed_fixture(&pool).await;

    let indexdef: Option<String> = sqlx::query_scalar(
        "SELECT indexdef FROM pg_indexes \
         WHERE schemaname = 'synthetic' AND tablename = 'resources' \
           AND indexdef ILIKE '%lower(id)%' \
         LIMIT 1",
    )
    .fetch_optional(&pool)
    .await
    .expect("query pg_indexes");

    assert!(
        indexdef.is_some(),
        "expected a functional index on lower(id) over synthetic.resources"
    );
}

/// The case-insensitive RG-scoped read paths (resources.rs listing +
/// cost.rs: `lower(resource_group_name) = lower($4)`) are backed by a functional index
/// on `(subscription_id, lower(resource_group_name), id)` — the sql/008 twin migration.
/// The plain `idx_res_rg (subscription_id, resource_group_name)` can serve only the
/// subscription prefix for the `lower(...)` predicate, so this functional index is what
/// keeps a scoped listing from scanning every resource in the subscription; the trailing
/// `id` serves the listing's `ORDER BY id` / keyset pagination.
#[tokio::test]
async fn rg_lower_functional_index_exists() {
    let (pool, _container) = start_pg().await;
    common::seed_fixture(&pool).await;

    let indexdef: Option<String> = sqlx::query_scalar(
        "SELECT indexdef FROM pg_indexes \
         WHERE schemaname = 'synthetic' AND tablename = 'resources' \
           AND indexdef ILIKE '%lower(resource_group_name)%' \
         LIMIT 1",
    )
    .fetch_optional(&pool)
    .await
    .expect("query pg_indexes");

    let def = indexdef.expect(
        "expected a functional index on lower(resource_group_name) over synthetic.resources",
    );
    // The index must lead with subscription_id (equality prefix) and carry id (keyset
    // ORDER BY id), so the RG-scoped listing/cost predicates and pagination are covered.
    assert!(
        def.contains("subscription_id"),
        "RG-lower index must lead with subscription_id: {def}"
    );
    assert!(
        def.contains("id)") || def.ends_with("id"),
        "RG-lower index must include id for keyset ORDER BY: {def}"
    );
}

/// The composite keyset index `idx_ra_sub_assignment (subscription_id, assignment_id)`
/// exists on `synthetic.role_assignments` — it backs the paginated roleAssignments read
/// (`WHERE subscription_id = $1 AND assignment_id > $2 ORDER BY assignment_id LIMIT $3`).
/// The single-key `idx_ra_sub` serves only the equality prefix, so this composite index is
/// what turns the seek + `ORDER BY` into an index range scan instead of a per-page sort of
/// the subscription's whole assignment set. It ships in sql/005 (idempotent), so an
/// existing DB gains it on the next `ensure_identity_schema` — hence seeding the identity
/// schema (which applies sql/005) is the precondition here.
#[tokio::test]
async fn ra_sub_assignment_composite_index_exists() {
    let (pool, _container) = start_pg().await;
    common::seed_fixture(&pool).await;
    common::seed_identity_rows(&pool).await; // applies sql/005 → creates the index

    let indexdef: Option<String> = sqlx::query_scalar(
        "SELECT indexdef FROM pg_indexes \
         WHERE schemaname = 'synthetic' AND tablename = 'role_assignments' \
           AND indexname = 'idx_ra_sub_assignment' \
         LIMIT 1",
    )
    .fetch_optional(&pool)
    .await
    .expect("query pg_indexes");

    let def = indexdef
        .expect("expected the composite idx_ra_sub_assignment over synthetic.role_assignments");
    // Must cover both keys, with subscription_id (the equality prefix) LEADING
    // assignment_id (the keyset seek + ORDER BY), so the paginated read is a range scan.
    let sub_pos = def
        .find("subscription_id")
        .expect("index must reference subscription_id");
    let asg_pos = def
        .find("assignment_id")
        .expect("index must reference assignment_id");
    assert!(
        sub_pos < asg_pos,
        "composite index must lead with subscription_id then assignment_id: {def}"
    );
}

/// The safe FK from synthetic.resources(subscription_id) -> synthetic.subscriptions
/// exists after sql/003 (validity holds: every resource is minted under a seeded
/// subscription; the fixture seeds all resources under SUB_A).
#[tokio::test]
async fn resources_subscription_fk_enforced() {
    let (pool, _container) = start_pg().await;
    common::seed_fixture(&pool).await;

    let fk_count: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM information_schema.table_constraints tc \
         WHERE tc.constraint_type = 'FOREIGN KEY' \
           AND tc.table_schema = 'synthetic' \
           AND tc.table_name = 'resources' \
           AND tc.constraint_name = 'fk_resources_subscription'",
    )
    .fetch_one(&pool)
    .await
    .expect("query table_constraints");

    assert_eq!(
        fk_count, 1,
        "expected the resources.subscription_id -> subscriptions FK from sql/003"
    );

    // The safe violations.resource_id -> resources(id) FK is also present (the
    // fixture seeds zero violations, so it trivially holds for the fixture).
    let viol_fk: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM information_schema.table_constraints tc \
         WHERE tc.constraint_type = 'FOREIGN KEY' \
           AND tc.table_schema = 'synthetic' \
           AND tc.table_name = 'violations' \
           AND tc.constraint_name = 'fk_violations_resource'",
    )
    .fetch_one(&pool)
    .await
    .expect("query table_constraints (violations)");
    assert_eq!(
        viol_fk, 1,
        "expected the violations.resource_id -> resources FK"
    );

    // The cross-sub dependency FKs are DEFERRED (documented in sql/003): they must
    // NOT exist, since dependencies intentionally reference resources across subs
    // and validity is enforced by the generator's pre-COPY 0-dangling gate instead.
    let dep_fk: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM information_schema.table_constraints tc \
         WHERE tc.constraint_type = 'FOREIGN KEY' \
           AND tc.table_schema = 'synthetic' \
           AND tc.table_name = 'dependencies'",
    )
    .fetch_one(&pool)
    .await
    .expect("query table_constraints (dependencies)");
    assert_eq!(
        dep_fk, 0,
        "dependency FKs are documented-deferred (cross-sub); none must be added"
    );
}

// ===========================================================================
// COST-02/03/04 + IAM-05: the Cost Management Query route, proven end-to-end
// against SCOPED-seeded cost rows (the shared seed_fixture stays zero-cost-row —
// project memory: fixture coupling). The reconciliation invariant is THE phase
// invariant (the XSUB-06 0-dangling analogue, made a test).
// ===========================================================================
mod cost {
    use super::{AppState, Metrics, PgPool, build_router, common, start_pg};
    use serde_json::{Value, json};

    /// Sub-scope cost query path.
    fn sub_scope_path(sub: &uuid::Uuid) -> String {
        format!("/subscriptions/{sub}/providers/Microsoft.CostManagement/query")
    }

    /// RG-scope cost query path (the `{*tail}` method-merge).
    fn rg_scope_path(sub: &uuid::Uuid, rg: &str) -> String {
        format!("/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.CostManagement/query")
    }

    /// A Cost Management Query body over the deterministic Custom window, with an
    /// optional single `grouping` entry (`None` ⇒ ungrouped total).
    fn cost_body(grouping: Option<(&str, &str)>, cost_type: &str) -> Value {
        let grouping = match grouping {
            Some((kind, name)) => json!([{ "type": kind, "name": name }]),
            None => json!([]),
        };
        json!({
            "type": cost_type,
            "timeframe": "Custom",
            "timePeriod": { "from": common::COST_FROM, "to": common::COST_TO },
            "dataset": {
                "granularity": "None",
                "aggregation": { "totalCost": { "name": "PreTaxCost", "function": "Sum" } },
                "grouping": grouping,
            }
        })
    }

    /// Sum the aggregation (Number) column — always cell position 0 — over every row.
    fn sum_cost(body: &Value) -> f64 {
        body["properties"]["rows"]
            .as_array()
            .expect("properties.rows array")
            .iter()
            .map(|r| r[0].as_f64().expect("row cost cell is a number"))
            .sum()
    }

    /// Build a router + cost-seeded pool. Returns the app, the container guard, a
    /// pool clone (for direct SQL asserts), and the cost ground truth.
    async fn cost_app() -> (
        axum::Router,
        testcontainers::ContainerAsync<testcontainers_modules::postgres::Postgres>,
        PgPool,
        common::CostSeed,
    ) {
        let (pool, container) = start_pg().await;
        common::seed_fixture(&pool).await;
        let seed = common::seed_cost_rows(&pool).await;
        let app = build_router(AppState {
            pool: pool.clone(),
            base_url: "http://test".to_string(),
            metrics: Metrics::new(),
            signer: common::test_signer(),
            enforce_auth: false,
            control: None,
        });
        (app, container, pool, seed)
    }

    /// COST-02 (THE invariant): every grouping slice
    /// (ResourceType / ResourceGroup / ServiceName / Tag:env / ungrouped) sums to the
    /// SAME ungrouped fact-table total. A grouping never drops or double-counts a row,
    /// so all five slices reconcile to one number.
    #[tokio::test]
    async fn reconciliation_all_groupings_equal_total() {
        let (app, _c, _pool, seed) = cost_app().await;
        let path = sub_scope_path(&common::SUB_A);

        // Ungrouped total → a single row carrying the whole sub-scope SUM.
        let (status, ungrouped) = common::request_json(
            app.clone(),
            "POST",
            &path,
            Some("x"),
            &cost_body(None, "ActualCost"),
        )
        .await;
        assert_eq!(status, 200, "ungrouped cost query must 200");
        assert_eq!(
            ungrouped["properties"]["rows"].as_array().unwrap().len(),
            1,
            "an ungrouped query yields exactly one total row"
        );
        let total = sum_cost(&ungrouped);
        assert!(
            (total - seed.sub_total).abs() < 1e-9,
            "ungrouped total {total} must equal the seeded sub-scope SUM {}",
            seed.sub_total
        );

        // Every grouping slice must reconcile to the SAME total.
        let slices = [
            ("Dimension", "ResourceType"),
            ("Dimension", "ResourceGroup"),
            ("Dimension", "ServiceName"),
            ("Tag", "env"),
        ];
        for (kind, name) in slices {
            let (st, body) = common::request_json(
                app.clone(),
                "POST",
                &path,
                Some("x"),
                &cost_body(Some((kind, name)), "ActualCost"),
            )
            .await;
            assert_eq!(st, 200, "grouping {name} must 200");
            // More than one slice row proves the grouping actually partitioned the data.
            assert!(
                body["properties"]["rows"].as_array().unwrap().len() >= 2,
                "grouping {name} should produce multiple slices"
            );
            let slice_sum = sum_cost(&body);
            assert!(
                (slice_sum - total).abs() < 1e-9,
                "grouping {name} sums to {slice_sum}, must reconcile to the ungrouped total {total}"
            );
        }
    }

    /// P2 regression: a calendar-shaped but DB-invalid date in a Custom timePeriod
    /// (year zero — PostgreSQL 16 rejects it) is a client 400 at parse time, NOT a
    /// 500 from a rejected `$1::date` cast. Body is the ARM CloudError shape.
    #[tokio::test]
    async fn year_zero_custom_date_is_400() {
        let (app, _c, _pool, _seed) = cost_app().await;
        let path = sub_scope_path(&common::SUB_A);
        let body = json!({
            "type": "ActualCost",
            "timeframe": "Custom",
            "timePeriod": { "from": "0000-01-01", "to": common::COST_TO },
            "dataset": {
                "granularity": "None",
                "aggregation": { "totalCost": { "name": "PreTaxCost", "function": "Sum" } },
                "grouping": json!([]),
            }
        });
        let (status, resp) = common::request_json(app, "POST", &path, Some("x"), &body).await;
        assert_eq!(
            status, 400,
            "year-zero Custom date must be a client 400, not a 500"
        );
        assert!(
            !resp["error"]["message"].as_str().unwrap_or("").is_empty(),
            "400 body must carry a non-empty ARM CloudError message"
        );
    }

    /// COST-02: repeating the SAME query returns byte-identical totals (the handler only
    /// reads/aggregates a materialized fact table — no per-call randomness). The random
    /// `id`/`name` envelope fields differ, so only `properties.rows` is compared.
    #[tokio::test]
    async fn repeat_query_is_identical() {
        let (app, _c, _pool, _seed) = cost_app().await;
        let path = sub_scope_path(&common::SUB_A);
        let body = cost_body(Some(("Dimension", "ResourceType")), "ActualCost");

        let (s1, b1) = common::request_json(app.clone(), "POST", &path, Some("x"), &body).await;
        let (s2, b2) = common::request_json(app.clone(), "POST", &path, Some("x"), &body).await;
        assert_eq!(s1, 200);
        assert_eq!(s2, 200);
        assert_eq!(
            b1["properties"]["rows"], b2["properties"]["rows"],
            "repeated cost query must return identical rows (no per-call randomness)"
        );
    }

    /// COST-03: the body is the positional `{properties:{columns,rows,nextLink}}`
    /// envelope — NOT the ARM-list `{value,nextLink}`. `columns[].type ∈ {Number,String}`;
    /// the Currency column is "USD"; the first column (aggregation) is the Number.
    #[tokio::test]
    async fn response_envelope_shape() {
        let (app, _c, _pool, _seed) = cost_app().await;
        let path = sub_scope_path(&common::SUB_A);
        let (status, body) = common::request_json(
            app,
            "POST",
            &path,
            Some("x"),
            &cost_body(Some(("Dimension", "ResourceType")), "ActualCost"),
        )
        .await;
        assert_eq!(status, 200);

        // Positional envelope, NOT the ARM list shape.
        assert!(
            body.get("value").is_none(),
            "cost result must NOT be a {{value,nextLink}} list"
        );
        let props = &body["properties"];
        assert!(props.is_object(), "properties must be an object");
        let columns = props["columns"].as_array().expect("columns array");
        assert!(props["rows"].is_array(), "rows must be an array");
        assert_eq!(body["type"], "microsoft.costmanagement/Query");

        // Column types are the closed {Number,String} set; the first is the Number.
        assert_eq!(
            columns[0]["type"], "Number",
            "the aggregation column is Number"
        );
        for c in columns {
            let ty = c["type"].as_str().expect("column type string");
            assert!(
                ty == "Number" || ty == "String",
                "column type must be Number|String, got {ty}"
            );
        }

        // A Currency=String column exists and every row's currency cell is "USD".
        let cur_ix = columns
            .iter()
            .position(|c| c["name"] == "Currency")
            .expect("a Currency column is present");
        assert_eq!(columns[cur_ix]["type"], "String");
        for row in props["rows"].as_array().unwrap() {
            assert_eq!(row[cur_ix], "USD", "Currency cell must be USD (D-11)");
        }
    }

    /// COST-02/03: BOTH the sub-scope and RG-scope POSTs route and reconcile; the
    /// RG-scope total is a STRICT subset of the sub-scope total.
    #[tokio::test]
    async fn both_scopes() {
        let (app, _c, _pool, seed) = cost_app().await;

        // Sub scope → full SUB_A total.
        let (s_sub, sub_body) = common::request_json(
            app.clone(),
            "POST",
            &sub_scope_path(&common::SUB_A),
            Some("x"),
            &cost_body(None, "ActualCost"),
        )
        .await;
        assert_eq!(s_sub, 200, "sub-scope POST must route and 200");
        let sub_total = sum_cost(&sub_body);
        assert!(
            (sub_total - seed.sub_total).abs() < 1e-9,
            "sub-scope total = {sub_total}"
        );

        // RG scope (rg-filter-000) → the strict subset total.
        let (s_rg, rg_body) = common::request_json(
            app,
            "POST",
            &rg_scope_path(&common::SUB_A, common::FILTER_RG_NAME),
            Some("x"),
            &cost_body(None, "ActualCost"),
        )
        .await;
        assert_eq!(
            s_rg, 200,
            "RG-scope POST must route via the {{*tail}} method-merge and 200"
        );
        let rg_total = sum_cost(&rg_body);
        assert!(
            (rg_total - seed.rg_filter_total).abs() < 1e-9,
            "RG-scope total = {rg_total}, expected {}",
            seed.rg_filter_total
        );
        assert!(
            rg_total < sub_total,
            "RG scope ({rg_total}) must be a strict subset of sub scope ({sub_total})"
        );
    }

    /// P2 (ARM contract): an RG-scoped cost query matches `{rg}` case-insensitively — a
    /// lowercased scope over a mixed-case-stored RG sums the SAME cost rows as the
    /// canonical case, the same rule the resource endpoints apply. Before the fix the
    /// exact `r.resource_group_name = $4` compare summed zero rows for any casing but
    /// the stored one, so a differently-cased scope silently read $0.
    #[tokio::test]
    async fn rg_scope_case_insensitive() {
        let (app, _c, _pool, _seed) = cost_app().await;

        // Canonical mixed-case scope → the single cost row under `Rg-Filter-Mixed`.
        let (s_canon, canon) = common::request_json(
            app.clone(),
            "POST",
            &rg_scope_path(&common::SUB_A, common::FILTER_MIXED_RG_NAME),
            Some("x"),
            &cost_body(None, "ActualCost"),
        )
        .await;
        assert_eq!(s_canon, 200);
        let canon_total = sum_cost(&canon);
        assert!(
            canon_total > 0.0,
            "the mixed-case RG must carry a non-zero cost total"
        );

        // Lowercased scope over the SAME (mixed-case-stored) RG → the SAME total.
        let (s_flip, flip) = common::request_json(
            app,
            "POST",
            &rg_scope_path(&common::SUB_A, &common::FILTER_MIXED_RG_NAME.to_lowercase()),
            Some("x"),
            &cost_body(None, "ActualCost"),
        )
        .await;
        assert_eq!(
            s_flip, 200,
            "a case-mismatched {{rg}} scope must still resolve"
        );
        let flip_total = sum_cost(&flip);
        assert!(
            (flip_total - canon_total).abs() < 1e-9,
            "lowercased RG scope total {flip_total} must equal the canonical {canon_total}"
        );
    }

    /// COST-04: `AmortizedCost` and `ActualCost` are both accepted and return EQUAL
    /// numbers (amortization math deferred for v2.0 — accept-and-return).
    #[tokio::test]
    async fn amortized_accepted_equals_actual() {
        let (app, _c, _pool, _seed) = cost_app().await;
        let path = sub_scope_path(&common::SUB_A);

        let (sa, actual) = common::request_json(
            app.clone(),
            "POST",
            &path,
            Some("x"),
            &cost_body(None, "ActualCost"),
        )
        .await;
        let (sm, amortized) = common::request_json(
            app,
            "POST",
            &path,
            Some("x"),
            &cost_body(None, "AmortizedCost"),
        )
        .await;
        assert_eq!(sa, 200);
        assert_eq!(sm, 200, "AmortizedCost must be accepted");
        assert!(
            (sum_cost(&actual) - sum_cost(&amortized)).abs() < 1e-9,
            "AmortizedCost must equal ActualCost in v2.0"
        );
    }

    /// IAM-05 (T-9-03): the cost route inherits the any-Bearer contract — an arbitrary
    /// Bearer → 200; a MISSING Bearer → 401 (DoS-by-401-of-scanner mitigation preserved).
    #[tokio::test]
    async fn arbitrary_bearer_200() {
        let (app, _c, _pool, _seed) = cost_app().await;
        let path = sub_scope_path(&common::SUB_A);
        let body = cost_body(None, "ActualCost");

        // Arbitrary, non-empty Bearer → 200.
        let (status_auth, _) = common::request_json(
            app.clone(),
            "POST",
            &path,
            Some("an-arbitrary-token"),
            &body,
        )
        .await;
        assert_eq!(
            status_auth, 200,
            "any non-empty Bearer must be accepted on the cost route"
        );

        // Missing Bearer → 401 (the route is gated like every ARM route).
        let (status_no_auth, _) = common::request_json(app, "POST", &path, None, &body).await;
        assert_eq!(
            status_no_auth, 401,
            "missing Bearer must 401 on the cost route"
        );
    }

    /// T-9-04 (the XSUB-06 0-dangling analogue, as a test): every seeded
    /// `cost_records.resource_id` resolves to a real `resources.id` — a NOT-EXISTS
    /// anti-join returns 0 dangling references. (The FK fk_cost_resource enforces this
    /// at write time; this asserts it over the live fixture.)
    #[tokio::test]
    async fn no_dangling_resource_refs() {
        let (_app, _c, pool, seed) = cost_app().await;

        // Sanity: the scoped seed actually inserted cost rows (NOT the zero-row default).
        let total_rows: i64 = sqlx::query_scalar("SELECT count(*) FROM synthetic.cost_records")
            .fetch_one(&pool)
            .await
            .expect("count cost rows");
        assert_eq!(
            total_rows, seed.row_count,
            "scoped cost seed must have inserted its rows"
        );

        let dangling: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM synthetic.cost_records c \
             WHERE NOT EXISTS (SELECT 1 FROM synthetic.resources r WHERE r.id = c.resource_id)",
        )
        .fetch_one(&pool)
        .await
        .expect("anti-join cost_records vs resources");
        assert_eq!(
            dangling, 0,
            "every cost_records.resource_id must resolve to a real resource"
        );
    }

    // -----------------------------------------------------------------------
    // Cost Query resource-exhaustion bounds. These seed a DEDICATED
    // subscription (isolated from SUB_A/SUB_B, so the reconciliation/count
    // assertions above stay green — project memory: fixture coupling) with
    // enough distinct resources that a ResourceId grouping crosses the
    // MAX_COST_QUERY_ROWS cap (1000). They share `cost_app()`/`start_pg` and so
    // require Docker: CI-run / local-via-Docker, NOT `#[ignore]`d, NOT
    // skip-clean (start_pg panics without a daemon — that is env, not failure).
    // -----------------------------------------------------------------------

    /// The MAX_COST_QUERY_ROWS cap mirrored from `handlers::cost` (crate-private
    /// there; the integration crate can't import it). Keep in sync if it changes.
    const CAP: i32 = 1000;

    /// Seed a dedicated subscription with `n` distinct resources, each carrying one
    /// cost row in the COST window — so a sub-scope ResourceId grouping yields exactly
    /// `n` groups. Bulk-inserted via `generate_series` (fast enough under the 3s
    /// statement_timeout on a testcontainer).
    async fn seed_over_cap_scope(pool: &PgPool, sub: uuid::Uuid, n: i32) {
        sqlx::query(
            r#"INSERT INTO synthetic.subscriptions
                   (subscription_id, tenant_id, display_name, state, archetype,
                    tags, authorization_source, spending_limit)
               VALUES ($1, $2, 'Cap-Test', 'Enabled', 'prod', '{}'::jsonb, 'RoleBased', 'Off')"#,
        )
        .bind(sub)
        .bind(common::TENANT_ID)
        .execute(pool)
        .await
        .expect("insert cap subscription");

        let rg = "rg-cap-000";
        let rg_id = format!("/subscriptions/{sub}/resourceGroups/{rg}");
        sqlx::query(
            r#"INSERT INTO synthetic.resource_groups
                   (id, subscription_id, name, location, template_type, tags, provisioning_state)
               VALUES ($1, $2, $3, 'eastus', 'network', '{}'::jsonb, 'Succeeded')"#,
        )
        .bind(&rg_id)
        .bind(sub)
        .bind(rg)
        .execute(pool)
        .await
        .expect("insert cap resource group");

        // n distinct resources under the dedicated sub/RG.
        sqlx::query(
            r#"INSERT INTO synthetic.resources
                   (id, subscription_id, resource_group_name, name, type, location,
                    tags, sku, kind, properties, provisioning_state, managed_by)
               SELECT
                   format('/subscriptions/%s/resourceGroups/%s/providers/Microsoft.Storage/storageAccounts/cap-%s',
                          $1::text, $2::text, g),
                   $1, $2, format('cap-%s', g), 'Microsoft.Storage/storageAccounts', 'eastus',
                   '{}'::jsonb, NULL, NULL, '{}'::jsonb, 'Succeeded', NULL
               FROM generate_series(0, $3 - 1) AS g"#,
        )
        .bind(sub)
        .bind(rg)
        .bind(n)
        .execute(pool)
        .await
        .expect("bulk insert cap resources");

        // One cost row per resource, in the COST window (FK references the ids above).
        sqlx::query(
            r#"INSERT INTO synthetic.cost_records
                   (resource_id, subscription_id, billing_period, cost_amount, currency)
               SELECT id, subscription_id, $2::date, 1.0, 'USD'
               FROM synthetic.resources
               WHERE subscription_id = $1"#,
        )
        .bind(sub)
        .bind(common::COST_BILLING_PERIOD)
        .execute(pool)
        .await
        .expect("bulk insert cap cost rows");
    }

    /// (b): a ResourceId grouping whose cardinality is EXACTLY the cap returns a
    /// full 200 result — the cap boundary is inclusive.
    #[tokio::test]
    async fn cost_query_at_cap_returns_200() {
        let (app, _c, pool, _seed) = cost_app().await;
        let sub = uuid::Uuid::from_u128(0x0CA9_0000_0000_0000_0000_0000_0000_0000);
        seed_over_cap_scope(&pool, sub, CAP).await;

        let (status, body) = common::request_json(
            app,
            "POST",
            &sub_scope_path(&sub),
            Some("x"),
            &cost_body(Some(("Dimension", "ResourceId")), "ActualCost"),
        )
        .await;
        assert_eq!(status, 200, "exactly CAP distinct groups must 200");
        assert_eq!(
            body["properties"]["rows"].as_array().unwrap().len(),
            CAP as usize,
            "a CAP-cardinality result returns all CAP rows"
        );
    }

    /// (c)/(d)/(e): a ResourceId grouping over CAP+1 distinct resources is bounded
    /// — a hard ARM-shaped 400 (LIMIT overflow or statement_timeout), NEVER a partial
    /// 200, and the 400 body carries NO nextLink suggesting more data.
    #[tokio::test]
    async fn cost_query_over_cap_resourceid_is_bounded_400() {
        let (app, _c, pool, _seed) = cost_app().await;
        let sub = uuid::Uuid::from_u128(0x0CAB_0000_0000_0000_0000_0000_0000_0000);
        seed_over_cap_scope(&pool, sub, CAP + 1).await;

        let (status, body) = common::request_json(
            app,
            "POST",
            &sub_scope_path(&sub),
            Some("x"),
            &cost_body(Some(("Dimension", "ResourceId")), "ActualCost"),
        )
        .await;
        // (c)/(d): fail-closed 400, never an unbounded/partial 200.
        assert_eq!(
            status, 400,
            "an over-cap ResourceId grouping must fail closed with a 400, not a partial 200"
        );
        // ARM CloudError shape; assert the message identifies the ROW-CAP overflow path
        // specifically (contains "exceeds") — CAP+1 fast rows deterministically hit the row
        // cap, NOT the statement_timeout, so this pins which control fired (the timeout
        // message is "too expensive"). Guards against the two 400 paths silently swapping.
        let msg = body["error"]["message"].as_str().unwrap_or("");
        assert!(
            msg.contains("exceeds"),
            "over-cap 400 must be the row-cap overflow (message contains 'exceeds'), got: {msg:?}"
        );
        // (e): no misleading nextLink anywhere in the 400 body.
        assert!(
            body.get("nextLink").is_none()
                && body["properties"].get("nextLink").is_none()
                && body["properties"].get("next_link").is_none(),
            "a fail-closed 400 must NOT emit a nextLink suggesting more data: {body}"
        );
    }

    /// Regression: a cost query must NOT loosen a tighter operator-configured global
    /// `statement_timeout`. Served over a pool whose connections carry a punishing 1ms
    /// session timeout (DB_STATEMENT_TIMEOUT_MS=1, far tighter than the cost query's OLD fixed
    /// 5000ms local override), a real ResourceId aggregate over many rows is cancelled by that
    /// global (SQLSTATE 57014 → 504). Under the removed override it would have run to a 200 —
    /// so a 504 here proves the tight global now survives entry into the cost transaction.
    #[tokio::test]
    async fn cost_query_respects_tight_global_statement_timeout() {
        use sqlx::postgres::PgPoolOptions;

        let (url, _container) = super::start_pg_url().await;

        // Seed via a normal (unbounded) pool: base schema/tenant + the cost schema + a
        // dedicated sub with enough distinct resources that the aggregate reliably takes far
        // longer than 1ms (mirrors `cost_app`'s seeding before `seed_over_cap_scope`).
        let seed_pool = PgPool::connect(&url).await.expect("seed pool");
        common::seed_fixture(&seed_pool).await;
        common::seed_cost_rows(&seed_pool).await;
        let sub = uuid::Uuid::from_u128(0x0CA9_0000_0000_0000_0000_0000_0000_0001);
        seed_over_cap_scope(&seed_pool, sub, 5000).await;

        // Serve over a pool with a 1ms session statement_timeout on every connection.
        let tight_pool = PgPoolOptions::new()
            .after_connect(|conn, _meta| {
                Box::pin(async move {
                    sqlx::query("SET statement_timeout = '1ms'")
                        .execute(conn)
                        .await?;
                    Ok(())
                })
            })
            .connect(&url)
            .await
            .expect("tight-timeout pool");
        let app = build_router(AppState {
            pool: tight_pool,
            base_url: "http://test".to_string(),
            metrics: Metrics::new(),
            signer: common::test_signer(),
            enforce_auth: false,
            control: None,
        });

        let (status, body) = common::request_json(
            app,
            "POST",
            &sub_scope_path(&sub),
            Some("x"),
            &cost_body(Some(("Dimension", "ResourceId")), "ActualCost"),
        )
        .await;
        assert_eq!(
            status, 504,
            "a tight global statement_timeout must govern the cost query (57014 → 504); the \
             cost transaction must not loosen it — got {status} / {body}"
        );
    }

    /// Seed a dedicated subscription with ONE resource carrying a tag whose VALUE is
    /// `tag_bytes` long, plus one cost row — so a `Tag:{tag_key}` grouping yields a single
    /// row whose cell is that large value. Built in SQL (`repeat`) to avoid shipping the
    /// blob from Rust.
    async fn seed_big_tag_scope(pool: &PgPool, sub: uuid::Uuid, tag_key: &str, tag_bytes: i32) {
        sqlx::query(
            r#"INSERT INTO synthetic.subscriptions
                   (subscription_id, tenant_id, display_name, state, archetype,
                    tags, authorization_source, spending_limit)
               VALUES ($1, $2, 'BigTag-Test', 'Enabled', 'prod', '{}'::jsonb, 'RoleBased', 'Off')"#,
        )
        .bind(sub)
        .bind(common::TENANT_ID)
        .execute(pool)
        .await
        .expect("insert big-tag subscription");

        let rg = "rg-bigtag-000";
        let rg_id = format!("/subscriptions/{sub}/resourceGroups/{rg}");
        sqlx::query(
            r#"INSERT INTO synthetic.resource_groups
                   (id, subscription_id, name, location, template_type, tags, provisioning_state)
               VALUES ($1, $2, $3, 'eastus', 'network', '{}'::jsonb, 'Succeeded')"#,
        )
        .bind(&rg_id)
        .bind(sub)
        .bind(rg)
        .execute(pool)
        .await
        .expect("insert big-tag resource group");

        let res_id = format!(
            "/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Storage/storageAccounts/bigtag-0"
        );
        sqlx::query(
            r#"INSERT INTO synthetic.resources
                   (id, subscription_id, resource_group_name, name, type, location,
                    tags, sku, kind, properties, provisioning_state, managed_by)
               VALUES ($1, $2, $3, 'bigtag-0', 'Microsoft.Storage/storageAccounts', 'eastus',
                       jsonb_build_object($4::text, repeat('x', $5)), NULL, NULL,
                       '{}'::jsonb, 'Succeeded', NULL)"#,
        )
        .bind(&res_id)
        .bind(sub)
        .bind(rg)
        .bind(tag_key)
        .bind(tag_bytes)
        .execute(pool)
        .await
        .expect("insert big-tag resource");

        sqlx::query(
            r#"INSERT INTO synthetic.cost_records
                   (resource_id, subscription_id, billing_period, cost_amount, currency)
               VALUES ($1, $2, $3::date, 1.0, 'USD')"#,
        )
        .bind(&res_id)
        .bind(sub)
        .bind(common::COST_BILLING_PERIOD)
        .execute(pool)
        .await
        .expect("insert big-tag cost row");
    }

    /// (byte axis): a query returning FEW rows can still be huge if a cell (here a
    /// JSONB tag value with no maxLength) is enormous. A single ~96 KiB tag value —
    /// over MAX_COST_CELL_BYTES (64 KiB) — must fail closed with a byte-limit 400, proving
    /// the response is bounded by BYTES, not just row count.
    #[tokio::test]
    async fn cost_query_oversized_cell_is_bounded_400() {
        let (app, _c, pool, _seed) = cost_app().await;
        let sub = uuid::Uuid::from_u128(0x0CAC_0000_0000_0000_0000_0000_0000_0000);
        seed_big_tag_scope(&pool, sub, "huge", 96 * 1024).await;

        let (status, body) = common::request_json(
            app,
            "POST",
            &sub_scope_path(&sub),
            Some("x"),
            &cost_body(Some(("Tag", "huge")), "ActualCost"),
        )
        .await;
        assert_eq!(
            status, 400,
            "a single oversized response cell must fail closed with a 400 (few rows, huge bytes)"
        );
        let msg = body["error"]["message"].as_str().unwrap_or("");
        assert!(
            msg.contains("byte"),
            "the 400 must be the byte-limit path (message mentions 'byte'), got: {msg:?}"
        );
    }

    /// Seed a dedicated subscription with `n` resources, EACH carrying a DISTINCT tag value
    /// of `per_value_bytes` (individually under MAX_COST_CELL_BYTES so no single cell trips
    /// the per-cell cap) — so a `Tag:{tag_key}` grouping yields `n` valid rows whose COMBINED
    /// serialized size crosses the cumulative budget. Distinctness via a per-row suffix.
    async fn seed_many_valid_cells_scope(
        pool: &PgPool,
        sub: uuid::Uuid,
        n: i32,
        tag_key: &str,
        per_value_bytes: i32,
    ) {
        sqlx::query(
            r#"INSERT INTO synthetic.subscriptions
                   (subscription_id, tenant_id, display_name, state, archetype,
                    tags, authorization_source, spending_limit)
               VALUES ($1, $2, 'ManyCells-Test', 'Enabled', 'prod', '{}'::jsonb, 'RoleBased', 'Off')"#,
        )
        .bind(sub)
        .bind(common::TENANT_ID)
        .execute(pool)
        .await
        .expect("insert many-cells subscription");

        let rg = "rg-manycells-000";
        let rg_id = format!("/subscriptions/{sub}/resourceGroups/{rg}");
        sqlx::query(
            r#"INSERT INTO synthetic.resource_groups
                   (id, subscription_id, name, location, template_type, tags, provisioning_state)
               VALUES ($1, $2, $3, 'eastus', 'network', '{}'::jsonb, 'Succeeded')"#,
        )
        .bind(&rg_id)
        .bind(sub)
        .bind(rg)
        .execute(pool)
        .await
        .expect("insert many-cells resource group");

        // n resources, each tags->{tag_key} = repeat('a', per_value_bytes) || <distinct suffix>.
        sqlx::query(
            r#"INSERT INTO synthetic.resources
                   (id, subscription_id, resource_group_name, name, type, location,
                    tags, sku, kind, properties, provisioning_state, managed_by)
               SELECT
                   format('/subscriptions/%s/resourceGroups/%s/providers/Microsoft.Storage/storageAccounts/mc-%s',
                          $1::text, $2::text, g),
                   $1, $2, format('mc-%s', g), 'Microsoft.Storage/storageAccounts', 'eastus',
                   jsonb_build_object($4::text, repeat('a', $5) || lpad(g::text, 8, '0')),
                   NULL, NULL, '{}'::jsonb, 'Succeeded', NULL
               FROM generate_series(0, $3 - 1) AS g"#,
        )
        .bind(sub)
        .bind(rg)
        .bind(n)
        .bind(tag_key)
        .bind(per_value_bytes)
        .execute(pool)
        .await
        .expect("bulk insert many-cells resources");

        sqlx::query(
            r#"INSERT INTO synthetic.cost_records
                   (resource_id, subscription_id, billing_period, cost_amount, currency)
               SELECT id, subscription_id, $2::date, 1.0, 'USD'
               FROM synthetic.resources
               WHERE subscription_id = $1"#,
        )
        .bind(sub)
        .bind(common::COST_BILLING_PERIOD)
        .execute(pool)
        .await
        .expect("insert many-cells cost rows");
    }

    /// (byte axis, CUMULATIVE): 150 rows × ~60 KiB distinct tag values — each cell is
    /// individually valid (< MAX_COST_CELL_BYTES) and the row count is under the row cap, yet
    /// the COMBINED serialized size crosses the 8 MiB budget. Must fail closed with the
    /// CUMULATIVE 400 ("response exceeds"), proving the cumulative wiring fires (were it
    /// removed, all 150 valid cells would return a ~9 MiB 200). Verifies the budget end-to-end.
    #[tokio::test]
    async fn cost_query_cumulative_budget_is_bounded_400() {
        let (app, _c, pool, _seed) = cost_app().await;
        let sub = uuid::Uuid::from_u128(0x0CAD_0000_0000_0000_0000_0000_0000_0000);
        // 150 × 60 KiB ≈ 9 MiB raw > 8 MiB; each 60 KiB < 64 KiB per-cell cap; 150 < row cap.
        seed_many_valid_cells_scope(&pool, sub, 150, "big", 60 * 1024).await;

        let (status, body) = common::request_json(
            app,
            "POST",
            &sub_scope_path(&sub),
            Some("x"),
            &cost_body(Some(("Tag", "big")), "ActualCost"),
        )
        .await;
        assert_eq!(
            status, 400,
            "many individually-valid cells exceeding the cumulative budget must fail closed"
        );
        let msg = body["error"]["message"].as_str().unwrap_or("");
        assert!(
            msg.contains("response exceeds"),
            "must be the CUMULATIVE byte path (\"response exceeds\"), not per-cell, got: {msg:?}"
        );
    }
}

/// Microsoft.Authorization data plane (Plan 10-03, IAM-02/IAM-03/IAM-05). Mirrors `mod
/// cost`: a scoped identity-seeded app builder, the verified 2022-04-01 shape assertions,
/// the three-way 0-dangling anti-join, cross-language catalogue agreement (Pitfall 3),
/// and the any-Bearer-OFF regression.
mod identity {
    use super::{AppState, Metrics, PgPool, build_router, common, start_pg};

    /// roleDefinitions list path (sub scope).
    fn role_definitions_path(sub: &uuid::Uuid) -> String {
        format!("/subscriptions/{sub}/providers/Microsoft.Authorization/roleDefinitions")
    }

    /// roleAssignments list path (sub scope).
    fn role_assignments_path(sub: &uuid::Uuid) -> String {
        format!("/subscriptions/{sub}/providers/Microsoft.Authorization/roleAssignments")
    }

    /// Percent-encode the chars our `$filter` values use (space, quote, parens) so the raw
    /// value survives `Request::builder().uri(...)` parsing.
    fn q(filter: &str) -> String {
        filter
            .chars()
            .map(|c| match c {
                ' ' => "%20".to_string(),
                '\'' => "%27".to_string(),
                '(' => "%28".to_string(),
                ')' => "%29".to_string(),
                other => other.to_string(),
            })
            .collect()
    }

    /// Build a router + identity-seeded pool. Returns the app, the container guard, a
    /// pool clone (for direct SQL anti-joins), and the identity ground truth.
    async fn identity_app() -> (
        axum::Router,
        testcontainers::ContainerAsync<testcontainers_modules::postgres::Postgres>,
        PgPool,
        common::IdentitySeed,
    ) {
        let (pool, container) = start_pg().await;
        common::seed_fixture(&pool).await;
        let seed = common::seed_identity_rows(&pool).await;
        let app = build_router(AppState {
            pool: pool.clone(),
            base_url: "http://test".to_string(),
            metrics: Metrics::new(),
            signer: common::test_signer(),
            enforce_auth: false,
            control: None,
        });
        (app, container, pool, seed)
    }

    /// IAM-03: GET roleDefinitions → 200, `{value:[...]}` with the eight built-in roles;
    /// Owner/Contributor/Reader GUIDs are present and a known item (Contributor) carries
    /// the verified 2022-04-01 properties shape (camelCase, BuiltInRole, ["/"]).
    #[tokio::test]
    async fn role_definitions_shape() {
        let (app, _c, _pool, _seed) = identity_app().await;
        let (status, body) = common::request(
            app,
            "GET",
            &role_definitions_path(&common::SUB_A),
            Some("x"),
        )
        .await;
        assert_eq!(status, 200);

        let value = body["value"].as_array().expect("value array");
        assert_eq!(value.len(), 8, "the eight built-in roles must be served");

        let names: std::collections::HashSet<&str> =
            value.iter().map(|d| d["name"].as_str().unwrap()).collect();
        for guid in [
            common::ROLE_OWNER_GUID,
            common::ROLE_CONTRIBUTOR_GUID,
            common::ROLE_READER_GUID,
        ] {
            assert!(
                names.contains(guid),
                "catalogue missing built-in GUID {guid}"
            );
        }

        // A known item (Contributor) has the verified properties shape.
        let contributor = value
            .iter()
            .find(|d| d["name"] == common::ROLE_CONTRIBUTOR_GUID)
            .expect("Contributor in the served catalogue");
        assert_eq!(
            contributor["type"],
            "Microsoft.Authorization/roleDefinitions"
        );
        assert_eq!(
            contributor["id"],
            format!(
                "/subscriptions/{}/providers/Microsoft.Authorization/roleDefinitions/{}",
                common::SUB_A,
                common::ROLE_CONTRIBUTOR_GUID
            ),
            "the GET-at-scope id carries the /subscriptions prefix"
        );
        let p = &contributor["properties"];
        assert_eq!(p["roleName"], "Contributor");
        assert_eq!(p["type"], "BuiltInRole");
        assert_eq!(p["assignableScopes"], serde_json::json!(["/"]));
        let perm = &p["permissions"][0];
        for key in ["actions", "notActions", "dataActions", "notDataActions"] {
            assert!(
                perm[key].is_array(),
                "permissions[0].{key} must be an array"
            );
        }
    }

    /// IAM-03: GET roleAssignments → 200,
    /// `{value:[{name,type,id,properties{principalId,principalType,roleDefinitionId,scope}}]}`
    /// with the TENANT-scoped roleDefinitionId (no /subscriptions prefix).
    #[tokio::test]
    async fn role_assignments_shape() {
        let (app, _c, _pool, seed) = identity_app().await;
        let (status, body) = common::request(
            app,
            "GET",
            &role_assignments_path(&common::SUB_A),
            Some("x"),
        )
        .await;
        assert_eq!(status, 200);

        let value = body["value"].as_array().expect("value array");
        assert_eq!(
            value.len() as i64,
            seed.assignment_count,
            "every seeded assignment under SUB_A is returned"
        );

        let item = &value[0];
        assert_eq!(item["type"], "Microsoft.Authorization/roleAssignments");
        assert!(item["name"].is_string(), "name is the assignment GUID");
        assert!(
            item["id"]
                .as_str()
                .unwrap()
                .contains("/providers/Microsoft.Authorization/roleAssignments/"),
            "id embeds the roleAssignments path"
        );

        let p = &item["properties"];
        for key in ["principalId", "principalType", "roleDefinitionId", "scope"] {
            assert!(p.get(key).is_some(), "properties missing {key}");
        }
        let rdid = p["roleDefinitionId"].as_str().unwrap();
        assert!(
            rdid.starts_with("/providers/Microsoft.Authorization/roleDefinitions/"),
            "roleDefinitionId must be tenant-scoped: {rdid}"
        );
        assert!(
            !rdid.contains("/subscriptions/"),
            "roleDefinitionId must NOT carry a /subscriptions prefix: {rdid}"
        );
        let pt = p["principalType"].as_str().unwrap();
        assert!(
            ["User", "Group", "ServicePrincipal"].contains(&pt),
            "principalType must be in the verified enum, got {pt}"
        );
    }

    /// IAM-03 (2022-04-01 id contract): the roleAssignment `id` is rooted at the
    /// assignment's ACTUAL scope — `{scope}/providers/Microsoft.Authorization/
    /// roleAssignments/{name}` — NOT unconditionally at the subscription. The fixture
    /// seeds RG- and resource-scoped assignments (not just subscription-scope), so a
    /// subscription-rooted id would contradict `properties.scope` and fail here. This
    /// distinguishes scope-rooted from the old `/subscriptions/{sub}/...` construction.
    #[tokio::test]
    async fn role_assignment_id_is_scope_rooted() {
        let (app, _c, _pool, seed) = identity_app().await;
        let (status, body) = common::request(
            app,
            "GET",
            &role_assignments_path(&common::SUB_A),
            Some("x"),
        )
        .await;
        assert_eq!(status, 200);

        let value = body["value"].as_array().expect("value array");
        assert_eq!(value.len() as i64, seed.assignment_count);

        // At least one seeded assignment must live below the bare subscription (RG or
        // resource scope), otherwise the assertion can't tell scope-rooted from
        // subscription-rooted.
        let sub_root = format!("/subscriptions/{}", common::SUB_A);
        let mut saw_non_sub_scope = false;

        for item in value {
            let name = item["name"].as_str().expect("name");
            let scope = item["properties"]["scope"].as_str().expect("scope");
            if scope != sub_root {
                saw_non_sub_scope = true;
            }
            assert_eq!(
                item["id"].as_str().unwrap(),
                format!("{scope}/providers/Microsoft.Authorization/roleAssignments/{name}"),
                "roleAssignment id must be rooted at its actual scope, not the subscription"
            );
        }

        assert!(
            saw_non_sub_scope,
            "fixture must seed at least one RG/resource-scoped assignment so the \
             scope-rooted id assertion is meaningful"
        );
    }

    /// roleAssignments pagination: a small `$top` returns exactly that many items plus an
    /// absolute `nextLink`; following the continuation collects EVERY seeded assignment
    /// exactly once, and the final page carries no `nextLink`.
    #[tokio::test]
    async fn role_assignments_paginated() {
        let (app, _c, _pool, seed) = identity_app().await;
        let base = role_assignments_path(&common::SUB_A);

        let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();
        let mut uri = format!("{base}?$top=2");
        let mut pages = 0;
        loop {
            let (status, body) = common::request(app.clone(), "GET", &uri, Some("x")).await;
            assert_eq!(status, 200);
            let value = body["value"].as_array().expect("value array");
            assert!(value.len() <= 2, "a page must not exceed $top");
            for item in value {
                assert!(
                    seen.insert(item["name"].as_str().unwrap().to_string()),
                    "no assignment may repeat across pages"
                );
            }
            pages += 1;
            assert!(pages < 10, "pagination must terminate");
            match body["nextLink"].as_str() {
                // nextLink is absolute against the configured base_url; drive its path+query.
                Some(link) => {
                    uri = link
                        .strip_prefix("http://test")
                        .expect("nextLink is absolute against base_url")
                        .to_string();
                }
                None => break,
            }
        }
        assert_eq!(
            seen.len() as i64,
            seed.assignment_count,
            "the paginated traversal must cover every seeded assignment exactly once"
        );
        assert!(
            pages >= 3,
            "{} rows at $top=2 must require >= 3 pages, got {pages}",
            seed.assignment_count
        );
    }

    /// #4 (roleAssignments `$filter=principalId eq`): only the target principal's
    /// assignments are returned — the filter is HONORED, not silently ignored.
    #[tokio::test]
    async fn role_assignments_filter_principal_id() {
        let (app, _c, _pool, _seed) = identity_app().await;
        let uri = format!(
            "{}?$filter={}",
            role_assignments_path(&common::SUB_A),
            q(&format!("principalId eq '{}'", common::PRINCIPAL_USER))
        );
        let (status, body) = common::request(app, "GET", &uri, Some("x")).await;
        assert_eq!(status, 200);

        let value = body["value"].as_array().expect("value array");
        assert_eq!(
            value.len(),
            2,
            "PRINCIPAL_USER holds exactly two seeded assignments"
        );
        for item in value {
            assert_eq!(
                item["properties"]["principalId"].as_str().unwrap(),
                common::PRINCIPAL_USER.to_string(),
                "every returned assignment must belong to the filtered principal"
            );
        }
    }

    /// roleAssignments `$filter=atScope()`: Azure defines `atScope()` as "assignments at or
    /// above the given scope". TenantLess models no management-group/tenant-root
    /// assignments, so at a subscription scope that reduces to exactly `/subscriptions/{sub}`
    /// — the two Owner-at-subscription grants — and the RG/resource-scoped assignments BELOW
    /// it are excluded. This asserts that modeled reduction, not the general Azure contract.
    #[tokio::test]
    async fn role_assignments_filter_at_scope() {
        let (app, _c, _pool, _seed) = identity_app().await;
        let uri = format!(
            "{}?$filter={}",
            role_assignments_path(&common::SUB_A),
            q("atScope()")
        );
        let (status, body) = common::request(app, "GET", &uri, Some("x")).await;
        assert_eq!(status, 200);

        let value = body["value"].as_array().expect("value array");
        let sub_scope = format!("/subscriptions/{}", common::SUB_A);
        assert_eq!(
            value.len(),
            2,
            "two seeded assignments live at the subscription scope (no mgmt-group/root \
             assignments are modeled, so 'at or above' reduces to exactly this scope)"
        );
        for item in value {
            assert_eq!(
                item["properties"]["scope"].as_str().unwrap(),
                sub_scope,
                "with no scopes above the subscription modeled, atScope() returns only \
                 assignments at /subscriptions/{{sub}} (never the RG/resource ones below)"
            );
        }
    }

    /// #4 (composed `atScope() and principalId eq`): both predicates apply — only the SP's
    /// subscription-scope Owner grant matches.
    #[tokio::test]
    async fn role_assignments_filter_combined() {
        let (app, _c, _pool, _seed) = identity_app().await;
        let uri = format!(
            "{}?$filter={}",
            role_assignments_path(&common::SUB_A),
            q(&format!(
                "atScope() and principalId eq '{}'",
                common::PRINCIPAL_SP
            ))
        );
        let (status, body) = common::request(app, "GET", &uri, Some("x")).await;
        assert_eq!(status, 200);

        let value = body["value"].as_array().expect("value array");
        assert_eq!(
            value.len(),
            1,
            "only the SP's subscription-scope Owner grant matches both predicates"
        );
        let p = &value[0]["properties"];
        assert_eq!(
            p["principalId"].as_str().unwrap(),
            common::PRINCIPAL_SP.to_string()
        );
        assert_eq!(
            p["scope"].as_str().unwrap(),
            format!("/subscriptions/{}", common::SUB_A)
        );
    }

    /// #4 (no silent-ignore): an unsupported or malformed `$filter` is an EXPLICIT 400
    /// with the fixed non-leaking message — never a silent-ignore 200.
    #[tokio::test]
    async fn role_assignments_unsupported_filter_is_400() {
        let (app, _c, _pool, _seed) = identity_app().await;
        for filter in [
            "assignedTo('0a0a0a0a-0a0a-0a0a-0a0a-0a0a0a0a0a0a')",
            "roleDefinitionId eq '8e3af657-bb00-4899-acbc-f0f7f5db61aa'",
            "principalId eq 'not-a-guid'",
        ] {
            let uri = format!(
                "{}?$filter={}",
                role_assignments_path(&common::SUB_A),
                q(filter)
            );
            let (status, body) = common::request(app.clone(), "GET", &uri, Some("x")).await;
            assert_eq!(
                status, 400,
                "unsupported/malformed $filter {filter:?} must be an explicit 400, not a silent-ignore 200"
            );
            assert_eq!(
                body["error"]["message"].as_str().unwrap_or_default(),
                "invalid $filter",
                "the 400 must carry the fixed non-leaking message for {filter:?}: {body}"
            );
        }
    }

    /// IAM-02/D-07 (the three-way 0-dangling chain): every seeded role_assignment
    /// resolves to (1) a real principal oid, (2) a real scope in the UNION of
    /// subscription/RG/resource ids, and (3) a built-in roleDefinition GUID in the SERVED
    /// catalogue. Each NOT-EXISTS / anti-join returns 0 dangling rows.
    #[tokio::test]
    async fn no_dangling_role_refs() {
        let (app, _c, pool, seed) = identity_app().await;

        // Sanity: the scoped seed actually inserted its rows (NOT the zero-row default).
        let total_rows: i64 = sqlx::query_scalar("SELECT count(*) FROM synthetic.role_assignments")
            .fetch_one(&pool)
            .await
            .expect("count role assignments");
        assert_eq!(
            total_rows, seed.assignment_count,
            "scoped identity seed must have inserted its rows"
        );

        // (1) principal anti-join — every principal_oid resolves to a real principal.
        let dangling_principal: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM synthetic.role_assignments ra \
             WHERE NOT EXISTS (SELECT 1 FROM synthetic.principals p WHERE p.oid = ra.principal_oid)",
        )
        .fetch_one(&pool)
        .await
        .expect("anti-join role_assignments vs principals");
        assert_eq!(
            dangling_principal, 0,
            "every principal_oid must resolve to a real principal"
        );

        // (2) scope anti-join — scope ∈ UNION(subscription ids, RG ids, resource ids).
        // `scope` is free-form text spanning three id namespaces (no single FK), so the
        // 0-dangling check is an anti-join against the UNION of the three id sets.
        let dangling_scope: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM synthetic.role_assignments ra \
             WHERE ra.scope NOT IN ( \
                 SELECT '/subscriptions/' || subscription_id::text FROM synthetic.subscriptions \
                 UNION SELECT id FROM synthetic.resource_groups \
                 UNION SELECT id FROM synthetic.resources)",
        )
        .fetch_one(&pool)
        .await
        .expect("anti-join role_assignments.scope vs the id UNION");
        assert_eq!(
            dangling_scope, 0,
            "every scope must resolve to a real subscription/RG/resource id"
        );

        // (3) catalogue anti-join — every role_definition_id is a built-in GUID the
        // SERVED catalogue ships (fetched over HTTP, so this pins the actual served set).
        let (st, defs) = common::request(
            app,
            "GET",
            &role_definitions_path(&common::SUB_A),
            Some("x"),
        )
        .await;
        assert_eq!(st, 200);
        let served_ids: Vec<String> = defs["value"]
            .as_array()
            .unwrap()
            .iter()
            .map(|d| {
                format!(
                    "/providers/Microsoft.Authorization/roleDefinitions/{}",
                    d["name"].as_str().unwrap()
                )
            })
            .collect();
        let dangling_role: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM synthetic.role_assignments ra WHERE ra.role_definition_id <> ALL($1)",
        )
        .bind(&served_ids)
        .fetch_one(&pool)
        .await
        .expect("anti-join role_assignments.role_definition_id vs the served catalogue");
        assert_eq!(
            dangling_role, 0,
            "every role_definition_id must be a built-in GUID in the served catalogue"
        );
    }

    /// Pitfall 3 (cross-language GUID agreement): every DISTINCT
    /// role_assignments.role_definition_id GUID in the seeded fixture is present in the
    /// SERVED roleDefinitions catalogue — the generator (identity.py) and the server
    /// (authorization.rs) cannot drift without this test going red.
    #[tokio::test]
    async fn role_def_catalogue_agrees() {
        let (app, _c, pool, _seed) = identity_app().await;

        let (st, defs) = common::request(
            app,
            "GET",
            &role_definitions_path(&common::SUB_A),
            Some("x"),
        )
        .await;
        assert_eq!(st, 200);
        let served: std::collections::HashSet<String> = defs["value"]
            .as_array()
            .unwrap()
            .iter()
            .map(|d| d["name"].as_str().unwrap().to_string())
            .collect();

        let seeded: Vec<String> = sqlx::query_scalar(
            "SELECT DISTINCT role_definition_id FROM synthetic.role_assignments",
        )
        .fetch_all(&pool)
        .await
        .expect("distinct seeded role_definition_ids");
        assert!(
            !seeded.is_empty(),
            "the identity seed must reference role definitions"
        );

        for rdid in &seeded {
            let guid = rdid.rsplit('/').next().unwrap();
            assert!(
                served.contains(guid),
                "seeded role_definition_id {rdid} (guid {guid}) is not in the served catalogue"
            );
        }
    }

    /// IAM-05 (OFF default, the any-Bearer scanner contract): an arbitrary non-JWT
    /// Bearer → 200 on the authorization route; a MISSING Bearer → 401. Mirrors the cost
    /// `arbitrary_bearer_200` regression on the new route.
    #[tokio::test]
    async fn arbitrary_bearer_200_identity() {
        let (app, _c, _pool, _seed) = identity_app().await;
        let path = role_assignments_path(&common::SUB_A);

        // Arbitrary, non-empty Bearer → 200 (enforce OFF — presence-only).
        let (status_auth, _) =
            common::request(app.clone(), "GET", &path, Some("an-arbitrary-token")).await;
        assert_eq!(
            status_auth, 200,
            "any non-empty Bearer must be accepted on the authorization route"
        );

        // Missing Bearer → 401 (the route is gated like every ARM route).
        let (status_no_auth, _) = common::request(app, "GET", &path, None).await;
        assert_eq!(
            status_no_auth, 401,
            "missing Bearer must 401 on the authorization route"
        );
    }

    // -----------------------------------------------------------------------
    // Plan 10-04 — the AAD token mint + JWKS (IAM-04) and the `--enforce-auth`
    // RS256 validation round-trip (IAM-05). The token + JWKS routes merge OUTSIDE
    // the bearer layer (console template) so they bootstrap a token even when
    // enforce is ON; the enforce branch decodes RS256 against the run's own JWKS.
    // -----------------------------------------------------------------------

    /// `POST /{tenant}/oauth2/v2.0/token` path.
    fn token_path(tenant: &str) -> String {
        format!("/{tenant}/oauth2/v2.0/token")
    }

    /// `GET /{tenant}/discovery/v2.0/keys` (JWKS) path.
    fn jwks_path(tenant: &str) -> String {
        format!("/{tenant}/discovery/v2.0/keys")
    }

    /// The served v1.0 ARM issuer/audience for the fixture tenant — the contract the
    /// minted token carries and the enforce branch validates against (RESEARCH Q2).
    fn served_issuer() -> String {
        format!("https://sts.windows.net/{}/", common::TENANT_ID)
    }
    fn served_audience() -> &'static str {
        "https://management.azure.com/"
    }

    /// GIVEN the token mint + JWKS routes, WHEN a client POSTs `/token` and fetches
    /// the JWKS, THEN the minted RS256 access_token decodes cleanly AGAINST the served
    /// JWK (n/e) under `Validation{RS256, iss, aud}` and carries tid/oid/appid/aud/iss/
    /// roles/exp; tid equals the served tenant_id (IAM-04, D-09).
    #[tokio::test]
    async fn token_decodable_via_jwks() {
        use jsonwebtoken::{Algorithm, DecodingKey, Validation, decode, jwk::Jwk};

        let (app, _c, _pool, _seed) = identity_app().await;

        // /token is exempt — POST WITHOUT any Authorization header.
        let (st, body) = common::request(app.clone(), "POST", &token_path("anytenant"), None).await;
        assert_eq!(st, 200, "token mint must return 200");
        assert_eq!(body["token_type"], "Bearer");
        assert_eq!(body["expires_in"], 3600);
        let access = body["access_token"].as_str().expect("access_token string");

        // Fetch the JWKS (also exempt) and build a DecodingKey from the served JWK.
        let (st_keys, keys) = common::request(app, "GET", &jwks_path("anytenant"), None).await;
        assert_eq!(st_keys, 200, "JWKS must return 200");
        let jwk: Jwk = serde_json::from_value(keys["keys"][0].clone()).expect("parse served JWK");
        let decoding = DecodingKey::from_jwk(&jwk).expect("decoding key from served JWK");

        let mut validation = Validation::new(Algorithm::RS256);
        validation.set_issuer(&[served_issuer()]);
        validation.set_audience(&[served_audience()]);
        validation.validate_exp = true;

        let data = decode::<serde_json::Value>(access, &decoding, &validation)
            .expect("minted token must decode against the served JWKS");
        let claims = data.claims;
        // The v1.0 ARM claim set is present.
        for key in ["tid", "oid", "appid", "aud", "iss", "roles", "exp"] {
            assert!(
                claims.get(key).is_some(),
                "minted token is missing claim {key}"
            );
        }
        assert_eq!(
            claims["tid"],
            common::TENANT_ID.to_string(),
            "tid must equal the served tenant_id"
        );
        assert_eq!(claims["iss"], served_issuer());
        assert_eq!(claims["aud"], served_audience());
        assert!(claims["roles"].is_array(), "roles must be an array");
    }

    /// GIVEN a router built with `enforce_auth: true`, WHEN the token + JWKS routes are
    /// hit WITHOUT any Authorization header, THEN they STILL return 200 — they merge
    /// OUTSIDE the bearer layer so a client can bootstrap a token even under enforce
    /// (D-11, the token-to-get-a-token deadlock avoidance).
    #[tokio::test]
    async fn token_routes_exempt() {
        let (pool, _c) = start_pg().await;
        common::seed_fixture(&pool).await;
        let app = build_router(AppState {
            pool: pool.clone(),
            base_url: "http://test".to_string(),
            metrics: Metrics::new(),
            signer: common::test_signer(),
            enforce_auth: true, // enforce ON — token + JWKS must STILL be reachable.
            control: None,
        });

        let (st_token, _) = common::request(app.clone(), "POST", &token_path("t"), None).await;
        assert_eq!(
            st_token, 200,
            "token mint must be exempt from the bearer layer even with enforce ON"
        );

        let (st_keys, _) = common::request(app, "GET", &jwks_path("t"), None).await;
        assert_eq!(
            st_keys, 200,
            "JWKS must be exempt from the bearer layer even with enforce ON"
        );
    }

    /// GIVEN a `{tenant}` path segment that differs from the served tenant_id, WHEN a
    /// token is minted, THEN the request still returns 200 and the token's tid equals
    /// the SERVED tenant_id (the sim has one tenant; D-09 lean = accept any segment).
    #[tokio::test]
    async fn tenant_segment_accepted() {
        use jsonwebtoken::{Algorithm, DecodingKey, Validation, decode, jwk::Jwk};

        let (app, _c, _pool, _seed) = identity_app().await;

        // A deliberately different tenant path segment.
        let foreign = "ffffffff-ffff-ffff-ffff-ffffffffffff";
        let (st, body) = common::request(app.clone(), "POST", &token_path(foreign), None).await;
        assert_eq!(st, 200, "any {{tenant}} segment is accepted");
        let access = body["access_token"].as_str().expect("access_token string");

        let (_st_keys, keys) = common::request(app, "GET", &jwks_path(foreign), None).await;
        let jwk: Jwk = serde_json::from_value(keys["keys"][0].clone()).expect("parse served JWK");
        let decoding = DecodingKey::from_jwk(&jwk).expect("decoding key");
        let mut validation = Validation::new(Algorithm::RS256);
        validation.set_issuer(&[served_issuer()]);
        validation.set_audience(&[served_audience()]);
        let data = decode::<serde_json::Value>(access, &decoding, &validation).expect("decode");
        assert_eq!(
            data.claims["tid"],
            common::TENANT_ID.to_string(),
            "tid is minted with the SERVED tenant_id, not the {{tenant}} path value"
        );
    }

    /// GIVEN a router built with `enforce_auth: true`, WHEN a mock-minted token is
    /// presented on a data route THEN it is accepted (200); WHEN a garbage / expired /
    /// wrong-aud / wrong-iss / HS256-signed token is presented THEN it is rejected with
    /// a 401 ARM CloudError (code `InvalidAuthenticationToken`) (IAM-05, D-10).
    ///
    /// The negative tokens are signed with the SAME run key (via the `signer` Arc) so
    /// each isolates a single validation check (exp/aud/iss/alg) rather than tripping on
    /// the signature — proving the enforce branch validates claims, not just the key.
    #[tokio::test]
    async fn enforce_auth_roundtrip() {
        use jsonwebtoken::{Algorithm, EncodingKey, Header, encode};

        let (pool, _c) = start_pg().await;
        common::seed_fixture(&pool).await;
        common::seed_identity_rows(&pool).await;
        let signer = common::test_signer();
        let app = build_router(AppState {
            pool: pool.clone(),
            base_url: "http://test".to_string(),
            metrics: Metrics::new(),
            signer: signer.clone(),
            enforce_auth: true,
            control: None,
        });

        let ra_path = role_assignments_path(&common::SUB_A);
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs();

        // (1) Round-trip core: a token from the server's OWN /token mint → 200.
        let (st_tok, body) = common::request(app.clone(), "POST", &token_path("t"), None).await;
        assert_eq!(st_tok, 200, "token mint must return 200");
        let minted = body["access_token"]
            .as_str()
            .expect("access_token")
            .to_string();
        let (st_ok, _) = common::request(app.clone(), "GET", &ra_path, Some(&minted)).await;
        assert_eq!(
            st_ok, 200,
            "a mock-minted token must be accepted on a data route under enforce"
        );

        // A claim builder over the SAME run key; vary exp/iss/aud per case.
        let claims = |exp: u64, iss: &str, aud: &str| {
            serde_json::json!({
                "iss": iss, "aud": aud, "tid": common::TENANT_ID.to_string(),
                "oid": "00000000-0000-0000-0000-0000000000aa",
                "sub": "00000000-0000-0000-0000-0000000000aa",
                "appid": "00000000-0000-0000-0000-0000000000bb",
                "azp": "00000000-0000-0000-0000-0000000000bb",
                "roles": ["mock-app-role"], "ver": "1.0",
                "iat": now, "nbf": now, "exp": exp,
            })
        };

        // Assert a data route 401s with the ARM CloudError envelope for `bearer`.
        async fn assert_401(app: axum::Router, path: &str, bearer: &str, why: &str) {
            let (st, body) = common::request(app, "GET", path, Some(bearer)).await;
            assert_eq!(st, 401, "{why}: must be 401");
            assert_eq!(
                body["error"]["code"], "InvalidAuthenticationToken",
                "{why}: 401 body must be the ARM CloudError InvalidAuthenticationToken"
            );
        }

        // (2) garbage (non-JWT) Bearer → 401.
        assert_401(app.clone(), &ra_path, "not-a-jwt", "garbage token").await;

        // (3) expired (exp well past jsonwebtoken's default 60s leeway), correctly
        // signed + iss + aud → 401.
        let expired = signer
            .mint(&claims(now - 7200, &signer.issuer, &signer.audience))
            .expect("mint expired");
        assert_401(app.clone(), &ra_path, &expired, "expired token").await;

        // (4) wrong audience → 401.
        let wrong_aud = signer
            .mint(&claims(
                now + 3600,
                &signer.issuer,
                "https://wrong.invalid/",
            ))
            .expect("mint wrong-aud");
        assert_401(app.clone(), &ra_path, &wrong_aud, "wrong-aud token").await;

        // (5) wrong issuer → 401.
        let wrong_iss = signer
            .mint(&claims(
                now + 3600,
                "https://sts.windows.net/ffffffff-ffff-ffff-ffff-ffffffffffff/",
                &signer.audience,
            ))
            .expect("mint wrong-iss");
        assert_401(app.clone(), &ra_path, &wrong_iss, "wrong-iss token").await;

        // (6) HS256 alg-confusion (symmetric secret) → 401 — the Validation accepts
        // only RS256, so an HS256 token never validates against the RSA key.
        let hs = encode(
            &Header::new(Algorithm::HS256),
            &claims(now + 3600, &signer.issuer, &signer.audience),
            &EncodingKey::from_secret(b"attacker-secret"),
        )
        .expect("mint HS256");
        assert_401(app, &ra_path, &hs, "HS256 alg-confusion token").await;
    }

    /// Startup schema preflight (this finding): GIVEN a pre-Phase-10 volume — the
    /// `synthetic` schema + a tenant exist (sql/001..003 + `seed_fixture`) but the
    /// identity tables were never provisioned (no sql/005 applied) — WHEN the server's
    /// boot preflight `ensure_identity_schema` runs, THEN it PROVISIONS the empty
    /// `synthetic.role_assignments` / `synthetic.principals` tables and the
    /// `Microsoft.Authorization/roleAssignments` read returns 200 with an empty `value`
    /// array, instead of 500ing on a missing relation.
    ///
    /// This is the no-error-masking contract: the table is REAL and empty (a `count(*)`
    /// of 0), not an empty response synthesized from a caught missing-relation error.
    /// Removing the `ensure_identity_schema` call makes the pre-flight assertions hold
    /// (the table is genuinely absent before) and the endpoint 500 — so this test fails
    /// if the preflight is dropped.
    #[tokio::test]
    async fn startup_preflight_provisions_missing_identity_schema() {
        // ISOLATED container/pool (never the shared seed_identity_rows fixture). Apply
        // ONLY the base fixture: synthetic schema + tenant + SUB_A exist, but sql/005 is
        // NOT applied — exactly a volume provisioned before Phase 10.
        let (pool, _c) = start_pg().await;
        common::seed_fixture(&pool).await;

        // Sanity: the identity table is genuinely ABSENT before the preflight (so the
        // endpoint would 500 on the missing relation — the bug this fixes).
        let pre = sqlx::query_scalar::<_, i64>("SELECT count(*) FROM synthetic.role_assignments")
            .fetch_one(&pool)
            .await;
        assert!(
            pre.is_err(),
            "synthetic.role_assignments must NOT exist before the preflight (got {pre:?})"
        );

        // The boot preflight: provision the identity schema idempotently.
        tenantless_server::ensure_identity_schema(&pool)
            .await
            .expect("ensure_identity_schema must provision the identity tables");

        // The table is now REAL and EMPTY — 0 rows, not a masked error. This is the
        // PROVISION-not-mask contract: empty business data is a genuine empty table.
        let post: i64 = sqlx::query_scalar("SELECT count(*) FROM synthetic.role_assignments")
            .fetch_one(&pool)
            .await
            .expect("synthetic.role_assignments exists after the preflight");
        assert_eq!(
            post, 0,
            "the provisioned table is empty — never rows masked as business data"
        );

        // And the served endpoint returns 200 with an empty list (not a 500).
        let app = build_router(AppState {
            pool,
            base_url: "http://test".to_string(),
            metrics: Metrics::new(),
            signer: common::test_signer(),
            enforce_auth: false,
            control: None,
        });
        let (status, body) = common::request(
            app,
            "GET",
            &format!(
                "{}?api-version=2022-04-01",
                role_assignments_path(&common::SUB_A)
            ),
            Some("x"),
        )
        .await;
        assert_eq!(
            status, 200,
            "roleAssignments must be 200 after the preflight provisions the schema"
        );
        let value = body["value"]
            .as_array()
            .expect("value must be an array, not a CloudError");
        assert!(
            value.is_empty(),
            "an identity-less tenant serves an EMPTY roleAssignments list, got {value:?}"
        );
    }
}

// ---------------------------------------------------------------------------------
// Execution-budget DB guards: preflight-timeout exemption and pool-exhaustion → 503.
// Both drive a REAL pool against the testcontainer, so they
// require Docker (CI-run / local-via-Docker), same posture as the cost-cap tests.
// ---------------------------------------------------------------------------------

/// The startup schema preflight must be EXEMPT from the runtime `statement_timeout`: a
/// legitimate first-run upgrade over a large estate could otherwise be cancelled and prevent
/// startup. `apply_schema_batch` runs the DDL under `SET LOCAL statement_timeout = 0`, and the
/// exemption must not leak back onto the pooled connection.
#[tokio::test]
async fn schema_preflight_exempt_from_runtime_statement_timeout() {
    use sqlx::postgres::PgPoolOptions;

    let (url, _container) = start_pg_url().await;
    // A pool whose connections carry a tight 50ms session statement_timeout — the stand-in
    // for DB_STATEMENT_TIMEOUT_MS during a slow first-run migration.
    let pool = PgPoolOptions::new()
        .after_connect(|conn, _meta| {
            Box::pin(async move {
                sqlx::query("SET statement_timeout = '50ms'")
                    .execute(conn)
                    .await?;
                Ok(())
            })
        })
        .connect(&url)
        .await
        .expect("tight-timeout pool");

    // Control: a bare 200ms statement IS cancelled by the 50ms budget (SQLSTATE 57014) —
    // proving the pool's runtime timeout is genuinely active.
    match sqlx::raw_sql("SELECT pg_sleep(0.2)").execute(&pool).await {
        Err(sqlx::Error::Database(db)) => assert_eq!(
            db.code().as_deref(),
            Some("57014"),
            "control statement must be a statement_timeout cancel"
        ),
        other => panic!("expected a 57014 cancel on the tight pool, got {other:?}"),
    }

    // Fixed: the SAME 200ms batch applied via apply_schema_batch runs to COMPLETION — the
    // preflight is exempt from the runtime timeout.
    tenantless_server::apply_schema_batch(&pool, "SELECT pg_sleep(0.2)")
        .await
        .expect("apply_schema_batch must exempt the migration from the runtime statement_timeout");

    // No leak: a fresh statement on the pool is bounded again (the LOCAL=0 reverted on commit).
    assert!(
        sqlx::raw_sql("SELECT pg_sleep(0.2)")
            .execute(&pool)
            .await
            .is_err(),
        "the LOCAL statement_timeout exemption must not leak past the migration batch"
    );
}

/// Pool exhaustion (the `acquire_timeout` elapses with no free connection → `PoolTimedOut`)
/// must map to a 503 ServiceUnavailable + `Retry-After: 1`, NOT a 500 — capacity exhaustion,
/// not a hard fault. Proven through a REAL single-connection pool with its sole connection
/// held.
#[tokio::test]
async fn pool_exhaustion_maps_to_503() {
    use axum::response::IntoResponse;
    use sqlx::postgres::PgPoolOptions;
    use std::time::Duration;

    let (url, _container) = start_pg_url().await;

    // Warm the container with a default-timeout connection first, so the tight pool below
    // doesn't race first-connection readiness (that would be a spurious PoolTimedOut on the
    // pool's own establishing acquire, before it is ever saturated).
    let warm = PgPool::connect(&url).await.expect("warm the container");
    warm.close().await;

    // A generous acquire_timeout: it bounds BOTH the pool's establishing connect (slow through
    // Docker Desktop's port proxy on Windows) AND the saturation wait below. It only needs to
    // be long enough that first-connection setup never races it — the saturation acquire still
    // fails closed once the sole connection is held.
    let pool = PgPoolOptions::new()
        .max_connections(1)
        .acquire_timeout(Duration::from_secs(8))
        .connect(&url)
        .await
        .expect("single-connection pool");

    // Pre-warm so the sole connection is established and returned to the pool as idle.
    sqlx::query("SELECT 1")
        .fetch_one(&pool)
        .await
        .expect("pre-warm the sole connection");

    // Hold the ONLY connection so the next acquire must time out.
    let _held = pool.acquire().await.expect("hold the sole connection");

    // A query needing a second connection times out acquiring → PoolTimedOut.
    let err = sqlx::query("SELECT 1")
        .fetch_one(&pool)
        .await
        .expect_err("acquire must time out while the sole connection is held");
    assert!(
        matches!(err, sqlx::Error::PoolTimedOut),
        "an exhausted acquire must be PoolTimedOut, got {err:?}"
    );

    // The From<sqlx::Error> mapping turns it into a 503 ServiceUnavailable + Retry-After: 1.
    let resp = tenantless_server::error::ApiError::from(err).into_response();
    assert_eq!(
        resp.status(),
        axum::http::StatusCode::SERVICE_UNAVAILABLE,
        "pool exhaustion must surface as a 503, not a 500"
    );
    assert_eq!(
        resp.headers()
            .get(axum::http::header::RETRY_AFTER)
            .and_then(|v| v.to_str().ok()),
        Some("1"),
        "a capacity 503 must carry Retry-After: 1"
    );
}
