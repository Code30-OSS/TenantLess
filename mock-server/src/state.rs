//! Shared application state — the single DB seam (project "one seam" convention,
//! mirroring `writer.py::open_writer` / `reader.py::open_duckdb`).
//!
//! `AppState` carries the `PgPool` and the configured `base_url`. Handlers receive
//! it via the axum `State` extractor and never open their own connections.
//! `base_url` is the only source of truth for absolute `nextLink`s (MOCK-08).

use crate::metrics::Metrics;

/// Cloneable shared state injected into every handler and the auth middleware.
#[derive(Clone)]
pub struct AppState {
    /// The pooled Postgres connection (the one DB seam).
    pub pool: sqlx::PgPool,
    /// Absolute base URL for `nextLink`s, e.g. `http://localhost:8080` (MOCK-08).
    pub base_url: String,
    /// In-memory request-activity metrics + live broadcast for the `/_console`
    /// dashboard. Cloned (cheap `Arc`) into every handler/middleware copy of state.
    pub metrics: Metrics,
    /// The run-scoped RS256 signer (IAM-04, D-08): mint, JWKS export, and `--enforce-auth`
    /// validation all read it. A [`SharedSigner`](crate::jwt::SharedSigner) (a
    /// `RwLock<Arc<JwtSigner>>` handle) rather than a bare `Arc`, because the control plane
    /// HOT-SWAPS it after a tenant-mutating job (generate/restore/reset) so the served
    /// identity tracks the current tenant instead of the boot-time one. `AppState` and
    /// `ControlPlane` hold clones of the SAME handle. Handlers call `.load()` to read.
    pub signer: crate::jwt::SharedSigner,
    /// Opt-in real-JWT enforcement (IAM-05, D-11). **Default false** — the
    /// presence-only any-Bearer contract is unchanged until Plan 10-04 wires the
    /// validation swap. Sourced from `--enforce-auth` / `ENFORCE_AUTH`.
    pub enforce_auth: bool,
    /// The armed control-plane bundle (Phase 17, D-02). `Some` **iff** the server was
    /// started with `--enable-control-plane` AND a non-empty control token; `None` (the
    /// default) keeps the read-only posture and leaves `/_control/*` unmerged (404).
    /// [`crate::build_router`] merges [`crate::control::router`] ONLY when this is `Some`,
    /// on the bearer-exempt seam so the ARM contract stays byte-identical (D-17).
    pub control: Option<crate::job::ControlPlane>,
}
