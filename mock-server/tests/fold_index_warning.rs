//! Boot-time (read-only) probe for the ARM-ID fold expression indexes.
//!
//! Every identity lookup compares `synthetic.arm_id_key(id)` / `synthetic.ascii_fold(...)`,
//! which only the fold indexes serve. The server never builds them at boot (a plain
//! `CREATE INDEX` on a populated table would take ACCESS EXCLUSIVE): `tenantless init-db` /
//! `tenantless generate` build them CONCURRENTLY. A volume upgraded by `serve` alone
//! therefore lacks them, and the server must say so loudly instead of silently scanning.

mod common;

use sqlx::Executor;
use sqlx::PgPool;
use testcontainers_modules::{postgres, testcontainers::runners::AsyncRunner};

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

#[tokio::test]
async fn missing_fold_indexes_are_reported_and_warned_about() {
    let (pool, _c) = start_pg().await;
    common::seed_empty_tenant(&pool).await;
    tenantless_server::ensure_arm_id_key_schema(&pool)
        .await
        .expect("fold functions");

    // Given a volume with no fold indexes, Then both are reported missing and warned about
    let missing = tenantless_server::missing_arm_id_fold_indexes(&pool)
        .await
        .expect("probe");
    assert_eq!(
        missing,
        vec![
            "synthetic.idx_res_arm_id_key".to_string(),
            "synthetic.idx_res_rg_ascii_fold".to_string()
        ]
    );
    let warning =
        tenantless_server::fold_index_boot_warning(&missing).expect("a warning is produced");
    assert!(warning.contains("tenantless init-db"), "{warning}");
    assert!(warning.contains("idx_res_arm_id_key"), "{warning}");

    // When one exists but is INVALID (an interrupted concurrent build), Then it still counts
    pool.execute(
        "CREATE INDEX idx_res_arm_id_key ON synthetic.resources (synthetic.arm_id_key(id))",
    )
    .await
    .expect("index");
    pool.execute(
        "UPDATE pg_index SET indisvalid = false \
         WHERE indexrelid = 'synthetic.idx_res_arm_id_key'::regclass",
    )
    .await
    .expect("mark invalid");
    let missing = tenantless_server::missing_arm_id_fold_indexes(&pool)
        .await
        .expect("probe");
    assert_eq!(
        missing.len(),
        2,
        "an invalid index serves nothing: {missing:?}"
    );

    // When both are present and valid, Then nothing is missing and no warning is produced
    pool.execute(
        "UPDATE pg_index SET indisvalid = true \
         WHERE indexrelid = 'synthetic.idx_res_arm_id_key'::regclass",
    )
    .await
    .expect("mark valid");
    pool.execute(
        "CREATE INDEX idx_res_rg_ascii_fold ON synthetic.resources \
         (subscription_id, synthetic.ascii_fold(resource_group_name), id)",
    )
    .await
    .expect("index");
    let missing = tenantless_server::missing_arm_id_fold_indexes(&pool)
        .await
        .expect("probe");
    assert!(missing.is_empty(), "{missing:?}");
    assert!(tenantless_server::fold_index_boot_warning(&missing).is_none());
}
