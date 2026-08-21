//! Generic write-plane ROUTING acceptance (writes ENABLED) — LIFE-06/07/08.
//!
//! These lock the routing behaviors the generic parsed-id write path already provides (Plan
//! 03), exercised end-to-end through the real `build_router`:
//!   * **LIFE-06 nested** — a PUT to a DEEP nested id (`.../Microsoft.Sql/servers/s1/databases/
//!     d1`) creates (`201`) and a GET round-trips the SAME nested id (the provider-onward
//!     catch-all reconstructs it; no per-depth special handling).
//!   * **LIFE-07 case-insensitive + canonical echo (D-08 first-write-wins)** — a first write in
//!     one casing then a DIFFERENT-case write to the same `id_lower` resolves to the SAME
//!     resource (a `200` replace, a single overlay row) AND the echoed `id`/`type`/`name` keep
//!     the FIRST-write casing; GET/DELETE match case-insensitively and never alter the casing.
//!   * **LIFE-08 opaque** — an uncatalogued type with arbitrary extra top-level keys
//!     (`identity`/`zones`/`plan`) completes the FULL generic lifecycle (PUT `201` → GET echoes
//!     the opaque keys verbatim → PATCH `tags` preserves them → DELETE `204` → GET `404`) with
//!     no typed model, and the raw overlay row stores the full submitted body.
//!
//! Harness mirrors `write_lifecycle.rs`/`etag_header.rs`: ephemeral testcontainers Postgres,
//! bare-first-boot overlay substrate, real router, `oneshot`. DB-gated (Docker/PG16).

mod common;

use axum::{
    Router,
    body::Body,
    http::{HeaderMap, Request, StatusCode},
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

// --------------------------------------------------------------------------------------- //
// LIFE-06: a DEEP nested id round-trips (PUT → GET) via the parsed-id catch-all.
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn nested_id_round_trips_put_then_get() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-1").await;
    let app = writes_enabled_router(pool);

    let nested = id_for("rg-1", "Microsoft.Sql/servers/s1/databases/d1");
    let (ps, _ph, pb) = put_json(&app, &nested, &json!({ "properties": {}, "tags": {} })).await;
    assert_eq!(ps, StatusCode::CREATED, "PUT to a deep nested id → 201");
    assert_eq!(pb["id"], nested, "the create echoes the nested id verbatim");
    assert_eq!(
        pb["name"], "s1/d1",
        "name parses as the nested type/name pairs' names"
    );
    assert_eq!(
        pb["type"], "Microsoft.Sql/servers/databases",
        "type parses as namespace + type pairs to depth (LIFE-06)"
    );

    // GET round-trips the SAME nested id (no per-depth special handling).
    let (gs, _gh, gb) = get(&app, &nested).await;
    assert_eq!(gs, StatusCode::OK, "GET the nested id → 200");
    assert_eq!(gb["id"], nested, "GET round-trips the nested id (LIFE-06)");
    assert_eq!(gb["type"], "Microsoft.Sql/servers/databases");
}

// --------------------------------------------------------------------------------------- //
// LIFE-07: a differently-cased second write hits the SAME resource; first-write casing wins.
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn case_insensitive_second_write_keeps_first_write_casing() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-1").await;
    let app = writes_enabled_router(pool.clone());

    // FIRST write with a distinctive mixed casing in BOTH the type-tail and the name segments.
    let first = id_for("rg-1", "Microsoft.Storage/StorageAccounts/MyAcct");
    let (fs, _fh, fb) = put_json(&app, &first, &json!({ "properties": {}, "tags": {} })).await;
    assert_eq!(fs, StatusCode::CREATED, "first write → 201");
    assert_eq!(fb["id"], first, "first write echoes its own casing");
    assert_eq!(fb["type"], "Microsoft.Storage/StorageAccounts");
    assert_eq!(fb["name"], "MyAcct");

    // SECOND write to the SAME id_lower via a DIFFERENT (all-lower) casing → 200 replace, and
    // the echoed id/type/name STAY the first-write casing (D-08 first-write-wins).
    let lowered = id_for("rg-1", "Microsoft.Storage/storageaccounts/myacct");
    let (ss, _sh, sb) = put_json(&app, &lowered, &json!({ "properties": {}, "tags": {} })).await;
    assert_eq!(
        ss,
        StatusCode::OK,
        "a different-case write to the same id_lower → 200 replace (same resource)"
    );
    assert_eq!(
        sb["id"], first,
        "echo keeps the FIRST-write id casing (D-08)"
    );
    assert_eq!(
        sb["type"], "Microsoft.Storage/StorageAccounts",
        "echo keeps the first-write type casing"
    );
    assert_eq!(
        sb["name"], "MyAcct",
        "echo keeps the first-write name casing"
    );

    // Exactly ONE overlay row backs both writes (not a second row).
    let rows: i64 =
        sqlx::query_scalar("SELECT count(*) FROM synthetic.arm_overlay WHERE id_lower = lower($1)")
            .bind(&first)
            .fetch_one(&pool)
            .await
            .expect("count");
    assert_eq!(
        rows, 1,
        "a different-case write resolves to the SAME single row"
    );

    // GET via the lowercased id still returns the resource with first-write casing …
    let (gs, _gh, gb) = get(&app, &lowered).await;
    assert_eq!(gs, StatusCode::OK, "GET matches case-insensitively");
    assert_eq!(gb["id"], first, "GET never alters the stored casing");

    // … and DELETE matches case-insensitively too (tombstones the same row).
    assert_eq!(delete(&app, &lowered).await.0, StatusCode::NO_CONTENT);
    assert_eq!(
        get(&app, &first).await.0,
        StatusCode::NOT_FOUND,
        "case-insensitive DELETE tombstones the shared resource"
    );
}

// --------------------------------------------------------------------------------------- //
// LIFE-08: opaque uncatalogued type — full lifecycle + opaque keys survive verbatim.
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn opaque_uncatalogued_type_full_lifecycle_preserves_extra_keys() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-1").await;
    let app = writes_enabled_router(pool.clone());

    // An UNCATALOGUED provider/type (no typed model, not in `casing::canonical_type`).
    let id = id_for("rg-1", "Contoso.Widgets/gadgets/gizmo1");
    let create = json!({
        "location": "eastus",
        "tags": { "env": "opaque" },
        "properties": { "spin": 3 },
        "identity": { "type": "SystemAssigned" },
        "zones": ["1", "2"],
        "plan": { "name": "free", "publisher": "contoso" }
    });
    let (cs, _ch, cb) = put_json(&app, &id, &create).await;
    assert_eq!(cs, StatusCode::CREATED, "opaque create → 201");
    assert_eq!(
        cb["type"], "Contoso.Widgets/gadgets",
        "an uncatalogued type echoes verbatim (no canonicalization)"
    );

    // GET echoes the opaque keys VERBATIM (D-17 full-body read for overlay rows).
    let (gs, _gh, gb) = get(&app, &id).await;
    assert_eq!(gs, StatusCode::OK);
    assert_eq!(
        gb["identity"]["type"], "SystemAssigned",
        "identity survives"
    );
    assert_eq!(gb["zones"], json!(["1", "2"]), "zones survives");
    assert_eq!(gb["plan"]["publisher"], "contoso", "plan survives");
    assert_eq!(
        gb["properties"]["provisioningState"], "Succeeded",
        "server-owned provisioningState is still forced (D-02)"
    );

    // The RAW overlay row stores the full submitted opaque body.
    let stored: Value =
        sqlx::query_scalar("SELECT body FROM synthetic.arm_overlay WHERE id_lower = lower($1)")
            .bind(&id)
            .fetch_one(&pool)
            .await
            .expect("overlay body");
    assert_eq!(
        stored["identity"]["type"], "SystemAssigned",
        "the overlay row persists the full opaque body"
    );
    assert_eq!(stored["zones"], json!(["1", "2"]));

    // PATCH of `tags` PRESERVES the opaque top-level keys (two-level merge keeps absent keys).
    let (as_, _ah, ab) = patch_json(&app, &id, &json!({ "tags": { "env": "patched" } })).await;
    assert_eq!(as_, StatusCode::OK, "opaque PATCH → 200");
    assert_eq!(ab["tags"]["env"], "patched", "tags replaced");
    assert_eq!(
        ab["identity"]["type"], "SystemAssigned",
        "identity preserved across a tags PATCH (LIFE-08)"
    );
    assert_eq!(
        ab["zones"],
        json!(["1", "2"]),
        "zones preserved across PATCH"
    );
    assert_eq!(ab["plan"]["name"], "free", "plan preserved across PATCH");

    // DELETE → 204, then GET → 404 (full generic lifecycle on an opaque type).
    assert_eq!(delete(&app, &id).await.0, StatusCode::NO_CONTENT);
    assert_eq!(
        get(&app, &id).await.0,
        StatusCode::NOT_FOUND,
        "opaque DELETE tombstones → GET 404 (full lifecycle)"
    );
}

// --------------------------------------------------------------------------------------- //
// A KNOWN type written through a NON-canonically-cased URL returns the SAME `type` on the
// detail GET and in the subscription list. The write echo may reflect the submitted casing,
// but the READ paths must agree (both canonicalize per MOCK-12) — never disagree.
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn detail_and_list_agree_on_type_casing() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool, "rg-1").await;
    let app = writes_enabled_router(pool);

    // finalize_body stores the URL casing verbatim in the overlay body; the list projection
    // canonicalizes it (`From<ResourceRow>`, MOCK-12), so the detail GET must canonicalize too
    // or the two views disagree on `type`.
    let id = id_for("rg-1", "Microsoft.Storage/STORAGEACCOUNTS/acct7");
    let (cs, _ch, _cb) = put_json(&app, &id, &json!({ "properties": {}, "tags": {} })).await;
    assert_eq!(cs, StatusCode::CREATED);

    let (ds, _dh, db) = get(&app, &id).await;
    assert_eq!(ds, StatusCode::OK);
    let detail_type = db["type"].as_str().expect("detail type").to_string();

    let (ls, _lh, lb) = get(&app, &format!("/subscriptions/{SUB}/resources")).await;
    assert_eq!(ls, StatusCode::OK);
    let list_type = lb["value"]
        .as_array()
        .expect("list value array")
        .iter()
        .find(|r| {
            r["id"]
                .as_str()
                .map(|s| s.eq_ignore_ascii_case(&id))
                .unwrap_or(false)
        })
        .expect("the written resource appears in the subscription list")["type"]
        .as_str()
        .expect("list type")
        .to_string();

    assert_eq!(
        detail_type, list_type,
        "detail and list must not disagree on type casing"
    );
    assert_eq!(
        detail_type, "Microsoft.Storage/storageAccounts",
        "both read paths serve the canonical MOCK-12 casing"
    );
}
