//! DELETE **nested-containment cascade** proofs (writes ENABLED) — the D-09 atomic,
//! segment-parsed cascade + the D-24 requested-parent tombstone ETag on the `204`.
//!
//! A DELETE of a parent id tombstones the target AND every resource whose canonical id is a
//! strict nested descendant (by parsed id SEGMENTS, never a raw string prefix), in ONE
//! transaction. Covers:
//!   * parent + child both present → both tombstoned → both `404` (mixed overlay set).
//!   * the `204` carries the REQUESTED-PARENT tombstone's `o-<revision>` ETag (D-24), not a
//!     descendant's, with an empty body.
//!   * the `s1`/`s10` sibling-lookalike trap: `DELETE .../servers/s1` never sweeps
//!     `.../servers/s10` (segment-parsed containment, not string-prefix).
//!   * a MIXED baseline + overlay descendant set: baseline descendants get user tombstones AND
//!     overlay descendants flip `present=false`, all in one cascade.
//!   * a stale parent `If-Match` → `412` BEFORE the transaction: nothing rolled forward, the
//!     child stays present (precondition-before-txn atomicity).
//!   * no descendants → the single-target `204` (idempotent) still carries the parent ETag.
//!
//! Harness mirrors `write_lifecycle.rs`: an ephemeral testcontainers Postgres, the
//! bare-first-boot overlay substrate, the real `build_router` with `enable_arm_writes: true`,
//! and `tower::ServiceExt::oneshot`. DB-gated (Docker/PG16; validates on the Linux CI gate).

mod common;

use axum::{
    Router,
    body::Body,
    http::{HeaderMap, Request, StatusCode, header},
};
use serde_json::{Value, json};
use sqlx::Executor;
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

fn writes_enabled_router(pool: PgPool) -> Router {
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

/// A resource id under `providers/{tail}` (tail is the provider-onward path, any depth).
fn id_for(rg: &str, tail: &str) -> String {
    format!("/subscriptions/{SUB}/resourceGroups/{rg}/providers/{tail}")
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
        .bind(format!("/subscriptions/{SUB}/resourceGroups/{rg}"))
        .bind(sub)
        .bind(rg),
    )
    .await
    .expect("resource group");
}

/// Seed a BASELINE (`synthetic.resources`) row at a canonical `id`/`name`/`type` (for the
/// mixed baseline+overlay cascade proof — a baseline descendant must get a user tombstone).
async fn seed_baseline_resource(pool: &PgPool, rg: &str, id: &str, name: &str, type_str: &str) {
    let sub = uuid::Uuid::parse_str(SUB).unwrap();
    pool.execute(
        sqlx::query(
            "INSERT INTO synthetic.resources \
                 (id, subscription_id, resource_group_name, name, type, location, tags, sku, \
                  kind, properties, provisioning_state, managed_by) \
             VALUES ($1, $2, $3, $4, $5, 'eastus', '{\"env\":\"base\"}'::jsonb, NULL, NULL, \
                     '{\"provisioningState\":\"Succeeded\"}'::jsonb, 'Succeeded', NULL)",
        )
        .bind(id)
        .bind(sub)
        .bind(rg)
        .bind(name)
        .bind(type_str),
    )
    .await
    .expect("baseline resource");
}

async fn request(
    app: &Router,
    method: &str,
    uri: &str,
    extra: &[(&str, &str)],
    body: Option<Vec<u8>>,
) -> (StatusCode, HeaderMap, Value) {
    let mut builder = Request::builder()
        .method(method)
        .uri(uri)
        .header("Authorization", "Bearer x");
    for (k, v) in extra {
        builder = builder.header(*k, *v);
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

/// PUT a resource as a present `source='user'` overlay row (minimal body: server derives
/// id/name/type from the URL, `location` defaults to `global`).
async fn put_overlay(app: &Router, id: &str) -> (StatusCode, HeaderMap, Value) {
    request(
        app,
        "PUT",
        id,
        &[("Content-Type", "application/json")],
        Some(serde_json::to_vec(&json!({ "properties": {}, "tags": {} })).unwrap()),
    )
    .await
}

async fn get(app: &Router, uri: &str) -> (StatusCode, HeaderMap, Value) {
    request(app, "GET", uri, &[], None).await
}

async fn delete(app: &Router, uri: &str, extra: &[(&str, &str)]) -> (StatusCode, HeaderMap, Value) {
    request(app, "DELETE", uri, extra, None).await
}

fn etag_of(headers: &HeaderMap) -> Option<String> {
    headers
        .get(header::ETAG)
        .map(|v| v.to_str().expect("etag ascii").to_string())
}

fn overlay_rev(tok: &str) -> i64 {
    tok.trim_matches('"')
        .strip_prefix("o-")
        .expect("o- prefixed token")
        .parse()
        .expect("revision is an i64")
}

/// The (present, source) of the overlay row for `id_lower`, or `None` if no overlay row.
async fn overlay_state(pool: &PgPool, id: &str) -> Option<(bool, String)> {
    sqlx::query_as("SELECT present, source FROM synthetic.arm_overlay WHERE id_lower = lower($1)")
        .bind(id)
        .fetch_optional(pool)
        .await
        .expect("overlay query")
}

/// The overlay row's stored revision for `id_lower`.
async fn overlay_revision(pool: &PgPool, id: &str) -> i64 {
    sqlx::query_scalar("SELECT revision FROM synthetic.arm_overlay WHERE id_lower = lower($1)")
        .bind(id)
        .fetch_one(pool)
        .await
        .expect("overlay revision")
}

// --------------------------------------------------------------------------------------- //
// Cascade: parent + child both present → both tombstoned → both 404 (D-09).
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn cascade_tombstones_parent_and_child() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-1").await;
    let app = writes_enabled_router(pool.clone());

    let parent = id_for("rg-1", "Microsoft.Sql/servers/s1");
    let child = id_for("rg-1", "Microsoft.Sql/servers/s1/databases/d1");
    assert_eq!(put_overlay(&app, &parent).await.0, StatusCode::CREATED);
    assert_eq!(put_overlay(&app, &child).await.0, StatusCode::CREATED);

    // DELETE the parent → 204 (cascade).
    let (ds, _dh, _db) = delete(&app, &parent, &[]).await;
    assert_eq!(ds, StatusCode::NO_CONTENT, "cascade DELETE → 204");

    // BOTH the parent and the nested child are now 404.
    assert_eq!(
        get(&app, &parent).await.0,
        StatusCode::NOT_FOUND,
        "parent → 404 after cascade"
    );
    assert_eq!(
        get(&app, &child).await.0,
        StatusCode::NOT_FOUND,
        "nested child → 404 after cascade (D-09)"
    );

    // Both carry a present=false, source='user' overlay tombstone row.
    assert_eq!(
        overlay_state(&pool, &parent).await,
        Some((false, "user".to_string())),
        "parent tombstone is source='user'"
    );
    assert_eq!(
        overlay_state(&pool, &child).await,
        Some((false, "user".to_string())),
        "child tombstone is source='user'"
    );
}

// --------------------------------------------------------------------------------------- //
// D-24: the cascade 204 carries the REQUESTED-PARENT tombstone ETag, not a descendant's.
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn cascade_204_carries_parent_tombstone_etag_not_a_descendants() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-1").await;
    let app = writes_enabled_router(pool.clone());

    let parent = id_for("rg-1", "Microsoft.Sql/servers/s2");
    let child = id_for("rg-1", "Microsoft.Sql/servers/s2/databases/d1");
    let _ = put_overlay(&app, &parent).await;
    let _ = put_overlay(&app, &child).await;

    let (ds, dh, db) = delete(&app, &parent, &[]).await;
    assert_eq!(ds, StatusCode::NO_CONTENT);
    assert_eq!(db, Value::Null, "the 204 has no body");

    let del_tok = etag_of(&dh).expect("the cascade 204 carries the parent tombstone ETag (D-24)");
    let del_rev = overlay_rev(&del_tok);

    // The 204 ETag equals the PARENT tombstone's stored revision …
    let parent_rev = overlay_revision(&pool, &parent).await;
    assert_eq!(
        del_rev, parent_rev,
        "the 204 ETag is the requested-parent tombstone revision (D-24)"
    );
    // … and NOT the child descendant's revision.
    let child_rev = overlay_revision(&pool, &child).await;
    assert_ne!(
        del_rev, child_rev,
        "the 204 ETag is NOT a descendant's revision (D-24)"
    );
}

// --------------------------------------------------------------------------------------- //
// Segment trap: DELETE .../servers/s1 must NOT sweep the sibling .../servers/s10.
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn sibling_lookalike_s10_survives_s1_cascade_delete() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-1").await;
    let app = writes_enabled_router(pool);

    let s1 = id_for("rg-1", "Microsoft.Sql/servers/s1");
    let s1_child = id_for("rg-1", "Microsoft.Sql/servers/s1/databases/d1");
    let s10 = id_for("rg-1", "Microsoft.Sql/servers/s10");
    let _ = put_overlay(&app, &s1).await;
    let _ = put_overlay(&app, &s1_child).await;
    let _ = put_overlay(&app, &s10).await;

    // DELETE s1 → the genuine child is swept, the sibling-lookalike s10 is NOT.
    assert_eq!(delete(&app, &s1, &[]).await.0, StatusCode::NO_CONTENT);
    assert_eq!(
        get(&app, &s1).await.0,
        StatusCode::NOT_FOUND,
        "s1 tombstoned"
    );
    assert_eq!(
        get(&app, &s1_child).await.0,
        StatusCode::NOT_FOUND,
        "s1/databases/d1 tombstoned"
    );
    assert_eq!(
        get(&app, &s10).await.0,
        StatusCode::OK,
        "sibling servers/s10 MUST survive (segment-parsed, not string-prefix)"
    );
}

// --------------------------------------------------------------------------------------- //
// Mixed set: a baseline descendant AND an overlay descendant both tombstone in one cascade.
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn cascade_mixes_baseline_and_overlay_descendants() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-1").await;

    let parent = id_for("rg-1", "Microsoft.Sql/servers/s3");
    let overlay_child = id_for("rg-1", "Microsoft.Sql/servers/s3/databases/dov");
    let baseline_child = id_for("rg-1", "Microsoft.Sql/servers/s3/databases/dbase");
    // A baseline (synthetic.resources) descendant — no overlay row yet.
    seed_baseline_resource(
        &pool,
        "rg-1",
        &baseline_child,
        "s3/dbase",
        "Microsoft.Sql/servers/databases",
    )
    .await;

    let app = writes_enabled_router(pool.clone());
    let _ = put_overlay(&app, &parent).await;
    let _ = put_overlay(&app, &overlay_child).await;

    // Sanity: the baseline descendant is live BEFORE the cascade.
    assert_eq!(
        get(&app, &baseline_child).await.0,
        StatusCode::OK,
        "baseline descendant live before cascade"
    );

    assert_eq!(delete(&app, &parent, &[]).await.0, StatusCode::NO_CONTENT);

    // BOTH descendants (overlay-present AND baseline) are now tombstoned → 404.
    assert_eq!(
        get(&app, &overlay_child).await.0,
        StatusCode::NOT_FOUND,
        "overlay descendant flips present=false"
    );
    assert_eq!(
        get(&app, &baseline_child).await.0,
        StatusCode::NOT_FOUND,
        "baseline descendant gets a user tombstone"
    );
    // The baseline descendant now has a user tombstone overlay row (was none before).
    assert_eq!(
        overlay_state(&pool, &baseline_child).await,
        Some((false, "user".to_string())),
        "baseline descendant → present=false, source='user'"
    );
}

// --------------------------------------------------------------------------------------- //
// Precondition before the transaction: a stale parent If-Match → 412, no partial cascade.
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn stale_parent_if_match_412_leaves_child_present() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-1").await;
    let app = writes_enabled_router(pool.clone());

    let parent = id_for("rg-1", "Microsoft.Sql/servers/s4");
    let child = id_for("rg-1", "Microsoft.Sql/servers/s4/databases/d1");
    let _ = put_overlay(&app, &parent).await;
    let _ = put_overlay(&app, &child).await;

    // A stale If-Match (not the parent's current ETag) → 412 BEFORE the cascade txn.
    let (ds, _dh, _db) = delete(&app, &parent, &[("If-Match", "\"o-999999\"")]).await;
    assert_eq!(
        ds,
        StatusCode::PRECONDITION_FAILED,
        "stale parent If-Match → 412 (precondition before txn)"
    );

    // Nothing was rolled forward: parent AND child are both still present.
    assert_eq!(
        get(&app, &parent).await.0,
        StatusCode::OK,
        "parent untouched after 412"
    );
    assert_eq!(
        get(&app, &child).await.0,
        StatusCode::OK,
        "child still present — no partial cascade after a 412"
    );
}

// --------------------------------------------------------------------------------------- //
// No descendants: the single-target 204 still works and carries the parent ETag (LIFE-03).
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn delete_with_no_descendants_still_204_with_parent_etag() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-1").await;
    let app = writes_enabled_router(pool.clone());

    let lone = id_for("rg-1", "Microsoft.Storage/storageAccounts/lone");
    let _ = put_overlay(&app, &lone).await;

    let (ds, dh, _db) = delete(&app, &lone, &[]).await;
    assert_eq!(ds, StatusCode::NO_CONTENT, "single-target DELETE → 204");
    let tok = etag_of(&dh).expect("204 carries the tombstone ETag");
    assert_eq!(
        overlay_rev(&tok),
        overlay_revision(&pool, &lone).await,
        "the 204 ETag equals the target tombstone revision (D-24)"
    );
    assert_eq!(get(&app, &lone).await.0, StatusCode::NOT_FOUND);
}

// --------------------------------------------------------------------------------------- //
// The descendant set is gathered INSIDE the DELETE's serialized transaction, so a child
// racing the cascade can never orphan a descendant that was live when the gather ran. A
// deterministic proof of the exact gather→commit window needs a fault-injection seam we do
// not have; this stress-races a parent cascade DELETE against a concurrent descendant PUT and
// asserts the tree is always left CONSISTENT (no deadlock, no partial cascade, no orphaned
// pre-existing descendant). The write-plane advisory lock that makes this hold is the same
// one proven deterministically serial by `write_concurrency`'s exactly-one-wins test.
// --------------------------------------------------------------------------------------- //

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn concurrent_delete_and_child_create_leave_a_consistent_tree() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-1").await;
    let app = writes_enabled_router(pool.clone());

    for i in 0..8 {
        let parent = id_for("rg-1", &format!("Microsoft.Sql/servers/p{i}"));
        let preexisting = id_for("rg-1", &format!("Microsoft.Sql/servers/p{i}/databases/d0"));
        let racer_child = id_for("rg-1", &format!("Microsoft.Sql/servers/p{i}/databases/d1"));
        assert_eq!(put_overlay(&app, &parent).await.0, StatusCode::CREATED);
        assert_eq!(put_overlay(&app, &preexisting).await.0, StatusCode::CREATED);

        // Race the parent cascade DELETE against creating ANOTHER child under it.
        let del_app = app.clone();
        let del_parent = parent.clone();
        let put_app = app.clone();
        let put_child = racer_child.clone();
        let del = tokio::spawn(async move { delete(&del_app, &del_parent, &[]).await.0 });
        let put = tokio::spawn(async move { put_overlay(&put_app, &put_child).await.0 });
        let del_status = del.await.unwrap();
        let _put_status = put.await.unwrap(); // the racer child may land either side of the delete
        assert_eq!(del_status, StatusCode::NO_CONTENT, "cascade DELETE → 204");

        // The parent is tombstoned, and the PRE-EXISTING descendant (live when the gather ran)
        // is ALWAYS swept with it — it can never be orphaned under a deleted parent.
        assert_eq!(
            get(&app, &parent).await.0,
            StatusCode::NOT_FOUND,
            "parent tombstoned after cascade"
        );
        assert_eq!(
            get(&app, &preexisting).await.0,
            StatusCode::NOT_FOUND,
            "a descendant live at gather time is always swept (never orphaned)"
        );
    }
}
