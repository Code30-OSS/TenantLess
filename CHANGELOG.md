# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Within the `1.x` line the public API — CLI flags, profile schema, and ARM response shapes —
follows Semantic Versioning: additive changes ship in minor releases, and breaking changes
wait for the next major release and are called out here.

## 1.1.11 — Fix: paginate role assignments and honor (or reject) their `$filter`

Bug-fix patch. No CLI-flag or profile-schema changes, and no change to the shape of a
`roleAssignments` item — the `1.x` public surface is unchanged. Brings the
`Microsoft.Authorization/roleAssignments` list endpoint in line with the other ARM list
endpoints.

### Fixed

- **The role-assignment listing is now keyset-paginated.** It previously fetched every
  assignment for a subscription into memory and returned them in one unbounded response,
  unlike every other ARM list endpoint. It now honors `$top` (clamped to `1..=1000`), an
  opaque `$skiptoken` continuation over the assignment id, and emits an absolute `nextLink`
  when another page exists — the same mechanism the resource list uses.
- **`$filter` is now honored instead of silently ignored.** A `$filter` was previously
  accepted and then quietly dropped, so a caller narrowing the list received the full
  unfiltered set with a `200` — misleading. The endpoint now applies the ARM `atScope()`
  and `principalId eq '{guid}'` forms (and their `and`-composition), and rejects any other
  or malformed form — including a non-GUID `principalId` — with an explicit
  `400 InvalidRequestContent` (`"invalid $filter"`, a fixed non-leaking message). `atScope()`
  returns only assignments stored at exactly the subscription scope; `principalId eq`
  returns only that principal's assignments. The `$filter` is echoed in `nextLink`, so a
  filtered traversal replays the same predicate on later pages. The principal id and the
  scope are bound as `$N` parameters, never spliced into SQL.

## 1.1.10 — Perf: index the case-insensitive resource-group predicate

Bug-fix patch (performance). No API, CLI-flag, or profile-schema changes. Follow-up to
1.1.8, which made the resource-group-scoped resource listing and cost query compare
`lower(resource_group_name) = lower($4)`.

### Fixed

- **The case-insensitive resource-group predicate is now index-backed.** The existing
  `idx_res_rg (subscription_id, resource_group_name)` can serve only the subscription
  prefix for a `lower(resource_group_name)` match — Postgres cannot use its second key for
  the functional predicate — so at scale a resource-group-scoped listing could scan every
  resource in the subscription, and the cost query could process a broader join before
  filtering. A new functional index
  `idx_res_rg_lower (subscription_id, lower(resource_group_name), id)` backs both paths;
  the trailing `id` serves the listing's `ORDER BY id` / keyset pagination. This mirrors
  the existing `lower(id)` functional index for the resource-detail lookup.
- **Existing databases upgrade automatically.** The index ships as a new idempotent
  migration (`sql/008_rg_lower_index.sql`) applied unconditionally by `generate` and
  `init-db` — the same "twin migration" mechanism as the cost/identity/drift/web-metadata
  schemas. A database provisioned before this release gains the index on its next
  `generate`/`init-db` with no manual step, and it is deliberately **not** part of the
  base-schema completeness check, so an additive performance index never makes a healthy
  install read as "incomplete" or demand a re-provision.

## 1.1.9 — Fix: bound the control-plane job store's memory

Bug-fix patch. No API, CLI-flag, or profile-schema changes — the `1.x` public surface is
unchanged. Affects only the in-memory control-plane job registry (present when the control
plane is armed).

### Fixed

- **The job registry no longer grows without bound.** Every armed control-plane job
  (generate / analyze / reset / snapshot / restore) was inserted into an in-memory map and
  never removed, so a long-running server accumulated job history for the life of the
  process. A fresh job now first evicts the oldest already-**terminal** jobs down to a
  retention bound (100), so completed history stays bounded; in-flight (queued/running)
  jobs are never evicted.
- **A single captured log line can no longer grow memory without bound.** The per-job log
  kept only the last N *lines*, but each line was read with an unbounded reader
  (`next_line()` reads the whole physical line into memory first), so a child emitting an
  enormous newline-free line could allocate it in full. Captured lines are now byte-capped
  (8 KiB retained per line, with a truncation marker) and the physical remainder is drained
  without being retained, so total per-job log memory is bounded.

## 1.1.8 — Fix: match resource-group names case-insensitively across ARM endpoints

Bug-fix patch. No API, CLI-flag, or profile-schema changes — the `1.x` public surface is
unchanged. Requests that already used the stored casing behave exactly as before.

### Fixed

- **A differently-cased resource-group name now resolves consistently across endpoints.**
  The resource-detail lookup already matched the ARM id case-insensitively
  (`lower(id) = lower($1)`), but the resource-group-scoped resource listing
  (`GET …/resourceGroups/{rg}/resources`) and the resource-group-scoped Cost Management
  query (`POST …/resourceGroups/{rg}/providers/Microsoft.CostManagement/query`) compared
  the group name with exact equality. As a result the same ARM path with a differently
  cased `{rg}` resolved to a resource in one endpoint yet returned an empty list — or a
  `$0` cost total — in the others. Both now compare with `lower(resource_group_name) =
  lower($4)`, so a scanner that mixes casing sees one coherent tenant. Azure treats
  resource-group names case-insensitively, so this matches the ARM contract.

## 1.1.7 — Fix: reject cost distributions that would crash the generator

Bug-fix patch. No API, CLI-flag, or command changes; the profile schema is tightened within
`1.2` (no version bump) — only cost distributions that would have crashed generation, or that
were internally inconsistent, are now rejected.

### Fixed

- **A schema-valid profile can no longer crash the generator with a cost distribution.** The
  `cost_distributions` schema left the gamma `shape`/`scale` unbounded, so a profile with a
  non-positive `shape` (or `scale`) passed validation and then raised an opaque numpy
  `ValueError` at draw time (`rng.gamma(shape, scale)` requires strictly-positive parameters).
  Both now require `exclusiveMinimum: 0`, so `generate` rejects such a profile up front (it
  validates the profile before generating) with a clear schema error. Additionally, a `gamma`
  distribution must now carry `shape` and `scale`, and a `lognormal` distribution must carry
  `mu` and `sigma`, instead of silently falling back to defaults. All bundled profiles are
  unaffected.

## 1.1.6 — Fix: ship a functional Python wheel; fail-fast `serve`; atomic `init-db`

Bug-fix patch. No new commands or flags; the `1.x` public surface is unchanged.

### Fixed

- **The Python wheel now ships its runtime data files.** The built wheel was missing the SQL
  migrations (`sql/*.sql`) and the profile schema (`profiles/schema.json`), so `init-db` and
  `generate` failed with a missing-file error when run from an installed wheel. Those files are
  now included in the wheel (via `force-include`), and both the packaged install and an
  editable/source checkout resolve them through a shared resolver.
- **`serve` fails fast with a clear error** instead of falling back to a cryptic `cargo run`
  when the compiled mock-server binary is absent — it now names the binary to build (or the
  published container image) rather than attempting a build the user did not ask for.
- **`init-db` is now atomic.** It pre-checks that all migration files are present before opening
  a transaction and applies them all-or-nothing, rolling back on any mid-apply failure instead
  of leaving a partially-provisioned schema; a partial base schema is reported explicitly rather
  than as a false success.

## 1.1.5 — Fix: web console refreshes after control-plane jobs, and survives an empty tenant

Bug-fix patch (web console). No API, CLI-flag, or profile-schema changes.

### Fixed

- **The Explorer now refreshes automatically when a control-plane job succeeds.** Generate,
  analyze, restore, and reset are asynchronous jobs; the console previously invalidated its
  caches at submit time (before the tenant changed) and did nothing on completion, so the
  Explorer kept showing the pre-job tenant until a manual Refresh. A single completion-driven
  invalidation now fires once when a job reaches `succeeded`. The job watcher lives at the app
  level (above the router), so the refresh still happens even if you navigate away from the
  control plane while the job is running, and the control-plane lock is disabled while a job is
  in flight so its auth cannot be torn down mid-poll.
- **An empty tenant no longer blanks the whole console.** Resetting to an empty tenant sends a
  null tenant id, which crashed the always-mounted top bar and, with no error boundary, took
  the entire page down. The top bar now guards null tenant/seed values, and an app-level error
  boundary contains any render error to a fallback instead of unmounting the whole app.

## 1.1.4 — Fix: bound generator memory and hold no locks across generation

Bug-fix patch. No API, CLI-flag, or profile-schema changes — the `1.x` public surface is
unchanged, and generated output is byte-for-byte identical.

### Fixed

- **`generate` no longer materializes millions of cost records in memory.** Cost rows now
  stream through a bounded on-disk spool during the CPU phase, so a large estate no longer
  holds the full set of cost records resident at once. The COPY payload and draw order are
  unchanged.
- **`generate` no longer holds a write transaction or DDL locks across the CPU phase.** The
  check-then-write mutual exclusion now rides a session-scoped advisory lock on a dedicated
  connection: schema provisioning commits in its own short transaction before generation, the
  CPU/worker phase runs with no open write transaction, and the write happens in a fresh
  transaction. Concurrent generators are still serialized. The expensive generation is also
  hoisted after the emptiness / destructive-confirm gate, so a declined or `--only-if-empty`
  run aborts before doing any of that work.

## 1.1.3 — Fix: bound the Cost Management query against resource exhaustion

Bug-fix patch. No API, CLI-flag, or profile-schema changes — the `1.x` public surface is
unchanged. Well-formed cost queries behave exactly as before; only abusive or pathological
queries are now rejected instead of consuming unbounded server resources.

### Fixed

- **The Cost Management query endpoint now fails closed on pathological inputs instead of
  exhausting server memory or compute.** Four bounds are enforced: a grouping-count guard
  rejects more than the ARM-documented maximum before any SQL is built; the result set is
  capped and an over-cap query returns a hard ARM-shaped `400` (never a partial `200`); a
  per-cell and cumulative response-byte budget is enforced *while streaming* rows, with the
  oversized-cell check pushed server-side so a huge value never crosses the wire; and an
  app-owned deadline bounds Postgres compute so an inherently high-cardinality aggregation is
  aborted deterministically. The response fold was also made `O(n)` (from `O(M²)`) while
  preserving first-appearance ordering.

## 1.1.2 — Fix: make snapshot restore validate-before-destroy and save atomic

Bug-fix patch. No API, CLI-flag, or profile-schema changes — the `1.x` public surface is
unchanged.

### Fixed

- **Snapshot restore no longer truncates the estate before the archive is validated.** A
  restore now runs a `pg_restore --list` table-of-contents dry run and decodes the archive to a
  server-owned temporary SQL file *before* any data is removed; the truncate and load then run
  in a single transaction (`--single-transaction`, `ON_ERROR_STOP`), so a corrupt, truncated, or
  unreadable archive aborts with the existing data fully intact instead of leaving an emptied
  estate.
- **Snapshot save is now atomic.** `pg_dump` writes to a same-directory temporary sibling that
  is renamed to the final `<name>.dump` only on a clean exit, and the job holds the writer
  permit through the rename before reporting `Succeeded` — so a listed snapshot always
  corresponds to a complete dump, and a failed or interrupted save leaves no partial artifact.

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
