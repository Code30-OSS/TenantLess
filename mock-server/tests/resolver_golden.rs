//! Frozen pre-v3 response golden: the byte-identity baseline the
//! unified resolver must reproduce.
//!
//! This suite captures the RAW response BODY bytes of the current ARM read handlers
//! (which read `synthetic.resources` DIRECTLY, overlay empty) for one list page and
//! one nested-resource detail, freezes them to committed fixture files under
//! `tests/common/pre_v3_golden/`, and asserts the freshly-served bytes equal the
//! frozen bytes.
//!
//! Why capture NOW (pre-resolver): capturing against the pre-swap handlers makes the
//! fixtures a genuine, independent pre-v3 reference rather than a post-hoc snapshot of
//! whatever the resolver happens to emit. The three ARM handlers re-point
//! their `FROM` clause onto `synthetic.arm_resolved_*` (the resolved view); with an
//! empty overlay THIS SAME TEST MUST STAY GREEN — that is the empty-overlay
//! `arm_byte_identical` proof, by construction (the `ResourceRow`/DTO/serde path is
//! unchanged; only the table name changes).
//!
//! FROZEN-FIXTURE RULE: the files under `tests/common/pre_v3_golden/` are the
//! independent pre-v3 golden. They MUST NOT be re-captured after the resolver swap — re-capturing
//! through the resolver would make the byte-identity assertion tautological. They are
//! (re)generated ONLY by a deliberate `TENANTLESS_BLESS_GOLDEN=1` run against the
//! pre-swap handlers; a missing golden is a HARD FAILURE otherwise (so a deleted or
//! absent fixture in CI fails loudly instead of silently re-blessing).

mod common;

use axum::{
    body::Body,
    http::{Request, StatusCode},
};
use sqlx::PgPool;
use std::path::PathBuf;
use tenantless_server::{build_router, metrics::Metrics, state::AppState};
use testcontainers_modules::{postgres, testcontainers::runners::AsyncRunner};
use tower::ServiceExt;

/// The committed pre-v3 golden fixtures (relative to this crate's manifest dir).
const GOLDEN_SUBDIR: &str = "tests/common/pre_v3_golden";
const LIST_GOLDEN: &str = "list_dense_rg_top3_page1.json";
const DETAIL_GOLDEN: &str = "detail_nested_sql_db.json";

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

/// Build the real router over the seeded fixture pool (the SAME `build_router` seam the
/// production server and every other integration test use — no hand-built bytes).
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

/// Issue a GET through the real router and return the status + RAW response body bytes
/// (NOT parsed — byte-identity is asserted on the wire bytes).
async fn serve_body_bytes(app: axum::Router, uri: &str) -> (StatusCode, Vec<u8>) {
    let req = Request::builder()
        .method("GET")
        .uri(uri)
        .header("Authorization", "Bearer x")
        .body(Body::empty())
        .expect("build request");
    let resp = app.oneshot(req).await.expect("oneshot");
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .expect("collect body");
    (status, bytes.to_vec())
}

/// Absolute path to a committed golden fixture.
fn golden_path(name: &str) -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join(GOLDEN_SUBDIR)
        .join(name)
}

/// Assert the live bytes equal the frozen golden. Captures the golden ONLY when
/// `TENANTLESS_BLESS_GOLDEN=1` (deliberate, pre-swap); a missing golden without the
/// bless flag is a hard failure (FROZEN-FIXTURE RULE).
fn assert_byte_identical(name: &str, live: &[u8]) {
    let path = golden_path(name);
    let bless = std::env::var("TENANTLESS_BLESS_GOLDEN").as_deref() == Ok("1");
    if !path.exists() {
        if bless {
            std::fs::write(&path, live).expect("write blessed golden");
        } else {
            panic!(
                "pre-v3 golden {name} is missing at {}. It MUST be committed. To (re)capture \
                 against the PRE-SWAP handlers, run with TENANTLESS_BLESS_GOLDEN=1 (never after the resolver swap).",
                path.display()
            );
        }
    }
    let frozen = std::fs::read(&path).expect("read frozen golden");
    assert_eq!(
        frozen, live,
        "pre-v3 byte-identity broken for {name}: the live response body diverged from the \
         frozen golden. Through the resolver with an EMPTY overlay this MUST match."
    );
}

/// The first list page (dense RG, `$top=3`) is byte-identical to the frozen
/// pre-v3 golden. Exercises the keyset list handler + `nextLink` emission through the
/// real router. After the resolver swap this same body must be produced by the resolved view on an
/// empty overlay.
#[tokio::test]
async fn list_body_byte_identical_to_pre_v3_golden() {
    let (pool, _container) = start_pg().await;
    common::seed_fixture(&pool).await;
    let app = seeded_router(pool);

    let sub = common::SUB_A;
    let uri = format!(
        "/subscriptions/{sub}/resourceGroups/{}/resources?$top=3",
        common::DENSE_RG_NAME
    );
    let (status, body) = serve_body_bytes(app, &uri).await;
    assert_eq!(status, StatusCode::OK, "list must 200");

    assert_byte_identical(LIST_GOLDEN, &body);
}

/// A nested-type resource detail is byte-identical to the frozen pre-v3
/// golden. Exercises the arbitrary-depth detail handler (single-object body, not an
/// envelope) through the real router. After the resolver swap this same body must be produced by the
/// resolved view on an empty overlay.
#[tokio::test]
async fn detail_body_byte_identical_to_pre_v3_golden() {
    let (pool, _container) = start_pg().await;
    common::seed_fixture(&pool).await;
    let app = seeded_router(pool);

    // NESTED_RESOURCE_ID is a full ARM path:
    //   /subscriptions/{SUB_A}/resourceGroups/rg-filter-000/providers/
    //   Microsoft.Sql/servers/sql-srv-000/databases/db-000
    // Used verbatim as the detail request URI (arbitrary nesting depth).
    let (status, body) = serve_body_bytes(app, common::NESTED_RESOURCE_ID).await;
    assert_eq!(status, StatusCode::OK, "detail must 200");

    assert_byte_identical(DETAIL_GOLDEN, &body);
}

// =====================================================================================
// Full-visit keyset pagination + `$filter` traversal over a
// MIXED baseline / overlay / tombstone estate.
//
// The byte-identity tests above prove the resolver reproduces the pre-v3 bytes with an EMPTY
// overlay. THESE prove that once the overlay is NON-empty, paginating the UNION-ALL resolved
// view still visits the ENTIRE live set exactly once — no row dropped or DUPLICATED at a page
// boundary (the real keyset risk across the baseline+overlay branches) — with the
// tombstone absent, the appeared overlay row present, and a `$filter` evaluated POST-resolution.
// Runs natively on the PG11 testcontainer (Docker): correctness at real fixture density, not
// 500K scale (the scale invariant lives in the DSN-gated `explain_plan_gate.rs`).
// =====================================================================================

/// RFC3986 percent-encode a `$filter` value for a query string (mirrors `integration.rs::enc`).
fn enc(s: &str) -> String {
    let mut out = String::new();
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(b as char)
            }
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

/// A present=true overlay snapshot body carrying the RESOLVED (post-drift) fields.
fn overlay_body(id: &str, name: &str, ty: &str, loc: &str) -> serde_json::Value {
    serde_json::json!({
        "id": id,
        "name": name,
        "type": ty,
        "location": loc,
        "tags": {},
        "properties": { "provisioningState": "Succeeded" }
    })
}

async fn seed_overlay(pool: &PgPool, id: &str, present: bool, body: Option<serde_json::Value>) {
    sqlx::query(
        "INSERT INTO synthetic.arm_overlay (id_lower, id, target_kind, source, present, body) \
         VALUES ($1, $2, 'resource', 'drift', $3, $4)",
    )
    .bind(id.to_lowercase())
    .bind(id)
    .bind(present)
    .bind(body)
    .execute(pool)
    .await
    .expect("seed overlay row");
}

/// The overlay-affected ids, returned for the walk assertions.
struct MixedIds {
    tomb: String,
    modr: String,
    appear: String,
}

/// Tombstone `res-0000`, modify `res-0001` → Compute/westus, and appear a NEW Storage row — all
/// under the dense RG (`seed_fixture` seeds 110 Storage rows `res-0000..res-0109` there).
async fn seed_mixed_overlay(pool: &PgPool) -> MixedIds {
    let sub = common::SUB_A;
    let rg = common::DENSE_RG_NAME;
    let rid = |name: &str| {
        format!(
            "/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Storage/storageAccounts/{name}"
        )
    };
    let tomb = rid("res-0000");
    let modr = rid("res-0001");
    // 'a' sorts after '0', so this appears LAST — proving a boundary row is still visited.
    let appear = rid("res-appear-9999");

    seed_overlay(pool, &tomb, false, None).await;
    seed_overlay(
        pool,
        &modr,
        true,
        Some(overlay_body(
            &modr,
            "res-0001",
            "Microsoft.Compute/virtualMachines",
            "westus",
        )),
    )
    .await;
    seed_overlay(
        pool,
        &appear,
        true,
        Some(overlay_body(
            &appear,
            "res-appear-9999",
            "Microsoft.Storage/storageAccounts",
            "eastus",
        )),
    )
    .await;

    MixedIds { tomb, modr, appear }
}

/// Walk `first_uri` following `nextLink` (over the `http://test` base) to exhaustion, returning
/// every `value[].id`. Asserts each page respects the requested `$top` cap.
async fn walk_ids(app: &axum::Router, first_uri: &str, top: usize) -> Vec<String> {
    let mut ids = Vec::new();
    let mut uri = first_uri.to_string();
    let mut pages = 0;
    loop {
        let (status, bytes) = serve_body_bytes(app.clone(), &uri).await;
        assert_eq!(status, StatusCode::OK, "list must 200 ({uri})");
        let body: serde_json::Value = serde_json::from_slice(&bytes).expect("list JSON");
        let value = body["value"].as_array().expect("value array");
        assert!(value.len() <= top, "no page may exceed $top={top}");
        for r in value {
            ids.push(r["id"].as_str().expect("id").to_string());
        }
        pages += 1;
        assert!(pages < 1000, "pagination must terminate");
        match body["nextLink"].as_str() {
            Some(link) => {
                uri = link
                    .strip_prefix("http://test")
                    .expect("nextLink is absolute on the test base_url")
                    .to_string()
            }
            None => break, // FINAL page omits nextLink.
        }
    }
    ids
}

/// SC-3: a full keyset walk of the dense RG over a MIXED estate returns EXACTLY the resolved
/// live set — no drops, no duplicates across page boundaries; the tombstone is absent and the
/// appeared overlay row (a last-position boundary id) is visited.
#[tokio::test]
async fn full_visit_pagination_mixed_estate_no_drops_or_dupes() {
    let (pool, _container) = start_pg().await;
    common::seed_fixture(&pool).await;
    let ids = seed_mixed_overlay(&pool).await;
    let app = seeded_router(pool.clone());

    let sub = common::SUB_A;
    let rg = common::DENSE_RG_NAME;
    let top = 7usize;
    let first = format!("/subscriptions/{sub}/resourceGroups/{rg}/resources?$top={top}");
    let walked = walk_ids(&app, &first, top).await;

    // No duplicates across page boundaries.
    let unique: std::collections::BTreeSet<&String> = walked.iter().collect();
    assert_eq!(
        unique.len(),
        walked.len(),
        "keyset pagination across the baseline+overlay union must not duplicate a row"
    );

    // The served set (sorted) == the resolved-view live set for the RG (ORDER BY id).
    let expected: Vec<String> = sqlx::query_scalar(
        "SELECT id FROM synthetic.arm_resolved_resources \
         WHERE subscription_id = $1 AND lower(resource_group_name) = lower($2) ORDER BY id",
    )
    .bind(sub)
    .bind(rg)
    .fetch_all(&pool)
    .await
    .expect("resolved-view RG set");
    let mut walked_sorted = walked.clone();
    walked_sorted.sort();
    assert_eq!(
        walked_sorted, expected,
        "full-visit pagination returns EXACTLY the resolved live set (no drop/dup)"
    );

    assert!(
        !walked.contains(&ids.tomb),
        "the tombstoned resource must be absent from the walk"
    );
    assert!(
        walked.contains(&ids.appear),
        "the appeared overlay resource (a boundary id) must be visited"
    );
    assert!(
        walked.contains(&ids.modr),
        "the modified resource is still live (resolved) and must be visited"
    );
    assert!(
        walked.len() > top,
        "the dense RG must require multiple pages (non-vacuous traversal)"
    );
}

/// SC-3: a `$filter` traversal over the mixed estate is evaluated POST-resolution — it returns
/// exactly the resolved rows of the filtered type, so the modified row (now Compute) drops out
/// of a Storage filter while the appeared Storage row is included, with no page drops/dupes.
#[tokio::test]
async fn filter_traversal_mixed_estate_matches_resolved_set() {
    let (pool, _container) = start_pg().await;
    common::seed_fixture(&pool).await;
    let ids = seed_mixed_overlay(&pool).await;
    let app = seeded_router(pool.clone());

    let sub = common::SUB_A;
    let rg = common::DENSE_RG_NAME;
    let ty = "Microsoft.Storage/storageAccounts";
    let top = 5usize;
    let filter = enc(&format!("resourceType eq '{ty}'"));
    let first =
        format!("/subscriptions/{sub}/resourceGroups/{rg}/resources?$top={top}&$filter={filter}");
    let walked = walk_ids(&app, &first, top).await;

    let unique: std::collections::BTreeSet<&String> = walked.iter().collect();
    assert_eq!(
        unique.len(),
        walked.len(),
        "$filter pagination must not duplicate a row"
    );

    // Post-resolution $filter: exactly the resolved Storage rows for the RG.
    let expected: Vec<String> = sqlx::query_scalar(
        "SELECT id FROM synthetic.arm_resolved_resources \
         WHERE subscription_id = $1 AND lower(resource_group_name) = lower($2) AND type = $3 \
         ORDER BY id",
    )
    .bind(sub)
    .bind(rg)
    .bind(ty)
    .fetch_all(&pool)
    .await
    .expect("resolved-view Storage set");
    let mut walked_sorted = walked.clone();
    walked_sorted.sort();
    assert_eq!(
        walked_sorted, expected,
        "$filter is evaluated POST-resolution over the resolved view"
    );

    assert!(
        !walked.contains(&ids.modr),
        "the modified→Compute row is excluded by the Storage $filter (resolved type)"
    );
    assert!(
        walked.contains(&ids.appear),
        "the appeared Storage overlay row matches the $filter"
    );
    assert!(
        !walked.contains(&ids.tomb),
        "the tombstoned row is absent under the $filter too"
    );
}
