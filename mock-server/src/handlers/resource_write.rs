//! Generic, service-model-independent ARM **write** handlers — `PUT` / `PATCH` /
//! single-target `DELETE` — dispatched off the existing
//! `/subscriptions/{sub}/resourceGroups/{rg}/providers/{*tail}` catch-all, gated behind
//! `--enable-arm-writes` (D-10). Writes are **synchronous**: the terminal status is
//! returned inline (`201`/`200`/`204`), never a `202`/LRO (SYNC-01/02).
//!
//! # Contract (Phase-23 decisions)
//! * **D-16 gating-before-parse:** the flag check is the FIRST statement of every write
//!   handler, before ANY body use — the body arrives as `axum::body::Bytes` (which never
//!   rejects on malformed JSON / wrong `Content-Type`), so a disabled write returns `405`
//!   even for a malformed body, and a malformed body under an enabled write becomes a
//!   controlled ARM `400 InvalidRequestContent` rather than axum's default rejection.
//! * **D-21 structural validation:** a top-level envelope field with the wrong JSON type
//!   for the `sql/009` CHECKs (`properties`/`tags`/`sku` object, `location`/`kind`/`id`/
//!   `name`/`type` string) is a controlled `400` BEFORE the write — never a `500` at the
//!   CHECK layer. Arbitrary NESTED content + unknown top-level keys stay permissive (D-05).
//! * **D-14 location:** absent → inject `"global"`; explicit `null` → `400 LocationRequired`.
//! * **D-03 identity conflict:** a body `id`/`name`/`type` that conflicts (case-insensitively)
//!   with the URL route → `400`; absent / case-insensitively-matching identity is accepted.
//! * **D-08/D-19 casing:** a NEW resource keeps the request-URL casing; an EXISTING overlay
//!   row keeps its first-write casing; an EXISTING baseline row keeps the BASELINE canonical
//!   casing (never the request-URL casing) — the upsert `ON CONFLICT` preserves the stored
//!   `id` (`id = synthetic.arm_overlay.id`, NOT `EXCLUDED.id`).
//! * **D-01 PATCH:** two-level merge over the FULL current body (overlay `body` if present,
//!   else the projected baseline) via [`two_level_merge`]; **D-20** PATCH on an absent /
//!   tombstoned id → `404` (PATCH never creates).
//! * **D-07/D-15 preconditions:** [`evaluate_precondition`] over the raw `If-Match` /
//!   `If-None-Match` headers vs the current resolved ETag; `Failed` → `412`.
//! * **D-04/D-06/D-24:** every accepted transition upserts a `source='user'` snapshot whose
//!   revision (assigned by the `sql/009` BEFORE trigger) derives the new `o-<revision>` ETag
//!   header — emitted on PUT/PATCH 200/201 AND on the DELETE `204` (the tombstone's ETag).
//!
//! All runtime values bind as `$N`; table/column names are static literals (project SQL
//! bar). The handlers read only `arm_resolved_resources` / `arm_overlay.body` — never the
//! raw `synthetic.resources` table (reader-inventory gate).

use crate::{
    arm::{Resource, ResourceRow},
    error::ApiError,
    etag::{Precond, baseline_resource_etag, evaluate_precondition, overlay_etag},
    state::AppState,
    write_merge::{descendant_ids, two_level_merge},
};
use axum::{
    Json,
    body::Bytes,
    extract::{Path, State},
    http::{HeaderMap, HeaderValue, StatusCode, header},
    response::{IntoResponse, Response},
};
use serde_json::{Value, json};
use uuid::Uuid;

/// A single transaction-scoped advisory-lock key serializing the ENTIRE ARM write plane
/// (D-27). Every PUT/PATCH/DELETE runs its `read_current` → precondition → mutation inside
/// ONE transaction holding this lock, which closes two TOCTOU races the per-call pooled
/// connections left open:
/// * **If-Match atomicity** — the current-ETag read and its dependent upsert are one atomic
///   unit, so a concurrent revision bump between them can no longer produce a lost update.
/// * **DELETE cascade snapshot** — the descendant `gather` and the cascade tombstone commit
///   under the same lock, so a child inserted in the old gather→commit window can no longer
///   escape the cascade.
///
/// One constant key (not a per-id key) is deliberate: it serializes cross-id operations too
/// (a parent DELETE vs a concurrent child PUT), which a per-id lock could not. The write
/// plane is off-by-default and single-tenant, so serialized writes are a non-issue —
/// correctness over write throughput.
const ARM_WRITE_PLANE_LOCK: i64 = 0x0041_524d_5752_4954; // "\0ARM_WRIT"

/// Take the [`ARM_WRITE_PLANE_LOCK`] advisory lock on an open write transaction
/// (`pg_advisory_xact_lock`, auto-released on commit/rollback). Each write handler opens its
/// transaction with `state.pool.begin()` then calls this FIRST, so its read→check→mutate runs
/// while holding the lock. (A free `&mut PgConnection` parameter — no borrowed return type — so
/// this file stays free of explicit lifetimes, which the reader-inventory source scanner's
/// naive `'`-as-string-literal tokenizer would otherwise mis-pair across the module.)
async fn take_write_lock(conn: &mut sqlx::PgConnection) -> Result<(), ApiError> {
    sqlx::query("SELECT pg_advisory_xact_lock($1)")
        .bind(ARM_WRITE_PLANE_LOCK)
        .execute(&mut *conn)
        .await?;
    Ok(())
}

/// The current resolved representation of an id, read ONCE before applying a write delta.
enum Current {
    /// A present `source ∈ {user,drift}` overlay row wins wholesale — its revision derives
    /// the current `o-<revision>` ETag and its full body is the PATCH merge base (Pitfall 2).
    Overlay { revision: i64, body: Value },
    /// No overlay row shadows the id, but a baseline `synthetic.resources` row is LIVE — the
    /// projected DTO derives the current `b-<hash>` ETag and is the PATCH merge base.
    Baseline { resource: Resource },
    /// No live representation, but a TOMBSTONE row holds this identity: absent for existence
    /// and precondition semantics (no current ETag; PATCH → 404; PUT creates → 201), yet its
    /// frozen stored `id` stays the casing authority when a PUT resurrects it (D-25: liveness
    /// and identity are modeled separately).
    Tombstoned { id: String },
    /// No representation at all (never existed) — no current ETag, the route casing is new.
    Absent,
}

/// Read the current resolved state of `id` ONCE: a present overlay row (revision + full
/// body), else the baseline resolved-view row, else a tombstone's frozen id, else absent. Reads only
/// `arm_overlay` / `arm_resolved_resources` (never raw `synthetic.resources`). The id is
/// BOUND as `$1`, never spliced.
async fn read_current(conn: &mut sqlx::PgConnection, id: &str) -> Result<Current, ApiError> {
    let overlay: Option<(i64, sqlx::types::Json<Value>)> = sqlx::query_as(
        "SELECT revision, body FROM synthetic.arm_overlay \
         WHERE id_lower = synthetic.arm_id_key($1) AND target_kind = 'resource' \
           AND present = true",
    )
    .bind(id)
    .fetch_optional(&mut *conn)
    .await?;
    if let Some((revision, body)) = overlay {
        return Ok(Current::Overlay {
            revision,
            body: body.0,
        });
    }
    let row = sqlx::query_as::<_, ResourceRow>(
        r#"SELECT id, name, type, location, tags, sku, kind, properties
           FROM synthetic.arm_resolved_resources
           WHERE synthetic.arm_id_key(id) = synthetic.arm_id_key($1)
           LIMIT 1"#,
    )
    .bind(id)
    .fetch_optional(&mut *conn)
    .await?;
    if let Some(r) = row {
        return Ok(Current::Baseline {
            resource: Resource::from(r),
        });
    }
    // Not live: a tombstone still carries the identity's frozen stored casing (D-25).
    let tombstone: Option<String> = sqlx::query_scalar(
        "SELECT id FROM synthetic.arm_overlay          WHERE id_lower = synthetic.arm_id_key($1) AND target_kind = 'resource'            AND present = false",
    )
    .bind(id)
    .fetch_optional(&mut *conn)
    .await?;
    Ok(match tombstone {
        Some(id) => Current::Tombstoned { id },
        None => Current::Absent,
    })
}

/// The current resolved ETag: `o-<revision>` for an overlay row, `b-<hash>` for a baseline
/// row, `None` when absent/tombstoned (D-06/D-07 — the token `evaluate_precondition` compares
/// `If-Match` against, and `None` is what makes an `If-Match` on an absent id a `412`, D-15).
fn current_etag(current: &Current) -> Option<String> {
    match current {
        Current::Overlay { revision, .. } => Some(overlay_etag(*revision)),
        Current::Baseline { resource } => Some(baseline_resource_etag(resource)),
        Current::Tombstoned { .. } | Current::Absent => None,
    }
}

/// Select the canonical server-owned `id`/`name`/`type` casing to force into the echo + the
/// stored body (D-08/D-19): an EXISTING overlay row keeps its first-write casing; an EXISTING
/// baseline row keeps the BASELINE casing; a TOMBSTONED identity keeps its frozen stored id
/// (and the name segment(s) that id carries) on resurrection (D-25); a genuinely NEW resource
/// takes the request-URL casing. The URL casing NEVER rewrites a stored canonical identity.
/// The `type` stays the route type for a tombstone (D-25a: the `type` field is a separate
/// surface from the id string).
fn canonical_identity(
    current: &Current,
    route_id: &str,
    route_name: &str,
    route_type: &str,
) -> (String, String, String) {
    let str_or = |body: &Value, key: &str, fallback: &str| -> String {
        body.get(key)
            .and_then(|v| v.as_str())
            .unwrap_or(fallback)
            .to_string()
    };
    match current {
        Current::Overlay { body, .. } => (
            str_or(body, "id", route_id),
            str_or(body, "name", route_name),
            str_or(body, "type", route_type),
        ),
        Current::Baseline { resource } => (
            resource.id.clone(),
            resource.name.clone(),
            resource.r#type.clone(),
        ),
        Current::Tombstoned { id } => (
            id.clone(),
            name_of_id(id).unwrap_or_else(|| route_name.to_string()),
            route_type.to_string(),
        ),
        Current::Absent => (
            route_id.to_string(),
            route_name.to_string(),
            route_type.to_string(),
        ),
    }
}

/// The resource `name` carried by a full resource id: the provider tail after the first
/// `providers` segment, parsed exactly like a route tail (`s1/d1` for a nested id). `None`
/// when the id has no parseable provider tail.
fn name_of_id(id: &str) -> Option<String> {
    let segments: Vec<&str> = id.split('/').collect();
    let at = segments
        .iter()
        .position(|s| s.eq_ignore_ascii_case("providers"))?;
    parse_type_and_name(&segments[at + 1..].join("/")).map(|(_, name)| name)
}

/// Project a baseline `Resource` DTO back into a full ARM body `Value` — the PATCH merge base
/// when no overlay row exists yet. `sku`/`kind` are OMITTED when `None` (never a stored JSON
/// null), mirroring the served DTO's `skip_serializing_if`.
fn resource_to_body(r: &Resource) -> Value {
    let mut map = serde_json::Map::new();
    map.insert("id".to_string(), json!(r.id));
    map.insert("name".to_string(), json!(r.name));
    map.insert("type".to_string(), json!(r.r#type));
    map.insert("location".to_string(), json!(r.location));
    map.insert("tags".to_string(), r.tags.clone());
    if let Some(sku) = &r.sku {
        map.insert("sku".to_string(), sku.clone());
    }
    if let Some(kind) = &r.kind {
        map.insert("kind".to_string(), json!(kind));
    }
    map.insert("properties".to_string(), r.properties.clone());
    Value::Object(map)
}

/// Parse the provider-onward `{*tail}` into the canonical ARM `type` + `name` (D-08 nested
/// parsing to any depth). `Microsoft.Storage/storageAccounts/acct` → (`Microsoft.Storage/
/// storageAccounts`, `acct`); `Microsoft.Sql/servers/s1/databases/d1` → (`Microsoft.Sql/
/// servers/databases`, `s1/d1`). A tail that is not `namespace` + `type/name` pairs (an
/// odd trailing segment / a bare provider path with no name) is a malformed write id → `None`.
///
/// An EMPTY segment (a doubled `//`, or a leading/trailing slash) is malformed and rejected
/// (`None`) — never silently normalized away, so two distinct request paths can never collapse
/// onto the same stored ARM id.
fn parse_type_and_name(tail: &str) -> Option<(String, String)> {
    let segments: Vec<&str> = tail.split('/').collect();
    if segments.iter().any(|s| s.is_empty()) {
        return None;
    }
    let (namespace, rest) = segments.split_first()?;
    if rest.is_empty() || rest.len() % 2 != 0 {
        return None;
    }
    let mut type_parts = vec![(*namespace).to_string()];
    let mut name_parts = Vec::new();
    // `rest.len()` is even (guarded above), so every `chunks(2)` slice is a full type/name
    // pair. (`chunks`, not `chunks_exact` — the latter draws a newer clippy lint under CI's
    // `-D warnings` toolchain, and the even-length guard makes the two equivalent here.)
    for pair in rest.chunks(2) {
        type_parts.push(pair[0].to_string());
        name_parts.push(pair[1].to_string());
    }
    Some((type_parts.join("/"), name_parts.join("/")))
}

/// D-21 structural envelope-type validation + D-14 explicit-`null` location, BEFORE any DB
/// write. A present top-level envelope field with the wrong JSON type for the `sql/009`
/// CHECKs → `400 InvalidRequestContent`; an explicit `location: null` → `400 LocationRequired`.
/// Arbitrary nested content + unknown top-level keys (identity/zones/plan/…) are UNRESTRICTED.
fn validate_envelope(body: &Value) -> Result<(), ApiError> {
    let map = body
        .as_object()
        .ok_or_else(|| ApiError::bad_request("InvalidRequestContent"))?;
    let bad = || ApiError::bad_request("InvalidRequestContent");
    for key in ["id", "name", "type", "kind"] {
        if let Some(v) = map.get(key)
            && !v.is_string()
        {
            return Err(bad());
        }
    }
    for key in ["properties", "tags", "sku"] {
        if let Some(v) = map.get(key)
            && !v.is_object()
        {
            return Err(bad());
        }
    }
    if let Some(v) = map.get("location") {
        // D-14: an explicit `location: null` is a controlled 400 LocationRequired (the
        // sql/009 CHECK requires a non-null string location); any non-null non-string is a
        // generic type mismatch.
        if v.is_null() {
            return Err(ApiError::bad_request("LocationRequired"));
        }
        if !v.is_string() {
            return Err(bad());
        }
    }
    Ok(())
}

/// D-03 identity conflict: a body `id`/`name`/`type` that names a DIFFERENT resource than
/// the URL route (case-insensitively) → `400`. Absent or case-insensitively-matching
/// identity fields are accepted (`provisioningState` is exempt — always forced per D-02).
fn check_identity_conflict(
    body: &Value,
    route_id: &str,
    route_name: &str,
    route_type: &str,
) -> Result<(), ApiError> {
    let conflicts = |key: &str, route: &str| -> bool {
        matches!(
            body.get(key).and_then(|v| v.as_str()),
            Some(supplied) if !supplied.eq_ignore_ascii_case(route)
        )
    };
    if conflicts("id", route_id) || conflicts("name", route_name) || conflicts("type", route_type) {
        return Err(ApiError::bad_request("InvalidRequestContent"));
    }
    Ok(())
}

/// Materialize a CHECK-satisfying body from a merged/replaced `Value`: force the canonical
/// server-owned `id`/`name`/`type`, ensure an object `tags` + `properties`, inject a default
/// `location: "global"` when absent (D-14), and force `properties.provisioningState =
/// "Succeeded"` (D-02). `sku`/`kind` are left exactly as merged (already type-validated;
/// never injected, never a stored null). The result satisfies every `sql/009` body CHECK.
fn finalize_body(mut body: Value, id: &str, name: &str, type_str: &str) -> Value {
    let map = body
        .as_object_mut()
        .expect("caller guarantees a JSON object body");
    map.insert("id".to_string(), json!(id));
    map.insert("name".to_string(), json!(name));
    map.insert("type".to_string(), json!(type_str));
    if !map.get("tags").map(Value::is_object).unwrap_or(false) {
        map.insert("tags".to_string(), json!({}));
    }
    if !map.contains_key("location") {
        map.insert("location".to_string(), json!("global"));
    }
    let properties = map.entry("properties").or_insert_with(|| json!({}));
    if !properties.is_object() {
        *properties = json!({});
    }
    properties
        .as_object_mut()
        .expect("properties coerced to an object above")
        .insert("provisioningState".to_string(), json!("Succeeded"));
    body
}

/// Upsert a present `source='user'` copy-on-write overlay snapshot for `canonical_id`,
/// returning the revision the BEFORE trigger assigned. `ON CONFLICT` preserves the stored
/// `id` (`id = synthetic.arm_overlay.id`) so a differently-cased write can never rewrite the
/// canonical casing (D-08/D-19). Every value binds `$N`.
async fn upsert_present(
    conn: &mut sqlx::PgConnection,
    canonical_id: &str,
    body: &Value,
) -> Result<i64, ApiError> {
    let revision: i64 = sqlx::query_scalar(
        r#"INSERT INTO synthetic.arm_overlay (id_lower, id, target_kind, source, present, body)
           VALUES (synthetic.arm_id_key($1), $1, 'resource', 'user', true, $2)
           ON CONFLICT (id_lower) DO UPDATE SET
               id = synthetic.arm_overlay.id,
               target_kind = EXCLUDED.target_kind,
               source = 'user',
               present = EXCLUDED.present,
               body = EXCLUDED.body
           RETURNING revision"#,
    )
    .bind(canonical_id)
    .bind(sqlx::types::Json(body))
    .fetch_one(&mut *conn)
    .await?;
    Ok(revision)
}

/// Upsert a `source='user'` tombstone (`present=false`, `body=NULL`) for `id` INSIDE an open
/// transaction, returning the revision the BEFORE trigger assigned. Used for BOTH the requested
/// target (whose revision is the D-24 `204` ETag) and every cascade descendant, so the whole
/// containment cascade commits (or rolls back) atomically (D-09). `ON CONFLICT` does not touch
/// `id`, so an existing overlay row's first-write casing is preserved; a genuinely new tombstone
/// (e.g. over a baseline descendant) stores the descendant's own canonical casing. Every value
/// binds `$N`; a baseline descendant id arrives from `arm_resolved_resources`, never spliced.
async fn tombstone_in_tx(tx: &mut sqlx::PgConnection, id: &str) -> Result<i64, ApiError> {
    let revision: i64 = sqlx::query_scalar(
        r#"INSERT INTO synthetic.arm_overlay (id_lower, id, target_kind, source, present, body)
           VALUES (synthetic.arm_id_key($1), $1, 'resource', 'user', false, NULL)
           ON CONFLICT (id_lower) DO UPDATE SET
               source = 'user',
               present = false,
               body = NULL
           RETURNING revision"#,
    )
    .bind(id)
    .fetch_one(&mut *tx)
    .await?;
    Ok(revision)
}

/// Gather the strict nested-containment descendant ids a DELETE of `target_id` must cascade
/// (D-09). Candidates come from BOTH sources that can hold a live descendant: the baseline
/// resolved view (`arm_resolved_resources`, which already unions present overlay rows) AND the
/// present `arm_overlay` rows directly (belt-and-braces). A coarse LIKE PREFILTER on the
/// target's identity key followed by `/%` — anchored to a child-segment boundary — narrows
/// the scan; [`descendant_ids`] (segment-parsed) is the AUTHORITATIVE filter that defeats the
/// `s1`/`s10` sibling-lookalike trap. The prefilter must never UNDER-match: ids are not
/// validated, so the key is escaped ([`DESCENDANT_LIKE`]) and a backslash, `%` or `_` in a
/// name matches only itself (an unescaped backslash is LIKE's default escape character and
/// would hide real descendants). Runs on the caller's DELETE transaction (under the
/// write-plane lock) so the
/// gathered set is snapshot-consistent with the tombstone writes. The target id binds as
/// `$1`; nothing is spliced. Scope fence: containment
/// (the id tree) only — NO graph / dependency / RG-container / `managed_by` traversal (Phase 24).
async fn gather_descendants(
    conn: &mut sqlx::PgConnection,
    target_id: &str,
) -> Result<Vec<String>, ApiError> {
    let baseline: Vec<(String,)> = sqlx::query_as(&format!(
        "SELECT id FROM synthetic.arm_resolved_resources \
         WHERE synthetic.arm_id_key(id) {DESCENDANT_LIKE}"
    ))
    .bind(target_id)
    .fetch_all(&mut *conn)
    .await?;
    let overlay: Vec<(String,)> = sqlx::query_as(&format!(
        "SELECT id FROM synthetic.arm_overlay \
         WHERE target_kind = 'resource' AND present = true \
           AND id_lower {DESCENDANT_LIKE}"
    ))
    .bind(target_id)
    .fetch_all(&mut *conn)
    .await?;

    // Dedupe candidates by identity key (a present overlay row also surfaces in the resolved
    // view), preserving each id's stored casing for the tombstone write.
    let candidates =
        crate::write_merge::dedupe_by_key(baseline.into_iter().chain(overlay).map(|(id,)| id));
    // Segment-parsed containment is authoritative (never a raw string prefix).
    Ok(descendant_ids(target_id, &candidates))
}

/// The descendant prefilter predicate (a STATIC fragment; the target id binds as `$1`): LIKE
/// the target's identity key + `/%`, with the key's own LIKE metacharacters escaped so they
/// match literally. `!` is the escape character (so a backslash in an id is ordinary text);
/// `!` itself is escaped first, then `%` and `_`.
const DESCENDANT_LIKE: &str = "LIKE replace(replace(replace(synthetic.arm_id_key($1), \
     '!', '!!'), '%', '!%'), '_', '!_') || '/%' ESCAPE '!'";

/// Read the raw `If-Match` / `If-None-Match` header values (comma-lists / whitespace / weak
/// validators are handled inside `evaluate_precondition`, D-22).
///
/// A header that is ABSENT is `None` → an unconditional write. A header that is PRESENT but
/// not valid visible-ASCII (`to_str` fails) is a malformed conditional request → a controlled
/// ARM `400`, NEVER silently dropped to an unconditional write (a dropped precondition would
/// turn a client's optimistic-concurrency guard into a blind overwrite).
fn conditional_headers(headers: &HeaderMap) -> Result<(Option<&str>, Option<&str>), ApiError> {
    // Elided lifetimes tie the borrowed &str outputs to `headers` with no explicit `'a` — see
    // `take_write_lock` on why this file avoids bare `'` lifetimes.
    let if_match = match headers.get(header::IF_MATCH) {
        None => None,
        Some(v) => Some(
            v.to_str()
                .map_err(|_| ApiError::bad_request("InvalidRequestContent"))?,
        ),
    };
    let if_none_match = match headers.get(header::IF_NONE_MATCH) {
        None => None,
        Some(v) => Some(
            v.to_str()
                .map_err(|_| ApiError::bad_request("InvalidRequestContent"))?,
        ),
    };
    Ok((if_match, if_none_match))
}

/// Build a mutation response: `status` + `Json(body)` + the new `o-<revision>` ETag header
/// (D-06 — every successful mutation carries the new ETag; the token is pure ASCII).
fn mutation_response(status: StatusCode, body: Value, etag: &str) -> Response {
    let mut response = (status, Json(body)).into_response();
    response.headers_mut().insert(
        header::ETAG,
        HeaderValue::from_str(etag).expect("etag token is valid header ASCII"),
    );
    response
}

/// The reconstructed canonical id + `Content-Type`/parse/validation/identity gate shared by
/// PUT and PATCH. Returns the parsed request object + the route-derived `(type, name)`.
fn parse_and_validate(
    tail: &str,
    route_id: &str,
    headers: &HeaderMap,
    body: &Bytes,
) -> Result<(Value, String, String), ApiError> {
    // D-16: require application/json BEFORE parsing (a Bytes body never rejects on media type).
    // The media type is the part before the first `;`, compared EXACTLY (parameters such as
    // `; charset=utf-8` are allowed): a prefix match would wrongly accept sibling types like
    // `application/json-patch+json` or `application/jsonx`.
    let content_type = headers
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    let media_type = content_type
        .split(';')
        .next()
        .unwrap_or("")
        .trim()
        // IDENTITY-ALLOW[protocol: Content-Type media type is case-insensitive, not an ARM id]
        .to_ascii_lowercase();
    if media_type != "application/json" {
        return Err(ApiError::bad_request("InvalidRequestContent"));
    }
    // D-16: a malformed / non-object body is a controlled ARM 400, never axum's default.
    let request: Value =
        serde_json::from_slice(body).map_err(|_| ApiError::bad_request("InvalidRequestContent"))?;
    if !request.is_object() {
        return Err(ApiError::bad_request("InvalidRequestContent"));
    }
    validate_envelope(&request)?;
    let (route_type, route_name) =
        parse_type_and_name(tail).ok_or_else(|| ApiError::bad_request("InvalidRequestContent"))?;
    check_identity_conflict(&request, route_id, &route_name, &route_type)?;
    Ok((request, route_type, route_name))
}

/// `PUT` — create (`201`) or full-replace (`200`) a resource, echoing the server-forced
/// id/name/type + `provisioningState=Succeeded` and the new `o-<revision>` ETag header.
pub async fn put_resource(
    State(state): State<AppState>,
    Path((sub, rg, tail)): Path<(Uuid, String, String)>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Response, ApiError> {
    // D-16: gating is the FIRST statement, before ANY body use.
    if !state.enable_arm_writes {
        return Err(ApiError::method_not_allowed("GET, HEAD"));
    }
    let route_id = format!("/subscriptions/{sub}/resourceGroups/{rg}/providers/{tail}");
    let (request, route_type, route_name) = parse_and_validate(&tail, &route_id, &headers, &body)?;
    let (if_match, if_none_match) = conditional_headers(&headers)?;

    // One serialized transaction: read → precondition → mutate is atomic (D-27).
    let mut tx = state.pool.begin().await?;
    take_write_lock(&mut tx).await?;
    let current = read_current(&mut tx, &route_id).await?;
    let etag = current_etag(&current);
    if evaluate_precondition(if_match, if_none_match, etag.as_deref()) == Precond::Failed {
        return Err(ApiError::PreconditionFailed); // tx dropped → rollback
    }

    let is_create = matches!(current, Current::Absent | Current::Tombstoned { .. });
    let (canon_id, canon_name, canon_type) =
        canonical_identity(&current, &route_id, &route_name, &route_type);

    // PUT = full replacement from the request body (D-01 keeps PATCH separate).
    let new_body = finalize_body(request, &canon_id, &canon_name, &canon_type);
    let revision = upsert_present(&mut tx, &canon_id, &new_body).await?;
    tx.commit().await?;

    let status = if is_create {
        StatusCode::CREATED
    } else {
        StatusCode::OK
    };
    Ok(mutation_response(status, new_body, &overlay_etag(revision)))
}

/// `PATCH` — D-01 two-level merge over the FULL current body, returning `200` + the new ETag.
/// PATCH never creates: an absent / tombstoned id → `404` (D-20).
pub async fn patch_resource(
    State(state): State<AppState>,
    Path((sub, rg, tail)): Path<(Uuid, String, String)>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Response, ApiError> {
    // D-16: gating is the FIRST statement, before ANY body use.
    if !state.enable_arm_writes {
        return Err(ApiError::method_not_allowed("GET, HEAD"));
    }
    let route_id = format!("/subscriptions/{sub}/resourceGroups/{rg}/providers/{tail}");
    let (request, route_type, route_name) = parse_and_validate(&tail, &route_id, &headers, &body)?;
    let (if_match, if_none_match) = conditional_headers(&headers)?;

    // One serialized transaction: read → precondition → merge → mutate is atomic (D-27).
    let mut tx = state.pool.begin().await?;
    take_write_lock(&mut tx).await?;
    let current = read_current(&mut tx, &route_id).await?;
    let etag = current_etag(&current);
    // Preconditions FIRST so an If-Match on an absent id is a 412 (D-15) before the 404.
    if evaluate_precondition(if_match, if_none_match, etag.as_deref()) == Precond::Failed {
        return Err(ApiError::PreconditionFailed); // tx dropped → rollback
    }

    // D-20: PATCH never creates/resurrects — no live resolved representation → 404.
    let mut merged = match &current {
        Current::Overlay { body, .. } => body.clone(),
        Current::Baseline { resource } => resource_to_body(resource),
        Current::Tombstoned { .. } | Current::Absent => {
            return Err(ApiError::NotFound { what: route_id });
        }
    };
    let (canon_id, canon_name, canon_type) =
        canonical_identity(&current, &route_id, &route_name, &route_type);

    // D-01 two-level merge over the FULL current body (Pitfall 2 — never the 8-column view).
    two_level_merge(&mut merged, &request);
    let new_body = finalize_body(merged, &canon_id, &canon_name, &canon_type);
    let revision = upsert_present(&mut tx, &canon_id, &new_body).await?;
    tx.commit().await?;

    Ok(mutation_response(
        StatusCode::OK,
        new_body,
        &overlay_etag(revision),
    ))
}

/// `DELETE` — an atomic nested-**containment** cascade (D-09). Tombstones the requested target
/// AND every resource whose canonical id is a strict nested DESCENDANT of it (by parsed id
/// SEGMENTS — never a raw string prefix, so `servers/s1` can never sweep `servers/s10`), all
/// in ONE transaction (any failure rolls the whole cascade back). The parent `If-Match`
/// precondition is checked BEFORE the transaction opens (D-07/D-09). Each tombstone is a
/// `source='user'` row (D-04). The `204` carries the new `o-<revision>` ETag of the REQUESTED
/// PARENT/target tombstone (D-24) — NOT a descendant's — with NO body. Unconditional when no
/// precondition is supplied (repeated / never-existed deletes each write a fresh advancing
/// tombstone → `204` + ETag; LIFE-03). Scope fence: containment only — NO graph / dependency /
/// RG-container / `managed_by` traversal (Phase 24).
pub async fn delete_resource(
    State(state): State<AppState>,
    Path((sub, rg, tail)): Path<(Uuid, String, String)>,
    headers: HeaderMap,
) -> Result<Response, ApiError> {
    // D-16: gating is the FIRST statement (DELETE carries no body, but the flag still gates).
    if !state.enable_arm_writes {
        return Err(ApiError::method_not_allowed("GET, HEAD"));
    }
    let route_id = format!("/subscriptions/{sub}/resourceGroups/{rg}/providers/{tail}");
    let (if_match, if_none_match) = conditional_headers(&headers)?;

    // ONE serialized transaction (D-27): read → precondition → gather descendants →
    // cascade-tombstone the target AND every descendant, all under the write-plane advisory
    // lock. The descendant set is gathered INSIDE this transaction, so a child inserted
    // concurrently cannot slip through a gather→commit window and orphan itself.
    let mut tx = state.pool.begin().await?;
    take_write_lock(&mut tx).await?;
    let current = read_current(&mut tx, &route_id).await?;
    let etag = current_etag(&current);
    // Parent If-Match FIRST — a stale precondition (D-07/D-15) rolls NOTHING forward (no
    // descendant is tombstoned; D-09 atomicity) because the tx is dropped here.
    if evaluate_precondition(if_match, if_none_match, etag.as_deref()) == Precond::Failed {
        return Err(ApiError::PreconditionFailed); // tx dropped → rollback
    }

    // Preserve the stored/baseline casing on the tombstone when a representation exists.
    let (canon_id, _, _) = canonical_identity(&current, &route_id, "", "");
    // Segment-parsed containment descendants, read INSIDE the txn (the LIKE is a prefilter).
    let descendants = gather_descendants(&mut tx, &canon_id).await?;

    // The PARENT/target upsert's RETURNING revision is the D-24 `204` ETag — a descendant's
    // revision is NOT reported. Any error propagates via `?`, rolling back the whole cascade.
    let parent_revision = tombstone_in_tx(&mut tx, &canon_id).await?;
    for descendant in &descendants {
        tombstone_in_tx(&mut tx, descendant).await?;
    }
    tx.commit().await?;

    // 204, no body, WITH the REQUESTED-PARENT tombstone's new ETag (D-24).
    let mut response = StatusCode::NO_CONTENT.into_response();
    response.headers_mut().insert(
        header::ETAG,
        HeaderValue::from_str(&overlay_etag(parent_revision))
            .expect("etag token is valid header ASCII"),
    );
    Ok(response)
}

/// D-18: an EXPLICIT ARM `405 MethodNotAllowed` handler for write methods on the bare
/// resource-group path (`.../resourceGroups/{rg}`). It ALWAYS `405`s regardless of the
/// `--enable-arm-writes` flag (RG CRUD is Phase 24, D-13) — the controlled ARM envelope +
/// `Allow: GET, HEAD`, never axum's implicit 405.
pub async fn rg_write_method_not_allowed() -> ApiError {
    ApiError::method_not_allowed("GET, HEAD")
}

#[cfg(test)]
mod tests {
    use super::parse_type_and_name;

    #[test]
    fn name_of_id_reads_the_provider_tail_name() {
        use super::name_of_id;
        assert_eq!(
            name_of_id(
                "/subscriptions/s/resourceGroups/RG/providers/Microsoft.Sql/servers/S1/databases/D1"
            ),
            Some("S1/D1".to_string())
        );
        assert_eq!(
            name_of_id(
                "/subscriptions/s/resourceGroups/rg/PROVIDERS/Microsoft.Storage/storageAccounts/Acct"
            ),
            Some("Acct".to_string())
        );
        assert_eq!(name_of_id("/subscriptions/s/resourceGroups/rg"), None);
    }

    #[test]
    fn parses_flat_and_nested_ids() {
        assert_eq!(
            parse_type_and_name("Microsoft.Storage/storageAccounts/acct"),
            Some((
                "Microsoft.Storage/storageAccounts".to_string(),
                "acct".to_string()
            ))
        );
        assert_eq!(
            parse_type_and_name("Microsoft.Sql/servers/s1/databases/d1"),
            Some((
                "Microsoft.Sql/servers/databases".to_string(),
                "s1/d1".to_string()
            ))
        );
    }

    #[test]
    fn rejects_odd_or_bare_tails() {
        // A bare provider path (no name) and an odd trailing segment are malformed.
        assert_eq!(parse_type_and_name("Microsoft.Storage"), None);
        assert_eq!(
            parse_type_and_name("Microsoft.Storage/storageAccounts"),
            None
        );
        assert_eq!(
            parse_type_and_name("Microsoft.Sql/servers/s1/databases"),
            None
        );
    }

    #[test]
    fn rejects_empty_segments_rather_than_normalizing() {
        // A doubled `//`, a leading `/`, or a trailing `/` is malformed — rejected, never
        // silently collapsed onto a well-formed id (two paths must never share one stored id).
        assert_eq!(
            parse_type_and_name("Microsoft.Storage//storageAccounts/acct"),
            None,
            "doubled slash is malformed"
        );
        assert_eq!(
            parse_type_and_name("Microsoft.Storage/storageAccounts/acct/"),
            None,
            "trailing slash is malformed"
        );
        assert_eq!(
            parse_type_and_name("/Microsoft.Storage/storageAccounts/acct"),
            None,
            "leading slash is malformed"
        );
    }
}
