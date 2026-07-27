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
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sqlx::Row;
use std::collections::BTreeMap;
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
// Handlers — sub scope + RG scope. Register INSIDE the auth-gated `arm` router so the
// cost route inherits the any-Bearer scanner contract (presence-only auth). Route wiring lands in 09-05.
// ---------------------------------------------------------------------------------

/// `POST /subscriptions/{sub}/providers/Microsoft.CostManagement/query` (sub scope).
pub async fn cost_query(
    State(state): State<AppState>,
    Path(sub): Path<Uuid>,
    Json(req): Json<QueryRequest>,
) -> Result<Json<QueryResult>, ApiError> {
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
) -> Result<Json<QueryResult>, ApiError> {
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
) -> Result<Json<QueryResult>, ApiError> {
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

    // Each grouping expr is selected ::text (uniform read) and aliased g{i};
    // SUM(cost_amount) is the first column.
    let select_cols: String = group_sql
        .iter()
        .enumerate()
        .map(|(i, e)| format!(", ({e})::text AS g{i}"))
        .collect();
    let group_by = if group_sql.is_empty() {
        String::new()
    } else {
        format!(" GROUP BY {}", group_sql.join(", "))
    };
    // P3 fix: GROUP BY does not guarantee row order; an explicit ORDER BY on the
    // grouping expressions makes the response row order deterministic across
    // repeated calls, query plans, and Postgres versions (the fold below preserves
    // first-appearance order, so a stable SQL order yields a stable response).
    let order_by = if group_sql.is_empty() {
        String::new()
    } else {
        format!(" ORDER BY {}", group_sql.join(", "))
    };
    let scope_pred = if rg.is_some() {
        " AND c.subscription_id = $3 AND r.resource_group_name = $4"
    } else {
        " AND c.subscription_id = $3"
    };
    let sql = format!(
        "SELECT SUM(c.cost_amount) AS total{select_cols}
         FROM synthetic.cost_records c
         JOIN synthetic.resources r ON r.id = c.resource_id
         WHERE c.billing_period BETWEEN $1::date AND $2::date{scope_pred}{group_by}{order_by}"
    );

    // Bind order: $1 from, $2 to, $3 sub, [$4 rg], then tag keys.
    let mut q = sqlx::query(&sql).bind(from).bind(to).bind(sub);
    if let Some(rg) = &rg {
        q = q.bind(rg);
    }
    for a in &tag_args {
        q = q.bind(a);
    }
    let rows = q.fetch_all(&state.pool).await?;

    // Read rows; fold type→ServiceName and re-sum rows collapsing to the same key.
    let mut acc: Vec<(Vec<Option<String>>, f64)> = Vec::new();
    for row in &rows {
        let total: Option<f64> = row.try_get("total")?;
        let total = total.unwrap_or(0.0);
        let mut key: Vec<Option<String>> = Vec::with_capacity(group_sql.len());
        for (i, g) in req.dataset.grouping.iter().enumerate() {
            let raw: Option<String> = row.try_get(format!("g{i}").as_str())?;
            let cell = if is_service_dimension(g) {
                raw.map(|t| service_name(&t).to_string())
            } else {
                raw
            };
            key.push(cell);
        }
        if let Some(slot) = acc.iter_mut().find(|(k, _)| *k == key) {
            slot.1 += total;
        } else {
            acc.push((key, total));
        }
    }

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
    Ok(Json(QueryResult {
        id: format!("{scope}/providers/Microsoft.CostManagement/Query/{guid}"),
        name: guid,
        r#type: "microsoft.costmanagement/Query".to_string(),
        properties: QueryProperties {
            columns,
            rows: rows_out,
            next_link: None, // nextLink = null for v2.0 (small grouping cardinalities)
        },
    }))
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
