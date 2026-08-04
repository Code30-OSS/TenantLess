//! TLS dual-bind integration test (PLAT-05, D-15/D-16).
//!
//! Proves the production `--tls` contract end-to-end over real sockets:
//!   * `serve_dual(state, tls)` (the production seam in `tenantless_server::serve_dual`)
//!     binds plain HTTP on one port AND — when `tls == true` — ALSO binds HTTPS on a
//!     second port with an ephemeral in-memory rcgen self-signed cert, both serving
//!     the SAME `build_router(state)` Router.
//!   * An HTTPS client that trusts the in-process self-signed cert and a plain-HTTP
//!     client both `GET /subscriptions` with any Bearer and get byte-identical ARM JSON.
//!   * The any-Bearer :8080-style HTTP path still returns 200 (any-Bearer scanner
//!     contract, invariant 3 / Pitfall 5) — re-asserted here against a real listener.
//!
//! RED until Task 3: `tenantless_server::serve_dual` does not exist yet, so this test
//! fails to COMPILE. Once `main.rs`'s dual-bind plumbing is extracted into
//! `serve_dual`, both listeners answer and the assertions pass (GREEN).
//!
//! Single TLS stack: the test's rustls `ClientConfig` is built on the SAME `ring`
//! crypto provider the server pins (Cargo.toml), so there is exactly one provider.
//! The shared testcontainers fixture (`common::seed_fixture`) is reused VERBATIM —
//! no count/row mutation (project memory: shared-fixture coupling).

mod common;

use std::sync::Arc;
use std::time::Duration;

use rustls::pki_types::{CertificateDer, ServerName, UnixTime};
use sqlx::PgPool;
use tenantless_server::{Budgets, metrics::Metrics, serve_dual, state::AppState};

/// A generous budget for the TLS bind tests — these exercise the dual HTTP/HTTPS listener,
/// not the budget limits, so the timeout is long and the concurrency ceiling is high.
fn test_budgets() -> Budgets {
    Budgets {
        request_timeout: std::time::Duration::from_secs(30),
        concurrency_limit: 1024,
    }
}
use testcontainers_modules::{postgres, testcontainers::runners::AsyncRunner};
use tokio::net::TcpListener;

/// Start an ephemeral Postgres container and return a connected pool plus the
/// container guard (kept alive for the test's duration). Mirrors the harness in
/// `integration.rs` so the fixture stays identical.
async fn start_pg() -> (PgPool, testcontainers::ContainerAsync<postgres::Postgres>) {
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
    (pool, container)
}

/// A rustls `ServerCertVerifier` that trusts ANY presented cert — TEST ONLY, so the
/// ephemeral self-signed server cert is accepted without a CA. Built on the same
/// `ring` provider the server uses, so there is one process-level crypto stack.
#[derive(Debug)]
struct TrustAnyCert(rustls::crypto::CryptoProvider);

impl rustls::client::danger::ServerCertVerifier for TrustAnyCert {
    fn verify_server_cert(
        &self,
        _end_entity: &CertificateDer<'_>,
        _intermediates: &[CertificateDer<'_>],
        _server_name: &ServerName<'_>,
        _ocsp_response: &[u8],
        _now: UnixTime,
    ) -> Result<rustls::client::danger::ServerCertVerified, rustls::Error> {
        Ok(rustls::client::danger::ServerCertVerified::assertion())
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &rustls::DigitallySignedStruct,
    ) -> Result<rustls::client::danger::HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls12_signature(
            message,
            cert,
            dss,
            &self.0.signature_verification_algorithms,
        )
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &rustls::DigitallySignedStruct,
    ) -> Result<rustls::client::danger::HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls13_signature(
            message,
            cert,
            dss,
            &self.0.signature_verification_algorithms,
        )
    }

    fn supported_verify_schemes(&self) -> Vec<rustls::SignatureScheme> {
        self.0.signature_verification_algorithms.supported_schemes()
    }
}

/// Build a reqwest client whose rustls `ClientConfig` trusts the self-signed cert,
/// using the `ring` provider (single TLS stack). `https_only == false` so the same
/// client can also hit the plain-HTTP listener.
fn trusting_client() -> reqwest::Client {
    let provider = rustls::crypto::ring::default_provider();
    let config = rustls::ClientConfig::builder_with_provider(Arc::new(provider.clone()))
        .with_safe_default_protocol_versions()
        .expect("default protocol versions")
        .dangerous()
        .with_custom_certificate_verifier(Arc::new(TrustAnyCert(provider)))
        .with_no_client_auth();

    reqwest::Client::builder()
        .use_preconfigured_tls(config)
        .timeout(Duration::from_secs(10))
        .build()
        .expect("build trusting reqwest client")
}

/// Reserve two ephemeral localhost ports by binding then immediately dropping the
/// listeners — `serve_dual` re-binds them. (A tiny TOCTOU window is fine for a
/// single-process test; the OS rarely re-hands these ports in the gap.)
async fn reserve_two_ports() -> (u16, u16) {
    let l1 = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("reserve http port");
    let l2 = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("reserve https port");
    let p1 = l1.local_addr().unwrap().port();
    let p2 = l2.local_addr().unwrap().port();
    drop(l1);
    drop(l2);
    (p1, p2)
}

/// BDD: Given the server started WITH --tls, When a client connects over HTTPS with
/// any Bearer, Then it gets the same ARM JSON as the plain-HTTP listener; And the
/// plain-HTTP client still gets 200 (any-Bearer scanner contract preserved).
#[tokio::test]
async fn tls_dual_bind_serves_identical_arm_json() {
    let (pool, _container) = start_pg().await;
    common::seed_fixture(&pool).await; // shared fixture — NOT mutated.

    let (http_port, https_port) = reserve_two_ports().await;
    let state = AppState {
        pool,
        base_url: format!("http://127.0.0.1:{http_port}"),
        metrics: Metrics::new(),
        signer: common::test_signer(),
        enforce_auth: false,
        control: None,
    };

    // Spawn the PRODUCTION dual-bind seam with TLS enabled. This is the function
    // Task 3 extracts from main.rs; until then this test does not compile (RED).
    let server = tokio::spawn(async move {
        serve_dual(
            state,
            test_budgets(),
            true,
            "127.0.0.1",
            http_port,
            https_port,
        )
        .await
    });

    // Give both listeners a moment to bind.
    tokio::time::sleep(Duration::from_millis(750)).await;

    let client = trusting_client();

    // Plain HTTP — the default path that must stay green (invariant 3 / Pitfall 5).
    let http_resp = client
        .get(format!("http://127.0.0.1:{http_port}/subscriptions"))
        .bearer_auth("any-token")
        .send()
        .await
        .expect("HTTP request to plain listener");
    assert_eq!(
        http_resp.status(),
        200,
        "plain HTTP any-Bearer must return 200"
    );
    let http_json: serde_json::Value = http_resp.json().await.expect("HTTP ARM JSON");

    // HTTPS — the opt-in TLS listener, same Router, self-signed cert trusted.
    let https_resp = client
        .get(format!("https://127.0.0.1:{https_port}/subscriptions"))
        .bearer_auth("any-token")
        .send()
        .await
        .expect("HTTPS request to TLS listener");
    assert_eq!(https_resp.status(), 200, "HTTPS any-Bearer must return 200");
    let https_json: serde_json::Value = https_resp.json().await.expect("HTTPS ARM JSON");

    // Identical ARM JSON from both listeners (one Router, two binds).
    assert_eq!(
        http_json, https_json,
        "HTTP and HTTPS must return byte-identical ARM JSON for /subscriptions"
    );
    let value = http_json["value"].as_array().expect("ARM value array");
    assert!(
        !value.is_empty(),
        "fixture has subscriptions, value must be non-empty"
    );

    server.abort();
}

/// BDD: Given --tls is ABSENT, When the server starts, Then nothing binds the HTTPS
/// port and the plain-HTTP listener serves normally (any Bearer → 200). Re-asserts
/// the byte-identical default path of Pitfall 5 against a real socket.
#[tokio::test]
async fn no_tls_binds_http_only() {
    let (pool, _container) = start_pg().await;
    common::seed_fixture(&pool).await; // shared fixture — NOT mutated.

    let (http_port, https_port) = reserve_two_ports().await;
    let state = AppState {
        pool,
        base_url: format!("http://127.0.0.1:{http_port}"),
        metrics: Metrics::new(),
        signer: common::test_signer(),
        enforce_auth: false,
        control: None,
    };

    // tls == false → only the plain-HTTP listener binds; https_port stays free.
    let server = tokio::spawn(async move {
        serve_dual(
            state,
            test_budgets(),
            false,
            "127.0.0.1",
            http_port,
            https_port,
        )
        .await
    });
    tokio::time::sleep(Duration::from_millis(500)).await;

    let client = trusting_client();

    // Plain HTTP still answers with any Bearer.
    let http_resp = client
        .get(format!("http://127.0.0.1:{http_port}/subscriptions"))
        .bearer_auth("any-token")
        .send()
        .await
        .expect("HTTP request to plain listener");
    assert_eq!(
        http_resp.status(),
        200,
        "no-TLS plain HTTP any-Bearer must return 200"
    );

    // The HTTPS port must NOT be serving TLS — a plain TCP connect should be refused
    // (nothing bound) OR at minimum no HTTPS handshake succeeds. We assert that an
    // HTTPS GET fails (connection refused / handshake error), proving :8443 is unbound.
    let https_attempt = client
        .get(format!("https://127.0.0.1:{https_port}/subscriptions"))
        .bearer_auth("any-token")
        .timeout(Duration::from_secs(2))
        .send()
        .await;
    assert!(
        https_attempt.is_err(),
        "with --tls absent, nothing must answer HTTPS on the tls port"
    );

    server.abort();
}
