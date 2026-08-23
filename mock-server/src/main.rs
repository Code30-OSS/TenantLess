//! `tenantless-server` entrypoint: parse clap config → build a capped Postgres pool
//! → construct `AppState` → `serve_dual`.
//!
//! `serve_dual` (in `lib.rs`) keeps the no-`--tls` path byte-identical to v1 (a
//! single `axum::serve` on `--port`) and, only when `--tls` is set, ALSO binds
//! HTTPS on `--tls-port` with an ephemeral in-memory self-signed cert.
//! The shared seam lets `tests/tls.rs` drive the real dual bind.

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
    // Logs go to STDERR (conventional for diagnostics) so the WAUTH-03 startup WARN and any
    // startup error share one stream the D-26 subprocess test can observe deterministically.
    tracing_subscriber::fmt()
        .with_writer(std::io::stderr)
        .init();

    let cli = Cli::parse();

    // Cap connections as a DoS guard and apply the
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
    // only in memory, inside the hot-swappable `SharedSigner` handle below.
    //
    // An initialized-but-EMPTY `synthetic` schema
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
    // provisioned before the identity tables existed (or by an older --no-identity generate) serves RBAC
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
    // before the drift tables existed (or before an older `tenantless generate`) serves list/detail
    // WITHOUT 500ing on the missing `drift_deleted_at` column referenced by the
    // soft-delete filter (the filter must NOT land before the
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
    // (`synthetic.tenant.profile_name`) so a volume provisioned before this column existed (or by an
    // older `tenantless generate`) serves `/_sim/summary` WITHOUT referencing a missing
    // `profile_name` column. We PROVISION — never mask; an un-set column
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

    // Startup schema preflight: idempotently provision the ARM overlay
    // substrate (`synthetic.arm_overlay` + the unowned revision sequence + the revision
    // trigger + the NAMED row-model CHECKs) so the resolver has a trustworthy
    // overlay/tombstone/revision store to migrate onto. We PROVISION — never mask; and the
    // structural inventory inside `ensure_arm_overlay_schema` fails LOUDLY on a malformed
    // pre-existing table rather than serving on a corrupt substrate. Boundary:
    // no reader consults this table yet, no drift path writes it, no ETag header is
    // emitted. Runs AFTER `ensure_web_metadata_schema` and BEFORE building `AppState`.
    tenantless_server::ensure_arm_overlay_schema(&pool)
        .await
        .map_err(|e| {
            format!(
                "arm overlay schema preflight (sql/009_arm_overlay.sql) failed: {e}. The \
             database is reachable and has a tenant, but the `synthetic.arm_overlay` \
             substrate could not be provisioned or failed its structural inventory. Check the \
             DB role's CREATE privilege on schema `synthetic`, or run `tenantless init-db` to \
             (re)provision (the overlay is NOT provisioned by `tenantless generate`)."
            )
        })?;

    // Startup schema preflight: idempotently provision the ARM-ID identity fold
    // functions (`synthetic.ascii_fold` / `synthetic.arm_id_key`, both IMMUTABLE STRICT
    // translate() functions) by applying sql/011. ADDITIVE + behaviour-neutral (D-22a):
    // this ONLY defines the two functions — no CHECK change, no index, no view edit, no
    // predicate cutover. It runs AFTER `ensure_arm_overlay_schema` (sql/009) and BEFORE
    // `ensure_arm_resolver_schema` (sql/010) purely so the functions EXIST before any future
    // sql/010 that references `arm_id_key` (the later predicate cutover) is applied against an upgraded volume —
    // the boot-safety guarantee. In THIS unit sql/010 is UNCHANGED (still `lower(...)`) and
    // no `011 -> audit -> 012 -> 010` cutover ordering is wired. No reader consults these
    // functions yet.
    tenantless_server::ensure_arm_id_key_schema(&pool)
        .await
        .map_err(|e| {
            format!(
                "arm id-key schema preflight (sql/011_arm_id_key.sql) failed: {e}. The \
             database is reachable and has a tenant, but the `synthetic.ascii_fold` / \
             `synthetic.arm_id_key` fold functions could not be provisioned. Check the DB \
             role's CREATE privilege on schema `synthetic`, or run `tenantless init-db` to \
             (re)provision."
            )
        })?;

    // Startup schema preflight: idempotently provision the resolver substrate
    // (`synthetic.drift_batches.storage_mode` + the two resolved views
    // `synthetic.arm_resolved_resources` / `synthetic.arm_resolved_resource_groups` + the
    // `(target_kind, id_lower)` overlay index) so the ARM readers have the liveness
    // authority + resolution seam to build on. We PROVISION — never mask; and the
    // structural inventory inside `ensure_arm_resolver_schema` fails LOUDLY on a malformed
    // view/column/index rather than serving on a corrupt resolver seam. The migration is
    // additive + idempotent + touches NOTHING on the populated `synthetic.resources` table.
    // Runs AFTER `ensure_arm_overlay_schema` (the views union against `synthetic.arm_overlay`,
    // the index is built on it) and BEFORE building `AppState`.
    tenantless_server::ensure_arm_resolver_schema(&pool)
        .await
        .map_err(|e| {
            format!(
                "arm resolver schema preflight (sql/010_arm_resolver.sql) failed: {e}. The \
             database is reachable and has a tenant, but the resolver substrate \
             (`synthetic.arm_resolved_resources` / `synthetic.arm_resolved_resource_groups` + \
             `storage_mode` + the overlay index) could not be provisioned or failed its \
             structural inventory. Check the DB role's CREATE privilege on schema `synthetic`, \
             or run `tenantless init-db` to (re)provision (the resolver is NOT provisioned by \
             `tenantless generate`)."
            )
        })?;

    // Provenance-based FAIL-CLOSED boot guard (mandatory safety net for the
    // reset-cutover). This release does NOT migrate historical in-place drift — so a tenant still
    // carrying legacy in-place drift applied by the pre-v3 binary (an ACTIVE
    // `storage_mode='synthetic'` drift batch, or any `synthetic.resources.drift_deleted_at`)
    // must REFUSE to boot rather than silently serve stale/invisible drift. An ACTIVE OVERLAY
    // batch (`storage_mode='overlay'`, the new apply-drift path) NEVER trips it — the provenance
    // marker is exactly what lets a valid post-cutover tenant boot. The probe is a single
    // read-only round trip (two EXISTS, ACCESS SHARE) that issues NO DDL, so it cannot
    // reintroduce the ACCESS-EXCLUSIVE startup deadlock. Runs AFTER
    // `ensure_arm_resolver_schema` (needs `storage_mode`) and BEFORE building `SharedSigner`/
    // `AppState`, so on a dirty tenant `serve_dual` is never reached and the server never binds.
    // The `String` error (the byte-exact locked message) surfaces verbatim as the boxed `main`
    // error via `?` (`From<String> for Box<dyn Error + Send + Sync>`).
    tenantless_server::assert_no_legacy_inplace_drift(&pool).await?;

    // D-12 / WAUTH-03 write-safety gate. Arming ARM writes WITHOUT `--enforce-auth` on a
    // NON-loopback bind would expose an UNAUTHENTICATED write plane on a public interface —
    // REFUSE to start unless the operator passes the explicit `--allow-insecure-writes`
    // local/test override. The decision is the pure `should_refuse_unauth_writes` predicate
    // (unit-tested by a behavioral matrix, D-23); `main` only composes it here. This guard is
    // ADDITIVE to the fail-closed boot guard above (D-11: a structurally-bad substrate still
    // refuses regardless of these flags) — it is checked AFTER the DB preflights/boot-guard and
    // BEFORE `serve_dual`, so on a bootable tenant it is the write-safety gate that decides the
    // bind. The `String` error surfaces verbatim as the boxed `main` error via `?` and the
    // process exits non-zero WITHOUT binding. NEVER log tokens, Authorization headers, or
    // request bodies (D-12) — the message names only the host and the operator's remedies.
    if tenantless_server::config::should_refuse_unauth_writes(
        cli.enable_arm_writes,
        cli.enforce_auth,
        &cli.host,
        cli.allow_insecure_writes,
    ) {
        return Err(format!(
            "{marker} (host={host}). ARM writes are armed but authentication is not enforced \
             and the bind is not loopback, so the write plane would accept ANY request on a \
             public interface. Choose one: add --enforce-auth to validate JWTs, bind loopback \
             (--host 127.0.0.1), or pass --allow-insecure-writes for a knowingly-local/test \
             deployment. See docs/arm-writes-security.md.",
            marker = tenantless_server::config::UNAUTH_WRITE_REFUSAL_MARKER,
            host = cli.host,
        )
        .into());
    }

    // WAUTH-03: a prominent startup WARN whenever writes are enabled without strict auth — both
    // the loopback-permitted case and the `--allow-insecure-writes` override reach here. The
    // mode is LOCAL/TEST-ONLY. Credential-safe: names no token, header, or body (D-12).
    if cli.enable_arm_writes && !cli.enforce_auth {
        tracing::warn!(
            "{} — this is a LOCAL/TEST-ONLY posture and MUST NOT be exposed on a public bind. \
             See docs/arm-writes-security.md.",
            tenantless_server::config::UNAUTH_WRITE_WARN_MARKER,
        );
    }

    // The run's signer, wrapped in a HOT-SWAPPABLE shared handle (IAM staleness fix): the
    // control plane rebuilds it after a tenant-mutating job so the served identity tracks the
    // current tenant, not this boot-time one. `AppState` and the `ControlPlane` below hold
    // clones of the SAME handle.
    let signer = SharedSigner::new(JwtSigner::ephemeral(&tenant_id)?);

    // Arm the control plane BEFORE moving `cli` fields into
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
        // Default OFF — any-Bearer preserved until the auth swap is wired in.
        enforce_auth: cli.enforce_auth,
        // Default OFF — write methods 405 until explicitly armed (WAUTH-01). The
        // D-12 non-loopback refusal guard is applied at startup; here the field is just plumbed.
        enable_arm_writes: cli.enable_arm_writes,
        // `Some` only when armed; `build_router` merges `/_control` iff `Some`.
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
