//! Permissive Bearer-auth middleware (MOCK-09).
//!
//! Applied at the router level (`from_fn_with_state`) so EVERY route is gated
//! (threat T-03-01). The check is presence/prefix only: ANY *non-empty* token
//! value after `Bearer ` is accepted — real token validation is explicitly out
//! of scope. A missing/malformed header, OR a `Bearer` prefix with an empty /
//! whitespace-only token (SEC-HIGH-3), short-circuits to a 401 ARM CloudError,
//! never reaching a handler. Any genuinely non-empty token (e.g. `Bearer health`
//! for the docker healthcheck, or a live localhost scanner's static token) still
//! passes, preserving the any-Bearer scanner contract.

use crate::{error::ApiError, state::AppState};
use axum::{
    extract::{Request, State},
    middleware::Next,
    response::Response,
};
use jsonwebtoken::{Algorithm, Validation, decode};

/// True only when `value` is a `Bearer` authorization header carrying a
/// non-empty token. The prefix match is case-insensitive (after a leading
/// trim); the token (everything after `bearer `) must be non-empty once its
/// surrounding whitespace is stripped — an empty `Bearer ` (SEC-HIGH-3) is
/// rejected while any real token is accepted.
///
/// Pure + std-only so it is unit-testable DB-free (Nyquist).
pub fn bearer_token_present(value: &str) -> bool {
    let trimmed = value.trim_start();
    // Case-insensitive prefix check without allocating a lowercased copy of the
    // (possibly long) token: only the prefix region is compared.
    const PREFIX: &str = "bearer ";
    if trimmed.len() < PREFIX.len() {
        return false;
    }
    let (prefix, rest) = trimmed.split_at(PREFIX.len());
    if !prefix.eq_ignore_ascii_case(PREFIX) {
        return false;
    }
    !rest.trim().is_empty()
}

/// Strip a leading (case-insensitive, leading-trimmed) `Bearer ` scheme and return
/// the trimmed token, or `None` if the header is not a non-empty `Bearer` token.
/// Reuses the [`bearer_token_present`] contract so the ON path's notion of "a Bearer
/// token" is identical to the OFF path's.
fn strip_bearer(value: &str) -> Option<&str> {
    if !bearer_token_present(value) {
        return None;
    }
    let trimmed = value.trim_start();
    const PREFIX: &str = "bearer ";
    let (_, rest) = trimmed.split_at(PREFIX.len());
    Some(rest.trim())
}

/// Gate every ARM route on the `Authorization: Bearer` header.
///
/// **OFF (`!enforce_auth`, the default) — byte-for-byte the v1 presence-only path
/// (D-10, Pitfall 2):** a missing/malformed header or an empty/whitespace-only token
/// short-circuits to 401; ANY non-empty token is accepted (the any-Bearer scanner
/// contract, `arbitrary_bearer_200`).
///
/// **ON (`--enforce-auth`):** the header MUST be a `Bearer <jwt>` that validates RS256
/// against THIS run's own JWKS, with an exact `iss`/`aud` match and `exp` in the future
/// (RESEARCH Q4). Any failure → 401 ARM CloudError `InvalidAuthenticationToken`. The
/// token + JWKS routes are merged OUTSIDE this layer (D-11) so they bootstrap freely.
///
/// `Request` MUST be the last extractor argument; `next.run(request).await` runs the
/// rest of the stack once authorized.
pub async fn bearer_auth(
    State(state): State<AppState>,
    request: Request,
    next: Next,
) -> Result<Response, ApiError> {
    let header = request
        .headers()
        .get(axum::http::header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok());

    if !state.enforce_auth {
        // DEFAULT (v1) — presence-only, UNCHANGED. arbitrary_bearer_200 stays green.
        let ok = header.map(bearer_token_present).unwrap_or(false);
        if !ok {
            return Err(ApiError::missing_auth());
        }
        return Ok(next.run(request).await);
    }

    // ENFORCE — must be `Bearer <jwt>` validating against this run's JWKS (D-10).
    let token = header
        .and_then(strip_bearer)
        .ok_or_else(ApiError::missing_auth)?;
    // Snapshot the CURRENT signer (the control plane may hot-swap it on a tenant mutation).
    // `load()` clones the `Arc` under a brief lock and releases it — nothing is held across
    // the decode, and this request validates against one coherent identity.
    let signer = state.signer.load();
    let mut validation = Validation::new(Algorithm::RS256);
    validation.set_issuer(std::slice::from_ref(&signer.issuer));
    validation.set_audience(std::slice::from_ref(&signer.audience));
    validation.validate_exp = true;
    decode::<serde_json::Value>(token, &signer.decoding, &validation)
        .map_err(|_| ApiError::invalid_token())?;
    Ok(next.run(request).await)
}

#[cfg(test)]
mod tests {
    use super::bearer_token_present;

    #[test]
    fn empty_bearer_token_is_rejected() {
        // SEC-HIGH-3: `Bearer ` with no token, or only whitespace, must fail.
        assert!(!bearer_token_present("Bearer "));
        assert!(!bearer_token_present("Bearer    "));
        assert!(!bearer_token_present("bearer "));
        assert!(!bearer_token_present("Bearer \t  "));
    }

    #[test]
    fn missing_or_malformed_header_is_rejected() {
        assert!(!bearer_token_present(""));
        assert!(!bearer_token_present("Basic abc123"));
        assert!(!bearer_token_present("Bearer")); // no trailing space/token
    }

    #[test]
    fn any_non_empty_token_is_accepted() {
        // The scanner contract: any non-empty token still passes (the live
        // localhost Path-A scan, docker healthcheck `Bearer health`).
        assert!(bearer_token_present("Bearer anything"));
        assert!(bearer_token_present("Bearer health"));
        assert!(bearer_token_present("bearer aTokenValue"));
        // Leading whitespace before the scheme is tolerated (matches v1 trim).
        assert!(bearer_token_present("  Bearer token"));
    }
}
