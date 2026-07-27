# Guided demo

A ten-minute walkthrough: build an estate, scan it like a real tenant, break it, detect the
break, and put it back. Every command is reproducible — the same
`(profile, seed, cost-as-of)` gives the same estate.

Requires [uv](https://docs.astral.sh/uv/), **Node 20+** and **Rust 1.95+** (to build the
server from source), and a **PostgreSQL 16** (Docker is the easy way to run PostgreSQL). If
you would rather not install Node and Rust, serve the estate with Docker instead — see
[Serving in Docker](#serving-in-docker) at the end.

## 0. Setup

```bash
cp .env.example .env
docker compose up -d postgres-sim
uv sync
cd frontend && npm ci && npm run build && cd ..   # the Rust server embeds frontend/dist at build time
```

## 1. Generate an estate

```bash
uv run tenantless generate --profile enterprise --seed 42 --cost-as-of 2026-01-01 --force
```

Expect a summary like:

```
Generated tenant …: 250 subscriptions, 4037 resource groups, 57207 resources,
14976 violations, 975 dependencies, 7650 principals, 8026 role assignments …
archetypes: shared=1041 vm-workload=741 web-app=490 network-hub=360 …
rg-naming: confirmed=2557 downgraded_to_generic=439 …
```

Two things worth noticing:

- **`--cost-as-of` is pinned.** Cost billing periods derive from it rather than from today,
  so this command produces the same estate whenever you run it.
- **`downgraded_to_generic`** counts resource groups whose contents did not justify the
  workload name their composition suggested. Tenantless would rather name a group
  generically than claim something its contents do not evidence.

Scale it with `--resources` / `--subscriptions`, and speed it up with `--jobs 0` (all
cores) — parallel output is byte-identical to single-threaded.

## 2. Serve it

```bash
uv run tenantless serve
```

`serve` looks for the server binary on `PATH`, then in `target/release` / `target/debug`,
then falls back to `cargo run` — so the first run compiles the Rust server (a minute or two).
It embeds `frontend/dist`, which is why step 0 builds the frontend first. Leave it running
and use a second terminal.

## 3. Scan it like a real tenant

Authentication is presence-only by default: any non-empty Bearer token works.

```bash
TOKEN="Bearer anything"
API="api-version=2022-12-01"

# Subscriptions
curl -s -H "Authorization: $TOKEN" "http://localhost:8080/subscriptions?$API" | head -c 400

# Pick one, then list its resource groups
SUB=$(curl -s -H "Authorization: $TOKEN" "http://localhost:8080/subscriptions?$API" \
      | python -c "import sys,json;print(json.load(sys.stdin)['value'][0]['subscriptionId'])")

curl -s -H "Authorization: $TOKEN" \
  "http://localhost:8080/subscriptions/$SUB/resourceGroups?$API" | head -c 400

# List resources, paginated
curl -s -H "Authorization: $TOKEN" \
  "http://localhost:8080/subscriptions/$SUB/resources?$API&\$top=5"
```

The response carries a `nextLink` when more pages exist — follow it exactly as a real
scanner would.

### `$filter`

```bash
curl -s -H "Authorization: $TOKEN" \
  "http://localhost:8080/subscriptions/$SUB/resources?$API&\$filter=resourceType%20eq%20'Microsoft.Storage/storageAccounts'"

curl -s -H "Authorization: $TOKEN" \
  "http://localhost:8080/subscriptions/$SUB/resources?$API&\$filter=location%20eq%20'westeurope'"
```

### Resource detail, including nested types

```bash
curl -s -H "Authorization: $TOKEN" \
  "http://localhost:8080/subscriptions/$SUB/resourceGroups/<rg>/providers/Microsoft.Sql/servers/<server>/databases/<db>?$API"
```

Nesting depth is arbitrary — the detail route is a catch-all, so a three-level provider path
resolves the same way a one-level path does.

## 4. Costs

```bash
curl -s -X POST -H "Authorization: $TOKEN" -H "Content-Type: application/json" \
  "http://localhost:8080/subscriptions/$SUB/providers/Microsoft.CostManagement/query?api-version=2023-03-01" \
  -d '{"type":"ActualCost","timeframe":"MonthToDate","dataset":{"granularity":"None","aggregation":{"totalCost":{"name":"Cost","function":"Sum"}},"grouping":[{"type":"Dimension","name":"ResourceType"}]}}'
```

## 5. Identity and RBAC

```bash
curl -s -H "Authorization: $TOKEN" \
  "http://localhost:8080/subscriptions/$SUB/providers/Microsoft.Authorization/roleAssignments?api-version=2022-04-01" | head -c 400

curl -s -H "Authorization: $TOKEN" \
  "http://localhost:8080/subscriptions/$SUB/providers/Microsoft.Authorization/roleDefinitions?api-version=2022-04-01" | head -c 400
```

Generation injects a configurable rate of over-privileged assignments
(`--over-privilege-rate`, default 5%) — Owner at subscription scope, service principals
granted Owner. That is what a privilege-analysis tool should flag.

To exercise real token validation instead of presence-only auth, restart with
`serve --enforce-auth` and mint a token from the built-in endpoint.

## 6. Break it, then detect the break

This is the part that is hard to do with a real tenant.

```bash
uv run tenantless apply-drift --type chaos --seed 99
```

Drift mutates resources in place — disabling encryption, opening a security rule, changing a
SKU — and records a batch. Re-run your scanner now: the delta it reports should be exactly
what the batch describes.

Inspect the batch through the API:

```bash
curl -s -H "Authorization: $TOKEN" "http://localhost:8080/simulator/drift"
curl -s -H "Authorization: $TOKEN" "http://localhost:8080/simulator/drift/<batch-id>"
```

Then put it back:

```bash
uv run tenantless revert-drift --batch-id <batch-id>
```

The revert is LIFO-guarded and runs in a single transaction: batches unwind in reverse
order, or not at all.

## 7. The web console

Open <http://localhost:8080/ui>.

- **Explorer** — walk subscriptions → resource groups → resources, with search and filters
- **Observability** — live request metrics and status-code breakdown; make some curl
  requests and watch them arrive
- **Scanner demo** — a guided view of what a discovery scan sees
- **Control plane** — generate, snapshot and restore estates from the browser

The control-plane write surface is disarmed unless the server was started with
`--enable-control-plane` and a `--control-token`. See
[`control-plane-setup.md`](control-plane-setup.md).

## 8. Prove it is reproducible

```bash
uv run tenantless generate --profile enterprise --seed 42 --cost-as-of 2026-01-01 --force
```

Same profile, same seed, same `--cost-as-of`, same estate — down to the bytes. This is what
makes a bug report against Tenantless a `(profile, seed, cost-as-of)` triple rather than a
database dump.

## Serving in Docker

```bash
docker compose up -d      # postgres-sim on :5433, mock-server on :8080
```

This builds and serves the mock server in a container (no local Node or Rust needed), but a
fresh Compose database is **empty** and the server image does **not** include the Python
generator — so it does not generate an estate. Run `uv run tenantless generate …` against the
same PostgreSQL first (step 1) to populate one, or use the control-plane generate action.
