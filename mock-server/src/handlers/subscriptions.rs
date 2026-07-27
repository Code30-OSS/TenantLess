//! `GET /subscriptions` — projects `synthetic.subscriptions` into the ARM
//! subscription envelope (MOCK-01).
//!
//! No pagination for subscriptions (A4 — sub counts are small). api-version, if
//! present in the query string, is accepted and ignored (MOCK-11) — the handler
//! takes no query extractor, so any query params are simply dropped. Uses a
//! runtime `query_as` with an explicit static column list (no `SELECT *`, no
//! string-built SQL) so the build needs no DATABASE_URL.

use crate::{
    arm::{ListResponse, Subscription, SubscriptionRow},
    error::ApiError,
    state::AppState,
};
use axum::{Json, extract::State};

/// List all subscriptions in the ARM envelope, ordered by `subscription_id` for
/// determinism.
pub async fn list_subscriptions(
    State(state): State<AppState>,
) -> Result<Json<ListResponse<Subscription>>, ApiError> {
    let rows = sqlx::query_as::<_, SubscriptionRow>(
        r#"SELECT subscription_id, tenant_id, display_name, state,
                  authorization_source, spending_limit
           FROM synthetic.subscriptions
           ORDER BY subscription_id"#,
    )
    .fetch_all(&state.pool)
    .await?;

    let value = rows.into_iter().map(Subscription::from).collect();
    Ok(Json(ListResponse::new(value)))
}
