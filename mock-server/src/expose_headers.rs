//! D-25 browser-exposure middleware: append `Access-Control-Expose-Headers: ETag`
//! to any arm-router response that already carries an `ETag`.
//!
//! The stateful ARM write plane emits a strong `ETag` header on detail GET/HEAD and on
//! every successful mutation (PUT/PATCH 200/201, DELETE 204). A browser `fetch` cannot
//! read a response header that is not listed in `Access-Control-Expose-Headers`, so this
//! `from_fn` response layer makes the already-served validator readable by script.
//!
//! Deliberately minimal (D-25): it introduces NO CORS-origin policy, NO
//! `Access-Control-Allow-Origin`/`-Credentials`, and NO new dependency — it only exposes
//! the single `ETag` header, and ONLY when an `ETag` is present. A response with no `ETag`
//! (e.g. a 405 gating response, a list body, a 404) is returned unchanged.

use axum::{
    extract::Request,
    http::{HeaderValue, header},
    middleware::Next,
    response::Response,
};

/// Response middleware: if the downstream response carries an `ETag` header, also emit
/// `Access-Control-Expose-Headers: ETag` so a browser client can read the validator. A
/// response with no `ETag` is passed through untouched (the exposure header only ever
/// rides alongside an `ETag`).
pub async fn expose_etag_header(request: Request, next: Next) -> Response {
    let mut response = next.run(request).await;
    if response.headers().contains_key(header::ETAG) {
        response.headers_mut().insert(
            header::ACCESS_CONTROL_EXPOSE_HEADERS,
            HeaderValue::from_static("ETag"),
        );
    }
    response
}
