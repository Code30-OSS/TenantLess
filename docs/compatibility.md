# Compatibility matrix

## Runtimes

| Component | Required | Notes |
|-----------|----------|-------|
| Python | **3.11+** | 3.12 and 3.13 also work. 3.11 is the supported floor. |
| Rust | **1.95+** | 2024 edition. Only needed to build the mock server from source. |
| PostgreSQL | **16** | Major version pinned. 15 and earlier are not tested; 17 is untested. |
| Node.js | **20+** | Only needed to build the web console from source. |
| Docker | optional | One convenient way to get PostgreSQL 16 and to run the server as a container. Not required — see the README. |

## Operating systems

| Platform | Status |
|----------|--------|
| Linux (x86-64) | Primary target. CI runs here. |
| macOS (Apple Silicon and Intel) | Supported. |
| Windows | Developed on it. Use the `uv` toolchain; Docker Desktop for PostgreSQL. |

## Azure API versions

Tenantless implements the **covered ARM endpoints and JSON shapes** a discovery scan
exercises — not the whole Azure ARM surface — and the management plane is **read-only**
(no `PUT` / `DELETE`, no live-resource behavior). Within that covered surface, the mock
server accepts the `api-version` query parameter and serves shapes matching these versions.
Requests carrying a different `api-version` are still served — the parameter is not used to
switch response shape — so a client pinned to a nearby version generally works.

| Surface | Version |
|---------|---------|
| Resources / resource groups / subscriptions | `2022-12-01` |
| Cost Management query | `2023-03-01` |
| Authorization (role assignments) | `2022-04-01` |

## Key dependencies

Exact versions are pinned in `uv.lock` and `Cargo.lock`, both committed.

| Layer | Package | Version |
|-------|---------|---------|
| Python | Polars | ≥ 1.40 |
| Python | psycopg | ≥ 3.3 (binary) |
| Python | ConnectorX | ≥ 0.4.5 |
| Python | SciPy | ≥ 1.17 |
| Python | scikit-learn | ≥ 1.8 |
| Python | Click | ≥ 8.4 |
| Python | orjson | ≥ 3.10 |
| Python | DuckDB | ≥ 1.4 |
| Rust | axum | 0.8 |
| Rust | sqlx | 0.8 |
| Rust | tokio | 1.x |
| Rust | clap | 4.x |
| Web | React + Vite + TypeScript | see `frontend/package.json` |

## Optional extras

| Extra | Install | Purpose |
|-------|---------|---------|
| `azure` | `uv sync --extra azure` | Enables `analyze --source azure:` to profile a live tenant through Azure Resource Graph. Pulls `azure-identity` and `azure-mgmt-resourcegraph`. A bare `uv sync` never installs these. |

## Profile schema

| Schema version | Status |
|----------------|--------|
| 1.2 | Current. Adds `cost_distributions`. |
| 1.1 | Accepted. Added co-occurrence, naming conventions, provenance. |
| 1.0 | Accepted. |

Schema changes are additive: every 1.0 and 1.1 profile still validates. The schema is
`profiles/schema.json`, and both the analyzer (on write) and the generator (on read)
validate against it.

## Support policy

This project follows Semantic Versioning within the `1.x` line:

- **The CLI, profile schema and ARM response shapes are stable within `1.x`.** Additive
  changes ship in minor releases; a breaking change waits for the next major release and is
  called out in [`CHANGELOG.md`](../CHANGELOG.md).
- Security fixes land on the latest released minor version of the current major line — see
  [`SECURITY.md`](../SECURITY.md).
- The determinism contract — `(profile, seed, cost-as-of)` → byte-identical estate — is
  treated as stable. A change that alters generated output for fixed inputs is a
  major-version change and will be noted.
