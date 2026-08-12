//! DB-backed proofs for the resolver substrate (`sql/010_arm_resolver.sql` +
//! `ensure_arm_resolver_schema`): the two per-kind resolved views
//! (`synthetic.arm_resolved_resources` — the liveness authority — and
//! `synthetic.arm_resolved_resource_groups`), the `storage_mode` provenance column, and the
//! `(target_kind, id_lower)` overlay resolution index.
//!
//! Every test spins its OWN ephemeral testcontainers Postgres and provisions a bare base +
//! overlay via `common::seed_overlay_first_boot` (NOT `seed_fixture` — project memory:
//! fixture coupling), then applies the resolver migration under test. This plan changes NO
//! reader and NO writer — these proofs exercise the SQL seam directly.
//!
//! Coverage:
//!   * `resolver_schema_idempotent` — a repeated `ensure_arm_resolver_schema` is a no-op.
//!   * `resolver_views_shape_matches_base_tables` — each resolved view's per-column
//!     `data_type` equals its baseline table's, so `sqlx::query_as::<_, ResourceRow>` /
//!     `ResourceGroupRow` decode identically (Task-1 typed-contract assertion).
//!   * `resource_view_decodes_as_resource_row` — a baseline row AND a valid overlay-present
//!     row both decode through the view into `ResourceRow`; the overlay wins wholesale.
//!   * `resolver_resolves_baseline_overlay_tombstone` — baseline-only, overlay-replaced, and
//!     tombstoned ids resolve per the liveness rule (the single anti-join covers both).
//!   * `overlay_only_malformed_id_fails_closed` — an overlay-only present row with a
//!     malformed id yields ZERO rows (fail-closed); a well-formed overlay-only row IS
//!     served with the derived scope.
//!   * `resolver_inventory_deep` — the deep structural inventory fails loudly on a dropped
//!     view, a missing/mistyped view column, an absent `storage_mode`, and a missing index.
//!   * `resolver_sql_file_is_initdb_honest` — sql/010 carries no transaction-scoped statement.

mod common;

use serde_json::{Value, json};
use sqlx::PgPool;
use sqlx::Row;
use testcontainers_modules::{postgres, testcontainers::runners::AsyncRunner};

/// Start an ephemeral Postgres container and return a connected pool plus the container
/// guard (kept alive for the test's duration). Mirrors the `start_pg` in the other suites.
async fn start_pg() -> (
    PgPool,
    testcontainers::ContainerAsync<postgres::Postgres>,
) {
    let container = postgres::Postgres::default()
        .start()
        .await
        .expect("start postgres container");
    let host = container.get_host().await.expect("container host");
    let port = container
        .get_host_port_ipv4(5432)
        .await
        .expect("container port");
    let url = format!("postgres://postgres:postgres@{host}:{port}/postgres");
    let pool = PgPool::connect(&url).await.expect("connect pool");
    (pool, container)
}

/// Model a VALID first boot for the resolver: provision base + overlay (via the shared
/// `seed_overlay_first_boot` harness), then apply `ensure_arm_resolver_schema` twice — the
/// 1st is the real create, the 2nd proves idempotency.
async fn seed_resolver_first_boot(pool: &PgPool) {
    common::seed_overlay_first_boot(pool).await;
    tenantless_server::ensure_arm_resolver_schema(pool)
        .await
        .expect("first real ensure_arm_resolver_schema apply");
    tenantless_server::ensure_arm_resolver_schema(pool)
        .await
        .expect("idempotent re-apply of ensure_arm_resolver_schema");
}

const SUB: &str = "11111111-1111-1111-1111-111111111111";

fn res_id(rg: &str, name: &str) -> String {
    format!("/subscriptions/{SUB}/resourceGroups/{rg}/providers/Microsoft.Storage/storageAccounts/{name}")
}

/// Insert a baseline subscription + resource_group + resource so the view's baseline branch
/// has a live row to resolve. Idempotent-friendly (uses fixed ids per call arg).
async fn seed_baseline_resource(pool: &PgPool, rg: &str, name: &str, tags: &str) -> String {
    let sub = uuid::Uuid::parse_str(SUB).unwrap();
    // Tenant + subscription + RG (FK chain). Guard against duplicate seeds across helper calls.
    sqlx::query(
        "INSERT INTO synthetic.tenant (tenant_id, display_name, profile_version, scale_params) \
         VALUES ('00000000-0000-0000-0000-000000000000', 't', '1.0', '{}'::jsonb) \
         ON CONFLICT DO NOTHING",
    )
    .execute(pool)
    .await
    .expect("tenant");
    sqlx::query(
        "INSERT INTO synthetic.subscriptions \
             (subscription_id, tenant_id, display_name, state, archetype, tags, \
              authorization_source, spending_limit) \
         VALUES ($1, '00000000-0000-0000-0000-000000000000', 'sub', 'Enabled', 'prod', \
                 '{}'::jsonb, 'RoleBased', 'Off') ON CONFLICT DO NOTHING",
    )
    .bind(sub)
    .execute(pool)
    .await
    .expect("subscription");
    let rg_id = format!("/subscriptions/{SUB}/resourceGroups/{rg}");
    sqlx::query(
        "INSERT INTO synthetic.resource_groups \
             (id, subscription_id, name, location, template_type, tags, provisioning_state) \
         VALUES ($1, $2, $3, 'eastus', 'network', '{}'::jsonb, 'Succeeded') \
         ON CONFLICT DO NOTHING",
    )
    .bind(&rg_id)
    .bind(sub)
    .bind(rg)
    .execute(pool)
    .await
    .expect("resource group");
    let id = res_id(rg, name);
    sqlx::query(
        "INSERT INTO synthetic.resources \
             (id, subscription_id, resource_group_name, name, type, location, tags, sku, \
              kind, properties, provisioning_state, managed_by) \
         VALUES ($1, $2, $3, $4, 'Microsoft.Storage/storageAccounts', 'eastus', \
                 $5::jsonb, NULL, NULL, '{\"provisioningState\":\"Succeeded\"}'::jsonb, \
                 'Succeeded', NULL)",
    )
    .bind(&id)
    .bind(sub)
    .bind(rg)
    .bind(name)
    .bind(tags)
    .execute(pool)
    .await
    .expect("resource");
    id
}

/// A complete, CHECK-valid ARM resource body for the given canonical `id`.
fn valid_resource_body(id: &str, name: &str) -> Value {
    json!({
        "id": id,
        "name": name,
        "type": "Microsoft.Storage/storageAccounts",
        "location": "westus",
        "tags": { "env": "overlay" },
        "properties": { "provisioningState": "Succeeded" }
    })
}

/// Insert an overlay row letting the BEFORE trigger assign `revision`. Returns the raw
/// `Result` so fail-closed / rejection tests can assert.
async fn insert_overlay(
    pool: &PgPool,
    id: &str,
    kind: &str,
    present: bool,
    body: Option<Value>,
) -> Result<sqlx::postgres::PgQueryResult, sqlx::Error> {
    sqlx::query(
        "INSERT INTO synthetic.arm_overlay (id_lower, id, target_kind, source, present, body) \
         VALUES ($1, $2, $3, 'drift', $4, $5)",
    )
    .bind(id.to_lowercase())
    .bind(id)
    .bind(kind)
    .bind(present)
    .bind(body)
    .execute(pool)
    .await
}

// --------------------------------------------------------------------------------------- //
// Idempotency
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn resolver_schema_idempotent() {
    let (pool, _c) = start_pg().await;
    seed_resolver_first_boot(&pool).await;
    // A further explicit re-apply is still a clean no-op, and the inventory passes.
    tenantless_server::ensure_arm_resolver_schema(&pool)
        .await
        .expect("third idempotent ensure_arm_resolver_schema apply");
    tenantless_server::arm_resolver_inventory(&pool)
        .await
        .expect("inventory passes on a correctly-provisioned resolver substrate");
    // Exactly one of each view exists.
    for view in ["arm_resolved_resources", "arm_resolved_resource_groups"] {
        let n: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM pg_class WHERE relname = $1 \
             AND relnamespace = 'synthetic'::regnamespace AND relkind = 'v'",
        )
        .bind(view)
        .fetch_one(&pool)
        .await
        .unwrap();
        assert_eq!(n, 1, "exactly one {view} view after repeated applies");
    }
}

// --------------------------------------------------------------------------------------- //
// Typed column contract (enumerated columns, matching baseline types)
// --------------------------------------------------------------------------------------- //

async fn col_type(pool: &PgPool, table: &str, col: &str) -> Option<String> {
    sqlx::query_scalar(
        "SELECT data_type FROM information_schema.columns \
         WHERE table_schema = 'synthetic' AND table_name = $1 AND column_name = $2",
    )
    .bind(table)
    .bind(col)
    .fetch_optional(pool)
    .await
    .unwrap()
}

#[tokio::test]
async fn resolver_views_shape_matches_base_tables() {
    let (pool, _c) = start_pg().await;
    seed_resolver_first_boot(&pool).await;

    // Resource view: every column's data_type equals synthetic.resources' — so
    // sqlx::query_as::<_, ResourceRow> decodes identically whether a row came from the
    // baseline branch or the overlay branch.
    for col in [
        "id",
        "name",
        "type",
        "location",
        "tags",
        "sku",
        "kind",
        "properties",
        "subscription_id",
        "resource_group_name",
        "provisioning_state",
        "managed_by",
    ] {
        let base = col_type(&pool, "resources", col).await;
        let view = col_type(&pool, "arm_resolved_resources", col).await;
        assert!(base.is_some(), "base resources.{col} exists");
        assert_eq!(
            base, view,
            "arm_resolved_resources.{col} type {view:?} must equal synthetic.resources.{col} type {base:?}"
        );
    }

    // RG view: id/name/location/tags/provisioning_state equal synthetic.resource_groups';
    // subscription_id is internal (uuid) and matches the base sub column type.
    for col in ["id", "name", "location", "tags", "provisioning_state", "subscription_id"] {
        let base = col_type(&pool, "resource_groups", col).await;
        let view = col_type(&pool, "arm_resolved_resource_groups", col).await;
        assert!(base.is_some(), "base resource_groups.{col} exists");
        assert_eq!(
            base, view,
            "arm_resolved_resource_groups.{col} type {view:?} must equal synthetic.resource_groups.{col} type {base:?}"
        );
    }

    // The RG view MUST NOT expose a served `type` column (ResourceGroupRow synthesizes the
    // const in Rust) nor `managed_by`.
    assert!(
        col_type(&pool, "arm_resolved_resource_groups", "type").await.is_none(),
        "the RG view must not carry a `type` column"
    );
}

#[tokio::test]
async fn resource_view_decodes_as_resource_row() {
    let (pool, _c) = start_pg().await;
    seed_resolver_first_boot(&pool).await;

    // A baseline-only resource decodes through the view.
    let base_id = seed_baseline_resource(&pool, "rg-a", "res-base", r#"{"env":"base"}"#).await;
    let row = sqlx::query(
        "SELECT id, name, type, location, tags, sku, kind, properties \
         FROM synthetic.arm_resolved_resources WHERE id = $1",
    )
    .bind(&base_id)
    .fetch_one(&pool)
    .await
    .expect("baseline row resolves through the view");
    let tags: Value = row.get::<sqlx::types::Json<Value>, _>("tags").0;
    assert_eq!(tags, json!({ "env": "base" }));

    // Overlay a present row over the SAME id — it must win wholesale (overlay tags/location).
    insert_overlay(
        &pool,
        &base_id,
        "resource",
        true,
        Some(valid_resource_body(&base_id, "res-base")),
    )
    .await
    .expect("overlay the baseline id");
    let row = sqlx::query(
        "SELECT id, name, type, location, tags, sku, kind, properties \
         FROM synthetic.arm_resolved_resources WHERE id = $1",
    )
    .bind(&base_id)
    .fetch_one(&pool)
    .await
    .expect("overlay row resolves through the view");
    let tags: Value = row.get::<sqlx::types::Json<Value>, _>("tags").0;
    let location: String = row.get("location");
    assert_eq!(tags, json!({ "env": "overlay" }), "overlay tags win");
    assert_eq!(location, "westus", "overlay location wins");

    // Exactly one row is served for the id (no duplication across branches).
    let n: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM synthetic.arm_resolved_resources WHERE id = $1",
    )
    .bind(&base_id)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(n, 1, "the anti-join yields exactly one resolved row per id");
}

// --------------------------------------------------------------------------------------- //
// Liveness rule: baseline / replaced / tombstoned
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn resolver_resolves_baseline_overlay_tombstone() {
    let (pool, _c) = start_pg().await;
    seed_resolver_first_boot(&pool).await;

    let live_id = seed_baseline_resource(&pool, "rg-a", "res-live", "{}").await;
    let replaced_id = seed_baseline_resource(&pool, "rg-a", "res-replaced", "{}").await;
    let tombstoned_id = seed_baseline_resource(&pool, "rg-a", "res-gone", "{}").await;

    // Replace one baseline id with a present overlay row; tombstone another (present=false).
    insert_overlay(
        &pool,
        &replaced_id,
        "resource",
        true,
        Some(valid_resource_body(&replaced_id, "res-replaced")),
    )
    .await
    .expect("replace overlay");
    insert_overlay(&pool, &tombstoned_id, "resource", false, None)
        .await
        .expect("tombstone overlay");

    let present = |id: &str| {
        let pool = pool.clone();
        let id = id.to_string();
        async move {
            let n: i64 = sqlx::query_scalar(
                "SELECT count(*) FROM synthetic.arm_resolved_resources WHERE id = $1",
            )
            .bind(&id)
            .fetch_one(&pool)
            .await
            .unwrap();
            n
        }
    };

    assert_eq!(present(&live_id).await, 1, "a baseline-only id is live");
    assert_eq!(
        present(&replaced_id).await,
        1,
        "a replaced id is live (the overlay wins, not duplicated)"
    );
    assert_eq!(
        present(&tombstoned_id).await,
        0,
        "a tombstoned id disappears from the resolved view"
    );
}

// --------------------------------------------------------------------------------------- //
// Fail-closed scope derivation
// --------------------------------------------------------------------------------------- //

#[tokio::test]
async fn overlay_only_malformed_id_fails_closed() {
    let (pool, _c) = start_pg().await;
    seed_resolver_first_boot(&pool).await;

    // A well-formed overlay-only present row (no baseline twin) IS served, with the scope
    // derived from its canonical id.
    let good_id = res_id("rg-derived", "res-forward");
    insert_overlay(
        &pool,
        &good_id,
        "resource",
        true,
        Some(valid_resource_body(&good_id, "res-forward")),
    )
    .await
    .expect("well-formed overlay-only row");
    let scope: Option<(uuid::Uuid, String)> = sqlx::query_as(
        "SELECT subscription_id, resource_group_name \
         FROM synthetic.arm_resolved_resources WHERE id = $1",
    )
    .bind(&good_id)
    .fetch_optional(&pool)
    .await
    .unwrap();
    let (sub, rg) = scope.expect("the well-formed overlay-only row is served");
    assert_eq!(sub, uuid::Uuid::parse_str(SUB).unwrap(), "subscription derived");
    assert_eq!(rg, "rg-derived", "resource_group derived from the id");

    // A malformed-id overlay present row (subscription segment is NOT a UUID) is EXCLUDED —
    // never served with a NULL / out-of-scope subscription.
    let bad_id =
        "/subscriptions/not-a-uuid/resourceGroups/rg-x/providers/Microsoft.Storage/storageAccounts/res-bad";
    insert_overlay(
        &pool,
        bad_id,
        "resource",
        true,
        Some(valid_resource_body(bad_id, "res-bad")),
    )
    .await
    .expect("the overlay INSERT itself is accepted (id shape is not a sql/009 CHECK)");
    let n: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM synthetic.arm_resolved_resources WHERE id = $1",
    )
    .bind(bad_id)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(n, 0, "a malformed-id overlay row fails closed (zero rows served)");

    // And it NEVER leaks a NULL-scope row into the resolved view.
    let null_scope: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM synthetic.arm_resolved_resources WHERE subscription_id IS NULL",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(null_scope, 0, "no resolved resource row ever has a NULL subscription");
}

// --------------------------------------------------------------------------------------- //
// Deep structural inventory (fails loudly on a damaged element)
// --------------------------------------------------------------------------------------- //

/// Provision a correct resolver schema, assert the inventory passes (no false-positive),
/// then apply `damage` DDL and assert the inventory now fails with a message mentioning
/// `needle`. Each case spins its own container for full independence.
async fn inventory_damage_case(damage: &[&str], needle: &str) {
    let (pool, _c) = start_pg().await;
    seed_resolver_first_boot(&pool).await;
    tenantless_server::arm_resolver_inventory(&pool)
        .await
        .expect("baseline: correctly-provisioned resolver passes the inventory");
    for sql in damage {
        sqlx::raw_sql(sql)
            .execute(&pool)
            .await
            .unwrap_or_else(|e| panic!("apply damage {sql:?}: {e}"));
    }
    let err = tenantless_server::arm_resolver_inventory(&pool)
        .await
        .expect_err("a damaged element must fail the inventory");
    assert!(
        err.contains(needle),
        "inventory error {err:?} must name the damaged element ({needle:?})"
    );
}

#[tokio::test]
async fn resolver_inventory_deep() {
    // A dropped resolved view is caught.
    inventory_damage_case(
        &["DROP VIEW synthetic.arm_resolved_resources"],
        "missing view synthetic.arm_resolved_resources",
    )
    .await;

    // A resolved view missing a required column is caught (drop + recreate without managed_by).
    inventory_damage_case(
        &[
            "DROP VIEW synthetic.arm_resolved_resources",
            "CREATE VIEW synthetic.arm_resolved_resources AS \
             SELECT id, name, type, location, tags, sku, kind, properties, subscription_id, \
                    resource_group_name, provisioning_state \
             FROM synthetic.resources",
        ],
        "missing column managed_by",
    )
    .await;

    // A resolved view with a MISTYPED column is caught (subscription_id cast to text).
    inventory_damage_case(
        &[
            "DROP VIEW synthetic.arm_resolved_resources",
            "CREATE VIEW synthetic.arm_resolved_resources AS \
             SELECT id, name, type, location, tags, sku, kind, properties, \
                    subscription_id::text AS subscription_id, \
                    resource_group_name, provisioning_state, managed_by \
             FROM synthetic.resources",
        ],
        "subscription_id",
    )
    .await;

    // An absent storage_mode is caught.
    inventory_damage_case(
        &["ALTER TABLE synthetic.drift_batches DROP COLUMN storage_mode"],
        "storage_mode",
    )
    .await;

    // A missing overlay resolution index is caught.
    inventory_damage_case(
        &["DROP INDEX synthetic.idx_arm_overlay_kind_id"],
        "idx_arm_overlay_kind_id",
    )
    .await;
}

// --------------------------------------------------------------------------------------- //
// sql/010 initdb-honesty (no transaction-scoped statement in the active DDL)
// --------------------------------------------------------------------------------------- //

/// `sql/010` must contain NO transaction-scoped statement, so it is honest when Docker's
/// `docker-entrypoint-initdb.d` runs it statement-by-statement under autocommit. The bounded
/// `lock_timeout` + serializing advisory lock live in the provisioning paths instead.
#[test]
fn resolver_sql_file_is_initdb_honest() {
    let sql = include_str!("../../sql/010_arm_resolver.sql");
    // Inspect ACTIVE SQL only — strip `--` comments (which necessarily NAME the relocated
    // statements in the documentation block).
    let active: String = sql
        .lines()
        .map(|l| match l.find("--") {
            Some(i) => &l[..i],
            None => l,
        })
        .collect::<Vec<_>>()
        .join("\n");
    assert!(
        !active.contains("SET LOCAL"),
        "sql/010 active DDL must not contain a transaction-scoped SET LOCAL (relocated to the provisioning paths)"
    );
    assert!(
        !active.contains("pg_advisory_xact_lock"),
        "sql/010 active DDL must not contain a transaction-scoped advisory lock (relocated to the provisioning paths)"
    );
    // Belt-and-suspenders: the active DDL must not touch the big resources table.
    assert!(
        !active.contains("ALTER TABLE synthetic.resources"),
        "sql/010 must not ALTER synthetic.resources (deadlock-safety invariant)"
    );
    assert!(
        !active.contains("ON synthetic.resources"),
        "sql/010 must not CREATE INDEX on synthetic.resources (deadlock-safety invariant)"
    );
}
