-- Configuration-drift tables (DRIFT-03/DRIFT-04) — the drift audit store the
-- simulator-only /simulator/drift* reads (handlers/drift.rs, later Plan 11-03)
-- serve, plus the additive `synthetic.resources.drift_deleted_at` soft-delete
-- column the server's list/detail filter (Plan 11-02) requires. Auto-applied on
-- first Postgres start via docker-entrypoint-initdb.d, and applied by the
-- testcontainers harness (mock-server/tests/common/mod.rs) +
-- writer.ensure_drift_schema.
-- This file ONLY adds the drift tables + the resources ALTER; sql/001..005 are never edited.
--
-- Design notes:
--   * Batch-level fields (drift_type, seed, options, parent/result fingerprints,
--     applied_at, reverted_at) live on drift_batches; per-field deltas on
--     drift_records (D-08, schema-shape discretion).
--   * `reverted_at` is a NULLABLE mark set on revert; a drift batch and its
--     records are NEVER deleted — the history is preserved for audit (D-03).
--   * `before` / `after` store the FULL pre/post served column value (A2) so a
--     column-level revert is a clean overwrite; `field_path` is audit
--     readability (DRIFT-04).
--   * `drift_deleted_at` on synthetic.resources is nullable with NO default, so
--     the additive ALTER is a metadata-only change (no table rewrite) and every
--     existing row stays NULL — existing counts are unchanged (RESEARCH Pitfall 2).
--   * Idempotent (CREATE ... IF NOT EXISTS + ADD COLUMN IF NOT EXISTS + guarded FK
--     DO block) so re-applying the migration on an already-migrated schema is a
--     no-op rather than an error (mirrors sql/004_cost.sql / sql/005_identity.sql).
--   * `seq` on drift_batches is a monotonic, unique, strictly-increasing total
--     order assigned at INSERT time (GENERATED ALWAYS AS IDENTITY). It is the
--     authoritative LIFO order for revert: it BREAKS `applied_at` ties (two
--     same-instant batches get distinct seq), so the strict-LIFO guard compares
--     `b.seq > target.seq` with no possibility of a same-time deadlock. Added to
--     the CREATE def (fresh volumes) AND via an additive ADD COLUMN IF NOT EXISTS
--     (pre-existing volumes — existing rows are back-filled in physical order at
--     migration time, every new INSERT is strictly greater).
--   * `drift_code` / `metadata` on drift_records hold the computed mutation code
--     and per-record metadata (consumed by the follow-on remediation). Both are
--     NULLABLE additive columns (no default → metadata-only ALTER, existing rows
--     stay NULL, existing counts unchanged).
--   * STATIC DDL only — no runtime/user/profile input is spliced; no injection surface.

CREATE TABLE IF NOT EXISTS synthetic.drift_batches (
    batch_id            UUID PRIMARY KEY,
    drift_type          TEXT NOT NULL,             -- chaos | temporal
    seed                BIGINT NOT NULL,
    options             JSONB NOT NULL,
    parent_fingerprint  TEXT NOT NULL,             -- pre-drift state fingerprint (D-08)
    result_fingerprint  TEXT NOT NULL,             -- post-drift state fingerprint (D-08)
    applied_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    reverted_at         TIMESTAMPTZ,                -- nullable; mark on revert, NEVER delete (D-03)
    seq                 BIGINT GENERATED ALWAYS AS IDENTITY  -- monotonic total order; the LIFO authority (breaks applied_at ties)
);

CREATE TABLE IF NOT EXISTS synthetic.drift_records (
    record_id        BIGSERIAL PRIMARY KEY,
    batch_id         UUID NOT NULL,
    resource_id      TEXT NOT NULL,
    subscription_id  UUID,                          -- nullable; the drifted resource's sub (DRIFT-04)
    field_path       TEXT NOT NULL,                 -- audit readability (DRIFT-04)
    before           JSONB,                         -- full pre-mutation column value (A2 — clean column-level revert)
    after            JSONB,                         -- full post-mutation column value
    drift_code       TEXT,                          -- computed mutation code (nullable; populated by remediation 2/3)
    metadata         JSONB                          -- per-record metadata (nullable; populated by remediation 2/3)
);

-- Additive total-order column on a pre-existing drift_batches. GENERATED ALWAYS
-- AS IDENTITY back-fills existing rows in physical order and makes every new
-- INSERT strictly greater.
--
-- NB: this uses a guarded DO block (duplicate_column), NOT `ADD COLUMN IF NOT
-- EXISTS`. On PostgreSQL 11 (the testcontainers fixture image) `ADD COLUMN IF
-- NOT EXISTS … GENERATED ALWAYS AS IDENTITY` does NOT skip the identity-sequence
-- creation when the column already exists — it leaves a SECOND owned sequence,
-- so the column ends up owning multiple sequences and every INSERT then fails
-- with "more than one owned sequence found". The DO block instead lets the plain
-- `ADD COLUMN` raise `duplicate_column`, which rolls the whole statement back
-- (no orphan sequence) — clean and idempotent on both PG11 and PG16.
DO $$
BEGIN
    ALTER TABLE synthetic.drift_batches
        ADD COLUMN seq BIGINT GENERATED ALWAYS AS IDENTITY;
EXCEPTION
    WHEN duplicate_column THEN NULL;
END $$;

-- Additive audit columns on a pre-existing drift_records (NULLABLE, no default →
-- metadata-only ALTER; existing rows stay NULL, counts unchanged).
ALTER TABLE synthetic.drift_records
    ADD COLUMN IF NOT EXISTS drift_code TEXT;
ALTER TABLE synthetic.drift_records
    ADD COLUMN IF NOT EXISTS metadata JSONB;

-- The seq total order is unique (IDENTITY never repeats within the sequence); the
-- explicit unique index makes the contract enforced AND backs the LIFO guard's
-- `b.seq > target.seq` range scan.
CREATE UNIQUE INDEX IF NOT EXISTS idx_drift_batches_seq
    ON synthetic.drift_batches (seq);

-- Backs the per-batch audit read (handlers/drift.rs get_batch: WHERE batch_id = $1).
CREATE INDEX IF NOT EXISTS idx_drift_rec_batch
    ON synthetic.drift_records (batch_id);

-- Backs the per-resource audit read (handlers/drift.rs by_resource: WHERE resource_id = $1).
CREATE INDEX IF NOT EXISTS idx_drift_rec_resource
    ON synthetic.drift_records (resource_id);

-- Safe FK: every drift record references a real batch. Idempotent via a guarded
-- DO block so a re-apply on an already-migrated schema is a no-op rather than an
-- error (mirrors sql/004_cost.sql / sql/005_identity.sql).
DO $$
BEGIN
    ALTER TABLE synthetic.drift_records
        ADD CONSTRAINT fk_drift_batch
        FOREIGN KEY (batch_id)
        REFERENCES synthetic.drift_batches (batch_id);
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

-- Additive soft-delete column on the existing resources table. NULLABLE with NO
-- default → metadata-only ALTER (no table rewrite), existing rows stay NULL, and
-- existing counts are unchanged (RESEARCH Pitfall 2). The server's list/detail
-- filter (Plan 11-02) hides rows where this is non-NULL.
ALTER TABLE synthetic.resources
    ADD COLUMN IF NOT EXISTS drift_deleted_at TIMESTAMPTZ;
