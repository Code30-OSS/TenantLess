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
    config::Cli,
    jwt::{JwtSigner, SharedSigner},
    metrics::Metrics,
    serve_dual,
    state::AppState,
};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    tracing_subscriber::fmt::init();

    let cli = Cli::parse();

    // Cap connections as a DoS guard (Security Domain, RESEARCH L565) and apply the
    // server-wide DB execution budgets: a session-level `statement_timeout` on EVERY pooled
    // connection (so a runaway query on any handler — not just cost — is cancelled, ⇒ a 504
    // via the SQLSTATE-57014 mapping), and an `acquire_timeout` so pool exhaustion fails
    // fast instead of hanging. The timeout value is a validated config integer, bound as
    // `$1` into `set_config` (never spliced). `false` ⇒ session scope, so it persists for
    // the connection's life; the cost query still sets its own tighter LOCAL override.
    let stmt_timeout_ms = cli.db_statement_timeout_ms;
    let pool = PgPoolOptions::new()
        .max_connections(15)
        .acquire_timeout(std::time::Duration::from_secs(cli.db_acquire_timeout_secs))
        .after_connect(move |conn, _meta| {
            Box::pin(async move {
                sqlx::query("SELECT set_config('statement_timeout', $1, false)")
                    .bind(stmt_timeout_ms.to_string())
                    .execute(conn)
                    .await?;
                Ok(())
            })
        })
        .connect(&cli.database_url)
        .await?;

    // Read the single served tenant_id once at startup (the sim has one tenant) so
    // the signer's v1.0 `iss` embeds it, then generate the ephemeral RS256 key
    // BEFORE building AppState (mirrors the TLS cert in `serve_dual`). The key lives
    // only in memory, inside the hot-swappable `SharedSigner` handle below (D-08).
    //
    // Phase 17 (D-09, RESEARCH Pitfall 3): an initialized-but-EMPTY `synthetic` schema
    // (migrations applied, `synthetic.tenant` still empty — the post-`reset` state) must
    // BOOT, not crash. `fetch_optional` tolerates zero rows and we fall back to
    // `Uuid::nil()`; the ARM read handlers already query `synthetic.*` directly and return
    // empty envelopes on an empty tenant, so startup is the sole remaining assertion to
    // relax. This boot-time id is no longer the LAST word: a later control-plane mutation
    // (generate/restore/reset) re-derives the tenant and rebuilds the signer (see the
    // `SharedSigner` below + `ControlPlane`), so the served identity tracks the current
    // tenant instead of freezing at this one.
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

    // The run's signer, wrapped in a HOT-SWAPPABLE shared handle (IAM staleness fix): the
    // control plane rebuilds it after a tenant-mutating job so the served identity tracks the
    // current tenant, not this boot-time one. `AppState` and the `ControlPlane` below hold
    // clones of the SAME handle.
    let signer = SharedSigner::new(JwtSigner::ephemeral(&tenant_id)?);

    // Phase 17 (CTRL-05, D-02): arm the control plane BEFORE moving `cli` fields into
    // AppState. `arm` is FAIL-CLOSED — disabled → `None` (read-only posture unchanged);
    // `--enable-control-plane` WITHOUT a non-empty token → `Err`, propagated here as a
    // clear startup error (the server never arms without a secret). The `String` error
    // converts into the boxed `main` error via `?`. The child job runner needs the DSN by
    // value, so `arm` takes a `pool.clone()` while `pool` still moves into AppState below;
    // it also takes the shared signer handle (cloned) so it can refresh the identity.
    let control = tenantless_server::job::ControlPlane::arm(&cli, pool.clone(), signer.clone())?;

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

    // Execution budgets applied to the router (request timeout + concurrency shed). The DB
    // budgets are already baked into `state.pool` above.
    let budgets = tenantless_server::Budgets {
        request_timeout: std::time::Duration::from_secs(cli.request_timeout_secs),
        concurrency_limit: cli.concurrency_limit as usize,
    };

    // Default (--tls absent): byte-identical single plain-HTTP bind on cli.port.
    // --tls: ALSO bind HTTPS on cli.tls_port (ephemeral self-signed cert).
    serve_dual(state, budgets, cli.tls, &cli.host, cli.port, cli.tls_port).await?;

    Ok(())
}
