//! `GET /subscriptions/{sub}/resourceGroups/{rg}/providers/{*tail}` — the
//! resource-detail endpoint (MOCK-05, MOCK-07, MOCK-12).
//!
//! Resolves a single resource by **reconstructed id** (D-05) rather than by parsing
//! arbitrary nesting depth: the provider-onward catch-all `{*tail}` is just part of
//! the captured path, so `Microsoft.Sql/servers/{n}/databases/{n}` resolves the same
//! way as `Microsoft.Storage/storageAccounts/{n}`. The lookup is case-insensitive via
//! `lower(id) = lower($1)` (D-08, MOCK-07) with the reconstructed id **bound** as `$1`
//! — never spliced into SQL (T-04-06, carries forward T-03-09/10). A hit returns the
//! same `Resource` ARM DTO as the list endpoints (single object, NOT a `{value:[]}`
//! envelope — D-07); `type` is echoed verbatim by `From<ResourceRow>` (MOCK-12, D-09).
//! A miss is a true 404 `ResourceNotFound` (D-06), unlike the list endpoints' empty
//! `{value:[]}`.

use crate::{
    arm::{Resource, ResourceRow},
    error::ApiError,
    state::AppState,
};
use axum::{
    Json,
    extract::{Path, State},
};
use uuid::Uuid;

/// Resolve a single resource by its reconstructed ARM id (MOCK-05/07/12).
///
/// `{sub}` parses as `Uuid` (parse-before-bind, T-03-09); `{rg}` and the catch-all
/// `{*tail}` arrive already percent-decoded by axum (do NOT double-decode). The
/// reconstructed id is bound as `$1` and compared case-insensitively; a miss yields
/// 404 `ResourceNotFound`.
pub async fn get_resource_detail(
    State(state): State<AppState>,
    Path((sub, rg, tail)): Path<(Uuid, String, String)>,
) -> Result<Json<Resource>, ApiError> {
    let id = format!("/subscriptions/{sub}/resourceGroups/{rg}/providers/{tail}");

    let row = sqlx::query_as::<_, ResourceRow>(
        r#"SELECT id, name, type, location, tags, sku, kind, properties
           FROM synthetic.resources
           WHERE lower(id) = lower($1) AND drift_deleted_at IS NULL
           LIMIT 1"#,
    )
    .bind(&id) // bound, never spliced (T-04-06; carries forward T-03-09/10)
    .fetch_optional(&state.pool)
    .await?;

    row.map(|r| Json(Resource::from(r)))
        .ok_or(ApiError::NotFound { what: id }) // 404 ResourceNotFound (D-06)
}
