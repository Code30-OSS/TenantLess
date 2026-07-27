//! The `/_control` write-surface sub-router + its constant-time control-token gate
//! (Phase 17, CTRL-05/CTRL-06).
//!
//! This is the interface-defining substrate for the control plane. It mounts on the SAME
//! bearer-exempt `arm.merge(...)` seam as `/_sim`/`/_console` — but is merged ONLY when the
//! server is armed (`AppState.control` is `Some`, D-02), so a default `serve` exposes NO
//! `/_control/*` routes (disarmed ⇒ 404, not 403). Because it is a FRESH `nest("/_control", …)`
//! prefix off the bearer/metrics layers, adding it cannot change ARM response bytes — the
//! `arm_byte_identical` guard stays green (D-17).
//!
//! Unlike `/_sim` (read-only, bearer-exempt) and the ARM routes (any-Bearer), `/_control`
//! carries its OWN [`control_token`] middleware — a DISTINCT auth realm (D-01): a
//! server-configured secret presented in the `X-Control-Token` header, compared in constant
//! time against its SHA-256 digest. The any-Bearer model is far too weak for a
//! subprocess-spawning mutation surface, so `bearer_auth` is deliberately NOT reused.
//!
//! **17-01 scope:** the router skeleton (`/probe`), the constant-time token gate, and
//! (added alongside) the pre-spawn validation contracts every write handler builds on. The
//! generate/analyze/reset/snapshots/jobs handlers land in 17-02/17-04 on this same inner
//! router.

use axum::{
    Json, Router,
    extract::{Path, Request, State},
    http::StatusCode,
    middleware::{Next, from_fn_with_state},
    response::Response,
    routing::{delete, get, post},
};
use serde::Deserialize;
use subtle::ConstantTimeEq;
use tokio::process::Command;
use uuid::Uuid;

use crate::{
    error::ApiError,
    job::{self, ControlPlane, Job, JobKind, JobSnapshot, digest},
    snapshot,
};

/// The UI-SPEC safe-name copy, shared by every name-guarded control op (profiles/snapshots).
const SAFE_NAME_MSG: &str = "Use letters, numbers, dashes or underscores only — no paths.";

/// The `/_control` sub-router. Merged into the top-level router ONLY when armed (see
/// [`crate::build_router`]), WITHOUT the ARM bearer/metrics layers. Registers the token-gated
/// `GET /probe` unlock signal plus a scoped JSON-404 fallback (mirroring `sim_not_found`); the
/// whole inner router sits behind the [`control_token`] layer. Later plans (17-02/17-04) add
/// `generate`/`analyze`/`reset`/`snapshots`/`jobs` routes to this same inner router.
pub fn router(cp: ControlPlane) -> Router {
    let inner = Router::new()
        .route("/probe", get(probe))
        .route("/generate", post(start_generate))
        .route("/analyze", post(start_analyze))
        .route("/reset", post(start_reset))
        .route("/snapshots", get(list_snapshots).post(save_snapshot))
        .route("/snapshots/{name}/restore", post(restore_snapshot))
        .route("/snapshots/{name}", delete(delete_snapshot))
        .route("/jobs/{id}", get(get_job))
        .route("/sources", get(list_sources))
        .route("/profiles", get(list_profiles))
        .layer(from_fn_with_state(cp.clone(), control_token)) // OWN gate, NOT bearer_auth
        .fallback(control_not_found) // scoped to /_control/* — global 404 unchanged
        .with_state(cp);
    Router::new().nest("/_control", inner) // fresh prefix — cannot shadow ARM (D-17)
}

/// Constant-time control-token middleware (D-01, T-17-03). Reads the `X-Control-Token`
/// header (default `""`), SHA-256s it, and compares against the configured `token_digest`
/// with `subtle::ConstantTimeEq::ct_eq` — comparing fixed-width digests so neither the
/// value NOR the length leaks via timing (RESEARCH "Don't Hand-Roll"). On mismatch returns
/// a fixed 401 `InvalidControlToken` (never `Unauthorized`, whose message names the ARM
/// `Authorization` header). The token is NEVER logged (T-17-05).
pub async fn control_token(
    State(cp): State<ControlPlane>,
    request: Request,
    next: Next,
) -> Result<Response, ApiError> {
    let presented = request
        .headers()
        .get("x-control-token")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    let ok: bool = digest(presented).ct_eq(&cp.token_digest).into();
    if !ok {
        return Err(ApiError::control_unauthorized());
    }
    Ok(next.run(request).await)
}

/// The UI unlock signal: a token-gated `200 { "armed": true }` confirming the presented
/// control token is valid. The frontend probes this to switch from the "Armed, no token"
/// gate to the full control forms (UI-SPEC).
async fn probe() -> Json<serde_json::Value> {
    Json(serde_json::json!({ "armed": true }))
}

/// Unknown `/_control` route → the ARM CloudError JSON shape, not a bare/HTML 404 (mirrors
/// `sim_not_found`). Scoped to the `/_control` nest ONLY, so the global 404 stays identical.
async fn control_not_found() -> ApiError {
    ApiError::NotFound {
        what: "the requested /_control resource".to_string(),
    }
}

// ---------------------------------------------------------------------------
// Pre-spawn validation contracts (D-03) — pure, fixed-`ApiError`-400 strictness.
// Every input is validated BEFORE any subprocess is spawned; bad input returns a fixed
// 400 and never reaches `uv run` (17-02). Placed here as the shared request contract the
// generate/analyze handlers (17-02) build on.
// ---------------------------------------------------------------------------

/// Bundled profile names always allowed (mirrors `profile_input.py::_BUNDLED_NAMES`).
const BUNDLED_PROFILES: [&str; 2] = ["enterprise", "small"];

/// Verbatim port of `profile_input.py::_is_bare_stem` (reject empty, `.`, `..`, any `/`,
/// `\`, `:`, or `..` segment), further restricted to `[A-Za-z0-9_-]` per the UI-SPEC copy
/// ("Use letters, numbers, dashes or underscores only — no paths."). This is the single
/// traversal guard for profile / source / snapshot names — no arbitrary paths reach a
/// server-owned dir or an argv (T-17-01/02).
pub fn is_safe_name(v: &str) -> bool {
    if v.is_empty() || v == "." || v == ".." {
        return false;
    }
    if v.contains('/') || v.contains('\\') || v.contains(':') {
        return false;
    }
    if v.contains("..") {
        return false;
    }
    v.chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
}

/// True for a bundled profile (`enterprise`/`small`) OR a safe-name `<name>.json` present
/// in the server-owned profiles dir (D-03/D-12). Never resolves an arbitrary path.
pub fn profile_allowed(cp: &ControlPlane, name: &str) -> bool {
    if BUNDLED_PROFILES.contains(&name) {
        return true;
    }
    if !is_safe_name(name) {
        return false;
    }
    cp.dirs.profiles.join(format!("{name}.json")).is_file()
}

/// Build the validated `generate` CLI flags (everything appended AFTER the `generate`
/// subcommand) for an already-`validate_generate`-passed request.
///
/// CR-01: the profile arg. A NON-bundled (derived) profile — validated by [`profile_allowed`]
/// to exist as `<control-data>/profiles/<name>.json` — is passed as that RESOLVED PATH, so the
/// Python `resolve_profile` step-1 `Path(..).is_file()` matches. Passing the BARE stem
/// (`derived-acme`) breaks the analyze→generate loop (D-12): the stem is neither a cwd-relative
/// path nor a bundled name, so `resolve_profile` raises `UsageError` and every derived generate
/// fails. A bundled profile (`enterprise`/`small`) still passes as its bare stem (the CLI
/// resolves it from the packaged `tenantless.profiles`).
pub fn generate_argv(args: &GenerateArgs, cp: &ControlPlane) -> Vec<String> {
    let profile_arg = if BUNDLED_PROFILES.contains(&args.profile.as_str()) {
        args.profile.clone()
    } else {
        // Resolve the derived profile to the server-owned artifact path `profile_allowed`
        // already validated exists — the bare stem does not resolve for the Python CLI (CR-01).
        cp.dirs
            .profiles
            .join(format!("{}.json", args.profile))
            .display()
            .to_string()
    };
    let mut argv = vec![
        "--profile".to_string(),
        profile_arg,
        "--seed".to_string(),
        args.seed.to_string(),
        "--resources".to_string(),
        args.resources.to_string(),
        "--subscriptions".to_string(),
        args.subscriptions.to_string(),
        "--jobs".to_string(),
        args.jobs.to_string(),
        "--force".to_string(), // non-TTY truncate guard (cli.py L573-583) — always required
    ];
    // Toggle mapping (D-08): the designed UI controls map to the real CLI flags. The violation
    // slider is profile-driven per-code, so it is treated as on/off (documented, NOT dropped):
    // on ⇒ inject, off ⇒ skip. Over-privilege on ⇒ the profile default rate, off ⇒ 0.0 (clean).
    if args.violations {
        argv.push("--violations".to_string());
    } else {
        argv.push("--no-violations".to_string());
    }
    argv.push("--over-privilege-rate".to_string());
    argv.push(if args.over_privilege { "0.05" } else { "0.0" }.to_string());
    argv
}

/// The shared `generate` request contract (D-08 flag mapping, clamped by the D-03 caps).
/// `seed` is an `i64` so an out-of-range value fails serde parse → 400 automatically, and
/// the numeric caps in [`validate_generate`] reject out-of-range scale BEFORE any spawn.
#[derive(Debug, Clone, Deserialize)]
pub struct GenerateArgs {
    pub profile: String,
    pub seed: i64,
    pub resources: i64,
    pub subscriptions: i64,
    pub jobs: i64,
    #[serde(default)]
    pub violations: bool,
    #[serde(default)]
    pub over_privilege: bool,
}

/// Pre-spawn cap + allowlist validation → a fixed `ApiError` 400 BEFORE any subprocess is
/// created (D-03). Copy strings match the UI-SPEC Copywriting Contract so the field-level
/// and server-level messages agree. Returns `Ok(())` when every input is within its cap and
/// the profile is allowed; the caller (17-02) only then spawns the job.
pub fn validate_generate(a: &GenerateArgs, cp: &ControlPlane) -> Result<(), ApiError> {
    let cores = std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(1) as i64;
    if !(1..=500_000).contains(&a.resources) {
        return Err(ApiError::bad_request(
            "Target resources must be between 1 and 500,000.",
        ));
    }
    if !(1..=5_000).contains(&a.subscriptions) {
        return Err(ApiError::bad_request(
            "Subscriptions must be between 1 and 5,000.",
        ));
    }
    if !(1..=cores).contains(&a.jobs) {
        return Err(ApiError::bad_request(format!(
            "Parallelism must be between 1 and {cores} (available cores)."
        )));
    }
    if !profile_allowed(cp, &a.profile) {
        return Err(ApiError::bad_request("Unknown profile."));
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Job handlers (Plan 17-02, Task 2) — validate → acquire the single-writer permit →
// insert a Queued job → spawn the runner → 202 {job_id}. The runner (job::run_command)
// owns stream drain + finalize; the permit moves into it and releases on completion.
// ---------------------------------------------------------------------------

/// Build the pipeline `Command` for `subcommand`: `<pipeline_cmd> <subcommand>`, with the
/// server's `DATABASE_URL` passed via env (NOT argv — `generate` has no `--database-url`
/// flag and the DSN is a secret kept out of the process list, T-17-05/T-07-02) and the child
/// running from the repo root. The caller appends the validated flags.
pub fn pipeline_command(cp: &ControlPlane, subcommand: &str) -> Command {
    let mut cmd = Command::new(&cp.pipeline_cmd[0]);
    cmd.args(&cp.pipeline_cmd[1..]);
    cmd.arg(subcommand);
    cmd.env("DATABASE_URL", &cp.database_url)
        .current_dir(&cp.repo_root);
    job::scrub_child_env(&mut cmd); // never inherit the control token (WR-03)
    cmd
}

/// Insert a fresh `Queued` job of `kind` into the registry and return its id.
fn register_job(cp: &ControlPlane, kind: JobKind) -> Uuid {
    let job = Job::new(kind);
    let id = job.id;
    cp.registry
        .lock()
        .expect("registry mutex not poisoned")
        .insert(id, job);
    id
}

/// `POST /_control/generate` (CTRL-01, D-08). Validates the caps + profile allowlist BEFORE
/// any spawn (D-03 → fixed 400), then acquires the single-writer permit (`try_acquire_owned`
/// → 409 `ControlBusy` on contention, D-11), inserts a `Queued` job, and spawns the runner
/// with the `generate` flag mapping. Returns `202 {job_id}`.
async fn start_generate(
    State(cp): State<ControlPlane>,
    Json(args): Json<GenerateArgs>,
) -> Result<(StatusCode, Json<serde_json::Value>), ApiError> {
    // 1) Validate FIRST — a 400 never contends for the write gate and never spawns (D-03).
    validate_generate(&args, &cp)?;

    // 2) Single-writer gate: Err ⇒ 409, no job inserted, no spawn (D-11 reject-409).
    let permit = cp
        .write_gate
        .clone()
        .try_acquire_owned()
        .map_err(|_| ApiError::busy())?;

    // 3) Register the job, then build the argv (validated names only; DSN via env).
    let id = register_job(&cp, JobKind::Generate);

    let mut cmd = pipeline_command(&cp, "generate");
    cmd.args(generate_argv(&args, &cp));

    tokio::spawn(job::run_command(cp.clone(), id, cmd, permit));
    Ok((
        StatusCode::ACCEPTED,
        Json(serde_json::json!({ "job_id": id })),
    ))
}

/// `POST /_control/reset` (CTRL-03, D-09/D-11). Wipes the active tenant to a blank simulator.
/// Takes NO input (reset has no args), but it is destructive, so it stays behind the control
/// token (D-01) + the single-writer gate: acquire the permit (`try_acquire_owned` → 409
/// `ControlBusy` on contention), insert a `Queued` `Reset` job, and spawn [`job::run_reset`]
/// (a `TRUNCATE synthetic.* RESTART IDENTITY CASCADE` under the held permit). Returns
/// `202 {job_id}`; the ARM read path then serves an empty tenant hot (D-05), no restart.
async fn start_reset(
    State(cp): State<ControlPlane>,
) -> Result<(StatusCode, Json<serde_json::Value>), ApiError> {
    let permit = cp
        .write_gate
        .clone()
        .try_acquire_owned()
        .map_err(|_| ApiError::busy())?;
    let id = register_job(&cp, JobKind::Reset);
    tokio::spawn(job::run_reset(cp.clone(), id, permit));
    Ok((
        StatusCode::ACCEPTED,
        Json(serde_json::json!({ "job_id": id })),
    ))
}

// ---------------------------------------------------------------------------
// Snapshot handlers (Plan 17-04, Task 2) — pg_dump/pg_restore under safe-name + the
// single-writer gate (CTRL-04, D-04/D-05/D-13/D-14). Every name is `is_safe_name`-guarded
// BEFORE any filesystem/subprocess touch (T-17-02); save/restore acquire the write permit
// (409 on contention, D-11); the DSN reaches pg_dump/pg_restore via PG* env, never argv
// (T-17-05). A missing pg_dump/pg_restore binary is a first-class `failed` job (D-13).
// ---------------------------------------------------------------------------

/// The `save snapshot` request contract (D-04): the artifact `name` (safe-name → the
/// server-owned snapshots dir). No arbitrary paths.
#[derive(Debug, Clone, Deserialize)]
pub struct SnapshotArgs {
    pub name: String,
}

/// `GET /_control/snapshots` (CTRL-04). The saved snapshots (safe-name `*.dump` stems +
/// mtime) as `{ snapshots: [{name, createdUnix}] }` — the tenants/snapshots manager list.
async fn list_snapshots(State(cp): State<ControlPlane>) -> Json<serde_json::Value> {
    let snapshots = snapshot::list(&cp);
    Json(serde_json::json!({ "snapshots": snapshots }))
}

/// `POST /_control/snapshots` (CTRL-04, D-13/D-14). Safe-name-validates the name BEFORE any
/// spawn (fixed 400), acquires the single-writer permit (409 on contention), inserts a
/// `Queued` `Snapshot` job, and spawns [`snapshot::save`] (pg_dump). Returns `202 {job_id}`.
async fn save_snapshot(
    State(cp): State<ControlPlane>,
    Json(args): Json<SnapshotArgs>,
) -> Result<(StatusCode, Json<serde_json::Value>), ApiError> {
    if !is_safe_name(&args.name) {
        return Err(ApiError::bad_request(SAFE_NAME_MSG));
    }
    let permit = cp
        .write_gate
        .clone()
        .try_acquire_owned()
        .map_err(|_| ApiError::busy())?;
    let id = register_job(&cp, JobKind::Snapshot);
    tokio::spawn(snapshot::save(cp.clone(), id, args.name, permit));
    Ok((
        StatusCode::ACCEPTED,
        Json(serde_json::json!({ "job_id": id })),
    ))
}

/// `POST /_control/snapshots/{name}/restore` (CTRL-04, D-05). Safe-name-validates (fixed 400)
/// and 404s an unknown snapshot BEFORE acquiring the permit, then acquires the single-writer
/// permit (409 on contention), inserts a `Queued` `Restore` job, and spawns [`snapshot::restore`]
/// (TRUNCATE + pg_restore under the permit — hot-swap live, D-05). Returns `202 {job_id}`.
async fn restore_snapshot(
    State(cp): State<ControlPlane>,
    Path(name): Path<String>,
) -> Result<(StatusCode, Json<serde_json::Value>), ApiError> {
    if !is_safe_name(&name) {
        return Err(ApiError::bad_request(SAFE_NAME_MSG));
    }
    // 404 the unknown snapshot up front (a clearer signal than a `failed` job for a typo).
    if !cp.dirs.snapshots.join(format!("{name}.dump")).is_file() {
        return Err(ApiError::NotFound {
            what: format!("snapshot '{name}'"),
        });
    }
    let permit = cp
        .write_gate
        .clone()
        .try_acquire_owned()
        .map_err(|_| ApiError::busy())?;
    let id = register_job(&cp, JobKind::Restore);
    tokio::spawn(snapshot::restore(cp.clone(), id, name, permit));
    Ok((
        StatusCode::ACCEPTED,
        Json(serde_json::json!({ "job_id": id })),
    ))
}

/// `DELETE /_control/snapshots/{name}` (CTRL-04, D-11). Safe-name-validates (fixed 400), then
/// acquires the single-writer permit (409 on contention) and removes the server-owned artifact
/// (`204` on success) UNDER that permit; a missing artifact maps to a `404`. Delete takes the
/// write gate — even though it is not itself a DB write — because a concurrent `restore`/`save`
/// depends on the `.dump` staying present between its `is_file` check and `pg_restore`/`pg_dump`.
/// Removing the artifact mid-restore (AFTER the truncate) would leave the tenant empty/dirty
/// (TOCTOU race, 17-UAT Run 2 P1). Holding `_permit` across [`snapshot::delete`] serializes the
/// removal with any in-flight restore/save; it drops at end of handler scope.
async fn delete_snapshot(
    State(cp): State<ControlPlane>,
    Path(name): Path<String>,
) -> Result<StatusCode, ApiError> {
    if !is_safe_name(&name) {
        return Err(ApiError::bad_request(SAFE_NAME_MSG));
    }
    // Serialize with restore/save/generate/reset: a concurrent restore reads this artifact
    // between its is_file check and pg_restore — deleting it there leaves the tenant dirty.
    let _permit = cp
        .write_gate
        .clone()
        .try_acquire_owned()
        .map_err(|_| ApiError::busy())?;
    match snapshot::delete(&cp, &name) {
        Ok(()) => Ok(StatusCode::NO_CONTENT),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Err(ApiError::NotFound {
            what: format!("snapshot '{name}'"),
        }),
        Err(e) => Err(ApiError::Internal(e.to_string())),
    }
}

/// The `analyze` request contract (D-12): a `source` DuckDB stem from the server-owned sources
/// allowlist and an `out_name` for the derived profile (safe-name → the profiles dir). No
/// arbitrary paths, no `azure:` source, no upload.
#[derive(Debug, Clone, Deserialize)]
pub struct AnalyzeArgs {
    pub source: String,
    pub out_name: String,
}

/// `POST /_control/analyze` (CTRL-01, D-12). Rejects a `source` not in the sources allowlist
/// and a non-safe / duplicate `out_name` with a fixed 400 BEFORE any spawn, then acquires the
/// single-writer permit and spawns `analyze --source duckdb:<sources>/<source>.duckdb --out
/// <profiles>/<out_name>.json --non-interactive`. Returns `202 {job_id}`.
async fn start_analyze(
    State(cp): State<ControlPlane>,
    Json(args): Json<AnalyzeArgs>,
) -> Result<(StatusCode, Json<serde_json::Value>), ApiError> {
    // Source: safe-name AND present in the server-owned sources dir (no arbitrary paths).
    if !is_safe_name(&args.source) {
        return Err(ApiError::bad_request("Unknown source."));
    }
    let source_path = cp.dirs.sources.join(format!("{}.duckdb", args.source));
    if !source_path.is_file() {
        return Err(ApiError::bad_request("Unknown source."));
    }
    // Output name: safe-name, and not already an existing profile (no overwrite).
    if !is_safe_name(&args.out_name) {
        return Err(ApiError::bad_request(
            "Use letters, numbers, dashes or underscores only — no paths.",
        ));
    }
    let out_path = cp.dirs.profiles.join(format!("{}.json", args.out_name));
    if out_path.exists() {
        return Err(ApiError::bad_request(
            "A profile with that name already exists.",
        ));
    }

    let permit = cp
        .write_gate
        .clone()
        .try_acquire_owned()
        .map_err(|_| ApiError::busy())?;
    let id = register_job(&cp, JobKind::Analyze);

    let mut cmd = pipeline_command(&cp, "analyze");
    cmd.args([
        "--source",
        &format!("duckdb:{}", source_path.display()),
        "--out",
        &out_path.display().to_string(),
        "--non-interactive",
    ]);

    tokio::spawn(job::run_command(cp.clone(), id, cmd, permit));
    Ok((
        StatusCode::ACCEPTED,
        Json(serde_json::json!({ "job_id": id })),
    ))
}

/// `GET /_control/jobs/{id}` (CTRL-02, D-07). Locks the registry, clones a `JobSnapshot`
/// (status + phase + bounded log tail + result), drops the lock, and returns it. Unknown id
/// → 404 in the shared ApiError envelope.
async fn get_job(
    State(cp): State<ControlPlane>,
    Path(id): Path<Uuid>,
) -> Result<Json<JobSnapshot>, ApiError> {
    let reg = cp.registry.lock().expect("registry mutex not poisoned");
    let job = reg.get(&id).ok_or_else(|| ApiError::NotFound {
        what: format!("job {id}"),
    })?;
    Ok(Json(job.snapshot()))
}

// ---------------------------------------------------------------------------
// Read-only enumeration (Plan 17-02, Task 3) — token-gated, safe-name-filtered lists that
// feed the AnalyzeForm SOURCE and GenerateForm PROFILE selects (17-03). Both are pure reads
// of a server-owned dir: no mutation, no subprocess. Only bare stems that pass `is_safe_name`
// (never full paths) with the required extension cross the boundary; traversal / unsafe names
// are filtered BEFORE listing (T-17-02b, `is_safe_name` authoritative).
// ---------------------------------------------------------------------------

/// One `{ name }` entry (a bare, safe-name stem — never a path).
#[derive(serde::Serialize)]
struct NamedEntry {
    name: String,
}

/// Enumerate safe-name stems in `dir` whose extension (case-insensitively) equals `ext`.
/// Missing dir / non-file entries / unsafe names / other extensions are skipped. Returns a
/// sorted, de-duplicated stem list.
fn safe_stems(dir: &std::path::Path, ext: &str) -> Vec<String> {
    let mut names: Vec<String> = Vec::new();
    let Ok(entries) = std::fs::read_dir(dir) else {
        return names;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if !path.is_file() {
            continue;
        }
        let has_ext = path
            .extension()
            .and_then(|e| e.to_str())
            .is_some_and(|e| e.eq_ignore_ascii_case(ext));
        if !has_ext {
            continue;
        }
        let Some(stem) = path.file_stem().and_then(|s| s.to_str()) else {
            continue;
        };
        if is_safe_name(stem) {
            names.push(stem.to_string());
        }
    }
    names.sort();
    names.dedup();
    names
}

/// `GET /_control/sources` (CTRL-01, D-12). Returns the safe-name `*.duckdb` stems in the
/// server-owned sources dir as `{ sources: [{name}] }` — the analyze SOURCE picker. Names are
/// bare stems (never paths); unsafe / non-`.duckdb` entries are excluded.
async fn list_sources(State(cp): State<ControlPlane>) -> Json<serde_json::Value> {
    let sources: Vec<NamedEntry> = safe_stems(&cp.dirs.sources, "duckdb")
        .into_iter()
        .map(|name| NamedEntry { name })
        .collect();
    Json(serde_json::json!({ "sources": sources }))
}

/// `GET /_control/profiles` (CTRL-01, D-03/D-12). Returns bundled `enterprise`/`small` plus the
/// safe-name `*.json` stems in the server-owned profiles dir (the D-12 derived-profile loop) as
/// `{ profiles: [{name}] }` — the generate PROFILE picker. De-duplicated; unsafe / non-`.json`
/// entries excluded.
async fn list_profiles(State(cp): State<ControlPlane>) -> Json<serde_json::Value> {
    let mut names: Vec<String> = BUNDLED_PROFILES.iter().map(|s| s.to_string()).collect();
    names.extend(safe_stems(&cp.dirs.profiles, "json"));
    // De-dup while preserving the bundled-first order (a derived profile named after a bundled
    // one collapses to one entry).
    let mut seen = std::collections::HashSet::new();
    let profiles: Vec<NamedEntry> = names
        .into_iter()
        .filter(|n| seen.insert(n.clone()))
        .map(|name| NamedEntry { name })
        .collect();
    Json(serde_json::json!({ "profiles": profiles }))
}
