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

    /// Arm the generic ARM write plane (PUT/PATCH/DELETE) over the overlay substrate
    /// (WAUTH-01, D-10/D-11). **Default OFF** — while unset every write method returns
    /// `405 MethodNotAllowed` with an `Allow` header, and the server stays the read-only
    /// surface it is today. This flag arms the write HANDLERS ONLY; it NEVER grants
    /// authorization, bypasses authentication, or bypasses the fail-closed boot guard
    /// (D-11). There is deliberately NO config-file / inference path — writes arm ONLY
    /// via this explicit flag or its `ENABLE_ARM_WRITES` env var (WAUTH-01: no silent enable).
    #[arg(long, env = "ENABLE_ARM_WRITES", default_value_t = false)]
    pub enable_arm_writes: bool,

    /// Insecure-development override for the D-12 unauthenticated-write safety guard
    /// (WAUTH-03). **Default OFF.** Enabling unauthenticated writes (`--enable-arm-writes`
    /// WITHOUT `--enforce-auth`) on a NON-loopback bind normally REFUSES startup; this
    /// explicit override permits it anyway, for a knowingly-local/test deployment, WITH a
    /// prominent startup WARN. It never affects a loopback bind (already permitted) and
    /// never grants authorization. Local/test-only — see docs.
    #[arg(long, env = "ALLOW_INSECURE_WRITES", default_value_t = false)]
    pub allow_insecure_writes: bool,

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

/// Classify a bind `host` string as a genuine loopback interface (D-12 / WAUTH-03).
///
/// Returns `true` ONLY for an address that parses as an `IpAddr` whose `is_loopback()`
/// holds (`127.0.0.0/8`, `::1`). Everything else is treated as NON-loopback — a
/// conservative default so the Plan-05 unauthenticated-write refusal fails SAFE:
/// - `0.0.0.0` / `::` parse as IPs but are *unspecified* (bind-all) → not loopback → unsafe.
/// - A non-IP hostname (e.g. `example.com`) fails to parse → `false` → treated as public.
///
/// This is a pure classifier: no DNS resolution, no network access.
pub fn host_is_loopback(host: &str) -> bool {
    host.parse::<std::net::IpAddr>()
        .map(|ip| ip.is_loopback())
        .unwrap_or(false)
}

/// Byte-stable marker embedded in the D-12 startup-REFUSAL error message so the real-binary
/// subprocess test (D-26, `write_startup_refusal.rs`) can distinguish a write-safety refusal
/// from any unrelated startup failure. Do NOT reword — the subprocess test greps stderr for it.
/// Credential-safe: names no token, header, or body.
pub const UNAUTH_WRITE_REFUSAL_MARKER: &str =
    "WRITE-SAFETY REFUSAL: ARM writes are enabled without --enforce-auth on a non-loopback bind";

/// Byte-stable marker for the WAUTH-03 startup WARN (writes enabled without strict auth). The
/// D-26 subprocess test asserts on this for the `--allow-insecure-writes` proceed-with-warn
/// case. Credential-safe: names no token, header, or body (D-12).
pub const UNAUTH_WRITE_WARN_MARKER: &str =
    "SECURITY WARNING: ARM writes are enabled WITHOUT --enforce-auth";

/// Pure guard predicate for the D-12 unauthenticated-write startup refusal (D-23).
///
/// Returns `true` (REFUSE to start) iff ARM writes are armed AND authentication is not
/// enforced AND the bind is NOT loopback AND no explicit insecure-development override is
/// supplied — i.e. an unauthenticated write plane would be exposed on a public interface.
/// Every other combination returns `false` (proceed):
///   * loopback bind → already local, not publicly exposed;
///   * `--enforce-auth` → writes are authenticated;
///   * `--allow-insecure-writes` → operator has knowingly opted into a local/test posture;
///   * writes off → nothing to expose.
///
/// This is the SINGLE source of the D-12 decision: pure, total, and composing the existing
/// [`host_is_loopback`] classifier (so `0.0.0.0`/`::`/non-IP hosts classify as non-loopback =
/// unsafe). `main.rs` only composes it; the end-to-end proof on the real binary is D-26.
pub fn should_refuse_unauth_writes(
    enable_arm_writes: bool,
    enforce_auth: bool,
    host: &str,
    allow_insecure_writes: bool,
) -> bool {
    enable_arm_writes && !enforce_auth && !host_is_loopback(host) && !allow_insecure_writes
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

    /// WAUTH-01: BOTH write-gating flags default OFF — no arg/env means no silent
    /// enable. `enable_arm_writes` arms the write handlers; `allow_insecure_writes`
    /// is the D-12 non-loopback insecure-development override. Neither may be true
    /// by default.
    #[test]
    fn write_flags_default_off() {
        let cli = Cli::try_parse_from(["tenantless-server"]).expect("defaults must parse");
        assert!(!cli.enable_arm_writes, "writes must default OFF (WAUTH-01)");
        assert!(
            !cli.allow_insecure_writes,
            "insecure-writes override must default OFF"
        );
    }

    /// The explicit `--enable-arm-writes` / `--allow-insecure-writes` arg path arms
    /// each flag. (The `env = "…"` wiring mirrors `enforce_auth` verbatim — not
    /// re-tested here to avoid process-global env mutation racing the defaults test.)
    #[test]
    fn write_flags_parse_from_args() {
        let cli = Cli::try_parse_from([
            "tenantless-server",
            "--enable-arm-writes",
            "--allow-insecure-writes",
        ])
        .expect("write flags must parse");
        assert!(cli.enable_arm_writes);
        assert!(cli.allow_insecure_writes);
    }

    /// D-12 / D-23 / WAUTH-03: the pure `should_refuse_unauth_writes` guard is the SINGLE
    /// source of the startup-refusal decision, proven by this behavioral matrix (NOT a source
    /// grep of the main.rs call site). A row is REFUSE (`true`) iff ARM writes are enabled AND
    /// auth is not enforced AND the bind is non-loopback AND no insecure-development override
    /// is supplied; every other combination PROCEEDS (`false`).
    #[test]
    fn should_refuse_unauth_writes_matrix() {
        use super::should_refuse_unauth_writes as refuse;

        // REFUSE: writes + no-auth + non-loopback bind + no override.
        assert!(
            refuse(true, false, "0.0.0.0", false),
            "0.0.0.0 bind-all + unauth writes + no override → refuse"
        );
        assert!(
            refuse(true, false, "::", false),
            ":: (unspecified IPv6) is non-loopback → refuse"
        );
        assert!(
            refuse(true, false, "example.com", false),
            "a non-IP hostname is conservatively non-loopback → refuse (fail-safe)"
        );

        // PROCEED: loopback bind (already safe — no public exposure).
        assert!(
            !refuse(true, false, "127.0.0.1", false),
            "IPv4 loopback → proceed"
        );
        assert!(
            !refuse(true, false, "::1", false),
            "IPv6 loopback → proceed"
        );

        // PROCEED: explicit insecure-development override.
        assert!(
            !refuse(true, false, "0.0.0.0", true),
            "--allow-insecure-writes override → proceed (with WARN)"
        );

        // PROCEED: auth enforced (writes are authenticated, so the guard does not apply).
        assert!(
            !refuse(true, true, "0.0.0.0", false),
            "--enforce-auth → proceed"
        );

        // PROCEED: writes off entirely (nothing to expose).
        assert!(
            !refuse(false, false, "0.0.0.0", false),
            "writes disabled → proceed"
        );
    }

    /// D-12 / WAUTH-03: `host_is_loopback` classifies ONLY genuine loopback IPs as
    /// loopback. `0.0.0.0` / `::` (unspecified = bind-all) and any non-IP hostname
    /// are NON-loopback (conservative → the Plan-05 refusal fails safe).
    #[test]
    fn host_is_loopback_table() {
        assert!(super::host_is_loopback("127.0.0.1"), "IPv4 loopback");
        assert!(super::host_is_loopback("::1"), "IPv6 loopback");
        assert!(
            super::host_is_loopback("127.0.0.5"),
            "the whole 127/8 block is loopback"
        );
        assert!(
            !super::host_is_loopback("0.0.0.0"),
            "0.0.0.0 is unspecified (bind-all), NOT loopback"
        );
        assert!(
            !super::host_is_loopback("::"),
            ":: is unspecified (bind-all), NOT loopback"
        );
        assert!(
            !super::host_is_loopback("example.com"),
            "a non-IP hostname is conservatively non-loopback"
        );
        assert!(
            !super::host_is_loopback("10.0.0.1"),
            "a private-range IP is not loopback"
        );
    }
}
