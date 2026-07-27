//! Pagination helpers: `$top` clamp, opaque base64 `$skiptoken` codec, and the
//! absolute-URL `nextLink` builder (MOCK-03 / MOCK-04 / MOCK-08).
//!
//! These are pure functions — no DB, no async — so they are unit-tested DB-free
//! (`cargo test -p tenantless-server --lib pagination::`) per the Nyquist sampling
//! rate. The cursor is opaque to the client: it carries the last-seen primary key
//! (TEXT for resources/resource_groups, UUID string for subscriptions) base64
//! url-safe encoded, and is always passed through a parameterized `.bind()` in the
//! handler — never string-spliced into SQL.

use crate::error::ApiError;
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use serde::Deserialize;
use sqlx::Row;
use sqlx::postgres::PgRow;
use uuid::Uuid;

/// The paginated list query parameters, deserialized from the query string.
///
/// ARM uses `$`-prefixed OData params (`$top`, `$skiptoken`, `$filter`) plus
/// `api-version`, none of which are valid Rust identifiers, so each is
/// `serde(rename)`-mapped. Other unknown query params are ignored (MOCK-11) —
/// serde drops fields not declared here, so `$expand`, etc. pass through silently.
///
/// `$filter` is now a *declared* field (MOCK-06): the resource list handlers parse
/// it into a placeholders-only `WHERE` conjunct, and [`next_link`] echoes it in the
/// emitted `nextLink` so a filtered traversal re-applies the same predicate on page 2+.
#[derive(Debug, Default, Deserialize)]
pub struct PageParams {
    /// `$top` — requested page size; clamped via [`clamp_top`] (default 100, max 1000).
    #[serde(rename = "$top", default)]
    pub top: Option<i64>,
    /// `$skiptoken` — opaque keyset cursor; decoded via [`decode_token`].
    #[serde(rename = "$skiptoken", default)]
    pub skiptoken: Option<String>,
    /// `api-version` — accepted and preserved in `nextLink`, never validated (MOCK-11).
    #[serde(rename = "api-version", default)]
    pub api_version: Option<String>,
    /// `$filter` — OData filter string (MOCK-06); parsed by the resource list handlers
    /// into a placeholders-only `WHERE` conjunct and echoed in `nextLink` (Research Q2).
    #[serde(rename = "$filter", default)]
    pub filter: Option<String>,
}

/// Implements the `LIMIT top+1` continuation trick: given the rows fetched with a
/// limit of `top + 1` and the clamped `top`, decide whether another page exists.
///
/// `key_of` extracts the keyset cursor key (the primary key) from a row. If more
/// than `top` rows came back, an extra page exists: the surplus row is dropped and
/// the encoded cursor of the last *returned* row is emitted. Otherwise this is the
/// final page and the cursor is `None` (so `nextLink` is omitted — Pitfall 4).
pub fn split_page<T>(
    mut rows: Vec<T>,
    top: i64,
    key_of: impl Fn(&T) -> &str,
) -> (Vec<T>, Option<String>) {
    if rows.len() as i64 > top {
        rows.truncate(top as usize);
        let token = rows.last().map(|r| encode_token(key_of(r)));
        (rows, token)
    } else {
        (rows, None)
    }
}

/// Default page size when `$top` is absent; clamp to `1..=1000` (MOCK-03, DoS guard).
pub fn clamp_top(raw: Option<i64>) -> i64 {
    raw.unwrap_or(100).clamp(1, 1000)
}

/// Encode the last-seen key into an opaque url-safe base64 `$skiptoken`.
pub fn encode_token(last_key: &str) -> String {
    URL_SAFE_NO_PAD.encode(last_key)
}

/// Decode an opaque `$skiptoken` back into the keyset cursor.
///
/// Both base64 and UTF-8 failures map to a 400 InvalidRequestContent — the client
/// supplied a malformed token (threat T-03-03: no SQL text ever leaks).
pub fn decode_token(tok: &str) -> Result<String, ApiError> {
    let bytes = URL_SAFE_NO_PAD
        .decode(tok)
        .map_err(|_| ApiError::BadRequest {
            message: "invalid $skiptoken".to_string(),
        })?;
    String::from_utf8(bytes).map_err(|_| ApiError::BadRequest {
        message: "invalid $skiptoken".to_string(),
    })
}

/// Decode an opaque `$skiptoken` into a NUMERIC keyset cursor (shared by the drift
/// audit reads and the `/_sim` collection reads — a numeric SERIAL/BIGSERIAL keyset).
/// `None` (absent token) → no cursor. A token that is not url-safe base64, or whose
/// decoded payload is not a base-10 `i64`, → 400 `BadRequest` with the same fixed,
/// non-leaking message the pagination codec uses — NEVER a 500, and no SQL/cursor text
/// ever leaks (T-03-03).
pub(crate) fn cursor_from_token(token: Option<&str>) -> Result<Option<i64>, ApiError> {
    match token {
        None => Ok(None),
        Some(t) => {
            let decoded = decode_token(t)?;
            let key = decoded.parse::<i64>().map_err(|_| ApiError::BadRequest {
                message: "invalid $skiptoken".to_string(),
            })?;
            Ok(Some(key))
        }
    }
}

/// Decode an opaque `$skiptoken` into a UUID keyset cursor — the UUID twin of
/// [`cursor_from_token`] (used by `/_sim/subscriptions`, whose PK
/// `synthetic.subscriptions.subscription_id` is a `UUID`, NOT a SERIAL, so the numeric
/// keyset does not apply). `None` (absent token) → no cursor. A token that is not url-safe
/// base64, or whose decoded payload is not a valid UUID, → 400 `BadRequest` with the SAME
/// fixed, non-leaking message the pagination codec uses — NEVER a 500, and no SQL/cursor
/// text ever leaks (T-14-03).
pub(crate) fn cursor_uuid_from_token(token: Option<&str>) -> Result<Option<Uuid>, ApiError> {
    match token {
        None => Ok(None),
        Some(t) => {
            let decoded = decode_token(t)?;
            let key = Uuid::parse_str(&decoded).map_err(|_| ApiError::BadRequest {
                message: "invalid $skiptoken".to_string(),
            })?;
            Ok(Some(key))
        }
    }
}

/// The `LIMIT top+1` surplus split for a UUID keyset — the UUID twin of [`split_numeric`].
/// Mirrors it exactly but reads the keyset column as a `Uuid` via `try_get` (the numeric
/// helper reads `i64`). Returns the kept page (surplus row dropped) and, when a surplus row
/// existed, the last KEPT row's UUID key (the next cursor); otherwise `None` (final page).
/// Used by the `/_sim/subscriptions` keyset read.
pub(crate) fn split_uuid(
    mut rows: Vec<PgRow>,
    top: i64,
    key_col: &str,
) -> Result<(Vec<PgRow>, Option<Uuid>), ApiError> {
    if rows.len() as i64 > top {
        rows.truncate(top as usize);
        // `top >= 1` (clamp_top floor) so after truncation at least one row remains.
        let key: Uuid = rows
            .last()
            .expect("a truncated-to-top page has >= 1 row")
            .try_get(key_col)?;
        Ok((rows, Some(key)))
    } else {
        Ok((rows, None))
    }
}

/// The `LIMIT top+1` surplus split for a NUMERIC keyset. Mirrors [`split_page`] but
/// reads an `i64` PgRow column via `try_get` (the `&str`-keyed [`split_page`] is awkward
/// with numeric PgRow keys). Returns the kept page (surplus row dropped) and, when a
/// surplus row existed, the last KEPT row's key (the next cursor); otherwise `None`
/// (final page). Shared by the drift audit reads and the `/_sim` collection reads.
pub(crate) fn split_numeric(
    mut rows: Vec<PgRow>,
    top: i64,
    key_col: &str,
) -> Result<(Vec<PgRow>, Option<i64>), ApiError> {
    if rows.len() as i64 > top {
        rows.truncate(top as usize);
        // `top >= 1` (clamp_top floor) so after truncation at least one row remains.
        let key: i64 = rows
            .last()
            .expect("a truncated-to-top page has >= 1 row")
            .try_get(key_col)?;
        Ok((rows, Some(key)))
    } else {
        Ok((rows, None))
    }
}

/// Build the absolute `nextLink` from the configured `base_url` (MOCK-08).
///
/// Preserves `$top`, the original `api-version` (when present), and the original
/// `$filter` (when present) so paging is self-consistent across pages — a filtered
/// traversal re-applies the same predicate on every page (Research Q2). EVERY query
/// value (`api-version` and `$filter`) is percent-encoded (SEC-MED-2) so odd input
/// (`&`, `#`, spaces, `'`) cannot inject a second parameter or a fragment — the
/// emitted `nextLink` is always a well-formed URL the client can replay verbatim.
pub fn next_link(
    base_url: &str,
    path: &str,
    top: i64,
    next_token: &str,
    api_version: Option<&str>,
    filter: Option<&str>,
) -> String {
    let mut url = format!("{base_url}{path}?$top={top}&$skiptoken={next_token}");
    if let Some(v) = api_version {
        // SEC-MED-2: percent-encode the value exactly as $filter is, so odd input
        // (`&`, `#`, spaces, `'`) cannot inject a second param or a fragment. Clean
        // ASCII versions like `2021-04-01` encode to themselves (byte-identical).
        url.push_str(&format!("&api-version={}", percent_encode_query(v)));
    }
    if let Some(f) = filter {
        url.push_str(&format!("&$filter={}", percent_encode_query(f)));
    }
    url
}

/// Percent-encode a query-component value (RFC 3986). Unreserved characters
/// (`A-Z a-z 0-9 - _ . ~`) pass through; everything else (spaces, `'`, `/`, `=`,
/// `&`, …) is `%XX`-encoded so the value survives a round-trip through a URL.
///
/// Hand-rolled (std-only) rather than pulling a `urlencoding`/`percent-encoding`
/// crate for one call site — the rule is small and auditable. `pub(crate)` so the
/// `/_sim` collection handlers can preserve their discrete filter params (which are NOT
/// OData `$filter`) in `nextLink` with the SAME SEC-MED-2 encoding bar (D-06).
pub(crate) fn percent_encode_query(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for &b in s.as_bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(b as char);
            }
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clamp_default_is_100() {
        assert_eq!(clamp_top(None), 100);
    }

    #[test]
    fn clamp_below_floor_is_1() {
        assert_eq!(clamp_top(Some(0)), 1);
        assert_eq!(clamp_top(Some(-50)), 1);
    }

    #[test]
    fn clamp_above_ceiling_is_1000() {
        assert_eq!(clamp_top(Some(1001)), 1000);
        assert_eq!(clamp_top(Some(999_999_999)), 1000);
    }

    #[test]
    fn clamp_within_range_passes_through() {
        assert_eq!(clamp_top(Some(250)), 250);
    }

    #[test]
    fn token_round_trips_arm_id() {
        let arm_id =
            "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-net-001";
        let encoded = encode_token(arm_id);
        // url-safe, no padding: never contains '+', '/', or '='.
        assert!(!encoded.contains('+') && !encoded.contains('/') && !encoded.contains('='));
        let decoded = decode_token(&encoded).expect("round-trip decode");
        assert_eq!(decoded, arm_id);
    }

    #[test]
    fn decode_rejects_garbage_token() {
        // '!' is outside the url-safe alphabet → BadRequest.
        let err = decode_token("not!base64!!").unwrap_err();
        match err {
            ApiError::BadRequest { message } => assert_eq!(message, "invalid $skiptoken"),
            other => panic!("expected BadRequest, got {other:?}"),
        }
    }

    #[test]
    fn next_link_includes_api_version_when_present() {
        let link = next_link(
            "http://test",
            "/subscriptions/s/resources",
            100,
            "TOK",
            Some("2021-04-01"),
            None,
        );
        assert_eq!(
            link,
            "http://test/subscriptions/s/resources?$top=100&$skiptoken=TOK&api-version=2021-04-01"
        );
    }

    #[test]
    fn next_link_omits_api_version_when_absent() {
        let link = next_link("http://test", "/p", 50, "TOK", None, None);
        assert_eq!(link, "http://test/p?$top=50&$skiptoken=TOK");
    }

    #[test]
    fn next_link_includes_filter_when_present() {
        // The $filter value is percent-encoded: space → %20, single quote → %27.
        let link = next_link(
            "http://test",
            "/subscriptions/s/resources",
            100,
            "TOK",
            None,
            Some("location eq 'eastus'"),
        );
        assert_eq!(
            link,
            "http://test/subscriptions/s/resources?$top=100&$skiptoken=TOK&$filter=location%20eq%20%27eastus%27"
        );
    }

    #[test]
    fn next_link_omits_filter_when_absent() {
        let link = next_link("http://test", "/p", 50, "TOK", None, None);
        assert_eq!(link, "http://test/p?$top=50&$skiptoken=TOK");
    }

    #[test]
    fn next_link_carries_both_api_version_and_filter() {
        // api-version is appended before $filter; both survive together.
        let link = next_link(
            "http://test",
            "/p",
            10,
            "TOK",
            Some("2021-04-01"),
            Some("resourceType eq 'X'"),
        );
        assert_eq!(
            link,
            "http://test/p?$top=10&$skiptoken=TOK&api-version=2021-04-01&$filter=resourceType%20eq%20%27X%27"
        );
    }

    #[test]
    fn next_link_percent_encodes_api_version() {
        // SEC-MED-2: an api-version carrying odd chars (`&`, space) must be
        // percent-encoded so it cannot inject a second query parameter.
        let link = next_link(
            "http://test",
            "/p",
            100,
            "TOK",
            Some("2021&injected=evil 01"),
            None,
        );
        // No raw `&` or space leaks after the encoded api-version value.
        assert_eq!(
            link,
            "http://test/p?$top=100&$skiptoken=TOK&api-version=2021%26injected%3Devil%2001"
        );
        // The only structural `&` separators are the ones we emit (top, skiptoken,
        // api-version) — exactly 2, never a third injected by the value.
        assert_eq!(link.matches('&').count(), 2);
    }

    #[test]
    fn next_link_no_param_injection_from_filter() {
        // SEC-MED-2: a hostile $filter with `&`, `#`, spaces, `'` produces a single
        // well-formed $filter value with every reserved char %XX-encoded — no
        // injected `&api-version=` and no `#fragment`.
        let hostile = "x' OR '1'='1 & api-version=evil # frag";
        let link = next_link("http://test", "/p", 100, "TOK", None, Some(hostile));
        // No raw fragment delimiter and no raw space survive in the value.
        assert!(!link.contains('#'), "no raw fragment delimiter may survive");
        assert!(!link.contains(' '), "no raw space may survive");
        // Exactly the structural separators we emit ($top, $skiptoken, $filter) = 2.
        assert_eq!(link.matches('&').count(), 2, "no injected &param= survives");
        assert_eq!(
            link,
            "http://test/p?$top=100&$skiptoken=TOK&$filter=x%27%20OR%20%271%27%3D%271%20%26%20api-version%3Devil%20%23%20frag"
        );
    }

    #[test]
    fn cursor_uuid_from_token_round_trips_and_rejects_garbage() {
        // Absent token → no cursor.
        assert!(cursor_uuid_from_token(None).unwrap().is_none());

        // A valid token (base64 of the UUID string) decodes back to Some(uuid).
        let u = uuid::Uuid::from_u128(0x1111_1111_1111_1111_1111_1111_1111_1111);
        let tok = encode_token(&u.to_string());
        assert_eq!(cursor_uuid_from_token(Some(&tok)).unwrap(), Some(u));

        // Non-url-safe-base64 payload → the fixed 400 (never a panic/500).
        match cursor_uuid_from_token(Some("not!base64!!")).unwrap_err() {
            ApiError::BadRequest { message } => assert_eq!(message, "invalid $skiptoken"),
            other => panic!("expected BadRequest, got {other:?}"),
        }

        // Valid base64 whose decoded payload is NOT a UUID ("aGVsbG8" = "hello") → fixed 400.
        match cursor_uuid_from_token(Some("aGVsbG8")).unwrap_err() {
            ApiError::BadRequest { message } => assert_eq!(message, "invalid $skiptoken"),
            other => panic!("expected BadRequest, got {other:?}"),
        }
    }

    #[test]
    fn split_page_emits_token_when_surplus_row_present() {
        // top+1 = 4 rows fetched for top=3 → extra page exists.
        let rows = vec!["a", "b", "c", "d"];
        let (page, token) = split_page(rows, 3, |r| r);
        assert_eq!(page, vec!["a", "b", "c"], "surplus row dropped");
        // cursor encodes the last RETURNED row ("c"), not the surplus ("d").
        assert_eq!(token, Some(encode_token("c")));
        assert_eq!(decode_token(&token.unwrap()).unwrap(), "c");
    }

    #[test]
    fn split_page_omits_token_on_last_page() {
        // Exactly top rows (no surplus) → final page, no cursor.
        let rows = vec!["a", "b", "c"];
        let (page, token) = split_page(rows, 3, |r| r);
        assert_eq!(page, vec!["a", "b", "c"]);
        assert_eq!(token, None);
    }

    #[test]
    fn split_page_handles_short_final_page() {
        let rows = vec!["a"];
        let (page, token) = split_page(rows, 3, |r| r);
        assert_eq!(page, vec!["a"]);
        assert_eq!(token, None);
    }
}
