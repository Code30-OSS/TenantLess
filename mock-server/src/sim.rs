//! The `/_sim` read-only projection sub-router (WAPI-04) — the Web Console's
//! simulator-internal JSON surface (violations, dependencies, tenant summary, and the
//! keyset-paginated subscriptions enumeration).
//!
//! This route group is deliberately **bearer-EXEMPT** and **uninstrumented**, merged
//! into the top-level router on the SAME exempt seam as `/_console` and `/token` (see
//! [`crate::build_router`]) — NOT inside the `arm` bearer/metrics layers. The drift audit
//! reads (`/simulator/drift*`) sit INSIDE the bearer gate on purpose (Ph11 D-15/D-16);
//! that placement is deliberately NOT mirrored here (D-02), and there is no `/_sim`
//! identity/RBAC mirror either (D-03).
//!
//! Two structural WAPI-04 guarantees are properties of THIS router regardless of how the
//! outer router is composed:
//!   * **Read-only (D-12.3):** only `GET` handlers are registered, so axum's
//!     `MethodRouter` returns `405 Method Not Allowed` (with an `Allow: GET` header) for
//!     `POST`/`PUT`/`PATCH`/`DELETE` — enforced structurally, no middleware.
//!   * **Scoped JSON-404 (D-12.5):** the inner router carries its own
//!     [`sim_not_found`] fallback, so an unknown `/_sim/*` path returns the ARM
//!     `{error:{code,message}}` CloudError body — NOT a bare/HTML 404. The fallback is
//!     scoped to the `/_sim` nest ONLY, so the global 404 behavior for unknown ARM paths
//!     stays byte-identical (D-12.1). The outer `arm` router keeps NO fallback, so the
//!     `merge()` never hits axum's two-fallbacks panic.
//!
//! `/_sim` is a FRESH prefix (`nest("/_sim", …)`), so it cannot shadow or capture any ARM
//! route (D-12.4) — axum 0.8 / matchit 0.8 treat the static `/_sim` nest and the ARM
//! routes as non-overlapping.

use axum::{Router, routing::get};

use crate::{error::ApiError, handlers, state::AppState};

/// The `/_sim` projection sub-router. Merged into the top-level router WITHOUT the bearer
/// or metrics layers (see [`crate::build_router`]). Registers FIVE GET routes under the
/// `/_sim` prefix, each bound to a read-only handler in [`crate::handlers::sim`], plus a scoped
/// JSON-404 fallback. `/subscriptions` (D-15) is the keyset-paginated full-enumeration companion
/// to the bounded inline `summary.subscriptions[]` preview (GAP-14-03 / T-14-05);
/// `/resources/search` (15-14) is the tenant-wide name/type substring search (EXPL-01/EXPL-05).
/// Drift and identity/RBAC stay deferred (no `/_sim` mirror — D-02/D-03), so the surface is
/// exactly these five.
pub fn router(state: AppState) -> Router {
    let inner = Router::new()
        .route("/violations", get(handlers::sim::list_violations))
        .route("/dependencies", get(handlers::sim::list_dependencies))
        .route("/summary", get(handlers::sim::summary))
        .route("/subscriptions", get(handlers::sim::list_subscriptions))
        .route("/resources/search", get(handlers::sim::search_resources))
        .fallback(sim_not_found) // scoped to /_sim/* only — global 404 unchanged (D-12.5)
        .with_state(state);
    Router::new().nest("/_sim", inner) // fresh prefix — cannot shadow ARM (D-12.4)
}

/// Unknown `/_sim` route → the ARM CloudError JSON shape, not a bare/HTML 404 (D-12.5).
async fn sim_not_found() -> ApiError {
    ApiError::NotFound {
        what: "the requested /_sim resource".to_string(),
    }
}
