//! Generic ARM write-plane LIFECYCLE proofs (writes ENABLED) — full HTTP + DB round-trips
//! through the real `build_router` with `enable_arm_writes: true`.
//!
//! Covers STATE-03, LIFE-01/02/03/04, SYNC-01/02, WAUTH-01 (positive), and the review-locked
//! decisions the write+read round-trip touches: D-03 identity conflict, D-14 location
//! default/null, D-16 gating/parse ordering (enabled side), D-17 opaque-survives-GET, D-19
//! baseline casing, D-20 PATCH-404, D-21 structural type mismatch, D-24 DELETE-204 tombstone
//! ETag, D-25 Access-Control-Expose-Headers on GET + mutation.
//!
//! Harness mirrors `etag_header.rs`: an ephemeral testcontainers Postgres, the bare-first-boot
//! overlay substrate via `common::seed_overlay_first_boot` (NOT `seed_fixture`), the real
//! `build_router`, and `tower::ServiceExt::oneshot`. DB-gated: these require Docker/PG16 and
//! validate on the Linux CI gate.

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

/// A router with the write plane ARMED (`enable_arm_writes: true`).
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

fn res_id(rg: &str, name: &str) -> String {
    format!(
        "/subscriptions/{SUB}/resourceGroups/{rg}/providers/Microsoft.Storage/storageAccounts/{name}"
    )
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

/// Seed a baseline resource with a CALLER-SUPPLIED canonical id/name/type (for the D-19
/// baseline-casing proof — the baseline stores canonical casing the write must reuse).
async fn seed_baseline_resource(pool: &PgPool, id: &str, name: &str, type_str: &str) {
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
        .bind("rg-1")
        .bind(name)
        .bind(type_str),
    )
    .await
    .expect("baseline resource");
}

/// Drive ONE request through the real router. `extra` carries additional headers (e.g.
/// Content-Type, If-Match); `body` is the raw request bytes (may be malformed on purpose).
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

/// PUT a JSON body (Content-Type: application/json).
async fn put_json(app: &Router, uri: &str, body: &Value) -> (StatusCode, HeaderMap, Value) {
    request(
        app,
        "PUT",
        uri,
        &[("Content-Type", "application/json")],
        Some(serde_json::to_vec(body).unwrap()),
    )
    .await
}

async fn patch_json(app: &Router, uri: &str, body: &Value) -> (StatusCode, HeaderMap, Value) {
    request(
        app,
        "PATCH",
        uri,
        &[("Content-Type", "application/json")],
        Some(serde_json::to_vec(body).unwrap()),
    )
    .await
}

async fn get(app: &Router, uri: &str) -> (StatusCode, HeaderMap, Value) {
    request(app, "GET", uri, &[], None).await
}

async fn delete(app: &Router, uri: &str) -> (StatusCode, HeaderMap, Value) {
    request(app, "DELETE", uri, &[], None).await
}

fn etag_of(headers: &HeaderMap) -> Option<String> {
    headers
        .get(header::ETAG)
        .map(|v| v.to_str().expect("etag ascii").to_string())
}

fn expose_of(headers: &HeaderMap) -> Option<String> {
    headers
        .get(header::ACCESS_CONTROL_EXPOSE_HEADERS)
        .map(|v| v.to_str().expect("ascii").to_string())
}

/// Parse the numeric revision out of an `"o-<n>"` overlay ETag token.
fn overlay_rev(tok: &str) -> i64 {
    tok.trim_matches('"')
        .strip_prefix("o-")
        .expect("o- prefixed token")
        .parse()
        .expect("revision is an i64")
}

fn body_for(id: &str, name: &str) -> Value {
    json!({
        "id": id,
        "name": name,
        "type": "Microsoft.Storage/storageAccounts",
        "location": "eastus",
        "tags": { "env": "user" },
        "properties": { "sku": "Standard_LRS" }
    })
}

// --------------------------------------------------------------------------------------- //
// RED anchor: PUT to an absent id creates (201) with server-injected identity + ETag.
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn put_absent_creates_201_with_injected_identity_and_etag() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-1").await;
    let app = writes_enabled_router(pool);
    let id = res_id("rg-1", "acct-a");

    let (status, headers, body) = put_json(&app, &id, &body_for(&id, "acct-a")).await;
    assert_eq!(status, StatusCode::CREATED, "PUT to an absent id → 201");
    assert_eq!(body["id"], id, "server injects id from the URL");
    assert_eq!(body["name"], "acct-a");
    assert_eq!(body["type"], "Microsoft.Storage/storageAccounts");
    assert_eq!(
        body["properties"]["provisioningState"], "Succeeded",
        "provisioningState is always Succeeded (D-02)"
    );
    let tok = etag_of(&headers).expect("a create carries an ETag header");
    assert!(
        tok.starts_with("\"o-"),
        "created overlay id → o-<rev>: {tok}"
    );

    // GET reflects the create.
    let (gs, gh, gb) = get(&app, &id).await;
    assert_eq!(gs, StatusCode::OK);
    assert_eq!(gb["properties"]["sku"], "Standard_LRS");
    assert_eq!(etag_of(&gh), Some(tok), "GET ETag equals the create ETag");
}

#[tokio::test]
async fn put_existing_replaces_200_and_advances_etag() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-1").await;
    let app = writes_enabled_router(pool);
    let id = res_id("rg-1", "acct-b");

    let (_s, h1, _b) = put_json(&app, &id, &body_for(&id, "acct-b")).await;
    let rev1 = overlay_rev(&etag_of(&h1).unwrap());

    let mut second = body_for(&id, "acct-b");
    second["tags"] = json!({ "env": "replaced" });
    let (status, h2, body) = put_json(&app, &id, &second).await;
    assert_eq!(
        status,
        StatusCode::OK,
        "PUT over an existing id → 200 replace"
    );
    assert_eq!(body["tags"]["env"], "replaced");
    let rev2 = overlay_rev(&etag_of(&h2).unwrap());
    assert!(rev2 > rev1, "the replace advances the revision/ETag");
}

// --------------------------------------------------------------------------------------- //
// PATCH two-level merge (LIFE-02) — absent top-level key preserved, properties merges deep.
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn patch_two_level_merge_preserves_absent_keys() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-1").await;
    let app = writes_enabled_router(pool);
    let id = res_id("rg-1", "acct-c");

    // Create with a kind + a two-key properties object.
    let mut create = body_for(&id, "acct-c");
    create["kind"] = json!("StorageV2");
    create["properties"] = json!({ "x": 0, "y": 9 });
    let _ = put_json(&app, &id, &create).await;

    // PATCH only tags + properties.x. kind and properties.y must survive.
    let (status, headers, body) = patch_json(
        &app,
        &id,
        &json!({ "tags": { "env": "patched" }, "properties": { "x": 1 } }),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "PATCH → 200");
    assert_eq!(body["tags"]["env"], "patched", "tags replaced wholesale");
    assert_eq!(body["properties"]["x"], 1, "properties.x overwritten");
    assert_eq!(
        body["properties"]["y"], 9,
        "properties.y preserved (deep merge)"
    );
    assert_eq!(
        body["kind"], "StorageV2",
        "absent top-level key kind preserved"
    );
    assert!(etag_of(&headers).is_some(), "PATCH carries the new ETag");
}

// --------------------------------------------------------------------------------------- //
// DELETE lifecycle (LIFE-03) + resurrection (STATE-03) + D-24 tombstone ETag.
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn delete_204_then_get_404_and_resurrect() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-1").await;
    let app = writes_enabled_router(pool);
    let id = res_id("rg-1", "acct-d");

    let (_s, ch, _b) = put_json(&app, &id, &body_for(&id, "acct-d")).await;
    let create_rev = overlay_rev(&etag_of(&ch).unwrap());

    // DELETE → 204, empty body, WITH a strictly-newer o- ETag (D-24).
    let (ds, dh, db) = delete(&app, &id).await;
    assert_eq!(ds, StatusCode::NO_CONTENT, "DELETE → 204");
    assert_eq!(db, Value::Null, "204 carries no body");
    let del_tok = etag_of(&dh).expect("the 204 carries the tombstone ETag (D-24)");
    assert!(
        overlay_rev(&del_tok) > create_rev,
        "the DELETE ETag is strictly newer than the pre-delete ETag"
    );

    // GET-after-delete → true 404.
    let (gs, _gh, _gb) = get(&app, &id).await;
    assert_eq!(gs, StatusCode::NOT_FOUND, "GET after delete → 404");

    // PUT resurrects via the create path → 201 (STATE-03).
    let (rs, _rh, _rb) = put_json(&app, &id, &body_for(&id, "acct-d")).await;
    assert_eq!(rs, StatusCode::CREATED, "PUT after delete resurrects → 201");
}

#[tokio::test]
async fn delete_is_idempotent_and_each_carries_an_etag() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-1").await;
    let app = writes_enabled_router(pool);
    let id = res_id("rg-1", "acct-e");
    let _ = put_json(&app, &id, &body_for(&id, "acct-e")).await;

    let (s1, h1, _b) = delete(&app, &id).await;
    let (s2, h2, _b) = delete(&app, &id).await;
    assert_eq!(s1, StatusCode::NO_CONTENT);
    assert_eq!(s2, StatusCode::NO_CONTENT, "repeated DELETE → 204");
    let r1 = overlay_rev(&etag_of(&h1).unwrap());
    let r2 = overlay_rev(&etag_of(&h2).unwrap());
    assert!(
        r2 > r1,
        "a repeated DELETE writes a fresh advancing tombstone"
    );

    // DELETE of a never-existed id → 204 with an o- ETag (the tombstone allocated a revision).
    let never = res_id("rg-1", "never-was");
    let (sn, hn, _b) = delete(&app, &never).await;
    assert_eq!(sn, StatusCode::NO_CONTENT, "DELETE of never-existed → 204");
    assert!(
        etag_of(&hn).map(|t| t.starts_with("\"o-")).unwrap_or(false),
        "DELETE-of-never-existed still carries an o- ETag"
    );
}

// --------------------------------------------------------------------------------------- //
// LIFE-04: list + $filter reflect create/delete. DB: overlay row source='user'.
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn list_and_filter_reflect_writes_and_overlay_is_user_sourced() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-1").await;
    let app = writes_enabled_router(pool.clone());
    let id = res_id("rg-1", "acct-f");
    let _ = put_json(&app, &id, &body_for(&id, "acct-f")).await;

    let list_uri = format!("/subscriptions/{SUB}/resources");
    let filter_uri =
        format!("{list_uri}?$filter=resourceType%20eq%20%27Microsoft.Storage/storageAccounts%27");

    let (_s, _h, list) = get(&app, &list_uri).await;
    assert!(
        list["value"]
            .as_array()
            .unwrap()
            .iter()
            .any(|r| r["id"] == json!(id)),
        "a created resource appears in the list (LIFE-04)"
    );
    let (_s, _h, filtered) = get(&app, &filter_uri).await;
    assert!(
        filtered["value"]
            .as_array()
            .unwrap()
            .iter()
            .any(|r| r["id"] == json!(id)),
        "a created resource appears in a $filter result (LIFE-04)"
    );

    // DB: the overlay row is source='user', present=true.
    let (source, present): (String, bool) = sqlx::query_as(
        "SELECT source, present FROM synthetic.arm_overlay WHERE id_lower = lower($1)",
    )
    .bind(&id)
    .fetch_one(&pool)
    .await
    .expect("overlay row exists after create");
    assert_eq!(source, "user", "a user create writes source='user'");
    assert!(present, "a create is present=true");

    // After delete: absent from list + $filter, and present=false in the DB.
    let _ = delete(&app, &id).await;
    let (_s, _h, list2) = get(&app, &list_uri).await;
    assert!(
        !list2["value"]
            .as_array()
            .unwrap()
            .iter()
            .any(|r| r["id"] == json!(id)),
        "a deleted resource is absent from the list"
    );
    let present_after: bool =
        sqlx::query_scalar("SELECT present FROM synthetic.arm_overlay WHERE id_lower = lower($1)")
            .bind(&id)
            .fetch_one(&pool)
            .await
            .expect("tombstone row exists");
    assert!(!present_after, "a delete writes present=false");
}

// --------------------------------------------------------------------------------------- //
// SYNC-01/02: terminal statuses, no async/LRO headers, never a 202.
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn writes_are_synchronous_with_no_async_headers() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-1").await;
    let app = writes_enabled_router(pool);
    let id = res_id("rg-1", "acct-g");

    for (status, headers) in [
        {
            let (s, h, _b) = put_json(&app, &id, &body_for(&id, "acct-g")).await;
            (s, h)
        },
        {
            let (s, h, _b) = patch_json(&app, &id, &json!({ "tags": { "k": "v" } })).await;
            (s, h)
        },
        {
            let (s, h, _b) = delete(&app, &id).await;
            (s, h)
        },
    ] {
        assert_ne!(status, StatusCode::ACCEPTED, "no 202/LRO (SYNC-02)");
        assert!(status.is_success(), "terminal 2xx: {status}");
        for async_header in ["azure-asyncoperation", "location", "operation"] {
            assert!(
                !headers.contains_key(async_header),
                "no async header {async_header} (SYNC-01)"
            );
        }
    }
}

// --------------------------------------------------------------------------------------- //
// D-25: Access-Control-Expose-Headers: ETag on GET + every mutation; absent on a 405.
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn expose_headers_rides_every_etag_bearing_response() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-1").await;
    let app = writes_enabled_router(pool);
    let id = res_id("rg-1", "acct-h");

    let (_s, ph, _b) = put_json(&app, &id, &body_for(&id, "acct-h")).await;
    assert_eq!(expose_of(&ph).as_deref(), Some("ETag"), "PUT exposes ETag");

    let (_s, gh, _b) = get(&app, &id).await;
    assert_eq!(expose_of(&gh).as_deref(), Some("ETag"), "GET exposes ETag");

    let (_s, ah, _b) = patch_json(&app, &id, &json!({ "tags": { "k": "v" } })).await;
    assert_eq!(
        expose_of(&ah).as_deref(),
        Some("ETag"),
        "PATCH exposes ETag"
    );

    let (_s, dh, _b) = delete(&app, &id).await;
    assert_eq!(
        expose_of(&dh).as_deref(),
        Some("ETag"),
        "DELETE 204 exposes ETag"
    );
}

// --------------------------------------------------------------------------------------- //
// D-14 location default / explicit-null.
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn location_defaults_to_global_and_explicit_null_is_400() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-1").await;
    let app = writes_enabled_router(pool);

    // Omit location → 201 and stored/echoed location == "global".
    let id = res_id("rg-1", "acct-i");
    let body = json!({ "properties": {}, "tags": {} });
    let (s, _h, b) = put_json(&app, &id, &body).await;
    assert_eq!(s, StatusCode::CREATED);
    assert_eq!(b["location"], "global", "absent location → global (D-14)");
    let (_gs, _gh, gb) = get(&app, &id).await;
    assert_eq!(gb["location"], "global", "GET also reports global");

    // Explicit location: null → 400 LocationRequired.
    let id2 = res_id("rg-1", "acct-j");
    let (s2, _h2, _b2) = put_json(
        &app,
        &id2,
        &json!({ "location": null, "properties": {}, "tags": {} }),
    )
    .await;
    assert_eq!(s2, StatusCode::BAD_REQUEST, "explicit location:null → 400");
}

// --------------------------------------------------------------------------------------- //
// D-03 identity conflict.
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn identity_conflict_is_400_matching_is_accepted() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-1").await;
    let app = writes_enabled_router(pool);
    let id = res_id("rg-1", "acct-k");

    // Body type names a DIFFERENT resource than the route → 400.
    let mut conflict = body_for(&id, "acct-k");
    conflict["type"] = json!("Microsoft.Compute/virtualMachines");
    let (s, _h, _b) = put_json(&app, &id, &conflict).await;
    assert_eq!(
        s,
        StatusCode::BAD_REQUEST,
        "conflicting body type → 400 (D-03)"
    );

    // Case-insensitively MATCHING identity is accepted (echoes canonical casing).
    let mut matching = body_for(&id, "acct-k");
    matching["type"] = json!("microsoft.storage/storageaccounts");
    let (s2, _h2, b2) = put_json(&app, &id, &matching).await;
    assert_eq!(
        s2,
        StatusCode::CREATED,
        "case-insensitively matching type accepted"
    );
    assert_eq!(
        b2["type"], "Microsoft.Storage/storageAccounts",
        "echo keeps the URL canonical casing"
    );
}

// --------------------------------------------------------------------------------------- //
// D-16 malformed / non-object / wrong Content-Type (writes ENABLED) → ARM 400.
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn malformed_bodies_are_arm_400_when_enabled() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-1").await;
    let app = writes_enabled_router(pool);
    let id = res_id("rg-1", "acct-l");

    // Non-JSON body.
    let (s, _h, b) = request(
        &app,
        "PUT",
        &id,
        &[("Content-Type", "application/json")],
        Some(b"not json {".to_vec()),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_REQUEST, "non-JSON body → 400");
    assert_eq!(b["error"]["code"], "InvalidRequestContent", "ARM envelope");

    // JSON array (non-object) body.
    let (sa, _h, _b) = put_json(&app, &id, &json!([1, 2, 3])).await;
    assert_eq!(sa, StatusCode::BAD_REQUEST, "array body → 400");

    // JSON scalar body.
    let (ss, _h, _b) = put_json(&app, &id, &json!(42)).await;
    assert_eq!(ss, StatusCode::BAD_REQUEST, "scalar body → 400");

    // Wrong Content-Type (a valid JSON object, but text/plain).
    let (sc, _h, _b) = request(
        &app,
        "PUT",
        &id,
        &[("Content-Type", "text/plain")],
        Some(serde_json::to_vec(&body_for(&id, "acct-l")).unwrap()),
    )
    .await;
    assert_eq!(sc, StatusCode::BAD_REQUEST, "wrong Content-Type → 400");
}

// --------------------------------------------------------------------------------------- //
// D-21 structural type mismatch → 400, no overlay row (never a 500).
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn structural_type_mismatch_is_400_and_writes_nothing() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-1").await;
    let app = writes_enabled_router(pool.clone());
    let id = res_id("rg-1", "acct-m");

    let cases = [
        json!({ "properties": [] }),
        json!({ "tags": "x" }),
        json!({ "location": 42 }),
        json!({ "sku": [] }),
        json!({ "kind": 5 }),
    ];
    for case in cases {
        let (s, _h, _b) = put_json(&app, &id, &case).await;
        assert_eq!(s, StatusCode::BAD_REQUEST, "type mismatch → 400: {case}");
    }
    // No overlay row was ever materialized (never reached the CHECK layer as a 500).
    let count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM synthetic.arm_overlay WHERE id_lower = lower($1)")
            .bind(&id)
            .fetch_one(&pool)
            .await
            .expect("count");
    assert_eq!(count, 0, "a rejected write leaves no overlay row");
}

// --------------------------------------------------------------------------------------- //
// D-17 opaque top-level keys survive a create → GET round-trip.
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn opaque_keys_survive_create_then_get() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-1").await;
    let app = writes_enabled_router(pool);
    let id = res_id("rg-1", "acct-n");

    let mut body = body_for(&id, "acct-n");
    body["identity"] = json!({ "type": "SystemAssigned" });
    body["zones"] = json!(["1", "2"]);
    body["plan"] = json!({ "name": "free" });
    let (s, _h, _b) = put_json(&app, &id, &body).await;
    assert_eq!(s, StatusCode::CREATED);

    // The DETAIL GET must return the opaque keys verbatim (D-17 full-body read).
    let (gs, _gh, gb) = get(&app, &id).await;
    assert_eq!(gs, StatusCode::OK);
    assert_eq!(
        gb["identity"]["type"], "SystemAssigned",
        "identity survives GET"
    );
    assert_eq!(gb["zones"], json!(["1", "2"]), "zones survives GET");
    assert_eq!(gb["plan"]["name"], "free", "plan survives GET");
}

// --------------------------------------------------------------------------------------- //
// D-19 baseline casing retained on a differently-cased write over a baseline row.
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn differently_cased_put_over_baseline_keeps_baseline_casing() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-1").await;
    // Baseline stores canonical mixed casing.
    let canonical_id = res_id("rg-1", "MixedCaseAcct");
    seed_baseline_resource(
        &pool,
        &canonical_id,
        "MixedCaseAcct",
        "Microsoft.Storage/storageAccounts",
    )
    .await;
    let app = writes_enabled_router(pool);

    // PUT over the SAME id_lower via a differently-cased resource-NAME segment (the fixed
    // `resourceGroups`/`providers` route literals stay canonical — axum matches those
    // case-sensitively; ARM case-insensitivity applies to the resource identifier).
    let lower_id = res_id("rg-1", "mixedcaseacct");
    let (s, _h, b) = put_json(&app, &lower_id, &json!({ "properties": {}, "tags": {} })).await;
    assert_eq!(s, StatusCode::OK, "write over a baseline row → 200 replace");
    assert_eq!(
        b["id"], canonical_id,
        "echo keeps the BASELINE id casing (D-19)"
    );
    assert_eq!(
        b["name"], "MixedCaseAcct",
        "echo keeps the BASELINE name casing"
    );

    // GET (via the canonical id) also reports the baseline casing.
    let (_gs, _gh, gb) = get(&app, &canonical_id).await;
    assert_eq!(gb["id"], canonical_id, "GET keeps the baseline casing");
}

// --------------------------------------------------------------------------------------- //
// D-20 PATCH never creates: absent → 404, tombstoned → 404.
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn patch_absent_or_tombstoned_is_404() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-1").await;
    let app = writes_enabled_router(pool.clone());

    // Never existed → 404.
    let absent = res_id("rg-1", "acct-never");
    let (s, _h, _b) = patch_json(&app, &absent, &json!({ "tags": { "k": "v" } })).await;
    assert_eq!(
        s,
        StatusCode::NOT_FOUND,
        "PATCH on an absent id → 404 (D-20)"
    );

    // Create then delete → tombstoned → 404.
    let id = res_id("rg-1", "acct-o");
    let _ = put_json(&app, &id, &body_for(&id, "acct-o")).await;
    let _ = delete(&app, &id).await;
    let (s2, _h2, _b2) = patch_json(&app, &id, &json!({ "tags": { "k": "v" } })).await;
    assert_eq!(
        s2,
        StatusCode::NOT_FOUND,
        "PATCH on a tombstoned id → 404 (D-20)"
    );

    // No overlay body was materialized from the empty base (the tombstone stays present=false).
    let present: bool =
        sqlx::query_scalar("SELECT present FROM synthetic.arm_overlay WHERE id_lower = lower($1)")
            .bind(&id)
            .fetch_one(&pool)
            .await
            .expect("tombstone row");
    assert!(!present, "PATCH did not resurrect the tombstone");
}
