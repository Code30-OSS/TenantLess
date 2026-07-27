//! Build script — the `/ui` embedded-SPA freshness guard.
//!
//! `include_dir!("$CARGO_MANIFEST_DIR/../frontend/dist")` in [`crate::ui`] captures the
//! built SPA bytes at COMPILE time. Cargo does not otherwise know the embedded `dist`
//! changed, so a rebuilt frontend would be silently ignored and the *previous* UI shipped.
//! This script closes two gaps:
//!
//!   * **Re-embed on change:** emit `cargo:rerun-if-changed=../frontend/dist` so cargo
//!     re-runs — and thus re-invokes the `include_dir!` embed — whenever the built SPA
//!     changes. Without this, `cargo build` after `npm run build` ships the old bytes.
//!   * **Fail loudly on a missing dist:** hard-`panic!` (a build error) if
//!     `../frontend/dist/index.html` is absent, rather than embedding a stale/empty tree.
//!     The message tells the developer to build the SPA first.
//!
//! `$CARGO_MANIFEST_DIR` is `mock-server/`; the built SPA lives at repo-root `frontend/dist`,
//! i.e. `../frontend/dist` relative to this manifest (matches the `include_dir!` path).

use std::path::Path;

fn main() {
    // (a) Re-run this script — and re-embed the SPA — whenever the built dist changes.
    println!("cargo:rerun-if-changed=../frontend/dist");
    // Re-run if the guard itself changes.
    println!("cargo:rerun-if-changed=build.rs");

    // (b) Fail the build LOUDLY if the SPA has not been built. `include_dir!` would
    // otherwise error opaquely (or embed an empty tree) — surface the real cause + fix.
    let index = Path::new("../frontend/dist/index.html");
    if !index.exists() {
        panic!(
            "frontend/dist/index.html is missing — the /ui embed (src/ui.rs `include_dir!`) \
             requires a built SPA. Build it before the server: \
             `cd frontend && npm ci && npm run build`."
        );
    }
}
