//! tenantless-server public API.
//!
//! `build_router(state) -> Router` is the single app factory shared by `main` and
//! the integration tests (RESEARCH L163: the most important testability decision).
//! Every module this phase creates is re-exported here.

pub mod arm;
pub mod auth;
pub mod casing;
pub mod config;
pub mod console;
pub mod control;
pub mod error;
pub mod filter;
pub mod handlers;
pub mod job;
pub mod jwt;
pub mod metrics;
pub mod pagination;
pub mod sim;
pub mod snapshot;
pub mod state;
pub mod ui;

use axum::{
    Router,
    middleware::from_fn_with_state,
    routing::{get, post},
};
use state::AppState;
use std::net::SocketAddr;
use tokio::net::TcpListener;

/// Build the axum router. Registers `GET /subscriptions` (Wave 1), the
/// `/subscriptions/{sub}/resourceGroups` and `/subscriptions/{sub}/resources`
/// paginated list routes (Wave 2), and the RG-scoped
/// `/subscriptions/{sub}/resourceGroups/{rg}/resources` route (Wave 3, MOCK-04). The
/// Bearer layer is applied at the ARM router level
/// so EVERY ARM route is gated (threat T-03-01, MOCK-09). Param routes use the axum 0.8
/// `/{param}` curly-brace syntax convention.
///
/// The `/_console` dashboard sub-router is merged in SEPARATELY, outside both the
/// bearer layer (it must load in a plain browser) and the metrics layer (its own
/// polling/SSE traffic must not pollute the activity feed). The `record_metrics`
/// layer wraps the bearer layer so the recorded status reflects the final response,
/// including 401s.
pub fn build_router(state: AppState) -> Router {
    // Delegate to the pre-merge ARM baseline, then add the `/_sim` surface AND the `/ui`
    // Web Console SPA on the SAME bearer-exempt seam (WAPI-04 / WEBUI-03). Keeping the
    // composition in `build_router_without_sim` gives the contract test a GENUINE pre-merge
    // router to compare against, so `arm_byte_identical` can detect a merge regression
    // instead of asserting X == X (D-17). `/ui` is a FRESH nested prefix with its OWN
    // scoped fallback (see [`ui::router`]) — it cannot shadow an ARM route (D-06), and the
    // fallback-free `arm` router never hits the two-fallbacks merge panic. `ui::router()`
    // takes NO state (the SPA assets are static, embedded via `include_dir!`).
    // Phase 17 (D-02/D-17): merge the `/_control` write surface ONLY when the server is
    // armed (`state.control` is `Some`). A disarmed server exposes NO `/_control/*` routes
    // (they 404, not 403). `/_control` is a FRESH `nest` prefix on this SAME bearer-exempt
    // seam, off the ARM bearer/metrics layers, so merging it CANNOT change ARM response
    // bytes — `arm_byte_identical` stays green (CTRL-06). It carries its OWN control-token
    // gate (see [`control::router`]), a distinct realm from the any-Bearer ARM model (D-01).
    let mut r = build_router_without_sim(state.clone())
        .merge(sim::router(state.clone()))
        .merge(ui::router());
    if let Some(cp) = state.control.clone() {
        r = r.merge(control::router(cp));
    }
    r
}

/// The pre-merge ARM baseline (WAPI-04 test seam, D-17): the FULL runtime router MINUS the
/// `/_sim` merge. Builds the `arm` chain (with its `bearer_auth` + `record_metrics` layers)
/// and merges the two other bearer-exempt sub-routers (`/_console` and `/token` + JWKS) —
/// but does NOT `.merge(sim::router)`, so it exposes NO `/_sim` surface.
///
/// [`build_router`] delegates here and then adds `.merge(sim::router(state))`, so this is
/// the exact router that served every prior phase before Phase 14. `arm_byte_identical`
/// (`tests/sim.rs`) builds its reference app from this fn and compares it byte-for-byte
/// against the merged [`build_router`] — the pre-merge/merged pair is what makes that proof
/// non-tautological (a `/_sim` merge that altered ARM bytes/headers would fail the test).
pub fn build_router_without_sim(state: AppState) -> Router {
    let arm = Router::new()
        .route("/subscriptions", get(handlers::list_subscriptions))
        .route(
            "/subscriptions/{sub}/resourceGroups",
            get(handlers::list_resource_groups),
        )
        .route(
            "/subscriptions/{sub}/resources",
            get(handlers::list_resources),
        )
        .route(
            "/subscriptions/{sub}/resourceGroups/{rg}/resources",
            get(handlers::list_rg_resources),
        )
        // Cost Management Query — sub scope (COST-03). Registered INSIDE `arm` (above
        // the bearer/metrics layers) so it inherits the any-Bearer scanner contract
        // (IAM-05 / T-9-03). The sub-scope path has no catch-all, so it registers
        // directly with no static-vs-wildcard overlap.
        .route(
            "/subscriptions/{sub}/providers/Microsoft.CostManagement/query",
            post(handlers::cost_query),
        )
        // Microsoft.Authorization data plane (IAM-03) — three sub-scoped GET routes
        // registered INSIDE `arm` (above the bearer/metrics layers) so they inherit the
        // any-Bearer scanner contract + the `--enforce-auth` swap. These are static
        // `providers/...` paths (the SAME shape the cost sub-scope route already proved
        // registers cleanly — the only `{*tail}` catch-all is RG-scoped). Any api-version
        // is accepted/ignored (MOCK-11).
        .route(
            "/subscriptions/{sub}/providers/Microsoft.Authorization/roleDefinitions",
            get(handlers::list_role_definitions),
        )
        .route(
            "/subscriptions/{sub}/providers/Microsoft.Authorization/roleDefinitions/{role_id}",
            get(handlers::get_role_definition),
        )
        .route(
            "/subscriptions/{sub}/providers/Microsoft.Authorization/roleAssignments",
            get(handlers::list_role_assignments),
        )
        // Simulator-only drift audit reads (DRIFT-05) — three GET routes registered
        // INSIDE `arm` (above the bearer/metrics layers) so they sit INSIDE the bearer
        // gate (D-15): missing Bearer → 401, any non-empty Bearer → 200 (enforce off),
        // valid RS256 JWT under `--enforce-auth`. These are NOT merged via the outer
        // `arm.merge(...)` bearer-exempt path — only `/token`+JWKS+`/_console` stay
        // exempt (D-16). `/simulator` is a fresh prefix (no static-vs-wildcard overlap);
        // the by-resource route uses a `{*resource_id}` catch-all because ARM ids
        // contain `/`. The audit data served here is NEVER injected into ARM bodies
        // (D-17) — this is the only drift-audit surface.
        .route("/simulator/drift", get(handlers::drift::list_drift))
        .route(
            "/simulator/drift/{batch_id}",
            get(handlers::drift::get_batch),
        )
        .route(
            "/simulator/drift/resources/{*resource_id}",
            get(handlers::drift::by_resource),
        )
        // RG-scope Cost Management Query shares the existing GET `{*tail}` catch-all as a
        // method-merge (POST+GET on ONE path is the standard axum merge — no
        // static-vs-catch-all panic). `cost_query_scoped` 404s any tail other than
        // `Microsoft.CostManagement/query`.
        .route(
            "/subscriptions/{sub}/resourceGroups/{rg}/providers/{*tail}",
            get(handlers::get_resource_detail).post(handlers::cost_query_scoped),
        )
        .layer(from_fn_with_state(state.clone(), auth::bearer_auth))
        .layer(from_fn_with_state(state.clone(), metrics::record_metrics))
        .with_state(state.clone());

    // The `/_console` dashboard AND the token mint + JWKS (Plan 10-04) are merged
    // OUTSIDE the bearer layer: the console must load in a plain browser, and
    // `/token` + JWKS must be reachable with NO auth header to bootstrap a token
    // even when `--enforce-auth` is ON (D-11, the token-to-get-a-token deadlock
    // avoidance). Neither sub-router inherits the `bearer_auth`/`record_metrics`
    // layers above.
    // NOTE: `/_sim` (Phase 14, WAPI-04) is NOT merged here — it is added by the caller
    // [`build_router`] on this SAME bearer-exempt, uninstrumented seam. Keeping the `/_sim`
    // merge out of this baseline is exactly what lets `arm_byte_identical` compare a genuine
    // pre-merge router against the merged one (D-17). `/_sim` sits on the exempt seam — NOT
    // inside `arm` (that is where the drift audit reads sit, INSIDE the bearer gate,
    // deliberately not mirrored — D-02) — and is a fresh `nest("/_sim", …)` prefix with its
    // own scoped JSON-404 fallback, so it cannot shadow an ARM route (D-12.4) and the `arm`
    // router (which keeps NO fallback) never hits the two-fallbacks merge panic.
    arm.merge(console::router(state.clone()))
        .merge(handlers::token::router(state))
}

/// Bind and serve the mock server (PLAT-05, D-15/D-16).
///
/// The default (`tls == false`) path is a single plain-HTTP bind: one
/// [`build_router`] served by `axum::serve` on `{host}:{http_port}`. The `host`
/// defaults to loopback `127.0.0.1` (SEC-HIGH-3); pass `0.0.0.0` to bind every
/// interface. Nothing touches `tls_port` and no cert is generated — this
/// preserves the any-Bearer HTTP scanner contract (invariant 3 / RESEARCH Pitfall 5).
///
/// When `tls == true`, the SAME `Router` is ALSO served over HTTPS on
/// `{host}:{tls_port}` via `axum_server::bind_rustls`, using an **ephemeral
/// in-memory** self-signed cert (CN/SAN = `localhost`, `127.0.0.1`) generated fresh
/// at startup by `rcgen` — never written to disk (D-16). Both listeners run
/// concurrently over one `tokio::try_join!`; either erroring brings the process down.
///
/// Extracted from `main.rs` so integration tests can drive the real dual bind
/// (`tests/tls.rs`). One rustls stack only: the `ring` provider (see `Cargo.toml`).
/// Build a `host:port` bind address (SEC-HIGH-3). The default host is the
/// loopback `127.0.0.1`; an explicit `0.0.0.0` binds all interfaces. Pure +
/// std-only so it is unit-testable DB-free (Nyquist).
pub fn bind_addr(host: &str, port: u16) -> String {
    format!("{host}:{port}")
}

/// Idempotently provision the Phase-10 identity tables (`synthetic.principals`,
/// `synthetic.role_assignments`) by applying `sql/005_identity.sql`. Safe to run on
/// every boot: the migration is `CREATE ... IF NOT EXISTS` + a guarded-FK `DO` block,
/// a no-op on an already-migrated schema. This PROVISIONS the (possibly empty) tables
/// so the Microsoft.Authorization/roleAssignments read returns `[]` on an
/// identity-less tenant — it never masks a missing relation as empty business data.
/// Requires the `synthetic` schema to already exist (the caller confirms a tenant first).
///
/// Uses `sqlx::raw_sql` (the simple-query protocol) so the multi-statement DDL + the
/// guarded `DO $$ … $$` block execute as one unsplit batch — we never parse/split the
/// SQL ourselves (mirrors the Python twin `writer.ensure_identity_schema`).
pub async fn ensure_identity_schema(pool: &sqlx::PgPool) -> Result<(), sqlx::Error> {
    const SQL_005: &str = include_str!("../../sql/005_identity.sql");
    sqlx::raw_sql(SQL_005).execute(pool).await?;
    Ok(())
}

/// Idempotently provision the Phase-11 drift tables (`synthetic.drift_batches`,
/// `synthetic.drift_records`) AND the `synthetic.resources.drift_deleted_at`
/// soft-delete column by applying `sql/006_drift.sql`. Safe to run on every boot:
/// the migration is `CREATE ... IF NOT EXISTS` + `ADD COLUMN IF NOT EXISTS` + a
/// guarded-FK `DO` block, a no-op on an already-migrated schema. This PROVISIONS the
/// `drift_deleted_at` column so the list/detail soft-delete filter
/// (`AND drift_deleted_at IS NULL`) never references a missing relation/column on a
/// volume provisioned before Phase 11 (RESEARCH Pitfall 2) — it never masks a missing
/// column as empty business data. Requires the `synthetic` schema to already exist
/// (the caller confirms a tenant first).
///
/// Uses `sqlx::raw_sql` (the simple-query protocol) so the multi-statement DDL + the
/// guarded `DO $$ … $$` block execute as one unsplit batch — we never parse/split the
/// SQL ourselves (mirrors the Python twin `writer.ensure_drift_schema` and the
/// identity twin [`ensure_identity_schema`]).
pub async fn ensure_drift_schema(pool: &sqlx::PgPool) -> Result<(), sqlx::Error> {
    const SQL_006: &str = include_str!("../../sql/006_drift.sql");
    sqlx::raw_sql(SQL_006).execute(pool).await?;
    Ok(())
}

/// Idempotently provision the Phase-14 Web Console metadata column
/// (`synthetic.tenant.profile_name`) by applying `sql/007_web_metadata.sql`. Safe to run
/// on every boot: the migration is a single `ADD COLUMN IF NOT EXISTS`, a no-op on an
/// already-migrated schema. This PROVISIONS the nullable `profile_name` column so the
/// `/_sim/summary` handler's `SELECT ... profile_name ...` never references a missing
/// column on a volume provisioned before Phase 14 (WAPI-03 / D-14) — an un-set column
/// simply reads NULL (⇒ `profile: null`), it never masks a missing column as empty data.
/// Requires the `synthetic` schema to already exist (the caller confirms a tenant first).
///
/// Uses `sqlx::raw_sql` (the simple-query protocol) so the DDL executes as one unsplit
/// batch — we never parse/split the SQL ourselves (mirrors the Python twin
/// `writer.ensure_web_metadata_schema` and the drift twin [`ensure_drift_schema`]).
pub async fn ensure_web_metadata_schema(pool: &sqlx::PgPool) -> Result<(), sqlx::Error> {
    const SQL_007: &str = include_str!("../../sql/007_web_metadata.sql");
    sqlx::raw_sql(SQL_007).execute(pool).await?;
    Ok(())
}

pub async fn serve_dual(
    state: AppState,
    tls: bool,
    host: &str,
    http_port: u16,
    tls_port: u16,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let app = build_router(state);

    let http_addr = bind_addr(host, http_port);
    tracing::info!(addr = %http_addr, tls, "tenantless-server listening (HTTP)");

    if !tls {
        // DEFAULT path — byte-identical to v1: a single plain-HTTP bind.
        let listener = TcpListener::bind(&http_addr).await?;
        axum::serve(listener, app).await?;
        return Ok(());
    }

    // Opt-in HTTPS: ephemeral in-memory self-signed cert (D-16), one shared Router.
    use axum_server::tls_rustls::RustlsConfig;
    use rcgen::generate_simple_self_signed;

    // Pin the process-level rustls CryptoProvider to `ring` explicitly. Although
    // our `rustls` dep is feature-pinned to `ring`, transitive crates can surface
    // a second provider, leaving rustls unable to auto-select one — it then panics
    // inside `from_pem` ("Could not automatically determine the process-level
    // CryptoProvider"). Installing the default once removes that ambiguity and keeps
    // a single TLS stack (T-08-02-V6). `install_default` errors only if a provider
    // is already installed, which is fine — we ignore that.
    let _ = rustls::crypto::ring::default_provider().install_default();

    let certified =
        generate_simple_self_signed(vec!["localhost".to_string(), "127.0.0.1".to_string()])?;
    let cert_pem = certified.cert.pem().into_bytes();
    let key_pem = certified.signing_key.serialize_pem().into_bytes();
    let tls_config = RustlsConfig::from_pem(cert_pem, key_pem).await?;

    let https_addr: SocketAddr = bind_addr(host, tls_port).parse()?;
    tracing::info!(addr = %https_addr, "tenantless-server listening (HTTPS, ephemeral self-signed cert)");

    // Plain HTTP listener (still the default port, still always up under --tls).
    // Clone the Router up front so the HTTP coroutine owns its copy and the HTTPS
    // coroutine can move the original (no overlapping borrow). `Router` is cheap to clone.
    let http_app = app.clone();
    let http = async move {
        let listener = TcpListener::bind(&http_addr).await?;
        axum::serve(listener, http_app).await?;
        Ok::<(), Box<dyn std::error::Error + Send + Sync>>(())
    };

    // HTTPS listener over the SAME Router (tower MakeService form — axum 0.8 compatible).
    let https = async move {
        axum_server::bind_rustls(https_addr, tls_config)
            .serve(app.into_make_service())
            .await?;
        Ok::<(), Box<dyn std::error::Error + Send + Sync>>(())
    };

    tokio::try_join!(http, https)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::bind_addr;

    #[test]
    fn bind_addr_defaults_to_loopback() {
        // SEC-HIGH-3: the default host yields a loopback bind, NOT 0.0.0.0.
        assert_eq!(bind_addr("127.0.0.1", 8080), "127.0.0.1:8080");
    }

    #[test]
    fn bind_addr_honors_explicit_all_interfaces() {
        // An explicit 0.0.0.0 (compose/HOST override) binds every interface.
        assert_eq!(bind_addr("0.0.0.0", 8080), "0.0.0.0:8080");
        assert_eq!(bind_addr("0.0.0.0", 8443), "0.0.0.0:8443");
    }
}
