//! Determinism proof for the shared canonical immutability-hash SQL
//! (`tests/common/immutability_hash.sql`) -- the single source both the
//! baseline-immutability bracket AND the estate hash read
//! verbatim.
//!
//! This harness pins the ONE property the later consumers depend on: computing the
//! digest twice against an UNCHANGED database yields an identical scalar. The
//! baseline-immutability bracket reuses the same SQL to assert
//! `H(before) == H(after apply-drift) == H(after revert-drift)`; if that expression
//! were non-deterministic, the bracket would be meaningless. Proving determinism here,
//! against the shared file, is the foundation.

mod common;

use sqlx::PgPool;
use testcontainers_modules::{postgres, testcontainers::runners::AsyncRunner};

/// The canonical shared hash expression, embedded verbatim from the committed file so
/// the test and every future consumer read the SAME bytes (the single-source rule).
const IMMUTABILITY_HASH_SQL: &str = include_str!("common/immutability_hash.sql");

/// Start an ephemeral Postgres container and return a connected pool plus the container
/// guard (kept alive for the test's duration).
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

/// Baseline-immutability foundation: the shared immutability-hash SQL is deterministic --
/// executed twice against the same unchanged DB it returns an identical, non-empty
/// scalar digest.
#[tokio::test]
async fn immutability_hash_is_deterministic() {
    let (pool, _container) = start_pg().await;
    common::seed_fixture(&pool).await;

    let h1: String = sqlx::query_scalar(IMMUTABILITY_HASH_SQL)
        .fetch_one(&pool)
        .await
        .expect("first immutability-hash compute");
    let h2: String = sqlx::query_scalar(IMMUTABILITY_HASH_SQL)
        .fetch_one(&pool)
        .await
        .expect("second immutability-hash compute");

    assert!(
        !h1.is_empty(),
        "the digest must be a non-empty md5 hex over the seeded baseline"
    );
    assert_eq!(
        h1.len(),
        32,
        "md5 hex is 32 chars (got {}): {h1}",
        h1.len()
    );
    assert_eq!(
        h1, h2,
        "the shared immutability-hash SQL must be deterministic across repeated \
         computes on an unchanged DB -- the baseline-immutability bracket depends on this"
    );
}
