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
    response::{IntoResponse, Response},
};
use serde_json::Value;
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
) -> Result<Response, ApiError> {
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

    // ETag domain selection + D-17 body source. A present overlay row for this id →
    // `o-<revision>` straight from the monotonic `arm_overlay.revision`, and the response
    // serves the FULL stored `arm_overlay.body` verbatim so opaque top-level keys
    // (identity/zones/plan/…) written by the user survive a create→GET round-trip (the
    // `sql/010` resolver projection drops them). A baseline row keeps the byte-identical typed
    // projection and the `b-<hash>` served-DTO ETag. The id is BOUND as `$1`, never spliced.
    let overlay: Option<(i64, sqlx::types::Json<Value>)> = sqlx::query_as(
        "SELECT revision, body FROM synthetic.arm_overlay \
         WHERE id_lower = lower($1) AND target_kind = 'resource' AND present = true",
    )
    .bind(&id)
    .fetch_optional(&state.pool)
    .await?;

    let (etag, mut response) = match overlay {
        // Overlay-sourced: serve the full stored body (D-17 opaque survival) — but normalize
        // the `type` CASING through `canonical_type` exactly as the list projection does
        // (`From<ResourceRow>`, MOCK-12), so a non-canonically-cased write can never make
        // detail and list disagree on `type`. Every other key is preserved verbatim.
        Some((revision, mut body)) => {
            if let Some(t) = body.0.get("type").and_then(Value::as_str) {
                let canonical = crate::casing::canonical_type(t);
                if let Some(obj) = body.0.as_object_mut() {
                    obj.insert("type".to_string(), Value::String(canonical));
                }
            }
            (overlay_etag(revision), Json(body.0).into_response())
        }
        // Baseline: the typed projection stays byte-identical to the pre-write read (D-06).
        None => (
            baseline_resource_etag(&resource),
            Json(resource).into_response(),
        ),
    };
    // The token is pure ASCII (`"b-<hex>"` / `"o-<decimal>"`) so `from_str` never fails; the
    // baseline JSON body bytes are UNCHANGED — only a response header is added.
    let value = HeaderValue::from_str(&etag).expect("etag token is valid header ASCII");
    response.headers_mut().insert(header::ETAG, value);
    Ok(response)
}
