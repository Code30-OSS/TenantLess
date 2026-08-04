//! ARM CloudError error type (MOCK-10).
//!
//! `ApiError` is the single error model for the whole crate. It owns both the
//! HTTP status and the ARM `{ error: { code, message } }` body. The `Internal`
//! arm NEVER serializes its wrapped string into the response — DB/SQL/schema
//! text is logged via `tracing` but never leaks to the client (threat T-03-02,
//! Security Domain L562). `From<sqlx::Error>` routes every `?` in handlers to a
//! generic `InternalServerError`.

use axum::{
    Json,
    http::StatusCode,
    response::{IntoResponse, Response},
};

/// All error outcomes the API can produce, each mapping to an ARM CloudError body.
#[derive(Debug)]
pub enum ApiError {
    /// 404 ResourceNotFound.
    NotFound { what: String },
    /// 400 InvalidRequestContent (e.g. a malformed `$skiptoken`).
    BadRequest { message: String },
    /// 401 MissingAuthenticationToken.
    Unauthorized,
    /// 401 InvalidAuthenticationToken — `--enforce-auth` rejected the presented JWT
    /// (bad signature / expired / wrong iss/aud / alg-confusion). The body stays a
    /// fixed generic message; no validation detail leaks (T-10-15).
    InvalidToken,
    /// 401 InvalidControlToken — the control-plane token gate rejected the request
    /// (missing/wrong `X-Control-Token`). Deliberately DISTINCT from `Unauthorized`,
    /// whose message names the ARM `Authorization` header — the control realm is a
    /// separate auth model (D-01/D-17). The message is fixed and non-leaking.
    ControlUnauthorized,
    /// 409 ControlBusy — another destructive control job already holds the single-writer
    /// gate; at most one generate/reset/restore is in flight (D-11).
    Busy,
    /// 413 RequestEntityTooLarge — the inbound request body exceeded the size budget
    /// (execution budgets). Enforced at extraction time, chunked-safe (T-BUDGET). A
    /// server-side limit, NOT malformed content, so it is distinct from `BadRequest`.
    PayloadTooLarge,
    /// 415 UnsupportedMediaType — a JSON body was posted with a missing or non-JSON
    /// `Content-Type`. Preserves the media-type contract of the stock axum `Json`
    /// extractor (the bounded cost extractor replaced it). Distinct from a 400: the
    /// content is not malformed, its declared type is simply wrong.
    UnsupportedMediaType,
    /// 503 ServiceUnavailable — the server is at its concurrency limit and shed this
    /// request rather than queueing it (execution budgets, load-shed), OR the DB pool was
    /// exhausted and the acquire timed out (`sqlx::Error::PoolTimedOut`). Carries a
    /// `Retry-After: 1` header. Capacity exhaustion, NOT a client error.
    ServiceUnavailable,
    /// 504 GatewayTimeout — a server-side execution deadline elapsed (the global request
    /// timeout, or a Postgres `statement_timeout`/`57014` on a non-cost query). A
    /// server-side timeout is NOT malformed client input, so it is a 504, never a 400.
    /// (The cost endpoint keeps its own "query too expensive" 400 via its app deadline.)
    GatewayTimeout,
    /// 500 InternalServerError — generic; wrapped detail is logged, never serialized.
    Internal(String),
}

impl ApiError {
    /// Constructor used by the Bearer middleware when the header is absent (MOCK-09).
    pub fn missing_auth() -> ApiError {
        ApiError::Unauthorized
    }

    /// Constructor used by the `--enforce-auth` Bearer middleware when a presented JWT
    /// fails RS256 / iss / aud / exp validation (IAM-05, D-10). Reuses the existing ARM
    /// CloudError envelope — only the `code`/`message` differ from `missing_auth`.
    pub fn invalid_token() -> ApiError {
        ApiError::InvalidToken
    }

    /// 400 InvalidRequestContent with a fixed, non-leaking message — used by the cost
    /// handler for an unknown grouping dimension / timeframe (T-9-IV).
    pub fn bad_request(message: impl Into<String>) -> ApiError {
        ApiError::BadRequest {
            message: message.into(),
        }
    }

    /// 401 for the control-plane token gate (D-01/D-17). Reuses the ARM CloudError
    /// envelope but with a control-specific `code`/`message` — NOT `Unauthorized`, whose
    /// message references the `Authorization` header (misleading for the control realm).
    pub fn control_unauthorized() -> ApiError {
        ApiError::ControlUnauthorized
    }

    /// 409 for a second destructive control job while one is still running (D-11).
    pub fn busy() -> ApiError {
        ApiError::Busy
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        // Load-shed 503s carry `Retry-After: 1` (the client should retry shortly, not
        // give up). Captured before the match moves `self`.
        let retry_after = matches!(self, ApiError::ServiceUnavailable);
        let (status, code, message): (StatusCode, &str, String) = match self {
            ApiError::NotFound { what } => (
                StatusCode::NOT_FOUND,
                "ResourceNotFound",
                format!("The entity '{what}' was not found."),
            ),
            ApiError::BadRequest { message } => {
                (StatusCode::BAD_REQUEST, "InvalidRequestContent", message)
            }
            ApiError::Unauthorized => (
                StatusCode::UNAUTHORIZED,
                "MissingAuthenticationToken",
                "Authentication failed. The 'Authorization' header is missing.".to_string(),
            ),
            ApiError::InvalidToken => (
                StatusCode::UNAUTHORIZED,
                "InvalidAuthenticationToken",
                "Authentication failed. The access token is invalid.".to_string(),
            ),
            ApiError::ControlUnauthorized => (
                StatusCode::UNAUTHORIZED,
                "InvalidControlToken",
                "Invalid control token.".to_string(),
            ),
            ApiError::Busy => (
                StatusCode::CONFLICT,
                "ControlBusy",
                "Another job is still running. Try again when it finishes.".to_string(),
            ),
            ApiError::PayloadTooLarge => (
                StatusCode::PAYLOAD_TOO_LARGE,
                "RequestEntityTooLarge",
                "The request body is too large.".to_string(),
            ),
            ApiError::UnsupportedMediaType => (
                StatusCode::UNSUPPORTED_MEDIA_TYPE,
                "UnsupportedMediaType",
                "The 'Content-Type' must be 'application/json'.".to_string(),
            ),
            ApiError::ServiceUnavailable => (
                StatusCode::SERVICE_UNAVAILABLE,
                "ServiceUnavailable",
                "The server is temporarily at its concurrency limit. Retry shortly.".to_string(),
            ),
            ApiError::GatewayTimeout => (
                StatusCode::GATEWAY_TIMEOUT,
                "GatewayTimeout",
                "The request exceeded the server execution deadline.".to_string(),
            ),
            ApiError::Internal(detail) => {
                // Log the real cause server-side; NEVER put it in the response body.
                tracing::error!(error = %detail, "internal server error");
                (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "InternalServerError",
                    "An internal error occurred.".to_string(),
                )
            }
        };
        let mut response = (
            status,
            Json(serde_json::json!({ "error": { "code": code, "message": message } })),
        )
            .into_response();
        if retry_after {
            response.headers_mut().insert(
                axum::http::header::RETRY_AFTER,
                axum::http::HeaderValue::from_static("1"),
            );
        }
        response
    }
}

impl From<sqlx::Error> for ApiError {
    fn from(err: sqlx::Error) -> Self {
        // Pool exhaustion: the `acquire_timeout` elapsed with no free connection
        // (`PoolTimedOut`). This is capacity exhaustion, NOT a client error and NOT a lost
        // connection — the correct signal is 503 ServiceUnavailable + `Retry-After: 1`
        // (the SAME shed response the concurrency limiter emits), so a scanner backs off
        // and retries rather than treating it as a hard 500.
        if matches!(err, sqlx::Error::PoolTimedOut) {
            return ApiError::ServiceUnavailable;
        }
        // A cancelled statement (Postgres SQLSTATE 57014 `query_canceled`) means a
        // server-side `statement_timeout` fired — a server execution deadline, mapped to
        // 504 (NOT a 400: it is not malformed client input). The cost handler never reaches
        // here for its own deadline — its app-owned `tokio::time::timeout` returns a 400
        // first — so this maps only the server-wide statement_timeout on the other reads.
        if let sqlx::Error::Database(db) = &err
            && db.code().as_deref() == Some("57014")
        {
            return ApiError::GatewayTimeout;
        }
        // Otherwise wrap for server-side logging only; the body stays generic.
        ApiError::Internal(err.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::header;

    /// Pool exhaustion (`acquire_timeout` elapsed → `PoolTimedOut`) maps to a 503
    /// ServiceUnavailable carrying `Retry-After: 1` — capacity exhaustion, never a 500.
    #[tokio::test]
    async fn pool_timeout_maps_to_503_retry_after() {
        let err = ApiError::from(sqlx::Error::PoolTimedOut);
        assert!(
            matches!(err, ApiError::ServiceUnavailable),
            "PoolTimedOut must map to ServiceUnavailable, got {err:?}"
        );
        // The rendered response is a 503 + Retry-After: 1, same as the load-shed path.
        let resp = err.into_response();
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(
            resp.headers()
                .get(header::RETRY_AFTER)
                .and_then(|v| v.to_str().ok()),
            Some("1"),
            "a capacity 503 must carry Retry-After: 1"
        );
    }

    /// A non-pool, non-timeout sqlx error stays a generic 500 (detail logged, never
    /// serialized) — the fallback arm is unchanged by the PoolTimedOut special-case.
    #[test]
    fn other_sqlx_error_stays_500() {
        let err = ApiError::from(sqlx::Error::RowNotFound);
        assert!(
            matches!(err, ApiError::Internal(_)),
            "an unmapped sqlx error must stay Internal(500), got {err:?}"
        );
    }
}
