//! Startup configuration surface (clap 4 derive) for `tenantless serve`.
//!
//! No api-version handling anywhere — the server accepts any api-version query
//! param without validation (MOCK-11). The `database_url` default matches the
//! Python generator seam (`writer.py` L27-30) so dev wiring is consistent across
//! the Python and Rust layers.

use clap::Parser;

/// CLI flags for the mock server. Each flag falls back to an environment variable.
#[derive(Parser, Debug, Clone)]
#[command(
    name = "tenantless-server",
    about = "ARM-compatible mock server for the synthetic tenant"
)]
pub struct Cli {
    /// Host/interface to bind (SEC-HIGH-3). Defaults to loopback `127.0.0.1` so a
    /// fresh local run is NOT exposed on the network; pass `--host 0.0.0.0` (or
    /// `HOST=0.0.0.0`) to bind all interfaces. Inside the docker image the compose
    /// file sets `HOST=0.0.0.0` so the container is reachable on its published
    /// (loopback-only) host port.
    #[arg(long, env = "HOST", default_value = "127.0.0.1")]
    pub host: String,

    /// TCP port to bind.
    #[arg(long, env = "PORT", default_value_t = 8080)]
    pub port: u16,

    /// Absolute base URL emitted in `nextLink`s (MOCK-08).
    #[arg(long, env = "BASE_URL", default_value = "http://localhost:8080")]
    pub base_url: String,

    /// Postgres connection string (must match the generator's `writer.py` default).
    #[arg(
        long,
        env = "DATABASE_URL",
        default_value = "postgres://tenantless:tenantless_dev@localhost:5433/tenantless"
    )]
    pub database_url: String,

    /// Also bind HTTPS alongside the default plain HTTP listener (PLAT-05, D-15).
    /// When absent, only plain HTTP on `--port` is served (byte-identical to v1).
    /// When set, an ephemeral in-memory self-signed cert (D-16) is generated at
    /// startup and HTTPS is served on `--tls-port` WHILE `--port` stays up.
    #[arg(long, env = "TLS", default_value_t = false)]
    pub tls: bool,

    /// TCP port for the opt-in HTTPS listener (only used when `--tls` is set).
    #[arg(long, env = "TLS_PORT", default_value_t = 8443)]
    pub tls_port: u16,

    /// Enforce real RS256 JWT validation on the ARM data routes (IAM-05, D-11).
    /// **Default OFF** — when absent, the presence-only any-Bearer scanner contract
    /// is byte-for-byte preserved (an arbitrary non-empty Bearer → 200, missing →
    /// 401), exactly as before. When set, Plan 10-04 swaps in RS256 + claims
    /// (iss/aud/exp) validation against the run's own JWKS; the `/token` + JWKS
    /// routes stay exempt. Threaded Python `serve` → Rust clap flag/env like `--tls`.
    #[arg(long, env = "ENFORCE_AUTH", default_value_t = false)]
    pub enforce_auth: bool,

    /// Arm the control-plane write surface (CTRL-05, D-02). **Default OFF.** The
    /// `/_control/*` routes are merged ONLY when this is set AND a non-empty
    /// `--control-token` is configured; otherwise the server stays the read-only surface
    /// it is today and `/_control/probe` returns 404. Set WITHOUT a token → the server
    /// **fails closed** at startup with a clear error (never arm without a secret).
    #[arg(long, env = "ENABLE_CONTROL_PLANE", default_value_t = false)]
    pub enable_control_plane: bool,

    /// The control-plane admin secret (D-01). Presented by the browser in the
    /// `X-Control-Token` header and compared in constant time against its SHA-256 digest.
    /// A DISTINCT realm from the any-Bearer ARM gate — it is never coupled to the RS256/AAD
    /// stack. Required (with `--enable-control-plane`) to arm the control plane; never logged.
    #[arg(long, env = "TENANTLESS_CONTROL_TOKEN")]
    pub control_token: Option<String>,

    /// Server-owned root for control-plane artifacts (D-03/D-12/D-13). Three subdirs
    /// (`profiles/`, `sources/`, `snapshots/`) are created here at arm time; operators drop
    /// DuckDB analyze sources into `sources/` out-of-band. Only used when armed.
    #[arg(long, env = "CONTROL_DATA_DIR", default_value = "./control-data")]
    pub control_data_dir: std::path::PathBuf,

    // ---- Execution budgets (resource-exhaustion guards) --------------------
    // Operational knobs — env-tunable. The structural caps ($filter size, search-term
    // length, cost body size) are fixed consts in their modules, not exposed here.
    /// Global per-request wall-clock deadline, in seconds. Every request runs under a
    /// `tower` timeout; an elapsed deadline surfaces an ARM 504 GatewayTimeout. The
    /// `value_parser` range makes an out-of-range value FAIL STARTUP (never a silent 0).
    #[arg(long, env = "REQUEST_TIMEOUT_SECS", default_value_t = 30,
          value_parser = clap::value_parser!(u64).range(1..=3600))]
    pub request_timeout_secs: u64,

    /// Maximum concurrent in-flight requests. At capacity the server SHEDS (does not queue)
    /// with an ARM 503 ServiceUnavailable + `Retry-After: 1`. Validated at startup: a
    /// non-positive or absurd value fails to start (never admits 0 or an unbounded fleet).
    #[arg(long, env = "CONCURRENCY_LIMIT", default_value_t = 64,
          value_parser = clap::value_parser!(u32).range(1..=1_000_000))]
    pub concurrency_limit: u32,

    /// Postgres `statement_timeout` applied to EVERY pooled connection (session-level), in
    /// milliseconds — the server-wide DB execution deadline. A cancelled statement
    /// (SQLSTATE 57014) surfaces an ARM 504. The cost query keeps its own tighter app
    /// deadline on top of this.
    #[arg(long, env = "DB_STATEMENT_TIMEOUT_MS", default_value_t = 10_000,
          value_parser = clap::value_parser!(u64).range(100..=600_000))]
    pub db_statement_timeout_ms: u64,

    /// Max seconds to wait for a free pooled connection before failing, so pool exhaustion
    /// fails fast instead of hanging. Bounds the queue behind the 15-connection pool cap.
    #[arg(long, env = "DB_ACQUIRE_TIMEOUT_SECS", default_value_t = 5,
          value_parser = clap::value_parser!(u64).range(1..=300))]
    pub db_acquire_timeout_secs: u64,
}

#[cfg(test)]
mod tests {
    use super::Cli;
    use clap::Parser;

    /// An invalid execution-budget value must FAIL STARTUP (clap parse error), never be
    /// silently accepted — a 0 concurrency limit would admit no requests, an absurd one
    /// would defeat the guard. The `value_parser` range enforces this at parse time.
    #[test]
    fn rejects_out_of_range_budgets() {
        for args in [
            ["tenantless-server", "--concurrency-limit", "0"],
            ["tenantless-server", "--concurrency-limit", "100000000"],
            ["tenantless-server", "--request-timeout-secs", "0"],
            ["tenantless-server", "--db-statement-timeout-ms", "0"],
            ["tenantless-server", "--db-acquire-timeout-secs", "0"],
        ] {
            assert!(
                Cli::try_parse_from(args).is_err(),
                "{args:?} must be rejected at startup"
            );
        }
    }

    /// The defaults parse and carry the documented budget values.
    #[test]
    fn defaults_carry_budget_values() {
        let cli = Cli::try_parse_from(["tenantless-server"]).expect("defaults must parse");
        assert_eq!(cli.concurrency_limit, 64);
        assert_eq!(cli.request_timeout_secs, 30);
        assert_eq!(cli.db_statement_timeout_ms, 10_000);
        assert_eq!(cli.db_acquire_timeout_secs, 5);
    }
}
