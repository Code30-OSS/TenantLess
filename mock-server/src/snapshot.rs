//! `pg_dump`/`pg_restore` snapshot orchestration (Phase 17-04, CTRL-04 / D-04/D-05/D-13/D-14).
//!
//! Named snapshots capture and restore the FULL served `synthetic.*` state (including
//! `drift_records`/`drift_batches`, D-14) as server-owned, safe-name artifacts under
//! [`ControlDirs::snapshots`]. `save` runs `pg_dump --format=custom --data-only
//! --schema=synthetic`; `restore` TRUNCATEs `synthetic.*` under the write lock then
//! `pg_restore --data-only --disable-triggers` (the running server then serves the restored
//! tenant hot — no restart, D-05). Both run as tracked jobs through the SAME single-writer
//! gate as generate/reset (D-11); a MISSING `pg_dump`/`pg_restore` binary ends the job
//! `failed` with a clear log, NEVER a crash (D-13, Pitfall 4 — the default state on a box
//! without `postgresql-client`).
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

use std::path::PathBuf;
use std::time::UNIX_EPOCH;

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

/// The save runner (CTRL-04, D-13/D-14): `pg_dump --format=custom --data-only
/// --schema=synthetic --file <snapshots>/<name>.dump`, credentials via [`pg_env`]. Driven by
/// [`job::run_command`], so a missing `pg_dump` binary OR a nonzero exit ends the job `failed`
/// with the captured stderr — never a crash. `--data-only` keeps the migration-managed schema
/// intact; custom format captures every `synthetic.*` table (incl. drift) in FK/TOC order.
/// The `_permit` moves into `run_command` and releases the write gate on completion (D-11).
pub async fn save(cp: ControlPlane, job_id: Uuid, name: String, permit: OwnedSemaphorePermit) {
    let cmd = dump_command(&cp, &name);
    job::run_command(cp, job_id, cmd, permit).await;
}

/// Build the `pg_dump` command that captures `name`'s artifact (`pg_dump --format=custom
/// --data-only --schema=synthetic --file <snapshots>/<name>.dump`), with credentials via
/// [`pg_env`] (never argv, T-17-05). The caller MUST have safe-name-validated `name`.
pub fn dump_command(cp: &ControlPlane, name: &str) -> Command {
    let file = dump_path(cp, name);
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

/// The restore runner (CTRL-04, D-05/D-14, Pitfall 5): under the held write permit, TRUNCATE
/// every `synthetic.*` table (FK-safe) then `pg_restore --data-only --disable-triggers
/// --schema=synthetic --dbname <db> <snapshots>/<name>.dump` (credentials via [`pg_env`]).
/// `--disable-triggers` defers FK checks during the data-only load (the connecting role owns
/// `synthetic.*`); TRUNCATE-first guarantees no stale rows conflict. The subprocess is driven
/// by [`job::run_command`] so a missing binary / nonzero exit ends the job `failed`, never a
/// crash. The running server reads the pool per-request, so the restored tenant is served hot
/// (no restart, D-05). NOTE: because TRUNCATE runs before pg_restore, a restore that then fails
/// leaves the tenant empty (dirty) — recover via reset/regenerate (D-15).
pub async fn restore(cp: ControlPlane, job_id: Uuid, name: String, permit: OwnedSemaphorePermit) {
    job::with_job(&cp, job_id, |j| j.status = JobStatus::Running);

    let file = dump_path(&cp, &name);
    if !file.is_file() {
        job::with_job(&cp, job_id, |j| {
            j.push_log(format!("snapshot '{name}' not found"));
            j.status = JobStatus::Failed;
        });
        return; // `permit` drops here → gate released.
    }

    // TRUNCATE synthetic.* under the permit (Rust twin of writer.truncate_synthetic).
    if let Err(e) = job::truncate_synthetic(&cp.pool).await {
        job::with_job(&cp, job_id, |j| {
            j.push_log(format!("truncate before restore failed: {e}"));
            j.status = JobStatus::Failed;
        });
        return; // `permit` drops here → gate released.
    }

    // run_command finalizes the job on the child's exit code + releases the permit on drop.
    let cmd = restore_command(&cp, &name);
    job::run_command(cp, job_id, cmd, permit).await;
}

/// Build the `pg_restore` command that loads `name`'s artifact (`pg_restore --data-only
/// --disable-triggers --schema=synthetic --dbname <db> <snapshots>/<name>.dump`), with
/// credentials via [`pg_env`] (never argv, T-17-05). The caller MUST have safe-name-validated
/// `name` (and TRUNCATEd `synthetic.*` first — the restore is data-only).
pub fn restore_command(cp: &ControlPlane, name: &str) -> Command {
    let file = dump_path(cp, name);
    let env = pg_env(&cp.database_url);
    let dbname = env
        .iter()
        .find(|(k, _)| *k == "PGDATABASE")
        .map(|(_, v)| v.clone())
        .unwrap_or_default();

    let mut cmd = Command::new("pg_restore");
    cmd.args([
        "--data-only",
        "--disable-triggers",
        "--schema=synthetic",
        "--dbname",
    ]);
    cmd.arg(&dbname);
    cmd.arg(&file);
    for (k, v) in env {
        cmd.env(k, v);
    }
    job::scrub_child_env(&mut cmd); // never inherit the control token (WR-03)
    cmd
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
    use super::pg_env;

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
}
