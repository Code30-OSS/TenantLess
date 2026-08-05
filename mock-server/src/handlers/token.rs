//! Client-credentials token mint + JWKS + OpenID discovery (IAM-04, D-09/D-11).
//!
//! These three routes are merged OUTSIDE the bearer layer (the `console::router`
//! template — see [`crate::build_router`]): a client MUST be able to fetch a token
//! and the JWKS with NO `Authorization` header, even when `--enforce-auth` is ON —
//! otherwise it could never bootstrap the very token the enforce branch demands
//! (the token-to-get-a-token deadlock, T-10-11).
//!
//! `POST /{tenant}/oauth2/v2.0/token` mints a **v1.0-shaped ARM token** (RESEARCH
//! Q2): `iss = https://sts.windows.net/{tid}/`, `aud = https://management.azure.com/`,
//! signed RS256 with the run's ephemeral key ([`crate::jwt::JwtSigner`]). The
//! `{tenant}` path segment is ACCEPTED at any value but the token is ALWAYS minted
//! with the SERVED tenant_id — the sim has exactly one tenant (D-09 lean = accept).
//! `iat`/`nbf`/`exp` are computed at REQUEST time in the server (Pitfall 6 — never
//! from the seeded generator, which must stay byte-reproducible).
//!
//! The signer carries no raw tenant_id field; the served tid is recovered from the
//! fixed issuer format `https://sts.windows.net/{tid}/` so this module need not touch
//! `jwt.rs`/`state.rs` (their public surface is the contract).

use crate::{error::ApiError, state::AppState};
use axum::{
    Json, Router,
    extract::{Path, State},
    routing::{get, post},
};
use serde::Serialize;
use serde_json::{Value, json};
use std::time::{SystemTime, UNIX_EPOCH};

/// Token lifetime in seconds (1 hour — the canonical ARM access-token TTL).
const EXPIRES_IN: u64 = 3600;

/// The auth-exempt sub-router (cloned from `console::router`). Merged OUTSIDE the
/// bearer + metrics layers in [`crate::build_router`] so `/token` and the JWKS are
/// always reachable with no auth header (D-11).
pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/{tenant}/oauth2/v2.0/token", post(mint_token))
        .route("/{tenant}/discovery/v2.0/keys", get(jwks))
        .route(
            "/{tenant}/v2.0/.well-known/openid-configuration",
            get(openid_configuration),
        )
        .with_state(state)
}

/// The v1.0-shaped ARM JWT claim set (RESEARCH Q2). Both `appid` (v1) and `azp` (v2)
/// are emitted so the token decodes cleanly under either reader.
#[derive(Serialize)]
struct ArmTokenClaims {
    iss: String,
    aud: String,
    tid: String,
    oid: String,
    sub: String,
    appid: String,
    azp: String,
    roles: Vec<String>,
    ver: String,
    iat: u64,
    nbf: u64,
    exp: u64,
}

/// The OAuth token response body (`{access_token, token_type, expires_in}`).
#[derive(Serialize)]
struct TokenResponse {
    access_token: String,
    token_type: &'static str,
    expires_in: u64,
}

/// Recover the served tenant_id from the signer's fixed issuer
/// (`https://sts.windows.net/{tid}/`). The format is server-owned (`jwt.rs`), so this
/// parse is a stable contract, not a guess.
fn served_tid(issuer: &str) -> &str {
    issuer
        .trim_start_matches("https://sts.windows.net/")
        .trim_end_matches('/')
}

/// Current UNIX time in whole seconds (server-mint only — Pitfall 6).
fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// `POST /{tenant}/oauth2/v2.0/token` — mint a v1.0 ARM token. The `{tenant}` segment
/// and any request body are accepted and ignored; the token is minted with the served
/// tenant_id. A per-request service-principal `oid`/`appid` is generated (it need not
/// resolve to a `synthetic.principals` row — IAM-04 requires only a decodable token).
async fn mint_token(
    State(state): State<AppState>,
    Path(_tenant): Path<String>,
) -> Result<Json<TokenResponse>, ApiError> {
    let now = now_secs();
    // Snapshot the CURRENT signer once (the control plane may hot-swap it); this whole token
    // is minted against one coherent identity even if a mutation lands mid-request.
    let signer = state.signer.load();
    let tid = served_tid(&signer.issuer).to_string();
    // A run/request service-principal identity. Generated (not seeded) — the token is
    // an auth artifact, never part of the reproducible generator output.
    let sp_oid = uuid::Uuid::new_v4().to_string();
    let sp_appid = uuid::Uuid::new_v4().to_string();

    let claims = ArmTokenClaims {
        iss: signer.issuer.clone(),
        aud: signer.audience.clone(),
        tid,
        oid: sp_oid.clone(),
        sub: sp_oid,
        appid: sp_appid.clone(),
        azp: sp_appid,
        // Representative synthetic app role; an array is always present (IAM-04 names
        // the `roles` claim). No real-tenant identifier (data boundary).
        roles: vec!["mock-app-role".to_string()],
        ver: "1.0".to_string(),
        iat: now,
        nbf: now,
        exp: now + EXPIRES_IN,
    };

    let access_token = signer
        .mint(&claims)
        .map_err(|e| ApiError::Internal(format!("token mint failed: {e}")))?;

    Ok(Json(TokenResponse {
        access_token,
        token_type: "Bearer",
        expires_in: EXPIRES_IN,
    }))
}

/// `GET /{tenant}/discovery/v2.0/keys` — serve the run's JWKS (public half). `JwkSet`
/// is `Serialize`; cloned out of the `Arc`-shared signer for an owned response value.
async fn jwks(State(state): State<AppState>, Path(_tenant): Path<String>) -> Json<Value> {
    Json(json!(state.signer.load().jwks))
}

/// `GET /{tenant}/v2.0/.well-known/openid-configuration` — a minimal OIDC discovery
/// document pointing at the issuer + the JWKS/token endpoints (absolute URLs built
/// from `base_url`). Optional convenience for SDK consumers.
async fn openid_configuration(
    State(state): State<AppState>,
    Path(tenant): Path<String>,
) -> Json<Value> {
    let base = &state.base_url;
    Json(json!({
        "issuer": state.signer.load().issuer,
        "token_endpoint": format!("{base}/{tenant}/oauth2/v2.0/token"),
        "jwks_uri": format!("{base}/{tenant}/discovery/v2.0/keys"),
        "response_types_supported": ["token"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
    }))
}

#[cfg(test)]
mod tests {
    use super::served_tid;

    /// `served_tid` recovers the bare tenant GUID from the v1.0 issuer template.
    #[test]
    fn served_tid_strips_issuer_envelope() {
        assert_eq!(
            served_tid("https://sts.windows.net/00000000-0000-0000-0000-000000000000/"),
            "00000000-0000-0000-0000-000000000000"
        );
    }
}
