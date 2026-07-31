//! `POST /{scope}/providers/Microsoft.CostManagement/query` — the Cost Management
//! Query API mock core (COST-03/04/05).
//!
//! This is the SQL-safety + contract core of the cost domain. It builds its OWN
//! positional `{properties:{columns,rows,nextLink}}` [`QueryResult`] envelope — NOT
//! the ARM-list `{value,nextLink}` (RESEARCH Pitfall 2/6) — and dispatches the
//! `dataset.grouping[]` dimensions through a **closed match** ([`group_expr`], D-07):
//! every column name is a trusted closed-set literal and the only user data that
//! reaches SQL is a `$N` bind (the Tag key via `r.tags ->> $N`, and the timeframe
//! date bounds via `$N::date`). The injection-safety invariant is pinned DB-free by
//! [`tests::group_dispatch_is_placeholders_only`] (mirrors `resources.rs`'s
//! `filter_conjunct_is_placeholders_only_even_with_sql_metachars`).
//!
//! The ServiceName/MeterCategory dimension groups by `r.type` in SQL and folds
//! `type → ServiceName` in Rust via a static [`SERVICE_MAP`] constant (D-10) — zero
//! scan provenance (the seed carries no meter strings; COST-05). `type`
//! (Usage/ActualCost/AmortizedCost) is accepted and produces the same SUM in v2.0
//! (amortization math deferred — COST-04). Currency is fixed `USD` (D-11).
//!
//! Full DB-backed integration (reconciliation, both scopes, any-Bearer, amortized
//! equality) lands in Plan 09-05; the `#[cfg(test)]` units here are DB-free.

use crate::{error::ApiError, state::AppState};
use axum::{
    Json,
    extract::{Path, State},
    http::header,
    response::{IntoResponse, Response},
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sqlx::Row;
use std::collections::{BTreeMap, HashMap};
use tokio_stream::StreamExt;
use uuid::Uuid;

// ---------------------------------------------------------------------------------
// Request DTOs (serde) — the Cost Management Query body (api-version 2025-03-01).
// ---------------------------------------------------------------------------------

/// The Query request body. `type` is accept-and-echo (COST-04); `timePeriod` is read
/// only when `timeframe == "Custom"`.
#[derive(Deserialize)]
pub struct QueryRequest {
    /// `Usage` | `ActualCost` | `AmortizedCost` — accepted; identical SUM in v2.0.
    #[serde(default)]
    r#type: Option<String>,
    timeframe: String,
    #[serde(rename = "timePeriod", default)]
    time_period: Option<TimePeriod>,
    #[serde(default)]
    dataset: Dataset,
}

/// `timePeriod.from`/`.to` — RFC3339 timestamps (only the date prefix is used).
#[derive(Deserialize)]
struct TimePeriod {
    from: String,
    to: String,
}

/// `dataset`: granularity + aggregation + grouping. All fields optional/defaulted so a
/// minimal body (just `timeframe`) deserializes.
#[derive(Deserialize, Default)]
struct Dataset {
    /// `Daily` | `None` — there is NO `Monthly` (RESEARCH Pitfall 5). `Daily` against
    /// our monthly store downgrades to the monthly aggregate (Open Question 1).
    #[serde(default)]
    granularity: Option<String>,
    /// `{ "<key>": { "name": "PreTaxCost", "function": "Sum" } }` — the column name is
    /// echoed; the function is always `Sum` for v2.0.
    #[serde(default)]
    aggregation: Option<BTreeMap<String, Value>>,
    #[serde(default)]
    grouping: Vec<Grouping>,
}

/// A `grouping[]` entry: `{ "type": "Dimension"|"Tag", "name": "..." }`.
#[derive(Deserialize, Clone)]
struct Grouping {
    #[serde(rename = "type")]
    kind: String,
    name: String,
}

// ---------------------------------------------------------------------------------
// Response DTO — the positional {properties:{columns,rows,nextLink}} envelope.
// This is the deliberate non-reuse of arm::ListResponse (RESEARCH Pitfall 2/6).
// ---------------------------------------------------------------------------------

/// A response column descriptor. `type` ∈ `{"Number","String"}`.
#[derive(Serialize)]
pub struct QueryColumn {
    name: String,
    r#type: String,
}

/// The positional result body: `columns` describe each cell position, `rows` are
/// positional arrays matching `columns`. `nextLink` is omitted when absent (v2.0 = null).
#[derive(Serialize)]
pub struct QueryProperties {
    columns: Vec<QueryColumn>,
    rows: Vec<Vec<Value>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    next_link: Option<String>,
}

/// The Cost Management Query result. `type` is the const `"microsoft.costmanagement/Query"`.
#[derive(Serialize)]
pub struct QueryResult {
    id: String,
    name: String,
    r#type: String,
    properties: QueryProperties,
}

// ---------------------------------------------------------------------------------
// Static type → ServiceName map (D-10) — Rust constants, ZERO scan provenance.
// ---------------------------------------------------------------------------------

/// Curated, deterministic `resource type → ServiceName` map (D-10/COST-05). These are
/// Rust constants — the seed carries no meter/service strings, so this static table is
/// the ONLY source for the ServiceName/MeterCategory dimension. Keys are lowercased
/// canonical types; lookup is case-insensitive. Unmapped types fall back to the
/// provider namespace (everything before the first `/`).
const SERVICE_MAP: &[(&str, &str)] = &[
    ("microsoft.compute/virtualmachines", "Virtual Machines"),
    ("microsoft.compute/disks", "Storage"),
    ("microsoft.compute/snapshots", "Storage"),
    ("microsoft.storage/storageaccounts", "Storage"),
    ("microsoft.sql/servers", "SQL Database"),
    ("microsoft.sql/servers/databases", "SQL Database"),
    ("microsoft.web/serverfarms", "App Service"),
    ("microsoft.web/sites", "App Service"),
    ("microsoft.network/publicipaddresses", "Virtual Network"),
    ("microsoft.network/virtualnetworks", "Virtual Network"),
    ("microsoft.documentdb/databaseaccounts", "Azure Cosmos DB"),
    ("microsoft.operationalinsights/workspaces", "Log Analytics"),
    ("microsoft.keyvault/vaults", "Key Vault"),
    (
        "microsoft.containerservice/managedclusters",
        "Azure Kubernetes Service",
    ),
];

/// Resolve a resource `type` to its ServiceName via the static [`SERVICE_MAP`] (D-10).
/// Case-insensitive; an unmapped type falls back to the provider namespace (the token
/// before the first `/`). Returns only constant strings or a slice of the input — never
/// a scan-derived label.
fn service_name(type_str: &str) -> &str {
    for &(key, service) in SERVICE_MAP {
        if type_str.eq_ignore_ascii_case(key) {
            return service;
        }
    }
    // Fallback: the provider namespace (e.g. `Microsoft.FooBar/widgets` → `Microsoft.FooBar`).
    type_str.split('/').next().unwrap_or(type_str)
}

// ---------------------------------------------------------------------------------
// Closed-match grouping dispatch (D-07) — the one place SQL is built at runtime.
// ---------------------------------------------------------------------------------

/// Map a `grouping[]` entry to its GROUP BY expression (D-07). Dimension column names
/// are TRUSTED closed-match literals; the only user data reaching SQL is the Tag KEY,
/// bound as `$N` via `r.tags ->> $N` (`bind_ix` is advanced and the key pushed to
/// `args`). An unknown dimension is a 400 (T-9-IV — never a permissive default).
///
/// ServiceName/MeterCategory both group by `r.type` here; the `type → ServiceName`
/// fold happens in Rust after the query (D-10), keeping SQL trivial and the map a pure
/// constant.
fn group_expr(g: &Grouping, bind_ix: &mut i32, args: &mut Vec<String>) -> Result<String, ApiError> {
    match (g.kind.as_str(), g.name.as_str()) {
        ("Dimension", "ResourceType") => Ok("r.type".to_string()),
        ("Dimension", "ResourceGroup") => Ok("r.resource_group_name".to_string()),
        ("Dimension", "ResourceId") => Ok("c.resource_id".to_string()),
        ("Dimension", "SubscriptionId") => Ok("c.subscription_id".to_string()),
        ("Dimension", "ServiceName") | ("Dimension", "MeterCategory") => Ok("r.type".to_string()),
        ("Tag", _) => {
            *bind_ix += 1;
            let p = *bind_ix;
            args.push(g.name.clone()); // tag KEY bound, never spliced
            Ok(format!("r.tags ->> ${p}"))
        }
        _ => Err(ApiError::bad_request("unsupported grouping dimension")),
    }
}

/// True when a grouping folds `r.type` to a ServiceName in Rust (D-10).
fn is_service_dimension(g: &Grouping) -> bool {
    g.kind == "Dimension" && (g.name == "ServiceName" || g.name == "MeterCategory")
}

// ---------------------------------------------------------------------------------
// Timeframe → date range (COST-03) — pure, no chrono (the phase adds zero crates).
// ---------------------------------------------------------------------------------

/// Minimal civil date (year, month, day) — pure std arithmetic, no chrono dependency
/// (the threat model mandates zero new crates). Supports the month-boundary math the
/// Cost Management `timeframe` enums need.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
struct CivilDate {
    y: i32,
    m: u32,
    d: u32,
}

impl CivilDate {
    /// ISO `YYYY-MM-DD`.
    fn iso(&self) -> String {
        format!("{:04}-{:02}-{:02}", self.y, self.m, self.d)
    }

    fn is_leap(y: i32) -> bool {
        (y % 4 == 0 && y % 100 != 0) || y % 400 == 0
    }

    /// Days in (year, month), leap-aware.
    fn days_in_month(y: i32, m: u32) -> u32 {
        match m {
            1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
            4 | 6 | 9 | 11 => 30,
            2 if Self::is_leap(y) => 29,
            2 => 28,
            _ => 30,
        }
    }

    /// First day of this date's month.
    fn first_of_month(&self) -> CivilDate {
        CivilDate {
            y: self.y,
            m: self.m,
            d: 1,
        }
    }

    /// First day of the previous month.
    fn prev_month_first(&self) -> CivilDate {
        if self.m == 1 {
            CivilDate {
                y: self.y - 1,
                m: 12,
                d: 1,
            }
        } else {
            CivilDate {
                y: self.y,
                m: self.m - 1,
                d: 1,
            }
        }
    }

    /// Last day of (year, month).
    fn last_of(y: i32, m: u32) -> CivilDate {
        CivilDate {
            y,
            m,
            d: Self::days_in_month(y, m),
        }
    }

    /// The current UTC date from the system clock (handler use). Pure std: convert
    /// whole days since the UNIX epoch to a civil date via Howard Hinnant's algorithm.
    fn today_utc() -> CivilDate {
        let secs = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs() as i64)
            .unwrap_or(0);
        Self::from_unix_days(secs.div_euclid(86_400))
    }

    /// Days since 1970-01-01 → (y, m, d) — Hinnant `civil_from_days`.
    fn from_unix_days(z: i64) -> CivilDate {
        let z = z + 719_468;
        let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
        let doe = z - era * 146_097; // [0, 146096]
        let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365; // [0, 399]
        let y = yoe + era * 400;
        let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
        let mp = (5 * doy + 2) / 153; // [0, 11]
        let d = doy - (153 * mp + 2) / 5 + 1; // [1, 31]
        let m = if mp < 10 { mp + 3 } else { mp - 9 }; // [1, 12]
        CivilDate {
            y: (y + i64::from(m <= 2)) as i32,
            m: m as u32,
            d: d as u32,
        }
    }
}

/// Days in a given month, accounting for Gregorian leap years.
fn days_in_month(y: i32, m: u32) -> u32 {
    match m {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 => {
            let leap = (y % 4 == 0 && y % 100 != 0) || (y % 400 == 0);
            if leap { 29 } else { 28 }
        }
        _ => 0,
    }
}

/// Parse the `YYYY-MM-DD` date prefix out of an ARM timestamp (RFC3339 or bare date).
///
/// P2 fix: the day is validated against the actual length of the month (incl. leap
/// years), so a malformed Custom date like `2026-02-31` is a client 400 here rather
/// than reaching Postgres as an invalid `$1::date` cast and surfacing as a 500.
fn parse_iso_date(s: &str) -> Result<CivilDate, ApiError> {
    let date_part = s
        .get(0..10)
        .ok_or_else(|| ApiError::bad_request("invalid timePeriod date"))?;
    let mut it = date_part.split('-');
    let y = it.next().and_then(|v| v.parse::<i32>().ok());
    let m = it.next().and_then(|v| v.parse::<u32>().ok());
    let d = it.next().and_then(|v| v.parse::<u32>().ok());
    match (y, m, d) {
        (Some(y), Some(m), Some(d))
            if y >= 1 && (1..=12).contains(&m) && d >= 1 && d <= days_in_month(y, m) =>
        {
            Ok(CivilDate { y, m, d })
        }
        // y < 1 (e.g. `0000-01-01`) is calendar-shaped but PostgreSQL rejects year
        // zero, so it must 400 here rather than reaching Postgres as a bad
        // `$1::date` cast and surfacing as a 500.
        _ => Err(ApiError::bad_request("invalid timePeriod date")),
    }
}

/// Translate a Cost Management `timeframe` enum to an inclusive `(from, to)` ISO
/// date-string pair (COST-03). The four CONTEXT must-haves (MonthToDate /
/// TheLastMonth / BillingMonthToDate / Custom) are implemented precisely; the other
/// documented 2025-03-01 members are accepted and clamped to a sane range; a genuinely
/// unknown timeframe is a 400 (T-9-IV — never a permissive default). The bounds are
/// returned as data and bound as `$N::date` in the handler (never spliced).
fn timeframe_to_range(
    timeframe: &str,
    custom: &Option<(String, String)>,
    today: CivilDate,
) -> Result<(String, String), ApiError> {
    let (from, to) = match timeframe {
        "MonthToDate" | "BillingMonthToDate" => (today.first_of_month(), today),
        "TheLastMonth" | "TheLastBillingMonth" => {
            let first = today.prev_month_first();
            (first, CivilDate::last_of(first.y, first.m))
        }
        // Clamp the current week to month-to-date (weekday math needs no extra crate;
        // a documented graceful approximation rather than a 400).
        "WeekToDate" => (today.first_of_month(), today),
        // Clamp to the 12 monthly periods we materialize: first-of-month 11 months back.
        "TheLastYear" => {
            let mut y = today.y;
            let mut m = today.m;
            for _ in 0..11 {
                if m == 1 {
                    y -= 1;
                    m = 12;
                } else {
                    m -= 1;
                }
            }
            (CivilDate { y, m, d: 1 }, today)
        }
        "Custom" => {
            let (f, t) = custom.as_ref().ok_or_else(|| {
                ApiError::bad_request("timeframe=Custom requires timePeriod.from/to")
            })?;
            (parse_iso_date(f)?, parse_iso_date(t)?)
        }
        _ => return Err(ApiError::bad_request("unsupported timeframe")),
    };
    Ok((from.iso(), to.iso()))
}

/// The echoed aggregation column name (`PreTaxCost` default if the body omits it).
fn aggregation_name(req: &QueryRequest) -> String {
    req.dataset
        .aggregation
        .as_ref()
        .and_then(|agg| agg.values().next())
        .and_then(|v| v.get("name"))
        .and_then(|n| n.as_str())
        .map(|s| s.to_string())
        .unwrap_or_else(|| "PreTaxCost".to_string())
}

// ---------------------------------------------------------------------------------
// Resource-exhaustion guards — fail-closed bounds on inbound shape and result
// size so an any-Bearer caller cannot force unbounded memory/CPU via a cost query.
// These constants are TRUSTED server values, inlined into SQL like the closed-match
// column literals — they never weaken the "$N binds only for user data" invariant.
// ---------------------------------------------------------------------------------

/// Maximum grouping dimensions per cost query. Azure Cost Management documents a max of
/// 2 groupings; rejecting more BEFORE any SQL runs bounds the GROUP BY fan-out and the
/// result cardinality at the source (faithful to Azure AND defensive).
const MAX_COST_GROUPINGS: usize = 2;

/// Hard cap on the rows a single cost query may return. The aggregate fetches
/// `MAX_COST_QUERY_ROWS + 1` and fails closed with a 400 when the extra row exists, so a
/// high-cardinality grouping (e.g. ResourceId → one row per resource) can never
/// materialize an unbounded row set / JSON body. A hard 400 — never a truncated 200.
const MAX_COST_QUERY_ROWS: usize = 1000;

/// App-owned cost-query deadline (milliseconds) — the AUTHORITATIVE timeout. The whole
/// streaming read runs inside a `tokio::time::timeout`, so an over-long query fails closed
/// with a DETERMINISTIC 400 without parsing Postgres error text (a message match breaks
/// under non-English `lc_messages`). See [`COST_QUERY_DB_BACKSTOP_MS`] for the server-side
/// safety net.
const COST_QUERY_TIMEOUT_MS: u64 = 3000;

/// Postgres `statement_timeout` (milliseconds) — the server-side BACKSTOP, set LOCAL in the
/// cost-query transaction and deliberately LONGER than [`COST_QUERY_TIMEOUT_MS`] so the app
/// deadline is the one that normally fires. Stops a runaway server-side query if the app
/// future is somehow not cancelled promptly. Bound as `$1` into `set_config` — never spliced.
const COST_QUERY_DB_BACKSTOP_MS: i32 = 5000;

/// Max UTF-8 bytes for a SINGLE response cell (a grouping key — resource id or JSONB tag
/// value). Row count bounds the number of cells, but a cell's size is otherwise unbounded
/// (profile tag values carry no `maxLength`, and a BYO-Postgres tenant is not guaranteed
/// synthetic-bounded), so one pathological value could still blow memory. 64 KiB is far
/// above any legitimate ARM id / tag value. Enforced SERVER-SIDE (`octet_length` + a `CASE`
/// that NULLs an oversized value) so the big value is never transferred to / decoded in Rust
/// — rejection AND pre-allocation safety (byte axis).
const MAX_COST_CELL_BYTES: usize = 64 * 1024;

/// Max SERIALIZED response body bytes, enforced WHILE streaming rows so the handler aborts
/// before materializing a giant result — the row cap alone does not bound total bytes. The
/// counter sums each cell's JSON-ESCAPED length (not raw UTF-8 — an escapable-heavy value
/// serializes larger) plus per-row and base envelope overhead, so it bounds the actual
/// response body. 8 MiB is generous for a bounded cost aggregate; over it fails closed
/// (byte axis).
const MAX_COST_RESPONSE_BYTES: usize = 8 * 1024 * 1024;

/// Fixed serialized-byte overhead of the response envelope OUTSIDE the row cells (the
/// `columns` array, `id`/`name`/`type`, braces). Seeded into the cumulative counter so the
/// budget bounds the whole body. Generous constant upper bound.
const RESPONSE_BASE_OVERHEAD_BYTES: usize = 2048;

/// Per-row serialized-byte overhead OUTSIDE the grouping-key cells (the aggregation number,
/// the `"USD"` currency cell, array brackets + commas). Added once per response row.
const ROW_ENVELOPE_OVERHEAD_BYTES: usize = 64;

/// Reject a cost query carrying more than [`MAX_COST_GROUPINGS`] grouping dimensions
/// (exhaustion guard) with an ARM-shaped 400 BEFORE any SQL is built or run.
/// Pure / DB-free so the "no DB round-trip on over-cap grouping" contract is provable
/// without a database.
fn check_grouping_limit(n: usize) -> Result<(), ApiError> {
    if n > MAX_COST_GROUPINGS {
        return Err(ApiError::bad_request(format!(
            "at most {MAX_COST_GROUPINGS} grouping dimensions are supported; the request \
             has {n} — reduce the grouping[] array to {MAX_COST_GROUPINGS} or fewer"
        )));
    }
    Ok(())
}

/// The LIMIT-overflow DECISION: given the number of rows actually fetched (the
/// query fetches [`MAX_COST_QUERY_ROWS`]` + 1`), fail closed with an ARM-shaped 400 when
/// the count exceeds the cap. Exactly [`MAX_COST_QUERY_ROWS`] is allowed — the 200
/// boundary. Pure / DB-free so the boundary is provable without a database.
fn cost_rows_within_cap(fetched_len: usize) -> Result<(), ApiError> {
    if fetched_len > MAX_COST_QUERY_ROWS {
        return Err(ApiError::bad_request(format!(
            "cost query result exceeds the {MAX_COST_QUERY_ROWS}-row cap; narrow the scope \
             (subscription / resource group) or use a coarser grouping"
        )));
    }
    Ok(())
}

/// The ARM 400 for an oversized response cell. The per-cell BYTE limit itself is now
/// enforced SERVER-SIDE (a `CASE` on `octet_length` NULLs an oversized value AND it is never
/// a hash/sort key), and a `bool_or` flag surfaces here so the handler still fails closed.
fn oversized_cell_error() -> ApiError {
    ApiError::bad_request(format!(
        "cost query response cell exceeds the {MAX_COST_CELL_BYTES}-byte limit; \
         use a coarser grouping"
    ))
}

/// A `std::io::Write` sink that buffers output but ERRORS once `cap` bytes would be exceeded.
/// Serializing the response through it bounds the ACTUAL body size — including all
/// user-controlled metadata (aggregation / grouping names, scope / id) that per-cell
/// accounting misses (P2). `over` records that the cap (not an I/O fault) stopped the write.
struct CappedWriter {
    buf: Vec<u8>,
    cap: usize,
    over: bool,
}

impl std::io::Write for CappedWriter {
    fn write(&mut self, data: &[u8]) -> std::io::Result<usize> {
        if self.buf.len() + data.len() > self.cap {
            self.over = true;
            return Err(std::io::Error::new(
                std::io::ErrorKind::WriteZero,
                "cost response exceeds cap",
            ));
        }
        self.buf.extend_from_slice(data);
        Ok(data.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

/// Run `fut` under an app-owned deadline (P2): on elapse, fail closed with a DETERMINISTIC
/// ARM 400 — locale-independent, no Postgres error-text parsing. Duration-parameterized so
/// the elapsed→400 mapping is unit-testable without needing a genuinely slow query.
async fn run_within_deadline<F>(
    deadline: std::time::Duration,
    fut: F,
) -> Result<Vec<(Vec<Option<String>>, f64)>, ApiError>
where
    F: std::future::Future<Output = Result<Vec<(Vec<Option<String>>, f64)>, ApiError>>,
{
    match tokio::time::timeout(deadline, fut).await {
        Ok(result) => result,
        Err(_elapsed) => Err(ApiError::bad_request(
            "cost query too expensive; narrow the scope (subscription / resource group) or \
             use a coarser grouping",
        )),
    }
}

/// Fail closed on the cumulative response byte budget (byte axis). Enforced WHILE
/// streaming so the handler aborts before materializing an oversized result. Pure / DB-free.
fn check_cumulative_bytes(total_len: usize) -> Result<(), ApiError> {
    if total_len > MAX_COST_RESPONSE_BYTES {
        return Err(ApiError::bad_request(format!(
            "cost query response exceeds the {MAX_COST_RESPONSE_BYTES}-byte limit; narrow \
             the scope (subscription / resource group) or use a coarser grouping"
        )));
    }
    Ok(())
}

/// Exact serialized byte length of `s` as a JSON string, INCLUDING the two surrounding
/// quotes, matching serde_json's default escaping. The cumulative response budget
/// counts this — not the raw UTF-8 length — so it bounds the actual response body size (a
/// value full of escapable characters serializes larger than its raw bytes). Pure / DB-free.
fn json_escaped_len(s: &str) -> usize {
    let mut n = 2; // the two surrounding double-quotes
    for c in s.chars() {
        n += match c {
            // serde_json escapes these to a 2-byte sequence (`\"`, `\\`, `\n`, …).
            '"' | '\\' | '\n' | '\r' | '\t' | '\u{08}' | '\u{0C}' => 2,
            // Other C0 control chars become `\u00XX` (6 bytes).
            c if (c as u32) < 0x20 => 6,
            c => c.len_utf8(),
        };
    }
    n
}

/// Collapse `(group-key, total)` pairs to unique keys, summing totals — **O(n)** via a
/// `HashMap` index into an order-preserving `Vec` (replaces the previous O(M²)
/// `iter_mut().find` linear scan, quadratic in the number of distinct keys). Preserves
/// FIRST-APPEARANCE order — the determinism contract: the SQL `ORDER BY` fixes
/// appearance order and this fold keeps it, so the response row order stays stable across
/// query plans and Postgres versions. The `type → ServiceName` service fold (D-10) happens
/// UPSTREAM, so keys arriving here are already folded and simply re-sum on collision.
fn fold_rows(pairs: Vec<(Vec<Option<String>>, f64)>) -> Vec<(Vec<Option<String>>, f64)> {
    let mut index: HashMap<Vec<Option<String>>, usize> = HashMap::with_capacity(pairs.len());
    let mut acc: Vec<(Vec<Option<String>>, f64)> = Vec::new();
    for (key, total) in pairs {
        match index.get(&key) {
            Some(&i) => acc[i].1 += total,
            None => {
                index.insert(key.clone(), acc.len());
                acc.push((key, total));
            }
        }
    }
    acc
}

// ---------------------------------------------------------------------------------
// Handlers — sub scope + RG scope. Register INSIDE the auth-gated `arm` router so the
// cost route inherits the any-Bearer scanner contract (presence-only auth). Route wiring lands in 09-05.
// ---------------------------------------------------------------------------------

/// `POST /subscriptions/{sub}/providers/Microsoft.CostManagement/query` (sub scope).
pub async fn cost_query(
    State(state): State<AppState>,
    Path(sub): Path<Uuid>,
    Json(req): Json<QueryRequest>,
) -> Result<Response, ApiError> {
    let scope = format!("/subscriptions/{sub}");
    run_cost_query(&state, &scope, sub, None, req).await
}

/// RG-scoped cost query. Shares the `{*tail}` catch-all with the GET resource-detail
/// route (POST+GET on one path is the standard axum merge — no static-vs-wildcard
/// overlap); anything other than the CostManagement query path is a 404.
pub async fn cost_query_scoped(
    State(state): State<AppState>,
    Path((sub, rg, tail)): Path<(Uuid, String, String)>,
    Json(req): Json<QueryRequest>,
) -> Result<Response, ApiError> {
    if tail != "Microsoft.CostManagement/query" {
        return Err(ApiError::NotFound { what: tail });
    }
    let scope = format!("/subscriptions/{sub}/resourceGroups/{rg}");
    run_cost_query(&state, &scope, sub, Some(rg), req).await
}

/// Shared query path for both scopes: timeframe → bound range, closed-match grouping
/// dispatch, JOIN `cost_records ⋈ resources` (D-06), `SUM(cost_amount)`, ServiceName
/// fold (D-10), positional QueryResult envelope.
async fn run_cost_query(
    state: &AppState,
    scope: &str,
    sub: Uuid,
    rg: Option<String>,
    req: QueryRequest,
) -> Result<Response, ApiError> {
    // fail closed on an over-cap grouping[] BEFORE any SQL is built or run — an
    // unbounded grouping array must never fan out the GROUP BY / result cardinality.
    check_grouping_limit(req.dataset.grouping.len())?;

    // COST-04: Usage/ActualCost/AmortizedCost all accepted; identical SUM in v2.0
    // (amortization math deferred). granularity Daily downgrades to the monthly
    // aggregate against our monthly store (Open Question 1) — both aggregate here.
    let _cost_type = req.r#type.as_deref().unwrap_or("ActualCost");
    let _granularity = req.dataset.granularity.as_deref().unwrap_or("None");

    // Timeframe → bound date range ($1 from, $2 to).
    let custom = req
        .time_period
        .as_ref()
        .map(|t| (t.from.clone(), t.to.clone()));
    let (from, to) = timeframe_to_range(&req.timeframe, &custom, CivilDate::today_utc())?;

    // Closed-match grouping dispatch. Fixed binds: $1 from, $2 to, $3 sub, [$4 rg].
    // Tag-key binds start after the scope binds.
    let mut bind_ix: i32 = if rg.is_some() { 4 } else { 3 };
    let mut tag_args: Vec<String> = Vec::new();
    let mut group_sql: Vec<String> = Vec::new();
    for g in &req.dataset.grouping {
        group_sql.push(group_expr(g, &mut bind_ix, &mut tag_args)?);
    }

    // BOUNDED group/order/select key per grouping expr: the CASE result — the value
    // when its byte length is within MAX_COST_CELL_BYTES, else NULL. GROUP BY and ORDER BY key
    // on THIS bounded expression, so the raw oversized value is NEVER a Postgres hash/sort key
    // (it is read once per row to compute octet_length, but never accumulated in a sort buffer
    // or hash table). For normal (in-cap) values the CASE is identity, so grouping semantics
    // are unchanged.
    let bounded_keys: Vec<String> = group_sql
        .iter()
        .map(|e| {
            format!(
                "CASE WHEN octet_length(({e})::text) <= {MAX_COST_CELL_BYTES} \
                 THEN ({e})::text ELSE NULL END"
            )
        })
        .collect();
    // Each grouping yields the bounded value `g{i}` plus `g{i}_over` = did ANY member of this
    // group exceed the cap? (bool_or over the raw octet_length). The handler fails closed on
    // `g{i}_over` so an oversized value still 400s even though it was never a sort/hash key.
    let select_cols: String = group_sql
        .iter()
        .enumerate()
        .map(|(i, e)| {
            format!(
                ", {key} AS g{i}, bool_or(octet_length(({e})::text) > {MAX_COST_CELL_BYTES}) AS g{i}_over",
                key = bounded_keys[i]
            )
        })
        .collect();
    let group_by = if bounded_keys.is_empty() {
        String::new()
    } else {
        format!(" GROUP BY {}", bounded_keys.join(", "))
    };
    // P3 fix: GROUP BY does not guarantee row order; an explicit ORDER BY on the (bounded)
    // grouping expressions makes the response row order deterministic across repeated calls,
    // query plans, and Postgres versions (the fold below preserves first-appearance order, so
    // a stable SQL order yields a stable response).
    let order_by = if bounded_keys.is_empty() {
        String::new()
    } else {
        format!(" ORDER BY {}", bounded_keys.join(", "))
    };
    let scope_pred = if rg.is_some() {
        " AND c.subscription_id = $3 AND r.resource_group_name = $4"
    } else {
        " AND c.subscription_id = $3"
    };
    // fetch CAP+1 so an over-cap result is DETECTABLE (rows.len() > CAP → 400).
    // The limit is a trusted server constant, inlined like the closed-match column
    // literals — it is not user data, so it never weakens the "$N binds only for user
    // data" invariant (T-9-01).
    let fetch_limit = MAX_COST_QUERY_ROWS + 1;
    let sql = format!(
        "SELECT SUM(c.cost_amount) AS total{select_cols}
         FROM synthetic.cost_records c
         JOIN synthetic.resources r ON r.id = c.resource_id
         WHERE c.billing_period BETWEEN $1::date AND $2::date{scope_pred}{group_by}{order_by}
         LIMIT {fetch_limit}"
    );

    // Bind order: $1 from, $2 to, $3 sub, [$4 rg], then tag keys.
    let mut q = sqlx::query(&sql).bind(from).bind(to).bind(sub);
    if let Some(rg) = &rg {
        q = q.bind(rg);
    }
    for a in &tag_args {
        q = q.bind(a);
    }

    // bound Postgres compute with a LOCAL statement_timeout — the server-side
    // BACKSTOP (LIMIT bounds our memory but Postgres may compute all groups before applying
    // it). The ms value is BOUND as $1 (never spliced); set_config's 3rd arg `true` scopes it
    // LOCAL to this txn. The AUTHORITATIVE deadline is the app-owned tokio::time::timeout
    // below — locale-independent, no Postgres error-text parsing.
    let mut tx = state.pool.begin().await?;
    sqlx::query("SELECT set_config('statement_timeout', $1, true)")
        .bind(COST_QUERY_DB_BACKSTOP_MS.to_string())
        .execute(&mut *tx)
        .await?;

    // Precompute per-grouping "is this a ServiceName fold?" so the streaming future captures
    // only `group_meta` + the count (not `req`) — `req`/`group_sql` stay owned for the column
    // build below.
    let group_meta: Vec<bool> = req
        .dataset
        .grouping
        .iter()
        .map(is_service_dimension)
        .collect();
    let group_count = group_meta.len();

    // STREAM rows and enforce EVERY bound WHILE reading — row count, per-cell bytes
    // (the oversized value is already NULLed server-side; `g{i}_len` carries the real byte
    // length, so nothing huge is decoded in Rust), and cumulative SERIALIZED bytes — failing
    // closed BEFORE materializing a large result. The whole read runs inside an app-owned
    // deadline: on elapse we return a DETERMINISTIC 400 without parsing Postgres error text
    // (locale-safe). Any DB error (including a backstop statement_timeout or an admin cancel)
    // maps to a 500 via `?` — the client-facing "too expensive" signal is the app timer, not
    // a fragile SQLSTATE + message match.
    //
    // The row-count cap is on RAW fetched rows (pre-fold): a query whose raw cardinality
    // exceeds the cap is rejected even if the rows would fold to fewer service groups —
    // deliberate, the bound is on the memory the DB hands us (with the ≤2 grouping limit and
    // the bounded type catalogue this is unreachable in practice, but the contract is: raw
    // rows, pre-fold). The type→ServiceName fold (D-10) is applied here, so keys are folded.
    let read = async move {
        let mut stream = q.fetch(&mut *tx);
        let mut pairs: Vec<(Vec<Option<String>>, f64)> = Vec::new();
        let mut serialized_bytes: usize = RESPONSE_BASE_OVERHEAD_BYTES;
        while let Some(item) = stream.next().await {
            let row = item?; // any DB error → ApiError::Internal (500)

            // Row-count cap on RAW rows: LIMIT is CAP+1, so a (CAP+1)th row exceeds the cap.
            cost_rows_within_cap(pairs.len() + 1)?;

            let total: Option<f64> = row.try_get("total")?;
            let total = total.unwrap_or(0.0);
            serialized_bytes += ROW_ENVELOPE_OVERHEAD_BYTES;

            let mut key: Vec<Option<String>> = Vec::with_capacity(group_count);
            for (i, &is_service) in group_meta.iter().enumerate() {
                // an oversized member is flagged server-side (`bool_or`); the raw value
                // was never a hash/sort key and the CASE already NULLed it, so nothing huge is
                // decoded in Rust. Fail closed here. `bool_or` is NULL for an all-NULL group
                // (a genuinely-null grouping value) → not oversized.
                let oversized: Option<bool> = row.try_get(format!("g{i}_over").as_str())?;
                if oversized == Some(true) {
                    return Err(oversized_cell_error());
                }

                let raw: Option<String> = row.try_get(format!("g{i}").as_str())?; // ≤ cap bytes
                let cell = if is_service {
                    raw.map(|t| service_name(&t).to_string())
                } else {
                    raw
                };
                // Cumulative memory guard (bounds the `pairs`/`acc` we build). The
                // AUTHORITATIVE response-body bound is the CappedWriter serialization below,
                // which also counts metadata; here we count the JSON-escaped cell length so
                // this early abort tracks response bytes closely.
                serialized_bytes += match &cell {
                    Some(s) => json_escaped_len(s),
                    None => 4, // "null"
                };
                check_cumulative_bytes(serialized_bytes)?;
                key.push(cell);
            }
            pairs.push((key, total));
        }
        // The stream borrows `tx`; drop it before committing the (read-only) transaction.
        drop(stream);
        tx.commit().await?;
        Ok::<Vec<(Vec<Option<String>>, f64)>, ApiError>(pairs)
    };

    // App-owned deadline (authoritative, locale-independent) — see `run_within_deadline`.
    // On elapse it returns the ARM 400; the future is dropped, rolling back the read-only tx.
    let pairs = run_within_deadline(
        std::time::Duration::from_millis(COST_QUERY_TIMEOUT_MS),
        read,
    )
    .await?;

    // Re-sum rows collapsing to the same key — O(n), first-appearance order preserved.
    let acc = fold_rows(pairs);

    // Columns: aggregation (Number) first, then one String per grouping, then Currency.
    let mut columns = vec![QueryColumn {
        name: aggregation_name(&req),
        r#type: "Number".to_string(),
    }];
    for g in &req.dataset.grouping {
        let col_name = if is_service_dimension(g) {
            "ServiceName".to_string()
        } else {
            g.name.clone()
        };
        columns.push(QueryColumn {
            name: col_name,
            r#type: "String".to_string(),
        });
    }
    columns.push(QueryColumn {
        name: "Currency".to_string(),
        r#type: "String".to_string(),
    });

    let rows_out: Vec<Vec<Value>> = acc
        .into_iter()
        .map(|(key, total)| {
            let mut cells: Vec<Value> = Vec::with_capacity(columns.len());
            cells.push(json!(total));
            for k in key {
                cells.push(json!(k)); // null when None
            }
            cells.push(json!("USD")); // D-11
            cells
        })
        .collect();

    let guid = Uuid::new_v4().to_string();
    let result = QueryResult {
        id: format!("{scope}/providers/Microsoft.CostManagement/Query/{guid}"),
        name: guid,
        r#type: "microsoft.costmanagement/Query".to_string(),
        properties: QueryProperties {
            columns,
            rows: rows_out,
            // nextLink is always null: the result is bounded fail-closed by
            // MAX_COST_QUERY_ROWS + the byte budget. An over-cap query returns a hard
            // 400 (narrow scope / coarser grouping), it does NOT paginate, so a non-null
            // nextLink could never be honored.
            next_link: None,
        },
    };

    // P2: serialize through a capped writer so the AUTHORITATIVE response-body bound covers
    // ALL fields — including the user-controlled aggregation name, grouping (column) names and
    // scope/id that per-cell accounting misses. Over the cap → a fail-closed 400, never a
    // response larger than the documented limit.
    let mut writer = CappedWriter {
        buf: Vec::new(),
        cap: MAX_COST_RESPONSE_BYTES,
        over: false,
    };
    match serde_json::to_writer(&mut writer, &result) {
        Ok(()) => {}
        Err(_) if writer.over => {
            return Err(ApiError::bad_request(format!(
                "cost query response exceeds the {MAX_COST_RESPONSE_BYTES}-byte limit; narrow \
                 the scope (subscription / resource group) or use a coarser grouping"
            )));
        }
        Err(e) => return Err(ApiError::Internal(format!("cost response serialize: {e}"))),
    }

    Ok(([(header::CONTENT_TYPE, "application/json")], writer.buf).into_response())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// T-9-01: a Tag grouping whose key carries SQL metacharacters must reach SQL only
    /// as a `$N` bind (`r.tags ->> $N`) — never spliced — and the closed-match
    /// dimensions are trusted column literals consuming no bind. `placeholder_count ==
    /// args.len()`.
    #[test]
    fn group_dispatch_is_placeholders_only() {
        let attack = "'; DROP TABLE synthetic.cost_records;--";
        let g = Grouping {
            kind: "Tag".to_string(),
            name: attack.to_string(),
        };
        let mut bind_ix = 3; // sub-scope: $1 from, $2 to, $3 sub → next free is $4
        let mut args = Vec::<String>::new();
        let expr = group_expr(&g, &mut bind_ix, &mut args).expect("Tag grouping is valid");

        // The fragment references only the JSONB ->> operator with a $N placeholder.
        assert_eq!(expr, "r.tags ->> $4", "tag key must be bound, not spliced");
        assert!(
            !expr.contains(attack),
            "raw attack literal must not reach SQL: {expr:?}"
        );
        assert!(!expr.contains("DROP"), "no spliced SQL keyword: {expr:?}");
        assert!(!expr.contains(';'), "no statement separator: {expr:?}");

        // The key is carried verbatim as a bound arg (data, not code).
        assert_eq!(args, vec![attack.to_string()]);
        // Every $N has exactly one bound arg.
        assert_eq!(expr.matches('$').count(), args.len());
        // The bind index advanced exactly once.
        assert_eq!(bind_ix, 4);

        // Closed-match dimensions are trusted literals, consume no bind, do not advance.
        for (kind, name, col) in [
            ("Dimension", "ResourceType", "r.type"),
            ("Dimension", "ResourceGroup", "r.resource_group_name"),
            ("Dimension", "ResourceId", "c.resource_id"),
            ("Dimension", "SubscriptionId", "c.subscription_id"),
            ("Dimension", "ServiceName", "r.type"),
            ("Dimension", "MeterCategory", "r.type"),
        ] {
            let g = Grouping {
                kind: kind.to_string(),
                name: name.to_string(),
            };
            let mut bi = 3;
            let mut a = Vec::<String>::new();
            let e = group_expr(&g, &mut bi, &mut a).expect("known dimension");
            assert_eq!(e, col, "{name} must map to the trusted column {col}");
            assert!(a.is_empty(), "{name} consumes no bind");
            assert_eq!(bi, 3, "{name} must not advance the bind index");
        }

        // An unknown dimension → 400, never a permissive default (T-9-IV).
        let bad = Grouping {
            kind: "Dimension".to_string(),
            name: "Nonsense".to_string(),
        };
        let mut bi = 3;
        let mut a = Vec::<String>::new();
        assert!(group_expr(&bad, &mut bi, &mut a).is_err());
    }

    /// (a): the grouping-count guard rejects more than MAX_COST_GROUPINGS with an
    /// ARM-shaped 400 — pure/DB-free, so this fires BEFORE any SQL round-trip.
    #[test]
    fn grouping_guard_rejects_more_than_two() {
        let err = check_grouping_limit(MAX_COST_GROUPINGS + 1).expect_err("3 groupings → 400");
        assert!(
            matches!(err, ApiError::BadRequest { .. }),
            "over-cap grouping must be a 400 BadRequest, got {err:?}"
        );
    }

    /// (a): zero and exactly MAX_COST_GROUPINGS groupings are allowed.
    #[test]
    fn grouping_guard_allows_zero_and_two() {
        assert!(check_grouping_limit(0).is_ok(), "0 groupings is allowed");
        assert!(
            check_grouping_limit(MAX_COST_GROUPINGS).is_ok(),
            "exactly MAX_COST_GROUPINGS is allowed"
        );
    }

    /// (LIMIT-overflow decision): exactly MAX_COST_QUERY_ROWS is the 200 boundary.
    #[test]
    fn row_cap_allows_exactly_cap() {
        assert!(
            cost_rows_within_cap(MAX_COST_QUERY_ROWS).is_ok(),
            "exactly CAP rows → Ok (the 200 boundary)"
        );
    }

    /// (c): CAP+1 rows fail closed with an ARM-shaped 400 — never a partial 200.
    #[test]
    fn row_cap_rejects_cap_plus_one() {
        let err = cost_rows_within_cap(MAX_COST_QUERY_ROWS + 1).expect_err("CAP+1 → 400");
        assert!(
            matches!(err, ApiError::BadRequest { .. }),
            "CAP+1 must be a 400 BadRequest, got {err:?}"
        );
    }

    /// (byte axis): the oversized-cell error is an ARM 400 (the per-cell limit itself
    /// is enforced server-side via the CASE + bool_or flag; this maps the flag to a 400).
    #[test]
    fn oversized_cell_is_400() {
        assert!(
            matches!(oversized_cell_error(), ApiError::BadRequest { .. }),
            "oversized cell must be a 400 BadRequest"
        );
    }

    /// P2: the CappedWriter accepts writes up to `cap` and fails closed (setting `over`) on
    /// the write that would exceed it — the mechanism that bounds the SERIALIZED response body
    /// including user-controlled metadata.
    #[test]
    fn capped_writer_fails_closed_over_cap() {
        use std::io::Write as _;
        let mut w = CappedWriter {
            buf: Vec::new(),
            cap: 8,
            over: false,
        };
        assert!(w.write_all(b"12345").is_ok(), "within cap ok");
        assert!(w.write_all(b"678").is_ok(), "exactly at cap ok");
        assert!(!w.over, "not over at exactly cap");
        let err = w.write_all(b"9").expect_err("over cap must error");
        assert_eq!(err.kind(), std::io::ErrorKind::WriteZero);
        assert!(w.over, "over flag set once the cap is crossed");
        // serde_json serializing a value larger than the cap trips `over`.
        let mut w2 = CappedWriter {
            buf: Vec::new(),
            cap: 4,
            over: false,
        };
        assert!(serde_json::to_writer(&mut w2, &"a long string value").is_err());
        assert!(w2.over, "serde overflow sets the cap flag");
    }

    /// (byte axis): cumulative bytes at the budget are allowed; over it → ARM 400.
    #[test]
    fn cumulative_bytes_boundary() {
        assert!(
            check_cumulative_bytes(MAX_COST_RESPONSE_BYTES).is_ok(),
            "exactly the cumulative budget is allowed"
        );
        let err = check_cumulative_bytes(MAX_COST_RESPONSE_BYTES + 1)
            .expect_err("over-budget response → 400");
        assert!(
            matches!(err, ApiError::BadRequest { .. }),
            "an over-budget response must be a 400, got {err:?}"
        );
    }

    /// (byte axis, serialized): `json_escaped_len` matches serde_json's exact
    /// serialized length (incl. quotes) so the cumulative budget bounds the RESPONSE body,
    /// not raw UTF-8. Cross-checked against `serde_json::to_string` on the same inputs.
    #[test]
    fn json_escaped_len_matches_serde() {
        for s in [
            "",
            "plain",
            "with \"quotes\" and \\backslash",
            "tabs\tand\nnewlines",
            "unicode: café — ☃",
            "\u{0001}\u{001f}", // C0 controls → \u00XX (6 bytes each)
        ] {
            let expected = serde_json::to_string(s).expect("serialize str").len();
            assert_eq!(
                json_escaped_len(s),
                expected,
                "escaped len must match serde_json for {s:?}"
            );
        }
        // An escapable-heavy value serializes LARGER than its raw byte length — the reason
        // the cumulative budget must count escaped, not raw, bytes.
        let quotes = "\"".repeat(100);
        assert!(
            json_escaped_len(&quotes) > quotes.len(),
            "escaping must inflate the counted length"
        );
    }

    /// P2 (timeout): an ELAPSED app deadline maps to an ARM 400 — deterministic and
    /// locale-independent, proven without a slow query by parameterizing the deadline. The
    /// slow future never completes (the timeout drops it), so the test is fast.
    #[tokio::test]
    async fn deadline_elapsed_maps_to_400() {
        let slow = async {
            tokio::time::sleep(std::time::Duration::from_secs(3600)).await;
            Ok::<Vec<(Vec<Option<String>>, f64)>, ApiError>(vec![])
        };
        let err = run_within_deadline(std::time::Duration::from_millis(5), slow)
            .await
            .expect_err("elapsed deadline → 400");
        assert!(
            matches!(err, ApiError::BadRequest { .. }),
            "deadline elapse must be a 400, got {err:?}"
        );
    }

    /// P2 (timeout): a future that finishes within the deadline passes through unchanged
    /// (Ok and Err results are both forwarded verbatim — the deadline only adds the 400).
    #[tokio::test]
    async fn deadline_not_elapsed_passes_through() {
        let fast = async {
            Ok::<Vec<(Vec<Option<String>>, f64)>, ApiError>(vec![(
                vec![Some("a".to_string())],
                1.0,
            )])
        };
        let out = run_within_deadline(std::time::Duration::from_secs(3600), fast)
            .await
            .expect("within deadline");
        assert_eq!(
            out.len(),
            1,
            "result forwarded verbatim when within deadline"
        );
    }

    /// (f): the O(n) fold preserves FIRST-APPEARANCE order (the determinism
    /// contract) and re-sums a repeated key into its first slot.
    #[test]
    fn fold_preserves_first_appearance_order() {
        let key = |s: &str| vec![Some(s.to_string())];
        let pairs = vec![
            (key("A"), 1.0),
            (key("B"), 2.0),
            (key("A"), 3.0), // re-sum into A's first slot
            (key("C"), 4.0),
        ];
        let out = fold_rows(pairs);
        let order: Vec<String> = out.iter().map(|(k, _)| k[0].clone().unwrap()).collect();
        assert_eq!(
            order,
            vec!["A", "B", "C"],
            "first-appearance order preserved"
        );
        assert_eq!(out[0].1, 4.0, "A re-summed (1.0 + 3.0)");
        assert_eq!(out[1].1, 2.0);
        assert_eq!(out[2].1, 4.0);
    }

    /// (f): two rows folding to the same (service) key sum into one row, not two.
    #[test]
    fn fold_resums_duplicate_keys() {
        let key = |s: &str| vec![Some(s.to_string()), None];
        let pairs = vec![
            (key("Storage"), 10.0),
            (key("Storage"), 5.5), // service-fold collapse
            (key("Compute"), 2.0),
        ];
        let out = fold_rows(pairs);
        assert_eq!(out.len(), 2, "duplicate keys collapse to one row");
        assert_eq!(out[0], (key("Storage"), 15.5));
        assert_eq!(out[1], (key("Compute"), 2.0));
    }

    /// (f): the fold stays CORRECT at scale — ≥5000 distinct keys, each also re-summed
    /// on a second pass, preserving first-appearance order. O(n) is guaranteed structurally by
    /// the `HashMap`-indexed lookup in `fold_rows` (not by a wall-clock assertion — a timing
    /// bound is flaky under loaded CI / emulation and doesn't mathematically prove complexity;
    /// if a throughput regression check is ever wanted it belongs in a benchmark, not a unit
    /// test).
    #[test]
    fn fold_is_correct_on_large_input() {
        let n = 5000usize;
        let mut pairs: Vec<(Vec<Option<String>>, f64)> = Vec::with_capacity(n * 2);
        for i in 0..n {
            pairs.push((vec![Some(format!("k{i}"))], 1.0));
        }
        for i in 0..n {
            pairs.push((vec![Some(format!("k{i}"))], 2.0)); // every one re-sums
        }
        let out = fold_rows(pairs);
        assert_eq!(out.len(), n, "exactly n distinct keys");
        assert!(
            out.iter().all(|(_, t)| (*t - 3.0).abs() < 1e-9),
            "each key re-summed to 3.0 (1.0 + 2.0)"
        );
        assert_eq!(
            out[0].0,
            vec![Some("k0".to_string())],
            "first-appearance order"
        );
        assert_eq!(out[n - 1].0, vec![Some(format!("k{}", n - 1))]);
    }

    /// COST-03: each timeframe enum maps to a `(from, to)` date pair; the four CONTEXT
    /// must-haves precisely, Custom reads timePeriod.from/to, an unknown timeframe and
    /// a Custom-without-timePeriod are 400 (T-9-IV).
    #[test]
    fn timeframe_to_range_maps_each_enum() {
        // A fixed "today" for determinism: 2026-03-15. Feb 2026 is NOT a leap year (28d).
        let today = CivilDate {
            y: 2026,
            m: 3,
            d: 15,
        };

        let (f, t) = timeframe_to_range("MonthToDate", &None, today).unwrap();
        assert_eq!((f.as_str(), t.as_str()), ("2026-03-01", "2026-03-15"));

        let (f, t) = timeframe_to_range("TheLastMonth", &None, today).unwrap();
        assert_eq!((f.as_str(), t.as_str()), ("2026-02-01", "2026-02-28"));

        let (f, t) = timeframe_to_range("BillingMonthToDate", &None, today).unwrap();
        assert_eq!((f.as_str(), t.as_str()), ("2026-03-01", "2026-03-15"));

        let custom = Some((
            "2025-11-04T00:00:00Z".to_string(),
            "2025-12-20T00:00:00Z".to_string(),
        ));
        let (f, t) = timeframe_to_range("Custom", &custom, today).unwrap();
        assert_eq!((f.as_str(), t.as_str()), ("2025-11-04", "2025-12-20"));

        // Custom without a timePeriod → 400.
        assert!(timeframe_to_range("Custom", &None, today).is_err());
        // Genuinely unknown timeframe → 400 (never a permissive default).
        assert!(timeframe_to_range("Nonsense", &None, today).is_err());
    }

    /// COST-05/D-10: the type→ServiceName map is a Rust constant with zero scan
    /// provenance; known types resolve to fixed service strings (case-insensitively);
    /// unmapped types fall back to the provider namespace.
    #[test]
    fn service_map_is_constant() {
        assert_eq!(
            service_name("Microsoft.Compute/virtualMachines"),
            "Virtual Machines"
        );
        assert_eq!(service_name("Microsoft.Storage/storageAccounts"), "Storage");
        assert_eq!(
            service_name("Microsoft.Sql/servers/databases"),
            "SQL Database"
        );
        // Case-insensitive: the stored real-profile casing differs from canonical.
        assert_eq!(
            service_name("microsoft.compute/virtualmachines"),
            "Virtual Machines"
        );
        // Unmapped type → provider namespace (everything before the first '/').
        assert_eq!(service_name("Microsoft.FooBar/widgets"), "Microsoft.FooBar");
        // Unmapped with no separator → the whole string.
        assert_eq!(service_name("weird"), "weird");
    }

    /// P2: a calendar-invalid Custom date is a client 400 at parse time, never a
    /// 500 from a rejected `$1::date` cast in Postgres. Days are validated against
    /// the real month length, including leap years.
    #[test]
    fn parse_iso_date_rejects_invalid_calendar_days() {
        // Valid month-ends pass.
        assert!(parse_iso_date("2026-01-31").is_ok());
        assert!(parse_iso_date("2024-02-29").is_ok()); // 2024 is a leap year
        assert!(parse_iso_date("2026-04-30").is_ok());
        assert!(parse_iso_date("2025-12-20T00:00:00Z").is_ok()); // RFC3339 prefix

        // Calendar-invalid days → Err (a 400, not a downstream 500).
        assert!(parse_iso_date("2026-02-31").is_err());
        assert!(parse_iso_date("2026-02-29").is_err()); // 2026 is NOT a leap year
        assert!(parse_iso_date("2026-04-31").is_err());
        assert!(parse_iso_date("2026-00-10").is_err()); // month 0
        assert!(parse_iso_date("2026-13-10").is_err()); // month 13
        assert!(parse_iso_date("2026-06-00").is_err()); // day 0
        assert!(parse_iso_date("2026-06-31").is_err()); // June has 30 days

        // Year zero is calendar-shaped but PostgreSQL rejects it → must 400 here.
        assert!(parse_iso_date("0000-01-01").is_err());
        assert!(parse_iso_date("0001-01-01").is_ok()); // year 1 is the lower bound
    }

    /// P2 helper: leap-year day counts are correct.
    #[test]
    fn days_in_month_leap_years() {
        assert_eq!(days_in_month(2024, 2), 29); // div by 4, not 100
        assert_eq!(days_in_month(2000, 2), 29); // div by 400
        assert_eq!(days_in_month(1900, 2), 28); // div by 100, not 400
        assert_eq!(days_in_month(2026, 2), 28);
        assert_eq!(days_in_month(2026, 4), 30);
        assert_eq!(days_in_month(2026, 12), 31);
    }
}
