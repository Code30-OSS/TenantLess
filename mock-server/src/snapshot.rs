//! `pg_dump`/`pg_restore` snapshot orchestration (Phase 17-04, CTRL-04 / D-04/D-05/D-13/D-14).
//!
//! Named snapshots capture and restore the FULL served `synthetic.*` state (including
//! `drift_records`/`drift_batches`, D-14) as server-owned, safe-name artifacts under
//! [`ControlDirs::snapshots`]. `save` runs `pg_dump --format=custom --data-only
//! --schema=synthetic` to a `<name>.dump.partial` temp and atomically renames it to the final
//! `<name>.dump` only on exit 0 (temp-then-rename — a crashed dump never leaves a truncated file
//! that looks restorable) AND holds the writer permit through that rename so `Succeeded`
//! never precedes the published artifact (Finding B). `restore` VALIDATES the archive
//! (`pg_restore --list` TOC dry-run), DECODES it to a server-owned permission-restricted temp SQL
//! file (`pg_restore --data-only --disable-triggers -f` — a corrupt data block fails HERE, before
//! any mutation), guards the decoded SQL against transaction-control/reconnection, then applies it
//! via ONE `psql --single-transaction --set=ON_ERROR_STOP=1 -c <TRUNCATE> -f <temp>` so the TRUNCATE
//! and load share a SINGLE transaction — any load failure (or a kill/timeout) rolls the TRUNCATE
//! back too and leaves the live estate intact (Finding A). The temp is removed on every exit path
//! (RAII). The running server then serves the restored tenant hot — no restart, D-05.
//! Both run as tracked jobs through the SAME single-writer gate as generate/reset (D-11); a
//! MISSING `pg_dump`/`pg_restore` binary ends the job `failed` with a clear log, NEVER a crash
//! (D-13, Pitfall 4 — the default state on a box without `postgresql-client`).
//!
//! ## Setup requirement (D-13)
//! Snapshots require the Postgres client tools (`pg_dump`, `pg_restore`) on `PATH` — install
//! `postgresql-client` (Debian/Ubuntu) / `postgresql` (Homebrew) etc. All non-snapshot control
//! features (generate/analyze/reset/jobs/auth) work WITHOUT them. `restore`'s `--disable-triggers`
//! requires the connecting role to own `synthetic.*` (the dev/superuser role does) — otherwise
//! restore fails cleanly and the tenant may be left empty (recover via reset/regenerate, D-15).
//!
//! ## Credential handling (T-17-05)
//! The DSN is NEVER placed in argv (the process list is world-readable, and `serve.py` treats
//! the DSN as a secret). Instead [`pg_env`] derives `PGHOST`/`PGPORT`/`PGUSER`/`PGDATABASE`/
//! `PGPASSWORD` from `cp.database_url` and passes them via `.env(...)`; the snapshot NAME is
//! safe-name-validated by the caller (control.rs) BEFORE any path/subprocess (T-17-02).

use std::path::{Path, PathBuf};
use std::time::{Duration, UNIX_EPOCH};

use serde::Serialize;
use tokio::process::Command;
use tokio::sync::OwnedSemaphorePermit;
use uuid::Uuid;

use crate::job::{self, ControlPlane, JobStatus};

/// One entry in the snapshots listing (`GET /_control/snapshots`): the bare safe-name stem
/// (never a path) and its artifact mtime as Unix seconds (`0` if unavailable) so the UI can
/// sort/label most-recent-first.
#[derive(Debug, Clone, Serialize)]
pub struct SnapshotEntry {
    pub name: String,
    #[serde(rename = "createdUnix")]
    pub created_unix: i64,
}

/// Percent-decode a DSN userinfo component (`%XX` + `+`→space is NOT applied — userinfo uses
/// `%XX` only). Best-effort: an invalid escape is left verbatim so a malformed password never
/// panics. Kept minimal (no new dep) — the common dev DSN needs no decoding, but a password
/// with an encoded special char still round-trips (correctness, not just the happy path).
fn pct_decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out: Vec<u8> = Vec::with_capacity(bytes.len());
    let mut i = 0;
    let hex = |b: u8| -> Option<u8> {
        match b {
            b'0'..=b'9' => Some(b - b'0'),
            b'a'..=b'f' => Some(b - b'a' + 10),
            b'A'..=b'F' => Some(b - b'A' + 10),
            _ => None,
        }
    };
    while i < bytes.len() {
        if bytes[i] == b'%'
            && let (Some(h), Some(l)) = (
                bytes.get(i + 1).copied().and_then(hex),
                bytes.get(i + 2).copied().and_then(hex),
            )
        {
            out.push((h << 4) | l);
            i += 3;
            continue;
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8(out).unwrap_or_else(|_| s.to_string())
}

/// Derive the `PG*` connection env from a `postgres://user:pass@host:port/dbname?params` DSN
/// (T-17-05): the credentials travel via env, NEVER argv. Missing components are simply
/// omitted (`libpq` then falls back to its own defaults). Handles a bracketed IPv6 host
/// (`[::1]:5432`) and a percent-encoded password. Only keys that are present are returned.
pub fn pg_env(database_url: &str) -> Vec<(&'static str, String)> {
    let mut env: Vec<(&'static str, String)> = Vec::new();
    // Strip the scheme.
    let after_scheme = database_url
        .split_once("://")
        .map(|(_, rest)| rest)
        .unwrap_or(database_url);
    // authority[/path[?query]]
    let (authority, path) = match after_scheme.split_once('/') {
        Some((a, p)) => (a, p),
        None => (after_scheme, ""),
    };
    // [userinfo@]hostport
    let (userinfo, hostport) = match authority.rsplit_once('@') {
        Some((u, h)) => (Some(u), h),
        None => (None, authority),
    };
    if let Some(ui) = userinfo {
        let (user, pass) = match ui.split_once(':') {
            Some((u, p)) => (u, Some(p)),
            None => (ui, None),
        };
        if !user.is_empty() {
            env.push(("PGUSER", pct_decode(user)));
        }
        if let Some(p) = pass {
            env.push(("PGPASSWORD", pct_decode(p)));
        }
    }
    // host[:port] — support a bracketed IPv6 literal so its inner colons aren't split.
    let (host, port) = if let Some(rest) = hostport.strip_prefix('[') {
        match rest.split_once(']') {
            Some((h6, tail)) => (h6, tail.strip_prefix(':').filter(|p| !p.is_empty())),
            None => (hostport, None),
        }
    } else {
        match hostport.rsplit_once(':') {
            Some((h, p)) => (h, Some(p).filter(|p| !p.is_empty())),
            None => (hostport, None),
        }
    };
    if !host.is_empty() {
        env.push(("PGHOST", host.to_string()));
    }
    if let Some(p) = port {
        env.push(("PGPORT", p.to_string()));
    }
    // dbname = path up to '?'.
    let dbname = path.split('?').next().unwrap_or("");
    if !dbname.is_empty() {
        env.push(("PGDATABASE", pct_decode(dbname)));
    }
    env
}

/// The artifact path for a snapshot `name`. The caller MUST have safe-name-validated `name`
/// (T-17-02) — this only joins a `<name>.dump` under the server-owned snapshots dir.
fn dump_path(cp: &ControlPlane, name: &str) -> PathBuf {
    cp.dirs.snapshots.join(format!("{name}.dump"))
}

/// The in-progress TEMP path for a snapshot `name`: the FINAL [`dump_path`] with `.partial`
/// appended, in the SAME directory so the post-dump `std::fs::rename` to final is atomic on one
/// filesystem. `pg_dump` writes here; only an exit-0 dump is renamed to the final
/// `<name>.dump`. `list()` filters on the `.dump` extension, so a `.dump.partial` is never
/// surfaced — a crashed/partial dump can never be mistaken for a restorable archive.
fn partial_path(cp: &ControlPlane, name: &str) -> PathBuf {
    cp.dirs.snapshots.join(format!("{name}.dump.partial"))
}

/// The save runner (CTRL-04, D-13/D-14): `pg_dump --format=custom --data-only
/// --schema=synthetic --file <snapshots>/<name>.dump.partial`, credentials via [`pg_env`], driven
/// by [`job::run_command`] (a missing `pg_dump` binary OR a nonzero exit ends the job `failed`
/// with the captured stderr — never a crash). `--data-only` keeps the migration-managed schema
/// intact; custom format captures every `synthetic.*` table (incl. drift) in FK/TOC order.
///
/// The dump writes to a same-directory TEMP sibling and is renamed to the FINAL `<name>.dump`
/// **only** after pg_dump exits 0 — so a crashed/partial dump never leaves a truncated file that
/// `list()`/`restore()` would treat as valid (temp-then-rename atomicity). On any failure the temp
/// is best-effort removed, leaving no leftover artifact. The `permit` moves into `run_command` and
/// releases the write gate on completion (D-11); the rename is a local atomic fs op AFTER that,
/// and `list()` never surfaces the partial, so the single-writer contract is preserved.
pub async fn save(cp: ControlPlane, job_id: Uuid, name: String, permit: OwnedSemaphorePermit) {
    // Hold the writer permit through the pg_dump run AND the finalize rename — so the gate is
    // never released, and the job is never `Succeeded`, before the final artifact is published
    // (Finding B). `_permit` drops at scope end, AFTER `finalize_save` renames + sets status.
    let _permit = permit;
    let partial = partial_path(&cp, &name);
    let final_path = dump_path(&cp, &name);

    // Drive pg_dump WITHOUT letting the runner own/drop the permit or set `Succeeded` — this
    // runner leaves a successful job `Running` for `finalize_save` to promote.
    let mut cmd = dump_command(&cp, &name);
    let outcome = job::run_command_keep_permit(&cp, job_id, &mut cmd, job::JOB_TIMEOUT).await;
    finalize_save(&cp, job_id, outcome.succeeded, &partial, &final_path);
}

/// Finalize a [`save`] under the still-held writer permit (Finding B): the job is `Succeeded` IFF
/// the final `<name>.dump` artifact exists on disk. On `succeeded`, atomically rename the temp
/// partial to the final path and mark `Succeeded`; if the rename itself fails, remove the temp and
/// mark `Failed` (a half-published artifact is never advertised). On `!succeeded` (the runner
/// already set `Failed`), just remove the temp so no leftover partial remains. Extracted so the
/// "Succeeded ⇔ final exists" invariant is unit-testable DB-free.
fn finalize_save(
    cp: &ControlPlane,
    job_id: Uuid,
    succeeded: bool,
    partial: &std::path::Path,
    final_path: &std::path::Path,
) {
    if succeeded {
        match std::fs::rename(partial, final_path) {
            Ok(()) => job::with_job(cp, job_id, |j| j.status = JobStatus::Succeeded),
            Err(e) => {
                let _ = std::fs::remove_file(partial);
                job::with_job(cp, job_id, |j| {
                    j.push_log(format!(
                        "snapshot save could not finalize (rename temp→final): {e}"
                    ));
                    j.status = JobStatus::Failed;
                });
            }
        }
    } else {
        // Failed/timed-out dump: remove the temp so no leftover partial remains.
        let _ = std::fs::remove_file(partial);
    }
}

/// Build the `pg_dump` command that captures `name`'s artifact to the TEMP sibling (`pg_dump
/// --format=custom --data-only --schema=synthetic --file <snapshots>/<name>.dump.partial`), with
/// credentials via [`pg_env`] (never argv, T-17-05). The caller MUST have safe-name-validated
/// `name`. Dump CONTENT/format is unchanged from the pre-atomic-save version (fingerprint-safe,
/// D-11 reproducibility); only the on-disk target is the temp path, promoted by [`save`].
pub fn dump_command(cp: &ControlPlane, name: &str) -> Command {
    let file = partial_path(cp, name);
    let mut cmd = Command::new("pg_dump");
    cmd.args([
        "--format=custom",
        "--data-only",
        "--schema=synthetic",
        "--file",
    ]);
    cmd.arg(&file);
    for (k, v) in pg_env(&cp.database_url) {
        cmd.env(k, v);
    }
    job::scrub_child_env(&mut cmd); // never inherit the control token (WR-03)
    cmd
}

/// An unpredictable, permission-restricted temp SQL file whose backing file is removed on Drop —
/// on EVERY exit path (success/failure/early-return/cancel/timeout/panic). The decode step writes
/// the validated archive here BEFORE any DB mutation, and the psql apply reads it; the RAII Drop
/// guarantees the decoded SQL never lingers on disk (locked spec). The name is `.restore-<uuid>.sql`
/// so [`list`] (which surfaces only `*.dump`) never advertises it as a restorable archive.
struct TempSqlFile {
    path: PathBuf,
}

impl TempSqlFile {
    /// Create the temp file in `dir` with restricted perms (0o600 on unix). `create(true)` +
    /// a fresh v4 uuid make the name unpredictable; the file is created empty and the decode step
    /// (pg_restore `-f`) overwrites it. A create failure (e.g. full disk / no write perms) is a
    /// pre-mutation error surfaced to the caller — the decode-to-temp-first ordering is itself the
    /// disk-bound guard (a partial decode fails BEFORE any DB mutation).
    fn new(dir: &Path) -> std::io::Result<Self> {
        let path = dir.join(format!(".restore-{}.sql", Uuid::new_v4()));
        let mut opts = std::fs::OpenOptions::new();
        opts.write(true).create(true).truncate(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            opts.mode(0o600);
        }
        opts.open(&path)?; // handle drops immediately; pg_restore reopens to write.
        Ok(TempSqlFile { path })
    }

    fn path(&self) -> &Path {
        &self.path
    }
}

impl Drop for TempSqlFile {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.path);
    }
}

/// The restore runner (CTRL-04, D-05/D-14, Finding A). Delegates to [`restore_with_timeout`] with
/// the production [`job::JOB_TIMEOUT`]; the injectable-timeout variant exists ONLY so tests can
/// drive a short timeout (kill/rollback proof).
pub async fn restore(cp: ControlPlane, job_id: Uuid, name: String, permit: OwnedSemaphorePermit) {
    restore_with_timeout(cp, job_id, name, permit, job::JOB_TIMEOUT).await;
}

/// As [`restore`], but with an injectable wall-clock `timeout`. Under the held write permit:
/// (1) VALIDATE the archive with a fail-closed `pg_restore --list` TOC dry-run (a cheap fast-fail,
/// KEPT); (2) create a server-owned [`TempSqlFile`]; (3) DECODE the validated archive to that temp
/// (`pg_restore --data-only --disable-triggers -f` — a corrupt data block OR a full-disk write
/// fails HERE, BEFORE any DB mutation); (4) GUARD the decoded SQL (no transaction-control /
/// reconnection, streamed — never read into memory); (5) probe the CURRENTLY-existing `synthetic.*`
/// tables; (6) apply via ONE `psql --single-transaction --set=ON_ERROR_STOP=1 -c <TRUNCATE> -f
/// <temp>` — the TRUNCATE and the load share a SINGLE transaction, so ANY load failure (or a
/// kill/timeout dropping the connection) rolls the TRUNCATE back too and the live estate is
/// preserved. The job is `Succeeded` only after psql commits. The temp is removed by RAII on every
/// path; the permit is held throughout (decode → apply → cleanup) and released only at scope end.
///
/// Finding A closes the old "validated-but-incompatible archive empties the estate" window: the old
/// path TRUNCATEd in a SEPARATE committed transaction and only then loaded, so a load failure left
/// the estate empty. Now the TRUNCATE lives INSIDE the psql transaction with the load.
pub async fn restore_with_timeout(
    cp: ControlPlane,
    job_id: Uuid,
    name: String,
    permit: OwnedSemaphorePermit,
    timeout: Duration,
) {
    // Hold the permit through decode → apply → temp cleanup; it drops at scope end.
    let _permit = permit;
    job::with_job(&cp, job_id, |j| j.status = JobStatus::Running);

    let file = dump_path(&cp, &name);
    if !file.is_file() {
        job::with_job(&cp, job_id, |j| {
            j.push_log(format!("snapshot '{name}' not found"));
            j.status = JobStatus::Failed;
        });
        return; // `_permit` drops here → gate released.
    }

    // (1) Validate the TOC BEFORE any decode/mutation — a cheap fail-closed `pg_restore --list`.
    if let Err(e) = validate_archive(&cp, &name).await {
        job::with_job(&cp, job_id, |j| {
            j.push_log(e);
            j.status = JobStatus::Failed;
        });
        return; // untouched estate.
    }

    // (2) Server-owned temp SQL sink (RAII cleanup on EVERY subsequent return path).
    let temp = match TempSqlFile::new(&cp.dirs.snapshots) {
        Ok(t) => t,
        Err(e) => {
            job::with_job(&cp, job_id, |j| {
                j.push_log(format!("could not create restore temp file: {e}"));
                j.status = JobStatus::Failed;
            });
            return;
        }
    };

    // (3) DECODE the validated archive to the temp — the pre-mutation corrupt/full-disk fail point.
    if let Err(e) = decode_archive(&cp, &name, temp.path()).await {
        job::with_job(&cp, job_id, |j| {
            j.push_log(e);
            j.status = JobStatus::Failed;
        });
        return; // temp drops → cleaned; estate untouched.
    }

    // (4) GUARD: the decoded SQL must contain no transaction-control / reconnection (streamed).
    if let Err(e) = guard_no_txn_control_file(temp.path()) {
        job::with_job(&cp, job_id, |j| {
            j.push_log(format!("decoded restore SQL rejected: {e}"));
            j.status = JobStatus::Failed;
        });
        return; // temp drops → cleaned; estate untouched.
    }

    // (5) Which synthetic.* tables currently exist (the in-txn TRUNCATE targets, FK-ordered).
    let existing = match job::existing_synthetic_tables(&cp.pool).await {
        Ok(v) => v,
        Err(e) => {
            job::with_job(&cp, job_id, |j| {
                j.push_log(format!("probe of existing synthetic tables failed: {e}"));
                j.status = JobStatus::Failed;
            });
            return;
        }
    };

    // (6) ONE psql transaction: TRUNCATE (-c, ordered first) + load (-f), all-or-nothing. If no
    // synthetic table exists yet (subset-migration fixture), apply the load alone (no TRUNCATE).
    let mut cmd = if existing.is_empty() {
        psql_apply_command(&cp, None, temp.path())
    } else {
        let truncate = format!("TRUNCATE {} RESTART IDENTITY CASCADE", existing.join(", "));
        restore_apply_command(&cp, &truncate, temp.path())
    };
    let outcome = job::run_command_keep_permit(&cp, job_id, &mut cmd, timeout).await;
    if outcome.succeeded {
        // Succeeded only AFTER psql committed the single TRUNCATE+load transaction. Restore
        // REPLACES the tenant → refresh the served identity to the restored tenant BEFORE
        // publishing `Succeeded` (never report a successful restore while the pre-restore
        // signer is still active). A refresh failure fails the job.
        match job::refresh_signer_for_current_tenant(&cp).await {
            Ok(()) => job::with_job(&cp, job_id, |j| j.status = JobStatus::Succeeded),
            Err(e) => job::with_job(&cp, job_id, |j| {
                j.push_log(format!(
                    "snapshot restored but identity refresh failed: {e}"
                ));
                j.status = JobStatus::Failed;
            }),
        }
    }
    // `temp` drops here (cleaned on every path); `_permit` drops → the write gate is released.
}

/// Build the `pg_restore` DECODE command: `pg_restore --data-only --disable-triggers
/// --schema=synthetic -f <out_sql> <snapshots>/<name>.dump`. It writes the archive's data as SQL
/// to `out_sql` WITHOUT connecting to any database (no `--dbname`) and WITHOUT `--single-transaction`
/// (which would emit `BEGIN`/`COMMIT` into the file — forbidden by the psql apply wrapper + the
/// txn-control guard). `--disable-triggers` emits only `SET session_replication_role` (allowed).
/// `scrub_child_env` still applied (WR-03). The caller MUST have safe-name-validated `name`.
pub fn decode_command(cp: &ControlPlane, name: &str, out_sql: &Path) -> Command {
    let archive = dump_path(cp, name);
    let mut cmd = Command::new("pg_restore");
    cmd.args([
        "--data-only",
        "--disable-triggers",
        "--schema=synthetic",
        "-f",
    ]);
    cmd.arg(out_sql);
    cmd.arg(&archive);
    job::scrub_child_env(&mut cmd); // never inherit the control token (WR-03)
    cmd
}

/// Build the `psql` APPLY command (Finding A): `psql -X --single-transaction
/// --set=ON_ERROR_STOP=1 [-c <truncate_stmt>] -f <sql_file>`, credentials/dbname via [`pg_env`]
/// (never argv, T-17-05), `scrub_child_env` applied. `--single-transaction` wraps the WHOLE session
/// (the optional `-c TRUNCATE` ordered BEFORE the `-f` load) in ONE transaction; `ON_ERROR_STOP`
/// aborts on the first error → a load failure rolls the TRUNCATE back too. `-X` ignores any
/// `~/.psqlrc`. The `truncate_stmt` relation list is built from the STATIC
/// [`job::SYNTHETIC_TABLES`] allowlist (never user input → no injection surface).
fn psql_apply_command(cp: &ControlPlane, truncate_stmt: Option<&str>, sql_file: &Path) -> Command {
    let mut cmd = Command::new("psql");
    cmd.args(["-X", "--single-transaction", "--set=ON_ERROR_STOP=1"]);
    if let Some(stmt) = truncate_stmt {
        cmd.arg("-c");
        cmd.arg(stmt); // ordered before -f: psql runs -c/-f in argv order, one transaction.
    }
    cmd.arg("-f");
    cmd.arg(sql_file);
    for (k, v) in pg_env(&cp.database_url) {
        cmd.env(k, v);
    }
    job::scrub_child_env(&mut cmd); // never inherit the control token (WR-03)
    cmd
}

/// The Finding-A apply command with the in-transaction `TRUNCATE` ordered before the load (the
/// production restore path). A thin wrapper over [`psql_apply_command`] with `Some(truncate_stmt)`
/// so the "TRUNCATE + load share one psql transaction" shape is unit-testable DB-free.
pub fn restore_apply_command(cp: &ControlPlane, truncate_stmt: &str, sql_file: &Path) -> Command {
    psql_apply_command(cp, Some(truncate_stmt), sql_file)
}

/// Run the [`decode_command`] and classify the outcome (Finding A): `Ok(())` if the archive decodes
/// to the temp SQL file (exit 0); `Err(msg)` if `pg_restore` cannot run (missing/unusable binary)
/// OR the archive is corrupt / a data block is unreadable / the write fails (nonzero exit). Runs
/// BEFORE any DB mutation, so a bad archive can never empty the estate. Fails clean, never panics.
async fn decode_archive(cp: &ControlPlane, name: &str, out_sql: &Path) -> Result<(), String> {
    use std::process::Stdio;
    let mut cmd = decode_command(cp, name, out_sql);
    cmd.stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    match cmd.output().await {
        Err(e) => Err(format!(
            "snapshot archive decode could not run (pg_restore missing/unusable): {e}"
        )),
        Ok(out) if out.status.success() => Ok(()),
        Ok(out) => {
            let stderr = String::from_utf8_lossy(&out.stderr);
            Err(format!(
                "snapshot '{name}' could not be decoded (corrupt data block or write failure): {}",
                stderr.trim()
            ))
        }
    }
}

/// Reject any transaction-control (`BEGIN`/`COMMIT`/`ROLLBACK`/`SAVEPOINT`/`START`) or reconnection
/// (`\connect`, `\c`) statement in the decoded restore SQL, so nothing inside the file can break out
/// of the single psql transaction (Finding A). Tracks COPY-data-body state: while inside a `COPY …
/// FROM stdin;` body (until a `\.` terminator line) arbitrary row data is opaque (it may literally
/// contain the word "commit"); outside a COPY body, comments/blanks are skipped, `SET …` /
/// `SELECT pg_catalog.set_config(…)` are allowed, and a transaction-control opener or a reconnection
/// meta-command is rejected. Operates over a line iterator so the caller can STREAM the file (the
/// decoded SQL is never read whole into memory — locked spec).
fn guard_lines(lines: impl Iterator<Item = String>) -> Result<(), String> {
    let mut in_copy = false;
    for line in lines {
        let trimmed = line.trim();
        if in_copy {
            // Inside a COPY data body: only the `\.` terminator line matters; rows are opaque.
            if trimmed == "\\." {
                in_copy = false;
            }
            continue;
        }
        // Enter a COPY data body: `COPY <table> (...) FROM stdin;`.
        if trimmed.starts_with("COPY ") && trimmed.ends_with("FROM stdin;") {
            in_copy = true;
            continue;
        }
        if trimmed.is_empty() || trimmed.starts_with("--") {
            continue;
        }
        // Reject reconnection meta-commands (would attach a fresh, un-wrapped session).
        if trimmed.starts_with("\\connect") || trimmed == "\\c" || trimmed.starts_with("\\c ") {
            return Err(format!("reconnection meta-command: {trimmed}"));
        }
        // Reject transaction-control openers (first token, case-insensitive).
        let first = trimmed
            .split(|c: char| c.is_whitespace() || c == ';')
            .next()
            .unwrap_or("")
            .to_ascii_lowercase();
        if matches!(
            first.as_str(),
            "begin" | "commit" | "rollback" | "savepoint" | "start"
        ) {
            return Err(format!("transaction-control statement: {trimmed}"));
        }
    }
    Ok(())
}

/// Stream `path`'s lines through [`guard_lines`] (BufReader — never the whole decoded SQL into
/// memory, locked spec). A read/open error is surfaced as `Err` (fail-closed: an unreadable
/// decoded file must not be applied).
fn guard_no_txn_control_file(path: &Path) -> Result<(), String> {
    use std::io::BufRead;
    let file = std::fs::File::open(path)
        .map_err(|e| format!("could not open decoded restore SQL for validation: {e}"))?;
    let mut lines = std::io::BufReader::new(file).lines();
    // Drive the reader one line at a time so a mid-file read error is fail-closed (never silently
    // truncated) and the whole decoded SQL is never buffered into memory (locked spec).
    let mut read_err: Option<String> = None;
    let streamed = std::iter::from_fn(|| match lines.next() {
        Some(Ok(l)) => Some(l),
        Some(Err(e)) => {
            read_err = Some(format!("read error while validating decoded SQL: {e}"));
            None
        }
        None => None,
    });
    guard_lines(streamed)?;
    match read_err {
        Some(e) => Err(e),
        None => Ok(()),
    }
}

/// Build the `pg_restore --list <snapshots>/<name>.dump` command: a TOC dry-run that
/// parses the archive header/table-of-contents WITHOUT connecting to any database (no `--dbname`),
/// used by [`validate_archive`] as the fail-closed validate-before-truncate gate. Mirrors the
/// other builders (credentials-free; `scrub_child_env` still applied, WR-03) so it is unit-testable
/// DB-free. The caller MUST have safe-name-validated `name`.
pub fn validate_command(cp: &ControlPlane, name: &str) -> Command {
    let file = dump_path(cp, name);
    let mut cmd = Command::new("pg_restore");
    cmd.arg("--list");
    cmd.arg(&file);
    job::scrub_child_env(&mut cmd); // never inherit the control token (WR-03)
    cmd
}

/// Run the [`validate_command`] TOC dry-run and classify the outcome: `Ok(())` if the
/// archive parses (exit 0); `Err(msg)` if `pg_restore` cannot run (missing/unusable binary) OR the
/// archive is corrupt/truncated (nonzero exit). This runs BEFORE any truncate, so a bad archive
/// leaves the live estate intact. Fails clean, never crashes (D-13): a spawn error is mapped to an
/// `Err`, not a panic. No DB connection is opened.
async fn validate_archive(cp: &ControlPlane, name: &str) -> Result<(), String> {
    use std::process::Stdio;
    let mut cmd = validate_command(cp, name);
    cmd.stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    match cmd.output().await {
        Err(e) => Err(format!(
            "snapshot archive validation could not run (pg_restore missing/unusable): {e}"
        )),
        Ok(out) if out.status.success() => Ok(()),
        Ok(out) => {
            let stderr = String::from_utf8_lossy(&out.stderr);
            Err(format!(
                "snapshot '{name}' is not a valid/complete archive: {}",
                stderr.trim()
            ))
        }
    }
}

/// List the saved snapshots (`GET /_control/snapshots`): the safe-name `*.dump` stems in the
/// server-owned snapshots dir, each with its mtime as Unix seconds. A missing dir / non-file /
/// unsafe-name / non-`.dump` entry is skipped. Sorted most-recent-first (ties broken by name).
pub fn list(cp: &ControlPlane) -> Vec<SnapshotEntry> {
    let mut entries: Vec<SnapshotEntry> = Vec::new();
    let Ok(read) = std::fs::read_dir(&cp.dirs.snapshots) else {
        return entries;
    };
    for entry in read.flatten() {
        let path = entry.path();
        if !path.is_file() {
            continue;
        }
        let is_dump = path
            .extension()
            .and_then(|e| e.to_str())
            .is_some_and(|e| e.eq_ignore_ascii_case("dump"));
        if !is_dump {
            continue;
        }
        let Some(stem) = path.file_stem().and_then(|s| s.to_str()) else {
            continue;
        };
        if !crate::control::is_safe_name(stem) {
            continue;
        }
        let created_unix = entry
            .metadata()
            .and_then(|m| m.modified())
            .ok()
            .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
            .map(|d| d.as_secs() as i64)
            .unwrap_or(0);
        entries.push(SnapshotEntry {
            name: stem.to_string(),
            created_unix,
        });
    }
    entries.sort_by(|a, b| {
        b.created_unix
            .cmp(&a.created_unix)
            .then_with(|| a.name.cmp(&b.name))
    });
    entries
}

/// Delete a snapshot artifact (`DELETE /_control/snapshots/{name}`). The caller MUST have
/// safe-name-validated `name` (T-17-02). Returns the `std::io` result so the handler maps a
/// `NotFound` to a 404 and anything else to a 500.
pub fn delete(cp: &ControlPlane, name: &str) -> std::io::Result<()> {
    std::fs::remove_file(dump_path(cp, name))
}

#[cfg(test)]
mod tests {
    use super::{
        decode_command, dump_command, list, pg_env, restore_apply_command, validate_command,
    };
    use crate::job::{ControlDirs, ControlPlane, JobStatus};
    use std::collections::HashMap;
    use std::path::PathBuf;
    use std::sync::{Arc, Mutex};

    /// Build a DB-free `ControlPlane` whose snapshots dir is `snapshots`, for pure arg-shape /
    /// listing assertions. The pool is a LAZY connection (never dialed) and the token/gate are
    /// placeholders — none of the command builders or `list` touch them.
    fn test_cp(snapshots: PathBuf) -> ControlPlane {
        let pool = sqlx::postgres::PgPool::connect_lazy("postgres://u:p@localhost:5432/db")
            .expect("build a lazy (never-dialed) pool");
        ControlPlane {
            token_digest: [0u8; 32],
            database_url: "postgres://u:p@localhost:5432/db".to_string(),
            repo_root: std::env::temp_dir(),
            dirs: ControlDirs {
                profiles: snapshots.clone(),
                sources: snapshots.clone(),
                snapshots,
            },
            registry: Arc::new(Mutex::new(HashMap::new())),
            write_gate: Arc::new(tokio::sync::Semaphore::new(1)),
            pipeline_cmd: Vec::new(),
            pool,
            signer: crate::jwt::SharedSigner::new(
                crate::jwt::JwtSigner::ephemeral(&uuid::Uuid::nil()).expect("test_cp signer"),
            ),
        }
    }

    /// Collect a `tokio::process::Command`'s argv as owned `String`s for shape assertions.
    fn args_of(cmd: &tokio::process::Command) -> Vec<String> {
        cmd.as_std()
            .get_args()
            .map(|a| a.to_string_lossy().into_owned())
            .collect()
    }

    /// Atomic save: `dump_command` writes pg_dump to the same-directory TEMP sibling
    /// `<name>.dump.partial`, NOT straight to the final `<name>.dump` — so a crashed dump never
    /// leaves a truncated file that `list()`/`restore()` treat as valid.
    #[tokio::test]
    async fn dump_command_writes_to_temp_then_final() {
        let dir =
            std::env::temp_dir().join(format!("tenantless-snap-unit-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).expect("mk snapshots dir");
        let cp = test_cp(dir);
        let cmd = dump_command(&cp, "s1");
        let args = args_of(&cmd);
        let file_pos = args
            .iter()
            .position(|a| a == "--file")
            .expect("--file flag present");
        let file = &args[file_pos + 1];
        assert!(
            file.ends_with("s1.dump.partial"),
            "dump writes to the temp partial, got {file:?}"
        );
    }

    /// Finding A (fail-closed apply): `restore_apply_command` is a `psql` run carrying
    /// `--single-transaction` + `--set=ON_ERROR_STOP=1`, with the `-c TRUNCATE` ordered BEFORE
    /// the `-f` load — so the TRUNCATE and the load share ONE transaction and a load failure rolls
    /// the TRUNCATE back too. Credentials/dbname are NOT in argv (env only).
    #[tokio::test]
    async fn restore_apply_command_is_fail_closed() {
        let dir =
            std::env::temp_dir().join(format!("tenantless-snap-unit-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).expect("mk snapshots dir");
        let cp = test_cp(dir);
        let cmd = restore_apply_command(
            &cp,
            "TRUNCATE synthetic.tenant RESTART IDENTITY CASCADE",
            std::path::Path::new("/tmp/x.sql"),
        );
        assert_eq!(
            cmd.as_std().get_program().to_string_lossy(),
            "psql",
            "apply is a single psql transaction"
        );
        let args = args_of(&cmd);
        assert!(
            args.iter().any(|a| a == "--single-transaction"),
            "apply is fail-closed (--single-transaction), got {args:?}"
        );
        assert!(
            args.iter().any(|a| a == "--set=ON_ERROR_STOP=1"),
            "apply aborts on first error (ON_ERROR_STOP), got {args:?}"
        );
        let c_pos = args
            .iter()
            .position(|a| a == "-c")
            .expect("-c TRUNCATE present");
        let f_pos = args
            .iter()
            .position(|a| a == "-f")
            .expect("-f load present");
        assert!(
            c_pos < f_pos,
            "the TRUNCATE (-c) is ordered BEFORE the load (-f), got {args:?}"
        );
        // The TRUNCATE relation list must not carry the dbname in argv.
        assert!(
            !args.iter().any(|a| a == "--dbname"),
            "dbname travels via PG* env, never argv, got {args:?}"
        );
    }

    /// Finding A (decode step): `decode_command` decodes the validated archive to a SQL file
    /// (`--data-only --disable-triggers -f`) and MUST NOT carry `--single-transaction` — that
    /// would emit BEGIN/COMMIT into the file, which the psql apply's `--single-transaction`
    /// wrapper (and the txn-control guard) forbid.
    #[tokio::test]
    async fn decode_command_is_not_single_transaction() {
        let dir =
            std::env::temp_dir().join(format!("tenantless-snap-unit-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).expect("mk snapshots dir");
        let cp = test_cp(dir);
        let cmd = decode_command(&cp, "s1", std::path::Path::new("/tmp/x.sql"));
        assert_eq!(
            cmd.as_std().get_program().to_string_lossy(),
            "pg_restore",
            "decode is a pg_restore -f to a temp SQL file"
        );
        let args = args_of(&cmd);
        assert!(args.iter().any(|a| a == "--data-only"), "got {args:?}");
        assert!(
            args.iter().any(|a| a == "--disable-triggers"),
            "got {args:?}"
        );
        assert!(
            args.iter().any(|a| a == "-f"),
            "decode writes to a file (-f), got {args:?}"
        );
        assert!(
            !args.iter().any(|a| a == "--single-transaction"),
            "decode must NOT emit BEGIN/COMMIT (no --single-transaction), got {args:?}"
        );
    }

    /// Extra (Finding A guard): the no-transaction-control guard REJECTS BEGIN/COMMIT/ROLLBACK/
    /// SAVEPOINT/START and `\connect`/`\c` OUTSIDE a COPY body, but ACCEPTS `SET
    /// session_replication_role`, comments/blanks, and arbitrary COPY row data (which may
    /// literally contain the word "commit").
    #[test]
    fn guard_rejects_txn_control_accepts_copy_data() {
        // A benign decoded body: a comment, a SET, and a COPY body whose data rows literally
        // contain "commit"/"BEGIN" (opaque inside COPY), then the reset SET.
        let ok = vec![
            "-- Data for Name: tenant; Type: TABLE DATA".to_string(),
            "SET session_replication_role = replica;".to_string(),
            String::new(),
            "COPY synthetic.tenant (id, note) FROM stdin;".to_string(),
            "1\tcommit here".to_string(),
            "2\tBEGIN in data".to_string(),
            "\\.".to_string(),
            "SET session_replication_role = DEFAULT;".to_string(),
            "SELECT pg_catalog.set_config('search_path', '', false);".to_string(),
        ];
        assert!(
            super::guard_lines(ok.into_iter()).is_ok(),
            "SET + comment + COPY data body must be accepted"
        );

        // A bare COMMIT outside COPY → rejected.
        assert!(
            super::guard_lines(["SELECT 1;".to_string(), "COMMIT;".to_string()].into_iter())
                .is_err(),
            "a COMMIT outside COPY is rejected"
        );
        // A reconnection meta-command → rejected.
        assert!(
            super::guard_lines(["\\connect other".to_string()].into_iter()).is_err(),
            "a \\connect reconnection is rejected"
        );
        // Each transaction-control opener → rejected.
        for stmt in [
            "BEGIN;",
            "START TRANSACTION;",
            "ROLLBACK;",
            "SAVEPOINT sp1;",
        ] {
            assert!(
                super::guard_lines(std::iter::once(stmt.to_string())).is_err(),
                "{stmt} must be rejected"
            );
        }
    }

    /// Required-test #7 (RAII): a `TempSqlFile` exists while held and is removed on Drop (every
    /// exit path — success/failure/early-return/panic).
    #[test]
    fn temp_sql_file_removed_on_drop() {
        let dir =
            std::env::temp_dir().join(format!("tenantless-snap-unit-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).expect("mk snapshots dir");
        let p = {
            let t = super::TempSqlFile::new(&dir).expect("create temp sql file");
            assert!(t.path().exists(), "temp exists while the guard is held");
            t.path().to_path_buf()
        };
        assert!(!p.exists(), "temp removed on drop (RAII, every exit path)");
    }

    /// Required-test #5 (restore half): `restore` holds the writer permit across its whole run —
    /// here it fails fast at the not-found guard (before touching the pool/tools), and the gate is
    /// unavailable while it runs and released only after it finalizes.
    #[tokio::test]
    async fn restore_holds_permit_until_finalize() {
        let dir =
            std::env::temp_dir().join(format!("tenantless-snap-unit-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).expect("mk snapshots dir");
        let cp = test_cp(dir);
        let id = register_job(&cp, JobStatus::Queued);

        let permit = cp
            .write_gate
            .clone()
            .try_acquire_owned()
            .expect("first permit free");
        assert!(
            cp.write_gate.clone().try_acquire_owned().is_err(),
            "the write gate is exhausted while restore holds the permit"
        );

        // A snapshot that does not exist → restore fails at the is_file guard, before any tool.
        super::restore(cp.clone(), id, "does-not-exist".to_string(), permit).await;

        assert!(
            cp.write_gate.clone().try_acquire_owned().is_ok(),
            "restore releases the permit only after it finalizes"
        );
        assert_eq!(
            job_status(&cp, id),
            JobStatus::Failed,
            "missing snapshot → Failed"
        );
    }

    /// Validate before truncate: the `validate_command` is a `pg_restore --list`
    /// TOC dry-run over the FINAL `<name>.dump` archive (the committed artifact, never the
    /// partial) — no DB connection needed.
    #[tokio::test]
    async fn validate_command_lists_final_archive() {
        let dir =
            std::env::temp_dir().join(format!("tenantless-snap-unit-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).expect("mk snapshots dir");
        let cp = test_cp(dir);
        let cmd = validate_command(&cp, "s1");
        assert_eq!(
            cmd.as_std().get_program().to_string_lossy(),
            "pg_restore",
            "validation is a pg_restore TOC dry-run"
        );
        let args = args_of(&cmd);
        assert!(
            args.iter().any(|a| a == "--list" || a == "-l"),
            "validation lists the TOC (--list), got {args:?}"
        );
        assert!(
            args.iter()
                .any(|a| a.ends_with("s1.dump") && !a.ends_with(".partial")),
            "validation reads the FINAL committed archive, got {args:?}"
        );
    }

    /// Atomic save: `list()` never surfaces an in-progress `<name>.dump.partial` artifact — only the
    /// committed `<name>.dump` stems appear.
    #[tokio::test]
    async fn list_excludes_partial_artifacts() {
        let dir =
            std::env::temp_dir().join(format!("tenantless-snap-unit-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).expect("mk snapshots dir");
        std::fs::write(dir.join("s1.dump"), b"final").expect("write final");
        std::fs::write(dir.join("s1.dump.partial"), b"partial").expect("write partial");
        let cp = test_cp(dir);
        let names: Vec<String> = list(&cp).into_iter().map(|e| e.name).collect();
        assert_eq!(
            names,
            vec!["s1".to_string()],
            "the partial is never surfaced"
        );
    }

    /// T-17-05: the standard dev DSN maps to the expected PG* env (credentials via env, not
    /// argv). Password + user + host + port + dbname all extracted.
    #[test]
    fn pg_env_parses_standard_dsn() {
        let env = pg_env("postgres://tenantless:tenantless_dev@localhost:5433/tenantless");
        let get = |k: &str| env.iter().find(|(ek, _)| *ek == k).map(|(_, v)| v.as_str());
        assert_eq!(get("PGUSER"), Some("tenantless"));
        assert_eq!(get("PGPASSWORD"), Some("tenantless_dev"));
        assert_eq!(get("PGHOST"), Some("localhost"));
        assert_eq!(get("PGPORT"), Some("5433"));
        assert_eq!(get("PGDATABASE"), Some("tenantless"));
    }

    /// A percent-encoded password decodes; a `?param` query tail is stripped from the dbname.
    #[test]
    fn pg_env_decodes_password_and_strips_query() {
        let env = pg_env("postgres://u:p%40ss@host:5432/db?sslmode=disable");
        let get = |k: &str| env.iter().find(|(ek, _)| *ek == k).map(|(_, v)| v.as_str());
        assert_eq!(get("PGPASSWORD"), Some("p@ss"), "percent-decoded password");
        assert_eq!(get("PGDATABASE"), Some("db"), "query tail stripped");
    }

    /// A bracketed IPv6 host keeps its inner colons; the trailing `:port` is still split off.
    #[test]
    fn pg_env_handles_ipv6_host() {
        let env = pg_env("postgres://u:p@[::1]:5432/db");
        let get = |k: &str| env.iter().find(|(ek, _)| *ek == k).map(|(_, v)| v.as_str());
        assert_eq!(get("PGHOST"), Some("::1"));
        assert_eq!(get("PGPORT"), Some("5432"));
    }

    // ----- Task 2 (Finding B — atomic save finalization) -----

    /// As [`test_cp`] but with a caller-chosen `database_url` — for the save permit-lifecycle
    /// test that needs a fail-fast (connection-refused) DSN.
    fn test_cp_dsn(snapshots: PathBuf, database_url: &str) -> ControlPlane {
        let mut cp = test_cp(snapshots);
        cp.database_url = database_url.to_string();
        cp
    }

    /// Register a fresh job in `cp`'s registry with the given status and return its id.
    fn register_job(cp: &ControlPlane, status: JobStatus) -> uuid::Uuid {
        let mut job = crate::job::Job::new(crate::job::JobKind::Snapshot);
        job.status = status;
        let id = job.id;
        cp.registry.lock().unwrap().insert(id, job);
        id
    }

    /// Read a registered job's status.
    fn job_status(cp: &ControlPlane, id: uuid::Uuid) -> JobStatus {
        let mut s = None;
        crate::job::with_job(cp, id, |j| s = Some(j.status));
        s.expect("job registered")
    }

    /// Required-test #6: a save reaches `Succeeded` IFF the final `<name>.dump` exists.
    /// (A) a present partial renames to final → `Succeeded`, final exists, partial gone;
    /// (B) a MISSING partial (rename fails) → `Failed`, no final; (C) `!succeeded` → stays
    /// `Failed`, partial removed, no final.
    #[tokio::test]
    async fn finalize_save_succeeded_iff_final_exists() {
        let dir =
            std::env::temp_dir().join(format!("tenantless-snap-unit-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).expect("mk snapshots dir");
        let cp = test_cp(dir.clone());
        let partial = dir.join("s.dump.partial");
        let final_path = dir.join("s.dump");

        // (A) succeeded + partial present → Succeeded, final exists, partial gone.
        std::fs::write(&partial, b"dump-bytes").expect("write partial");
        let id_a = register_job(&cp, JobStatus::Running);
        super::finalize_save(&cp, id_a, true, &partial, &final_path);
        assert_eq!(job_status(&cp, id_a), JobStatus::Succeeded);
        assert!(final_path.exists(), "final artifact exists after finalize");
        assert!(!partial.exists(), "partial renamed away");

        // (B) succeeded but NO partial on disk → rename fails → Failed, no final.
        let final_b = dir.join("b.dump");
        let partial_b = dir.join("b.dump.partial"); // never created
        let id_b = register_job(&cp, JobStatus::Running);
        super::finalize_save(&cp, id_b, true, &partial_b, &final_b);
        assert_eq!(job_status(&cp, id_b), JobStatus::Failed);
        assert!(!final_b.exists(), "no final artifact when the rename fails");

        // (C) !succeeded (keep_permit already set Failed) → stays Failed, partial removed.
        let final_c = dir.join("c.dump");
        let partial_c = dir.join("c.dump.partial");
        std::fs::write(&partial_c, b"dump-bytes").expect("write partial c");
        let id_c = register_job(&cp, JobStatus::Failed);
        super::finalize_save(&cp, id_c, false, &partial_c, &final_c);
        assert_eq!(job_status(&cp, id_c), JobStatus::Failed);
        assert!(!partial_c.exists(), "failed save removes the partial");
        assert!(!final_c.exists(), "failed save leaves no final");
    }

    /// Required-test #5 (save half): `save` holds the writer permit across the pg_dump run AND
    /// the finalize — the gate is unavailable while save runs and released only after finalize.
    #[tokio::test]
    async fn save_holds_permit_until_finalize() {
        let dir =
            std::env::temp_dir().join(format!("tenantless-snap-unit-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).expect("mk snapshots dir");
        // Connection-refused DSN → pg_dump fails fast (or spawn-fails if pg_dump is absent).
        let cp = test_cp_dsn(dir.clone(), "postgres://u:p@127.0.0.1:1/db");
        let id = register_job(&cp, JobStatus::Queued);

        // Take the sole permit — a second acquire must fail while it is held.
        let permit = cp
            .write_gate
            .clone()
            .try_acquire_owned()
            .expect("first permit free");
        assert!(
            cp.write_gate.clone().try_acquire_owned().is_err(),
            "the write gate is exhausted while the permit is held"
        );

        super::save(cp.clone(), id, "x".to_string(), permit).await;

        // The permit is released only AFTER save finalized (rename/cleanup done).
        assert!(
            cp.write_gate.clone().try_acquire_owned().is_ok(),
            "save releases the permit after finalize"
        );
        assert_eq!(
            job_status(&cp, id),
            JobStatus::Failed,
            "unreachable DSN → Failed"
        );
        assert!(!dir.join("x.dump").exists(), "no final artifact on failure");
        assert!(
            !dir.join("x.dump.partial").exists(),
            "the temp partial is cleaned up on failure"
        );
    }
}
