//! ARM write-plane AUTH proofs (WAUTH-02 / D-07 / D-11) — full HTTP round-trips through the
//! real `build_router` with `enable_arm_writes: true`.
//!
//! Writes inherit the READ auth posture, because the write handlers register INSIDE the same
//! `bearer_auth`-layered router as the reads. This suite proves it end to end:
//!   (a) any-Bearer default — a write with NO Authorization → 401; any non-empty Bearer →
//!       proceeds to the write (201);
//!   (b) `--enforce-auth` — an invalid/expired JWT → 401; a valid minted JWT → success. Reads
//!       and writes share the SAME RS256 validation;
//!   (c) authz-before-precondition (D-07/D-11) — an unauthenticated write that ALSO carries an
//!       If-Match that would fail returns 401 (auth), NOT 412 (precondition), AND writes NO
//!       overlay row. The bearer layer runs before the handler evaluates the precondition or
//!       mutates.
//!
//! Harness mirrors `write_lifecycle.rs`: an ephemeral testcontainers Postgres, the bare
//! first-boot overlay substrate via `common::seed_overlay_first_boot`, the real `build_router`,
//! and `tower::ServiceExt::oneshot`. DB-gated: requires Docker/PG16; validates on the Linux CI
//! gate.

mod common;

use axum::{
    Router,
    body::Body,
    http::{HeaderMap, Request, StatusCode},
};
use serde::Serialize;
use serde_json::{Value, json};
use sqlx::Executor;
use sqlx::PgPool;
use std::time::{SystemTime, UNIX_EPOCH};
use tenantless_server::{
    build_router,
    jwt::{JwtSigner, SharedSigner},
    metrics::Metrics,
    state::AppState,
};
use testcontainers_modules::{postgres, testcontainers::runners::AsyncRunner};
use tower::ServiceExt;

const SUB: &str = "11111111-1111-1111-1111-111111111111";
const TENANT: uuid::Uuid = uuid::Uuid::from_u128(0x5757_5757_5757_5757_5757_5757_5757_5757);

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

/// A writes-armed router sharing `signer` with `AppState` (so a token minted from the SAME
/// signer validates under `--enforce-auth`). `enforce_auth` toggles the read/write auth posture.
fn writes_router(pool: PgPool, signer: SharedSigner, enforce_auth: bool) -> Router {
    build_router(AppState {
        pool,
        base_url: "http://test".to_string(),
        metrics: Metrics::new(),
        signer,
        enforce_auth,
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

fn body_for(id: &str, name: &str) -> Value {
    json!({
        "id": id,
        "name": name,
        "type": "Microsoft.Storage/storageAccounts",
        "location": "eastus",
        "tags": { "env": "user" },
        "properties": {}
    })
}

/// v1.0-shaped ARM claims. `iss`/`aud` are taken from the signer so the token validates against
/// the run's own JWKS under `--enforce-auth` (issuer/audience/expiry).
#[derive(Serialize)]
struct Claims {
    iss: String,
    aud: String,
    tid: String,
    sub: String,
    exp: usize,
}

fn future_exp() -> usize {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs();
    (now + 3600) as usize
}

/// Mint a valid RS256 token from `signer` (the one `AppState` also holds).
fn mint_valid(signer: &SharedSigner) -> String {
    let s = signer.load();
    let claims = Claims {
        iss: s.issuer.clone(),
        aud: s.audience.clone(),
        tid: TENANT.to_string(),
        sub: "spn-write-test".to_string(),
        exp: future_exp(),
    };
    s.mint(&claims).expect("mint valid token")
}

/// Drive one PUT with an OPTIONAL `Authorization` header and OPTIONAL `If-Match`.
async fn put_with(
    app: &Router,
    uri: &str,
    authorization: Option<&str>,
    if_match: Option<&str>,
    body: &Value,
) -> (StatusCode, HeaderMap, Value) {
    let mut builder = Request::builder()
        .method("PUT")
        .uri(uri)
        .header("Content-Type", "application/json");
    if let Some(a) = authorization {
        builder = builder.header("Authorization", a);
    }
    if let Some(im) = if_match {
        builder = builder.header("If-Match", im);
    }
    let req = builder
        .body(Body::from(serde_json::to_vec(body).unwrap()))
        .expect("build request");
    let resp = app.clone().oneshot(req).await.expect("oneshot");
    let status = resp.status();
    let headers = resp.headers().clone();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .expect("collect body");
    let jsonv = if bytes.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(&bytes).unwrap_or(Value::Null)
    };
    (status, headers, jsonv)
}

async fn overlay_row_count(pool: &PgPool, id: &str) -> i64 {
    sqlx::query_scalar("SELECT count(*) FROM synthetic.arm_overlay WHERE id_lower = lower($1)")
        .bind(id)
        .fetch_one(pool)
        .await
        .expect("count overlay rows")
}

// --------------------------------------------------------------------------------------- //
// (a) any-Bearer default: no Authorization → 401; any non-empty Bearer → proceeds (201).
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn write_without_authorization_is_401() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool).await;
    let id = res_id("acct-noauth");
    let app = writes_router(pool.clone(), common::test_signer(), false);

    let (status, _h, body) = put_with(&app, &id, None, None, &body_for(&id, "acct-noauth")).await;
    assert_eq!(
        status,
        StatusCode::UNAUTHORIZED,
        "a write with no Authorization is a 401 (any-Bearer default), exactly like a read"
    );
    assert_eq!(body["error"]["code"], "MissingAuthenticationToken");
    // The bearer layer ran before the handler: nothing was written.
    assert_eq!(
        overlay_row_count(&pool, &id).await,
        0,
        "a 401'd write must not write an overlay row"
    );
}

#[tokio::test]
async fn write_with_any_bearer_proceeds_to_create() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool).await;
    let id = res_id("acct-anybearer");
    let app = writes_router(pool, common::test_signer(), false);

    let (status, _h, body) = put_with(
        &app,
        &id,
        Some("Bearer any-nonempty-token"),
        None,
        &body_for(&id, "acct-anybearer"),
    )
    .await;
    assert_eq!(
        status,
        StatusCode::CREATED,
        "any non-empty Bearer proceeds to the write (201) — writes inherit the read posture"
    );
    assert_eq!(body["id"], id);
}

// --------------------------------------------------------------------------------------- //
// (b) --enforce-auth: invalid JWT → 401; a valid minted JWT → success. Same policy as reads.
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn enforce_auth_write_with_invalid_jwt_is_401() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool).await;
    let id = res_id("acct-badjwt");
    // A fresh signer shared with AppState; the token below is NOT minted from it.
    let signer = SharedSigner::new(JwtSigner::ephemeral(&TENANT).expect("signer"));
    let app = writes_router(pool.clone(), signer, true);

    // A syntactically-plausible but unsigned/garbage token → RS256 validation fails.
    let (status, _h, body) = put_with(
        &app,
        &id,
        Some("Bearer not.a.validjwt"),
        None,
        &body_for(&id, "acct-badjwt"),
    )
    .await;
    assert_eq!(
        status,
        StatusCode::UNAUTHORIZED,
        "under --enforce-auth an invalid JWT is a 401 for a write, exactly like a read"
    );
    assert_eq!(body["error"]["code"], "InvalidAuthenticationToken");
    assert_eq!(
        overlay_row_count(&pool, &id).await,
        0,
        "a 401'd write must not write an overlay row"
    );
}

#[tokio::test]
async fn enforce_auth_write_with_valid_jwt_succeeds() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool).await;
    let id = res_id("acct-goodjwt");
    let signer = SharedSigner::new(JwtSigner::ephemeral(&TENANT).expect("signer"));
    let token = mint_valid(&signer);
    let app = writes_router(pool, signer, true);

    let (status, _h, body) = put_with(
        &app,
        &id,
        Some(&format!("Bearer {token}")),
        None,
        &body_for(&id, "acct-goodjwt"),
    )
    .await;
    assert_eq!(
        status,
        StatusCode::CREATED,
        "a valid minted JWT lets the write through (201) under --enforce-auth"
    );
    assert_eq!(body["id"], id);
}

// --------------------------------------------------------------------------------------- //
// (c) authz precedes precondition + mutation (D-07/D-11): an unauthenticated write that would
//     ALSO fail an If-Match returns 401 (auth), NOT 412, and writes no overlay row.
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn unauthenticated_write_with_failing_if_match_is_401_not_412() {
    let (pool, _c) = start_pg().await;
    seed_reads_first_boot(&pool).await;
    seed_scope(&pool).await;
    let id = res_id("acct-order");
    let app = writes_router(pool.clone(), common::test_signer(), false);

    // No Authorization header AND an If-Match that could never match an absent resource. If the
    // handler evaluated the precondition first this would be a 412; because authorization runs
    // in the layer BEFORE the handler, it is a 401.
    let (status, _h, body) = put_with(
        &app,
        &id,
        None,
        Some("\"o-999999\""),
        &body_for(&id, "acct-order"),
    )
    .await;
    assert_eq!(
        status,
        StatusCode::UNAUTHORIZED,
        "authorization precedes the If-Match precondition — 401, never 412 (D-07/D-11)"
    );
    assert_ne!(
        status,
        StatusCode::PRECONDITION_FAILED,
        "the precondition must not be reached on an unauthenticated request"
    );
    assert_eq!(body["error"]["code"], "MissingAuthenticationToken");
    assert_eq!(
        overlay_row_count(&pool, &id).await,
        0,
        "authz precedes mutation — no overlay row written for a 401'd write"
    );
}
