<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/tenantless-logo-dark.svg">
    <img src="docs/assets/tenantless-logo.svg" alt="TenantLess" width="400">
  </picture>
</p>

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Release](https://img.shields.io/github/v/tag/Code30-OSS/TenantLess?label=release)](https://github.com/Code30-OSS/TenantLess/releases)
[![Security Audit](https://github.com/Code30-OSS/TenantLess/actions/workflows/audit.yml/badge.svg)](https://github.com/Code30-OSS/TenantLess/actions/workflows/audit.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)](pyproject.toml)
[![Rust](https://img.shields.io/badge/rust-2024%20edition-CE422B.svg)](mock-server/Cargo.toml)
[![Azure](https://img.shields.io/badge/Azure-ARM%20API%20mock-0078D4.svg)](docs/compatibility.md)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED.svg)](docker-compose.yml)

**Tenantless simulates statistically realistic Azure estates for scanner, governance,
FinOps, drift and cross-subscription risk testing.**

It generates a synthetic Azure tenant from statistical distributions and serves it over
ARM-compatible HTTP, so an unmodified scanner, policy engine or cost tool can run against
it as if it were a real tenant — over the covered endpoints and response shapes a discovery
scan exercises, with matching pagination and error envelopes. It implements that covered
surface, not all of Azure ARM. Nothing it generates is running; everything it generates is
*shaped* like production.

Reproducible from `(profile, seed, cost-as-of)`: the same inputs produce a byte-identical
estate, and parallel generation (`--jobs N`) is byte-identical to single-threaded
(`--jobs 1`). Cost billing periods derive from `--cost-as-of`, so pin it whenever a run
needs to be reproducible.

**Scope — read-only, covered surface.** Tenantless serves the *covered* ARM
management-plane endpoints and JSON shapes a discovery scan exercises (list, detail,
`$filter`, Cost Management query, RBAC) — not the whole of Azure ARM. The surface is
**read-only**: there is no `PUT`/`DELETE` and no live-resource behavior, so a
`terraform apply` will not persist against it.

## Why

Testing anything that reads an Azure estate leaves you two bad options:

- **Point at a real tenant** — expensive, slow, irreproducible, and it only contains the
  problems it happens to have.
- **Hand-write fixtures** — reproducible, but they encode what you already thought of, at a
  scale that never exposes what scale actually breaks.

Tenantless is the third option: a tenant you can *specify*. Ask for 300 subscriptions with
a hub-and-spoke topology, an 8% rate of storage accounts allowing public blob access, and a
long tail of resource types — then scan it, break it, regenerate it, and get the same thing
back tomorrow.

## What it does

| Plane | What you get |
|-------|--------------|
| **Management (ARM)** | List / detail / `$filter` with pagination and arbitrary-depth nested resource types; ARM `CloudError` shapes |
| **FinOps** | Cost Management Query API over a generated cost fact table |
| **Identity** | AAD/Entra token issuance (RS256/JWKS), 8 built-in RBAC roles, role assignments, injectable over-privilege |
| **Drift** | Deterministic configuration drift a re-scan detects — and can revert |
| **Topology** | Cross-subscription dependencies: hub-spoke peering, shared Key Vaults, centralized logging, shared registries, private endpoints |
| **Governance** | 18 injectable violation types across severities, at rates you control |
| **Web console** | Browser UI to generate, snapshot, restore and inspect estates, plus a scanner demo |

The committed benchmark measures 145K resources across 300 subscriptions at p95 latencies of
1.7–5.6 ms, generated at ~1,970 resources/sec — reproducible from the bundled synthetic
profile with `scripts/bench_arm_latency.py`. See [`docs/benchmarks/`](docs/benchmarks/).
Latency is machine-dependent; treat it as a shape, not a promise about your hardware.

## Architecture

```
  ┌──────────────┐      ┌───────────────────────────┐      ┌──────────────────────┐
  │ Seed Analyzer│  →   │ Synthetic Tenant Generator│  →   │  ARM API Mock Server │
  │   (Python)   │      │         (Python)          │      │        (Rust)        │
  └──────────────┘      └───────────────────────────┘      └──────────────────────┘
   a statistical          a synthetic tenant in              ARM-compatible HTTP
   profile                PostgreSQL (subs/RGs/              + web console
   (distributions only)   resources/costs/RBAC/drift)
```

- **Seed Analyzer** distills a scan into a *statistical profile* — distributions,
  archetypes, co-occurrence — never raw identifiers. The data boundary is enforced by a
  denylist scan, minimum-aggregation thresholds and per-extractor leak tests.
- **Generator** inverts a profile into a full synthetic tenant written to PostgreSQL via
  binary `COPY`.
- **Mock Server** serves it over ARM-compatible REST so an existing scanner walks it
  unmodified.

Design detail lives in [`docs/architecture.md`](docs/architecture.md).

## Quickstart (Docker-assisted)

Requires [Docker](https://docs.docker.com/get-docker/) and [uv](https://docs.astral.sh/uv/)
on the host. **Node and Rust are supplied by the Docker build stages** — you do not need
them installed to follow this path.

```bash
# 1. Copy the example env
cp .env.example .env

# 2. Start PostgreSQL (port 5433 to avoid clashing with a local PG)
docker compose up -d postgres-sim

# 3. Install the Python toolchain
uv sync

# 4. Generate a synthetic estate — pin --cost-as-of so the run is reproducible
uv run tenantless generate --profile enterprise --seed 42 --cost-as-of 2026-01-01

# 5. Build and start the mock server via Docker Compose (the image builds the
#    frontend and the Rust binary in its own stages)
docker compose up -d --build mock-server

# 6. Scan it like a real ARM tenant (presence-only Bearer auth by default)
curl -H "Authorization: Bearer anything" \
  "http://localhost:8080/subscriptions?api-version=2022-12-01"
```

Then open <http://localhost:8080/ui> for the web console.

> **Compose alone serves an *empty* estate.** `docker compose up -d` starts PostgreSQL and
> the mock server, but a fresh Compose database is empty and the slim server image does not
> contain the Python generator — it does **not** generate an estate. Populate one first with
> the host-side `uv run tenantless generate` step (step 4). The control plane's generate and
> analyze actions also shell out to `uv run tenantless`, so they need a source-host
> deployment (uv, Python, and the Tenantless package/repository) — they do **not** run inside
> the slim server image.

`generate` accepts a bundled profile name (`enterprise`, `small`) or a path to your own
profile JSON, plus `--subscriptions` / `--resources` to set the scale, `--seed` and
`--cost-as-of` for reproducibility.

## Quickstart (source development)

Build everything from source. Prerequisites: **PostgreSQL 16**, **uv** (Python 3.11+),
**Node 20+ / npm**, and **Rust 1.95+ / Cargo**.

```bash
# 1. Env + a reachable PostgreSQL 16 (Docker is one option; see "Bring your own" below)
cp .env.example .env
docker compose up -d postgres-sim

# 2. Python toolchain
uv sync

# 3. Build the web console FIRST — the Rust build embeds frontend/dist at compile time
#    and fails if it is missing (frontend/dist is not tracked)
cd frontend && npm ci && npm run build && cd ..

# 4. Build the Rust server
cargo build --release -p tenantless-server

# 5. Generate an estate (pin --cost-as-of for reproducibility)
uv run tenantless generate --profile enterprise --seed 42 --cost-as-of 2026-01-01

# 6. Serve it over ARM-compatible HTTP on :8080 (add --tls for HTTPS on :8443)
uv run tenantless serve
```

`tenantless serve` discovers the server binary in order — first on `PATH`, then the repo's
`target/release` or `target/debug`, then a `cargo run` fallback. The `cargo run` fallback
compiles the Rust server, which embeds `frontend/dist` at build time, so **if you skip
step 3 the build fails** until the frontend has been built. Building the binary explicitly
(step 4) keeps `serve` fast and avoids the implicit compile.

A guided end-to-end walkthrough — generate, serve, scan, drift, revert — is in
[`docs/demo.md`](docs/demo.md).

## Bring your own PostgreSQL (Docker optional)

Docker is **not** required. You need **PostgreSQL 16** — Docker is just one convenient way
to get it. Any reachable PG16 works: pick whichever fits your setup.

1. **Native install** — a local PostgreSQL 16 (`apt`/`brew`/installer), or one you already run.
2. **A hosted PostgreSQL** — a managed PG16 instance (Neon, Supabase, Amazon RDS, or similar).
3. **The bundled Docker Postgres** — `docker compose up -d postgres-sim` (the Quickstart above).

Then point the simulator at it with `DATABASE_URL` and run the pipeline — no Docker `initdb`
mount needed:

```bash
# 1. Point at any reachable PostgreSQL 16
export DATABASE_URL=postgres://user:pass@host:5432/dbname

# 2a. Generate a tenant — this self-provisions the full sql/001..007 schema on the
#     first run (base tables, cost, identity, drift, web-metadata migrations),
#     so a bare database is set up automatically before any data is written.
uv run tenantless generate --profile small --seed 42 --cost-as-of 2026-01-01

# 2b. …OR provision the schema WITHOUT generating any data (e.g. to serve an initially
#     empty tenant, or to set the schema up ahead of time), then serve:
uv run tenantless init-db
uv run tenantless serve
```

`init-db` is idempotent — re-running it against an already-provisioned database (including a
Docker volume) is a harmless no-op. **`generate` is not:** it replaces the current synthetic
estate — it truncates `synthetic.*` and writes a freshly generated one — so against a
non-empty estate it prompts for confirmation, or requires `--force` / `--yes` when stdin is
not a TTY. Do not treat re-running `generate` as a no-op.

If you prefer to apply the schema by hand, the SQL files are plain migrations you can run
with `psql`:

```bash
psql "$DATABASE_URL" \
  -f sql/001_synthetic_tenant.sql \
  -f sql/002_cross_sub_dependencies.sql \
  -f sql/003_integrity_and_index.sql \
  -f sql/004_cost.sql \
  -f sql/005_identity.sql \
  -f sql/006_drift.sql \
  -f sql/007_web_metadata.sql
```

## ARM endpoints

The mock server implements the ARM REST surface a discovery scan exercises:

| Endpoint | Purpose |
|----------|---------|
| `GET /subscriptions` | list subscriptions |
| `GET /subscriptions/{sub}/resourceGroups` | list resource groups |
| `GET /subscriptions/{sub}/resources` | list resources (paginated via `$top` / `$skiptoken` + `nextLink`) |
| `GET /subscriptions/{sub}/resourceGroups/{rg}/providers/{type}/{name}` | resource detail (arbitrary nesting depth) |
| `POST /subscriptions/{sub}/providers/Microsoft.CostManagement/query` | Cost Management query |
| `GET /subscriptions/{sub}/providers/Microsoft.Authorization/roleAssignments` | RBAC role assignments |

`$filter` is supported on `resourceType`, `location`, and `tagName`/`tagValue`.

**Authentication** is presence-only by default (any non-empty `Bearer` token is accepted) —
this is a local mock, not an auth gateway. `serve --enforce-auth` switches to real RS256 JWT
validation against the built-in JWKS endpoint when you want to exercise a client's token
handling. Opt-in TLS (`serve --tls`) adds an HTTPS listener on `:8443` with an ephemeral
self-signed certificate alongside the default plain-HTTP `:8080`.

**The ARM surface is read-only** (see *Scope* above): there is no `PUT`/`DELETE`, so a
`terraform apply` will not persist against it.

## Configuration drift

```bash
uv run tenantless apply-drift --type chaos --seed 99   # mutate the live tenant, deterministically
uv run tenantless revert-drift --batch-id <batch-id>   # LIFO-guarded, single-transaction revert
```

Drift is designed so a re-scan *detects* it: a scanner that ran before and after should
report exactly the delta the drift batch describes.

## Web console

`serve` also mounts a browser console at `/ui`:

- **Explorer** — browse subscriptions, resource groups and resources; search and filter
- **Observability** — live request metrics and status-code breakdowns
- **Control plane** — generate, snapshot and restore estates with job tracking
- **Scanner demo** — a guided walkthrough of what a discovery scan sees

The control-plane *write* surface is disarmed by default; it requires
`serve --enable-control-plane` plus `--control-token`. See
[`docs/control-plane-setup.md`](docs/control-plane-setup.md).

## Serving in Docker

```bash
docker compose up -d            # postgres-sim (:5433) + mock-server (:8080)
```

This starts PostgreSQL and the mock server, but **does not generate an estate** — a fresh
Compose database is empty and the slim server image does not include the Python generator.
Populate one first with the host-side `uv run tenantless generate` (see the Docker-assisted
Quickstart). The control plane's generate/analyze actions also shell out to `uv run
tenantless`, so they require a source-host deployment with uv, Python and the Tenantless
package — they do not run inside the slim server image.

## Compatibility

| Component | Version |
|-----------|---------|
| Python | 3.11+ |
| Rust | 1.95+ (2024 edition) |
| PostgreSQL | 16 |
| Node (web console build) | 20+ |
| ARM `api-version` | `2022-12-01` (resources), `2022-04-01` (RBAC) |

Full matrix and support policy: [`docs/compatibility.md`](docs/compatibility.md).

## Tech stack

| Layer | Stack |
|-------|-------|
| Analyzer + Generator | Python 3.11+, uv, Polars, scikit-learn, SciPy, Click, orjson, psycopg 3, ConnectorX |
| Mock Server | Rust (2024 edition), axum 0.8, sqlx 0.8, tokio |
| Web Console | React, TypeScript, Vite |
| Store | PostgreSQL 16 |

## Profiles

A *profile* is the statistical specification an estate is generated from. Two ship with the
project:

| Profile | Shape |
|---------|-------|
| `enterprise` | ~250 subscriptions, ~4.3K resource groups, ~59K resources, 38 resource types |
| `small` | 50 subscriptions, 600 resource groups, 5K resources |

Both are **synthetic**: `enterprise` is derived by generating an estate from the
hand-authored [`profiles/oss-bootstrap.json`](profiles/oss-bootstrap.json) and analyzing
*that*, so the chain has no real-tenant ancestor at any point. Each bundled profile records
this in a machine-readable `provenance` block, and
`scripts/check_release_provenance.py` enforces it.

To rebuild the bundled profile from scratch, or to fit one from your own scan, see
[`docs/profiles.md`](docs/profiles.md).

## Development

```bash
uv run pytest -q                              # Python suite (integration/scale markers opt-in)
cargo test -p tenantless-server               # Rust suite (testcontainers)
cd frontend && npm ci && npm test             # Web console suite
```

Note: the full Python suite truncates the tenant in the configured database — regenerate
before any manual UI or scanner session.

Contribution guidelines are in [`CONTRIBUTING.md`](CONTRIBUTING.md); security reporting is in
[`SECURITY.md`](SECURITY.md).

### Ways to contribute

- **Bug reports** — open an issue with a minimal reproduction. Generation is deterministic
  from `(profile, seed, --cost-as-of)`, so a repro is usually just the command and its seed.
- **Feature requests** — open an issue describing the Azure ARM behavior, resource type, or
  endpoint you need.
- **Pull requests** — bug fixes, new ARM resource-type or endpoint coverage, or improvements.
- **Compatibility tests** — add ARM contract cases to the server suite
  ([`mock-server/tests/`](mock-server/tests/)) or response-shape cases to
  [`tests/test_type_shapes.py`](tests/test_type_shapes.py); the compatibility matrix is in
  [`docs/compatibility.md`](docs/compatibility.md).

### Contributing an archetype

Resource-group names are generated from a static archetype catalog, and a name is a claim about
what the RG contains. Adding or changing a catalog entry has its own checklist —
[`docs/archetype-catalog-checklist.md`](docs/archetype-catalog-checklist.md). Read it first: the
rules there keep a generated name honest about its contents, and an automated coherence gate
([`scripts/audit_rg_coherence.py`](scripts/audit_rg_coherence.py)) enforces that a green run
reflects real coverage rather than a vacuous pass.

### Benchmarks

A reproducible latency/throughput harness lives at `scripts/bench_arm_latency.py`; see
[`docs/benchmarks/README.md`](docs/benchmarks/README.md). Committed results render as a
self-contained HTML dashboard (open the `.html` directly — no server needed).

```bash
uv run python scripts/bench_arm_latency.py --subscriptions 300 --resources 150000
```

## Notice

Tenantless is an independent Apache-2.0 open-source project developed and maintained by
[Code30](https://code30.io/).

It is **not affiliated with, endorsed by, or sponsored by Microsoft**. Azure and Microsoft
are trademarks of Microsoft Corporation. ARM resource-type names and API shapes appear here
descriptively, to identify the API surface this project emulates.

Tenantless does not connect to, proxy, or replace any Microsoft service. It serves
synthetic data from your own machine.

## License

[Apache-2.0](LICENSE). Third-party components — including the bundled fonts and
their SIL Open Font License texts — are listed in
[`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md).
