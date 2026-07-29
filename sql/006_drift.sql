-- Configuration-drift tables (DRIFT-03/DRIFT-04) — the drift audit store.
-- Auto-applied on first Postgres start via docker-entrypoint-initdb.d

-- Enum type for drift operations
CREATE TYPE synthetic.drift_type_enum AS ENUM ('chaos', 'temporal');

CREATE TABLE IF NOT EXISTS synthetic.drift_batches (
    batch_id            UUID PRIMARY KEY,
    drift_type          synthetic.drift_type_enum NOT NULL,
    seed                BIGINT NOT NULL,
    options             JSONB NOT NULL DEFAULT '{}'::jsonb,
    parent_fingerprint  VARCHAR(255) NOT NULL,
    result_fingerprint  VARCHAR(255) NOT NULL,
    applied_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    reverted_at         TIMESTAMPTZ,
    seq                 BIGINT GENERATED ALWAYS AS IDENTITY,

    -- Enforce logical time ordering: revert timestamp cannot be earlier than application timestamp
    CONSTRAINT chk_reverted_after_applied CHECK (reverted_at IS NULL OR reverted_at >= applied_at),
    -- Prevent invalid non-object payload injection
    CONSTRAINT chk_drift_options_is_object CHECK (jsonb_typeof(options) = 'object')
);

CREATE TABLE IF NOT EXISTS synthetic.drift_records (
    record_id        BIGSERIAL PRIMARY KEY,
    batch_id         UUID NOT NULL,
    resource_id      VARCHAR(512) NOT NULL,
    subscription_id  UUID,
    field_path       VARCHAR(255) NOT NULL,
    before           JSONB,
    after            JSONB,
    drift_code       VARCHAR(100),
    metadata         JSONB,

    -- Prevent invalid non-object payload injection when metadata is provided
    CONSTRAINT chk_drift_meta_is_object CHECK (metadata IS NULL OR jsonb_typeof(metadata) = 'object')
);

-- Idempotent DO block handling sequence column addition for PG11/16 compatibility
DO $$
BEGIN
    ALTER TABLE synthetic.drift_batches
        ADD COLUMN seq BIGINT GENERATED ALWAYS AS IDENTITY;
EXCEPTION
    WHEN duplicate_column THEN NULL;
END $$;

-- Additive audit columns on pre-existing drift_records
ALTER TABLE synthetic.drift_records
    ADD COLUMN IF NOT EXISTS drift_code VARCHAR(100);
ALTER TABLE synthetic.drift_records
    ADD COLUMN IF NOT EXISTS metadata JSONB;

-- Indexes for performance and total ordering
CREATE UNIQUE INDEX IF NOT EXISTS idx_drift_batches_seq
    ON synthetic.drift_batches (seq);

CREATE INDEX IF NOT EXISTS idx_drift_rec_batch
    ON synthetic.drift_records (batch_id);

CREATE INDEX IF NOT EXISTS idx_drift_rec_resource
    ON synthetic.drift_records (resource_id);

-- Safe FK constraints with explicit CASCADE behavior
DO $$
BEGIN
    ALTER TABLE synthetic.drift_records
        ADD CONSTRAINT fk_drift_batch
        FOREIGN KEY (batch_id)
        REFERENCES synthetic.drift_batches (batch_id)
        ON DELETE CASCADE,

        ADD CONSTRAINT fk_drift_resource
        FOREIGN KEY (resource_id)
        REFERENCES synthetic.resources (id)
        ON DELETE CASCADE;
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

-- Additive soft-delete column on resources
ALTER TABLE synthetic.resources
    ADD COLUMN IF NOT EXISTS drift_deleted_at TIMESTAMPTZ;

-- Partial index backing the server's primary list/detail query filter (WHERE drift_deleted_at IS NULL).
-- Highly optimizes active resource lookups on large tables (500k+ rows) without indexing soft-deleted rows.
CREATE INDEX IF NOT EXISTS idx_res_active
    ON synthetic.resources (id)
    WHERE drift_deleted_at IS NULL;
