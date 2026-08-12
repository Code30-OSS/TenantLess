# Pre-v3 response golden

These files are the **frozen, independent pre-v3 reference** for the ARM read-response
BODY bytes. They were captured against the **current, pre-resolver** handlers
(`mock-server/src/handlers/{resources,resource_detail}.rs`) which read
`synthetic.resources` **directly**, with the ARM overlay **empty**.

They are the byte-identity baseline the unified resolver must
reproduce: when the ARM read handlers re-point their `FROM` clause onto
`synthetic.arm_resolved_resources` (the resolved view) and the overlay is empty, the
served body MUST equal these frozen bytes — the `arm_byte_identical` proof.

The assertions live in `mock-server/tests/resolver_golden.rs`.

## FROZEN-FIXTURE RULE

Do **NOT** re-capture these files after the resolver swap. Re-capturing through the resolver
would make the byte-identity assertion tautological (it would compare the resolver to
itself). They are (re)generated ONLY by a deliberate pre-swap run:

```
TENANTLESS_BLESS_GOLDEN=1 cargo test -p mock-server --test resolver_golden
```

Without that flag a missing golden is a **hard failure** (a deleted/absent fixture in
CI fails loudly rather than silently re-blessing).

## Captured responses

Every capture is served through the real `build_router` seam over the shared
`common::seed_fixture` testcontainers fixture (1 tenant, subs A/B, dense RG
`rg-dense-000` with 110 storage resources, filter RGs, a nested `Microsoft.Sql`
resource). No hand-built bytes.

| File | Request | What it exercises |
|------|---------|-------------------|
| `list_dense_rg_top3_page1.json` | `GET /subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-dense-000/resources?$top=3` | Keyset list first page (3 resources, ordered by `id`) + `nextLink` emission. The ARM list envelope `{ "value": [...], "nextLink": ... }`. |
| `detail_nested_sql_db.json` | `GET /subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-filter-000/providers/Microsoft.Sql/servers/sql-srv-000/databases/db-000` | Arbitrary-depth resource detail (single-object body, not an envelope); `sku`/`kind` absent (NULL), `properties = {"status":"Online"}`, `tags = {"env":"prod"}`. |

Determinism: rows are `ORDER BY id`; JSON object keys are emitted in sorted order
(`serde_json` with `preserve_order` OFF), so the byte output is stable run-to-run and
independent of Postgres' internal `jsonb` key ordering.
