//! Control-plane job registry types + the `ControlPlane` bundle (Phase 17).
//!
//! **Types only in 17-01** — the tokio subprocess runner that drives `uv run
//! tenantless generate|analyze` lands in 17-02, and the pg_dump/pg_restore snapshot
//! orchestration in 17-04. This module defines the shared contracts every later
//! Phase-17 plan builds on: the simple `JobStatus` state machine (D-15), the in-memory
//! `Job` record with a bounded log tail (D-06), the serializable `JobSnapshot` the poll
//! endpoint returns (D-07), and the `ControlPlane` bundle carried in `AppState.control`
//! (`Some` ⇔ armed, D-02) — mirroring the in-memory `Metrics` Arc-registry precedent.
//!
//! The control token is NEVER stored in the clear: `arm_decision`/`arm` keep only the
//! SHA-256 `token_digest`, compared in constant time by [`crate::control::control_token`].

use std::collections::{HashMap, VecDeque};
use std::path::PathBuf;
use std::process::Stdio;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde::Serialize;
use sha2::{Digest, Sha256};
use sqlx::PgPool;
use tokio::io::{AsyncBufRead, AsyncBufReadExt, BufReader};
use tokio::process::Command;
use tokio::sync::OwnedSemaphorePermit;
use uuid::Uuid;

/// Bounded per-job log tail cap (D-06): keep only the last `LOG_CAP` captured lines so
/// a 500K-resource generate cannot grow the in-memory log without bound.
pub const LOG_CAP: usize = 200;

/// Per-line byte cap for a captured log line — the companion to [`LOG_CAP`] (which bounds
/// the line COUNT). `LOG_CAP` alone leaves each individual line unbounded, so a child that
/// emits an enormous newline-free line could still grow the in-memory log without limit.
/// [`read_capped_line`] retains at most this many bytes per line and drains the physical
/// remainder, so total per-job log memory is bounded by `LOG_CAP * LOG_LINE_CAP`.
pub const LOG_LINE_CAP: usize = 8 * 1024;

/// Maximum number of jobs retained in the in-memory registry. The registry otherwise grows
/// one entry per control job for the life of the process (a slow unbounded leak); a fresh
/// insert first evicts the oldest already-terminal jobs down to this bound (see
/// [`evict_terminal`]). In-flight jobs are never evicted, so this is a floor on how much
/// completed history is kept, not a hard cap on live jobs.
pub const JOB_RETENTION: usize = 100;

/// Server-only secret env vars that must NEVER be inherited by a spawned child (WR-03/T-17-05).
/// The control token arms the server via `TENANTLESS_CONTROL_TOKEN` (the recommended env path),
/// which `tokio::process::Command` inherits by default — but no child (generate/analyze/pg_dump/
/// pg_restore) needs it, so it is stripped from every child we spawn (keeps the secret off the
/// child's `/proc/<pid>/environ`, closing the surface T-17-05 shrinks).
pub const CHILD_SECRET_ENV: [&str; 1] = ["TENANTLESS_CONTROL_TOKEN"];

/// Strip the server-only secrets ([`CHILD_SECRET_ENV`]) from a child command's inherited env
/// (WR-03). Applied at EVERY child-spawn construction site (the pipeline generate/analyze command
/// AND the pg_dump/pg_restore snapshot commands) so the control secret is never inherited.
pub fn scrub_child_env(cmd: &mut Command) {
    for key in CHILD_SECRET_ENV {
        cmd.env_remove(key);
    }
}

/// SHA-256 a string to a fixed 32-byte digest. The control token is hashed to this
/// fixed width BEFORE the constant-time compare so the comparison never leaks length
/// (RESEARCH "Don't Hand-Roll"). Also used to derive `token_digest` at arm time.
pub fn digest(s: &str) -> [u8; 32] {
    Sha256::digest(s.as_bytes()).into()
}

/// The simple job state machine (D-15): `queued → running → succeeded | failed`. No
/// user cancel this phase. Serialized **lowercase** — the exact wire strings the
/// frontend keys on (D-17); the serde default would emit `"Queued"` and break the match.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum JobStatus {
    Queued,
    Running,
    Succeeded,
    Failed,
}

/// The kind of work a control job performs. Serialized lowercase for the same
/// frontend-keying reason as [`JobStatus`].
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum JobKind {
    Generate,
    Analyze,
    Reset,
    Snapshot,
    Restore,
}

/// One tracked job in the in-memory registry (D-06). Cloned out under the registry
/// mutex for polling; the runner (17-02) mutates it in place via lock-mutate-drop.
#[derive(Debug, Clone)]
pub struct Job {
    /// Unguessable v4 id (mild enumeration resistance over a sequential counter).
    pub id: Uuid,
    /// What the job is doing.
    pub kind: JobKind,
    /// Current lifecycle state.
    pub status: JobStatus,
    /// Coarse phase label mapped from the child's stderr (D-08), e.g. `"generating tenant…"`.
    pub phase: Option<String>,
    /// Bounded captured log tail (last [`LOG_CAP`] lines).
    pub log: VecDeque<String>,
    /// Opportunistically parsed final result (tenant_id + counts), if any (D-08).
    pub result: Option<serde_json::Value>,
    /// When the job entered the registry — for the UI elapsed timer.
    pub started_at: std::time::Instant,
}

impl Job {
    /// A fresh `queued` job with an unguessable id and an empty log.
    pub fn new(kind: JobKind) -> Job {
        Job {
            id: Uuid::new_v4(),
            kind,
            status: JobStatus::Queued,
            phase: None,
            log: VecDeque::new(),
            result: None,
            started_at: std::time::Instant::now(),
        }
    }

    /// Push a captured line, evicting the oldest so the tail stays bounded to [`LOG_CAP`].
    pub fn push_log(&mut self, line: String) {
        if self.log.len() >= LOG_CAP {
            self.log.pop_front();
        }
        self.log.push_back(line);
    }

    /// Project the mutable `Job` to the serializable wire shape the poll endpoint
    /// returns (status + phase + last-N log lines + result) — never the `Instant`.
    pub fn snapshot(&self) -> JobSnapshot {
        JobSnapshot {
            id: self.id,
            kind: self.kind,
            status: self.status,
            phase: self.phase.clone(),
            log: self.log.iter().cloned().collect(),
            result: self.result.clone(),
        }
    }

    /// True once the job has reached a terminal state (`Succeeded`/`Failed`) — only
    /// terminal jobs are eligible for retention eviction.
    fn is_terminal(&self) -> bool {
        matches!(self.status, JobStatus::Succeeded | JobStatus::Failed)
    }
}

/// Bound the in-memory job registry to [`JOB_RETENTION`] (companion to the per-job
/// [`LOG_CAP`]/[`LOG_LINE_CAP`] bounds): evict the OLDEST already-terminal jobs (by
/// `started_at`) until at most `retention - 1` remain, so the caller's subsequent insert
/// lands at or below `retention`. In-flight (`Queued`/`Running`) jobs are NEVER evicted —
/// if every remaining entry is in-flight the registry is left as-is (a transient overshoot
/// bounded by the single-writer gate, not a leak). Called under the registry lock by the
/// single insert seam (`control::register_job`), so history never grows without bound.
pub(crate) fn evict_terminal(reg: &mut HashMap<Uuid, Job>, retention: usize) {
    while reg.len() >= retention.max(1) {
        let oldest_terminal = reg
            .values()
            .filter(|j| j.is_terminal())
            .min_by_key(|j| j.started_at)
            .map(|j| j.id);
        match oldest_terminal {
            Some(id) => {
                reg.remove(&id);
            }
            None => break, // nothing terminal to evict — keep the in-flight jobs
        }
    }
}

/// The serializable projection of a [`Job`] returned by `GET /_control/jobs/{id}` (D-07).
/// A plain owned snapshot so the registry mutex is released before serialization.
#[derive(Debug, Clone, Serialize)]
pub struct JobSnapshot {
    pub id: Uuid,
    pub kind: JobKind,
    pub status: JobStatus,
    pub phase: Option<String>,
    pub log: Vec<String>,
    pub result: Option<serde_json::Value>,
}

/// Server-owned directories for control-plane artifacts (D-03/D-12/D-13). All names
/// crossing into these dirs are safe-name guarded — no arbitrary paths, no upload.
#[derive(Debug, Clone)]
pub struct ControlDirs {
    /// Derived profiles written by `analyze` (safe-name only) → the generate allowlist.
    pub profiles: PathBuf,
    /// Operator-populated DuckDB analyze sources (dropped in out-of-band).
    pub sources: PathBuf,
    /// `pg_dump` snapshot artifacts (safe-name only).
    pub snapshots: PathBuf,
}

/// The armed control-plane bundle carried in `AppState.control` (`Some` ⇔ armed, D-02).
///
/// Mirrors the in-memory `Metrics` Arc-registry precedent. The `registry` is a
/// `std::sync::Mutex` held only for lock-mutate-drop (never across an `.await`); the
/// `write_gate` is a `Semaphore(1)` that serializes ALL destructive jobs (D-11). The
/// `database_url` is threaded here because `AppState` today stores only the pool, not the
/// URL string the child `uv run tenantless generate` needs via `DATABASE_URL` (Pitfall 2).
#[derive(Clone)]
pub struct ControlPlane {
    /// SHA-256 of the configured control secret — the raw token is never stored (T-17-05).
    pub token_digest: [u8; 32],
    /// The server's Postgres DSN, passed to child jobs via env (Pitfall 2, T-07-02).
    pub database_url: String,
    /// The repo root the child `uv` runs from (`current_dir`).
    pub repo_root: PathBuf,
    /// Server-owned artifact directories (profiles / sources / snapshots).
    pub dirs: ControlDirs,
    /// In-memory ephemeral job registry (D-06) — reset on restart.
    pub registry: Arc<Mutex<HashMap<Uuid, Job>>>,
    /// Single-writer permit source: at most one destructive job in flight (D-11).
    pub write_gate: Arc<tokio::sync::Semaphore>,
    /// The pipeline argv prefix the runner invokes for generate/analyze jobs — production
    /// arms this to [`DEFAULT_PIPELINE_CMD`] (`uv run tenantless`); the handlers append the
    /// subcommand + validated flags. A per-instance seam so integration tests can substitute
    /// a deterministic stub (RESEARCH Wave-0 runner seam) without a runnable Python CLI.
    pub pipeline_cmd: Vec<String>,
    /// The pool used for `TRUNCATE` on reset/restore (17-02/17-04).
    pub pool: PgPool,
}

/// The fail-closed arming DECISION (D-02), factored out so the security-critical rule is
/// unit-testable DB-free (no pool, no dir creation). Returns:
///   * `Ok(None)` — the control plane is disabled (flag absent): stay read-only;
///   * `Err(msg)` — enabled but the token is missing/empty/whitespace: **fail closed**,
///     with a message naming BOTH `--control-token` and `TENANTLESS_CONTROL_TOKEN`;
///   * `Ok(Some(digest))` — enabled with a non-empty token: the SHA-256 token digest.
///
/// [`ControlPlane::arm`] wraps this and, on `Some`, creates the three control-data
/// subdirs and assembles the bundle.
pub fn arm_decision(enable: bool, token: Option<&str>) -> Result<Option<[u8; 32]>, String> {
    if !enable {
        return Ok(None);
    }
    let token = token.map(str::trim).unwrap_or("");
    if token.is_empty() {
        return Err(
            "control plane enabled (--enable-control-plane) but no control token configured \
             — set --control-token or TENANTLESS_CONTROL_TOKEN to a non-empty secret \
             (fail-closed, D-02)"
                .to_string(),
        );
    }
    Ok(Some(digest(token)))
}

impl ControlPlane {
    /// Assemble the armed bundle from the CLI config + the server pool, implementing the
    /// D-02 fail-closed rule via [`arm_decision`]: disabled → `Ok(None)`; enabled + empty
    /// token → `Err`; enabled + non-empty → `Ok(Some(ControlPlane))` after creating the
    /// three server-owned control-data subdirs (`profiles/`, `sources/`, `snapshots/`).
    pub fn arm(cli: &crate::config::Cli, pool: PgPool) -> Result<Option<ControlPlane>, String> {
        let token_digest =
            match arm_decision(cli.enable_control_plane, cli.control_token.as_deref())? {
                None => return Ok(None),
                Some(d) => d,
            };

        let base = &cli.control_data_dir;
        let dirs = ControlDirs {
            profiles: base.join("profiles"),
            sources: base.join("sources"),
            snapshots: base.join("snapshots"),
        };
        for d in [&dirs.profiles, &dirs.sources, &dirs.snapshots] {
            std::fs::create_dir_all(d)
                .map_err(|e| format!("failed to create control-data dir {}: {e}", d.display()))?;
        }

        let repo_root = std::env::current_dir()
            .map_err(|e| format!("failed to resolve repo root (cwd): {e}"))?;

        Ok(Some(ControlPlane {
            token_digest,
            database_url: cli.database_url.clone(),
            repo_root,
            dirs,
            registry: Arc::new(Mutex::new(HashMap::new())),
            write_gate: Arc::new(tokio::sync::Semaphore::new(1)),
            pipeline_cmd: DEFAULT_PIPELINE_CMD.iter().map(|s| s.to_string()).collect(),
            pool,
        }))
    }
}

// ---------------------------------------------------------------------------
// Job runner (Plan 17-02, Task 1) — a tokio subprocess runner that drives the
// Python `generate`/`analyze` CLI. RESEARCH Pattern 3 (concurrent drain) is the
// blueprint: drain stdout AND stderr concurrently (Pitfall 1 — a full pipe on one
// stream while blocking on the other deadlocks), map known stderr lines to coarse
// phase labels (D-08), opportunistically parse the final stdout summary, and finalize
// on the child's exit code (never a 500 on a bad child, D-08/D-15).
// ---------------------------------------------------------------------------

/// The full `synthetic.*` table set a reset TRUNCATEs and a snapshot must cover (D-14) —
/// a verbatim port of `writer.py::_SYNTHETIC_TABLES` in FK order (`role_assignments`
/// before `principals`, `drift_records` before `drift_batches`). This is a STATIC code
/// literal, NEVER user/profile input, so joining it into a `TRUNCATE`/`--table` fragment
/// introduces no injection surface (mirrors the `writer.truncate_synthetic` comment; the
/// project SQL bar binds user VALUES as `$N`, but relation names come from this allowlist).
pub const SYNTHETIC_TABLES: [&str; 11] = [
    "synthetic.tenant",
    "synthetic.subscriptions",
    "synthetic.resource_groups",
    "synthetic.resources",
    "synthetic.dependencies",
    "synthetic.violations",
    "synthetic.cost_records",
    "synthetic.role_assignments",
    "synthetic.principals",
    "synthetic.drift_records",
    "synthetic.drift_batches",
];

/// The production pipeline argv prefix: `uv run tenantless <subcommand> …`. Held in
/// [`ControlPlane::pipeline_cmd`] as a per-instance seam so integration tests can
/// substitute a deterministic stub instead of a runnable Python CLI (RESEARCH Wave-0
/// "runner seam"). The subcommand + validated flags are appended by the handlers (17-02).
pub const DEFAULT_PIPELINE_CMD: [&str; 3] = ["uv", "run", "tenantless"];

/// Wall-clock fail-safe (D-15): a job exceeding this is `start_kill`ed and marked `Failed`
/// with the same reset/regenerate recovery guidance. One hour leaves ample headroom over a
/// 500K-resource generate at the throughput the committed benchmark records for the bundled
/// synthetic profile (see `docs/benchmarks/`), which is reproducible on any machine.
pub const JOB_TIMEOUT: Duration = Duration::from_secs(60 * 60);

/// Map a raw child **stderr** line to a coarse phase label (D-08). Returns `Some(label)`
/// for the exact generator progress lines and `None` otherwise (an unknown line changes no
/// phase). Pure — the runner locks the registry only to APPLY the returned label.
pub fn phase_label(line: &str) -> Option<&'static str> {
    match line.trim() {
        "fitting distributions..." => Some("fitting distributions…"),
        "generating tenant..." => Some("generating tenant…"),
        "computing tag entropy..." => Some("computing tag entropy…"),
        "writing to database..." => Some("writing to database…"),
        _ => None,
    }
}

/// Opportunistically parse the final `generate` **stdout** summary line into the job result
/// (D-08). The canonical line is:
/// `Generated tenant {uuid}: {n} subscriptions, {n} resource groups, {n} resources, {n}
/// violations, …`. Returns a JSON object with `tenant_id` + the four headline counts, or
/// `None` if the line is not a well-formed summary (parse-failure keeps the job `Succeeded`).
pub fn parse_generate_summary(line: &str) -> Option<serde_json::Value> {
    let rest = line.trim().strip_prefix("Generated tenant ")?;
    let (tenant_id, counts) = rest.split_once(": ")?;
    let tenant_id = tenant_id.trim();
    if tenant_id.is_empty() {
        return None;
    }

    let (mut subscriptions, mut resource_groups, mut resources, mut violations) =
        (None, None, None, None);
    // Each comma-separated segment is `<n> <label words>`; match the four headline labels
    // and ignore the rest (dependencies / principals / role assignments / parenthetical tail).
    for part in counts.split(',') {
        let part = part.trim();
        let Some((num, label)) = part.split_once(' ') else {
            continue;
        };
        let Ok(n) = num.parse::<i64>() else {
            continue;
        };
        match label.trim() {
            "subscriptions" => subscriptions = Some(n),
            "resource groups" => resource_groups = Some(n),
            "resources" => resources = Some(n),
            "violations" => violations = Some(n),
            _ => {}
        }
    }

    Some(serde_json::json!({
        "tenant_id": tenant_id,
        "subscriptions": subscriptions?,
        "resource_groups": resource_groups?,
        "resources": resources?,
        "violations": violations?,
    }))
}

/// Lock the registry, apply `f` to the job if present, and drop the lock immediately —
/// the std `Mutex` is NEVER held across an `.await` (RESEARCH invariant / Pattern 4).
/// `pub(crate)` so the reset/snapshot runners (control.rs / snapshot.rs) mutate a job
/// through the SAME lock-mutate-drop seam as the subprocess runner.
pub(crate) fn with_job(cp: &ControlPlane, job_id: Uuid, f: impl FnOnce(&mut Job)) {
    if let Ok(mut reg) = cp.registry.lock()
        && let Some(job) = reg.get_mut(&job_id)
    {
        f(job);
    }
}

/// Spawn `cmd` as a tracked control job and drive it to a terminal state, updating the
/// in-memory registry as it runs. The caller (17-02 handlers) builds `cmd` (program, args,
/// the `DATABASE_URL` env, and `current_dir`) and holds the single-writer `_permit`, which
/// drops at the end here, releasing the write gate on success, failure, timeout, OR panic
/// (D-11).
///
/// Contract (D-08/D-15): stdio is forced (`stdin` null, `stdout`/`stderr` piped,
/// `kill_on_drop`); a spawn error (missing binary) marks the job `Failed` with a clear log
/// (never a panic); both child streams are drained CONCURRENTLY (Pitfall 1 — no pipe-buffer
/// deadlock); each line is pushed into the bounded log and a known stderr line updates the
/// phase label; a wall-clock timeout `start_kill`s the child and marks `Failed`; and exit 0
/// yields `Succeeded` (with an opportunistic summary parse into `result`), else `Failed`.
pub async fn run_command(
    cp: ControlPlane,
    job_id: Uuid,
    cmd: Command,
    _permit: OwnedSemaphorePermit,
) {
    // Production always uses the wall-clock [`JOB_TIMEOUT`]; the injectable variant below
    // exists ONLY so integration tests can drive a SHORT timeout (the default is an hour).
    run_command_with_timeout(cp, job_id, cmd, _permit, JOB_TIMEOUT).await;
}

/// The terminal outcome of a [`run_command_keep_permit`] run: whether the child exited 0 and
/// the LAST stdout line (for the opportunistic `generate` summary parse). Deliberately does NOT
/// carry the permit or a `Succeeded` transition — the CALLER (`run_command_with_timeout` for
/// generate/analyze, `save`/`restore` for snapshots) owns finalization so it can retain the
/// single-writer permit across artifact promotion / temp cleanup before dropping it.
pub(crate) struct RunOutcome {
    pub succeeded: bool,
    pub last_stdout: String,
}

/// Read one `\n`-delimited line from `reader`, retaining at most [`LOG_LINE_CAP`] bytes so a
/// child that emits an enormous newline-free line cannot grow memory without bound. This is
/// the per-line byte bound the plain `AsyncBufReadExt::lines()`/`next_line()` reader lacks:
/// `next_line` reads the WHOLE physical line into a `String` before returning, so a
/// megabytes-long line allocates megabytes. Here the first `LOG_LINE_CAP` bytes are kept and
/// the remainder up to the newline is drained via `fill_buf`/`consume` WITHOUT retaining it;
/// an over-long line is marked with a trailing ` …[truncated]`.
///
/// Returns `Ok(None)` only at EOF with no pending bytes (loop terminator); a final line with
/// no trailing newline is returned once, then the next call yields `None`. A trailing `\r` is
/// stripped (parity with `next_line`), and invalid UTF-8 is replaced lossily (the log is
/// display-only). Uses only `AsyncBufRead` so any `BufReader` over the child pipe works.
pub(crate) async fn read_capped_line<R>(reader: &mut R) -> std::io::Result<Option<String>>
where
    R: AsyncBufRead + Unpin,
{
    let mut buf: Vec<u8> = Vec::new();
    let mut truncated = false;
    let mut saw_any = false;
    loop {
        let available = reader.fill_buf().await?;
        if available.is_empty() {
            break; // EOF
        }
        saw_any = true;
        match available.iter().position(|&b| b == b'\n') {
            Some(pos) => {
                append_capped(&mut buf, &available[..pos], &mut truncated);
                reader.consume(pos + 1); // consume through the newline
                break;
            }
            None => {
                let len = available.len();
                append_capped(&mut buf, available, &mut truncated);
                reader.consume(len);
            }
        }
    }
    if !saw_any {
        return Ok(None); // clean EOF, nothing pending
    }
    if buf.last() == Some(&b'\r') {
        buf.pop();
    }
    let mut line = String::from_utf8_lossy(&buf).into_owned();
    if truncated {
        line.push_str(" …[truncated]");
    }
    Ok(Some(line))
}

/// Append `chunk` to `buf` but never past [`LOG_LINE_CAP`] retained bytes; set `truncated`
/// once any bytes are dropped. Overflow bytes are discarded here (the caller still `consume`s
/// them from the reader) so the physical line is drained without being retained.
fn append_capped(buf: &mut Vec<u8>, chunk: &[u8], truncated: &mut bool) {
    let room = LOG_LINE_CAP.saturating_sub(buf.len());
    if chunk.len() <= room {
        buf.extend_from_slice(chunk);
    } else {
        buf.extend_from_slice(&chunk[..room]);
        *truncated = true;
    }
}

/// Drive `cmd` to a terminal exit as a tracked job, WITHOUT owning/dropping a permit and WITHOUT
/// setting `JobStatus::Succeeded` — the permit-retaining core shared by the generate/analyze
/// runner and the snapshot save/restore runners (P1-A/P1-B). It sets `Running` on entry; on a
/// spawn error / nonzero exit / timeout it sets `Failed` (+ the same log line as before) and
/// returns `succeeded: false`; on exit 0 it leaves the job `Running` and returns
/// `succeeded: true` with the last stdout line, so the caller finalizes (`Succeeded` +
/// summary parse) only AFTER its own post-run work (rename / temp cleanup) under the still-held
/// permit. The P1-B kill-before-join timeout ordering is preserved verbatim.
pub(crate) async fn run_command_keep_permit(
    cp: &ControlPlane,
    job_id: Uuid,
    cmd: &mut Command,
    timeout: Duration,
) -> RunOutcome {
    with_job(cp, job_id, |j| j.status = JobStatus::Running);

    cmd.stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);

    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => {
            // Missing binary / not executable: a first-class `Failed`, never a crash (D-08).
            with_job(cp, job_id, |j| {
                j.push_log(format!("failed to spawn subprocess: {e}"));
                j.status = JobStatus::Failed;
            });
            return RunOutcome {
                succeeded: false,
                last_stdout: String::new(),
            };
        }
    };

    let stdout = child.stdout.take().expect("stdout piped");
    let stderr = child.stderr.take().expect("stderr piped");

    // Drain BOTH streams concurrently. stdout returns the LAST line for the summary parse.
    let out_task = {
        let cp = cp.clone();
        tokio::spawn(async move {
            let mut last = String::new();
            let mut reader = BufReader::new(stdout);
            while let Ok(Some(line)) = read_capped_line(&mut reader).await {
                with_job(&cp, job_id, |j| j.push_log(line.clone()));
                last = line;
            }
            last
        })
    };
    let err_task = {
        let cp = cp.clone();
        tokio::spawn(async move {
            let mut reader = BufReader::new(stderr);
            while let Ok(Some(line)) = read_capped_line(&mut reader).await {
                let label = phase_label(&line);
                with_job(&cp, job_id, |j| {
                    if let Some(lbl) = label {
                        j.phase = Some(lbl.to_string());
                    }
                    j.push_log(line);
                });
            }
        })
    };

    // Wait with a wall-clock fail-safe. On a TIMEOUT the child may still be alive holding its
    // stdout/stderr pipes open, so we MUST kill (and reap) it BEFORE joining the drain tasks —
    // otherwise the drains never see EOF, `join!` blocks forever, the job never finalizes, and
    // the single-writer permit is held permanently (the P1-B deadlock). Kill-before-join
    // guarantees the pipes close so both drains complete and the permit can drop.
    let waited = tokio::time::timeout(timeout, child.wait()).await;
    if waited.is_err() {
        let _ = child.start_kill();
        let _ = child.wait().await; // reap → the pipes close so the drains below can finish
    }
    let (out_join, _err_join) = tokio::join!(out_task, err_task);
    let last_stdout = out_join.unwrap_or_default();

    let succeeded = match waited {
        Ok(Ok(status)) => status.success(),
        Ok(Err(e)) => {
            with_job(cp, job_id, |j| {
                j.push_log(format!("failed to await child: {e}"))
            });
            false
        }
        Err(_) => {
            // The child was already killed + reaped above; just log the timeout and fail the job.
            with_job(cp, job_id, |j| {
                j.push_log(format!(
                    "job timed out after {}s — killed (tenant may be dirty; reset or regenerate)",
                    timeout.as_secs()
                ));
            });
            false
        }
    };

    // Set Failed here (so a failed job is terminal even if the caller does no further work); a
    // successful job is left `Running` for the caller to finalize under the still-held permit.
    if !succeeded {
        with_job(cp, job_id, |j| j.status = JobStatus::Failed);
    }
    RunOutcome {
        succeeded,
        last_stdout,
    }
}

/// As [`run_command`], but with an injectable wall-clock `timeout` — a test-only seam so the
/// timeout/permit-release path is drivable without waiting the production hour. Production code
/// always calls [`run_command`] (which passes [`JOB_TIMEOUT`]); nothing else changes.
///
/// Delegates the child drive to [`run_command_keep_permit`], then finalizes `Succeeded` (+ the
/// opportunistic `generate` summary parse) on success. `_permit` drops at scope end — so
/// generate/analyze behavior (and every existing runner test) is byte-for-byte unchanged.
pub async fn run_command_with_timeout(
    cp: ControlPlane,
    job_id: Uuid,
    mut cmd: Command,
    _permit: OwnedSemaphorePermit,
    timeout: Duration,
) {
    let outcome = run_command_keep_permit(&cp, job_id, &mut cmd, timeout).await;
    with_job(&cp, job_id, |j| {
        if outcome.succeeded {
            j.status = JobStatus::Succeeded;
            if let Some(result) = parse_generate_summary(&outcome.last_stdout) {
                j.result = Some(result);
            }
        }
        // Failure already set to `Failed` inside `run_command_keep_permit`.
    });
    // `_permit` drops here → the single-writer gate is released.
}

/// The reset runner (Plan 17-04, CTRL-03/D-09): `TRUNCATE` every `synthetic.*` table under
/// the held single-writer permit, wiping the active tenant to a blank simulator. Unlike
/// [`run_command`] this is a pure SQL mutation (no subprocess) — but it uses the SAME
/// registry seam ([`with_job`]) and the SAME permit lifecycle (the `_permit` moves in and
/// drops here, releasing the write gate on success OR failure, D-11).
///
/// `TRUNCATE … RESTART IDENTITY CASCADE` over the FK-ordered [`SYNTHETIC_TABLES`] wipes all
/// rows in one atomic statement (CASCADE also clears any FK-referencing rows, matching
/// `writer.truncate_synthetic`). The relation list is a STATIC allowlist, never user input.
/// The migration-managed schema itself is preserved, so the ARM read path immediately serves
/// an empty tenant (list 200-empty / detail 404 / summary zeros) and a fresh boot tolerates
/// the empty schema (17-01 D-09). On SQL error the job ends `Failed` with a logged cause —
/// never a panic; the tenant may be left dirty (recover via reset/regenerate, D-15).
pub async fn run_reset(cp: ControlPlane, job_id: Uuid, _permit: OwnedSemaphorePermit) {
    with_job(&cp, job_id, |j| j.status = JobStatus::Running);
    match truncate_synthetic(&cp.pool).await {
        Ok(_) => with_job(&cp, job_id, |j| j.status = JobStatus::Succeeded),
        Err(e) => with_job(&cp, job_id, |j| {
            j.push_log(format!("reset TRUNCATE failed: {e}"));
            j.status = JobStatus::Failed;
        }),
    }
    // `_permit` drops here → the single-writer gate is released.
}

/// `TRUNCATE` every EXISTING `synthetic.*` table (`RESTART IDENTITY CASCADE`, FK-safe) —
/// the Rust twin of `writer.truncate_synthetic`, shared by the reset runner and the
/// snapshot restore path (17-04). Only tables that CURRENTLY exist are truncated: a
/// synthetic table may be introduced by a later migration than the one applied to the
/// target schema (e.g. the test fixture applies a subset), so `to_regclass` returns NULL
/// for an absent table and it is skipped rather than aborting the whole TRUNCATE. The
/// table-name list is the STATIC [`SYNTHETIC_TABLES`] allowlist, never user input, so the
/// generated statement carries no injection surface. A no-op (all absent) succeeds.
pub(crate) async fn truncate_synthetic(pool: &PgPool) -> Result<(), sqlx::Error> {
    let existing = existing_synthetic_tables(pool).await?;
    if existing.is_empty() {
        return Ok(());
    }
    let sql = format!("TRUNCATE {} RESTART IDENTITY CASCADE", existing.join(", "));
    sqlx::query(&sql).execute(pool).await?;
    Ok(())
}

/// Filter the STATIC [`SYNTHETIC_TABLES`] allowlist to the relations that CURRENTLY exist in the
/// target schema (a `to_regclass` probe over the allowlist bound as a `$1 text[]`), preserving the
/// FK order of the allowlist. Shared by [`truncate_synthetic`] (reset) and the snapshot restore
/// path, which builds the in-transaction `TRUNCATE {existing}` from it — a test fixture may apply
/// only a subset of migrations, so an absent table is skipped rather than aborting the statement.
/// The result carries no injection surface: the names come only from the code-literal allowlist.
pub(crate) async fn existing_synthetic_tables(pool: &PgPool) -> Result<Vec<String>, sqlx::Error> {
    let names: Vec<String> = SYNTHETIC_TABLES.iter().map(|s| s.to_string()).collect();
    let existing: Vec<String> = sqlx::query_scalar(
        "SELECT t FROM unnest($1::text[]) AS t WHERE to_regclass(t) IS NOT NULL",
    )
    .bind(&names)
    .fetch_all(pool)
    .await?;
    Ok(existing)
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::sync::Semaphore;

    /// A DB-free `ControlPlane` whose pool is a LAZY (never-dialed) connection — the
    /// spawn-failure path of `run_command_keep_permit` never touches the pool.
    fn lazy_cp() -> ControlPlane {
        let pool = sqlx::postgres::PgPool::connect_lazy("postgres://u:p@localhost:5432/db")
            .expect("build a lazy (never-dialed) pool");
        ControlPlane {
            token_digest: [0u8; 32],
            database_url: "postgres://u:p@localhost:5432/db".to_string(),
            repo_root: std::env::temp_dir(),
            dirs: ControlDirs {
                profiles: std::env::temp_dir(),
                sources: std::env::temp_dir(),
                snapshots: std::env::temp_dir(),
            },
            registry: Arc::new(Mutex::new(HashMap::new())),
            write_gate: Arc::new(Semaphore::new(1)),
            pipeline_cmd: Vec::new(),
            pool,
        }
    }

    /// Task 1 (P1-B substrate): `run_command_keep_permit` on a nonexistent binary returns
    /// `succeeded == false`, marks the job `Failed`, and NEVER touches the caller's permit —
    /// the single-writer gate stays held until the CALLER drops it (so save/restore can retain
    /// it across artifact promotion / temp cleanup). Required-test #5 substrate.
    #[tokio::test]
    async fn keep_permit_spawn_failure_fails_and_retains_permit() {
        let cp = lazy_cp();
        let job = Job::new(JobKind::Snapshot);
        let job_id = job.id;
        cp.registry.lock().unwrap().insert(job_id, job);

        // A permit the caller retains across keep_permit (models the write gate).
        let gate = Arc::new(Semaphore::new(1));
        let permit = gate.clone().try_acquire_owned().expect("first permit free");

        // A binary that does not exist → spawn error → succeeded == false, no pool access.
        let mut cmd = Command::new("definitely-not-a-real-binary-tenantless-xyz");
        let outcome = run_command_keep_permit(&cp, job_id, &mut cmd, JOB_TIMEOUT).await;
        assert!(!outcome.succeeded, "spawn failure → succeeded == false");
        assert!(
            outcome.last_stdout.is_empty(),
            "no stdout captured on spawn failure"
        );

        // keep_permit did NOT take/drop the caller's permit: the gate is still exhausted.
        assert!(
            gate.clone().try_acquire_owned().is_err(),
            "keep_permit must not touch the caller's permit (gate still held)"
        );

        // keep_permit set the job Failed itself (the caller does not have to).
        let mut status = None;
        with_job(&cp, job_id, |j| status = Some(j.status));
        assert_eq!(
            status,
            Some(JobStatus::Failed),
            "spawn failure → job Failed"
        );

        // Dropping the caller's permit is what releases the gate.
        drop(permit);
        assert!(
            gate.try_acquire_owned().is_ok(),
            "dropping the caller's permit releases the gate"
        );
    }

    // ----------------------------------------------------------------------- //
    // read_capped_line — the per-line byte bound (companion to LOG_CAP).
    // ----------------------------------------------------------------------- //

    /// Ordinary newline-delimited lines are returned verbatim (no marker), split on `\n`,
    /// and EOF yields `None`.
    #[tokio::test]
    async fn read_capped_line_splits_normal_lines() {
        let data = b"first\nsecond\nthird\n";
        let mut reader = BufReader::new(&data[..]);
        let mut got = Vec::new();
        while let Some(line) = read_capped_line(&mut reader).await.unwrap() {
            got.push(line);
        }
        assert_eq!(got, vec!["first", "second", "third"]);
    }

    /// A final line with no trailing newline is still returned once, then `None`. A trailing
    /// `\r` is stripped (CRLF parity with `next_line`).
    #[tokio::test]
    async fn read_capped_line_handles_no_trailing_newline_and_crlf() {
        let data = b"windows\r\ntail-no-newline";
        let mut reader = BufReader::new(&data[..]);
        assert_eq!(
            read_capped_line(&mut reader).await.unwrap().as_deref(),
            Some("windows"),
            "CRLF line strips the trailing \\r"
        );
        assert_eq!(
            read_capped_line(&mut reader).await.unwrap().as_deref(),
            Some("tail-no-newline"),
            "a newline-less final line is returned"
        );
        assert_eq!(
            read_capped_line(&mut reader).await.unwrap(),
            None,
            "EOF after the final line"
        );
    }

    /// THE bound: an enormous newline-free line is retained at exactly `LOG_LINE_CAP` bytes
    /// (plus a truncation marker) — never the whole line — and the physical remainder is
    /// drained so the FOLLOWING line is read intact. This is what `next_line()` failed to do.
    #[tokio::test]
    async fn read_capped_line_caps_and_drains_an_overlong_line() {
        let huge = "a".repeat(LOG_LINE_CAP * 4); // 4× the cap, no newline until the end
        let data = format!("{huge}\nafter\n");
        let mut reader = BufReader::new(data.as_bytes());

        let first = read_capped_line(&mut reader).await.unwrap().unwrap();
        assert!(
            first.ends_with(" …[truncated]"),
            "an over-long line is marked truncated"
        );
        let retained = first.trim_end_matches(" …[truncated]");
        assert_eq!(
            retained.len(),
            LOG_LINE_CAP,
            "exactly LOG_LINE_CAP bytes retained, not the whole {}-byte line",
            huge.len()
        );

        // The remainder of the huge line was drained (not misread as content), so the next
        // read returns the following line intact.
        assert_eq!(
            read_capped_line(&mut reader).await.unwrap().as_deref(),
            Some("after"),
            "the physical remainder was drained, so the next line is intact"
        );
    }

    // ----------------------------------------------------------------------- //
    // evict_terminal — the registry retention bound.
    // ----------------------------------------------------------------------- //

    /// Build a job in a given terminal/in-flight state with a distinct, strictly increasing
    /// `started_at` (a tiny sleep guarantees monotonic ordering for the oldest-first check).
    fn aged_job(status: JobStatus) -> Job {
        std::thread::sleep(std::time::Duration::from_millis(2));
        let mut j = Job::new(JobKind::Generate);
        j.status = status;
        j
    }

    /// Retention evicts the OLDEST terminal jobs first, bounding the registry so a fresh
    /// insert lands at `retention`.
    #[test]
    fn evict_terminal_drops_oldest_first_to_bound() {
        let mut reg: HashMap<Uuid, Job> = HashMap::new();
        // Five terminal jobs, inserted oldest → newest.
        let jobs: Vec<Job> = (0..5).map(|_| aged_job(JobStatus::Succeeded)).collect();
        let ids: Vec<Uuid> = jobs.iter().map(|j| j.id).collect();
        for j in jobs {
            reg.insert(j.id, j);
        }

        // Retention 3 → evict down to 2 so the caller's next insert makes 3.
        evict_terminal(&mut reg, 3);
        assert_eq!(reg.len(), 2, "bounded to retention - 1 before insert");
        assert!(
            !reg.contains_key(&ids[0]) && !reg.contains_key(&ids[1]) && !reg.contains_key(&ids[2]),
            "the three OLDEST terminal jobs were evicted"
        );
        assert!(
            reg.contains_key(&ids[3]) && reg.contains_key(&ids[4]),
            "the two newest survive"
        );
    }

    /// In-flight (queued/running) jobs are NEVER evicted, even when that leaves the registry
    /// above the retention bound — only completed history is dropped.
    #[test]
    fn evict_terminal_never_drops_in_flight_jobs() {
        let mut reg: HashMap<Uuid, Job> = HashMap::new();
        let running: Vec<Uuid> = (0..3)
            .map(|_| {
                let j = aged_job(JobStatus::Running);
                let id = j.id;
                reg.insert(id, j);
                id
            })
            .collect();

        // All in-flight, at the bound: nothing terminal to evict → registry left intact.
        evict_terminal(&mut reg, 3);
        assert_eq!(reg.len(), 3, "no terminal job to evict → in-flight kept");
        assert!(
            running.iter().all(|id| reg.contains_key(id)),
            "every in-flight job survives"
        );

        // Mix in two terminal jobs: only those are evicted, the running ones stay.
        let t0 = aged_job(JobStatus::Succeeded);
        let t1 = aged_job(JobStatus::Failed);
        reg.insert(t0.id, t0);
        reg.insert(t1.id, t1);
        evict_terminal(&mut reg, 3);
        assert!(
            running.iter().all(|id| reg.contains_key(id)),
            "in-flight jobs still survive after terminal eviction"
        );
        assert!(
            reg.values().all(|j| !j.is_terminal()),
            "all terminal jobs were evicted, leaving only in-flight"
        );
    }
}
