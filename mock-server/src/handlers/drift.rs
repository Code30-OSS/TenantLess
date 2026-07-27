//! `GET /simulator/drift`, `/simulator/drift/{batchId}`,
//! `/simulator/drift/resources/{resourceId}` — the simulator-only drift audit
//! reads (DRIFT-05).
//!
//! These three endpoints are the ONLY surface that exposes the drift audit store
//! (`synthetic.drift_batches` / `synthetic.drift_records`). Drift audit data is
//! NEVER injected into any ARM response body (D-17): the only ARM-visible signal of
//! drift is the mutated resource state served by `resources.rs`. These reads serve
//! the before/after deltas purely for validation/debugging.
//!
//! Auth (D-15/D-16): all three routes register INSIDE the `arm` router BEFORE the
//! `bearer_auth` layer (NOT via the `/_console`+`/token` exempt merge), so they
//! inherit the any-Bearer scanner contract + the `--enforce-auth` RS256 swap —
//! missing Bearer → 401, any non-empty Bearer → 200 (enforcement off), a valid
//! RS256 JWT required under `--enforce-auth`.
//!
//! SQL (project injection bar / authorization.rs precedent): every read binds its
//! path param as `$N` — `batch_id` parses as `Path<Uuid>` (parse-before-bind), the
//! by-resource `resource_id` (a full ARM path containing `/`, captured via the
//! `{*resource_id}` catch-all) binds as `$1` and is never spliced. Timestamps are
//! read as `::text` so no `sqlx` chrono/time feature is required; JSONB columns
//! decode through `sqlx::types::Json<Value>`.

use crate::{
    error::ApiError,
    pagination::{
        PageParams, clamp_top, cursor_from_token, encode_token, next_link, split_numeric,
    },
    state::AppState,
};
use axum::{
    Json,
    extract::{Path, Query, State},
};
use serde::Serialize;
use serde_json::Value;
use sqlx::Row;
use sqlx::postgres::PgRow;
use sqlx::types::Json as SqlxJson;
use uuid::Uuid;

// ---------------------------------------------------------------------------------
// Response DTOs — OWN Serialize shapes (these are NOT ARM bodies; D-17).
// ---------------------------------------------------------------------------------

/// One drift batch (`synthetic.drift_batches`): batch-level audit metadata. `options`
/// is the JSONB option set the batch was applied with; the parent/result fingerprints
/// are the D-08 determinism anchors; `revertedAt` is `null` until the batch is reverted
/// (history is never deleted — D-03).
#[derive(Serialize)]
pub struct DriftBatchDto {
    #[serde(rename = "batchId")]
    batch_id: String,
    #[serde(rename = "driftType")]
    drift_type: String,
    seed: i64,
    options: Value,
    #[serde(rename = "parentFingerprint")]
    parent_fingerprint: String,
    #[serde(rename = "resultFingerprint")]
    result_fingerprint: String,
    #[serde(rename = "appliedAt")]
    applied_at: String,
    #[serde(rename = "revertedAt")]
    reverted_at: Option<String>,
}

/// One per-field drift delta (`synthetic.drift_records`): the `before`/`after` are the
/// full pre/post served-column values (A2) for a clean column-level revert; `fieldPath`
/// is audit readability (DRIFT-04).
#[derive(Serialize)]
pub struct DriftRecordDto {
    #[serde(rename = "recordId")]
    record_id: i64,
    #[serde(rename = "batchId")]
    batch_id: String,
    #[serde(rename = "resourceId")]
    resource_id: String,
    #[serde(rename = "subscriptionId")]
    subscription_id: Option<String>,
    #[serde(rename = "fieldPath")]
    field_path: String,
    before: Option<Value>,
    after: Option<Value>,
    /// The computed mutation code that produced this record (DRIFT audit surface —
    /// remediation 2/3). NULLABLE: rows back-filled before remediation 2 stay `null`,
    /// so this is `Option` to avoid a decode panic on legacy NULL audit columns.
    #[serde(rename = "driftCode")]
    drift_code: Option<String>,
    /// Per-record drift metadata (JSONB). NULLABLE for the same back-fill reason.
    metadata: Option<Value>,
}

/// `GET /simulator/drift` envelope — a capped, keyset-paginated page of batches.
/// `nextLink` carries the opaque `$skiptoken` continuation when more batches exist
/// past this page; it is omitted (None) on the final page (matching the ARM
/// resource-list `nextLink` contract — MOCK-08).
#[derive(Serialize)]
pub struct DriftBatchList {
    value: Vec<DriftBatchDto>,
    #[serde(rename = "nextLink", skip_serializing_if = "Option::is_none")]
    next_link: Option<String>,
}

/// `GET /simulator/drift/{batchId}` envelope — the (single) batch plus a capped,
/// keyset-paginated page of its per-field records. `nextLink` continues the RECORDS
/// (the batch itself is never paginated); omitted on the final records page.
#[derive(Serialize)]
pub struct DriftBatchDetail {
    batch: DriftBatchDto,
    records: Vec<DriftRecordDto>,
    #[serde(rename = "nextLink", skip_serializing_if = "Option::is_none")]
    next_link: Option<String>,
}

/// `GET /simulator/drift/resources/{resourceId}` envelope — a capped, keyset-paginated
/// page of every drift record that touched the given resource id (across batches).
/// `nextLink` continues the records; omitted on the final page.
#[derive(Serialize)]
pub struct DriftRecordList {
    value: Vec<DriftRecordDto>,
    #[serde(rename = "nextLink", skip_serializing_if = "Option::is_none")]
    next_link: Option<String>,
}

// ---------------------------------------------------------------------------------
// Row → DTO mappers (mirror authorization.rs::list_role_assignments try_get idiom).
// ---------------------------------------------------------------------------------

/// The batch column list — shared by `list_drift` (all batches) and `get_batch` (one
/// batch). `applied_at`/`reverted_at` are cast to `text` so no sqlx chrono/time feature
/// is required.
const BATCH_COLUMNS: &str = "batch_id, drift_type, seed, options, parent_fingerprint, \
     result_fingerprint, applied_at::text AS applied_at, reverted_at::text AS reverted_at";

/// The record column list — shared by `get_batch` and `by_resource`.
const RECORD_COLUMNS: &str = "record_id, batch_id, resource_id, subscription_id, field_path, before, after, \
     drift_code, metadata";

fn batch_dto(row: &PgRow) -> Result<DriftBatchDto, ApiError> {
    let batch_id: Uuid = row.try_get("batch_id")?;
    let options: SqlxJson<Value> = row.try_get("options")?;
    Ok(DriftBatchDto {
        batch_id: batch_id.to_string(),
        drift_type: row.try_get("drift_type")?,
        seed: row.try_get("seed")?,
        options: options.0,
        parent_fingerprint: row.try_get("parent_fingerprint")?,
        result_fingerprint: row.try_get("result_fingerprint")?,
        applied_at: row.try_get("applied_at")?,
        reverted_at: row.try_get("reverted_at")?,
    })
}

fn record_dto(row: &PgRow) -> Result<DriftRecordDto, ApiError> {
    let batch_id: Uuid = row.try_get("batch_id")?;
    let subscription_id: Option<Uuid> = row.try_get("subscription_id")?;
    let before: Option<SqlxJson<Value>> = row.try_get("before")?;
    let after: Option<SqlxJson<Value>> = row.try_get("after")?;
    let metadata: Option<SqlxJson<Value>> = row.try_get("metadata")?;
    Ok(DriftRecordDto {
        record_id: row.try_get("record_id")?,
        batch_id: batch_id.to_string(),
        resource_id: row.try_get("resource_id")?,
        subscription_id: subscription_id.map(|s| s.to_string()),
        field_path: row.try_get("field_path")?,
        before: before.map(|j| j.0),
        after: after.map(|j| j.0),
        drift_code: row.try_get("drift_code")?,
        metadata: metadata.map(|j| j.0),
    })
}

// ---------------------------------------------------------------------------------
// Handlers — register INSIDE the `arm` router (gated; D-15).
// ---------------------------------------------------------------------------------

/// `GET /simulator/drift` — drift batches, oldest first, keyset-paginated. Audit-only (D-17).
///
/// Keyset continuation (matching the resources.rs idiom): `$top` defaults to 100 and is
/// clamped to `1..=1000` via [`clamp_top`] (DoS guard). The cursor keysets on `seq` —
/// the unique monotonic IDENTITY (sql/006) that gives a STABLE total order. We fetch
/// `LIMIT $2 (= top + 1)`; if a surplus row comes back, [`split_numeric`] drops it and
/// emits a `nextLink` whose opaque `$skiptoken` encodes the last returned row's `seq`.
/// The cursor is bound as `$1::bigint` (NULL on page 1) and the limit as `$2` — neither
/// is ever spliced (project SQL bar).
pub async fn list_drift(
    State(state): State<AppState>,
    Query(params): Query<PageParams>,
) -> Result<Json<DriftBatchList>, ApiError> {
    let top = clamp_top(params.top);
    let cursor = cursor_from_token(params.skiptoken.as_deref())?;
    let sql = format!(
        "SELECT {BATCH_COLUMNS}, seq FROM synthetic.drift_batches \
         WHERE ($1::bigint IS NULL OR seq > $1) ORDER BY seq LIMIT $2"
    );
    let rows = sqlx::query(&sql)
        .bind(cursor)
        .bind(top + 1)
        .fetch_all(&state.pool)
        .await?;
    let (page, next_key) = split_numeric(rows, top, "seq")?;
    let mut value = Vec::with_capacity(page.len());
    for row in &page {
        value.push(batch_dto(row)?);
    }
    let next_link = next_key.map(|key| {
        next_link(
            &state.base_url,
            "/simulator/drift",
            top,
            &encode_token(&key.to_string()),
            params.api_version.as_deref(),
            None, // $filter is N/A for the drift reads.
        )
    });
    Ok(Json(DriftBatchList { value, next_link }))
}

/// `GET /simulator/drift/{batchId}` — the batch plus its per-field records. The
/// `batch_id` parses as `Path<Uuid>` (parse-before-bind) and binds as `$1`; an unknown
/// batch is a 404 `ResourceNotFound` (NOT an empty payload), matching the detail-route
/// 404 contract.
pub async fn get_batch(
    State(state): State<AppState>,
    Path(batch_id): Path<Uuid>,
    Query(params): Query<PageParams>,
) -> Result<Json<DriftBatchDetail>, ApiError> {
    let batch_sql =
        format!("SELECT {BATCH_COLUMNS} FROM synthetic.drift_batches WHERE batch_id = $1");
    let batch_row = sqlx::query(&batch_sql)
        .bind(batch_id)
        .fetch_optional(&state.pool)
        .await?
        .ok_or(ApiError::NotFound {
            what: batch_id.to_string(),
        })?;
    let batch = batch_dto(&batch_row)?;

    // The batch stays single; its RECORDS are keyset-paginated on `record_id` (the
    // BIGSERIAL PK total order). `$top` default 100, clamped 1..=1000; the cursor binds
    // as `$2::bigint` (NULL on page 1) and `LIMIT $3 (= top + 1)` drives the surplus
    // split. A surplus row emits a `nextLink` continuing THIS batch's records.
    let top = clamp_top(params.top);
    let cursor = cursor_from_token(params.skiptoken.as_deref())?;
    let records_sql = format!(
        "SELECT {RECORD_COLUMNS} FROM synthetic.drift_records \
         WHERE batch_id = $1 AND ($2::bigint IS NULL OR record_id > $2) \
         ORDER BY record_id LIMIT $3"
    );
    let record_rows = sqlx::query(&records_sql)
        .bind(batch_id)
        .bind(cursor)
        .bind(top + 1)
        .fetch_all(&state.pool)
        .await?;
    let (page, next_key) = split_numeric(record_rows, top, "record_id")?;
    let mut records = Vec::with_capacity(page.len());
    for row in &page {
        records.push(record_dto(row)?);
    }
    let next_link = next_key.map(|key| {
        next_link(
            &state.base_url,
            &format!("/simulator/drift/{batch_id}"),
            top,
            &encode_token(&key.to_string()),
            params.api_version.as_deref(),
            None,
        )
    });
    Ok(Json(DriftBatchDetail {
        batch,
        records,
        next_link,
    }))
}

/// `GET /simulator/drift/resources/{resourceId}` — every drift record touching the
/// given resource id, across batches. The captured `resource_id` is a full ARM path
/// (contains `/`) taken via the `{*resource_id}` catch-all, which strips the leading
/// `/`; ARM ids always begin with `/subscriptions/...`, so we re-add the leading slash
/// before the `$1` bind (the value is bound, NEVER spliced — project SQL bar).
pub async fn by_resource(
    State(state): State<AppState>,
    Path(resource_id): Path<String>,
    Query(params): Query<PageParams>,
) -> Result<Json<DriftRecordList>, ApiError> {
    let resource_id = if resource_id.starts_with('/') {
        resource_id
    } else {
        format!("/{resource_id}")
    };

    // Keyset-paginated on `record_id` (BIGSERIAL PK) across batches. `$top` default 100,
    // clamped 1..=1000; the cursor binds as `$2::bigint` (NULL on page 1) and `LIMIT $3
    // (= top + 1)` drives the surplus split. The resource id is bound as `$1`, never
    // spliced (project SQL bar). A surplus row emits a `nextLink` continuing this
    // resource's records — the path reproduces the received resource id.
    let top = clamp_top(params.top);
    let cursor = cursor_from_token(params.skiptoken.as_deref())?;
    let sql = format!(
        "SELECT {RECORD_COLUMNS} FROM synthetic.drift_records \
         WHERE resource_id = $1 AND ($2::bigint IS NULL OR record_id > $2) \
         ORDER BY record_id LIMIT $3"
    );
    let rows = sqlx::query(&sql)
        .bind(&resource_id)
        .bind(cursor)
        .bind(top + 1)
        .fetch_all(&state.pool)
        .await?;
    let (page, next_key) = split_numeric(rows, top, "record_id")?;
    let mut value = Vec::with_capacity(page.len());
    for row in &page {
        value.push(record_dto(row)?);
    }
    let next_link = next_key.map(|key| {
        // Reproduce the received (catch-all, leading-slash-stripped) resource id so the
        // emitted link replays the same route the client originally requested.
        let path = format!(
            "/simulator/drift/resources/{}",
            resource_id.trim_start_matches('/')
        );
        next_link(
            &state.base_url,
            &path,
            top,
            &encode_token(&key.to_string()),
            params.api_version.as_deref(),
            None,
        )
    });
    Ok(Json(DriftRecordList { value, next_link }))
}
