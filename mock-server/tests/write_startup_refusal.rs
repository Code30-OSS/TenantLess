//! Real-binary (subprocess) proof of the D-12 unauthenticated-write startup refusal (D-26).
//!
//! Task 1 unit-tests the pure `should_refuse_unauth_writes` predicate; this suite proves the
//! ACTUAL compiled `tenantless-server` binary is wired to it — refuse / bind / warn exactly as
//! D-12 requires, observed through process outcome + stderr markers (NO source grep, which is
//! precisely the reliance D-26 removes).
//!
//! Harness: a testcontainers Postgres provisioned with a BOOTABLE tenant (via the shared
//! first-boot overlay+resolver substrate) so the binary clears every DB preflight + the
//! fail-closed boot guard and REACHES the write-safety gate. The binary is spawned at
//! `env!("CARGO_BIN_EXE_tenantless-server")` (mirroring integration.rs's `tokio::process::Command`
//! idiom) with the container DSN, an ephemeral free port, and the per-case host/flag matrix;
//! stderr is captured, each case waits a bounded window, and every child is killed on every path.
//!
//! DB-gated: requires Docker/PG16; validates on the Linux CI gate.

mod common;

use sqlx::PgPool;
use std::process::Stdio;
use std::time::Duration;
use tenantless_server::config::{UNAUTH_WRITE_REFUSAL_MARKER, UNAUTH_WRITE_WARN_MARKER};
use testcontainers_modules::{postgres, testcontainers::runners::AsyncRunner};
use tokio::io::AsyncReadExt;
use tokio::process::{Child, Command};

/// Bounded window for a case to reach its terminal outcome (exit or bind). Generous for
/// container/binary latency; the polls below return as soon as the outcome is observed.
const WINDOW: Duration = Duration::from_secs(30);
const POLL: Duration = Duration::from_millis(250);

/// Start Postgres and return the pool AND the DSN string the subprocess connects with.
async fn start_pg() -> (
    PgPool,
    String,
    testcontainers::ContainerAsync<postgres::Postgres>,
) {
    let container = postgres::Postgres::default()
        .start()
        .await
        .expect("start postgres container");
    let host = container.get_host().await.expect("container host");
    let port = container
        .get_host_port_ipv4(5432)
        .await
        .expect("container port");
    let url = format!("postgres://postgres:postgres@{host}:{port}/postgres");
    let pool = PgPool::connect(&url).await.expect("connect pool");
    (pool, url, container)
}

/// Provision a BOOTABLE tenant so the binary's DB preflights + fail-closed boot guard pass and
/// it reaches the write-safety gate (the first-boot overlay substrate + the resolver views that
/// supply `storage_mode`, which `assert_no_legacy_inplace_drift` reads).
async fn seed_bootable(pool: &PgPool) {
    common::seed_overlay_first_boot(pool).await;
    tenantless_server::ensure_arm_resolver_schema(pool)
        .await
        .expect("ensure_arm_resolver_schema");
}

/// Reserve a currently-free localhost TCP port (bound then immediately released).
fn free_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0")
        .expect("bind ephemeral")
        .local_addr()
        .expect("local addr")
        .port()
}

/// Spawn the real binary with the given host/flags against `dsn` on `port`, stderr piped.
fn spawn_server(dsn: &str, host: &str, port: u16, extra: &[&str]) -> Child {
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_tenantless-server"));
    cmd.arg("--database-url")
        .arg(dsn)
        .arg("--host")
        .arg(host)
        .arg("--port")
        .arg(port.to_string())
        .args(extra)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    cmd.spawn().expect("spawn tenantless-server")
}

/// True once a TCP connection to `127.0.0.1:port` succeeds within the window (the server bound).
async fn wait_until_bound(port: u16) -> bool {
    let deadline = tokio::time::Instant::now() + WINDOW;
    while tokio::time::Instant::now() < deadline {
        if tokio::time::timeout(
            Duration::from_millis(500),
            tokio::net::TcpStream::connect(("127.0.0.1", port)),
        )
        .await
        .ok()
        .and_then(Result::ok)
        .is_some()
        {
            return true;
        }
        tokio::time::sleep(POLL).await;
    }
    false
}

/// Kill + reap the child, then drain its stderr pipe to a string.
async fn kill_and_read_stderr(mut child: Child) -> String {
    let mut stderr = child.stderr.take().expect("stderr piped");
    let _ = child.kill().await;
    let _ = child.wait().await;
    let mut buf = String::new();
    let _ = stderr.read_to_string(&mut buf).await;
    buf
}

// --------------------------------------------------------------------------------------- //
// (a) REFUSE: non-loopback + writes + no-auth + no-override → non-zero exit, no bind, marker.
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn non_loopback_unauth_writes_refuses_startup() {
    let (pool, dsn, _c) = start_pg().await;
    seed_bootable(&pool).await;
    let port = free_port();

    let child = spawn_server(&dsn, "0.0.0.0", port, &["--enable-arm-writes"]);

    // The process must EXIT (non-zero) within the window; wait_with_output also drains stderr.
    let output = tokio::time::timeout(WINDOW, child.wait_with_output())
        .await
        .expect("binary must exit within the window, not hang")
        .expect("collect child output");

    assert!(
        !output.status.success(),
        "unauthenticated writes on a non-loopback bind must exit NON-ZERO (refuse to start)"
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains(UNAUTH_WRITE_REFUSAL_MARKER),
        "stderr must carry the write-safety refusal marker (distinguishes it from any unrelated \
         startup failure); got:\n{stderr}"
    );
    // The port must never have opened.
    let bound = tokio::time::timeout(
        Duration::from_millis(500),
        tokio::net::TcpStream::connect(("127.0.0.1", port)),
    )
    .await
    .ok()
    .and_then(Result::ok)
    .is_some();
    assert!(!bound, "a refused startup must never bind the port");
}

// --------------------------------------------------------------------------------------- //
// (b) PROCEED (loopback): binds; stderr has NO refusal marker.
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn loopback_unauth_writes_proceeds_and_binds() {
    let (pool, dsn, _c) = start_pg().await;
    seed_bootable(&pool).await;
    let port = free_port();

    let child = spawn_server(&dsn, "127.0.0.1", port, &["--enable-arm-writes"]);
    let bound = wait_until_bound(port).await;
    let stderr = kill_and_read_stderr(child).await;

    assert!(
        bound,
        "unauthenticated writes on a loopback bind must PROCEED (the port binds); stderr:\n{stderr}"
    );
    assert!(
        !stderr.contains(UNAUTH_WRITE_REFUSAL_MARKER),
        "a loopback bind must NOT emit the write-safety refusal marker; got:\n{stderr}"
    );
}

// --------------------------------------------------------------------------------------- //
// (c) PROCEED-with-WARN (override): --allow-insecure-writes on 0.0.0.0 binds + emits the WARN,
//     no refusal marker.
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn insecure_override_proceeds_with_warn() {
    let (pool, dsn, _c) = start_pg().await;
    seed_bootable(&pool).await;
    let port = free_port();

    let child = spawn_server(
        &dsn,
        "0.0.0.0",
        port,
        &["--enable-arm-writes", "--allow-insecure-writes"],
    );
    let bound = wait_until_bound(port).await;
    let stderr = kill_and_read_stderr(child).await;

    assert!(
        bound,
        "--allow-insecure-writes must let a non-loopback bind PROCEED; stderr:\n{stderr}"
    );
    assert!(
        stderr.contains(UNAUTH_WRITE_WARN_MARKER),
        "the override path must still emit the WAUTH-03 startup WARN; got:\n{stderr}"
    );
    assert!(
        !stderr.contains(UNAUTH_WRITE_REFUSAL_MARKER),
        "the override path proceeds, so no refusal marker; got:\n{stderr}"
    );
}
