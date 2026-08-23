//! tenantless-server public API.
//!
//! `build_router(state) -> Router` is the single app factory shared by `main` and
//! the integration tests (the most important testability decision).
//! Every module in this crate is re-exported here.

pub mod arm;
pub mod arm_id;
pub mod auth;
pub mod casing;
pub mod config;
pub mod console;
pub mod control;
pub mod error;
pub mod etag;
pub mod expose_headers;
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
pub mod write_merge;

use axum::{
    Router,
    error_handling::HandleErrorLayer,
    middleware::from_fn_with_state,
    routing::{get, post},
};
use state::AppState;
use std::net::SocketAddr;
use std::time::Duration;
use tokio::net::TcpListener;
use tower::{
    BoxError, ServiceBuilder, limit::GlobalConcurrencyLimitLayer, load_shed::LoadShedLayer,
    timeout::TimeoutLayer,
};

/// Build the axum router. Registers `GET /subscriptions`, the
/// `/subscriptions/{sub}/resourceGroups` and `/subscriptions/{sub}/resources`
/// paginated list routes, and the RG-scoped
/// `/subscriptions/{sub}/resourceGroups/{rg}/resources` route. The
/// Bearer layer is applied at the ARM router level
/// so EVERY ARM route is gated. Param routes use the axum 0.8
/// `/{param}` curly-brace syntax convention.
///
/// The `/_console` dashboard sub-router is merged in SEPARATELY, outside both the
/// bearer layer (it must load in a plain browser) and the metrics layer (its own
/// polling/SSE traffic must not pollute the activity feed). The `record_metrics`
/// layer wraps the bearer layer so the recorded status reflects the final response,
/// including 401s.
pub fn build_router(state: AppState) -> Router {
    // Delegate to the pre-merge ARM baseline, then add the `/_sim` surface AND the `/ui`
    // Web Console SPA on the SAME bearer-exempt seam. Keeping the
    // composition in `build_router_without_sim` gives the contract test a GENUINE pre-merge
    // router to compare against, so `arm_byte_identical` can detect a merge regression
    // instead of asserting X == X. `/ui` is a FRESH nested prefix with its OWN
    // scoped fallback (see [`ui::router`]) — it cannot shadow an ARM route, and the
    // fallback-free `arm` router never hits the two-fallbacks merge panic. `ui::router()`
    // takes NO state (the SPA assets are static, embedded via `include_dir!`).
    // Merge the `/_control` write surface ONLY when the server is
    // armed (`state.control` is `Some`). A disarmed server exposes NO `/_control/*` routes
    // (they 404, not 403). `/_control` is a FRESH `nest` prefix on this SAME bearer-exempt
    // seam, off the ARM bearer/metrics layers, so merging it CANNOT change ARM response
    // bytes — `arm_byte_identical` stays green. It carries its OWN control-token
    // gate (see [`control::router`]), a distinct realm from the any-Bearer ARM model.
    let mut r = build_router_without_sim(state.clone())
        .merge(sim::router(state.clone()))
        .merge(ui::router());
    if let Some(cp) = state.control.clone() {
        r = r.merge(control::router(cp));
    }
    r
}

/// Execution-budget knobs applied to the whole router by [`apply_execution_budgets`].
/// Constructed from [`config::Cli`] in `main`. The DB budgets (`statement_timeout` /
/// `acquire_timeout`) are applied at pool-build time in `main`, so they are baked into the
/// pool rather than carried here.
#[derive(Clone, Copy, Debug)]
pub struct Budgets {
    /// Global per-request wall-clock deadline (elapsed → ARM 504 GatewayTimeout).
    pub request_timeout: Duration,
    /// Max concurrent in-flight requests; excess is SHED (not queued) → ARM 503 + Retry-After.
    pub concurrency_limit: usize,
}

/// Wrap `router` with the execution-budget middleware stack (resource-exhaustion guards).
///
/// Layer order (outermost → innermost), per the load-shed contract:
///   * `HandleErrorLayer` (error mapper) — OUTSIDE both limiters, so it catches the errors
///     they raise and turns them into ARM `CloudError` responses (never a bare status).
///   * `LoadShed` — IMMEDIATELY outside the concurrency limiter, with NO `Buffer` between
///     them, so a full limiter sheds INSTANTLY (503) instead of queueing the request.
///   * `GlobalConcurrencyLimit` — a GENUINELY server-wide cap: the `Global` variant shares
///     ONE semaphore across every cloned service (per-connection clones included), unlike
///     the plain `ConcurrencyLimitLayer`, whose per-clone permit pool would NOT bound total
///     in-flight requests.
///   * `Timeout` — innermost, so a timed-out request still RELEASES its concurrency permit
///     as its future resolves (permits release on completion, handler error, AND timeout).
///
/// Applied inside the shared serve path ([`serve_dual`]) AND directly by the budget tests,
/// so the guards are always exercised through the real middleware.
pub fn apply_execution_budgets(router: Router, budgets: Budgets) -> Router {
    router.layer(
        ServiceBuilder::new()
            .layer(HandleErrorLayer::new(map_budget_error))
            .layer(LoadShedLayer::new())
            .layer(GlobalConcurrencyLimitLayer::new(budgets.concurrency_limit))
            .layer(TimeoutLayer::new(budgets.request_timeout)),
    )
}

/// Map the `BoxError` the budget middleware raises into an ARM `CloudError` response: a shed
/// request (`LoadShed` `Overloaded`) → 503 ServiceUnavailable (+ `Retry-After: 1`); an
/// elapsed request timeout (`Elapsed`) → 504 GatewayTimeout; anything else → a generic 500.
/// Never leaks internals — the shaped variants carry fixed messages.
async fn map_budget_error(err: BoxError) -> error::ApiError {
    use error::ApiError;
    if err.is::<tower::load_shed::error::Overloaded>() {
        ApiError::ServiceUnavailable
    } else if err.is::<tower::timeout::error::Elapsed>() {
        ApiError::GatewayTimeout
    } else {
        ApiError::Internal(format!("budget middleware error: {err}"))
    }
}

/// The pre-merge ARM baseline (a test seam): the FULL runtime router MINUS the
/// `/_sim` merge. Builds the `arm` chain (with its `bearer_auth` + `record_metrics` layers)
/// and merges the two other bearer-exempt sub-routers (`/_console` and `/token` + JWKS) —
/// but does NOT `.merge(sim::router)`, so it exposes NO `/_sim` surface.
///
/// [`build_router`] delegates here and then adds `.merge(sim::router(state))`, so this is
/// the exact router that served the ARM surface before `/_sim` was added. `arm_byte_identical`
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
        // Read-only single-RG detail (ETag emission for the RG kind). An EXACT path with
        // no trailing segment — distinct from `{rg}/resources` and the `{rg}/providers/{*tail}`
        // catch-all below, so it registers with no static-vs-wildcard overlap. READ-ONLY for
        // now (no RG write path / overlay writer yet).
        .route(
            "/subscriptions/{sub}/resourceGroups/{rg}",
            // READ-ONLY: GET returns the RG detail. D-13/D-18: write methods return the
            // EXPLICIT ARM 405 envelope + `Allow: GET, HEAD` (never axum's implicit 405) even
            // when `--enable-arm-writes` is ON — resource-group CRUD is Phase 24.
            get(handlers::get_resource_group_detail)
                .put(handlers::rg_write_method_not_allowed)
                .patch(handlers::rg_write_method_not_allowed)
                .delete(handlers::rg_write_method_not_allowed),
        )
        .route(
            "/subscriptions/{sub}/resources",
            get(handlers::list_resources),
        )
        .route(
            "/subscriptions/{sub}/resourceGroups/{rg}/resources",
            get(handlers::list_rg_resources),
        )
        // Cost Management Query — sub scope. Registered INSIDE `arm` (above
        // the bearer/metrics layers) so it inherits the any-Bearer scanner contract.
        // The sub-scope path has no catch-all, so it registers
        // directly with no static-vs-wildcard overlap.
        .route(
            "/subscriptions/{sub}/providers/Microsoft.CostManagement/query",
            post(handlers::cost_query),
        )
        // Microsoft.Authorization data plane — three sub-scoped GET routes
        // registered INSIDE `arm` (above the bearer/metrics layers) so they inherit the
        // any-Bearer scanner contract + the `--enforce-auth` swap. These are static
        // `providers/...` paths (the SAME shape the cost sub-scope route already proved
        // registers cleanly — the only `{*tail}` catch-all is RG-scoped). Any api-version
        // is accepted/ignored.
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
        // Simulator-only drift audit reads — three GET routes registered
        // INSIDE `arm` (above the bearer/metrics layers) so they sit INSIDE the bearer
        // gate: missing Bearer → 401, any non-empty Bearer → 200 (enforce off),
        // valid RS256 JWT under `--enforce-auth`. These are NOT merged via the outer
        // `arm.merge(...)` bearer-exempt path — only `/token`+JWKS+`/_console` stay
        // exempt. `/simulator` is a fresh prefix (no static-vs-wildcard overlap);
        // the by-resource route uses a `{*resource_id}` catch-all because ARM ids
        // contain `/`. The audit data served here is NEVER injected into ARM bodies
        // — this is the only drift-audit surface.
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
        // Generic ARM write plane (PUT/PATCH/DELETE) method-merged onto the SAME catch-all as
        // the detail GET + RG-scoped cost POST — one path, no static-vs-wildcard overlap. The
        // handlers are registered ALWAYS (not conditionally on the flag) so a disabled write
        // returns the controlled ARM 405 + `Allow` header (D-10) instead of axum's implicit
        // 405; they sit INSIDE the bearer layer below so authn precedes any body (WAUTH-02/D-11).
        .route(
            "/subscriptions/{sub}/resourceGroups/{rg}/providers/{*tail}",
            get(handlers::get_resource_detail)
                .post(handlers::cost_query_scoped)
                .put(handlers::put_resource)
                .patch(handlers::patch_resource)
                .delete(handlers::delete_resource),
        )
        // D-25: append `Access-Control-Expose-Headers: ETag` to any arm response carrying an
        // ETag (detail GET/HEAD, PUT/PATCH mutation, DELETE 204) so a browser can read the
        // validator. Innermost of the three layers so it observes the handler's ETag directly;
        // no state, no new dependency, no CORS-origin policy.
        .layer(axum::middleware::from_fn(
            expose_headers::expose_etag_header,
        ))
        .layer(from_fn_with_state(state.clone(), auth::bearer_auth))
        .layer(from_fn_with_state(state.clone(), metrics::record_metrics))
        .with_state(state.clone());

    // The `/_console` dashboard AND the token mint + JWKS are merged
    // OUTSIDE the bearer layer: the console must load in a plain browser, and
    // `/token` + JWKS must be reachable with NO auth header to bootstrap a token
    // even when `--enforce-auth` is ON (the token-to-get-a-token deadlock
    // avoidance). Neither sub-router inherits the `bearer_auth`/`record_metrics`
    // layers above.
    // NOTE: `/_sim` is NOT merged here — it is added by the caller
    // [`build_router`] on this SAME bearer-exempt, uninstrumented seam. Keeping the `/_sim`
    // merge out of this baseline is exactly what lets `arm_byte_identical` compare a genuine
    // pre-merge router against the merged one. `/_sim` sits on the exempt seam — NOT
    // inside `arm` (that is where the drift audit reads sit, INSIDE the bearer gate,
    // deliberately not mirrored) — and is a fresh `nest("/_sim", …)` prefix with its
    // own scoped JSON-404 fallback, so it cannot shadow an ARM route and the `arm`
    // router (which keeps NO fallback) never hits the two-fallbacks merge panic.
    arm.merge(console::router(state.clone()))
        .merge(handlers::token::router(state))
}

/// Bind and serve the mock server.
///
/// The default (`tls == false`) path is a single plain-HTTP bind: one
/// [`build_router`] served by `axum::serve` on `{host}:{http_port}`. The `host`
/// defaults to loopback `127.0.0.1`; pass `0.0.0.0` to bind every
/// interface. Nothing touches `tls_port` and no cert is generated — this
/// preserves the any-Bearer HTTP scanner contract.
///
/// When `tls == true`, the SAME `Router` is ALSO served over HTTPS on
/// `{host}:{tls_port}` via `axum_server::bind_rustls`, using an **ephemeral
/// in-memory** self-signed cert (CN/SAN = `localhost`, `127.0.0.1`) generated fresh
/// at startup by `rcgen` — never written to disk. Both listeners run
/// concurrently over one `tokio::try_join!`; either erroring brings the process down.
///
/// Extracted from `main.rs` so integration tests can drive the real dual bind
/// (`tests/tls.rs`). One rustls stack only: the `ring` provider (see `Cargo.toml`).
/// Build a `host:port` bind address. The default host is the
/// loopback `127.0.0.1`; an explicit `0.0.0.0` binds all interfaces. Pure +
/// std-only so it is unit-testable DB-free.
pub fn bind_addr(host: &str, port: u16) -> String {
    format!("{host}:{port}")
}

/// Apply a schema-migration batch with the runtime `statement_timeout` DISABLED for its
/// duration. The startup preflight runs on the SAME pool the request handlers use, whose
/// connections carry the server-wide session `statement_timeout` (`DB_STATEMENT_TIMEOUT_MS`)
/// — so a legitimate first-run upgrade over a large estate (index or future constraint
/// creation) could otherwise be CANCELLED after that budget elapses and prevent the server
/// from starting. Running the DDL inside a transaction that first issues
/// `SET LOCAL statement_timeout = 0` exempts ONLY this migration batch: the setting is
/// transaction-scoped and reverts on commit, so the connection returns to the pool with the
/// runtime budget intact (no leak to later request handlers).
///
/// Uses `sqlx::raw_sql` (the simple-query protocol) so the multi-statement DDL + any guarded
/// `DO $$ … $$` blocks execute as one unsplit batch inside the transaction — we never
/// parse/split the SQL ourselves (mirrors the Python twins in `writer`).
pub async fn apply_schema_batch(pool: &sqlx::PgPool, sql: &str) -> Result<(), sqlx::Error> {
    let mut tx = pool.begin().await?;
    // LOCAL ⇒ transaction-scoped: it disables the timeout only for this migration batch and
    // reverts on commit, never leaking a disabled timeout back onto the pooled connection.
    sqlx::query("SET LOCAL statement_timeout = 0")
        .execute(&mut *tx)
        .await?;
    sqlx::raw_sql(sql).execute(&mut *tx).await?;
    tx.commit().await?;
    Ok(())
}

/// Idempotently provision the identity tables (`synthetic.principals`,
/// `synthetic.role_assignments`) by applying `sql/005_identity.sql`. Safe to run on
/// every boot: the migration is `CREATE ... IF NOT EXISTS` + a guarded-FK `DO` block,
/// a no-op on an already-migrated schema. This PROVISIONS the (possibly empty) tables
/// so the Microsoft.Authorization/roleAssignments read returns `[]` on an
/// identity-less tenant — it never masks a missing relation as empty business data.
/// Requires the `synthetic` schema to already exist (the caller confirms a tenant first).
///
/// Applied via [`apply_schema_batch`], so the DDL runs with the runtime `statement_timeout`
/// disabled (a first-run index/constraint build cannot be cancelled by the request budget).
pub async fn ensure_identity_schema(pool: &sqlx::PgPool) -> Result<(), sqlx::Error> {
    const SQL_005: &str = include_str!("../../sql/005_identity.sql");
    apply_schema_batch(pool, SQL_005).await
}

/// Idempotently provision the drift tables (`synthetic.drift_batches`,
/// `synthetic.drift_records`) AND the `synthetic.resources.drift_deleted_at`
/// soft-delete column by applying `sql/006_drift.sql`. Safe to run on every boot:
/// the migration is `CREATE ... IF NOT EXISTS` + `ADD COLUMN IF NOT EXISTS` + a
/// guarded-FK `DO` block, a no-op on an already-migrated schema. This PROVISIONS the
/// `drift_deleted_at` column so the list/detail soft-delete filter
/// (`AND drift_deleted_at IS NULL`) never references a missing relation/column on a
/// volume provisioned before the drift schema existed — it never masks a missing
/// column as empty business data. Requires the `synthetic` schema to already exist
/// (the caller confirms a tenant first).
///
/// Applied via [`apply_schema_batch`] (runtime `statement_timeout` disabled for the batch),
/// mirroring the Python twin `writer.ensure_drift_schema` and [`ensure_identity_schema`].
pub async fn ensure_drift_schema(pool: &sqlx::PgPool) -> Result<(), sqlx::Error> {
    const SQL_006: &str = include_str!("../../sql/006_drift.sql");
    apply_schema_batch(pool, SQL_006).await
}

/// Idempotently provision the Web Console metadata column
/// (`synthetic.tenant.profile_name`) by applying `sql/007_web_metadata.sql`. Safe to run
/// on every boot: the migration is a single `ADD COLUMN IF NOT EXISTS`, a no-op on an
/// already-migrated schema. This PROVISIONS the nullable `profile_name` column so the
/// `/_sim/summary` handler's `SELECT ... profile_name ...` never references a missing
/// column on a volume provisioned before this column existed — an un-set column
/// simply reads NULL (⇒ `profile: null`), it never masks a missing column as empty data.
/// Requires the `synthetic` schema to already exist (the caller confirms a tenant first).
///
/// Applied via [`apply_schema_batch`] (runtime `statement_timeout` disabled for the batch),
/// mirroring the Python twin `writer.ensure_web_metadata_schema` and [`ensure_drift_schema`].
pub async fn ensure_web_metadata_schema(pool: &sqlx::PgPool) -> Result<(), sqlx::Error> {
    const SQL_007: &str = include_str!("../../sql/007_web_metadata.sql");
    apply_schema_batch(pool, SQL_007).await
}

/// Idempotently provision the ARM overlay substrate by applying
/// `sql/009_arm_overlay.sql`: the `synthetic.arm_overlay` table, the unowned
/// `arm_overlay_revision_seq` sequence, the `arm_overlay_set_revision` trigger, and the twelve
/// NAMED row-model CHECK constraints.
///
/// Safe to run on every boot. The migration uses `CREATE ... IF NOT EXISTS`, `CREATE OR
/// REPLACE FUNCTION`, a guarded (existence-checked) `CREATE TRIGGER`, and guarded `DO`
/// constraint blocks, so it is a no-op on an already-migrated schema; the preamble prepended
/// by this function takes a transaction-scoped advisory lock so four or more racing boots all
/// succeed. Requires the `synthetic` schema to already exist (the caller confirms a tenant
/// first).
///
/// Boundary: this only PROVISIONS the substrate. No reader consults it, no drift
/// path writes it, and no ETag header is emitted.
///
/// Applied via [`apply_schema_batch`] (runtime `statement_timeout` disabled for the batch),
/// with a transaction-scoped preamble — a bounded `lock_timeout` + the serializing advisory
/// lock — prepended HERE rather than in the `.sql` file, so `sql/009` stays honest under
/// Docker initdb autocommit. Mirrors the Python twin
/// `writer.ensure_arm_overlay_schema`. After the DDL applies, [`arm_overlay_inventory`]
/// deep-verifies the resulting schema — actual constraint DEFINITIONS **and behavioural
/// probes** — and fails boot loudly if a malformed pre-existing `arm_overlay` table was left
/// intact by `CREATE TABLE IF NOT EXISTS`.
pub async fn ensure_arm_overlay_schema(pool: &sqlx::PgPool) -> Result<(), sqlx::Error> {
    const SQL_009: &str = include_str!("../../sql/009_arm_overlay.sql");
    // Transaction-scoped preamble RELOCATED here from sql/009. `apply_schema_batch`
    // wraps the whole batch in ONE explicit transaction, so a bounded `lock_timeout` and the
    // serializing advisory lock take effect and cover the DDL — while sql/009 stays pure,
    // idempotent DDL that is honest under Docker's initdb autocommit path (where a
    // transaction-scoped statement is a silent no-op). The advisory lock serializes concurrent
    // first-boot applies (>=4 racing tasks all return Ok); `lock_timeout` bounds any
    // ACCESS-EXCLUSIVE wait. Both revert/release on commit — no leak back to the pooled conn.
    const PREAMBLE: &str = "SET LOCAL lock_timeout = '3s';\n\
        SELECT pg_advisory_xact_lock(hashtext('synthetic.arm_overlay:009'));\n";
    apply_schema_batch(pool, &format!("{PREAMBLE}{SQL_009}")).await?;
    arm_overlay_inventory(pool)
        .await
        .map_err(sqlx::Error::Protocol)
}

/// Idempotently provision the ARM-ID identity fold functions by applying
/// `sql/011_arm_id_key.sql`: the `synthetic.ascii_fold(text)` primitive and the
/// `synthetic.arm_id_key(text)` whole-ID wrapper, both `IMMUTABLE STRICT` `translate()`
/// functions (INV-01, D-01/D-02/D-28).
///
/// Safe to run on every boot. The migration is `CREATE OR REPLACE FUNCTION` only, so it is
/// a no-op-equivalent re-definition on an already-migrated schema; it takes NO table lock,
/// touches NOTHING on the populated `synthetic.resources` table, and needs no advisory-lock
/// preamble (function redefinition does not contend). Requires the `synthetic` schema to
/// already exist (the caller confirms a tenant first).
///
/// ADDITIVE + behaviour-neutral (D-22a): this ONLY defines the two functions. It changes NO
/// CHECK, builds NO index, edits NO view, and cuts over NO predicate. In THIS unit NOTHING
/// consumes the functions — `sql/010` still references `lower(...)` and is UNCHANGED. It runs
/// at boot AFTER [`ensure_arm_overlay_schema`] (sql/009) and BEFORE [`ensure_arm_resolver_schema`]
/// (sql/010) purely so the functions EXIST before any future sql/010 that references
/// `arm_id_key` (the later predicate cutover) is applied against an upgraded volume — the boot-safety guarantee.
/// No `011 -> audit -> 012 -> 010` cutover ordering is wired here.
///
/// Applied via [`apply_schema_batch`] (runtime `statement_timeout` disabled for the batch),
/// mirroring the Python twin `writer.ensure_arm_id_key_schema`.
pub async fn ensure_arm_id_key_schema(pool: &sqlx::PgPool) -> Result<(), sqlx::Error> {
    const SQL_011: &str = include_str!("../../sql/011_arm_id_key.sql");
    apply_schema_batch(pool, SQL_011).await
}

/// Idempotently provision the resolver substrate by applying `sql/010_arm_resolver.sql`:
/// the `storage_mode` provenance column on `synthetic.drift_batches`, the two per-kind
/// resolved views (`synthetic.arm_resolved_resources` — the liveness authority —
/// and `synthetic.arm_resolved_resource_groups`), and the `(target_kind, id_lower)` overlay
/// resolution index.
///
/// Safe to run on every boot. The migration uses `CREATE OR REPLACE VIEW`, `CREATE INDEX
/// IF NOT EXISTS`, and a guarded `DO` block for the column, so it is a no-op on an
/// already-migrated schema; the preamble prepended by this function takes a
/// transaction-scoped advisory lock (a key DISTINCT from the sql/009 key) so racing boots
/// all succeed. It touches NOTHING on the populated `synthetic.resources` table.
///
/// Requires `ensure_arm_overlay_schema` (the views union against `synthetic.arm_overlay`
/// and the index is built on it) and the base + drift schemas to already exist — so this
/// runs AFTER `ensure_arm_overlay_schema` and BEFORE `AppState`/`serve_dual`.
///
/// Boundary: this only PROVISIONS the resolver seam. It changes NO reader and NO
/// writer — the ARM handler `FROM`-swap and the drift re-point land in later releases.
///
/// Applied via [`apply_schema_batch`] (runtime `statement_timeout` disabled for the batch),
/// with a transaction-scoped preamble prepended HERE rather than in the `.sql` file so
/// `sql/010` stays honest under Docker initdb autocommit. Mirrors the Python twin
/// `writer.ensure_arm_resolver_schema`. After the DDL applies, [`arm_resolver_inventory`]
/// deep-verifies the resulting views/column-types/column/index and fails boot loudly if a
/// malformed pre-existing object was left intact.
pub async fn ensure_arm_resolver_schema(pool: &sqlx::PgPool) -> Result<(), sqlx::Error> {
    const SQL_010: &str = include_str!("../../sql/010_arm_resolver.sql");
    // Transaction-scoped preamble (see `ensure_arm_overlay_schema`) — a DISTINCT advisory
    // key from the 009 key so a 009 apply and a 010 apply do not needlessly serialize, while
    // concurrent 010 applies still serialize with each other. Both revert/release on commit.
    const PREAMBLE: &str = "SET LOCAL lock_timeout = '3s';\n\
        SELECT pg_advisory_xact_lock(hashtext('synthetic.arm_resolver:010'));\n";
    apply_schema_batch(pool, &format!("{PREAMBLE}{SQL_010}")).await?;
    arm_resolver_inventory(pool)
        .await
        .map_err(sqlx::Error::Protocol)
}

/// Deep structural-completeness inventory for the resolver substrate. Because
/// `CREATE OR REPLACE VIEW` silently keeps a mis-shaped pre-existing view (and a re-apply
/// can leave a stale definition from an aborted upgrade), boot must independently verify
/// every required element and fail LOUDLY (naming the first bad element)
/// rather than serve on a corrupt resolver seam:
///   * both resolved views exist (`to_regclass`);
///   * each view exposes its EXACT enumerated typed column contract (so a missing or
///     mistyped column that would break `sqlx::query_as::<_, ResourceRow>` decode is
///     caught) — views carry no NOT NULL, so only column name + `data_type` are asserted;
///   * `synthetic.drift_batches.storage_mode` exists as `TEXT NOT NULL DEFAULT 'synthetic'`;
///   * the `idx_arm_overlay_kind_id` overlay resolution index exists.
///
/// Returns `Ok(())` on a correctly-provisioned substrate; `Err(String)` naming the first
/// missing / mistyped element otherwise. All catalog queries are PG11-safe.
pub async fn arm_resolver_inventory(pool: &sqlx::PgPool) -> Result<(), String> {
    // --- 1. Both resolved views exist -----------------------------------------------------
    for view in ["arm_resolved_resources", "arm_resolved_resource_groups"] {
        let reg: Option<String> = sqlx::query_scalar("SELECT to_regclass($1)::text")
            .bind(format!("synthetic.{view}"))
            .fetch_one(pool)
            .await
            .map_err(|e| format!("arm_resolver inventory: probing view {view} failed: {e}"))?;
        if reg.is_none() {
            return Err(format!(
                "arm_resolver inventory: missing view synthetic.{view}"
            ));
        }
    }

    // --- 2. Each view's EXACT enumerated typed column contract ----------------------------
    // The resource view MUST decode into `ResourceRow`'s 8-column SELECT (id, name, type,
    // location, tags, sku, kind, properties) via `->>`(text)/`->`(jsonb); the internal
    // scope/state columns are carried too. A UNION-ALL view resolves each column to a
    // single type, so `information_schema.columns.data_type` is the byte-decode contract.
    let resource_cols: &[(&str, &str)] = &[
        ("id", "text"),
        ("name", "text"),
        ("type", "text"),
        ("location", "text"),
        ("tags", "jsonb"),
        ("sku", "jsonb"),
        ("kind", "text"),
        ("properties", "jsonb"),
        ("subscription_id", "uuid"),
        ("resource_group_name", "text"),
        ("provisioning_state", "text"),
        ("managed_by", "text"),
    ];
    verify_view_columns(pool, "arm_resolved_resources", resource_cols).await?;

    let rg_cols: &[(&str, &str)] = &[
        ("id", "text"),
        ("name", "text"),
        ("location", "text"),
        ("tags", "jsonb"),
        ("provisioning_state", "text"),
        ("subscription_id", "uuid"),
    ];
    verify_view_columns(pool, "arm_resolved_resource_groups", rg_cols).await?;

    // --- 3. drift_batches.storage_mode: TEXT NOT NULL DEFAULT 'synthetic' -----------------
    let sm: Option<(String, String, Option<String>)> = sqlx::query_as(
        "SELECT data_type, is_nullable, column_default FROM information_schema.columns \
         WHERE table_schema = 'synthetic' AND table_name = 'drift_batches' \
           AND column_name = 'storage_mode'",
    )
    .fetch_optional(pool)
    .await
    .map_err(|e| format!("arm_resolver inventory: probing storage_mode failed: {e}"))?;
    match sm {
        None => {
            return Err(
                "arm_resolver inventory: missing column synthetic.drift_batches.storage_mode"
                    .to_string(),
            );
        }
        Some((data_type, is_nullable, default)) => {
            if data_type != "text" {
                return Err(format!(
                    "arm_resolver inventory: storage_mode has type {data_type:?}, expected \"text\""
                ));
            }
            if is_nullable != "NO" {
                return Err(format!(
                    "arm_resolver inventory: storage_mode is_nullable={is_nullable:?}, expected \"NO\""
                ));
            }
            // The FAIL-SAFE default: an unstamped batch reads as legacy ('synthetic') and
            // trips the boot guard rather than being silently trusted as overlay.
            match default {
                Some(d) if d.contains("synthetic") => {}
                other => {
                    return Err(format!(
                        "arm_resolver inventory: storage_mode default {other:?} must default to 'synthetic'"
                    ));
                }
            }
        }
    }

    // --- 4. The overlay resolution index exists -------------------------------------------
    let idx: Option<String> =
        sqlx::query_scalar("SELECT to_regclass('synthetic.idx_arm_overlay_kind_id')::text")
            .fetch_one(pool)
            .await
            .map_err(|e| format!("arm_resolver inventory: probing overlay index failed: {e}"))?;
    if idx.is_none() {
        return Err(
            "arm_resolver inventory: missing index synthetic.idx_arm_overlay_kind_id".to_string(),
        );
    }

    Ok(())
}

/// Verify a resolved view exposes exactly the expected enumerated columns with the expected
/// `information_schema.columns.data_type`, failing loudly on the first missing / mistyped
/// column. Views carry no NOT NULL, so nullability is intentionally NOT asserted.
async fn verify_view_columns(
    pool: &sqlx::PgPool,
    view: &str,
    expected: &[(&str, &str)],
) -> Result<(), String> {
    for (col, ty) in expected {
        let data_type: Option<String> = sqlx::query_scalar(
            "SELECT data_type FROM information_schema.columns \
             WHERE table_schema = 'synthetic' AND table_name = $1 AND column_name = $2",
        )
        .bind(view)
        .bind(col)
        .fetch_optional(pool)
        .await
        .map_err(|e| format!("arm_resolver inventory: probing {view}.{col} failed: {e}"))?;
        match data_type {
            None => {
                return Err(format!(
                    "arm_resolver inventory: view {view} missing column {col}"
                ));
            }
            Some(dt) if dt != *ty => {
                return Err(format!(
                    "arm_resolver inventory: view {view} column {col} has type {dt:?}, expected {ty:?}"
                ));
            }
            Some(_) => {}
        }
    }
    Ok(())
}

/// The provenance-based fail-closed boot-guard probe SQL. A SINGLE read-only round trip:
/// two `EXISTS` sub-selects OR'd together. Extracted as a `const` so a DB-free unit test
/// (`assert_no_legacy_inplace_drift_query_shape`) can assert its shape — both EXISTS clauses,
/// the mandatory `storage_mode = 'synthetic'` provenance conjunct, and ZERO DDL keywords — so a
/// refactor can never silently turn it into an index-creating / table-altering statement that
/// reintroduces the ACCESS-EXCLUSIVE startup deadlock.
///
/// Both sub-selects run under `ACCESS SHARE` only. On a clean post-cutover tenant
/// `drift_deleted_at` is all-NULL, so the second EXISTS seq-scans `synthetic.resources` once
/// (read-only, ~sub-second at 500K) — acceptable for a one-time boot check. NO index is added.
const D12_GUARD_PROBE_SQL: &str = "SELECT \
    EXISTS(SELECT 1 FROM synthetic.drift_batches \
           WHERE storage_mode = 'synthetic' AND reverted_at IS NULL) \
    OR \
    EXISTS(SELECT 1 FROM synthetic.resources WHERE drift_deleted_at IS NOT NULL)"; // SYNRES-ALLOW[schema/provisioning]: fail-closed boot guard probes the raw baseline for the retired drift_deleted_at column

/// Provenance-based FAIL-CLOSED boot guard (mandatory safety net for the
/// reset-cutover). This release does NOT migrate historical in-place drift; instead, a tenant still
/// carrying legacy in-place drift (applied by the pre-v3 binary) must refuse to boot rather than
/// silently serve stale/invisible drift.
///
/// Fails closed iff there is an ACTIVE legacy in-place drift batch
/// (`storage_mode = 'synthetic' AND reverted_at IS NULL`) OR any `synthetic.resources` row with
/// `drift_deleted_at IS NOT NULL`. The `storage_mode = 'synthetic'` conjunct is NON-NEGOTIABLE:
/// an active OVERLAY batch (`storage_mode = 'overlay'`, the new apply-drift path) must NOT
/// trip the guard — a `reverted_at IS NULL`-only probe would brick a valid
/// post-cutover tenant on the first restart after legitimate overlay drift. The
/// provenance marker is exactly what lets a valid overlay-drift tenant boot.
///
/// On a dirty tenant returns `Err` carrying the EXACT locked message, so the caller propagates
/// it and `serve_dual` is never reached (the server never binds). The probe is a single
/// read-only round trip that issues NO DDL — no `ALTER`, no `CREATE INDEX` — so it cannot
/// reintroduce the ACCESS-EXCLUSIVE startup deadlock. Runs at boot AFTER
/// [`ensure_arm_resolver_schema`] (needs `storage_mode`) and BEFORE building `AppState`.
pub async fn assert_no_legacy_inplace_drift(pool: &sqlx::PgPool) -> Result<(), String> {
    let dirty: bool = sqlx::query_scalar(D12_GUARD_PROBE_SQL)
        .fetch_one(pool)
        .await
        .map_err(|e| format!("fail-closed boot guard probe failed: {e}"))?;
    if dirty {
        // Byte-exact locked message (do NOT reword) — the operator's actionable next step.
        return Err(
            "Applied in-place drift detected. Revert drift or regenerate the tenant before \
             restarting."
                .to_string(),
        );
    }
    Ok(())
}

/// Deep structural-completeness inventory for `synthetic.arm_overlay`. Because
/// `CREATE TABLE IF NOT EXISTS` silently leaves a malformed pre-existing table intact, boot
/// must independently verify every required element — actual constraint DEFINITIONS (so a
/// same-named `CHECK (true)` stub is caught), column types + nullability, the sequence's
/// type / `NO CYCLE` / unowned-ness, the revision trigger (flags AND that it invokes
/// `arm_overlay_set_revision()` AND that it strictly advances revisions), and the `id_lower`
/// primary key — and fail LOUDLY (naming the first bad element) rather than serve on a corrupt
/// substrate.
///
/// Returns `Ok(())` on a correctly-provisioned table; `Err(String)` naming the first
/// missing / stubbed / mistyped element otherwise. All catalog queries are PG11-safe.
pub async fn arm_overlay_inventory(pool: &sqlx::PgPool) -> Result<(), String> {
    // --- 1. NAMED CHECK constraint DEFINITIONS (not just names) ---------------------------
    // Each expected constraint must exist AND its `pg_get_constraintdef` text must contain a
    // characteristic predicate fragment, so a same-named `CHECK (true)` stub fails.
    let expected_checks: &[(&str, &str)] = &[
        ("ck_arm_overlay_id_lower", "lower(id)"),
        ("ck_arm_overlay_kind", "resource_group"),
        ("ck_arm_overlay_source", "drift"),
        ("ck_arm_overlay_revision_pos", "revision > 0"),
        ("ck_arm_overlay_present_body", "body IS NOT NULL"),
        ("ck_arm_overlay_body_nonempty", "'{}'"),
        ("ck_arm_overlay_body_id_agree", "'id'"),
        ("ck_arm_overlay_envelope", "jsonb_typeof"),
        (
            "ck_arm_overlay_kind_shape",
            "Microsoft.Resources/resourceGroups",
        ),
        // minimum-complete-snapshot invariants.
        ("ck_arm_overlay_tags", "tags"),
        ("ck_arm_overlay_optional_types", "sku"),
        ("ck_arm_overlay_rg_provisioning_state", "provisioningState"),
    ];
    for (name, needle) in expected_checks {
        let def: Option<String> = sqlx::query_scalar(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint \
             WHERE conrelid = 'synthetic.arm_overlay'::regclass \
               AND contype = 'c' AND conname = $1",
        )
        .bind(name)
        .fetch_optional(pool)
        .await
        .map_err(|e| format!("arm_overlay inventory: probing CHECK {name} failed: {e}"))?;
        match def {
            None => {
                return Err(format!(
                    "arm_overlay inventory: missing CHECK constraint {name}"
                ));
            }
            Some(d) if !d.contains(needle) => {
                return Err(format!(
                    "arm_overlay inventory: CHECK {name} definition is stubbed/wrong \
                     (expected to contain {needle:?}, got {d:?})"
                ));
            }
            Some(_) => {}
        }
    }

    // --- 1b. EXACT-definition checks for the two constraints that cannot be behaviourally
    // isolated. `ck_arm_overlay_revision_pos`: the BEFORE trigger overwrites
    // any caller `revision` before a probe could force <= 0. `ck_arm_overlay_body_nonempty`: an
    // empty `{}` body is ALSO rejected by the envelope/tags CHECKs, so a probe cannot attribute
    // the rejection to it. For BOTH, a vacuous rewrite (`CHECK (true OR <needle>)`) would still
    // contain the substring needle above — so assert the NORMALISED definition EXACTLY (strip
    // whitespace + lowercase for PG11/16 parity), which a vacuous/stubbed form cannot match.
    let exact_checks: &[(&str, &str)] = &[
        ("ck_arm_overlay_revision_pos", "check((revision>0))"),
        (
            "ck_arm_overlay_body_nonempty",
            "check(((bodyisnull)or(body<>'{}'::jsonb)))",
        ),
    ];
    for (name, expected) in exact_checks {
        let def: Option<String> = sqlx::query_scalar(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint \
             WHERE conrelid = 'synthetic.arm_overlay'::regclass \
               AND contype = 'c' AND conname = $1",
        )
        .bind(name)
        .fetch_optional(pool)
        .await
        .map_err(|e| format!("arm_overlay inventory: probing exact CHECK {name} failed: {e}"))?;
        match def {
            None => {
                return Err(format!(
                    "arm_overlay inventory: missing CHECK constraint {name}"
                ));
            }
            Some(d) => {
                let norm: String = d
                    .chars()
                    .filter(|c| !c.is_whitespace())
                    .collect::<String>()
                    .to_lowercase();
                if norm != *expected {
                    return Err(format!(
                        "arm_overlay inventory: CHECK {name} is not its exact expected predicate \
                         (vacuous/stubbed?) — normalised {norm:?}, expected {expected:?}"
                    ));
                }
            }
        }
    }

    // --- 2. Column types + nullability ----------------------------------------------------
    let expected_cols: &[(&str, &str, &str)] = &[
        ("id_lower", "text", "NO"),
        ("id", "text", "NO"),
        ("target_kind", "text", "NO"),
        ("source", "text", "NO"),
        ("present", "boolean", "NO"),
        ("body", "jsonb", "YES"),
        ("revision", "bigint", "NO"),
    ];
    for (col, ty, nullable) in expected_cols {
        let row: Option<(String, String)> = sqlx::query_as(
            "SELECT data_type, is_nullable FROM information_schema.columns \
             WHERE table_schema = 'synthetic' AND table_name = 'arm_overlay' \
               AND column_name = $1",
        )
        .bind(col)
        .fetch_optional(pool)
        .await
        .map_err(|e| format!("arm_overlay inventory: probing column {col} failed: {e}"))?;
        match row {
            None => return Err(format!("arm_overlay inventory: missing column {col}")),
            Some((data_type, is_nullable)) => {
                if data_type != *ty {
                    return Err(format!(
                        "arm_overlay inventory: column {col} has type {data_type:?}, expected {ty:?}"
                    ));
                }
                if is_nullable != *nullable {
                    return Err(format!(
                        "arm_overlay inventory: column {col} is_nullable={is_nullable:?}, expected {nullable:?}"
                    ));
                }
            }
        }
    }

    // --- 3. Sequence: exists, BIGINT, NO CYCLE, and UNOWNED -------------------------------
    let seq: Option<(String, bool, i64)> = sqlx::query_as(
        "SELECT data_type::text, cycle, increment_by FROM pg_sequences \
         WHERE schemaname = 'synthetic' AND sequencename = 'arm_overlay_revision_seq'",
    )
    .fetch_optional(pool)
    .await
    .map_err(|e| format!("arm_overlay inventory: probing sequence failed: {e}"))?;
    match seq {
        None => {
            return Err(
                "arm_overlay inventory: missing sequence synthetic.arm_overlay_revision_seq"
                    .to_string(),
            );
        }
        Some((data_type, cycle, increment_by)) => {
            if data_type != "bigint" {
                return Err(format!(
                    "arm_overlay inventory: revision sequence type is {data_type:?}, expected \"bigint\""
                ));
            }
            if cycle {
                return Err(
                    "arm_overlay inventory: revision sequence is CYCLE, expected NO CYCLE"
                        .to_string(),
                );
            }
            // A non-positive increment would fail to ADVANCE revisions — a
            // negative step would run them backwards, breaking the monotonic-revision contract.
            if increment_by <= 0 {
                return Err(format!(
                    "arm_overlay inventory: revision sequence increment is {increment_by}, \
                     expected a positive step"
                ));
            }
        }
    }
    // Unowned: an OWNED-BY sequence has a pg_depend auto-dependency (deptype 'a') on a table
    // column. A `TRUNCATE ... RESTART IDENTITY` WOULD rewind an owned sequence, so ownership
    // is a correctness failure here.
    let owned: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM pg_depend d \
           JOIN pg_class s ON s.oid = d.objid \
           JOIN pg_namespace n ON n.oid = s.relnamespace \
         WHERE s.relname = 'arm_overlay_revision_seq' AND n.nspname = 'synthetic' \
           AND d.classid = 'pg_class'::regclass AND d.deptype = 'a' AND d.refobjsubid > 0",
    )
    .fetch_one(pool)
    .await
    .map_err(|e| format!("arm_overlay inventory: probing sequence ownership failed: {e}"))?;
    if owned > 0 {
        return Err(
            "arm_overlay inventory: revision sequence is OWNED BY a column (must be unowned so \
             TRUNCATE ... RESTART IDENTITY cannot rewind it)"
                .to_string(),
        );
    }

    // --- 4. Revision trigger: ROW-level BEFORE INSERT OR UPDATE trigger, wired to the right
    // function -----------------------------------------------------------------------------
    // tgtype bitmask: ROW=1, BEFORE=2, INSERT=4, UPDATE=16. Verify all FOUR bits are set — a
    // STATEMENT-level trigger (bit 1 clear) would otherwise certify as valid yet cannot
    // reference NEW.revision at write time. ALSO verify `tgfoid` resolves to
    // `synthetic.arm_overlay_set_revision()`: a same-named, same-shape trigger
    // wired to a DIFFERENT function (e.g. one assigning a constant revision) would pass the bit
    // check yet break the monotonic-revision contract. The `::regprocedure` cast is safe — the
    // function is guaranteed to exist (sql/009 `CREATE OR REPLACE`s it and the table exists). The
    // behavioural monotonic probe below is the belt to this suspenders (it also catches a REPLACED
    // function body that still bears the same name).
    let trig: Option<(i16, bool)> = sqlx::query_as(
        "SELECT tgtype::int2, \
                (tgfoid = 'synthetic.arm_overlay_set_revision()'::regprocedure) AS right_fn \
         FROM pg_trigger \
         WHERE tgrelid = 'synthetic.arm_overlay'::regclass \
           AND tgname = 'trg_arm_overlay_revision' AND NOT tgisinternal",
    )
    .fetch_optional(pool)
    .await
    .map_err(|e| format!("arm_overlay inventory: probing trigger failed: {e}"))?;
    match trig {
        None => {
            return Err(
                "arm_overlay inventory: missing trigger trg_arm_overlay_revision".to_string(),
            );
        }
        Some((tgtype, right_fn)) => {
            let t = tgtype as i32;
            if t & 1 == 0 || t & 2 == 0 || t & 4 == 0 || t & 16 == 0 {
                return Err(format!(
                    "arm_overlay inventory: trigger trg_arm_overlay_revision is not a ROW-level \
                     BEFORE INSERT OR UPDATE trigger (tgtype bitmask {t})"
                ));
            }
            if !right_fn {
                return Err(
                    "arm_overlay inventory: trigger trg_arm_overlay_revision invokes the wrong \
                     trigger function (expected synthetic.arm_overlay_set_revision())"
                        .to_string(),
                );
            }
        }
    }

    // --- 5. id_lower PRIMARY KEY — EXACTLY one column, = id_lower -------------------------
    // A substring check on pg_get_constraintdef would accept a composite `PRIMARY KEY
    // (id_lower, id)`. Enumerate the PK columns and require they are exactly [id_lower].
    let pk_cols: Vec<String> = sqlx::query_scalar(
        "SELECT a.attname::text FROM pg_constraint c \
           JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey) \
         WHERE c.conrelid = 'synthetic.arm_overlay'::regclass AND c.contype = 'p' \
         ORDER BY a.attnum",
    )
    .fetch_all(pool)
    .await
    .map_err(|e| format!("arm_overlay inventory: probing primary key failed: {e}"))?;
    if pk_cols.as_slice() != ["id_lower"] {
        return Err(format!(
            "arm_overlay inventory: PRIMARY KEY must be exactly (id_lower), got {pk_cols:?}"
        ));
    }

    // --- 6. Behavioural CHECK probes (savepoint-and-rollback) -----------------------------
    behavioral_probes(pool).await
}

/// Behavioural CHECK probes. Definition-substring matching in
/// [`arm_overlay_inventory`] can be fooled by a vacuous rewrite that still contains the
/// characteristic needle (e.g. `CHECK (true OR id_lower = lower(id))`). This ALSO exercises the
/// row-model for real: inside a transaction that is ALWAYS rolled back, a fully-valid row must
/// be ACCEPTED and each single-field-invalid row must be REJECTED by exactly its target CHECK.
/// Probe ids are suffixed with the backend pid so concurrent boots never contend on the PK.
async fn behavioral_probes(pool: &sqlx::PgPool) -> Result<(), String> {
    const INS: &str = "INSERT INTO synthetic.arm_overlay \
        (id_lower, id, target_kind, source, present, body) SELECT ";
    const FROM_PID: &str = " FROM (SELECT '__probe_' || pg_backend_pid()::text || '__' AS p) s";
    // The same pid-suffixed probe id as an inline scalar (for the monotonic probe's UPDATE ... WHERE).
    const PID_EXPR: &str = "'__probe_' || pg_backend_pid()::text || '__'";
    // A complete, valid resource body (type is non-RG; tags present; no optional sku/kind).
    const OK_BODY: &str = "jsonb_build_object('id',p,'name','n',\
        'type','Microsoft.Storage/storageAccounts','location','eastus',\
        'tags','{}'::jsonb,'properties','{}'::jsonb)";
    // Single-field-invalid body variants (each isolates ONE constraint).
    const BODY_ID_MISMATCH: &str = "jsonb_build_object('id','__mismatch__','name','n',\
        'type','Microsoft.Storage/storageAccounts','location','eastus',\
        'tags','{}'::jsonb,'properties','{}'::jsonb)";
    const BODY_NO_NAME: &str = "jsonb_build_object('id',p,\
        'type','Microsoft.Storage/storageAccounts','location','eastus',\
        'tags','{}'::jsonb,'properties','{}'::jsonb)";
    const BODY_RG_TYPE: &str = "jsonb_build_object('id',p,'name','n',\
        'type','Microsoft.Resources/resourceGroups','location','eastus',\
        'tags','{}'::jsonb,'properties','{}'::jsonb)";
    const BODY_NO_TAGS: &str = "jsonb_build_object('id',p,'name','n',\
        'type','Microsoft.Storage/storageAccounts','location','eastus',\
        'properties','{}'::jsonb)";
    const BODY_SKU_STR: &str = "jsonb_build_object('id',p,'name','n',\
        'type','Microsoft.Storage/storageAccounts','location','eastus',\
        'tags','{}'::jsonb,'properties','{}'::jsonb,'sku','x')";
    const BODY_RG_NO_PS: &str = "jsonb_build_object('id',p,'name','n',\
        'type','Microsoft.Resources/resourceGroups','location','eastus',\
        'tags','{}'::jsonb,'properties','{}'::jsonb)";
    // Envelope variants beyond missing-name: each drops/mistypes exactly one
    // required served field, isolating `ck_arm_overlay_envelope`.
    const BODY_NO_ID: &str = "jsonb_build_object('name','n',\
        'type','Microsoft.Storage/storageAccounts','location','eastus',\
        'tags','{}'::jsonb,'properties','{}'::jsonb)";
    const BODY_NO_TYPE: &str = "jsonb_build_object('id',p,'name','n',\
        'location','eastus','tags','{}'::jsonb,'properties','{}'::jsonb)";
    const BODY_NO_LOCATION: &str = "jsonb_build_object('id',p,'name','n',\
        'type','Microsoft.Storage/storageAccounts','tags','{}'::jsonb,'properties','{}'::jsonb)";
    // `properties` key entirely ABSENT: distinct from a non-object properties —
    // an "optional properties" weakening (`NOT jsonb_exists(body,'properties') OR ...`) accepts a
    // MISSING key while still rejecting a wrong-typed one, so a dedicated missing-key probe is
    // required. Isolates the envelope's properties presence requirement.
    const BODY_NO_PROPERTIES: &str = "jsonb_build_object('id',p,'name','n',\
        'type','Microsoft.Storage/storageAccounts','location','eastus','tags','{}'::jsonb)";
    const BODY_NAME_INT: &str = "jsonb_build_object('id',p,'name',5,\
        'type','Microsoft.Storage/storageAccounts','location','eastus',\
        'tags','{}'::jsonb,'properties','{}'::jsonb)";
    // `kind` present but not a string — the second half of `ck_arm_overlay_optional_types`.
    const BODY_KIND_INT: &str = "jsonb_build_object('id',p,'name','n',\
        'type','Microsoft.Storage/storageAccounts','location','eastus',\
        'tags','{}'::jsonb,'properties','{}'::jsonb,'kind',5)";
    // A resource_group row whose body `type` is NOT the RG constant (the RG direction of
    // `ck_arm_overlay_kind_shape`); provisioningState present so ONLY kind_shape rejects it.
    const BODY_RG_WRONGTYPE: &str = "jsonb_build_object('id',p,'name','n',\
        'type','Microsoft.Storage/storageAccounts','location','eastus',\
        'tags','{}'::jsonb,'properties',jsonb_build_object('provisioningState','Succeeded'))";
    // PRESENT-but-WRONG-TYPE variants: each key is PRESENT (so an existence-only
    // weakening — e.g. `body ? 'tags'` — still ACCEPTS the row) but carries the WRONG jsonb type,
    // which only the real `jsonb_typeof(...) = <t>` predicate rejects. Each isolates ONE type
    // predicate: no OTHER CHECK also rejects it, so a vacuous target is detected.
    // `tags` present as an ARRAY (not an object) — isolates ck_arm_overlay_tags's type check.
    const BODY_TAGS_ARR: &str = "jsonb_build_object('id',p,'name','n',\
        'type','Microsoft.Storage/storageAccounts','location','eastus',\
        'tags','[]'::jsonb,'properties','{}'::jsonb)";
    // `type` present as a NUMBER — isolates the envelope's type=string predicate. For a `resource`
    // row `body ->> 'type'` ('5') is still `<> 'Microsoft.Resources/resourceGroups'`, so kind_shape
    // stays satisfied and only the envelope rejects it.
    const BODY_TYPE_INT: &str = "jsonb_build_object('id',p,'name','n',\
        'type',5,'location','eastus','tags','{}'::jsonb,'properties','{}'::jsonb)";
    // `location` present as a NUMBER — isolates the envelope's location=string predicate.
    const BODY_LOCATION_INT: &str = "jsonb_build_object('id',p,'name','n',\
        'type','Microsoft.Storage/storageAccounts','location',5,\
        'tags','{}'::jsonb,'properties','{}'::jsonb)";
    // `properties` present as a STRING (not an object) — isolates the envelope's properties=object
    // predicate.
    const BODY_PROPS_STR: &str = "jsonb_build_object('id',p,'name','n',\
        'type','Microsoft.Storage/storageAccounts','location','eastus',\
        'tags','{}'::jsonb,'properties','x')";
    // A resource_group row whose properties.provisioningState is a NUMBER — isolates
    // ck_arm_overlay_rg_provisioning_state's string check (envelope/kind_shape/tags all pass).
    const BODY_RG_PS_INT: &str = "jsonb_build_object('id',p,'name','n',\
        'type','Microsoft.Resources/resourceGroups','location','eastus',\
        'tags','{}'::jsonb,'properties',jsonb_build_object('provisioningState',7))";

    let ok_sql = format!("{INS}p, p, 'resource','user',true, {OK_BODY}{FROM_PID}");
    let neg: Vec<(&str, String)> = vec![
        (
            "id_lower <> lower(id)",
            format!(
                "{INS}'WRONG_' || pg_backend_pid()::text, p, 'resource','user',true, {OK_BODY}{FROM_PID}"
            ),
        ),
        (
            "bad target_kind",
            format!("{INS}p, p, 'widget','user',false, NULL{FROM_PID}"),
        ),
        (
            "bad source",
            format!("{INS}p, p, 'resource','system',true, {OK_BODY}{FROM_PID}"),
        ),
        (
            "present=true+null body",
            format!("{INS}p, p, 'resource','user',true, NULL{FROM_PID}"),
        ),
        (
            "present=false+body",
            format!("{INS}p, p, 'resource','user',false, {OK_BODY}{FROM_PID}"),
        ),
        (
            "body id mismatch",
            format!("{INS}p, p, 'resource','user',true, {BODY_ID_MISMATCH}{FROM_PID}"),
        ),
        (
            "envelope missing name",
            format!("{INS}p, p, 'resource','user',true, {BODY_NO_NAME}{FROM_PID}"),
        ),
        (
            "resource body with RG type",
            format!("{INS}p, p, 'resource','user',true, {BODY_RG_TYPE}{FROM_PID}"),
        ),
        (
            "missing tags",
            format!("{INS}p, p, 'resource','user',true, {BODY_NO_TAGS}{FROM_PID}"),
        ),
        (
            "non-object sku",
            format!("{INS}p, p, 'resource','user',true, {BODY_SKU_STR}{FROM_PID}"),
        ),
        (
            "RG missing provisioningState",
            format!("{INS}p, p, 'resource_group','user',true, {BODY_RG_NO_PS}{FROM_PID}"),
        ),
        // fill the behavioural coverage gaps.
        (
            "empty {} body",
            format!("{INS}p, p, 'resource','user',true, '{{}}'::jsonb{FROM_PID}"),
        ),
        (
            "envelope missing id",
            format!("{INS}p, p, 'resource','user',true, {BODY_NO_ID}{FROM_PID}"),
        ),
        (
            "envelope missing type",
            format!("{INS}p, p, 'resource','user',true, {BODY_NO_TYPE}{FROM_PID}"),
        ),
        (
            "envelope missing location",
            format!("{INS}p, p, 'resource','user',true, {BODY_NO_LOCATION}{FROM_PID}"),
        ),
        (
            "envelope missing properties",
            format!("{INS}p, p, 'resource','user',true, {BODY_NO_PROPERTIES}{FROM_PID}"),
        ),
        (
            "envelope non-string name",
            format!("{INS}p, p, 'resource','user',true, {BODY_NAME_INT}{FROM_PID}"),
        ),
        (
            "non-string kind",
            format!("{INS}p, p, 'resource','user',true, {BODY_KIND_INT}{FROM_PID}"),
        ),
        (
            "RG body with non-RG type",
            format!("{INS}p, p, 'resource_group','user',true, {BODY_RG_WRONGTYPE}{FROM_PID}"),
        ),
        // PRESENT-but-WRONG-TYPE probes (an existence-only weakening ACCEPTS these).
        (
            "tags present as array",
            format!("{INS}p, p, 'resource','user',true, {BODY_TAGS_ARR}{FROM_PID}"),
        ),
        // `id` as a NUMBER: to keep body_id_agree satisfied (so ONLY the envelope rejects it), the
        // row id must equal the number's text form — use pg_backend_pid() as BOTH so the probe id
        // stays per-backend unique (no PK contention with concurrent boots). No `p`/FROM_PID here.
        (
            "id present as number",
            format!(
                "{INS}pg_backend_pid()::text, pg_backend_pid()::text, 'resource','user',true, \
                 jsonb_build_object('id', pg_backend_pid(), 'name','n',\
                 'type','Microsoft.Storage/storageAccounts','location','eastus',\
                 'tags','{{}}'::jsonb,'properties','{{}}'::jsonb)"
            ),
        ),
        (
            "type present as number",
            format!("{INS}p, p, 'resource','user',true, {BODY_TYPE_INT}{FROM_PID}"),
        ),
        (
            "location present as number",
            format!("{INS}p, p, 'resource','user',true, {BODY_LOCATION_INT}{FROM_PID}"),
        ),
        (
            "properties present as string",
            format!("{INS}p, p, 'resource','user',true, {BODY_PROPS_STR}{FROM_PID}"),
        ),
        (
            "RG provisioningState present as number",
            format!("{INS}p, p, 'resource_group','user',true, {BODY_RG_PS_INT}{FROM_PID}"),
        ),
    ];

    let mut tx = pool
        .begin()
        .await
        .map_err(|e| format!("arm_overlay inventory: begin probe tx failed: {e}"))?;

    // Positive control: a fully-valid snapshot MUST be accepted (constraints not over-tight).
    sqlx::query("SAVEPOINT p")
        .execute(&mut *tx)
        .await
        .map_err(|e| format!("arm_overlay inventory: probe savepoint failed: {e}"))?;
    match sqlx::query(&ok_sql).execute(&mut *tx).await {
        Ok(_) => {
            sqlx::query("ROLLBACK TO SAVEPOINT p")
                .execute(&mut *tx)
                .await
                .map_err(|e| format!("arm_overlay inventory: probe rollback failed: {e}"))?;
        }
        Err(e) => {
            let _ = tx.rollback().await;
            return Err(format!(
                "arm_overlay inventory: behavioural probe [valid row] was REJECTED but must be \
                 accepted — the row-model over-constrains ({e})"
            ));
        }
    }

    // Monotonic-revision probe: the trigger must ASSIGN a fresh, strictly
    // INCREASING revision on both INSERT and a subsequent UPDATE. A same-shape trigger wired to a
    // function that assigns a CONSTANT (e.g. `NEW.revision := 1`) passes the flag + tgfoid checks
    // yet violates the monotonic contract — only exercising TWO writes on one row catches it.
    // Reuses savepoint `p` (still active after the positive control's ROLLBACK TO); a trailing
    // ROLLBACK TO undoes both writes so the shared probe id is free for the negatives below.
    let r1: i64 = match sqlx::query_scalar(&format!(
        "{INS}p, p, 'resource','user',true, {OK_BODY}{FROM_PID} RETURNING revision"
    ))
    .fetch_one(&mut *tx)
    .await
    {
        Ok(v) => v,
        Err(e) => {
            let _ = tx.rollback().await;
            return Err(format!(
                "arm_overlay inventory: monotonic probe INSERT leg was REJECTED but must be \
                 accepted ({e})"
            ));
        }
    };
    let r2: i64 = match sqlx::query_scalar(&format!(
        "UPDATE synthetic.arm_overlay SET body = body WHERE id_lower = {PID_EXPR} RETURNING revision"
    ))
    .fetch_one(&mut *tx)
    .await
    {
        Ok(v) => v,
        Err(e) => {
            let _ = tx.rollback().await;
            return Err(format!(
                "arm_overlay inventory: monotonic probe UPDATE leg was REJECTED but must be \
                 accepted ({e})"
            ));
        }
    };
    if r2 <= r1 {
        let _ = tx.rollback().await;
        return Err(format!(
            "arm_overlay inventory: revision did not advance on UPDATE (INSERT revision {r1}, \
             UPDATE revision {r2}) — the trigger must assign a strictly increasing revision on \
             every write"
        ));
    }
    sqlx::query("ROLLBACK TO SAVEPOINT p")
        .execute(&mut *tx)
        .await
        .map_err(|e| format!("arm_overlay inventory: monotonic probe rollback failed: {e}"))?;

    // Each negative MUST be rejected; an ACCEPT means the target CHECK is missing or vacuous.
    for (label, sql) in &neg {
        match sqlx::query(sql).execute(&mut *tx).await {
            Err(_) => {
                sqlx::query("ROLLBACK TO SAVEPOINT p")
                    .execute(&mut *tx)
                    .await
                    .map_err(|e| format!("arm_overlay inventory: probe rollback failed: {e}"))?;
            }
            Ok(_) => {
                let _ = tx.rollback().await;
                return Err(format!(
                    "arm_overlay inventory: behavioural probe [{label}] was ACCEPTED but must be \
                     rejected — the corresponding CHECK is missing or vacuous"
                ));
            }
        }
    }

    tx.rollback()
        .await
        .map_err(|e| format!("arm_overlay inventory: probe tx rollback failed: {e}"))?;
    Ok(())
}

pub async fn serve_dual(
    state: AppState,
    budgets: Budgets,
    tls: bool,
    host: &str,
    http_port: u16,
    tls_port: u16,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    // Wrap the shared router in the execution-budget middleware (request timeout +
    // concurrency shed). The DB budgets are already baked into `state.pool`.
    let app = apply_execution_budgets(build_router(state), budgets);

    let http_addr = bind_addr(host, http_port);
    tracing::info!(addr = %http_addr, tls, "tenantless-server listening (HTTP)");

    if !tls {
        // DEFAULT path — byte-identical to v1: a single plain-HTTP bind.
        let listener = TcpListener::bind(&http_addr).await?;
        axum::serve(listener, app).await?;
        return Ok(());
    }

    // Opt-in HTTPS: ephemeral in-memory self-signed cert, one shared Router.
    use axum_server::tls_rustls::RustlsConfig;
    use rcgen::generate_simple_self_signed;

    // Pin the process-level rustls CryptoProvider to `ring` explicitly. Although
    // our `rustls` dep is feature-pinned to `ring`, transitive crates can surface
    // a second provider, leaving rustls unable to auto-select one — it then panics
    // inside `from_pem` ("Could not automatically determine the process-level
    // CryptoProvider"). Installing the default once removes that ambiguity and keeps
    // a single TLS stack. `install_default` errors only if a provider
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

    /// The fail-closed boot guard is a READ. Assert the extracted probe
    /// SQL is the exact both-EXISTS conjunction and carries ZERO DDL — so a refactor can never
    /// silently turn it into an index-creating / table-altering statement that reintroduces the
    /// ACCESS-EXCLUSIVE startup deadlock. DB-free: inspects the extracted const.
    #[test]
    fn assert_no_legacy_inplace_drift_query_shape() {
        let sql = super::D12_GUARD_PROBE_SQL;
        let upper = sql.to_uppercase();

        // Exactly two EXISTS sub-selects, OR'd together.
        assert_eq!(
            upper.matches("EXISTS").count(),
            2,
            "probe must be two EXISTS sub-selects"
        );
        assert!(
            upper.contains(" OR "),
            "the two EXISTS clauses must be OR'd"
        );

        // The MANDATORY provenance conjunction: a `reverted_at IS NULL`-only probe WITHOUT the
        // `storage_mode = 'synthetic'` conjunct would brick a valid overlay-drift tenant on the
        // first restart after legitimate overlay drift (non-negotiable).
        assert!(
            sql.contains("storage_mode = 'synthetic'"),
            "provenance conjunct required"
        );
        assert!(
            sql.contains("reverted_at IS NULL"),
            "active-batch predicate required"
        );
        assert!(
            sql.contains("drift_deleted_at IS NOT NULL"),
            "soft-delete predicate required"
        );

        // Zero DDL — the probe must never take ACCESS EXCLUSIVE at boot.
        for kw in ["CREATE", "ALTER", "INDEX", "DROP", "TRUNCATE"] {
            assert!(
                !upper.contains(kw),
                "guard probe must issue no DDL (found {kw})"
            );
        }
        // A single statement (no multi-statement batch).
        assert!(!sql.contains(';'), "guard probe must be a single statement");
    }

    #[test]
    fn bind_addr_defaults_to_loopback() {
        // The default host yields a loopback bind, NOT 0.0.0.0.
        assert_eq!(bind_addr("127.0.0.1", 8080), "127.0.0.1:8080");
    }

    #[test]
    fn bind_addr_honors_explicit_all_interfaces() {
        // An explicit 0.0.0.0 (compose/HOST override) binds every interface.
        assert_eq!(bind_addr("0.0.0.0", 8080), "0.0.0.0:8080");
        assert_eq!(bind_addr("0.0.0.0", 8443), "0.0.0.0:8443");
    }
}
