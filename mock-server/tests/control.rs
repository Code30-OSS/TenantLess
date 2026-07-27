//! Control-plane integration suite (Phase 17, CTRL-05/CTRL-06).
//!
//! Covers the interface-defining contracts every later Phase-17 plan builds on:
//!   * fail-closed arming (`arm_decision`) — DB-free unit assertions;
//!   * the lowercase `JobStatus` wire contract the frontend keys on (D-17);
//!   * empty-tenant read tolerance (D-09) — an initialized-but-empty `synthetic`
//!     schema serves `{value:[]}` instead of crashing;
//!   * disarmed posture (D-02/D-06) — with `control: None` the `/_control/*` routes
//!     are absent (`/_control/probe` → 404), the default read-only surface;
//!   * the constant-time control-token gate (D-01, added in Task 2);
//!   * safe-name + pre-spawn validation contracts (D-03, added in Task 3).
//!
//! Like every server integration test, this spins an EPHEMERAL testcontainers
//! Postgres (never the `:5433` dev DB — pytest/generator truncation hazard).

mod common;

use axum::Router;
use axum::body::Body;
use axum::http::{Request, StatusCode};
use sqlx::PgPool;
use tenantless_server::{
    build_router,
    control::{GenerateArgs, is_safe_name, validate_generate},
    error::ApiError,
    job::{self, JobStatus},
    metrics::Metrics,
    state::AppState,
};
use testcontainers_modules::{postgres, testcontainers::runners::AsyncRunner};
use tower::ServiceExt; // for `oneshot`

/// The control secret used by the armed test routers.
const TEST_TOKEN: &str = "s3cr3t-control-token";

/// Start an ephemeral Postgres container and return a connected pool plus the
/// container guard (kept alive for the test's duration). Mirrors `tests/sim.rs`.
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

/// A DISARMED `AppState` (`control: None`) — the default read-only posture (D-02).
fn disarmed_state(pool: &PgPool) -> AppState {
    AppState {
        pool: pool.clone(),
        base_url: "http://test".to_string(),
        metrics: Metrics::new(),
        signer: common::test_signer(),
        enforce_auth: false,
        control: None,
    }
}

/// An ARMED `AppState` (`control: Some`) with `token` as the control secret — arms the
/// `/_control/*` surface behind the constant-time token gate.
fn armed_state(pool: &PgPool, token: &str) -> AppState {
    AppState {
        pool: pool.clone(),
        base_url: "http://test".to_string(),
        metrics: Metrics::new(),
        signer: common::test_signer(),
        enforce_auth: false,
        control: Some(common::armed_control_plane(pool, token)),
    }
}

/// Drive a request carrying an optional `X-Control-Token` header (the shared
/// `common::request` only sets `Authorization: Bearer`). Returns status + parsed JSON.
async fn control_request(
    app: Router,
    method: &str,
    uri: &str,
    control_token: Option<&str>,
) -> (StatusCode, serde_json::Value) {
    let mut builder = Request::builder().method(method).uri(uri);
    if let Some(t) = control_token {
        builder = builder.header("x-control-token", t);
    }
    let req = builder.body(Body::empty()).expect("build request");
    let resp = app.oneshot(req).await.expect("oneshot");
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .expect("collect body");
    let json: serde_json::Value = if bytes.is_empty() {
        serde_json::Value::Null
    } else {
        serde_json::from_slice(&bytes).expect("parse json body")
    };
    (status, json)
}

// ---------------------------------------------------------------------------
// Task 1 — arming + AppState.control + empty-tenant startup + JobStatus wire
// ---------------------------------------------------------------------------

/// D-09: an initialized-but-empty `synthetic` schema (schema provisioned, ZERO tenant
/// rows) builds a router WITHOUT panicking, and an ARM list returns `200 {value:[]}`.
#[tokio::test]
async fn startup_tolerates_empty_tenant() {
    let (pool, _pg) = start_pg().await;
    common::seed_empty_tenant(&pool).await; // schema present, NO tenant row
    let app = build_router(disarmed_state(&pool));
    let (status, body) = common::request(app, "GET", "/subscriptions", Some("t")).await;
    assert_eq!(
        status,
        StatusCode::OK,
        "empty tenant lists 200, not a startup crash"
    );
    assert_eq!(
        body["value"],
        serde_json::json!([]),
        "empty tenant yields an empty ARM collection envelope"
    );
}

/// D-02/D-06: with `control: None` the `/_control/*` routes are NOT merged, so
/// `GET /_control/probe` is a 404 (routes absent, not 403) — the default posture.
#[tokio::test]
async fn disarmed_router_has_no_control() {
    let (pool, _pg) = start_pg().await;
    common::seed_fixture(&pool).await;
    let app = build_router(disarmed_state(&pool));
    let (status, _body) = common::request(app, "GET", "/_control/probe", Some("t")).await;
    assert_eq!(
        status,
        StatusCode::NOT_FOUND,
        "disarmed ⇒ /_control absent (404)"
    );
}

/// D-02: fail-closed arming. Disabled → `Ok(None)`; enabled + missing/empty/whitespace
/// token → `Err` (naming the flags); enabled + non-empty → `Ok(Some(digest))`.
#[test]
fn arming_requires_nonempty_token() {
    // Disabled → Ok(None) regardless of any token value.
    assert!(matches!(job::arm_decision(false, None), Ok(None)));
    assert!(matches!(job::arm_decision(false, Some("secret")), Ok(None)));

    // Enabled + missing/empty/whitespace token → fail closed, message names the flags.
    let err = job::arm_decision(true, None).unwrap_err();
    assert!(
        err.contains("--control-token") && err.contains("TENANTLESS_CONTROL_TOKEN"),
        "fail-closed message must name both the flag and the env var, got: {err}"
    );
    assert!(job::arm_decision(true, Some("")).is_err());
    assert!(job::arm_decision(true, Some("   ")).is_err());

    // Enabled + non-empty → Ok(Some(digest)) matching the SHA-256 of the token.
    let d = job::arm_decision(true, Some("secret")).unwrap().unwrap();
    assert_eq!(d, job::digest("secret"));
}

/// D-17: `JobStatus` serializes lowercase — the exact wire strings the frontend keys on.
/// The serde default would emit `"Queued"`, breaking the key match.
#[test]
fn job_status_serializes_lowercase() {
    assert_eq!(
        serde_json::to_string(&JobStatus::Queued).unwrap(),
        "\"queued\""
    );
    assert_eq!(
        serde_json::to_string(&JobStatus::Running).unwrap(),
        "\"running\""
    );
    assert_eq!(
        serde_json::to_string(&JobStatus::Succeeded).unwrap(),
        "\"succeeded\""
    );
    assert_eq!(
        serde_json::to_string(&JobStatus::Failed).unwrap(),
        "\"failed\""
    );
}

// ---------------------------------------------------------------------------
// Task 2 — constant-time control-token gate + /probe + conditional merge
// ---------------------------------------------------------------------------

/// D-01/CTRL-05: an armed `/_control/probe` rejects a missing/wrong `X-Control-Token`
/// with a 401 `InvalidControlToken` ApiError and accepts the configured token (200).
#[tokio::test]
async fn control_token_gate() {
    let (pool, _pg) = start_pg().await;
    common::seed_fixture(&pool).await;
    let app = build_router(armed_state(&pool, TEST_TOKEN));

    // No token → 401 InvalidControlToken (the ApiError envelope, D-17).
    let (s, body) = control_request(app.clone(), "GET", "/_control/probe", None).await;
    assert_eq!(s, StatusCode::UNAUTHORIZED, "missing token → 401");
    assert_eq!(body["error"]["code"], "InvalidControlToken");
    assert_eq!(body["error"]["message"], "Invalid control token.");

    // Wrong token → 401 (constant-time compare, no length leak).
    let (s, _b) = control_request(app.clone(), "GET", "/_control/probe", Some("wrong")).await;
    assert_eq!(s, StatusCode::UNAUTHORIZED, "wrong token → 401");

    // Correct token → 200 { armed: true } (the UI unlock signal).
    let (s, body) = control_request(app, "GET", "/_control/probe", Some(TEST_TOKEN)).await;
    assert_eq!(s, StatusCode::OK, "correct token → 200");
    assert_eq!(body["armed"], true);
}

/// D-17/CTRL-06: merging `/_control` (armed) must NOT alter ARM bytes. This is the
/// armed companion to `tests/sim.rs::arm_byte_identical` (which runs disarmed): it builds
/// a disarmed and an armed router over the SAME pool+signer, proves `/_control` is genuinely
/// present only when armed (the non-tautological discriminator), and asserts a representative
/// ARM list response is byte-identical across the two.
#[tokio::test]
async fn control_merge_keeps_arm_byte_identical() {
    let (pool, _pg) = start_pg().await;
    common::seed_fixture(&pool).await;
    let signer = common::test_signer();

    let disarmed = AppState {
        pool: pool.clone(),
        base_url: "http://test".to_string(),
        metrics: Metrics::new(),
        signer: signer.clone(),
        enforce_auth: false,
        control: None,
    };
    let armed = AppState {
        pool: pool.clone(),
        base_url: "http://test".to_string(),
        metrics: Metrics::new(),
        signer,
        enforce_auth: false,
        control: Some(common::armed_control_plane(&pool, TEST_TOKEN)),
    };
    let app_disarmed = build_router(disarmed);
    let app_armed = build_router(armed);

    // DISCRIMINATOR: /_control is ABSENT (404) when disarmed and PRESENT (token-gated 401,
    // not 404) when armed — so the byte-identity below is a genuine merge proof.
    let (d_probe, _) = control_request(app_disarmed.clone(), "GET", "/_control/probe", None).await;
    assert_eq!(d_probe, StatusCode::NOT_FOUND, "disarmed: /_control absent");
    let (a_probe, _) = control_request(app_armed.clone(), "GET", "/_control/probe", None).await;
    assert_eq!(
        a_probe,
        StatusCode::UNAUTHORIZED,
        "armed: /_control present (token-gated)"
    );

    // ARM list byte-identical across disarmed vs armed (/_control merged).
    let (s1, b1) = common::request(app_disarmed, "GET", "/subscriptions", Some("t")).await;
    let (s2, b2) = common::request(app_armed, "GET", "/subscriptions", Some("t")).await;
    assert_eq!(s1, StatusCode::OK);
    assert_eq!(s1, s2, "ARM list status identical with /_control merged");
    assert_eq!(
        b1, b2,
        "ARM list body identical with /_control merged (D-17/CTRL-06)"
    );
}

// ---------------------------------------------------------------------------
// Task 3 — pre-spawn validation contracts (safe-name + caps, reject before spawn)
// ---------------------------------------------------------------------------

/// Extract the fixed 400 message from an `ApiError::BadRequest`, panicking on any other arm.
fn bad_request_message(err: ApiError) -> String {
    match err {
        ApiError::BadRequest { message } => message,
        other => panic!("expected BadRequest, got {other:?}"),
    }
}

/// A base-valid `GenerateArgs` (bundled profile, in-cap scale) that each case mutates one field of.
fn valid_generate_args() -> GenerateArgs {
    GenerateArgs {
        profile: "enterprise".to_string(),
        seed: 7,
        resources: 1_000,
        subscriptions: 10,
        jobs: 1,
        violations: true,
        over_privilege: false,
    }
}

/// T-17-01/02 (D-03): the safe-name traversal guard (port of `_is_bare_stem`, restricted to
/// `[A-Za-z0-9_-]`) rejects path syntax / `..` segments and accepts plain stems.
#[test]
fn is_safe_name_rejects_paths() {
    for bad in ["", ".", "..", "a/b", "a\\b", "a:b", "a..b"] {
        assert!(!is_safe_name(bad), "must reject {bad:?}");
    }
    for good in ["enterprise", "small", "my-derived_profile"] {
        assert!(is_safe_name(good), "must accept {good:?}");
    }
}

/// CTRL-05 (D-03): `validate_generate` returns a fixed 400 (UI-SPEC copy) for out-of-cap
/// resources / subscriptions / jobs and an unknown profile — and NO job is inserted into the
/// registry (validation runs strictly BEFORE any spawn seam).
#[tokio::test]
async fn validation_rejects_before_spawn() {
    let (pool, _pg) = start_pg().await;
    let cp = common::armed_control_plane(&pool, TEST_TOKEN);
    let cores = std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(1) as i64;

    // resources over cap.
    let mut a = valid_generate_args();
    a.resources = 600_000;
    let msg = bad_request_message(validate_generate(&a, &cp).unwrap_err());
    assert_eq!(msg, "Target resources must be between 1 and 500,000.");

    // subscriptions over cap.
    let mut a = valid_generate_args();
    a.subscriptions = 6_000;
    let msg = bad_request_message(validate_generate(&a, &cp).unwrap_err());
    assert_eq!(msg, "Subscriptions must be between 1 and 5,000.");

    // jobs over the available-cores cap.
    let mut a = valid_generate_args();
    a.jobs = cores + 1;
    let msg = bad_request_message(validate_generate(&a, &cp).unwrap_err());
    assert!(
        msg.starts_with("Parallelism must be between 1 and") && msg.contains("(available cores)."),
        "unexpected jobs message: {msg}"
    );

    // unknown profile (safe-name but not bundled and not present in the profiles dir).
    let mut a = valid_generate_args();
    a.profile = "nonexistent-profile".to_string();
    let msg = bad_request_message(validate_generate(&a, &cp).unwrap_err());
    assert_eq!(msg, "Unknown profile.");

    // NO spawn seam ran → the registry never gained a job.
    assert_eq!(
        cp.registry.lock().unwrap().len(),
        0,
        "validation must reject BEFORE any job is inserted (no subprocess)"
    );
}

// ---------------------------------------------------------------------------
// Plan 17-02 Task 1 — job.rs tokio subprocess runner (concurrent drain, phase
// labels, opportunistic summary parse, never-500 on a bad child).
// ---------------------------------------------------------------------------

/// D-08: the pure stderr→coarse-phase-label map. Known generator stderr lines map to a
/// coarse label; an unknown line changes no phase (None).
#[test]
fn phase_label_map() {
    assert_eq!(
        job::phase_label("generating tenant..."),
        Some("generating tenant…")
    );
    assert_eq!(
        job::phase_label("fitting distributions..."),
        Some("fitting distributions…")
    );
    assert_eq!(
        job::phase_label("computing tag entropy..."),
        Some("computing tag entropy…")
    );
    assert_eq!(
        job::phase_label("writing to database..."),
        Some("writing to database…")
    );
    // An unknown line → no label change.
    assert_eq!(job::phase_label("some unrelated chatter"), None);
    assert_eq!(job::phase_label(""), None);
}

/// D-08: the opportunistic stdout summary parse. The exact `generate` summary line parses
/// to {tenant_id, subscriptions, resource_groups, resources, violations}; a garbled line → None.
#[test]
fn parse_generate_summary_extracts_counts() {
    let line = "Generated tenant 00000000-0000-0000-0000-000000000000: \
                3 subscriptions, 5 resource groups, 20 resources, 4 violations, \
                5 dependencies, 3 principals, 6 role assignments (2 over-privilege) \
                (seed=7, target_resources=20, jobs=1, elapsed=100ms).";
    let v = job::parse_generate_summary(line).expect("the canonical summary line parses");
    assert_eq!(v["tenant_id"], "00000000-0000-0000-0000-000000000000");
    assert_eq!(v["subscriptions"], 3);
    assert_eq!(v["resource_groups"], 5);
    assert_eq!(v["resources"], 20);
    assert_eq!(v["violations"], 4);

    // A garbled / non-summary line yields None (parse-failure is non-fatal, D-08).
    assert!(job::parse_generate_summary("just some log chatter").is_none());
    assert!(job::parse_generate_summary("Generated tenant abc: incomplete").is_none());
}

/// D-08/D-15: a runner pointed at a NONEXISTENT command finalizes the job as `Failed` with a
/// log line — no panic, no 500. This is a first-class path (missing binary), not an edge case.
#[tokio::test]
async fn run_records_failure_on_missing_binary() {
    let (pool, _pg) = start_pg().await;
    let cp = common::armed_control_plane(&pool, TEST_TOKEN);

    let j = job::Job::new(job::JobKind::Generate);
    let id = j.id;
    cp.registry.lock().unwrap().insert(id, j);

    let permit = cp
        .write_gate
        .clone()
        .try_acquire_owned()
        .expect("write permit is free");
    let cmd = tokio::process::Command::new("tenantless-nonexistent-binary-zzz");

    // Must NOT panic even though the child cannot spawn.
    job::run_command(cp.clone(), id, cmd, permit).await;

    let reg = cp.registry.lock().unwrap();
    let snap = reg.get(&id).expect("job still present");
    assert_eq!(
        snap.status,
        JobStatus::Failed,
        "a missing binary finalizes the job as Failed"
    );
    assert!(
        !snap.log.is_empty(),
        "the spawn failure is captured in the job log"
    );
}

/// P1-B regression (17-UAT): a job that OUTLIVES the wall-clock timeout while keeping a pipe
/// open must NOT deadlock the runner. On the buggy code the drain tasks `join!` before the
/// child is killed, so — with the child still alive holding stdout/stderr open — the two drains
/// never return, `join!` blocks forever, the timeout kill is never reached, the job never
/// finalizes, and the single-writer `_permit` is held permanently. This test drives a SHORT
/// injected timeout against a child that prints a line then sleeps well past it, and asserts
/// the job reaches `Failed` AND the write gate is RELEASED afterward (a second permit acquires).
/// The whole runner call is wrapped in an OUTER timeout so the buggy version fails cleanly
/// (assert) instead of hanging the suite. Skips if no `python` interpreter is available.
#[tokio::test]
async fn run_timeout_releases_write_gate() {
    // Skip cleanly if python is absent (mirrors the project's connect/skip idiom). The armed
    // control-plane test stub already relies on `python`, so CI has it; this guards a bare box.
    if tokio::process::Command::new("python")
        .arg("--version")
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()
        .await
        .is_err()
    {
        eprintln!("skipping run_timeout_releases_write_gate: no `python` interpreter");
        return;
    }

    let (pool, _pg) = start_pg().await;
    let cp = common::armed_control_plane(&pool, TEST_TOKEN);

    let j = job::Job::new(job::JobKind::Generate);
    let id = j.id;
    cp.registry.lock().unwrap().insert(id, j);

    let permit = cp
        .write_gate
        .clone()
        .try_acquire_owned()
        .expect("write permit is free");

    // A child that prints a line (keeping stdout OPEN) then sleeps far beyond the timeout.
    let mut cmd = tokio::process::Command::new("python");
    cmd.arg("-c")
        .arg("import sys,time; print('started'); sys.stdout.flush(); time.sleep(30)");

    let short = std::time::Duration::from_millis(300);

    // On the BUGGY runner this deadlocks in `join!`; the outer timeout bounds the test so RED
    // fails cleanly. After the kill-before-join fix the runner returns promptly.
    let ran = tokio::time::timeout(
        std::time::Duration::from_secs(10),
        job::run_command_with_timeout(cp.clone(), id, cmd, permit, short),
    )
    .await;
    assert!(
        ran.is_ok(),
        "runner deadlocked on job timeout — the single-writer gate would never release"
    );

    // The timed-out job finalized as Failed (never stuck Running).
    {
        let reg = cp.registry.lock().unwrap();
        let snap = reg.get(&id).expect("job still present");
        assert_eq!(
            snap.status,
            JobStatus::Failed,
            "a timed-out job is finalized as Failed"
        );
    }

    // The single-writer gate was RELEASED — a second permit is immediately acquirable.
    let _second = cp
        .write_gate
        .clone()
        .try_acquire_owned()
        .expect("write gate released after timeout — a second permit is available");
}

// ---------------------------------------------------------------------------
// Plan 17-02 Task 2 — generate + analyze handlers (validate → permit → spawn → 202;
// single-writer 409; job poll). Uses the SAME `ControlPlane` instance in the router
// state so the test can inspect the shared registry.
// ---------------------------------------------------------------------------

/// An ARMED `AppState` wrapping the CALLER's `ControlPlane` (so the test and the router
/// share one registry + write-gate + control-data dirs).
fn armed_state_with(pool: &PgPool, cp: tenantless_server::job::ControlPlane) -> AppState {
    AppState {
        pool: pool.clone(),
        base_url: "http://test".to_string(),
        metrics: Metrics::new(),
        signer: common::test_signer(),
        enforce_auth: false,
        control: Some(cp),
    }
}

/// POST a JSON body with an optional `X-Control-Token` header; returns status + parsed JSON.
async fn control_post_json(
    app: Router,
    uri: &str,
    control_token: Option<&str>,
    body: &serde_json::Value,
) -> (StatusCode, serde_json::Value) {
    let mut builder = Request::builder()
        .method("POST")
        .uri(uri)
        .header("content-type", "application/json");
    if let Some(t) = control_token {
        builder = builder.header("x-control-token", t);
    }
    let payload = serde_json::to_vec(body).expect("serialize body");
    let req = builder.body(Body::from(payload)).expect("build request");
    let resp = app.oneshot(req).await.expect("oneshot");
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .expect("collect body");
    let json: serde_json::Value = if bytes.is_empty() {
        serde_json::Value::Null
    } else {
        serde_json::from_slice(&bytes).expect("parse json body")
    };
    (status, json)
}

/// A base-valid generate request body (bundled profile, in-cap scale).
fn generate_body() -> serde_json::Value {
    serde_json::json!({
        "profile": "enterprise",
        "seed": 7,
        "resources": 1000,
        "subscriptions": 10,
        "jobs": 1,
        "violations": true,
        "over_privilege": false
    })
}

/// CTRL-01/D-06: a valid generate POST validates, acquires the permit, inserts ONE job, and
/// returns 202 `{job_id}`; the job is poll-able with a lowercase wire status (D-17).
#[tokio::test]
async fn control_generate_starts_job() {
    let (pool, _pg) = start_pg().await;
    let cp = common::armed_control_plane(&pool, TEST_TOKEN);
    let app = build_router(armed_state_with(&pool, cp.clone()));

    let (status, json) = control_post_json(
        app.clone(),
        "/_control/generate",
        Some(TEST_TOKEN),
        &generate_body(),
    )
    .await;
    assert_eq!(status, StatusCode::ACCEPTED, "in-cap generate → 202");
    let job_id = json["job_id"]
        .as_str()
        .expect("202 body carries a job_id")
        .to_string();
    assert_eq!(
        cp.registry.lock().unwrap().len(),
        1,
        "exactly one job is registered"
    );

    // Poll-able: GET /_control/jobs/{id} → 200 with a LOWERCASE status string (D-17).
    let (s, jb) = control_request(
        app,
        "GET",
        &format!("/_control/jobs/{job_id}"),
        Some(TEST_TOKEN),
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    let st = jb["status"].as_str().expect("status is a string");
    assert!(
        ["queued", "running", "succeeded", "failed"].contains(&st),
        "status is the lowercase wire string, got {st:?}"
    );
    assert_eq!(jb["id"].as_str(), Some(job_id.as_str()));
}

/// CR-01 (D-12): the analyze→generate handoff. `generate_argv` MUST pass a NON-bundled derived
/// profile as the RESOLVED `<profiles>/<name>.json` path (so the Python `resolve_profile` step-1
/// `Path(..).is_file()` matches), NOT the bare stem (which is neither a cwd-relative path nor
/// bundled → `UsageError`, so every derived generate would fail). A bundled profile still passes
/// as its bare stem.
#[tokio::test]
async fn generate_argv_resolves_derived_profile_path() {
    use tenantless_server::control::generate_argv;
    let (pool, _pg) = start_pg().await;
    let cp = common::armed_control_plane(&pool, TEST_TOKEN);
    // A real derived profile the server already validated (as `analyze` would have written it).
    std::fs::write(cp.dirs.profiles.join("derived-acme.json"), b"{}")
        .expect("write derived profile");

    // Non-bundled derived profile → the resolved <profiles>/<name>.json path (CR-01).
    let mut a = valid_generate_args();
    a.profile = "derived-acme".to_string();
    let argv = generate_argv(&a, &cp);
    let pi = argv
        .iter()
        .position(|s| s == "--profile")
        .expect("--profile present in the generate argv");
    let passed = &argv[pi + 1];
    let expected = cp
        .dirs
        .profiles
        .join("derived-acme.json")
        .display()
        .to_string();
    assert_eq!(
        passed, &expected,
        "a derived profile must be passed as the resolved <profiles>/<name>.json path (CR-01)"
    );
    assert_ne!(
        passed, "derived-acme",
        "must NOT pass the bare stem (Python resolve_profile → UsageError, the D-12 loop break)"
    );

    // Bundled profile → still the bare stem (the CLI resolves it from the packaged profiles).
    let mut b = valid_generate_args();
    b.profile = "enterprise".to_string();
    let argv_b = generate_argv(&b, &cp);
    let pib = argv_b
        .iter()
        .position(|s| s == "--profile")
        .expect("--profile present");
    assert_eq!(
        argv_b[pib + 1],
        "enterprise",
        "a bundled profile stays the bare stem"
    );
}

/// WR-03 (T-17-05): the control token MUST NOT be inherited by any spawned child. The server
/// arms via `TENANTLESS_CONTROL_TOKEN` (the recommended env path), which `tokio::process::Command`
/// inherits by default — so every child builder (pipeline generate/analyze + pg_dump/pg_restore)
/// must `env_remove` it. Each builder's env must carry an EXPLICIT removal of the token key.
#[tokio::test]
async fn child_env_omits_control_token() {
    /// The child command explicitly REMOVES the control-token var (an env_remove records it as
    /// `(key, None)` in `get_envs`; a builder that never touches it has no such entry).
    fn scrubs_token(cmd: &tokio::process::Command) -> bool {
        let key = std::ffi::OsStr::new("TENANTLESS_CONTROL_TOKEN");
        cmd.as_std()
            .get_envs()
            .any(|(k, v)| k == key && v.is_none())
    }

    let (pool, _pg) = start_pg().await;
    let cp = common::armed_control_plane(&pool, TEST_TOKEN);

    // The pipeline (generate/analyze) child.
    let pipe = tenantless_server::control::pipeline_command(&cp, "generate");
    assert!(
        scrubs_token(&pipe),
        "the pipeline child must not inherit TENANTLESS_CONTROL_TOKEN (WR-03)"
    );

    // The pg_dump (save) child.
    let dump = tenantless_server::snapshot::dump_command(&cp, "snap1");
    assert!(
        scrubs_token(&dump),
        "the pg_dump child must not inherit TENANTLESS_CONTROL_TOKEN (WR-03)"
    );

    // The pg_restore (restore) child.
    let restore = tenantless_server::snapshot::restore_command(&cp, "snap1");
    assert!(
        scrubs_token(&restore),
        "the pg_restore child must not inherit TENANTLESS_CONTROL_TOKEN (WR-03)"
    );
}

/// CTRL-01/D-11: with the single-writer permit already held (a destructive job "running"), a
/// second POST /_control/generate → 409 `ControlBusy` and NO second job is inserted.
#[tokio::test]
async fn single_writer_409() {
    let (pool, _pg) = start_pg().await;
    let cp = common::armed_control_plane(&pool, TEST_TOKEN);
    // Hold the permit to simulate an in-flight destructive job.
    let _held = cp
        .write_gate
        .clone()
        .try_acquire_owned()
        .expect("first permit is free");
    let app = build_router(armed_state_with(&pool, cp.clone()));

    let (status, json) = control_post_json(
        app,
        "/_control/generate",
        Some(TEST_TOKEN),
        &generate_body(),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT, "second destructive job → 409");
    assert_eq!(json["error"]["code"], "ControlBusy");
    assert_eq!(
        cp.registry.lock().unwrap().len(),
        0,
        "no job is inserted when the write gate is busy"
    );
}

/// CTRL-01/D-12: POST /_control/analyze against an allowlisted DuckDB source → 202; after the
/// job runs, the derived profile lands in the profiles dir and passes `profile_allowed` (so it
/// appears in the generate allowlist).
#[tokio::test]
async fn control_analyze_roundtrip() {
    let (pool, _pg) = start_pg().await;
    let cp = common::armed_control_plane(&pool, TEST_TOKEN);
    // Seed an allowlisted DuckDB source (bytes are irrelevant to the stub runner).
    std::fs::write(cp.dirs.sources.join("acme.duckdb"), b"duckdb-bytes").expect("write source");
    let app = build_router(armed_state_with(&pool, cp.clone()));

    let body = serde_json::json!({ "source": "acme", "out_name": "derived-acme" });
    let (status, json) = control_post_json(app, "/_control/analyze", Some(TEST_TOKEN), &body).await;
    assert_eq!(status, StatusCode::ACCEPTED, "allowlisted analyze → 202");
    assert!(
        json["job_id"].as_str().is_some(),
        "202 body carries a job_id"
    );

    // Poll until the derived profile is written (the analyze job produces it).
    let mut landed = false;
    for _ in 0..100 {
        if tenantless_server::control::profile_allowed(&cp, "derived-acme") {
            landed = true;
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    assert!(
        landed,
        "the analyze job's derived profile appears in the generate allowlist (profile_allowed)"
    );
}

// ---------------------------------------------------------------------------
// Plan 17-02 Task 3 — read-only source + profile enumeration (token-gated,
// safe-name filtered; feed the AnalyzeForm SOURCE / GenerateForm PROFILE selects).
// ---------------------------------------------------------------------------

/// Collect the `name` field of each `{name}` entry under `body[key]`.
fn names_of(body: &serde_json::Value, key: &str) -> Vec<String> {
    body[key]
        .as_array()
        .expect("array field")
        .iter()
        .map(|e| e["name"].as_str().expect("name string").to_string())
        .collect()
}

/// T-17-02b (D-03/D-12): GET /_control/sources returns safe-name `*.duckdb` stems from the
/// server-owned sources dir; unsafe-name and non-`.duckdb` entries are excluded (bare stems,
/// never paths).
#[tokio::test]
async fn list_sources_returns_safe_duckdb() {
    let (pool, _pg) = start_pg().await;
    let cp = common::armed_control_plane(&pool, TEST_TOKEN);
    std::fs::write(cp.dirs.sources.join("foo.duckdb"), b"x").expect("write safe duckdb");
    std::fs::write(cp.dirs.sources.join("a b.duckdb"), b"x").expect("write unsafe-name duckdb");
    std::fs::write(cp.dirs.sources.join("notes.txt"), b"x").expect("write non-duckdb");
    let app = build_router(armed_state_with(&pool, cp.clone()));

    let (s, body) = control_request(app, "GET", "/_control/sources", Some(TEST_TOKEN)).await;
    assert_eq!(s, StatusCode::OK);
    let names = names_of(&body, "sources");
    assert!(
        names.contains(&"foo".to_string()),
        "safe duckdb stem present"
    );
    assert!(
        !names.iter().any(|n| n.contains(' ')),
        "unsafe-name (space) excluded"
    );
    assert!(
        !names.contains(&"notes".to_string()),
        "non-.duckdb excluded"
    );
}

/// T-17-02b (D-03/D-12): GET /_control/profiles lists bundled `enterprise` + `small` plus
/// safe-name `*.json` stems in the profiles dir; unsafe-name and non-`.json` excluded.
#[tokio::test]
async fn list_profiles_includes_bundled() {
    let (pool, _pg) = start_pg().await;
    let cp = common::armed_control_plane(&pool, TEST_TOKEN);
    std::fs::write(cp.dirs.profiles.join("derived.json"), b"{}").expect("write safe profile");
    std::fs::write(cp.dirs.profiles.join("bad name.json"), b"{}").expect("write unsafe profile");
    std::fs::write(cp.dirs.profiles.join("ignore.txt"), b"{}").expect("write non-json");
    let app = build_router(armed_state_with(&pool, cp.clone()));

    let (s, body) = control_request(app, "GET", "/_control/profiles", Some(TEST_TOKEN)).await;
    assert_eq!(s, StatusCode::OK);
    let names = names_of(&body, "profiles");
    assert!(
        names.contains(&"enterprise".to_string()),
        "bundled enterprise"
    );
    assert!(names.contains(&"small".to_string()), "bundled small");
    assert!(
        names.contains(&"derived".to_string()),
        "safe-name derived profile"
    );
    assert!(
        !names.iter().any(|n| n.contains(' ')),
        "unsafe-name (space) excluded"
    );
    assert!(!names.contains(&"ignore".to_string()), "non-.json excluded");
}

/// T-17-02b (CTRL-05): GET /_control/sources with no / a wrong `X-Control-Token` → 401
/// `InvalidControlToken` (same gate + ApiError contract as every control route).
#[tokio::test]
async fn list_sources_token_gated() {
    let (pool, _pg) = start_pg().await;
    let cp = common::armed_control_plane(&pool, TEST_TOKEN);
    let app = build_router(armed_state_with(&pool, cp));

    let (s, body) = control_request(app.clone(), "GET", "/_control/sources", None).await;
    assert_eq!(s, StatusCode::UNAUTHORIZED, "no token → 401");
    assert_eq!(body["error"]["code"], "InvalidControlToken");

    let (s2, _b) = control_request(app, "GET", "/_control/sources", Some("wrong")).await;
    assert_eq!(s2, StatusCode::UNAUTHORIZED, "wrong token → 401");
}

// ---------------------------------------------------------------------------
// Plan 17-04 Task 1 — reset-to-empty (CTRL-03, D-09) + the empty-tenant ARM
// read-path proof. `reset` TRUNCATEs synthetic.* under the single-writer gate;
// afterward ARM lists 200-empty, detail 404s, /_sim/summary zeros, and a fresh
// router over the now-empty schema still boots (the 17-01 startup-tolerance proof).
// ---------------------------------------------------------------------------

/// Poll `GET /_control/jobs/{id}` until it reaches `want` (panics on the opposite terminal
/// state or on timeout). Shared by the reset + snapshot round-trip tests.
async fn await_status(app: &Router, job_id: &str, want: &str) {
    for _ in 0..400 {
        let (_, jb) = control_request(
            app.clone(),
            "GET",
            &format!("/_control/jobs/{job_id}"),
            Some(TEST_TOKEN),
        )
        .await;
        match jb["status"].as_str() {
            Some(s) if s == want => return,
            Some("failed") if want != "failed" => panic!("job {job_id} failed: {jb:?}"),
            Some("succeeded") if want == "failed" => {
                panic!("job {job_id} succeeded, wanted failed")
            }
            _ => {}
        }
        tokio::time::sleep(std::time::Duration::from_millis(25)).await;
    }
    panic!("job {job_id} never reached {want:?}");
}

/// CTRL-03 (D-03/D-09/D-11): a seeded tenant, then POST /_control/reset → after the job
/// succeeds the ARM read path serves an EMPTY tenant (list 200 `{value:[]}`, a resource
/// detail GET 404 `ResourceNotFound`, `/_sim/summary` zeros) — not a crash — and a FRESH
/// router built over the now-empty schema still boots and serves the empties (the 17-01
/// startup-tolerance proof).
#[tokio::test]
async fn empty_tenant_read_path() {
    let (pool, _pg) = start_pg().await;
    common::seed_fixture(&pool).await;
    // A real armed server applies every `ensure_*_schema` at startup; the in-process test
    // router does not, so provision the sql/007 `profile_name` column the `/_sim/summary`
    // read depends on (seed_fixture applies 001/002/003/006 only).
    tenantless_server::ensure_web_metadata_schema(&pool)
        .await
        .expect("provision web-metadata schema");
    let cp = common::armed_control_plane(&pool, TEST_TOKEN);
    let app = build_router(armed_state_with(&pool, cp.clone()));

    // Precondition: the seeded tenant lists non-empty BEFORE reset (a genuine discriminator).
    let (s0, b0) = common::request(app.clone(), "GET", "/subscriptions", Some("t")).await;
    assert_eq!(s0, StatusCode::OK);
    assert!(
        !b0["value"].as_array().unwrap().is_empty(),
        "seeded tenant lists non-empty before reset"
    );

    // Reset → 202 {job_id}; wait for it to succeed.
    let (status, json) = control_post_json(
        app.clone(),
        "/_control/reset",
        Some(TEST_TOKEN),
        &serde_json::json!({}),
    )
    .await;
    assert_eq!(status, StatusCode::ACCEPTED, "reset → 202");
    let job_id = json["job_id"]
        .as_str()
        .expect("reset 202 carries a job_id")
        .to_string();
    await_status(&app, &job_id, "succeeded").await;

    // ARM list → 200 empty envelope.
    let (s1, b1) = common::request(app.clone(), "GET", "/subscriptions", Some("t")).await;
    assert_eq!(s1, StatusCode::OK, "empty tenant lists 200, not a crash");
    assert_eq!(
        b1["value"],
        serde_json::json!([]),
        "empty ARM collection envelope"
    );

    // Resource detail → 404 ResourceNotFound.
    let detail_uri = format!(
        "/subscriptions/{}/resourceGroups/{}/providers/Microsoft.Storage/storageAccounts/res-0000",
        common::SUB_A,
        common::DENSE_RG_NAME
    );
    let (s2, b2) = common::request(app.clone(), "GET", &detail_uri, Some("t")).await;
    assert_eq!(s2, StatusCode::NOT_FOUND, "detail on an empty tenant → 404");
    assert_eq!(b2["error"]["code"], "ResourceNotFound");

    // /_sim/summary → zeros + null metadata (bearer-exempt).
    let (s3, b3) = common::request(app.clone(), "GET", "/_sim/summary", None).await;
    assert_eq!(s3, StatusCode::OK);
    assert_eq!(b3["totals"]["subscriptions"], 0);
    assert_eq!(b3["totals"]["resources"], 0);
    assert_eq!(b3["tenantId"], serde_json::Value::Null, "no active tenant");

    // A FRESH router over the now-empty schema still boots and serves empties (17-01 D-09).
    let app2 = build_router(disarmed_state(&pool));
    let (s4, b4) = common::request(app2, "GET", "/subscriptions", Some("t")).await;
    assert_eq!(
        s4,
        StatusCode::OK,
        "fresh server over an empty schema boots + serves 200"
    );
    assert_eq!(b4["value"], serde_json::json!([]));
}

/// CTRL-03 (D-11): with the single-writer permit already held (an in-flight destructive
/// job), POST /_control/reset → 409 `ControlBusy` and NO job is inserted.
#[tokio::test]
async fn reset_serializes_under_gate() {
    let (pool, _pg) = start_pg().await;
    common::seed_fixture(&pool).await;
    let cp = common::armed_control_plane(&pool, TEST_TOKEN);
    // Hold the permit to simulate an in-flight destructive job.
    let _held = cp
        .write_gate
        .clone()
        .try_acquire_owned()
        .expect("first permit is free");
    let app = build_router(armed_state_with(&pool, cp.clone()));

    let (status, json) = control_post_json(
        app,
        "/_control/reset",
        Some(TEST_TOKEN),
        &serde_json::json!({}),
    )
    .await;
    assert_eq!(
        status,
        StatusCode::CONFLICT,
        "reset while a job holds the permit → 409"
    );
    assert_eq!(json["error"]["code"], "ControlBusy");
    assert_eq!(
        cp.registry.lock().unwrap().len(),
        0,
        "no reset job is inserted when the write gate is busy"
    );
}

// ---------------------------------------------------------------------------
// Plan 17-04 Task 2 — pg_dump/pg_restore snapshots (CTRL-04, D-04/D-05/D-13/D-14):
// safe-name-guarded save/restore/delete under the single-writer gate, the DSN via
// PG* env (never argv), the missing-binary path a first-class clean `failed` job, and
// a full-state (incl. drift) round-trip that hot-swaps the served tenant.
// ---------------------------------------------------------------------------

/// True if `bin --version` can be spawned (the client tool is on PATH). `pg_dump`/`pg_restore`
/// are NOT on this dev box (RESEARCH Env Availability) — the round-trip skips cleanly then.
fn binary_present(bin: &str) -> bool {
    std::process::Command::new(bin)
        .arg("--version")
        .output()
        .is_ok()
}

/// T-17-02 (D-13): every snapshot op safe-name-validates BEFORE touching the filesystem or
/// spawning a subprocess — an unsafe name is a fixed 400 and NO job is inserted. `save` takes
/// the name in the JSON body (so `../evil` is testable); `restore`/`delete` take a routable
/// unsafe stem (`bad..name`) in the path.
#[tokio::test]
async fn snapshot_name_rejects_paths() {
    let (pool, _pg) = start_pg().await;
    let cp = common::armed_control_plane(&pool, TEST_TOKEN);
    let app = build_router(armed_state_with(&pool, cp.clone()));

    // save with an unsafe name → 400, no job/subprocess.
    let (s, b) = control_post_json(
        app.clone(),
        "/_control/snapshots",
        Some(TEST_TOKEN),
        &serde_json::json!({ "name": "../evil" }),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_REQUEST, "unsafe save name → 400");
    assert_eq!(b["error"]["code"], "InvalidRequestContent");

    // restore with an unsafe (routable) name → 400 (BEFORE the exists/404 + gate).
    let (s2, _b2) = control_post_json(
        app.clone(),
        "/_control/snapshots/bad..name/restore",
        Some(TEST_TOKEN),
        &serde_json::json!({}),
    )
    .await;
    assert_eq!(s2, StatusCode::BAD_REQUEST, "unsafe restore name → 400");

    // delete with an unsafe (routable) name → 400.
    let (s3, _b3) = control_request(
        app,
        "DELETE",
        "/_control/snapshots/bad..name",
        Some(TEST_TOKEN),
    )
    .await;
    assert_eq!(s3, StatusCode::BAD_REQUEST, "unsafe delete name → 400");

    assert_eq!(
        cp.registry.lock().unwrap().len(),
        0,
        "no job is inserted by any unsafe-name op (validated before spawn)"
    );
}

/// T-17-04 (D-13, Pitfall 4): a snapshot `save` when `pg_dump` cannot dump (absent binary OR
/// unreachable DSN — the default `armed_control_plane` uses a placeholder DSN) ends the job
/// `failed` with a logged cause and the server STAYS UP (no 500/panic). The missing-binary
/// path is a first-class outcome, not an edge case.
#[tokio::test]
async fn snapshot_missing_binary_fails_clean() {
    let (pool, _pg) = start_pg().await;
    let cp = common::armed_control_plane(&pool, TEST_TOKEN);
    let app = build_router(armed_state_with(&pool, cp.clone()));

    let (status, json) = control_post_json(
        app.clone(),
        "/_control/snapshots",
        Some(TEST_TOKEN),
        &serde_json::json!({ "name": "snap1" }),
    )
    .await;
    assert_eq!(
        status,
        StatusCode::ACCEPTED,
        "save → 202 (the job is accepted; failure surfaces in the job record, D-17)"
    );
    let job_id = json["job_id"].as_str().expect("save 202 carries a job_id");

    // The job MUST reach `failed` (absent pg_dump ⇒ spawn error; present ⇒ the placeholder DSN
    // is unreachable ⇒ nonzero exit). Either way a clean `failed`, never a 500/panic.
    await_status(&app, job_id, "failed").await;

    // Server stays up: a follow-up control request still responds.
    let (s, _b) = control_request(app, "GET", "/_control/probe", Some(TEST_TOKEN)).await;
    assert_eq!(
        s,
        StatusCode::OK,
        "the server keeps serving after a failed snapshot job"
    );
}

/// P1 regression (17-UAT Run 2): `DELETE /_control/snapshots/{name}` MUST serialize under the
/// single-writer gate so it cannot race an in-flight restore/save (TOCTOU). `restore` checks the
/// artifact `is_file()`, TRUNCATEs `synthetic.*`, then hands the path to `pg_restore`; a DELETE
/// that removes the `.dump` between the check and `pg_restore` makes the restore fail AFTER the
/// truncate → the live tenant is left empty/dirty. With the write permit already HELD (a
/// restore/save "running"), a DELETE of an EXISTING snapshot must be rejected `409 ControlBusy`
/// AND the artifact must SURVIVE. On the buggy code delete skipped the gate → `204` + the `.dump`
/// removed mid-restore.
#[tokio::test]
async fn delete_serializes_under_gate() {
    let (pool, _pg) = start_pg().await;
    let cp = common::armed_control_plane(&pool, TEST_TOKEN);
    // A real artifact a concurrent restore/save depends on.
    let dump = cp.dirs.snapshots.join("held.dump");
    std::fs::write(&dump, b"snapshot-bytes").expect("write snapshot artifact");
    // Hold the permit to simulate an in-flight restore/save job.
    let _held = cp
        .write_gate
        .clone()
        .try_acquire_owned()
        .expect("first permit is free");
    let app = build_router(armed_state_with(&pool, cp.clone()));

    let (status, json) =
        control_request(app, "DELETE", "/_control/snapshots/held", Some(TEST_TOKEN)).await;
    assert_eq!(
        status,
        StatusCode::CONFLICT,
        "delete while a job holds the write gate → 409 (no race with restore/save)"
    );
    assert_eq!(json["error"]["code"], "ControlBusy");
    assert!(
        dump.is_file(),
        "the artifact SURVIVES a delete attempt during an in-flight job (the race is closed)"
    );
}

/// CTRL-04: with the write gate FREE (idle server), a delete of an existing snapshot → `204`
/// and the artifact is removed (the happy path still works once no job holds the gate).
#[tokio::test]
async fn delete_idle_removes_artifact() {
    let (pool, _pg) = start_pg().await;
    let cp = common::armed_control_plane(&pool, TEST_TOKEN);
    let dump = cp.dirs.snapshots.join("gone.dump");
    std::fs::write(&dump, b"snapshot-bytes").expect("write snapshot artifact");
    let app = build_router(armed_state_with(&pool, cp.clone()));

    let (status, _json) =
        control_request(app, "DELETE", "/_control/snapshots/gone", Some(TEST_TOKEN)).await;
    assert_eq!(status, StatusCode::NO_CONTENT, "idle delete → 204");
    assert!(
        !dump.exists(),
        "the artifact is removed on a successful (idle) delete"
    );
}

/// CTRL-04 (D-05/D-14): with `pg_dump`/`pg_restore` present, save `s1` → mutate (reset) →
/// restore `s1` reproduces the FULL served state INCLUDING drift, and the running server
/// serves the restored tenant with NO restart (hot-swap). Skips cleanly (does NOT fail) when
/// the client tools are absent — the default state on this dev box.
#[tokio::test]
async fn snapshot_roundtrip() {
    if !binary_present("pg_dump") || !binary_present("pg_restore") {
        eprintln!("skipping snapshot_roundtrip: pg_dump/pg_restore not on PATH (RESEARCH default)");
        return;
    }
    let (pool, container) = start_pg().await;
    common::seed_fixture(&pool).await;
    tenantless_server::ensure_web_metadata_schema(&pool)
        .await
        .expect("provision web-metadata schema");
    // Drift rows prove the D-14 full-state capture (drift_records/drift_batches).
    let _drift = common::seed_drift_rows(&pool).await;

    // A ControlPlane whose DSN points at the SAME container so pg_dump/pg_restore connect there.
    let host = container.get_host().await.expect("container host");
    let port = container
        .get_host_port_ipv4(5432)
        .await
        .expect("container port");
    let dsn = format!("postgres://postgres:postgres@{host}:{port}/postgres");
    let cp = common::armed_control_plane_with_dsn(&pool, TEST_TOKEN, &dsn);
    let app = build_router(armed_state_with(&pool, cp.clone()));

    let pre_drift: i64 = sqlx::query_scalar("SELECT count(*) FROM synthetic.drift_records")
        .fetch_one(&pool)
        .await
        .expect("count drift pre");
    assert!(pre_drift > 0, "fixture seeded drift rows to capture");

    // Save s1.
    let (s, j) = control_post_json(
        app.clone(),
        "/_control/snapshots",
        Some(TEST_TOKEN),
        &serde_json::json!({ "name": "s1" }),
    )
    .await;
    assert_eq!(s, StatusCode::ACCEPTED, "save → 202");
    await_status(&app, j["job_id"].as_str().unwrap(), "succeeded").await;

    // s1 appears in the listing.
    let (sl, bl) =
        control_request(app.clone(), "GET", "/_control/snapshots", Some(TEST_TOKEN)).await;
    assert_eq!(sl, StatusCode::OK);
    let names: Vec<String> = bl["snapshots"]
        .as_array()
        .expect("snapshots array")
        .iter()
        .map(|e| e["name"].as_str().expect("name").to_string())
        .collect();
    assert!(names.contains(&"s1".to_string()), "s1 listed after save");

    // Mutate: reset wipes everything (incl. drift).
    let (sr, jr) = control_post_json(
        app.clone(),
        "/_control/reset",
        Some(TEST_TOKEN),
        &serde_json::json!({}),
    )
    .await;
    assert_eq!(sr, StatusCode::ACCEPTED);
    await_status(&app, jr["job_id"].as_str().unwrap(), "succeeded").await;
    let mid_drift: i64 = sqlx::query_scalar("SELECT count(*) FROM synthetic.drift_records")
        .fetch_one(&pool)
        .await
        .expect("count drift mid");
    assert_eq!(mid_drift, 0, "reset wiped drift");

    // Restore s1.
    let (sre, jre) = control_post_json(
        app.clone(),
        "/_control/snapshots/s1/restore",
        Some(TEST_TOKEN),
        &serde_json::json!({}),
    )
    .await;
    assert_eq!(sre, StatusCode::ACCEPTED, "restore → 202");
    await_status(&app, jre["job_id"].as_str().unwrap(), "succeeded").await;

    // Full state incl. drift restored; served live (no restart).
    let post_drift: i64 = sqlx::query_scalar("SELECT count(*) FROM synthetic.drift_records")
        .fetch_one(&pool)
        .await
        .expect("count drift post");
    assert_eq!(
        post_drift, pre_drift,
        "restore reproduced drift_records (D-14)"
    );

    let (sfin, bfin) = common::request(app, "GET", "/subscriptions", Some("t")).await;
    assert_eq!(sfin, StatusCode::OK);
    assert!(
        !bfin["value"].as_array().unwrap().is_empty(),
        "the running server serves the restored tenant hot (D-05, no restart)"
    );
}
