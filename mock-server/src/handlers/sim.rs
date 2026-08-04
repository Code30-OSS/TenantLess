//! `GET /_sim/violations`, `/_sim/dependencies`, `/_sim/summary`, `/_sim/subscriptions` —
//! the bearer-EXEMPT, read-only `/_sim` projection surface for the Web Console (WAPI-01..04).
//!
//! `/_sim/subscriptions` (D-15, GAP-14-03) is the keyset-paginated full-enumeration companion
//! to the bounded inline `summary.subscriptions[]` preview — it walks EVERY subscription via a
//! UUID keyset on `subscription_id`, closing the T-14-05 unbounded-response residual.
//!
//! These endpoints expose simulator-internal projections that have NO ARM
//! equivalent: governance violations, cross-subscription dependencies, and a
//! tenant-summary aggregate. Unlike the drift audit reads (which sit INSIDE the bearer
//! gate — Ph11 D-15), `/_sim` is served OUTSIDE the ARM Bearer layer, merged on the same
//! exempt seam as `/_console` and `/token` (WAPI-04). Read-only is enforced STRUCTURALLY:
//! only GET handlers are registered, so axum's `MethodRouter` returns 405 for any
//! mutating method (see [`crate::sim`]).
//!
//! Filter safety (D-09, project SQL bar): every collection read binds every filter value
//! as `$N` behind a CLOSED field→column allowlist — user values NEVER splice into SQL.
//! `subscription` is parsed to a `Uuid` BEFORE it reaches SQL; a malformed value is a
//! fixed 400. Case behavior is explicit: `code`/`severity`/`type` compare via
//! `lower(col) = lower($N)` (Pitfall 6). The `violations` table has NO `subscription_id`
//! column, so the `?subscription=` filter and `subscriptionId` field come from a
//! `LEFT JOIN synthetic.resources r ON r.id = v.resource_id` — LEFT (not INNER) so a
//! violation whose resource is soft-deleted/dangling is not silently dropped. The join
//! key is exact: the violations writer stores `resource_id = r.id` (the full ARM id;
//! `generator/violations.py` `"resource_id": r.id`).
//!
//! Casing (D-10): responses use EXPLICIT per-field camelCase DTOs — never a recursive
//! key transform — so the violations `detail` JSONB (`{field, observed, ...}`) passes
//! through with its inner keys VERBATIM.
//!
//! `summary` (WAPI-03) is the one unpaginated aggregate (D-07): it assembles `totals`,
//! per-subscription rollups, `byType[]`, and `byLocation[]` inside a SINGLE
//! `REPEATABLE READ, READ ONLY` snapshot so the sections cannot disagree under concurrent
//! generation (D-11). `byType[].type` is canonicalized (`casing::canonical_type`); an empty
//! (schema-only) tenant returns zeros + null metadata rather than a 500.

use crate::{
    casing::canonical_type,
    error::ApiError,
    pagination::{
        clamp_top, cursor_from_token, cursor_uuid_from_token, decode_token, encode_token,
        next_link, percent_encode_query, split_numeric, split_page, split_uuid,
    },
    state::AppState,
};
use axum::{
    Json,
    extract::{RawQuery, State},
};
use serde::Serialize;
use serde_json::Value;
use sqlx::Row;
use sqlx::postgres::PgRow;
use sqlx::types::Json as SqlxJson;
use uuid::Uuid;

// ---------------------------------------------------------------------------------
// Shared filter machinery (the SQL-safety bar, D-09).
// ---------------------------------------------------------------------------------

/// One bound filter value. Kept type-tagged (rather than a `Vec<String>`) because the
/// `/_sim` filters bind heterogeneous Postgres types — `subscription` binds as a `UUID`
/// column comparison, the rest as `TEXT` — while still flowing ONLY through `.bind()`
/// (never spliced). The handler binds these in push order after `$1`(cursor)/`$2`(limit).
enum Bind {
    Uuid(Uuid),
    Text(String),
}

// ---------------------------------------------------------------------------------
// Fail-closed query parsing (D-16 / T-14-03).
//
// `/_sim` is a STRICT surface (unlike ARM's lenient unknown-param ignore, MOCK-11): a
// query param OUTSIDE the documented set for the endpoint, an out-of-domain filter value,
// or a malformed `$top`/`$skiptoken` ALL return the SAME fixed JSON `ApiError` 400 — never
// axum's default `Query` plain-text rejection, and never a misleading empty page. To make
// EVERY bad-input path route through `ApiError`, the collection handlers parse
// `axum::extract::RawQuery` themselves rather than a serde `Query<T>` extractor (whose
// `$top: Option<i64>` would reject a non-integer as a PLAIN-TEXT 400 before `ApiError`).
// ---------------------------------------------------------------------------------

/// Percent-decode one query-string component (RFC 3986 plus the form-urlencoded `+`→space
/// rule) so the manual parse is behavior-preserving vs axum's default `Query`
/// (`serde_urlencoded`, which decodes the same way). A malformed `%XX` escape (truncated /
/// non-hex) or invalid UTF-8 is a fixed JSON 400 — never a panic. This is the decode
/// counterpart to [`crate::pagination::percent_encode_query`] (which BUILDS `nextLink`), so
/// a filtered page replayed via its own `nextLink` round-trips byte-for-byte.
///
/// WR-01: a decoded ASCII control character (the C0 range `0x00..=0x1F` and DEL `0x7F`) is
/// ALSO a fixed JSON 400. A `%00` NUL decodes to valid UTF-8, so without this guard it would
/// bind and Postgres would reject it with an HTTP 500 — a reachable breach of the D-16
/// "every bad-input path is a fixed 400" invariant. Rejecting at this single choke point
/// (which every `/_sim` query key AND value passes through before binding) guarantees no
/// control byte — however encoded — reaches SQL, for any parameter.
fn percent_decode_query(s: &str) -> Result<String, ApiError> {
    fn hex(b: u8) -> Option<u8> {
        match b {
            b'0'..=b'9' => Some(b - b'0'),
            b'a'..=b'f' => Some(b - b'a' + 10),
            b'A'..=b'F' => Some(b - b'A' + 10),
            _ => None,
        }
    }
    let bytes = s.as_bytes();
    let mut out: Vec<u8> = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'+' => {
                out.push(b' ');
                i += 1;
            }
            b'%' => {
                let (h, l) = bytes
                    .get(i + 1)
                    .zip(bytes.get(i + 2))
                    .and_then(|(&h, &l)| hex(h).zip(hex(l)))
                    .ok_or_else(|| ApiError::bad_request("invalid query encoding"))?;
                out.push((h << 4) | l);
                i += 3;
            }
            byte => {
                out.push(byte);
                i += 1;
            }
        }
    }
    let decoded =
        String::from_utf8(out).map_err(|_| ApiError::bad_request("invalid query encoding"))?;
    // WR-01: reject any decoded ASCII control char (C0 + DEL). `char::is_ascii_control`
    // covers `0x00..=0x1F` and `0x7F` — including NUL, which is otherwise valid UTF-8 and
    // would bind through to a Postgres 500. Stopped here, it is the same fixed JSON 400.
    if decoded.chars().any(|c| c.is_ascii_control()) {
        return Err(ApiError::bad_request("invalid query encoding"));
    }
    Ok(decoded)
}

/// A fail-closed view of a `/_sim` collection endpoint's query string (D-16). Built from
/// `axum::extract::RawQuery`: it parses the raw string into percent-decoded pairs, REJECTS
/// any key outside the documented set (the pagination trio + the caller's `filter_keys`)
/// with a fixed JSON 400, and parses `$top` as an `i64` HERE so a non-integer is the fixed
/// JSON 400 (T-14-03) rather than axum's plain-text `Query` rejection. `$skiptoken`,
/// `api-version`, and the filter values are carried through verbatim for the handler (which
/// then applies the existing `subscription` UUID parse / `cursor_from_token` fixed-400s).
struct SimQuery {
    top: Option<i64>,
    skiptoken: Option<String>,
    api_version: Option<String>,
    filters: Vec<(String, String)>,
}

impl SimQuery {
    /// Parse + validate the raw query for an endpoint whose allowed FILTER keys are
    /// `filter_keys` (the pagination trio `$top`/`$skiptoken`/`api-version` is ALWAYS
    /// allowed). Fail-closed: an unknown key, a malformed `$top`, or a malformed `%XX`
    /// encoding each returns the fixed JSON `ApiError` 400.
    fn parse(raw: Option<String>, filter_keys: &[&str]) -> Result<Self, ApiError> {
        let mut top = None;
        let mut skiptoken = None;
        let mut api_version = None;
        let mut filters: Vec<(String, String)> = Vec::new();

        let raw = raw.unwrap_or_default();
        for segment in raw.split('&').filter(|s| !s.is_empty()) {
            let (k, v) = match segment.split_once('=') {
                Some((k, v)) => (k, v),
                None => (segment, ""),
            };
            let key = percent_decode_query(k)?;
            let val = percent_decode_query(v)?;
            match key.as_str() {
                "$top" => {
                    top = Some(
                        val.parse::<i64>()
                            .map_err(|_| ApiError::bad_request("invalid $top"))?,
                    );
                }
                "$skiptoken" => skiptoken = Some(val),
                "api-version" => api_version = Some(val),
                other if filter_keys.contains(&other) => filters.push((key, val)),
                _ => return Err(ApiError::bad_request("unknown query parameter")),
            }
        }
        Ok(SimQuery {
            top,
            skiptoken,
            api_version,
            filters,
        })
    }

    /// The decoded value of a documented filter key, if the client supplied it.
    fn filter(&self, key: &str) -> Option<&str> {
        self.filters
            .iter()
            .find(|(k, _)| k == key)
            .map(|(_, v)| v.as_str())
    }
}

/// Build the absolute `nextLink` for a `/_sim` collection page. The shared
/// [`next_link`] handles `$top`/`$skiptoken`/`api-version`; the discrete `/_sim` filter
/// params (which are NOT OData `$filter`) are appended here so page 2+ re-applies the
/// SAME predicate (D-06). Each value is percent-encoded with the SAME SEC-MED-2 bar the
/// pagination codec uses, so a hostile value cannot inject a second query parameter.
fn sim_next_link(
    base_url: &str,
    path: &str,
    top: i64,
    token: &str,
    api_version: Option<&str>,
    filters: &[(&str, &str)],
) -> String {
    let mut url = next_link(base_url, path, top, token, api_version, None);
    for (k, v) in filters {
        url.push_str(&format!("&{k}={}", percent_encode_query(v)));
    }
    url
}

// ---------------------------------------------------------------------------------
// WAPI-01 — list_violations
// ---------------------------------------------------------------------------------

/// Parsed + validated violation filters (subscription already `Uuid`-parsed — D-09).
struct ViolationFilters {
    subscription: Option<Uuid>,
    code: Option<String>,
    resource: Option<String>,
    severity: Option<String>,
}

/// Build the `WHERE`-conjunct fragment + the parallel bound-value list for the violation
/// filters (D-09). The SQL string carries ONLY column names, boolean keywords, and `$N`
/// placeholders — every user value is returned as a [`Bind`] for the handler to `.bind()`.
/// `start_idx` is the LAST placeholder already consumed by the caller: the page query passes
/// `2` ( `$1` = keyset cursor, `$2` = LIMIT top+1 ) so filters begin at `$3`; the cursor-less
/// COUNT query passes `0` so the SAME filters begin at `$1`. The field→column allowlist and
/// the `$N`-bind guarantee are IDENTICAL across both bases (D-13 / T-14-01). Case-insensitive
/// exact match for `code`/`severity` via `lower()=lower()` (Pitfall 6).
fn violation_where(f: &ViolationFilters, start_idx: i32) -> (String, Vec<Bind>) {
    let mut conj: Vec<String> = Vec::new();
    let mut binds: Vec<Bind> = Vec::new();
    let mut idx = start_idx;
    if let Some(sub) = f.subscription {
        idx += 1;
        conj.push(format!("r.subscription_id = ${idx}"));
        binds.push(Bind::Uuid(sub));
    }
    if let Some(c) = &f.code {
        idx += 1;
        conj.push(format!("lower(v.violation_type) = lower(${idx})"));
        binds.push(Bind::Text(c.clone()));
    }
    if let Some(r) = &f.resource {
        idx += 1;
        conj.push(format!("v.resource_id = ${idx}"));
        binds.push(Bind::Text(r.clone()));
    }
    if let Some(s) = &f.severity {
        idx += 1;
        conj.push(format!("lower(v.severity) = lower(${idx})"));
        binds.push(Bind::Text(s.clone()));
    }
    let where_extra = if conj.is_empty() {
        String::new()
    } else {
        format!(" AND {}", conj.join(" AND "))
    };
    (where_extra, binds)
}

/// One governance violation (WAPI-01). `code` ← `violation_type` (UPPER_SNAKE);
/// `subscriptionId` comes from the LEFT JOIN (`None` if the resource was removed);
/// `detail` is the raw JSONB PASSTHROUGH — serde renames STRUCT fields only, so the
/// inner `{field, observed, ...}` keys are emitted verbatim (D-10).
#[derive(Serialize)]
pub struct ViolationDto {
    #[serde(rename = "resourceId")]
    resource_id: String,
    code: String,
    severity: String,
    #[serde(rename = "subscriptionId")]
    subscription_id: Option<String>,
    detail: Value,
}

/// `GET /_sim/violations` envelope — a keyset-paginated page of violations. `nextLink`
/// carries the opaque `$skiptoken` continuation (plus the active filters + api-version)
/// when more rows exist past this page; omitted on the final page.
#[derive(Serialize)]
pub struct ViolationList {
    /// Total rows MATCHING THE ACTIVE FILTER (a `COUNT(*)` over the same predicate as the
    /// page query — NOT the unfiltered table total, NOT the page size). Spec-required (D-13).
    count: i64,
    value: Vec<ViolationDto>,
    #[serde(rename = "nextLink", skip_serializing_if = "Option::is_none")]
    next_link: Option<String>,
}

fn violation_dto(row: &PgRow) -> Result<ViolationDto, ApiError> {
    let subscription_id: Option<Uuid> = row.try_get("subscription_id")?;
    let detail: SqlxJson<Value> = row.try_get("detail")?;
    Ok(ViolationDto {
        resource_id: row.try_get("resource_id")?,
        code: row.try_get("violation_type")?,
        severity: row.try_get("severity")?,
        subscription_id: subscription_id.map(|s| s.to_string()),
        detail: detail.0,
    })
}

/// `GET /_sim/violations` — governance violations, keyset-paginated (WAPI-01).
///
/// SERIAL `id` is cast `::bigint` so the shared i64 keyset helpers apply (Pitfall 7). The
/// `?subscription=` filter narrows via `LEFT JOIN synthetic.resources` on `r.subscription_id`
/// (Pitfall 2). The cursor binds as `$1::bigint` (NULL on page 1), the limit as `$2`, then
/// the filter values in push order — none is ever spliced (project SQL bar).
pub async fn list_violations(
    State(state): State<AppState>,
    RawQuery(raw): RawQuery,
) -> Result<Json<ViolationList>, ApiError> {
    // Fail-closed parse (D-16): reject unknown params + malformed `$top` as the fixed JSON
    // 400 (the documented set = the pagination trio + this endpoint's discrete filters).
    let qs = SimQuery::parse(raw, &["subscription", "code", "resource", "severity"])?;

    // `severity` is a CLOSED domain {High, Medium, Low}, case-insensitive: an out-of-domain
    // value is a fixed 400, NOT a misleading empty page (D-16). `code`/`resource` stay
    // OPEN-domain (an unknown value legitimately yields an empty page) — case-insensitive.
    if let Some(sev) = qs.filter("severity")
        && !matches!(sev.to_ascii_lowercase().as_str(), "high" | "medium" | "low")
    {
        return Err(ApiError::bad_request("invalid severity"));
    }

    // Parse subscription to Uuid BEFORE SQL (D-09); a malformed value is a fixed 400.
    let sub = match qs.filter("subscription") {
        Some(s) => {
            Some(Uuid::parse_str(s).map_err(|_| ApiError::bad_request("invalid subscription"))?)
        }
        None => None,
    };
    let filters = ViolationFilters {
        subscription: sub,
        code: qs.filter("code").map(str::to_string),
        resource: qs.filter("resource").map(str::to_string),
        severity: qs.filter("severity").map(str::to_string),
    };
    // Page query: filters start at `$3` (after `$1` cursor + `$2` limit).
    let (where_extra, binds) = violation_where(&filters, 2);

    // Filtered COUNT(*): the SAME table/JOIN + the SAME allowlist fragment, rebuilt to start
    // at `$1` (no cursor/limit) so `count` = the whole set matching the active filter, not the
    // page. `WHERE 1=1{count_where}` lets the `AND`-prefixed fragment append cleanly (and be
    // empty when unfiltered). Values are `$N`-bound in push order — never spliced (D-13/T-14-01).
    let (count_where, count_binds) = violation_where(&filters, 0);
    let count_sql = format!(
        "SELECT count(*) AS n \
         FROM synthetic.violations v \
         LEFT JOIN synthetic.resources r ON r.id = v.resource_id \
         WHERE 1=1{count_where}"
    );
    let mut cq = sqlx::query(&count_sql);
    for b in &count_binds {
        cq = match b {
            Bind::Uuid(u) => cq.bind(*u),
            Bind::Text(t) => cq.bind(t.clone()),
        };
    }
    let count: i64 = cq.fetch_one(&state.pool).await?.try_get("n")?;

    let top = clamp_top(qs.top);
    let cursor = cursor_from_token(qs.skiptoken.as_deref())?;
    let sql = format!(
        "SELECT v.id::bigint AS id, v.resource_id, v.violation_type, v.severity, v.detail, \
                r.subscription_id \
         FROM synthetic.violations v \
         LEFT JOIN synthetic.resources r ON r.id = v.resource_id \
         WHERE ($1::bigint IS NULL OR v.id > $1){where_extra} \
         ORDER BY v.id LIMIT $2"
    );
    let mut q = sqlx::query(&sql).bind(cursor).bind(top + 1);
    for b in &binds {
        q = match b {
            Bind::Uuid(u) => q.bind(*u),
            Bind::Text(t) => q.bind(t.clone()),
        };
    }
    let rows = q.fetch_all(&state.pool).await?;
    let (page, next_key) = split_numeric(rows, top, "id")?;
    let mut value = Vec::with_capacity(page.len());
    for row in &page {
        value.push(violation_dto(row)?);
    }
    let next_link = next_key.map(|key| {
        let mut active: Vec<(&str, String)> = Vec::new();
        if let Some(s) = &sub {
            active.push(("subscription", s.to_string()));
        }
        if let Some(c) = qs.filter("code") {
            active.push(("code", c.to_string()));
        }
        if let Some(r) = qs.filter("resource") {
            active.push(("resource", r.to_string()));
        }
        if let Some(sev) = qs.filter("severity") {
            active.push(("severity", sev.to_string()));
        }
        let active_ref: Vec<(&str, &str)> = active.iter().map(|(k, v)| (*k, v.as_str())).collect();
        sim_next_link(
            &state.base_url,
            "/_sim/violations",
            top,
            &encode_token(&key.to_string()),
            qs.api_version.as_deref(),
            &active_ref,
        )
    });
    Ok(Json(ViolationList {
        count,
        value,
        next_link,
    }))
}

// ---------------------------------------------------------------------------------
// WAPI-02 — list_dependencies
// ---------------------------------------------------------------------------------

/// Parsed + validated dependency filters (subscription already `Uuid`-parsed — D-09).
struct DependencyFilters {
    subscription: Option<Uuid>,
    dep_type: Option<String>,
}

/// Build the `WHERE`-conjunct fragment + parallel bound-value list for dependency filters
/// (D-09). `subscription` matches source OR target via ONE bound value used TWICE
/// (`(source_subscription = $N OR target_subscription = $N)`) — a single [`Bind`], two
/// `$N` uses. `type` is case-insensitive (`lower()=lower()`). `start_idx` is the last
/// placeholder already consumed (page query: `2` so filters begin at `$3`; cursor-less
/// COUNT query: `0` so the SAME filters begin at `$1`) — the allowlist and single-bind
/// property hold at either base (D-13 / T-14-01).
fn dependency_where(f: &DependencyFilters, start_idx: i32) -> (String, Vec<Bind>) {
    let mut conj: Vec<String> = Vec::new();
    let mut binds: Vec<Bind> = Vec::new();
    let mut idx = start_idx;
    if let Some(sub) = f.subscription {
        idx += 1;
        let p = idx;
        conj.push(format!(
            "(source_subscription = ${p} OR target_subscription = ${p})"
        ));
        binds.push(Bind::Uuid(sub)); // ONE bind, two `$p` uses
    }
    if let Some(t) = &f.dep_type {
        idx += 1;
        conj.push(format!("lower(dependency_type) = lower(${idx})"));
        binds.push(Bind::Text(t.clone()));
    }
    let where_extra = if conj.is_empty() {
        String::new()
    } else {
        format!(" AND {}", conj.join(" AND "))
    };
    (where_extra, binds)
}

/// One endpoint (source or target) of a dependency edge — the nested spec object carrying
/// the ARM `resourceId` and its owning `subscriptionId` (D-13, `sim-api-spec.md`). Explicit
/// camelCase per D-10 (the nested reshape touches only field names, never JSONB inner keys).
#[derive(Serialize)]
pub struct DependencyEndpoint {
    #[serde(rename = "resourceId")]
    resource_id: String,
    #[serde(rename = "subscriptionId")]
    subscription_id: String,
}

/// One cross-subscription dependency edge (WAPI-02), in the NESTED spec shape (D-13):
/// `{ type, source:{resourceId,subscriptionId}, target:{resourceId,subscriptionId},
/// crossSubscription }`. `type` (NOT the old flat `dependencyType`); `crossSubscription` is
/// derived in Rust as `source_subscription != target_subscription` (D-08). The two
/// subscription ids are always present (`NOT NULL` columns).
#[derive(Serialize)]
pub struct DependencyDto {
    #[serde(rename = "type")]
    dep_type: String,
    source: DependencyEndpoint,
    target: DependencyEndpoint,
    #[serde(rename = "crossSubscription")]
    cross_subscription: bool,
}

/// `GET /_sim/dependencies` envelope — a keyset-paginated page of edges. `nextLink`
/// carries the opaque `$skiptoken` continuation (plus active filters + api-version) when
/// more rows exist past this page; omitted on the final page.
#[derive(Serialize)]
pub struct DependencyList {
    /// Total edges MATCHING THE ACTIVE FILTER (a source-OR-target `COUNT(*)` over the same
    /// predicate as the page query — NOT the table total, NOT the page size). D-13.
    count: i64,
    value: Vec<DependencyDto>,
    #[serde(rename = "nextLink", skip_serializing_if = "Option::is_none")]
    next_link: Option<String>,
}

fn dependency_dto(row: &PgRow) -> Result<DependencyDto, ApiError> {
    let source: Uuid = row.try_get("source_subscription")?;
    let target: Uuid = row.try_get("target_subscription")?;
    Ok(DependencyDto {
        dep_type: row.try_get("dependency_type")?,
        source: DependencyEndpoint {
            resource_id: row.try_get("source_resource_id")?,
            subscription_id: source.to_string(),
        },
        target: DependencyEndpoint {
            resource_id: row.try_get("target_resource_id")?,
            subscription_id: target.to_string(),
        },
        cross_subscription: source != target,
    })
}

/// `GET /_sim/dependencies` — cross-subscription dependency edges, keyset-paginated
/// (WAPI-02). A `list_violations` twin over `synthetic.dependencies`: SERIAL `id` cast
/// `::bigint` for the shared i64 keyset; `?subscription=` matches source OR target (one
/// bound value, twice); `crossSubscription` derived in Rust.
pub async fn list_dependencies(
    State(state): State<AppState>,
    RawQuery(raw): RawQuery,
) -> Result<Json<DependencyList>, ApiError> {
    // Fail-closed parse (D-16): documented set = the pagination trio + subscription/type.
    // An unknown KEY is a fixed JSON 400; a malformed `$top` is the SAME fixed shape. `type`
    // stays OPEN-domain (an unknown type yields an empty page, consistent with violations
    // `code`) — only the KEY set is closed, the `type` VALUE is not.
    let qs = SimQuery::parse(raw, &["subscription", "type"])?;

    let sub = match qs.filter("subscription") {
        Some(s) => {
            Some(Uuid::parse_str(s).map_err(|_| ApiError::bad_request("invalid subscription"))?)
        }
        None => None,
    };
    let filters = DependencyFilters {
        subscription: sub,
        dep_type: qs.filter("type").map(str::to_string),
    };
    // Page query: filters start at `$3` (after `$1` cursor + `$2` limit).
    let (where_extra, binds) = dependency_where(&filters, 2);

    // Filtered COUNT(*): the SAME table + the SAME source-OR-target allowlist fragment, rebuilt
    // to start at `$1` (no cursor/limit). `subscription` is still ONE bound value used twice.
    // `count` = the whole set matching the active filter, not the page (D-13/T-14-01).
    let (count_where, count_binds) = dependency_where(&filters, 0);
    let count_sql =
        format!("SELECT count(*) AS n FROM synthetic.dependencies WHERE 1=1{count_where}");
    let mut cq = sqlx::query(&count_sql);
    for b in &count_binds {
        cq = match b {
            Bind::Uuid(u) => cq.bind(*u),
            Bind::Text(t) => cq.bind(t.clone()),
        };
    }
    let count: i64 = cq.fetch_one(&state.pool).await?.try_get("n")?;

    let top = clamp_top(qs.top);
    let cursor = cursor_from_token(qs.skiptoken.as_deref())?;
    let sql = format!(
        "SELECT id::bigint AS id, dependency_type, source_resource_id, target_resource_id, \
                source_subscription, target_subscription \
         FROM synthetic.dependencies \
         WHERE ($1::bigint IS NULL OR id > $1){where_extra} \
         ORDER BY id LIMIT $2"
    );
    let mut q = sqlx::query(&sql).bind(cursor).bind(top + 1);
    for b in &binds {
        q = match b {
            Bind::Uuid(u) => q.bind(*u),
            Bind::Text(t) => q.bind(t.clone()),
        };
    }
    let rows = q.fetch_all(&state.pool).await?;
    let (page, next_key) = split_numeric(rows, top, "id")?;
    let mut value = Vec::with_capacity(page.len());
    for row in &page {
        value.push(dependency_dto(row)?);
    }
    let next_link = next_key.map(|key| {
        let mut active: Vec<(&str, String)> = Vec::new();
        if let Some(s) = &sub {
            active.push(("subscription", s.to_string()));
        }
        if let Some(t) = qs.filter("type") {
            active.push(("type", t.to_string()));
        }
        let active_ref: Vec<(&str, &str)> = active.iter().map(|(k, v)| (*k, v.as_str())).collect();
        sim_next_link(
            &state.base_url,
            "/_sim/dependencies",
            top,
            &encode_token(&key.to_string()),
            qs.api_version.as_deref(),
            &active_ref,
        )
    });
    Ok(Json(DependencyList {
        count,
        value,
        next_link,
    }))
}

// ---------------------------------------------------------------------------------
// WAPI-03 — summary (the unpaginated tenant-summary aggregate, D-07/D-11)
// ---------------------------------------------------------------------------------

/// Tenant-wide `COUNT(*)` totals. Resource counts EXCLUDE soft-deleted rows
/// (`drift_deleted_at IS NOT NULL`), matching what ARM serves (A3).
#[derive(Serialize)]
pub struct SummaryTotals {
    subscriptions: i64,
    #[serde(rename = "resourceGroups")]
    resource_groups: i64,
    resources: i64,
    violations: i64,
    dependencies: i64,
}

/// Per-subscription rollup (D-11). Counts come from CTE-per-metric LEFT JOINs so a
/// subscription with no resources/RGs/violations still appears with zeros.
#[derive(Serialize)]
pub struct SummarySubscription {
    #[serde(rename = "subscriptionId")]
    subscription_id: String,
    name: String,
    archetype: String,
    #[serde(rename = "resourceCount")]
    resource_count: i64,
    #[serde(rename = "resourceGroupCount")]
    resource_group_count: i64,
    #[serde(rename = "violationCount")]
    violation_count: i64,
}

/// Build a [`SummarySubscription`] from a per-subscription rollup row. Shared by the inline
/// `summary.subscriptions[]` preview and the paginated `GET /_sim/subscriptions` endpoint —
/// both SELECT the SAME CTE-per-metric columns (`subscription_id`, `name`, `archetype`,
/// `resource_count`, `resource_group_count`, `violation_count`), so the DTO extraction is
/// identical and lives in one place (D-15).
fn subscription_row_dto(row: &PgRow) -> Result<SummarySubscription, ApiError> {
    let sid: Uuid = row.try_get("subscription_id")?;
    Ok(SummarySubscription {
        subscription_id: sid.to_string(),
        name: row.try_get("name")?,
        archetype: row.try_get("archetype")?,
        resource_count: row.try_get("resource_count")?,
        resource_group_count: row.try_get("resource_group_count")?,
        violation_count: row.try_get("violation_count")?,
    })
}

/// One `byType[]` bucket. The `type` string is canonicalized via
/// [`canonical_type`] (a single-STRING map — NOT the D-10-forbidden recursive transform).
#[derive(Serialize)]
pub struct SummaryByType {
    #[serde(rename = "type")]
    resource_type: String,
    count: i64,
}

/// One `byLocation[]` bucket.
#[derive(Serialize)]
pub struct SummaryByLocation {
    location: String,
    count: i64,
}

/// The `/_sim/summary` payload (D-07: always a single unpaginated object). Tenant
/// metadata (`tenantId`/`seed`/`profile`) is flattened at the top level per the
/// `sim-api-spec.md` shape; every field is `Option` so an empty (schema-only) tenant
/// serializes them as `null` (Pitfall 5) rather than 500-ing. `profile` is the
/// generation-profile NAME sourced from the nullable `synthetic.tenant.profile_name`
/// column (D-14, superseding the earlier A1 `profile_version` compromise).
#[derive(Serialize)]
pub struct SummaryResponse {
    #[serde(rename = "tenantId")]
    tenant_id: Option<String>,
    seed: Option<i64>,
    profile: Option<String>,
    totals: SummaryTotals,
    subscriptions: Vec<SummarySubscription>,
    #[serde(rename = "byType")]
    by_type: Vec<SummaryByType>,
    #[serde(rename = "byLocation")]
    by_location: Vec<SummaryByLocation>,
}

/// `GET /_sim/summary` — the unpaginated tenant-summary aggregate (WAPI-03).
///
/// ALL sections are computed inside ONE `pool.begin()` transaction set to
/// `REPEATABLE READ, READ ONLY`, so `totals`, the per-subscription rollups, `byType[]`,
/// and `byLocation[]` share a SINGLE consistent snapshot — they cannot disagree even if a
/// generation run mutates `synthetic.*` concurrently (D-11 / T-14-07). Resource counts
/// filter `drift_deleted_at IS NULL` (A3). The tenant row is read with `fetch_optional`,
/// so a schema-only container yields zeros + null metadata rather than a panic (Pitfall 5).
/// `byType`/`byLocation` are `ORDER BY count DESC, key ASC` (deterministic) and capped at
/// `LIMIT 500` (D-11 high-cardinality cap / T-14-05). The inline `subscriptions[]` is a
/// BOUNDED preview too (D-15): `ORDER BY resource_count DESC, subscription_id ASC LIMIT 500`;
/// full enumeration is served by the keyset-paginated [`list_subscriptions`]
/// (`GET /_sim/subscriptions`), and the >500-subscription residual is an accepted risk.
pub async fn summary(State(state): State<AppState>) -> Result<Json<SummaryResponse>, ApiError> {
    // One read-only snapshot for every section (D-11). READ ONLY documents intent and lets
    // Postgres optimize; the SET must be the first statement in the transaction.
    let mut tx = state.pool.begin().await?;
    sqlx::query("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        .execute(&mut *tx)
        .await?;

    // (1) totals — one row of independent sub-selects (soft-deleted resources excluded).
    let totals_row = sqlx::query(
        "SELECT (SELECT count(*) FROM synthetic.subscriptions)                            AS subscriptions, \
                (SELECT count(*) FROM synthetic.resource_groups)                          AS resource_groups, \
                (SELECT count(*) FROM synthetic.resources WHERE drift_deleted_at IS NULL) AS resources, \
                (SELECT count(*) FROM synthetic.violations)                               AS violations, \
                (SELECT count(*) FROM synthetic.dependencies)                             AS dependencies",
    )
    .fetch_one(&mut *tx)
    .await?;
    let totals = SummaryTotals {
        subscriptions: totals_row.try_get("subscriptions")?,
        resource_groups: totals_row.try_get("resource_groups")?,
        resources: totals_row.try_get("resources")?,
        violations: totals_row.try_get("violations")?,
        dependencies: totals_row.try_get("dependencies")?,
    };

    // (2) subscriptions[] — CTE-per-metric (NOT a triple LEFT JOIN: that would blow up
    //     cartesian-style at 2000 subs / 500K resources). BOUNDED preview (D-15 / T-14-05):
    //     deterministically `ORDER BY resource_count DESC, s.subscription_id ASC` and capped
    //     at `LIMIT 500` (the same ceiling as byType/byLocation). The residual — tenants with
    //     >500 subscriptions don't see every sub inline — is an ACCEPTED risk (14-SECURITY.md),
    //     mitigated by the keyset-paginated `GET /_sim/subscriptions` for full enumeration.
    let sub_rows = sqlx::query(
        "WITH res AS (SELECT subscription_id, count(*) c FROM synthetic.resources \
                      WHERE drift_deleted_at IS NULL GROUP BY 1), \
              rgs AS (SELECT subscription_id, count(*) c FROM synthetic.resource_groups GROUP BY 1), \
              viol AS (SELECT r.subscription_id, count(*) c \
                       FROM synthetic.violations v \
                       JOIN synthetic.resources r ON r.id = v.resource_id GROUP BY 1) \
         SELECT s.subscription_id, s.display_name AS name, s.archetype, \
                COALESCE(res.c, 0)  AS resource_count, \
                COALESCE(rgs.c, 0)  AS resource_group_count, \
                COALESCE(viol.c, 0) AS violation_count \
         FROM synthetic.subscriptions s \
         LEFT JOIN res  ON res.subscription_id  = s.subscription_id \
         LEFT JOIN rgs  ON rgs.subscription_id  = s.subscription_id \
         LEFT JOIN viol ON viol.subscription_id = s.subscription_id \
         ORDER BY resource_count DESC, s.subscription_id ASC LIMIT 500",
    )
    .fetch_all(&mut *tx)
    .await?;
    let mut subscriptions = Vec::with_capacity(sub_rows.len());
    for row in &sub_rows {
        subscriptions.push(subscription_row_dto(row)?);
    }

    // (3) byType[] — capped, deterministic; canonicalize the type STRING in Rust (A single
    //     resource holds ONE stored casing per type, so post-hoc canonicalization does not
    //     merge distinct DB groups in a real tenant).
    let type_rows = sqlx::query(
        "SELECT type, count(*) c FROM synthetic.resources WHERE drift_deleted_at IS NULL \
         GROUP BY type ORDER BY c DESC, type ASC LIMIT 500",
    )
    .fetch_all(&mut *tx)
    .await?;
    let mut by_type = Vec::with_capacity(type_rows.len());
    for row in &type_rows {
        let raw: String = row.try_get("type")?;
        by_type.push(SummaryByType {
            resource_type: canonical_type(&raw),
            count: row.try_get("c")?,
        });
    }

    // (4) byLocation[] — capped, deterministic.
    let loc_rows = sqlx::query(
        "SELECT location, count(*) c FROM synthetic.resources WHERE drift_deleted_at IS NULL \
         GROUP BY location ORDER BY c DESC, location ASC LIMIT 500",
    )
    .fetch_all(&mut *tx)
    .await?;
    let mut by_location = Vec::with_capacity(loc_rows.len());
    for row in &loc_rows {
        by_location.push(SummaryByLocation {
            location: row.try_get("location")?,
            count: row.try_get("c")?,
        });
    }

    // (5) tenant metadata — `fetch_optional`, NOT `fetch_one` (empty tenant → None ⇒ null
    //     metadata, never a 500). `profile` maps to the generation-profile NAME
    //     `profile_name` (D-14 supersedes the A1 `profile_version` compromise) — a nullable
    //     column, so an un-set / pre-Phase-14 tenant reads NULL ⇒ `profile: null`; `seed`
    //     lives in `scale_params` JSONB.
    let tenant_row = sqlx::query(
        "SELECT tenant_id, profile_name, (scale_params->>'seed')::bigint AS seed \
         FROM synthetic.tenant LIMIT 1",
    )
    .fetch_optional(&mut *tx)
    .await?;

    // Read-only tx — commit (or rollback) merely releases the snapshot.
    tx.commit().await?;

    let (tenant_id, profile, seed) = match tenant_row {
        Some(row) => {
            let tid: Uuid = row.try_get("tenant_id")?;
            // `profile_name` is NULLABLE (sql/007): an un-set / pre-Phase-14 tenant reads
            // NULL ⇒ `profile: null` (never a 500). D-14: this is the profile IDENTITY.
            let pn: Option<String> = row.try_get("profile_name")?;
            let sd: Option<i64> = row.try_get("seed")?;
            (Some(tid.to_string()), pn, sd)
        }
        None => (None, None, None),
    };

    Ok(Json(SummaryResponse {
        tenant_id,
        seed,
        profile,
        totals,
        subscriptions,
        by_type,
        by_location,
    }))
}

// ---------------------------------------------------------------------------------
// WAPI-03 / D-15 — list_subscriptions (the keyset-paginated full-enumeration endpoint)
// ---------------------------------------------------------------------------------

/// `GET /_sim/subscriptions` envelope — a keyset-paginated page of per-subscription rollups
/// (the SAME [`SummarySubscription`] shape the inline `summary.subscriptions[]` preview uses).
/// `nextLink` carries the opaque `$skiptoken` continuation (plus `api-version`) when more
/// rows exist past this page; omitted on the final page. `count` is the total subscription
/// count (D-13) — v1 has no filters, so it is the whole-table `COUNT(*)`.
#[derive(Serialize)]
pub struct SubscriptionList {
    /// Total subscriptions (D-13). No filters in v1, so this is `COUNT(*)` of the table —
    /// it always reports the FULL tenant size even though the inline summary preview caps at 500.
    count: i64,
    value: Vec<SummarySubscription>,
    #[serde(rename = "nextLink", skip_serializing_if = "Option::is_none")]
    next_link: Option<String>,
}

/// `GET /_sim/subscriptions` — full per-subscription enumeration, keyset-paginated on the
/// UUID PK `subscription_id` (WAPI-03 / D-15, superseding the D-01 three-route scope). This
/// is the unbounded-enumeration companion to the bounded inline `summary.subscriptions[]`
/// preview: it walks EVERY subscription across pages, closing the T-14-05 residual.
///
/// The per-subscription rollup SQL is the SAME CTE-per-metric shape as [`summary`], but
/// keyset-paginated: `WHERE ($1::uuid IS NULL OR s.subscription_id > $1) ORDER BY
/// s.subscription_id LIMIT $2`. The keyset is the UUID PK (NOT the numeric SERIAL keyset the
/// collection endpoints use — `subscription_id` is a `UUID`), so the cursor is decoded via
/// [`cursor_uuid_from_token`] and bound as `$1::uuid` (NULL on page 1). Fail-closed parse
/// (D-16): the documented param set is the pagination trio ONLY (no filters in v1) — any
/// other key, or a malformed `$top`/`$skiptoken`, is the fixed JSON `ApiError` 400.
pub async fn list_subscriptions(
    State(state): State<AppState>,
    RawQuery(raw): RawQuery,
) -> Result<Json<SubscriptionList>, ApiError> {
    // Fail-closed parse (D-16): the documented set is the pagination trio ONLY — this endpoint
    // has NO filters in v1. Any other key, or a malformed `$top`, is the fixed JSON 400.
    let qs = SimQuery::parse(raw, &[])?;

    // `count` = total subscriptions (D-13). No filter in v1, so it is the whole-table COUNT(*)
    // — and it always reports the FULL tenant size, unlike the 500-capped inline preview.
    let count: i64 = sqlx::query_scalar("SELECT count(*) FROM synthetic.subscriptions")
        .fetch_one(&state.pool)
        .await?;

    let top = clamp_top(qs.top);
    let cursor = cursor_uuid_from_token(qs.skiptoken.as_deref())?;

    // Keyset on the UUID PK `subscription_id` (deterministic total order — the PK is unique).
    // Same CTE-per-metric rollup as `summary`; the cursor binds as `$1::uuid` (NULL page 1),
    // the limit as `$2`. No user value is ever spliced (project SQL bar).
    let sql = "WITH res AS (SELECT subscription_id, count(*) c FROM synthetic.resources \
                            WHERE drift_deleted_at IS NULL GROUP BY 1), \
                    rgs AS (SELECT subscription_id, count(*) c FROM synthetic.resource_groups GROUP BY 1), \
                    viol AS (SELECT r.subscription_id, count(*) c \
                             FROM synthetic.violations v \
                             JOIN synthetic.resources r ON r.id = v.resource_id GROUP BY 1) \
               SELECT s.subscription_id, s.display_name AS name, s.archetype, \
                      COALESCE(res.c, 0)  AS resource_count, \
                      COALESCE(rgs.c, 0)  AS resource_group_count, \
                      COALESCE(viol.c, 0) AS violation_count \
               FROM synthetic.subscriptions s \
               LEFT JOIN res  ON res.subscription_id  = s.subscription_id \
               LEFT JOIN rgs  ON rgs.subscription_id  = s.subscription_id \
               LEFT JOIN viol ON viol.subscription_id = s.subscription_id \
               WHERE ($1::uuid IS NULL OR s.subscription_id > $1) \
               ORDER BY s.subscription_id LIMIT $2";
    let rows = sqlx::query(sql)
        .bind(cursor)
        .bind(top + 1)
        .fetch_all(&state.pool)
        .await?;
    let (page, next_key) = split_uuid(rows, top, "subscription_id")?;
    let mut value = Vec::with_capacity(page.len());
    for row in &page {
        value.push(subscription_row_dto(row)?);
    }
    let next_link = next_key.map(|key| {
        // No filters in v1 — preserve only `api-version` on the continuation link.
        sim_next_link(
            &state.base_url,
            "/_sim/subscriptions",
            top,
            &encode_token(&key.to_string()),
            qs.api_version.as_deref(),
            &[],
        )
    });
    Ok(Json(SubscriptionList {
        count,
        value,
        next_link,
    }))
}

// ---------------------------------------------------------------------------------
// 15-14 (EXPL-01 / EXPL-05) — search_resources (tenant-wide name/type substring search)
// ---------------------------------------------------------------------------------

/// Parsed + validated search filters. `q` is the non-empty search term (the handler
/// rejects an absent/all-whitespace term with a fixed 400 BEFORE constructing this — a
/// fail-closed choice so an empty term can never `ILIKE '%%'` the whole table, T-15-24);
/// `subscription` is already `Uuid`-parsed (D-09).
struct SearchFilters {
    q: String,
    subscription: Option<Uuid>,
}

/// Build the `WHERE`-conjunct fragment + parallel bound-value list for the resource search
/// (T-15-23). The SQL string carries ONLY column names, the `%` wildcard LITERALS, boolean
/// keywords, `ESCAPE '\'` clauses, and `$N` placeholders — every user value is returned as a
/// [`Bind`]. The search term binds as ONE [`Bind::Text`] referenced THREE times — `(name ILIKE
/// '%' || $N || '%' ESCAPE '\' OR type ILIKE '%' || $N || '%' ESCAPE '\' OR subscription_id IN
/// (SELECT subscription_id FROM synthetic.subscriptions WHERE display_name ILIKE '%' || $N || '%'
/// ESCAPE '\'))` — with the `%` wildcards WRITTEN INTO the constant SQL, never taken from `q`, so
/// no metacharacter splices in. The bound term is pre-escaped by [`normalize_search_term`] (`\`,
/// `%`, `_` → `\\`, `\%`, `\_`) and each clause declares `ESCAPE '\'`, so a `%`/`_` inside `q`
/// matches LITERALLY instead of acting as a wildcard (WR-02 — `q=%` no longer scans the whole
/// tenant). The subquery matches a resource whose SUBSCRIPTION name matches the term
/// (EXPL-GAP-01a). `subscription` binds as `subscription_id = $M`. `start_idx` is the last
/// placeholder already consumed: the page query passes `2` (`$1` cursor, `$2` LIMIT) so filters
/// begin at `$3`; the cursor-less COUNT passes `0` so the SAME fragment begins at `$1`. The
/// single-bind-twice + dual-base structure mirrors [`dependency_where`].
fn search_where(f: &SearchFilters, start_idx: i32) -> (String, Vec<Bind>) {
    let mut conj: Vec<String> = Vec::new();
    let mut binds: Vec<Bind> = Vec::new();
    let mut idx = start_idx;
    if !f.q.is_empty() {
        idx += 1;
        let p = idx;
        // The `%` wildcards are CONSTANT SQL text; `$p` (the term) is referenced THREE times —
        // resource name, resource type, AND the subscription-NAME subquery (EXPL-GAP-01a). The
        // subquery keys `synthetic.resources.subscription_id` on the FK-matching
        // `synthetic.subscriptions.subscription_id` whose `display_name` ILIKE-matches the SAME
        // bound term, so a term matching a subscription's name returns that subscription's
        // resources. Still ONE `Bind::Text`, never spliced. Each clause declares `ESCAPE '\'` so
        // the backslash-escaped `%`/`_` in the (pre-normalized) term match literally (WR-02).
        conj.push(format!(
            "(name ILIKE '%' || ${p} || '%' ESCAPE '\\' \
             OR type ILIKE '%' || ${p} || '%' ESCAPE '\\' \
             OR subscription_id IN (SELECT subscription_id FROM synthetic.subscriptions \
             WHERE display_name ILIKE '%' || ${p} || '%' ESCAPE '\\'))"
        ));
        binds.push(Bind::Text(f.q.clone())); // ONE bind, three `$p` uses
    }
    if let Some(sub) = f.subscription {
        idx += 1;
        conj.push(format!("subscription_id = ${idx}"));
        binds.push(Bind::Uuid(sub));
    }
    let where_extra = if conj.is_empty() {
        String::new()
    } else {
        format!(" AND {}", conj.join(" AND "))
    };
    (where_extra, binds)
}

/// Normalize a raw search term for `search_resources`: a literal `*` is a plain-substring marker
/// (NOT an ILIKE wildcard — ILIKE's wildcards are `%`/`_`) and is stripped, so `corp*` searches
/// for the substring `corp`. The ILIKE metacharacters `%`/`_` (and the escape `\` itself) are then
/// BACKSLASH-ESCAPED so they match LITERALLY, not as wildcards (WR-02). Every ILIKE clause that
/// consumes this term pairs it with `ESCAPE '\'`, so `q=%` matches only names containing a literal
/// `%` — it does NOT collapse to `ILIKE '%%%'` (a whole-tenant scan that would bypass the
/// empty-term guard's T-15-24 intent). Backslash is escaped FIRST so the `\` prefixes added for
/// `%`/`_` are not themselves re-escaped.
fn normalize_search_term(raw: &str) -> String {
    // Strip every `*` (plain-substring marker), then escape the ILIKE metacharacters so `%`/`_`
    // are literals under `... ILIKE '%' || $N || '%' ESCAPE '\'`. Order matters: `\` first.
    raw.replace('*', "")
        .replace('\\', "\\\\")
        .replace('%', "\\%")
        .replace('_', "\\_")
}

/// One resource-search hit — the SAME id/name/type/subscription/RG the tree already exposes
/// (T-15-25: only synthetic data crosses the boundary). Explicit camelCase per D-10.
/// `subscription_id` is stored as a `Uuid` column, surfaced as its string form.
#[derive(Serialize)]
pub struct ResourceSearchDto {
    id: String,
    name: String,
    #[serde(rename = "type")]
    resource_type: String,
    #[serde(rename = "subscriptionId")]
    subscription_id: String,
    #[serde(rename = "resourceGroupName")]
    resource_group_name: String,
}

/// One matching-subscription hit for the search response `subscriptions[]` section
/// (EXPL-GAP-01b). `id` is the subscription UUID string, `name` its `display_name`. Only
/// synthetic ids/names cross the boundary (T-15-25); already camelCase.
#[derive(Serialize)]
pub struct SearchSubscriptionDto {
    id: String,
    name: String,
}

/// Upper bound on the `subscriptions` array returned by `search_resources` (the locked
/// "bounded" requirement, T-15G-02) — used verbatim as the `LIMIT` on the sub-name query.
const SEARCH_SUBSCRIPTIONS_CAP: i64 = 50;

/// One matching-resource-group hit for the search response `resourceGroups[]` section
/// (RG-name search, live UAT). A resource group has no standalone UUID — it is addressed by
/// its `subscriptionId` + `name`, which together are the Miller-column selection key
/// (`?sub&rg`). Only synthetic ids/names cross the boundary (T-15-25); already camelCase.
#[derive(Serialize)]
pub struct SearchResourceGroupDto {
    name: String,
    #[serde(rename = "subscriptionId")]
    subscription_id: String,
}

/// Upper bound on the `resourceGroups` array returned by `search_resources` (mirrors
/// [`SEARCH_SUBSCRIPTIONS_CAP`]: bounded, name-ASC) — the `LIMIT` on the RG-name query.
const SEARCH_RESOURCE_GROUPS_CAP: i64 = 50;

/// Execution budget: max characters accepted for the search term `q`. The search is a
/// tenant-wide, non-sargable `ILIKE '%term%'` (leading `%` ⇒ sequential scan), so a
/// pathologically long term inflates every row comparison; this caps the input at the
/// source, complementing the server-wide `statement_timeout`. 200 chars is far beyond any
/// real search. A fixed const (mirrors the other hardcoded search caps above).
const MAX_SEARCH_TERM_CHARS: usize = 200;

/// `GET /_sim/resources/search` envelope — a keyset-paginated page of search hits. `count` is
/// the FULL filtered total (D-13), NOT the page size. `subscriptions` is a BOUNDED, name-ASC
/// list of subscriptions whose name matches the term (EXPL-GAP-01b; `[]` when none match).
/// `resourceGroups` is the analogous BOUNDED, name-ASC list of resource groups whose name
/// matches the term (RG-name search; `[]` when none match) — resources are named unlike their
/// RGs, so an RG-name query (e.g. `rg-corp-...`) matches zero resource rows and surfaces here.
/// `nextLink` carries the opaque `$skiptoken` continuation (plus `q` + optional `subscription`
/// + api-version) when more rows exist.
#[derive(Serialize)]
pub struct ResourceSearchList {
    count: i64,
    value: Vec<ResourceSearchDto>,
    subscriptions: Vec<SearchSubscriptionDto>,
    #[serde(rename = "resourceGroups")]
    resource_groups: Vec<SearchResourceGroupDto>,
    #[serde(rename = "nextLink", skip_serializing_if = "Option::is_none")]
    next_link: Option<String>,
}

/// `GET /_sim/resources/search` — tenant-wide resource search by name OR type substring,
/// keyset-paginated on the TEXT `id` PK (EXPL-01/EXPL-05, WAPI-04 additive fifth route).
///
/// Fail-closed (D-16): `SimQuery::parse` allows only the pagination trio + `q`/`subscription`;
/// an unknown key / bad `$top` / malformed `%XX` / ASCII control char is the fixed JSON 400.
/// `q` is REQUIRED — an absent or all-whitespace term is a fixed 400 (T-15-24, so no `%%`
/// whole-table scan). `subscription` is parsed to `Uuid` BEFORE SQL (a malformed value → fixed
/// 400, never a 500). Soft-deleted rows are excluded via the parameter-free `drift_deleted_at
/// IS NULL` predicate. The whole search is a SINGLE-statement read — no long-running txn on
/// `synthetic.resources` (respects the ALTER ACCESS-EXCLUSIVE startup-lock fragility).
pub async fn search_resources(
    State(state): State<AppState>,
    RawQuery(raw): RawQuery,
) -> Result<Json<ResourceSearchList>, ApiError> {
    // Fail-closed parse (D-16): documented set = the pagination trio + `q`/`subscription`.
    let qs = SimQuery::parse(raw, &["q", "subscription"])?;

    // Execution budget: cap the RAW term length BEFORE normalizing (escaping inflates
    // length, so bound what the caller actually sent). A tenant-wide `ILIKE '%term%'` is a
    // sequential scan, so an over-long term is rejected up front with a fixed 400.
    let raw_term = qs.filter("q").unwrap_or_default();
    if raw_term.chars().count() > MAX_SEARCH_TERM_CHARS {
        return Err(ApiError::bad_request("search term too long"));
    }

    // Normalize the literal `*` (plain-substring marker) BEFORE the empty-term guard so an
    // all-`*` term collapses to empty and fails-closed to the same fixed 400 (T-15G-03/04).
    // Require `q`: absent or all-whitespace → fixed 400 (T-15-24, no `%%` whole-table scan).
    let q = normalize_search_term(raw_term);
    if q.trim().is_empty() {
        return Err(ApiError::bad_request("missing search term"));
    }

    // Parse subscription to Uuid BEFORE SQL (D-09); a malformed value is a fixed 400.
    let sub = match qs.filter("subscription") {
        Some(s) => {
            Some(Uuid::parse_str(s).map_err(|_| ApiError::bad_request("invalid subscription"))?)
        }
        None => None,
    };
    let filters = SearchFilters {
        q: q.clone(),
        subscription: sub,
    };

    // Filtered COUNT(*): the SAME predicate rebuilt to start at `$1` (no cursor/limit) so `count`
    // is the whole filtered set, not the page (D-13). `drift_deleted_at IS NULL` is a fixed,
    // parameter-free predicate; the `AND`-prefixed fragment appends after it.
    let (count_where, count_binds) = search_where(&filters, 0);
    let count_sql = format!(
        "SELECT count(*) AS n FROM synthetic.resources \
         WHERE drift_deleted_at IS NULL{count_where}"
    );
    let mut cq = sqlx::query(&count_sql);
    for b in &count_binds {
        cq = match b {
            Bind::Uuid(u) => cq.bind(*u),
            Bind::Text(t) => cq.bind(t.clone()),
        };
    }
    let count: i64 = cq.fetch_one(&state.pool).await?.try_get("n")?;

    // Page query: keyset on the TEXT `id` PK (like `resources.rs`). `$1::text` = decoded cursor
    // (NULL page 1), `$2` = LIMIT top+1, then the search binds in push order (filters start `$3`).
    let top = clamp_top(qs.top);
    let cursor = qs.skiptoken.as_deref().map(decode_token).transpose()?;
    let (where_extra, binds) = search_where(&filters, 2);
    let sql = format!(
        "SELECT id, name, type, subscription_id, resource_group_name \
         FROM synthetic.resources \
         WHERE ($1::text IS NULL OR id > $1) AND drift_deleted_at IS NULL{where_extra} \
         ORDER BY id LIMIT $2"
    );
    let mut query = sqlx::query(&sql).bind(cursor).bind(top + 1);
    for b in &binds {
        query = match b {
            Bind::Uuid(u) => query.bind(*u),
            Bind::Text(t) => query.bind(t.clone()),
        };
    }
    let rows = query.fetch_all(&state.pool).await?;
    let mut dtos = Vec::with_capacity(rows.len());
    for row in &rows {
        let sid: Uuid = row.try_get("subscription_id")?;
        dtos.push(ResourceSearchDto {
            id: row.try_get("id")?,
            name: row.try_get("name")?,
            resource_type: row.try_get("type")?,
            subscription_id: sid.to_string(),
            resource_group_name: row.try_get("resource_group_name")?,
        });
    }
    // Bounded `subscriptions[]` (EXPL-GAP-01b): the subscriptions whose name matches the SAME
    // normalized term, `$1`-bound (never spliced), name-ASC, capped at `SEARCH_SUBSCRIPTIONS_CAP`
    // (T-15G-02). Runs on EVERY search — the array is empty when no subscription name matches.
    // `ESCAPE '\'` mirrors `search_where`: the pre-escaped `%`/`_` in the term match literally
    // (WR-02), so `q=%` does not name-match every subscription.
    let subs_sql = "SELECT subscription_id, display_name FROM synthetic.subscriptions \
         WHERE display_name ILIKE '%' || $1 || '%' ESCAPE '\\' \
         ORDER BY display_name ASC LIMIT $2";
    let subs_rows = sqlx::query(subs_sql)
        .bind(&q)
        .bind(SEARCH_SUBSCRIPTIONS_CAP)
        .fetch_all(&state.pool)
        .await?;
    let mut subscriptions = Vec::with_capacity(subs_rows.len());
    for row in &subs_rows {
        let sid: Uuid = row.try_get("subscription_id")?;
        subscriptions.push(SearchSubscriptionDto {
            id: sid.to_string(),
            name: row.try_get("display_name")?,
        });
    }

    // Bounded `resourceGroups[]` (RG-name search): the resource groups whose NAME matches the
    // SAME normalized term, `$1`-bound (never spliced), name-ASC, capped at
    // `SEARCH_RESOURCE_GROUPS_CAP`. Runs on EVERY search — the array is empty when no RG name
    // matches. `ESCAPE '\'` mirrors `search_where`: the pre-escaped `%`/`_` in the term match
    // literally (WR-02), so `q=%` does not name-match every RG. Ordered by (name, subscription_id)
    // so a name shared across subscriptions yields a stable, individually-addressable set.
    let rgs_sql = "SELECT subscription_id, name FROM synthetic.resource_groups \
         WHERE name ILIKE '%' || $1 || '%' ESCAPE '\\' \
         ORDER BY name ASC, subscription_id ASC LIMIT $2";
    let rgs_rows = sqlx::query(rgs_sql)
        .bind(&q)
        .bind(SEARCH_RESOURCE_GROUPS_CAP)
        .fetch_all(&state.pool)
        .await?;
    let mut resource_groups = Vec::with_capacity(rgs_rows.len());
    for row in &rgs_rows {
        let sid: Uuid = row.try_get("subscription_id")?;
        resource_groups.push(SearchResourceGroupDto {
            name: row.try_get("name")?,
            subscription_id: sid.to_string(),
        });
    }

    // `split_page` performs the LIMIT top+1 surplus split and emits the ENCODED cursor of the
    // last kept row (the TEXT `id` keyset) directly.
    let (value, next_token) = split_page(dtos, top, |d| d.id.as_str());
    let next_link = next_token.map(|tok| {
        let mut active: Vec<(&str, String)> = vec![("q", q.clone())];
        if let Some(s) = &sub {
            active.push(("subscription", s.to_string()));
        }
        let active_ref: Vec<(&str, &str)> = active.iter().map(|(k, v)| (*k, v.as_str())).collect();
        // `tok` is already the encoded `$skiptoken` (split_page encodes it).
        sim_next_link(
            &state.base_url,
            "/_sim/resources/search",
            top,
            &tok,
            qs.api_version.as_deref(),
            &active_ref,
        )
    });
    Ok(Json(ResourceSearchList {
        count,
        value,
        subscriptions,
        resource_groups,
        next_link,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// RG-name search: the resource-group hit DTO serializes to EXACTLY `{name, subscriptionId}`
    /// (camelCase `subscriptionId`, no snake_case leak) — the Miller-column selection key.
    #[test]
    fn search_resource_group_dto_serializes() {
        let dto = SearchResourceGroupDto {
            name: "rg-corp-dev-backup-43".to_string(),
            subscription_id: "11111111-1111-1111-1111-111111111111".to_string(),
        };
        let v = serde_json::to_value(&dto).expect("serialize");
        let mut keys: Vec<&str> = v
            .as_object()
            .expect("object")
            .keys()
            .map(|k| k.as_str())
            .collect();
        keys.sort();
        assert_eq!(keys, vec!["name", "subscriptionId"]);
        assert_eq!(v["name"], "rg-corp-dev-backup-43");
        assert_eq!(v["subscriptionId"], "11111111-1111-1111-1111-111111111111");
    }

    /// D-10: the violation DTO serializes to the exact camelCase key set, and the `detail`
    /// JSONB passthrough keeps its inner keys VERBATIM (never camelCase-mangled).
    #[test]
    fn violation_dto_serializes() {
        let dto = ViolationDto {
            resource_id: "/subscriptions/s/x".to_string(),
            code: "STORAGE_NO_ENCRYPTION".to_string(),
            severity: "High".to_string(),
            subscription_id: Some("11111111-1111-1111-1111-111111111111".to_string()),
            detail: json!({ "field": "publicNetworkAccess", "observed": "Enabled" }),
        };
        let v = serde_json::to_value(&dto).expect("serialize");
        let obj = v.as_object().expect("object");

        // Exact camelCase key set — no snake_case leaks, no extra/missing fields.
        let mut keys: Vec<&str> = obj.keys().map(|k| k.as_str()).collect();
        keys.sort();
        assert_eq!(
            keys,
            vec!["code", "detail", "resourceId", "severity", "subscriptionId"]
        );
        assert_eq!(v["resourceId"], "/subscriptions/s/x");
        assert_eq!(v["code"], "STORAGE_NO_ENCRYPTION");
        assert_eq!(v["severity"], "High");
        assert_eq!(v["subscriptionId"], "11111111-1111-1111-1111-111111111111");

        // The JSONB detail's INNER keys survive byte-for-byte (D-10) — NOT renamed to
        // e.g. `Field`/`Observed` or camelCased in any way.
        assert_eq!(v["detail"]["field"], "publicNetworkAccess");
        assert_eq!(v["detail"]["observed"], "Enabled");
        assert!(
            v["detail"].get("Field").is_none(),
            "detail inner keys must NOT be mangled"
        );
    }

    /// T-14-01: an SQL-metacharacter filter value NEVER reaches the SQL fragment — the
    /// fragment carries only columns + `$N`, and `'$'.count() == binds.len()`.
    #[test]
    fn violation_filter_is_placeholders_only() {
        let attack = "'; DROP TABLE synthetic.violations;--";
        let f = ViolationFilters {
            subscription: Some(Uuid::nil()),
            code: Some(attack.to_string()),
            resource: Some(attack.to_string()),
            severity: Some(attack.to_string()),
        };
        // Page context (filters start at `$3`).
        let (where_extra, binds) = violation_where(&f, 2);

        assert!(
            !where_extra.contains("DROP"),
            "no spliced keyword: {where_extra:?}"
        );
        assert!(
            !where_extra.contains(';'),
            "no statement separator: {where_extra:?}"
        );
        assert!(
            !where_extra.contains(attack),
            "raw attack literal must not reach SQL: {where_extra:?}"
        );
        // Every `$N` has exactly one bound value (subscription binds once, used once).
        assert_eq!(where_extra.matches('$').count(), binds.len());
        assert_eq!(binds.len(), 4, "subscription + code + resource + severity");
        assert!(
            where_extra.contains("r.subscription_id = $3"),
            "page filters start at $3: {where_extra:?}"
        );

        // COUNT context (D-13): the SAME allowlist rebuilt to start at `$1`. Still `$N`-bound,
        // same bind count, first placeholder `$1` (no cursor/limit precede it), no splice.
        let (count_where, count_binds) = violation_where(&f, 0);
        assert!(
            !count_where.contains("DROP") && !count_where.contains(';'),
            "count fragment: no spliced keyword/separator: {count_where:?}"
        );
        assert!(
            !count_where.contains(attack),
            "count fragment: raw attack literal must not reach SQL: {count_where:?}"
        );
        assert_eq!(
            count_where.matches('$').count(),
            count_binds.len(),
            "count fragment is fully $N-bound"
        );
        assert_eq!(count_binds.len(), 4);
        assert!(
            count_where.contains("r.subscription_id = $1"),
            "count filters start at $1: {count_where:?}"
        );

        // No filters → no fragment, no binds (both bases).
        let (empty, eb) = violation_where(
            &ViolationFilters {
                subscription: None,
                code: None,
                resource: None,
                severity: None,
            },
            2,
        );
        assert!(empty.is_empty());
        assert!(eb.is_empty());
    }

    /// D-13: the dependency DTO serializes to EXACTLY the nested spec shape
    /// `{ type, source:{resourceId,subscriptionId}, target:{...}, crossSubscription }` —
    /// key `type` present, the old flat keys ABSENT; nested objects carry `resourceId` +
    /// `subscriptionId`. `crossSubscription` = (source != target) computed in Rust (D-08).
    #[test]
    fn dependency_dto_serializes() {
        let cross = DependencyDto {
            dep_type: "private-endpoint".to_string(),
            source: DependencyEndpoint {
                resource_id: "/subscriptions/a/x".to_string(),
                subscription_id: "11111111-1111-1111-1111-111111111111".to_string(),
            },
            target: DependencyEndpoint {
                resource_id: "/subscriptions/b/y".to_string(),
                subscription_id: "22222222-2222-2222-2222-222222222222".to_string(),
            },
            cross_subscription: true,
        };
        let v = serde_json::to_value(&cross).expect("serialize");
        let obj = v.as_object().expect("object");

        // EXACT top-level key set: the nested spec shape, nothing else.
        let mut keys: Vec<&str> = obj.keys().map(|k| k.as_str()).collect();
        keys.sort();
        assert_eq!(keys, vec!["crossSubscription", "source", "target", "type"]);

        // The old FLAT keys must be gone.
        for gone in [
            "dependencyType",
            "sourceResourceId",
            "targetResourceId",
            "sourceSubscriptionId",
            "targetSubscriptionId",
        ] {
            assert!(obj.get(gone).is_none(), "flat key `{gone}` must be absent");
        }

        // Field is `type`, and the nested endpoints carry resourceId + subscriptionId.
        assert_eq!(v["type"], "private-endpoint");
        let src = v["source"].as_object().expect("source object");
        let mut src_keys: Vec<&str> = src.keys().map(|k| k.as_str()).collect();
        src_keys.sort();
        assert_eq!(src_keys, vec!["resourceId", "subscriptionId"]);
        assert_eq!(v["source"]["resourceId"], "/subscriptions/a/x");
        assert_eq!(
            v["source"]["subscriptionId"],
            "11111111-1111-1111-1111-111111111111"
        );
        assert_eq!(v["target"]["resourceId"], "/subscriptions/b/y");
        assert_eq!(
            v["target"]["subscriptionId"],
            "22222222-2222-2222-2222-222222222222"
        );
        assert_eq!(v["crossSubscription"], serde_json::Value::Bool(true));

        // An intra-sub edge derives crossSubscription = false.
        let intra = DependencyDto {
            dep_type: "shared-keyvault".to_string(),
            source: DependencyEndpoint {
                resource_id: "/subscriptions/a/x".to_string(),
                subscription_id: "11111111-1111-1111-1111-111111111111".to_string(),
            },
            target: DependencyEndpoint {
                resource_id: "/subscriptions/a/z".to_string(),
                subscription_id: "11111111-1111-1111-1111-111111111111".to_string(),
            },
            cross_subscription: false,
        };
        let iv = serde_json::to_value(&intra).expect("serialize intra");
        assert_eq!(iv["crossSubscription"], serde_json::Value::Bool(false));
    }

    /// T-14-01: the dependency `?subscription=` filter reaches SQL as a SINGLE bound value
    /// used TWICE (`(source_subscription = $N OR target_subscription = $N)`); `?type` is
    /// one more `$N`. No user value ever splices in.
    #[test]
    fn dependency_filter_is_placeholders_only() {
        let attack = "'; DROP TABLE synthetic.dependencies;--";
        let f = DependencyFilters {
            subscription: Some(Uuid::nil()),
            dep_type: Some(attack.to_string()),
        };
        // Page context (filters start at `$3`).
        let (where_extra, binds) = dependency_where(&f, 2);

        assert!(
            !where_extra.contains("DROP"),
            "no spliced keyword: {where_extra:?}"
        );
        assert!(
            !where_extra.contains(';'),
            "no statement separator: {where_extra:?}"
        );
        assert!(
            !where_extra.contains(attack),
            "raw literal must not reach SQL"
        );
        assert!(
            where_extra.contains("(source_subscription = $3 OR target_subscription = $3)"),
            "source-OR-target uses ONE placeholder twice: {where_extra:?}"
        );
        assert!(
            where_extra.contains("lower(dependency_type) = lower($4)"),
            "type is a bound placeholder: {where_extra:?}"
        );
        assert_eq!(
            binds.len(),
            2,
            "one subscription bind (used twice) + one type bind"
        );

        // COUNT context (D-13): the SAME source-OR-target allowlist rebuilt to start at `$1`.
        // The single subscription bind is still referenced twice; type is one more `$N`.
        let (count_where, count_binds) = dependency_where(&f, 0);
        assert!(
            !count_where.contains("DROP") && !count_where.contains(';'),
            "count fragment: no spliced keyword/separator: {count_where:?}"
        );
        assert!(
            !count_where.contains(attack),
            "count fragment: raw literal must not reach SQL: {count_where:?}"
        );
        assert!(
            count_where.contains("(source_subscription = $1 OR target_subscription = $1)"),
            "count source-OR-target uses ONE placeholder ($1) twice: {count_where:?}"
        );
        assert!(
            count_where.contains("lower(dependency_type) = lower($2)"),
            "count type placeholder is $2: {count_where:?}"
        );
        assert_eq!(count_binds.len(), 2, "count: subscription (twice) + type");

        // subscription only: a SINGLE bound value, but the `$3` appears twice.
        let (sub_only, sb) = dependency_where(
            &DependencyFilters {
                subscription: Some(Uuid::nil()),
                dep_type: None,
            },
            2,
        );
        assert_eq!(sb.len(), 1, "subscription is a single bound value");
        assert_eq!(
            sub_only.matches('$').count(),
            2,
            "the single bind is referenced twice (source OR target)"
        );

        // No filters → no fragment, no binds.
        let (empty, eb) = dependency_where(
            &DependencyFilters {
                subscription: None,
                dep_type: None,
            },
            2,
        );
        assert!(empty.is_empty());
        assert!(eb.is_empty());
    }

    /// WR-01: a decoded ASCII control character (NUL and the rest of C0 + DEL) is a
    /// reachable contract violation — encoded as `%00`/`%1f`/`%7f` it decodes to valid
    /// UTF-8, so without an explicit guard it binds and Postgres 500s, contradicting the
    /// D-16 "every bad-input path is a fixed JSON 400" invariant. The decode choke point
    /// must REJECT it so no control byte ever reaches SQL, for keys AND values of every
    /// `/_sim` query component (filters, `$skiptoken`, `api-version`, ...).
    #[test]
    fn percent_decode_query_rejects_ascii_control_chars() {
        // NUL and a representative spread of C0 controls + DEL, percent-encoded.
        for enc in ["%00", "%01", "%09", "%0a", "%0d", "%1f", "%7f"] {
            assert!(
                percent_decode_query(enc).is_err(),
                "encoded control {enc:?} must be a fixed 400, not decoded through to SQL"
            );
            // Also rejected when embedded in an otherwise-benign value.
            let embedded = format!("prod{enc}code");
            assert!(
                percent_decode_query(&embedded).is_err(),
                "embedded control {embedded:?} must be rejected"
            );
        }

        // A raw (already-literal) control byte in the query is rejected the same way.
        assert!(percent_decode_query("a\u{0}b").is_err());
        assert!(percent_decode_query("a\tb").is_err());

        // Benign printable values (incl. legitimately-encoded `/`, space, `.`) still decode.
        assert_eq!(
            percent_decode_query("Microsoft.Storage%2Faccounts").expect("ok"),
            "Microsoft.Storage/accounts"
        );
        assert_eq!(percent_decode_query("prod+east").expect("ok"), "prod east");
        assert_eq!(
            percent_decode_query("STORAGE_NO_ENCRYPTION").expect("ok"),
            "STORAGE_NO_ENCRYPTION"
        );

        // A malformed escape remains rejected (unchanged behavior).
        assert!(percent_decode_query("%zz").is_err());
        assert!(percent_decode_query("%0").is_err());
    }

    /// T-15-23: the tenant-wide resource-search predicate reaches SQL as placeholders ONLY.
    /// The search term `q` binds as ONE `$N` referenced THREE times (name ILIKE OR type ILIKE OR
    /// the subscription-name subquery `subscription_id IN (SELECT … WHERE display_name ILIKE …)`)
    /// with the `%` wildcards written as CONSTANT SQL text (never taken from user input);
    /// `subscription` binds as one more `$N`. No raw attack literal, `DROP`, or `;` reaches the
    /// fragment. The dual-base structure matches `dependency_where`: the page query seeds at
    /// `$3` (after `$1` cursor + `$2` limit), the cursor-less COUNT rebuilds the SAME fragment
    /// starting at `$1`.
    #[test]
    fn search_where_is_placeholders_only() {
        let attack = "'; DROP TABLE synthetic.resources;--";
        let f = SearchFilters {
            q: attack.to_string(),
            subscription: Some(Uuid::nil()),
        };
        // Page context (filters start at `$3`).
        let (where_extra, binds) = search_where(&f, 2);

        assert!(
            !where_extra.contains("DROP"),
            "no spliced keyword: {where_extra:?}"
        );
        assert!(
            !where_extra.contains(';'),
            "no statement separator: {where_extra:?}"
        );
        assert!(
            !where_extra.contains(attack),
            "raw attack literal must not reach SQL: {where_extra:?}"
        );
        assert!(
            where_extra.contains("ILIKE"),
            "the search predicate uses ILIKE: {where_extra:?}"
        );
        // The `%` wildcards are CONSTANT SQL text ('%' literals), never taken from `q`.
        assert!(
            where_extra.contains("'%'"),
            "wildcards are constant SQL '%' literals: {where_extra:?}"
        );
        // WR-02: every ILIKE clause declares `ESCAPE '\'` so a `%`/`_` in the (pre-normalized) term
        // matches literally — one per name/type/sub-name predicate (three total).
        assert_eq!(
            where_extra.matches("ESCAPE '\\'").count(),
            3,
            "each ILIKE clause pairs with ESCAPE '\\' (WR-02): {where_extra:?}"
        );
        // The subscription-NAME subquery is present and `$N`-bound (EXPL-GAP-01a).
        assert!(
            where_extra.contains(
                "subscription_id IN (SELECT subscription_id FROM synthetic.subscriptions WHERE display_name ILIKE"
            ),
            "sub-name subquery references the same $N-bound term: {where_extra:?}"
        );
        // `q` binds as ONE placeholder `$3` referenced THREE times
        // (name ILIKE OR type ILIKE OR the sub-name subquery).
        assert_eq!(
            where_extra.matches("$3").count(),
            3,
            "q placeholder $3 used thrice (name ILIKE OR type ILIKE OR sub-name subquery): {where_extra:?}"
        );
        assert!(
            where_extra.contains("subscription_id = $4"),
            "subscription is a bound placeholder $4: {where_extra:?}"
        );
        assert_eq!(
            binds.len(),
            2,
            "one q bind (used thrice) + one subscription bind"
        );

        // COUNT context (D-13): the SAME fragment rebuilt to start at `$1`. `q` is still ONE
        // bound value referenced thrice (now `$1`); subscription is `$2`.
        let (count_where, count_binds) = search_where(&f, 0);
        assert!(
            !count_where.contains("DROP") && !count_where.contains(';'),
            "count fragment: no spliced keyword/separator: {count_where:?}"
        );
        assert!(
            !count_where.contains(attack),
            "count fragment: raw attack literal must not reach SQL: {count_where:?}"
        );
        assert_eq!(
            count_where.matches("$1").count(),
            3,
            "count q placeholder $1 used thrice: {count_where:?}"
        );
        assert!(
            count_where.contains("subscription_id = $2"),
            "count subscription placeholder is $2: {count_where:?}"
        );
        assert_eq!(count_binds.len(), 2);

        // No filters (empty q, no subscription) → empty fragment, no binds (both bases).
        let (empty, eb) = search_where(
            &SearchFilters {
                q: String::new(),
                subscription: None,
            },
            2,
        );
        assert!(empty.is_empty());
        assert!(eb.is_empty());
    }

    /// `normalize_search_term` strips every literal `*` (plain-substring marker) AND backslash-
    /// escapes the ILIKE metacharacters `%`/`_`/`\` so they match literally under `ESCAPE '\'`
    /// (WR-02 — a `%` term no longer becomes a whole-tenant wildcard).
    #[test]
    fn normalize_search_term_cases() {
        assert_eq!(normalize_search_term("corp*"), "corp");
        assert_eq!(normalize_search_term("*corp*"), "corp");
        assert_eq!(normalize_search_term("a*b"), "ab");
        assert_eq!(normalize_search_term("corp"), "corp");
        // `%`/`_` are ILIKE wildcards — now backslash-escaped so they bind as LITERALS (WR-02).
        assert_eq!(normalize_search_term("a%b_c"), "a\\%b\\_c");
        // A lone `%` becomes the literal-matching `\%`, never the bare wildcard that scans all rows.
        assert_eq!(normalize_search_term("%"), "\\%");
        // A backslash is escaped FIRST so the prefixes added for `%`/`_` are not re-escaped.
        assert_eq!(normalize_search_term("a\\b"), "a\\\\b");
        assert_eq!(normalize_search_term("100%_x"), "100\\%\\_x");
    }
}
