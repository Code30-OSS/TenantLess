//! The `/ui` embedded Web Console SPA sub-router (WEBUI-03, D-06) — serves the built
//! `frontend/dist` React app from INSIDE the single `tenantless-server` binary.
//!
//! This route group is deliberately **bearer-EXEMPT** and **uninstrumented**, merged into
//! the top-level router on the SAME exempt seam as `/_console`, `/token`, and `/_sim` (see
//! [`crate::build_router`]) — NOT inside the `arm` bearer/metrics layers (the SPA must load
//! in a plain browser with no `Authorization` header).
//!
//! It mirrors the `/_sim` router's composition discipline EXACTLY (see `sim.rs`):
//!   * **Fresh prefix — cannot shadow ARM:** `nest("/ui", …)` — axum 0.8 / matchit 0.8 treat
//!     the static `/ui` nest and the ARM routes as non-overlapping, so no ARM route is
//!     captured or shadowed (mirrors `sim.rs` D-12.4).
//!   * **Scoped fallback — no two-fallbacks panic:** the catch-all [`serve_ui`] fallback lives
//!     INSIDE the `/ui` nest. The outer `arm` router keeps NO fallback, so `.merge()` never
//!     hits axum's two-fallbacks panic, and the global 404 for unknown ARM paths stays
//!     byte-identical (WAPI-04 `arm_byte_identical`, D-06). This is what keeps the ARM scanner
//!     contract intact after the UI overlay merges.
//!
//! Unlike `sim.rs`, [`router`] takes NO [`crate::state::AppState`] — the assets are STATIC,
//! embedded at COMPILE time via `include_dir!` (a `build.rs` guard re-embeds on `dist` change
//! and fails the build if the SPA is unbuilt — RESEARCH Pitfall 4).
//!
//! Serving rules (RESEARCH Pattern 1):
//!   * **asset hit** → the embedded bytes + a `mime_guess` Content-Type, so `.js`/`.css`/
//!     `.woff2` are never mis-typed (Pitfall 5 — a wrong MIME makes the browser refuse the
//!     module script);
//!   * **asset-looking miss** (the lookup path contains `.`) → a real `404`, NEVER index.html
//!     (returning HTML for a missing `.js` triggers the "Unexpected token '<'" MIME trap);
//!   * **extensionless miss** (a client-side nav route) → `index.html` as the SPA fallback,
//!     so a deep-linked React route reloads correctly.

use axum::{
    Router,
    http::{StatusCode, Uri, header},
    response::{IntoResponse, Response},
    routing::get,
};
use include_dir::{Dir, include_dir};

/// The built Web Console SPA, embedded at COMPILE time. `$CARGO_MANIFEST_DIR` is
/// `mock-server/`; the Vite `dist` lives at repo-root `frontend/dist`, i.e. `../frontend/dist`
/// relative to the manifest. The `build.rs` guard emits
/// `cargo:rerun-if-changed=../frontend/dist` (re-embed whenever the SPA rebuilds) and hard-
/// errors if `dist/index.html` is absent, so a stale/empty dist can never be shipped silently
/// (RESEARCH Pitfall 4).
static UI_DIST: Dir<'static> = include_dir!("$CARGO_MANIFEST_DIR/../frontend/dist");

/// The `/ui` SPA sub-router. Merged into the top-level router WITHOUT the bearer or metrics
/// layers (see [`crate::build_router`]). A FRESH `nest("/ui", …)` prefix whose single
/// [`serve_ui`] catch-all fallback is scoped INSIDE the nest — so it cannot shadow an ARM
/// route (D-06) and the fallback-free `arm` router never hits the two-fallbacks merge panic.
/// Takes NO state: the assets are static (embedded via `include_dir!`).
pub fn router() -> Router {
    let inner = Router::new().fallback(get(serve_ui)); // catch-all scoped to /ui/* only
    Router::new().nest("/ui", inner) // fresh prefix — cannot shadow ARM (D-06)
}

/// Serve an embedded SPA asset, or fall back to `index.html` for client-side nav routes.
///
/// axum 0.8 strips the `/ui` nest prefix from the `Uri` this fallback sees (RESEARCH A5,
/// pinned by `tests/ui.rs::ui_hashed_asset_js_mime`), so the lookup key is the dist-relative
/// path (e.g. `assets/index-<hash>.js`). An empty path resolves to `index.html`.
async fn serve_ui(uri: Uri) -> Response {
    let path = uri.path().trim_start_matches('/');
    let lookup = if path.is_empty() { "index.html" } else { path };

    if let Some(file) = UI_DIST.get_file(lookup) {
        // Correct Content-Type per extension (Pitfall 5) — `.js`/`.css`/`.woff2` etc.
        let mime = mime_guess::from_path(lookup).first_or_octet_stream();
        return ([(header::CONTENT_TYPE, mime.as_ref())], file.contents()).into_response();
    }

    // Asset-looking miss (has an extension) → a REAL 404, never index.html (Pitfall 5).
    if lookup.contains('.') {
        return (StatusCode::NOT_FOUND, "not found").into_response();
    }

    // Client-side nav route (no extension) → SPA fallback to index.html.
    match UI_DIST.get_file("index.html") {
        Some(index) => (
            [(header::CONTENT_TYPE, "text/html; charset=utf-8")],
            index.contents(),
        )
            .into_response(),
        None => (StatusCode::INTERNAL_SERVER_ERROR, "ui not built").into_response(),
    }
}
