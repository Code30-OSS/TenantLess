//! Integration tests for the simulator-only drift audit endpoints (DRIFT-05) and
//! the soft-delete ARM exclusion (D-11), driving the real router against an ephemeral
//! testcontainers Postgres seeded by `common::seed_fixture` + the SCOPED
//! `common::seed_drift_rows` helper (the shared fixture stays drift-free — project
//! memory: fixture coupling).
//!
//! Coverage:
//!   * `simulator_drift_auth` — the D-15/D-16 auth matrix: missing Bearer → 401,
//!     any non-empty Bearer → 200 (enforce OFF), valid RS256 JWT → 200 + invalid →
//!     401 (enforce ON).
//!   * `simulator_drift_reads` — the three audit reads return the seeded ground
//!     truth (list batches, get-batch + records + 404 on unknown, by-resource).
//!   * `drift_soft_delete_excluded` — a soft-deleted resource is absent from the ARM
//!     list AND 404 on detail, while the row still exists in the DB (D-11).

mod common;

use sqlx::PgPool;
use tenantless_server::{build_router, metrics::Metrics, state::AppState};
use testcontainers_modules::{postgres, testcontainers::runners::AsyncRunner};

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

/// Build a drift-seeded router (enforce OFF). Returns app, container guard, a pool
/// clone (for direct SQL asserts), and the drift ground truth.
async fn drift_app() -> (
    axum::Router,
    testcontainers::ContainerAsync<postgres::Postgres>,
    PgPool,
    common::DriftSeed,
) {
    let (pool, container) = start_pg().await;
    common::seed_fixture(&pool).await;
    let seed = common::seed_drift_rows(&pool).await;
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

/// Split a stored ARM resource id into (sub, rg, tail) for a detail request.
fn split_resource_id(id: &str) -> (String, String, String) {
    let rest = id
        .strip_prefix("/subscriptions/")
        .expect("id starts with /subscriptions/");
    let (sub, rest) = rest
        .split_once("/resourceGroups/")
        .expect("has resourceGroups");
    let (rg, tail) = rest.split_once("/providers/").expect("has providers");
    (sub.to_string(), rg.to_string(), tail.to_string())
}

/// DRIFT-05 / D-15/D-16 auth matrix on `/simulator/drift`:
///   * missing Bearer → 401 (the route is gated, NOT a bearer exemption — D-16);
///   * empty Bearer → 401;
///   * any non-empty Bearer → 200 (enforce OFF — the any-Bearer scanner contract);
///   * under `--enforce-auth`: a valid RS256 JWT (from the server's OWN /token mint)
///     → 200, and an invalid (non-JWT) token → 401 with the ARM CloudError code.
#[tokio::test]
async fn simulator_drift_auth() {
    let path = "/simulator/drift";

    // --- enforcement OFF (default) ---
    let (app, _c, _pool, _seed) = drift_app().await;

    let (no_auth, _) = common::request(app.clone(), "GET", path, None).await;
    assert_eq!(
        no_auth, 401,
        "missing Bearer must 401 on /simulator/drift (D-16: not exempt)"
    );

    let (empty, _) = common::request(app.clone(), "GET", path, Some("")).await;
    assert_eq!(empty, 401, "empty Bearer token must 401");

    let (any, _) = common::request(app, "GET", path, Some("arbitrary-non-jwt")).await;
    assert_eq!(any, 200, "any non-empty Bearer must 200 (enforce OFF)");

    // --- enforcement ON (--enforce-auth) ---
    let (pool, _c2) = start_pg().await;
    common::seed_fixture(&pool).await;
    common::seed_drift_rows(&pool).await;
    let app = build_router(AppState {
        pool: pool.clone(),
        base_url: "http://test".to_string(),
        metrics: Metrics::new(),
        signer: common::test_signer(),
        enforce_auth: true,
        control: None,
    });

    // A token from the server's OWN /token mint (exempt route) → valid RS256 JWT.
    let (st_tok, body) = common::request(app.clone(), "POST", "/t/oauth2/v2.0/token", None).await;
    assert_eq!(
        st_tok, 200,
        "token mint must 200 (exempt even under enforce)"
    );
    let minted = body["access_token"]
        .as_str()
        .expect("access_token")
        .to_string();

    let (st_ok, _) = common::request(app.clone(), "GET", path, Some(&minted)).await;
    assert_eq!(
        st_ok, 200,
        "a valid RS256 JWT must be accepted under --enforce-auth"
    );

    let (st_bad, bad_body) = common::request(app, "GET", path, Some("not-a-jwt")).await;
    assert_eq!(
        st_bad, 401,
        "an invalid token must 401 under --enforce-auth"
    );
    assert_eq!(
        bad_body["error"]["code"], "InvalidAuthenticationToken",
        "401 body must be the ARM CloudError InvalidAuthenticationToken"
    );
}

/// DRIFT-05: the three audit reads return the seeded ground truth.
#[tokio::test]
async fn simulator_drift_reads() {
    let (app, _c, _pool, seed) = drift_app().await;

    // (1) list_drift → all batches on one large page; the primary seeded batch is
    // present + unreverted. (`$top=1000` clears the small default so every seeded
    // batch — primary + continuation + bare — lands on a single page here.)
    let (st, body) =
        common::request(app.clone(), "GET", "/simulator/drift?$top=1000", Some("x")).await;
    assert_eq!(st, 200);
    let batches = body["value"].as_array().expect("value array");
    assert_eq!(
        batches.len() as i64,
        seed.batch_count,
        "every seeded batch on a full page"
    );
    let primary = batches
        .iter()
        .find(|b| b["batchId"].as_str().unwrap() == seed.batch_id.to_string())
        .expect("the primary seeded batch must be present");
    assert_eq!(primary["driftType"].as_str().unwrap(), seed.drift_type);
    assert!(
        primary["revertedAt"].is_null(),
        "an unreverted batch → revertedAt null"
    );
    assert!(
        primary["options"].is_object(),
        "options is the JSONB option set"
    );

    // (2) get_batch → the batch plus its per-field records.
    let batch_path = format!("/simulator/drift/{}", seed.batch_id);
    let (st, body) = common::request(app.clone(), "GET", &batch_path, Some("x")).await;
    assert_eq!(st, 200);
    assert_eq!(
        body["batch"]["batchId"].as_str().unwrap(),
        seed.batch_id.to_string()
    );
    let recs = body["records"].as_array().expect("records array");
    assert_eq!(
        recs.len() as i64,
        seed.record_count,
        "all batch records returned"
    );

    // P2c surface: every record exposes the persisted drift_code + metadata audit
    // columns (remediation 2/3) so a consumer can identify which mutation produced it.
    for r in recs {
        assert!(
            r["driftCode"].is_string() && !r["driftCode"].as_str().unwrap().is_empty(),
            "every record must surface a non-empty driftCode: {r}"
        );
        assert!(
            r["metadata"].is_object(),
            "every record must surface its metadata JSONB object: {r}"
        );
    }

    // (2b) unknown batch_id → 404 ResourceNotFound (not an empty payload).
    let unknown = uuid::Uuid::from_u128(0xdead_dead_dead_dead_dead_dead_dead_dead);
    let (st, b) = common::request(
        app.clone(),
        "GET",
        &format!("/simulator/drift/{unknown}"),
        Some("x"),
    )
    .await;
    assert_eq!(st, 404, "an unknown batch must 404");
    assert_eq!(b["error"]["code"], "ResourceNotFound");

    // (3) by_resource → records for the drifted resource. The `{*resource_id}` catch-all
    // strips the leading '/', so strip it when building the URL; the handler re-adds it.
    let stripped = seed
        .drifted_resource_id
        .strip_prefix('/')
        .expect("ARM id starts with /");
    let by_res_path = format!("/simulator/drift/resources/{stripped}");
    let (st, body) = common::request(app, "GET", &by_res_path, Some("x")).await;
    assert_eq!(st, 200);
    let recs = body["value"].as_array().expect("value array");
    assert_eq!(
        recs.len() as i64,
        seed.drifted_resource_record_count,
        "exactly the records touching the drifted resource"
    );
    for r in recs {
        assert_eq!(
            r["resourceId"].as_str().unwrap(),
            seed.drifted_resource_id,
            "every record must belong to the queried resource id"
        );
    }
}

/// P2d: the three audit reads are capped-paginated (`$top`) so a large batch/resource
/// cannot return an unbounded record set. The seed batch has 3 records (2 on the nested
/// resource); a small `$top` caps the page, while a `$top` above the 1000 ceiling is
/// clamped (never errors) and returns everything that exists under the cap.
#[tokio::test]
async fn simulator_drift_pagination() {
    let (app, _c, _pool, seed) = drift_app().await;

    // (1) get_batch records capped to $top=2 (3 records exist in the batch).
    let batch_path = format!("/simulator/drift/{}?$top=2", seed.batch_id);
    let (st, body) = common::request(app.clone(), "GET", &batch_path, Some("x")).await;
    assert_eq!(st, 200);
    let recs = body["records"].as_array().expect("records array");
    assert_eq!(
        recs.len(),
        2,
        "$top=2 must cap the batch records page to 2 of 3"
    );

    // (2) $top above the 1000 ceiling is clamped (no error) → all 3 records returned.
    let batch_path_big = format!("/simulator/drift/{}?$top=999999", seed.batch_id);
    let (st, body) = common::request(app.clone(), "GET", &batch_path_big, Some("x")).await;
    assert_eq!(st, 200, "a $top above the max must clamp, not error");
    let recs = body["records"].as_array().expect("records array");
    assert_eq!(
        recs.len() as i64,
        seed.record_count,
        "clamped $top returns all 3 records"
    );

    // (3) by_resource capped to $top=1 (the nested resource carries 2 records).
    let stripped = seed
        .drifted_resource_id
        .strip_prefix('/')
        .expect("ARM id starts with /");
    let by_res_path = format!("/simulator/drift/resources/{stripped}?$top=1");
    let (st, body) = common::request(app.clone(), "GET", &by_res_path, Some("x")).await;
    assert_eq!(st, 200);
    let recs = body["value"].as_array().expect("value array");
    assert_eq!(
        recs.len(),
        1,
        "$top=1 must cap by-resource records to 1 of 2"
    );

    // (4) list_drift: a `$top` >= batch_count fits every batch on a single page and
    // therefore omits nextLink (final page — no surplus row).
    let big = format!("/simulator/drift?$top={}", seed.batch_count + 10);
    let (st, body) = common::request(app, "GET", &big, Some("x")).await;
    assert_eq!(st, 200);
    assert_eq!(
        body["value"].as_array().expect("value array").len() as i64,
        seed.batch_count,
        "a $top above the batch count returns every batch on one page"
    );
    assert!(
        body["nextLink"].is_null(),
        "a full single page must omit nextLink"
    );
}

/// Strip the test `base_url` so an absolute `nextLink` can be replayed via `request`.
fn next_path(link: &str) -> String {
    link.strip_prefix("http://test")
        .expect("nextLink must be absolute under the configured base_url")
        .to_string()
}

/// 11-11 — the CORE gap: capped pagination previously truncated with NO continuation,
/// leaving every row past the first page permanently inaccessible. All three drift
/// reads must now emit an opaque `$skiptoken` `nextLink` while more rows exist and omit
/// it on the FINAL page; following `nextLink` retrieves the remaining rows. With a small
/// `$top=2` against M>2 seeded rows, the traversal MUST require multiple pages and
/// recover EVERY row exactly once.
#[tokio::test]
async fn simulator_drift_continuation() {
    let (app, _c, _pool, seed) = drift_app().await;

    // (1) list_drift: page through ALL batches with $top=2.
    let mut batch_ids: Vec<String> = Vec::new();
    let mut path = "/simulator/drift?$top=2".to_string();
    let mut pages = 0;
    loop {
        let (st, body) = common::request(app.clone(), "GET", &path, Some("x")).await;
        assert_eq!(st, 200);
        let v = body["value"].as_array().expect("value array");
        assert!(v.len() as i64 <= 2, "no page may exceed $top=2");
        for b in v {
            batch_ids.push(b["batchId"].as_str().unwrap().to_string());
        }
        pages += 1;
        assert!(
            pages <= seed.batch_count + 2,
            "list_drift pagination must terminate"
        );
        match body["nextLink"].as_str() {
            Some(link) => path = next_path(link),
            None => break, // FINAL page omits nextLink.
        }
    }
    assert_eq!(
        batch_ids.len() as i64,
        seed.batch_count,
        "every batch must be retrievable across pages — no row inaccessible"
    );
    assert!(
        batch_ids.contains(&seed.batch_id.to_string()),
        "the primary batch is reachable"
    );
    let mut uniq = batch_ids.clone();
    uniq.sort();
    uniq.dedup();
    assert_eq!(
        uniq.len(),
        batch_ids.len(),
        "no batch returned twice across pages"
    );

    // (2) get_batch records: page through ALL records of the continuation batch.
    let mut rec_ids: Vec<i64> = Vec::new();
    let mut path = format!("/simulator/drift/{}?$top=2", seed.cont_batch_id);
    let mut pages = 0;
    loop {
        let (st, body) = common::request(app.clone(), "GET", &path, Some("x")).await;
        assert_eq!(st, 200);
        assert_eq!(
            body["batch"]["batchId"].as_str().unwrap(),
            seed.cont_batch_id.to_string(),
            "the batch stays single on every records page"
        );
        let recs = body["records"].as_array().expect("records array");
        assert!(recs.len() as i64 <= 2, "no records page may exceed $top=2");
        for r in recs {
            rec_ids.push(r["recordId"].as_i64().unwrap());
        }
        pages += 1;
        assert!(
            pages <= seed.cont_record_count + 2,
            "get_batch pagination must terminate"
        );
        match body["nextLink"].as_str() {
            Some(link) => path = next_path(link),
            None => break,
        }
    }
    assert_eq!(
        rec_ids.len() as i64,
        seed.cont_record_count,
        "every record of the batch must be retrievable across pages"
    );

    // (3) by_resource: page through ALL records of the continuation resource.
    let stripped = seed
        .cont_resource_id
        .strip_prefix('/')
        .expect("ARM id starts with /");
    let mut rids: Vec<i64> = Vec::new();
    let mut path = format!("/simulator/drift/resources/{stripped}?$top=2");
    let mut pages = 0;
    loop {
        let (st, body) = common::request(app.clone(), "GET", &path, Some("x")).await;
        assert_eq!(st, 200);
        let recs = body["value"].as_array().expect("value array");
        assert!(
            recs.len() as i64 <= 2,
            "no by-resource page may exceed $top=2"
        );
        for r in recs {
            assert_eq!(
                r["resourceId"].as_str().unwrap(),
                seed.cont_resource_id,
                "every record must belong to the queried resource id"
            );
            rids.push(r["recordId"].as_i64().unwrap());
        }
        pages += 1;
        assert!(
            pages <= seed.cont_resource_record_count + 2,
            "by_resource pagination must terminate"
        );
        match body["nextLink"].as_str() {
            Some(link) => path = next_path(link),
            None => break,
        }
    }
    assert_eq!(
        rids.len() as i64,
        seed.cont_resource_record_count,
        "every record touching the resource must be retrievable across pages"
    );
}

/// 11-11 — a garbage `$skiptoken` must be a 400 BadRequest (NOT a 500, and NEVER an
/// SQL/cursor leak), on all three reads. `!` is outside the base64-url-safe alphabet so
/// the opaque token decode fails before any query runs; a token that decodes to non-i64
/// text likewise fails the numeric cursor parse. Neither path may surface a 500.
#[tokio::test]
async fn simulator_drift_bad_skiptoken() {
    let (app, _c, _pool, seed) = drift_app().await;

    // base64-undecodable token.
    let garbage = "not!a!token!!";
    // a VALID base64 token whose decoded payload is non-numeric ("abc") → bad cursor.
    let non_numeric = "YWJj"; // base64url("abc")

    let stripped = seed
        .cont_resource_id
        .strip_prefix('/')
        .expect("ARM id starts with /");
    let cases = [
        format!("/simulator/drift?$skiptoken={garbage}"),
        format!("/simulator/drift?$skiptoken={non_numeric}"),
        format!(
            "/simulator/drift/{}?$skiptoken={garbage}",
            seed.cont_batch_id
        ),
        format!(
            "/simulator/drift/{}?$skiptoken={non_numeric}",
            seed.cont_batch_id
        ),
        format!("/simulator/drift/resources/{stripped}?$skiptoken={garbage}"),
        format!("/simulator/drift/resources/{stripped}?$skiptoken={non_numeric}"),
    ];
    for path in cases {
        let (st, body) = common::request(app.clone(), "GET", &path, Some("x")).await;
        assert_eq!(st, 400, "a malformed $skiptoken must 400, not 500: {path}");
        // No SQL/cursor text leaks — the message is the fixed opaque-token string.
        let msg = body["error"]["message"].as_str().unwrap_or_default();
        assert!(
            !msg.to_lowercase().contains("select")
                && !msg.contains("$2")
                && !msg.contains("bigint"),
            "400 body must not leak SQL/cursor internals: {msg}"
        );
    }
}

/// D-11: a soft-deleted resource is ABSENT from the ARM resource list AND 404 on its
/// detail id, while the row still EXISTS in the DB with `drift_deleted_at IS NOT NULL`
/// (soft-delete, not hard delete — D-09).
#[tokio::test]
async fn drift_soft_delete_excluded() {
    let (app, _c, pool, seed) = drift_app().await;
    let hidden = seed.soft_deleted_resource_id.clone();

    // (1) absent from the sub-scoped list (all 116 − 1 hidden = 115 on one page).
    let list_path = format!("/subscriptions/{}/resources?$top=1500", common::SUB_A);
    let (st, body) = common::request(app.clone(), "GET", &list_path, Some("x")).await;
    assert_eq!(st, 200);
    let items = body["value"].as_array().expect("value array");
    assert_eq!(
        items.len(),
        115,
        "one of the 116 fixture resources is hidden"
    );
    assert!(
        items.iter().all(|i| i["id"].as_str().unwrap() != hidden),
        "the soft-deleted resource must be absent from the ARM list"
    );

    // (2) detail GET on the hidden id → 404 ResourceNotFound.
    let (_sub, rg, tail) = split_resource_id(&hidden);
    let detail_path = format!(
        "/subscriptions/{}/resourceGroups/{}/providers/{}",
        common::SUB_A,
        rg,
        tail
    );
    let (st, b) = common::request(app, "GET", &detail_path, Some("x")).await;
    assert_eq!(st, 404, "soft-deleted resource detail must 404");
    assert_eq!(b["error"]["code"], "ResourceNotFound");

    // (3) the row is STILL in the DB (hidden, not deleted) with drift_deleted_at set.
    let still_present: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM synthetic.resources WHERE id = $1 AND drift_deleted_at IS NOT NULL",
    )
    .bind(&hidden)
    .fetch_one(&pool)
    .await
    .expect("count hidden row");
    assert_eq!(
        still_present, 1,
        "the row is hidden (drift_deleted_at set), not deleted"
    );
}
