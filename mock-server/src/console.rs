//! The `/_console` live activity dashboard data API (local dev observability).
//!
//! This route group is deliberately **unauthenticated** and **uninstrumented**:
//! it loads directly in a browser (no Bearer token) and its own requests are not
//! fed back into the activity stream. The ARM API routes keep their bearer auth.
//!
//! Four endpoints:
//! - `GET /_console`         → a **302 Found** redirect to `/ui/console` (the React
//!   console — the legacy embedded HTML page was retired, D-02).
//! - `GET /_console/stats`   → a JSON [`crate::metrics::StatsSnapshot`] for the
//!   initial render and periodic aggregate refresh.
//! - `GET /_console/history` → a JSON [`crate::metrics::HistorySnapshot`]: the
//!   rolling p50/p95/max-over-time bucket series (survives reload, D-03). Takes no
//!   user input.
//! - `GET /_console/stream`  → a Server-Sent Events stream of live
//!   [`crate::metrics::RequestEvent`]s for the instant feed.

use std::convert::Infallible;

use axum::{
    Json, Router,
    extract::State,
    http::{StatusCode, header},
    response::{
        IntoResponse,
        sse::{Event, KeepAlive, Sse},
    },
    routing::get,
};
use tokio_stream::{Stream, StreamExt, wrappers::BroadcastStream};

use crate::{
    metrics::{HistorySnapshot, StatsSnapshot},
    state::AppState,
};

/// The dashboard sub-router. Merged into the top-level router WITHOUT the bearer
/// or metrics layers (see [`crate::build_router`]).
pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/_console", get(redirect_to_console))
        .route("/_console/stats", get(stats))
        .route("/_console/history", get(history))
        .route("/_console/stream", get(stream))
        .with_state(state)
}

/// Retire the legacy embedded HTML dashboard: `GET /_console` now issues an exact
/// **302 Found** to the React console at `/ui/console` (D-02). axum 0.8's `Redirect`
/// has no 302 constructor (only 303/307/308), so the response is built manually. The
/// `Location` is a compile-time literal — no user input is reflected (no open-redirect).
async fn redirect_to_console() -> impl IntoResponse {
    (StatusCode::FOUND, [(header::LOCATION, "/ui/console")])
}

/// JSON snapshot of current aggregates + recent events.
async fn stats(State(state): State<AppState>) -> Json<StatsSnapshot> {
    Json(state.metrics.snapshot())
}

/// JSON rolling p50/p95/max-over-time bucket series (D-03). Reads the in-memory
/// bucket ring only — no user input, no DB. The React console polls it for the
/// reload-surviving aggregate latency chart.
async fn history(State(state): State<AppState>) -> Json<HistorySnapshot> {
    Json(state.metrics.history())
}

/// Live SSE feed: one `message` event per served ARM request. A lagging
/// subscriber (slow tab) drops intermediate events rather than blocking the
/// recorder — the periodic `/stats` poll reconciles the aggregates regardless.
async fn stream(
    State(state): State<AppState>,
) -> Sse<impl Stream<Item = Result<Event, Infallible>>> {
    let rx = state.metrics.subscribe();
    let events = BroadcastStream::new(rx).filter_map(|res| {
        let ev = res.ok()?; // skip lagged-receiver errors
        Event::default().json_data(&ev).ok().map(Ok)
    });
    Sse::new(events).keep_alive(KeepAlive::default())
}
