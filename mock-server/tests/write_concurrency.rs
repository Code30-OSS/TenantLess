//! ETag / conditional-write concurrency proofs (LIFE-05, D-06/D-07/D-15/D-22).
//!
//! Through the real `build_router` with `enable_arm_writes: true`:
//!   * If-Match stale → 412; If-Match current → 200 + a strictly-newer ETag.
//!   * If-None-Match:"*" on an absent id → 201 (create-guard); on an existing id → 412.
//!   * If-Match on an ABSENT id → 412 (D-15, evaluated before existence — NOT 404).
//!   * D-22: a comma-list If-Match with the current token as one member → 200; a WEAK
//!     `W/"…"` If-Match → 412 (a weak validator never satisfies the strong compare).
//!   * D-06: the empty-overlay detail-GET and list BODIES carry NO `etag` field (header-only).
//!
//! DB-gated: requires Docker/PG16; validates on the Linux CI gate. The empty-overlay
//! body-byte identity is additionally pinned by `resolver_golden.rs` (run separately).

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

fn res_id(name: &str) -> String {
    format!(
        "/subscriptions/{SUB}/resourceGroups/rg-1/providers/Microsoft.Storage/storageAccounts/{name}"
    )
}

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

async fn seed_baseline_resource(pool: &PgPool, name: &str) -> String {
    let sub = uuid::Uuid::parse_str(SUB).unwrap();
    let id = res_id(name);
    pool.execute(
        sqlx::query(
            "INSERT INTO synthetic.resources \
                 (id, subscription_id, resource_group_name, name, type, location, tags, sku, \
                  kind, properties, provisioning_state, managed_by) \
             VALUES ($1, $2, 'rg-1', $3, 'Microsoft.Storage/storageAccounts', 'eastus', \
                     '{\"env\":\"base\"}'::jsonb, NULL, NULL, \
                     '{\"provisioningState\":\"Succeeded\"}'::jsonb, 'Succeeded', NULL)",
        )
        .bind(&id)
        .bind(sub)
        .bind(name),
    )
    .await
    .expect("baseline resource");
    id
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

fn body_for(id: &str, name: &str) -> Value {
    json!({
        "id": id,
        "name": name,
        "type": "Microsoft.Storage/storageAccounts",
        "location": "eastus",
        "tags": {},
        "properties": {}
    })
}

/// PUT with an optional conditional header.
async fn put_cond(
    app: &Router,
    uri: &str,
    body: &Value,
    cond: &[(&str, &str)],
) -> (StatusCode, HeaderMap, Value) {
    let mut extra = vec![("Content-Type", "application/json")];
    extra.extend_from_slice(cond);
    request(
        app,
        "PUT",
        uri,
        &extra,
        Some(serde_json::to_vec(body).unwrap()),
    )
    .await
}

async fn get(app: &Router, uri: &str) -> (StatusCode, HeaderMap, Value) {
    request(app, "GET", uri, &[], None).await
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

// --------------------------------------------------------------------------------------- //
// (a) If-Match stale → 412; current → 200 + a strictly-newer ETag. (d) GET returns it.
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn if_match_stale_412_current_200_and_get_returns_new_etag() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool).await;
    let app = writes_enabled_router(pool);
    let id = res_id("acct-a");

    // Create, then replace unconditionally so the first token is genuinely stale.
    let (_s, h1, _b) = put_cond(&app, &id, &body_for(&id, "acct-a"), &[]).await;
    let stale = etag_of(&h1).unwrap();
    let (_s, h2, _b) = put_cond(&app, &id, &body_for(&id, "acct-a"), &[]).await;
    let current = etag_of(&h2).unwrap();
    assert!(overlay_rev(&current) > overlay_rev(&stale));

    // Stale If-Match → 412.
    let (ss, _sh, _sb) =
        put_cond(&app, &id, &body_for(&id, "acct-a"), &[("If-Match", &stale)]).await;
    assert_eq!(ss, StatusCode::PRECONDITION_FAILED, "stale If-Match → 412");

    // Current If-Match → 200 + a strictly-newer ETag.
    let (cs, ch, _cb) = put_cond(
        &app,
        &id,
        &body_for(&id, "acct-a"),
        &[("If-Match", &current)],
    )
    .await;
    assert_eq!(cs, StatusCode::OK, "current If-Match → 200");
    let after = etag_of(&ch).expect("mutation returns the new ETag");
    assert!(
        overlay_rev(&after) > overlay_rev(&current),
        "the mutation advances the ETag"
    );

    // A subsequent GET returns that same token.
    let (_gs, gh, _gb) = get(&app, &id).await;
    assert_eq!(
        etag_of(&gh).as_deref(),
        Some(after.as_str()),
        "GET returns the new token"
    );
}

// --------------------------------------------------------------------------------------- //
// (b) If-None-Match:"*" create-guard.
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn if_none_match_star_guards_creation() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool).await;
    let app = writes_enabled_router(pool);
    let id = res_id("acct-b");

    // Absent → the create-guard passes → 201.
    let (s1, _h1, _b1) = put_cond(
        &app,
        &id,
        &body_for(&id, "acct-b"),
        &[("If-None-Match", "*")],
    )
    .await;
    assert_eq!(s1, StatusCode::CREATED, "If-None-Match:* on absent → 201");

    // Now existing → the create-guard fails → 412.
    let (s2, _h2, _b2) = put_cond(
        &app,
        &id,
        &body_for(&id, "acct-b"),
        &[("If-None-Match", "*")],
    )
    .await;
    assert_eq!(
        s2,
        StatusCode::PRECONDITION_FAILED,
        "If-None-Match:* on existing → 412"
    );
}

// --------------------------------------------------------------------------------------- //
// (c) If-Match on an absent id → 412 (D-15, not 404).
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn if_match_on_absent_is_412_not_404() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool).await;
    let app = writes_enabled_router(pool);
    let id = res_id("acct-c");

    let (s, _h, _b) = put_cond(
        &app,
        &id,
        &body_for(&id, "acct-c"),
        &[("If-Match", "\"o-1\"")],
    )
    .await;
    assert_eq!(
        s,
        StatusCode::PRECONDITION_FAILED,
        "If-Match on an absent id → 412 (D-15), never 404 or 201"
    );
}

// --------------------------------------------------------------------------------------- //
// (e) D-22 comma-list (any strong member matches) + weak validator (never matches).
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn if_match_comma_list_matches_and_weak_validator_fails() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool).await;
    let app = writes_enabled_router(pool);
    let id = res_id("acct-d");

    let (_s, h, _b) = put_cond(&app, &id, &body_for(&id, "acct-d"), &[]).await;
    let current = etag_of(&h).unwrap();

    // Comma-list with the current token as one member → 200 (any strong member matches).
    let list = format!("\"o-0\", {current}");
    let (ls, _lh, _lb) =
        put_cond(&app, &id, &body_for(&id, "acct-d"), &[("If-Match", &list)]).await;
    assert_eq!(
        ls,
        StatusCode::OK,
        "comma-list with the current token → 200 (D-22)"
    );

    // A WEAK validator of the (now-newer) current token → 412 (strong compare never matches).
    let (_gs, gh, _gb) = get(&app, &id).await;
    let now = etag_of(&gh).unwrap();
    let weak = format!("W/{now}");
    let (ws, _wh, _wb) =
        put_cond(&app, &id, &body_for(&id, "acct-d"), &[("If-Match", &weak)]).await;
    assert_eq!(
        ws,
        StatusCode::PRECONDITION_FAILED,
        "a weak W/ validator → 412 (D-22)"
    );
}

// --------------------------------------------------------------------------------------- //
// (g) TOCTOU: concurrent If-Match writes on the SAME token serialize — exactly one wins,
//     the revision advances exactly once (no lost update). Sequential tests (a) cannot see
//     this; this races real handlers through the pool.
// --------------------------------------------------------------------------------------- //

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn concurrent_if_match_writes_serialize_exactly_one_wins() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool).await;
    let app = writes_enabled_router(pool);
    let id = res_id("acct-race");

    // Create the resource; capture its ETag — the shared token every racer conditions on.
    let created = body_for(&id, "acct-race");
    let (_s, h0, _b) = put_cond(&app, &id, &created, &[]).await;
    let original = etag_of(&h0).unwrap();
    let base_rev = overlay_rev(&original);

    // Fire N concurrent PUTs, each conditioned on the SAME original ETag. With read→check→
    // write serialized under the write-plane advisory lock, exactly ONE observes the token as
    // current (200) and advances the revision; every other sees the now-newer revision → 412.
    // Without serialization (the old TOCTOU) several read the original before any commit and
    // all succeed → multiple 200s and a revision advanced more than once (lost updates).
    const RACERS: usize = 12;
    let mut handles = Vec::new();
    for _ in 0..RACERS {
        let app = app.clone();
        let id = id.clone();
        let cond = original.clone();
        handles.push(tokio::spawn(async move {
            let body = body_for(&id, "acct-race");
            let (status, headers, _b) =
                put_cond(&app, &id, &body, &[("If-Match", cond.as_str())]).await;
            (status, etag_of(&headers).map(|t| overlay_rev(&t)))
        }));
    }
    let mut ok = 0usize;
    let mut precond = 0usize;
    let mut winner_rev: Option<i64> = None;
    for h in handles {
        let (status, rev) = h.await.unwrap();
        match status {
            StatusCode::OK => {
                ok += 1;
                winner_rev = rev;
            }
            StatusCode::PRECONDITION_FAILED => precond += 1,
            other => panic!("unexpected status {other}"),
        }
    }
    assert_eq!(ok, 1, "exactly one conditional writer wins the race");
    assert_eq!(
        precond,
        RACERS - 1,
        "every other racer sees the advanced revision → 412"
    );

    // No lost update: the persisted revision is EXACTLY the single winner's write — no other
    // racer's write slipped through on top of it. (`base_rev` only anchors that the winner
    // advanced past the create; a conflicting upsert advances the revision by more than one,
    // so we compare to the winner's own token, not `base_rev + 1`.)
    let winner_rev = winner_rev.expect("the single winner returned its new ETag");
    assert!(
        winner_rev > base_rev,
        "the winning conditional write advanced the revision past the create"
    );
    let (_gs, gh, _gb) = get(&app, &id).await;
    let final_rev = overlay_rev(&etag_of(&gh).unwrap());
    assert_eq!(
        final_rev, winner_rev,
        "the persisted state is the winner's write — no lost update overwrote it"
    );
}

// --------------------------------------------------------------------------------------- //
// (f) D-06: empty-overlay detail + list bodies carry NO `etag` field (header-only).
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn empty_overlay_bodies_have_no_etag_field() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool).await;
    let id = seed_baseline_resource(&pool, "acct-e").await;
    let app = writes_enabled_router(pool);

    // Detail GET: an ETag header is present, but the JSON body has NO `etag` field.
    let (ds, dh, db) = get(&app, &id).await;
    assert_eq!(ds, StatusCode::OK);
    assert!(
        etag_of(&dh).is_some(),
        "detail GET still carries an ETag header"
    );
    assert!(
        db.get("etag").is_none(),
        "the detail body carries NO etag field (header-only, D-06)"
    );

    // List: no per-item `etag` field, no list ETag header.
    let (ls, lh, lb) = get(&app, &format!("/subscriptions/{SUB}/resources")).await;
    assert_eq!(ls, StatusCode::OK);
    assert!(etag_of(&lh).is_none(), "a list carries no ETag header");
    for item in lb["value"].as_array().unwrap() {
        assert!(
            item.get("etag").is_none(),
            "no per-item etag field in the list"
        );
    }
}
