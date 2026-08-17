//! DB-backed fail-closed / boots proofs for the provenance-based boot guard
//! (`assert_no_legacy_inplace_drift`).
//!
//! The current design does NOT migrate historical in-place drift (reset-cutover). The guard is the
//! safety net: a tenant still carrying legacy in-place drift (applied by the pre-v3 binary)
//! must REFUSE to boot with the exact locked message, while a valid post-cutover tenant with
//! ACTIVE OVERLAY drift (`storage_mode='overlay'`) boots normally. The `storage_mode`
//! provenance marker is what tells the two apart.
//!
//! Every test spins its OWN ephemeral testcontainers Postgres and provisions a bare base +
//! overlay + resolver substrate via `common::seed_overlay_first_boot` + `ensure_arm_resolver_schema`
//! (NOT `seed_fixture` — project memory: fixture coupling), then seeds each scenario directly in
//! SQL (`apply-drift`/`cli.py` is NOT needed here). One test additionally proves the guard is
//! reusable across a reset (dirty → Err, cleared → Ok on the same pool).
//!
//! Coverage:
//!   * `legacy_synthetic_active_batch_fails_closed`  — storage_mode='synthetic', reverted_at NULL → Err(locked).
//!   * `soft_delete_drift_deleted_at_fails_closed`   — a synthetic.resources row with drift_deleted_at set → Err(locked).
//!   * `active_overlay_batch_boots`                  — storage_mode='overlay', reverted_at NULL → Ok (the provenance point).
//!   * `clean_tenant_boots`                          — no drift at all → Ok.
//!   * `reverted_synthetic_batch_boots`              — a REVERTED legacy batch (reverted_at set) → Ok (reverted_at conjunct).
//!   * `guard_reset_between_scenarios_is_reusable`   — dirty → Err, TRUNCATE → Ok, overlay-active → Ok on one pool.
//!   * `post_reset_state_boots_direct_sql_simulation` — seed legacy batch + present overlay → Err, then
//!       DIRECT-SQL simulate reset's DELETE scope (drift_records → drift_batches → arm_overlay) → Ok. This is a
//!       DB-side simulation of reset's documented DELETE scope, COMPLEMENTARY to the authoritative Python `reset`
//!       proof in `tests/test_reset.py` — it does NOT invoke the Python command (no cross-language process calls).

mod common;

use sqlx::PgPool;
use testcontainers_modules::{postgres, testcontainers::runners::AsyncRunner};
use uuid::Uuid;

/// The byte-exact locked fail-closed message (must match `assert_no_legacy_inplace_drift`).
const LOCKED_MSG: &str =
    "Applied in-place drift detected. Revert drift or regenerate the tenant before restarting.";

const SUB: &str = "11111111-1111-1111-1111-111111111111";

/// Start an ephemeral Postgres container and return a connected pool plus the container
/// guard (kept alive for the test's duration). Mirrors the `start_pg` in the other suites.
async fn start_pg() -> (PgPool, testcontainers::ContainerAsync<postgres::Postgres>) {
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

/// Model a valid boot substrate: base schema (001..007) + overlay (009) via the shared
/// `seed_overlay_first_boot` harness, then apply `ensure_arm_resolver_schema` so
/// `synthetic.drift_batches.storage_mode` exists (the guard reads it). No drift is seeded yet.
async fn boot_ready(pool: &PgPool) {
    common::seed_overlay_first_boot(pool).await;
    tenantless_server::ensure_arm_resolver_schema(pool)
        .await
        .expect("ensure_arm_resolver_schema (storage_mode + resolved views + overlay index)");
}

/// INSERT a `synthetic.drift_batches` row with the given `storage_mode`; `reverted` chooses an
/// active (`reverted_at IS NULL`) vs a reverted (`reverted_at = now()`) batch. Literals are
/// bound as `$N` (memory [[mock-server-sql-injection-bar]]); the `reverted_at` branch is a
/// fixed SQL fragment (no external input spliced).
async fn insert_batch(pool: &PgPool, storage_mode: &str, reverted: bool) -> Uuid {
    let id = Uuid::new_v4();
    let q = if reverted {
        "INSERT INTO synthetic.drift_batches \
             (batch_id, drift_type, seed, options, parent_fingerprint, result_fingerprint, \
              storage_mode, reverted_at) \
         VALUES ($1, 'chaos', 1, '{}'::jsonb, 'p', 'r', $2, now())"
    } else {
        "INSERT INTO synthetic.drift_batches \
             (batch_id, drift_type, seed, options, parent_fingerprint, result_fingerprint, \
              storage_mode, reverted_at) \
         VALUES ($1, 'chaos', 1, '{}'::jsonb, 'p', 'r', $2, NULL)"
    };
    sqlx::query(q)
        .bind(id)
        .bind(storage_mode)
        .execute(pool)
        .await
        .expect("insert drift batch");
    id
}

/// Insert the minimal FK chain (tenant → subscription → resource_group → resource) and set the
/// resource's `drift_deleted_at` — the legacy soft-delete (disappear) in-place mutation the
/// guard must detect independent of `drift_batches`.
async fn insert_soft_deleted_resource(pool: &PgPool) {
    let sub = Uuid::parse_str(SUB).unwrap();
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
    sqlx::query(
        "INSERT INTO synthetic.resource_groups \
             (id, subscription_id, name, location, template_type, tags, provisioning_state) \
         VALUES ('/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-x', \
                 $1, 'rg-x', 'eastus', 'network', '{}'::jsonb, 'Succeeded') \
         ON CONFLICT DO NOTHING",
    )
    .bind(sub)
    .execute(pool)
    .await
    .expect("resource group");
    sqlx::query(
        "INSERT INTO synthetic.resources \
             (id, subscription_id, resource_group_name, name, type, location, tags, sku, \
              kind, properties, provisioning_state, managed_by, drift_deleted_at) \
         VALUES ('/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-x/\
providers/Microsoft.Storage/storageAccounts/res-x', \
                 $1, 'rg-x', 'res-x', 'Microsoft.Storage/storageAccounts', 'eastus', \
                 '{}'::jsonb, NULL, NULL, '{}'::jsonb, 'Succeeded', NULL, now())",
    )
    .bind(sub)
    .execute(pool)
    .await
    .expect("soft-deleted resource");
}

// The `insert_present_overlay_row` seeder was RELOCATED to `tests/common/mod.rs` as a
// `pub async fn` so the sibling `run_reset`/restore full-wipe proofs in the
// integration.rs and control.rs test binaries can reach it. Called below as
// `common::insert_present_overlay_row`.

// ---------------------------------------------------------------------------------------------
// Fail-closed scenarios
// ---------------------------------------------------------------------------------------------

#[tokio::test]
async fn legacy_synthetic_active_batch_fails_closed() {
    let (pool, _c) = start_pg().await;
    boot_ready(&pool).await;
    insert_batch(&pool, "synthetic", false).await;

    let err = tenantless_server::assert_no_legacy_inplace_drift(&pool)
        .await
        .expect_err("an active legacy in-place batch must fail closed");
    assert_eq!(err, LOCKED_MSG, "fail-closed message must be byte-exact");
}

#[tokio::test]
async fn soft_delete_drift_deleted_at_fails_closed() {
    let (pool, _c) = start_pg().await;
    boot_ready(&pool).await;
    insert_soft_deleted_resource(&pool).await;

    let err = tenantless_server::assert_no_legacy_inplace_drift(&pool)
        .await
        .expect_err("a drift_deleted_at row must fail closed");
    assert_eq!(err, LOCKED_MSG, "fail-closed message must be byte-exact");
}

// ---------------------------------------------------------------------------------------------
// Boots scenarios
// ---------------------------------------------------------------------------------------------

#[tokio::test]
async fn active_overlay_batch_boots() {
    // The provenance point: an ACTIVE overlay batch (the new apply-drift path) must NOT trip the
    // guard even though `reverted_at IS NULL` — this is what a `storage_mode='synthetic'`-only
    // conjunction would (wrongly) brick.
    let (pool, _c) = start_pg().await;
    boot_ready(&pool).await;
    insert_batch(&pool, "overlay", false).await;

    tenantless_server::assert_no_legacy_inplace_drift(&pool)
        .await
        .expect("an active OVERLAY batch must boot (provenance conjunct)");
}

#[tokio::test]
async fn clean_tenant_boots() {
    let (pool, _c) = start_pg().await;
    boot_ready(&pool).await;

    tenantless_server::assert_no_legacy_inplace_drift(&pool)
        .await
        .expect("a clean tenant (no drift) must boot");
}

#[tokio::test]
async fn reverted_synthetic_batch_boots() {
    // A legacy in-place batch that has already been REVERTED (reverted_at set) is not active, so
    // the guard does not trip — proves the `reverted_at IS NULL` half of the conjunct.
    let (pool, _c) = start_pg().await;
    boot_ready(&pool).await;
    insert_batch(&pool, "synthetic", true).await;

    tenantless_server::assert_no_legacy_inplace_drift(&pool)
        .await
        .expect("a reverted legacy batch must boot");
}

#[tokio::test]
async fn guard_reset_between_scenarios_is_reusable() {
    // One pool, three states: an active legacy batch fails closed; clearing it boots; an active
    // overlay batch still boots. Proves the guard is a pure read reusable across a reset.
    let (pool, _c) = start_pg().await;
    boot_ready(&pool).await;

    insert_batch(&pool, "synthetic", false).await;
    let err = tenantless_server::assert_no_legacy_inplace_drift(&pool)
        .await
        .expect_err("legacy batch must fail closed");
    assert_eq!(err, LOCKED_MSG);

    sqlx::query("TRUNCATE synthetic.drift_batches CASCADE")
        .execute(&pool)
        .await
        .expect("clear drift batches");
    tenantless_server::assert_no_legacy_inplace_drift(&pool)
        .await
        .expect("cleared tenant must boot");

    insert_batch(&pool, "overlay", false).await;
    tenantless_server::assert_no_legacy_inplace_drift(&pool)
        .await
        .expect("active overlay batch must still boot after reset");
}

#[tokio::test]
async fn post_reset_state_boots_direct_sql_simulation() {
    // COMPLEMENTARY to the AUTHORITATIVE Python proof in `tests/test_reset.py` (which drives
    // the real `reset` CLI). This case does NOT invoke the Python `reset` command — boot_guard.rs
    // makes no cross-language process calls. It DIRECTLY SIMULATES reset's documented DELETE
    // scope in raw SQL to prove that a tenant left in reset's POST-DELETE state boots clean.
    //
    // Steps: seed a legacy in-place drift batch AND a present arm_overlay row via raw SQL →
    // confirm the guard fails closed while the legacy batch is active → SIMULATE reset's
    // FK-ordered DELETE scope (drift_records → drift_batches → arm_overlay) with direct SQL →
    // assert `assert_no_legacy_inplace_drift` returns Ok(()) and the overlay is empty.
    let (pool, _c) = start_pg().await;
    boot_ready(&pool).await;

    // A legacy in-place batch (the guard trips) + a present overlay row (reset's DELETE scope
    // is extended to also clear the overlay, so the simulation seeds one to clear).
    insert_batch(&pool, "synthetic", false).await;
    common::insert_present_overlay_row(&pool).await;

    let err = tenantless_server::assert_no_legacy_inplace_drift(&pool)
        .await
        .expect_err("an active legacy in-place batch must fail closed pre-reset");
    assert_eq!(err, LOCKED_MSG, "fail-closed message must be byte-exact");

    // Direct-SQL simulation of reset's DELETE scope (matches cli.py::reset's FK order):
    // drift_records (child) → drift_batches (parent) → arm_overlay. NOT a Python invocation.
    for stmt in [
        "DELETE FROM synthetic.drift_records",
        "DELETE FROM synthetic.drift_batches",
        "DELETE FROM synthetic.arm_overlay",
    ] {
        sqlx::query(stmt)
            .execute(&pool)
            .await
            .unwrap_or_else(|e| panic!("simulate reset delete ({stmt}): {e}"));
    }

    // The post-reset-STATE tenant boots (no active legacy in-place drift remains).
    tenantless_server::assert_no_legacy_inplace_drift(&pool)
        .await
        .expect("a tenant in reset's post-DELETE state must boot (Ok)");

    // The overlay clear (reset's extended scope) actually happened.
    let overlay_rows: i64 = sqlx::query_scalar("SELECT count(*) FROM synthetic.arm_overlay")
        .fetch_one(&pool)
        .await
        .expect("count overlay rows");
    assert_eq!(overlay_rows, 0, "reset simulation must clear the overlay");
}
