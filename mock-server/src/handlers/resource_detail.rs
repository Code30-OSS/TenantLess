//! `GET /subscriptions/{sub}/resourceGroups/{rg}/providers/{*tail}` — the
//! resource-detail endpoint.
//!
//! Resolves a single resource by **reconstructed id** rather than by parsing
//! arbitrary nesting depth: the provider-onward catch-all `{*tail}` is just part of
//! the captured path, so `Microsoft.Sql/servers/{n}/databases/{n}` resolves the same
//! way as `Microsoft.Storage/storageAccounts/{n}`. The lookup is case-insensitive via
//! `lower(id) = lower($1)` with the reconstructed id **bound** as `$1`
//! — never spliced into SQL. A hit returns the
//! same `Resource` ARM DTO as the list endpoints (single object, NOT a `{value:[]}`
//! envelope); `type` is echoed verbatim by `From<ResourceRow>`.
//! A miss is a true 404 `ResourceNotFound`, unlike the list endpoints' empty
//! `{value:[]}`.

use crate::{
    arm::{Resource, ResourceRow},
    error::ApiError,
    etag::{baseline_resource_etag, overlay_etag},
    state::AppState,
};
use axum::{
    Json,
    extract::{Path, State},
    http::{HeaderValue, header},
};
use uuid::Uuid;

/// Resolve a single resource by its reconstructed ARM id.
///
/// `{sub}` parses as `Uuid` (parse-before-bind); `{rg}` and the catch-all
/// `{*tail}` arrive already percent-decoded by axum (do NOT double-decode). The
/// reconstructed id is bound as `$1` and compared case-insensitively; a miss yields
/// 404 `ResourceNotFound`.
pub async fn get_resource_detail(
    State(state): State<AppState>,
    Path((sub, rg, tail)): Path<(Uuid, String, String)>,
) -> Result<([(header::HeaderName, HeaderValue); 1], Json<Resource>), ApiError> {
    let id = format!("/subscriptions/{sub}/resourceGroups/{rg}/providers/{tail}");

    // Resolve through the view. A tombstoned (present=false) id is absent
    // from the view → the `fetch_optional` miss yields a 404 without consulting the legacy
    // soft-delete oracle (that conjunct is DROPPED); a present overlay row wins wholesale.
    let row = sqlx::query_as::<_, ResourceRow>(
        r#"SELECT id, name, type, location, tags, sku, kind, properties
           FROM synthetic.arm_resolved_resources
           WHERE lower(id) = lower($1)
           LIMIT 1"#,
    )
    .bind(&id) // bound, never spliced
    .fetch_optional(&state.pool)
    .await?;

    // Tombstoned / unknown id → 404 ResourceNotFound with NO ETag header.
    let resource = Resource::from(row.ok_or(ApiError::NotFound { what: id.clone() })?);

    // ETag domain selection. A present overlay (drift) row for this id → `o-<revision>`
    // straight from the monotonic `arm_overlay.revision`; otherwise the served baseline row →
    // `b-<hash>` over the served DTO. The id is BOUND as `$1`, never spliced.
    let overlay_revision: Option<i64> = sqlx::query_scalar(
        "SELECT revision FROM synthetic.arm_overlay \
         WHERE id_lower = lower($1) AND target_kind = 'resource' AND present = true",
    )
    .bind(&id)
    .fetch_optional(&state.pool)
    .await?;

    let etag = match overlay_revision {
        Some(revision) => overlay_etag(revision),
        None => baseline_resource_etag(&resource),
    };
    // The token is pure ASCII (`"b-<hex>"` / `"o-<decimal>"`) so `from_str` never fails; the
    // JSON body bytes are UNCHANGED — only a response header is added (byte-identity preserved).
    let header = HeaderValue::from_str(&etag).expect("etag token is valid header ASCII");
    Ok(([(header::ETAG, header)], Json(resource)))
}
