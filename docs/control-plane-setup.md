# Control-Plane Setup & Operations

The **control plane** is the simulator's first write surface: an opt-in, token-gated
`/_control/*` API (and its React `/ui/control-plane` operator screens) that drives the
Python `generate` / `analyze` pipeline as tracked async jobs, resets the served tenant to
empty, and saves / restores / deletes named snapshots — all against the single active
`synthetic.*` tenant the ARM mock serves.

Everything else in the server stays **read-only** and **ARM byte-identical**. The control
plane is **absent by default**: a plain `tenantless serve` exposes no `/_control` routes at
all (they 404), so the default scanner-demo posture is unchanged.

Because it is the only mutation and subprocess-orchestration surface in the project, the
control plane is held to a stricter security posture than the read-only ARM API (see §7).

> **Runtime requirement — the control plane runs on a source host, not the slim image.**
> The generate and analyze jobs shell out to `uv run tenantless`, so the server must run
> where **uv, Python, and the Tenantless package/repository** are installed. The slim,
> server-only Docker image contains only the Rust binary (no uv, no Python generator), so
> its control plane cannot run generate or analyze. Save/restore snapshots separately require
> the PostgreSQL client tools (`pg_dump` / `pg_restore`) — see §4.

---

## 1. Arming the control plane (fail-closed)

The control plane arms **only** when **BOTH** conditions hold:

1. `--enable-control-plane` is set, **AND**
2. a **non-empty** control token is configured.

```bash
# HTTP (default port 8080)
uv run tenantless serve \
  --enable-control-plane \
  --control-token demo-secret

# or supply the token via env (never appears in the process list / shell history)
TENANTLESS_CONTROL_TOKEN=demo-secret \
  uv run tenantless serve --enable-control-plane
```

Fail-closed behavior:

| Startup flags | Result |
|---------------|--------|
| _(neither flag)_ | Read-only server. `/_control/*` routes are **absent (404)**. |
| `--enable-control-plane` **without** a token | **Startup error** naming both `--control-token` and `TENANTLESS_CONTROL_TOKEN`. Never arms without a secret. |
| `--enable-control-plane` **+** non-empty token | Armed. `/_control/*` is merged on the bearer-exempt seam; the three server-owned control-data subdirs are created. |

Relevant flags (all also settable via env):

| Flag | Env | Default | Purpose |
|------|-----|---------|---------|
| `--enable-control-plane` | `ENABLE_CONTROL_PLANE` | `false` | Opt-in arming toggle. |
| `--control-token <secret>` | `TENANTLESS_CONTROL_TOKEN` | _(none)_ | The admin secret. Non-empty required to arm. |
| `--control-data-dir <path>` | `CONTROL_DATA_DIR` | `./control-data` | Roots the `profiles/`, `sources/`, `snapshots/` subdirs. |
| `--port <n>` | `PORT` | `8080` | HTTP port. TLS (`--tls`) defaults to `8443`. |

Arming does **not** re-mint the ARM JWKS signer and does **not** change any ARM route —
`arm_byte_identical` stays green with `/_control` merged (verified in the pre-flight below).

---

## 2. Two distinct auth realms

The control plane authenticates on a **separate realm** from the ARM API — do not conflate
them:

| Realm | Header | Rule |
|-------|--------|------|
| **ARM API** (`/subscriptions`, resource routes, …) | `Authorization: Bearer <anything>` | Any non-empty Bearer passes (the placeholder `Bearer tenantless-ui` the UI sends). Deliberately weak — read-only scanner surface. |
| **Bearer-exempt surfaces** (`/_sim`, `/ui`, `/_console`, `/token` + JWKS) | *(none)* | **No auth.** These are merged outside the ARM Bearer layer, so they respond without any `Authorization` header — the console must load in a plain browser, and `/_sim` exposes read-only simulation metadata (e.g. `/_sim/summary`). If exposing the server beyond localhost, gate these at your proxy. |
| **Control plane** (`/_control/*`) | `X-Control-Token: <secret>` | Must **exactly** match the configured control token. Compared as a SHA-256 digest with a constant-time `subtle::ct_eq` (no value/length leak). Wrong/missing → fixed `401 InvalidControlToken`. |

The raw token is **never stored** (only its digest) and **never logged**. In the browser the
token is held **in memory only** — never in `localStorage`, a cookie, a query key, or the URL.

---

## 3. Server-owned control-data directories

Armed startup creates three subdirectories under `--control-data-dir` (default
`./control-data`). All names inside them are **safe-name allowlisted** (`[A-Za-z0-9_-]+`,
no path separators, no `..`) — the control plane **never** resolves an arbitrary path, and
there is **no upload endpoint**.

```
control-data/
├── profiles/    # derived profiles written by `analyze` (safe-name *.json) → feed the generate PROFILE allowlist
├── sources/     # operator-populated DuckDB analyze inputs (safe-name *.duckdb)
└── snapshots/   # pg_dump artifacts written by "save snapshot" (safe-name *.dump)
```

**Populating `sources/` for analyze:** the analyze form's SOURCE dropdown lists the
safe-name `*.duckdb` stems the server finds in `control-data/sources/`. To make a source
available, drop a DuckDB scan file into that directory before (or while) the server runs, e.g.:

```bash
cp /path/to/my-scan.duckdb ./control-data/sources/my-scan.duckdb
```

It then appears in the analyze SOURCE select. There is **no** path input and **no** upload —
this is the entire trust boundary for analyze inputs. The generate PROFILE select likewise
offers the bundled `enterprise` / `small` profiles **plus** any safe-name `*.json` in
`control-data/profiles/` (typically the output of a prior analyze run).

> **Denylist note:** analyze is spawned **without** `--allow-no-denylist`, so a source
> with no matching denylist fail-closes to a clean `failed` job. Live-`azure:` sources,
> credentials, and the denylist UX are deferred to a separate security-reviewed surface.

---

## 4. Snapshots require the PostgreSQL client tools

Save / restore snapshots shell out to **`pg_dump`** and **`pg_restore`** under the
single-writer lock:

- **save** → `pg_dump --format=custom --data-only --schema=synthetic` into
  `control-data/snapshots/<name>.dump`.
- **restore (select)** → `TRUNCATE synthetic.*` then `pg_restore --data-only
  --disable-triggers --schema=synthetic`, hot-swapped into the running server (no restart).
- A snapshot captures the **full served state** including applied config drift
  (`drift_records` + `drift_batches`), so restore reproduces exactly what was being served.

### Dependency

`pg_dump` / `pg_restore` are **not** part of the server binary — they ship with the
PostgreSQL client tools and must be on `PATH`:

| Platform | Install |
|----------|---------|
| Debian / Ubuntu | `apt-get install postgresql-client` |
| RHEL / Fedora | `dnf install postgresql` |
| macOS (Homebrew) | `brew install postgresql` (or `libpq` + add its `bin/` to `PATH`) |
| Windows | Install the PostgreSQL client bundle and add its `bin\` to `PATH` |

**Restore role requirement:** `pg_restore --disable-triggers` requires the connecting role to
**own** `synthetic.*` (the dev/superuser role does). If your role cannot disable triggers,
restore falls back to failing cleanly rather than crashing.

### Missing-binary behavior (first-class, not a crash)

If `pg_dump` / `pg_restore` are **absent** (or exit non-zero), the snapshot job ends
`failed` with the captured stderr in its log — **the server stays up and keeps serving**.
This is a deliberately first-class path (the tools are absent on many dev boxes). Every
**non-snapshot** control feature — generate, analyze, reset, jobs, auth — works **without**
the client tools. Credentials are passed to the tools via `PGHOST`/`PGPORT`/`PGUSER`/
`PGDATABASE`/`PGPASSWORD` env vars derived from the server DSN, **never** in argv (the
process list is world-readable).

---

## 5. Single-writer lock & the brief read-block trade

One in-process semaphore serializes **all** destructive control jobs — generate, reset, and
restore. At most one runs at a time; a second destructive request while one is in flight
returns **`409 ControlBusy`** immediately (the UI reflects this as a busy lock that disables
every start-action). The permit is held for the whole job and auto-releases on completion,
failure, or panic.

Because ARM reads are short per-request transactions (no long-lived transaction on
`synthetic.*`), the `TRUNCATE` / `pg_restore` (`ACCESS EXCLUSIVE`) acquires quickly and
**in-flight ARM reads briefly block** during the write — the **explicitly accepted trade**,
not a full maintenance-mode quiesce.

**Operator responsibility caveat:** the lock is **in-process only**. It does **not**
coordinate with out-of-band CLI mutation. Running `uv run tenantless generate` (or any direct
`synthetic.*` write) from a shell **while the armed server is running** is **operator
responsibility** — cross-process `pg_advisory_lock` coordination is deferred. Drive
generate/reset/restore **through the control plane** while the server is up.

---

## 6. Dirty-tenant recovery

The job state machine is `queued → running → succeeded | failed` — there is **no user
cancel**, and a **wall-clock timeout** is a terminal `failed` state with the same guidance below.

If a destructive job (generate / reset / restore) **fails mid-write**, or the **server is
killed** mid-`TRUNCATE`/write, the active tenant may be left **dirty** (half-written). This is
acceptable for a local dev/control surface and the recovery is deterministic:

1. **Reset to empty**, then **regenerate** — `POST /_control/reset` (or the Tenants screen's
   "Reset to empty") wipes `synthetic.*` to a blank simulator; then run a fresh generate.
   The empty tenant is a **first-class** state: ARM lists return `{value:[]}`, detail returns
   `404`, `/_sim/summary` returns zeros / no active tenant, and the server **boots** against
   an initialized-but-empty schema.
2. **Or restore a known-good snapshot** — if you saved one before the failed run, "select" it
   to hot-swap the last good state back in (predictable "put it back how it was", including
   any drift that was applied at save time).

Job history is **in-memory and ephemeral** — it resets on server restart. There is no
persisted job/audit table.

---

## 7. Security posture

The control plane is the highest-risk surface in the project because it is the only mutation
and subprocess-orchestration path. Summary of the shipped mitigations:

| Threat | Mitigation (shipped) |
|--------|----------------------|
| Elevation — disarmed default | A plain `serve` exposes **no** `/_control` routes (404); arming is opt-in + fail-closed. |
| Spoofing — token gate | SHA-256 + constant-time `subtle::ct_eq`, fixed `401`, token digest-only + never logged. |
| Tampering — path traversal | Every profile / source / snapshot name is safe-name allowlisted **before** any fs / subprocess touch; no arbitrary paths, no upload. |
| Tampering — DSN in argv | Snapshot credentials travel via `PG*` env, never argv. |
| DoS / Integrity — dirty tenant | Single-writer 409 + documented reset/regenerate recovery (§5, §6). |
| Tampering — ARM contract | `arm_byte_identical` green with `/_control` merged; empty-tenant ARM reads stay valid. |
| DoS — oversized generate | Hard server-side caps validated **before** spawn (resources ≤ 500,000; subscriptions ≤ 5,000; `--jobs` ≤ available parallelism; seed fits i64) → fixed `400`, no subprocess. |

### Dependency advisories

Dependency advisories are enforced in CI via `.github/workflows/audit.yml`: `cargo audit` at
the workspace root and `npm audit --audit-level=high` in `frontend/`. A single scoped Rust
advisory exception — `RUSTSEC-2023-0071`, the RSA "Marvin" timing side-channel — is documented
in `.cargo/audit.toml`. It is load-bearing only because the server generates an ephemeral RSA
key and uses it to **sign and verify** RS256 JWTs; it never performs RSA **decryption**, so the
timing oracle the advisory describes is absent. Vulnerability reporting goes through GitHub
private vulnerability reporting per `SECURITY.md`.

---

## 8. Quick operator loop

```bash
# 1. (re-)seed a tenant to serve   [running the Python test suite TRUNCATES the dev DB — re-seed first]
uv run tenantless generate --profile enterprise --subscriptions 20 --resources 5000 --seed 1 --cost-as-of 2026-01-01 --force

# 2. arm the server
uv run tenantless serve --enable-control-plane --control-token demo-secret
#    (optional) drop a DuckDB file into ./control-data/sources/ to exercise analyze

# 3. open the operator UI — SAME-ORIGIN embed on the axum server's own port :8080
#    (this is NOT the Vite dev server, which runs on :5173)
#    http://localhost:8080/ui/control-plane   (or your --port / TLS :8443)
#    enter the control token → generate / analyze / watch jobs / save·select·delete snapshots / reset
```

## See also

- `SECURITY.md` — vulnerability reporting and the dependency-advisory policy.
- `docs/architecture.md` — how the control plane fits the overall server design.
- `README.md` — project overview and getting started.
