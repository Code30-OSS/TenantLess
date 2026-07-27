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
        (
            status,
            Json(serde_json::json!({ "error": { "code": code, "message": message } })),
        )
            .into_response()
    }
}

impl From<sqlx::Error> for ApiError {
    fn from(err: sqlx::Error) -> Self {
        // Wrap for server-side logging only; the body stays generic.
        ApiError::Internal(err.to_string())
    }
}
