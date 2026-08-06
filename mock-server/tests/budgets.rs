//! Execution-budget middleware tests (resource-exhaustion guards).
//!
//! These exercise the REAL production seam `tenantless_server::apply_execution_budgets`
//! (the tower stack: request-timeout + shared concurrency-limit + load-shed + the ARM
//! error mapper) and the real cost route's body-size extractor. Every externally observable
//! failure must be an ARM `CloudError` JSON body — status + `application/json` + the exact
//! `{error:{code,message}}` shape, with a fixed non-leaking message.
//!
//! No database is required: the concurrency/timeout tests use a controlled slow router, and
//! the body-limit test rejects at extraction (before any query) over a lazily-created pool.

use axum::{
    Router,
    body::{Body, Bytes},
    http::{Request, StatusCode, header},
    response::Response,
    routing::get,
};
use std::time::Duration;
use tenantless_server::{Budgets, apply_execution_budgets};
use tower::ServiceExt; // oneshot

/// A slow route that holds its concurrency permit for `hold`, plus a fast route.
fn slow_router(hold: Duration) -> Router {
    Router::new()
        .route(
            "/slow",
            get(move || async move {
                tokio::time::sleep(hold).await;
                "ok"
            }),
        )
        .route("/fast", get(|| async { "ok" }))
}

fn get_req(path: &str) -> Request<Body> {
    Request::builder()
        .method("GET")
        .uri(path)
        .body(Body::empty())
        .unwrap()
}

/// (status, content-type, retry-after, parsed JSON body).
async fn parts(resp: Response) -> (StatusCode, String, Option<String>, serde_json::Value) {
    let status = resp.status();
    let ct = header_str(&resp, header::CONTENT_TYPE);
    let retry = resp
        .headers()
        .get(header::RETRY_AFTER)
        .and_then(|v| v.to_str().ok())
        .map(|s| s.to_string());
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .expect("collect body");
    let json = if bytes.is_empty() {
        serde_json::Value::Null
    } else {
        serde_json::from_slice(&bytes).unwrap_or(serde_json::Value::Null)
    };
    (status, ct, retry, json)
}

fn header_str(resp: &Response, name: header::HeaderName) -> String {
    resp.headers()
        .get(name)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_string()
}

/// Assert an ARM CloudError body: application/json, `{error:{code,message}}`, and a
/// non-empty message that leaks no internal/SQL text.
fn assert_arm_error(ct: &str, json: &serde_json::Value, expected_code: &str) {
    assert!(
        ct.starts_with("application/json"),
        "budget errors must be application/json, got {ct:?}"
    );
    assert_eq!(
        json["error"]["code"], expected_code,
        "wrong error code: {json}"
    );
    let msg = json["error"]["message"].as_str().unwrap_or_default();
    assert!(!msg.is_empty(), "message must be present: {json}");
    for leak in [
        "budget middleware",
        "SELECT",
        "Overloaded",
        "Elapsed",
        "sqlx",
        "panic",
    ] {
        assert!(
            !msg.contains(leak),
            "message leaks internal text {leak:?}: {msg}"
        );
    }
}

/// Concurrency limit = 1: while request A is in-flight (holding the only permit), request B
/// is SHED immediately with a 503 ARM body + `Retry-After: 1`; after A releases, request C
/// succeeds (the permit was returned on normal completion).
#[tokio::test]
async fn saturated_limit_sheds_then_recovers() {
    let app = apply_execution_budgets(
        slow_router(Duration::from_millis(400)),
        Budgets {
            request_timeout: Duration::from_secs(10), // generous: A must NOT time out
            concurrency_limit: 1,
        },
    );

    // A: spawn and let it acquire the single permit and start sleeping.
    let a = tokio::spawn(app.clone().oneshot(get_req("/slow")));
    tokio::time::sleep(Duration::from_millis(100)).await;

    // B: sent while A holds the permit → shed → 503 ARM body + Retry-After.
    let (status, ct, retry, json) =
        parts(app.clone().oneshot(get_req("/fast")).await.unwrap()).await;
    assert_eq!(
        status,
        StatusCode::SERVICE_UNAVAILABLE,
        "B must be shed while A is in-flight"
    );
    assert_arm_error(&ct, &json, "ServiceUnavailable");
    assert_eq!(
        retry.as_deref(),
        Some("1"),
        "a shed 503 must carry Retry-After: 1"
    );

    // A completes and releases the permit.
    let a_resp = a.await.unwrap().unwrap();
    assert_eq!(a_resp.status(), StatusCode::OK, "A itself must succeed");

    // C: after A released, a fresh request succeeds (permit returned on completion).
    let (status_c, ..) = parts(app.oneshot(get_req("/fast")).await.unwrap()).await;
    assert_eq!(
        status_c,
        StatusCode::OK,
        "C must succeed after A released the permit"
    );
}

/// An elapsed request deadline is a SERVER-side timeout → 504 GatewayTimeout ARM body,
/// never a 400 (it is not malformed client input).
#[tokio::test]
async fn elapsed_deadline_is_504() {
    let app = apply_execution_budgets(
        slow_router(Duration::from_millis(500)),
        Budgets {
            request_timeout: Duration::from_millis(50),
            concurrency_limit: 1024,
        },
    );
    let (status, ct, _retry, json) = parts(app.oneshot(get_req("/slow")).await.unwrap()).await;
    assert_eq!(
        status,
        StatusCode::GATEWAY_TIMEOUT,
        "an over-deadline request is a 504"
    );
    assert_arm_error(&ct, &json, "GatewayTimeout");
}

/// A timed-out request RELEASES its concurrency permit: with limit 1 and a short deadline,
/// a request that times out (504) does not wedge the limiter — the next request succeeds.
#[tokio::test]
async fn permit_released_after_timeout() {
    let app = apply_execution_budgets(
        slow_router(Duration::from_millis(500)),
        Budgets {
            request_timeout: Duration::from_millis(50),
            concurrency_limit: 1,
        },
    );
    // First request times out (holds then releases the permit).
    let (s1, ..) = parts(app.clone().oneshot(get_req("/slow")).await.unwrap()).await;
    assert_eq!(s1, StatusCode::GATEWAY_TIMEOUT);
    // Give the limiter a moment to reclaim the permit, then a fast request must succeed.
    tokio::time::sleep(Duration::from_millis(50)).await;
    let (s2, ..) = parts(app.oneshot(get_req("/fast")).await.unwrap()).await;
    assert_eq!(
        s2,
        StatusCode::OK,
        "the permit must be freed after a timeout"
    );
}

// ---- cost body-size budget (real route, lazy pool, no DB queried) ----------

/// Build the REAL router over a LAZY pool (never connected — the body-limit rejection
/// happens at extraction, before any query). Any non-empty Bearer passes (enforce off).
fn real_app() -> Router {
    use sqlx::postgres::PgPoolOptions;
    use tenantless_server::{
        build_router,
        jwt::{JwtSigner, SharedSigner},
        metrics::Metrics,
        state::AppState,
    };
    let pool = PgPoolOptions::new()
        .connect_lazy("postgres://unused:unused@127.0.0.1:1/unused")
        .expect("lazy pool");
    let tenant = uuid::Uuid::nil();
    let state = AppState {
        pool,
        base_url: "http://test".to_string(),
        metrics: Metrics::new(),
        signer: SharedSigner::new(JwtSigner::ephemeral(&tenant).expect("signer")),
        enforce_auth: false,
        control: None,
    };
    build_router(state)
}

fn cost_req(body: Body) -> Request<Body> {
    Request::builder()
        .method("POST")
        .uri("/subscriptions/11111111-1111-1111-1111-111111111111/providers/Microsoft.CostManagement/query")
        .header("Authorization", "Bearer health")
        .header(header::CONTENT_TYPE, "application/json")
        .body(body)
        .unwrap()
}

/// An oversized cost body (declared Content-Length) → 413 ARM body, rejected at extraction
/// before the pool is ever touched.
#[tokio::test]
async fn oversized_cost_body_is_413() {
    // 128 KiB, well over the 64 KiB cost-body cap.
    let big = vec![b'x'; 128 * 1024];
    let (status, ct, _retry, json) =
        parts(real_app().oneshot(cost_req(Body::from(big))).await.unwrap()).await;
    assert_eq!(
        status,
        StatusCode::PAYLOAD_TOO_LARGE,
        "oversized body must be 413"
    );
    assert_arm_error(&ct, &json, "RequestEntityTooLarge");
}

/// The 413 gate cannot be bypassed by a CHUNKED / unknown-`Content-Length` body: a streamed
/// body over the limit is still rejected at 413 (to_bytes enforces while streaming).
#[tokio::test]
async fn oversized_chunked_cost_body_is_413() {
    let chunk = Bytes::from(vec![b'x'; 8 * 1024]);
    // 20 * 8 KiB = 160 KiB, streamed with NO Content-Length.
    let stream = tokio_stream::iter((0..20).map(move |_| Ok::<_, std::io::Error>(chunk.clone())));
    let body = Body::from_stream(stream);
    let (status, ct, _retry, json) = parts(real_app().oneshot(cost_req(body)).await.unwrap()).await;
    assert_eq!(
        status,
        StatusCode::PAYLOAD_TOO_LARGE,
        "a chunked over-limit body must still be 413 (no Content-Length bypass)"
    );
    assert_arm_error(&ct, &json, "RequestEntityTooLarge");
}

/// A within-limit but malformed cost body is a client error → 400 (distinct from the 413
/// size budget): the size gate passed, the JSON parse did not.
#[tokio::test]
async fn small_malformed_cost_body_is_400() {
    let (status, ct, _retry, json) = parts(
        real_app()
            .oneshot(cost_req(Body::from("{not valid json")))
            .await
            .unwrap(),
    )
    .await;
    assert_eq!(
        status,
        StatusCode::BAD_REQUEST,
        "malformed (but small) body is a 400"
    );
    assert!(ct.starts_with("application/json"));
    assert_eq!(json["error"]["code"], "InvalidRequestContent");
}

/// A JSON body posted with a non-JSON `Content-Type` is rejected with a 415 — the media-type
/// contract of the stock `Json` extractor the bounded reader replaced. The body itself is
/// small and valid; only its declared type is wrong (distinct from the 400 malformed case).
#[tokio::test]
async fn wrong_content_type_cost_body_is_415() {
    let req = Request::builder()
        .method("POST")
        .uri("/subscriptions/11111111-1111-1111-1111-111111111111/providers/Microsoft.CostManagement/query")
        .header("Authorization", "Bearer health")
        .header(header::CONTENT_TYPE, "text/plain")
        .body(Body::from(r#"{"timeframe":"MonthToDate"}"#))
        .unwrap();
    let (status, ct, _retry, json) = parts(real_app().oneshot(req).await.unwrap()).await;
    assert_eq!(
        status,
        StatusCode::UNSUPPORTED_MEDIA_TYPE,
        "a non-JSON Content-Type must be a 415"
    );
    assert_arm_error(&ct, &json, "UnsupportedMediaType");
}

/// A body that FAILS mid-stream (a transport fault, not an over-limit) is a generic 500 —
/// NOT a 413. The extractor distinguishes a `LengthLimitError` (413) from any other body
/// failure, so a broken transfer is never silently reported as "too large".
#[tokio::test]
async fn body_stream_error_is_500_not_413() {
    // One small chunk (well under the 64 KiB cap) then a hard stream error.
    let stream = tokio_stream::iter(vec![
        Ok::<_, std::io::Error>(Bytes::from_static(b"{")),
        Err(std::io::Error::other("simulated transport fault")),
    ]);
    let (status, ct, _retry, json) = parts(
        real_app()
            .oneshot(cost_req(Body::from_stream(stream)))
            .await
            .unwrap(),
    )
    .await;
    assert_eq!(
        status,
        StatusCode::INTERNAL_SERVER_ERROR,
        "a body-stream transport fault (not over-limit) must be a 500, not a 413"
    );
    // Generic ARM body — the underlying error detail is logged, never serialized.
    assert_arm_error(&ct, &json, "InternalServerError");
}

/// An over-long `/_sim` search term is rejected up front with a 400 — BEFORE the tenant-wide
/// `ILIKE` scan runs (so the lazy pool is never queried). The search-term cap is a fixed
/// structural bound, and the rejection stays inside the ARM `CloudError` shape.
#[tokio::test]
async fn oversized_search_term_is_400() {
    let long = "a".repeat(300); // > MAX_SEARCH_TERM_CHARS (200)
    let req = Request::builder()
        .method("GET")
        .uri(format!("/_sim/resources/search?q={long}"))
        .body(Body::empty())
        .unwrap();
    let (status, ct, _retry, json) = parts(real_app().oneshot(req).await.unwrap()).await;
    assert_eq!(
        status,
        StatusCode::BAD_REQUEST,
        "an over-long search term is a 400"
    );
    assert!(ct.starts_with("application/json"));
    assert_eq!(json["error"]["code"], "InvalidRequestContent");
}
