# Architecture

Tenantless is three programs and one database. Each stage has a narrow contract with the
next, which is what makes the pieces independently useful and independently testable.

```
   ┌─────────────────┐        ┌─────────────────┐        ┌──────────────────┐
   │  Seed Analyzer  │        │    Generator    │        │   Mock Server    │
   │     (Python)    │        │     (Python)    │        │      (Rust)      │
   └────────┬────────┘        └────────┬────────┘        └────────┬─────────┘
            │                          │                          │
   a scan ──┴──▶  profile.json  ───────┴──▶  PostgreSQL  ─────────┴──▶  ARM HTTP
                  (statistics)              (synthetic.*)               + /ui + /_sim
```

The seams are deliberate:

- **A profile is a file.** You can write one by hand, commit it, diff it, and review it. The
  analyzer is one way to produce one, not the only way.
- **An estate is a database.** Anything that can read PostgreSQL can read the estate; the
  mock server has no privileged channel.
- **The server is read-only.** It never writes to `synthetic.*` during request handling, so
  serving and generating are independent activities.

## Stage 1 — Seed Analyzer

Turns a scan into a statistical profile. Input is either a DuckDB scan file
(`duckdb:<path>`) or a live Azure Resource Graph query (`azure:<subs>`); both are
normalized to the same small aggregation surface, so every extractor downstream is
source-agnostic.

**Reader seam** (`src/tenantless/analyzer/reader.py`, `src/tenantless/analyzer/azure/materialize.py`)
Pushes `COUNT` / `GROUP BY` into SQL and returns small Polars frames. No per-resource row is
ever materialized into Python — a 500K-resource scan crosses the seam as a few thousand
aggregate rows.

**Extractors** (`src/tenantless/analyzer/extractors/`)
Each derives one profile section: subscription archetypes (k-means over per-subscription
feature vectors), resource-group templates, resource-type shapes, tags and co-occurrence,
naming conventions, cross-subscription topology, governance violations, cost distributions
(SciPy MLE fits). None of them imports a reader type — they operate on frames.

**Privacy layer** (`src/tenantless/analyzer/privacy.py`)
The reason the seam is shaped this way. Identifier-shaped keys are rejected, rare buckets
are folded into `__other__`, and buckets below `--min-bucket-size` are dropped entirely. A
denylist of real identifiers is required for a real source; the analyzer fails closed
without one rather than emitting a profile nobody vetted.

Every extractor that emits strings carries its own leak test. That rule exists because a new
string-emitting path once bypassed the shared guard and shipped.

## Stage 2 — Generator

Inverts a profile into a concrete estate. This is the structural mirror of the analyzer:
where an extractor turned JSON into histograms, the generator samples histograms back into
ARM-valid JSON.

**Sampling** — subscription count and archetype assignment, resource-group count and
template assignment, resource type-mix per group, then per-resource properties, SKU, kind,
tags, location.

**Reference wiring** (`src/tenantless/generator/resources.py`)
Wiring runs over a subscription-scoped pool so every VM→NIC, NIC→subnet and VM→disk
reference resolves to a resource that actually exists. Dangling references are a
correctness bug, not an acceptable approximation.

**Archetype naming** (`src/tenantless/generator/archetypes.py`)
Resource-group names are derived from a static catalog of archetypes with explicit type
signatures. A name is treated as a *claim*: `network-hub` asserts the group contains
networking infrastructure. Each archetype declares a `ConfirmationPolicy` —
`ANCHOR_REQUIRED`, `SUPPORTING_ALLOWED` or `GENERIC` — with no default, so a new catalog
entry cannot inherit permissiveness by omission. A group whose contents do not confirm its
best-matching archetype is downgraded to a generic name rather than over-claiming.

**Violations, topology, cost, identity, drift** — layered on afterwards, each at
profile-controlled or flag-controlled rates.

**Writing** (`src/tenantless/generator/writer.py`)
Binary `COPY` into PostgreSQL. `--jobs` splits generation across processes by subscription,
each drawing from a `SeedSequence.spawn` child so parallel output is byte-identical to
single-threaded output.

### Determinism

A fixed `(profile, seed, cost-as-of)` produces a byte-identical estate. This is a contract,
and the suite proves `--jobs 1 == --jobs N` rather than assuming it. Practically it means a
bug report is a `(profile, seed, cost-as-of)` triple, benchmarks are comparable across runs,
and a refactor can be shown to have changed nothing by hashing the estate before and after.

Anything that introduces unseeded randomness, iteration over an unordered set, or
wall-clock time into generation breaks it. Cost periods derive from an explicit
`--cost-as-of` date for exactly this reason.

## Stage 3 — Mock Server

An axum service over sqlx, serving three surfaces on one port:

| Prefix | Surface |
|--------|---------|
| `/subscriptions/...`, `/providers/...` | ARM-compatible REST |
| `/_sim/...` | Simulator-native read APIs backing the console (violations, dependencies, summaries) |
| `/_control/...` | Control plane: generate, snapshot, restore — **disarmed by default** |
| `/ui` | The web console, served same-origin |

ARM and simulator APIs live on separate prefixes on purpose: the ARM surface must stay
byte-compatible with what a real scanner expects, so console features never leak into it.

**Query construction.** Every runtime-built SQL fragment binds literals as `$N` and never
splices them; the field→column mapping is a closed match, not string interpolation. A
metacharacter unit test pins this.

**Control plane.** The write surface requires both `--enable-control-plane` and a
`--control-token`, and confines artifacts to `--control-data-dir`. Default-off is the point:
a server started the ordinary way cannot be made to mutate anything.

## Data model

```
synthetic.tenant           one row: generation parameters and profile version
synthetic.subscriptions    id, display name, archetype, tags
synthetic.resource_groups  id, subscription, name, location, template type
synthetic.resources        id, type, location, tags, sku, kind, properties (JSONB), managed_by
synthetic.dependencies     cross-subscription edges
synthetic.violations       resource, violation type, severity, detail
synthetic.cost_records     resource, billing period, amount
synthetic.principals       synthetic identities
synthetic.role_assignments principal, role, scope
synthetic.drift_batches    a drift application
synthetic.drift_records    per-resource before/after for revert
```

Migrations are plain SQL in `sql/`, applied idempotently by `generate` and `init-db`.

## Cross-cutting invariants

These hold across stages and are each pinned by a gate:

1. **Determinism.** `(profile, seed, cost-as-of)` → byte-identical estate; `--jobs 1 == --jobs N`.
2. **Referential integrity.** No dangling reference, including after the archetype rename
   sweep rewrites resource-group names.
3. **The data boundary.** No real identifier reaches a profile; no bundled profile has a
   real-tenant ancestor.
4. **ARM compatibility.** Response shapes stay byte-compatible with what an unmodified
   scanner parses.
5. **Non-vacuity.** Every gate asserting "zero findings" carries a paired evidence floor
   asserting it examined a meaningful population. A gate that cannot fail proves nothing, so
   each such gate is required to demonstrate it inspected a non-empty, relevant population.

## What is deliberately absent

- **ARM writes.** No `PUT` / `DELETE`, no long-running-operation polling, no `ETag` /
  `If-Match`. A stateful lifecycle store is out of scope for this read-only surface — a
  different problem, because once writes persist, "just regenerate" stops being a recovery
  path.
- **Behavioral realism.** Resources look right and relate correctly. They do not run,
  serve traffic, or emit real metrics.
- **Complete service coverage.** The catalog covers common solution shapes and grows from
  observed gaps rather than trying to enumerate Azure.
