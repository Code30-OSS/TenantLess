//! `GET /subscriptions/{sub}/resourceGroups` — keyset-paginated ARM resource-group
//! list (MOCK-02).
//!
//! Pagination is opaque-cursor keyset over the `id` PK: `WHERE subscription_id = $1
//! AND ($2 IS NULL OR id > $2) ORDER BY id LIMIT $3` with `$3 = clamp_top + 1`. The
//! surplus row drives `nextLink` emission (Pitfall 4: omitted on the last page). The
//! `{sub}` path param is parsed as a `Uuid` BEFORE binding, and the decoded cursor
//! is `.bind()`-bound — never spliced into SQL (threats T-03-06/T-03-09). An unknown
//! `{sub}` naturally yields an empty result set → `{ "value": [] }` (locked behavior,
//! research Open Question 2).

use crate::{
    arm::{ListResponse, ResourceGroup, ResourceGroupRow},
    error::ApiError,
    pagination::{PageParams, clamp_top, decode_token, next_link, split_page},
    state::AppState,
};
use axum::{
    Json,
    extract::{Path, Query, State},
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

    let rows = sqlx::query_as::<_, ResourceGroupRow>(
        r#"SELECT id, name, location, tags, provisioning_state
           FROM synthetic.resource_groups
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
            // `$filter` is NOT supported on the resourceGroups listing (D-03);
            // pass `None` so the nextLink never echoes a filter for this endpoint.
            None,
        ));
    }
    Ok(Json(response))
}
