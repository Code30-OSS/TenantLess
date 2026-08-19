//! `GET /subscriptions/{sub}/resourceGroups` — keyset-paginated ARM resource-group
//! list.
//!
//! Pagination is opaque-cursor keyset over the `id` PK: `WHERE subscription_id = $1
//! AND ($2 IS NULL OR id > $2) ORDER BY id LIMIT $3` with `$3 = clamp_top + 1`. The
//! surplus row drives `nextLink` emission (omitted on the last page). The
//! `{sub}` path param is parsed as a `Uuid` BEFORE binding, and the decoded cursor
//! is `.bind()`-bound — never spliced into SQL. An unknown
//! `{sub}` naturally yields an empty result set → `{ "value": [] }` (locked behavior).

use crate::{
    arm::{ListResponse, ResourceGroup, ResourceGroupRow},
    error::ApiError,
    etag::{baseline_rg_etag, overlay_etag},
    pagination::{PageParams, clamp_top, decode_token, next_link, split_page},
    state::AppState,
};
use axum::{
    Json,
    extract::{Path, Query, State},
    http::{HeaderValue, header},
};
use uuid::Uuid;

/// List a subscription's resource groups in the ARM envelope, keyset-paginated by
/// `id` with `$top` clamp and opaque `$skiptoken` continuation.
pub async fn list_resource_groups(
    State(state): State<AppState>,
    Path(sub): Path<Uuid>,
    Query(params): Query<PageParams>,
) -> Result<Json<ListResponse<ResourceGroup>>, ApiError> {
    let top = clamp_top(params.top);
    let cursor = params.skiptoken.as_deref().map(decode_token).transpose()?;

    // Read FROM the resolved RG view. There is no RG overlay writer yet, so
    // the view is baseline-only content here (it ships the never-populated overlay branch for a
    // later release); the keyset shape and column list are preserved verbatim.
    let rows = sqlx::query_as::<_, ResourceGroupRow>(
        r#"SELECT id, name, location, tags, provisioning_state
           FROM synthetic.arm_resolved_resource_groups
           WHERE subscription_id = $1 AND ($2::text IS NULL OR id > $2)
           ORDER BY id
           LIMIT $3"#,
    )
    .bind(sub)
    .bind(cursor)
    .bind(top + 1)
    .fetch_all(&state.pool)
    .await?;

    let (page, next_token) = split_page(rows, top, |r| r.id.as_str());
    let value: Vec<ResourceGroup> = page.into_iter().map(ResourceGroup::from).collect();

    let mut response = ListResponse::new(value);
    if let Some(tok) = next_token {
        let path = format!("/subscriptions/{sub}/resourceGroups");
        response.next_link = Some(next_link(
            &state.base_url,
            &path,
            top,
            &tok,
            params.api_version.as_deref(),
            // `$filter` is NOT supported on the resourceGroups listing;
            // pass `None` so the nextLink never echoes a filter for this endpoint.
            None,
        ));
    }
    Ok(Json(response))
}

/// `GET /subscriptions/{sub}/resourceGroups/{rg}` — the READ-ONLY single-resource-group
/// detail endpoint (ETag emission for the RG kind).
///
/// Resolves one RG by reconstructed id through `synthetic.arm_resolved_resource_groups`
/// (`lower(id) = lower($1)`, the id BOUND, never spliced). A hit returns the
/// single `ResourceGroup` ARM DTO (NOT a `{value:[]}` envelope) plus an `ETag` header; a miss
/// is a true 404 `ResourceNotFound` with no ETag.
///
/// READ-ONLY for now: there is NO RG write path and no RG overlay writer yet (RG CRUD is a
/// later release). The resolved RG view exposes the overlay branch, so an overlay-present RG (if
/// one ever exists) selects the `o-<revision>` ETag; today every RG resolves via the baseline
/// branch → `b-<hash>`.
pub async fn get_resource_group_detail(
    State(state): State<AppState>,
    Path((sub, rg)): Path<(Uuid, String)>,
) -> Result<([(header::HeaderName, HeaderValue); 1], Json<ResourceGroup>), ApiError> {
    let id = format!("/subscriptions/{sub}/resourceGroups/{rg}");

    let row = sqlx::query_as::<_, ResourceGroupRow>(
        r#"SELECT id, name, location, tags, provisioning_state
           FROM synthetic.arm_resolved_resource_groups
           WHERE lower(id) = lower($1)
           LIMIT 1"#,
    )
    .bind(&id) // bound, never spliced
    .fetch_optional(&state.pool)
    .await?;

    // Unknown RG → 404 ResourceNotFound with NO ETag header.
    let group = ResourceGroup::from(row.ok_or(ApiError::NotFound { what: id.clone() })?);

    // ETag domain selection, mirroring the resource detail: a present overlay RG row →
    // `o-<revision>`; else the served baseline RG DTO → `b-<hash>`. Id bound as `$1`.
    let overlay_revision: Option<i64> = sqlx::query_scalar(
        "SELECT revision FROM synthetic.arm_overlay \
         WHERE id_lower = lower($1) AND target_kind = 'resource_group' AND present = true",
    )
    .bind(&id)
    .fetch_optional(&state.pool)
    .await?;

    let etag = match overlay_revision {
        Some(revision) => overlay_etag(revision),
        None => baseline_rg_etag(&group),
    };
    let header = HeaderValue::from_str(&etag).expect("etag token is valid header ASCII");
    Ok(([(header::ETAG, header)], Json(group)))
}
