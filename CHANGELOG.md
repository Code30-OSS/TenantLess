# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Within the `1.x` line the public API — CLI flags, profile schema, and ARM response shapes —
follows Semantic Versioning: additive changes ship in minor releases, and breaking changes
wait for the next major release and are called out here.

## 1.1.1 — Security: enforce the k-anonymity floor on `--min-bucket-size`

Security patch. The `1.x` public surface is unchanged and the default behavior is identical;
the only change is that unsafe `--min-bucket-size` values, which were previously accepted, are
now rejected. See the associated GitHub Security Advisory.

### Security

- **Enforce the minimum-aggregation (k-anonymity) floor on `analyze --min-bucket-size`.**
  The flag previously accepted any integer — including `1`, `0`, or negatives. A value below
  the floor disabled the minimum-aggregation threshold that keeps rare real values (locations,
  tag values, resource-group/type co-occurrences, cost outliers, name samples) out of the
  statistical profile, weakening the data-boundary guarantee that profiles contain no
  real-tenant identifiers. The floor is now a hard minimum of **5**, enforced at the CLI and
  independently in `build_profile()` for programmatic callers (reject, not clamp), failing
  closed on non-integer and int-subclass inputs. The default remains `5`, so runs using the
  default are unaffected.

## 1.1.0 — One-command demo and container images

Adoption release. No API, CLI, or profile-schema changes — the `1.x` public surface is unchanged.

### Added

- **One-command demo:** `docker compose --profile demo up` starts Postgres, a one-shot
  generator that seeds a deterministic synthetic estate, and the mock server with the
  embedded web console. Plain `docker compose up` keeps today's empty-tenant behavior; the
  generator never overwrites a non-empty volume. Reset with
  `docker compose --profile demo down -v && docker compose --profile demo up`.
- **Bundled `demo` profile** derived from synthetic data (with cost distributions), rebuilt
  byte-for-byte in a pinned canonical builder and verified in CI.
- **Container images (linux/amd64)** published to GHCR by digest behind a native amd64
  end-to-end gate: `ghcr.io/code30-oss/tenantless-mock-server:1.1.0` and
  `ghcr.io/code30-oss/tenantless-generator:1.1.0`.

### Changed

- Base images pinned by digest for reproducible builds.

### Notes

- Images are **amd64-only** in this release; arm64 is deferred behind a native-runner gate
  passing the same end-to-end test.

## 1.0.0 — Initial public release

First public release. Tenantless was developed privately before this point; the public
history starts here as a single root commit rather than a republished internal history.
Nothing is missing from the code — only the internal planning and review history, which was
never intended for publication.

### Added

**Management plane (ARM)**

- ARM-compatible REST surface: subscription / resource-group / resource listing, and
  resource detail at arbitrary nesting depth (e.g. `Microsoft.Sql/servers/{n}/databases/{n}`)
- Pagination via `$top` / `$skiptoken` with `nextLink`
- OData `$filter` on `resourceType`, `location`, `tagName` / `tagValue`
- ARM `CloudError` response shapes
- Presence-only Bearer auth by default; `--enforce-auth` for real RS256 JWT validation
- Opt-in TLS listener (`--tls`)

**FinOps**

- Cost Management Query API over a generated cost fact table
- Monthly and daily cost granularity, anchored to an explicit `--cost-as-of` date so a run
  is reproducible across calendar days

**Identity and RBAC**

- AAD/Entra token issuance with RS256 signing and a JWKS endpoint
- 8 built-in RBAC roles, synthetic principals and role assignments
- Configurable over-privilege injection rate (`--over-privilege-rate`)

**Configuration drift**

- Deterministic, seeded drift applied to a live tenant, detectable by a re-scan
- LIFO-guarded single-transaction revert

**Generation**

- Statistical profiles: subscription archetypes, resource-group templates, resource-type
  distributions, tag distributions and co-occurrence, cross-subscription topology,
  governance-violation rates, and fitted cost distributions
- Reproducible from `(profile, seed, cost-as-of)`; parallel generation (`--jobs N`) is
  byte-identical to single-threaded (`--jobs 1`)
- Multiprocess generation (`--jobs`)
- 18 governance violation types across severities, at profile-controlled rates
- Cross-subscription dependency topologies: hub-spoke peering, shared Key Vaults,
  centralized logging, shared registries, private endpoints
- Resource-group names derived from a static archetype catalog, which refuses to claim a
  workload the resource group's contents do not evidence

**Analysis**

- Seed Analyzer producing a statistical profile from a DuckDB scan or a live Azure Resource
  Graph query
- Data boundary enforced by denylist scanning, minimum-aggregation thresholds and
  per-extractor leak tests

**Web console**

- Explorer, Observability, Control Plane with job tracking, and a Scanner Demo
- Control-plane write surface disarmed by default, requiring explicit arming plus a token

**Operations**

- `docker compose up -d` for PostgreSQL plus the mock server
- Self-provisioning schema: `generate` and `init-db` apply migrations to a bare database
- Works against any reachable PostgreSQL 16; Docker is optional
- Reproducible latency/throughput benchmark harness emitting JSON, Markdown and a
  self-contained HTML dashboard

### Notes on the bundled profiles

Both bundled profiles are synthetic. `enterprise` is produced by generating an estate from
the hand-authored `profiles/oss-bootstrap.json` and analyzing that estate — so no profile,
benchmark or documented measurement in this repository derives from a real Azure tenant.
Each bundled profile carries a machine-readable `provenance` block, and
`scripts/check_release_provenance.py` enforces the claim.

### Known limitations

- **The ARM surface is read-only.** No `PUT` / `DELETE`, so `terraform apply` against it
  does not persist; the management plane does not model resource state.
- Not every Azure service is modeled; the catalog covers common solution shapes.
- Realism is **statistical**, not behavioral. Resources look right and relate correctly to
  one another. They do not run.

This release is tagged `v1.0` in Git. The public history begins with a single root commit
because the internal planning and review history is not republished.
