//! `tenantless-server` entrypoint: parse clap config → build a capped Postgres pool
//! → construct `AppState` → `serve_dual`.
//!
//! `serve_dual` (in `lib.rs`) keeps the no-`--tls` path byte-identical to v1 (a
//! single `axum::serve` on `--port`) and, only when `--tls` is set, ALSO binds
//! HTTPS on `--tls-port` with an ephemeral in-memory self-signed cert (PLAT-05,
//! D-15/D-16). The shared seam lets `tests/tls.rs` drive the real dual bind.

use clap::Parser;
use sqlx::postgres::PgPoolOptions;
use tenantless_server::{
    config::Cli, jwt::JwtSigner, metrics::Metrics, serve_dual, state::AppState,
};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    tracing_subscriber::fmt::init();

    let cli = Cli::parse();

    // Cap connections as a DoS guard (Security Domain, RESEARCH L565).
    let pool = PgPoolOptions::new()
        .max_connections(15)
        .connect(&cli.database_url)
        .await?;

    // Read the single served tenant_id once at startup (the sim has one tenant) so
    // the signer's v1.0 `iss` embeds it, then generate the ephemeral RS256 key
    // BEFORE building AppState (mirrors the TLS cert in `serve_dual`). The key lives
    // only in memory, behind an Arc shared by every handler copy of state (D-08).
    //
    // Phase 17 (D-09, RESEARCH Pitfall 3): an initialized-but-EMPTY `synthetic` schema
    // (migrations applied, `synthetic.tenant` still empty — the post-`reset` state) must
    // BOOT, not crash. `fetch_optional` tolerates zero rows and we fall back to
    // `Uuid::nil()`; the ARM read handlers already query `synthetic.*` directly and return
    // empty envelopes on an empty tenant, so startup is the sole remaining assertion to
    // relax. Under the default posture (`--enforce-auth` OFF) the signer's `iss` is
    // cosmetic, so a nil id is harmless (A3); re-minting the signer on a later generate is
    // a deferred nicety (the control realm is separate from the ARM bearer realm).
    let tenant_id: uuid::Uuid =
        sqlx::query_scalar("SELECT tenant_id FROM synthetic.tenant LIMIT 1")
            .fetch_optional(&pool)
            .await?
            .unwrap_or_else(uuid::Uuid::nil);

    // Startup schema preflight: idempotently provision the identity tables so a volume
    // provisioned before Phase 10 (or by an older --no-identity generate) serves RBAC
    // (empty) instead of 500ing on a missing relation. We PROVISION — never mask. Loud +
    // actionable if provisioning itself fails. Runs AFTER the tenant_id ok_or so the
    // `synthetic` schema is confirmed to exist (sql/005 assumes it).
    tenantless_server::ensure_identity_schema(&pool)
        .await
        .map_err(|e| {
            format!(
                "identity schema preflight (sql/005_identity.sql) failed: {e}. The database is \
             reachable and has a tenant, but the identity tables could not be provisioned. \
             Check the DB role's CREATE privilege on schema `synthetic`, or run \
             `tenantless generate` to (re)provision."
            )
        })?;

    // Startup schema preflight: idempotently provision the drift tables + the
    // `synthetic.resources.drift_deleted_at` soft-delete column so a volume provisioned
    // before Phase 11 (or before this plan's `tenantless generate`) serves list/detail
    // WITHOUT 500ing on the missing `drift_deleted_at` column referenced by the
    // soft-delete filter (RESEARCH Pitfall 2 — the filter must NOT land before the
    // column exists). We PROVISION — never mask. Runs AFTER `ensure_identity_schema`
    // and BEFORE `serve_dual` so `drift_deleted_at` exists before any list/detail SELECT.
    tenantless_server::ensure_drift_schema(&pool)
        .await
        .map_err(|e| {
            format!(
                "drift schema preflight (sql/006_drift.sql) failed: {e}. The database is \
             reachable and has a tenant, but the drift tables/column could not be \
             provisioned. Check the DB role's CREATE/ALTER privilege on schema \
             `synthetic`, or run `tenantless generate`."
            )
        })?;

    // Startup schema preflight: idempotently provision the Web Console metadata column
    // (`synthetic.tenant.profile_name`) so a volume provisioned before Phase 14 (or by an
    // older `tenantless generate`) serves `/_sim/summary` WITHOUT referencing a missing
    // `profile_name` column (WAPI-03 / D-14). We PROVISION — never mask; an un-set column
    // reads NULL ⇒ `profile: null`. The ALTER targets `synthetic.tenant` (1 row) and is
    // nullable-no-default → metadata-only fast path (minimal lock). Runs AFTER
    // `ensure_drift_schema` and BEFORE `serve_dual` so `profile_name` exists before any
    // summary SELECT.
    tenantless_server::ensure_web_metadata_schema(&pool)
        .await
        .map_err(|e| {
            format!(
                "web metadata schema preflight (sql/007_web_metadata.sql) failed: {e}. The \
             database is reachable and has a tenant, but the `synthetic.tenant.profile_name` \
             column could not be provisioned. Check the DB role's ALTER privilege on schema \
             `synthetic`, or run `tenantless generate`."
            )
        })?;

    let signer = std::sync::Arc::new(JwtSigner::ephemeral(&tenant_id)?);

    // Phase 17 (CTRL-05, D-02): arm the control plane BEFORE moving `cli` fields into
    // AppState. `arm` is FAIL-CLOSED — disabled → `None` (read-only posture unchanged);
    // `--enable-control-plane` WITHOUT a non-empty token → `Err`, propagated here as a
    // clear startup error (the server never arms without a secret). The `String` error
    // converts into the boxed `main` error via `?`. The child job runner needs the DSN by
    // value, so `arm` takes a `pool.clone()` while `pool` still moves into AppState below.
    let control = tenantless_server::job::ControlPlane::arm(&cli, pool.clone())?;

    let state = AppState {
        pool,
        base_url: cli.base_url,
        metrics: Metrics::new(),
        signer,
        // Default OFF — any-Bearer preserved until Plan 10-04 wires the swap (D-11).
        enforce_auth: cli.enforce_auth,
        // `Some` only when armed (D-02); `build_router` merges `/_control` iff `Some`.
        control,
    };

    // Default (--tls absent): byte-identical single plain-HTTP bind on cli.port.
    // --tls: ALSO bind HTTPS on cli.tls_port (ephemeral self-signed cert).
    serve_dual(state, cli.tls, &cli.host, cli.port, cli.tls_port).await?;

    Ok(())
}
