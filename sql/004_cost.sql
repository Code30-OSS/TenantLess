-- FinOps cost fact table (COST-01/02/03) — the narrow per-resource monthly cost
-- store the Cost Management Query API (handlers/cost.rs) reads + aggregates.
-- Auto-applied on first Postgres start via docker-entrypoint-initdb.d, and applied
-- by the testcontainers harness (mock-server/tests/common/mod.rs seed_fixture).
-- This file ONLY adds the cost table + its indexes/FK; sql/001..003 are never edited.
--
-- Design notes:
--   * Narrow fact table (D-06): ResourceType / ResourceGroup / ServiceName / Tag
--     dimensions are NOT duplicated onto cost rows — the handler JOINs to
--     synthetic.resources on resource_id and groups by the resource's existing
--     columns/tags JSONB. One source of truth for tags; one cost number per row.
--   * Composite PK (resource_id, billing_period) (RESEARCH Open Question 2): unique
--     per grain (monthly default, daily opt-in), no SERIAL needed for binary COPY,
--     and it doubly enforces "one row per resource per period".
--   * Column contract matches writer.copy_cost_records exactly:
--       (resource_id, subscription_id, billing_period, cost_amount, currency)
--       → (text, uuid, date, float8, text)
--   * Idempotent (CREATE ... IF NOT EXISTS + guarded FK DO block) so re-applying the
--     migration on an already-migrated schema is a no-op rather than an error.

CREATE TABLE IF NOT EXISTS synthetic.cost_records (
    resource_id     TEXT NOT NULL,
    subscription_id UUID NOT NULL,
    billing_period  DATE NOT NULL,
    cost_amount     FLOAT8 NOT NULL,
    currency        TEXT NOT NULL,
    PRIMARY KEY (resource_id, billing_period)
);

-- (a) Backs the D-06 join (cost_records ⋈ resources on resource_id) that derives the
-- ResourceType/ResourceGroup/ServiceName/Tag dimensions in the handler.
CREATE INDEX IF NOT EXISTS idx_cost_resource
    ON synthetic.cost_records (resource_id);

-- (b) Covering shape for the D-05 group-bys: the timeframe range scans
-- billing_period and the join keys off resource_id, so a (billing_period,
-- resource_id) index lets a grouped query satisfy the range + join from the index.
CREATE INDEX IF NOT EXISTS idx_cost_period_resource
    ON synthetic.cost_records (billing_period, resource_id);

-- (c) Safe FK: every cost row references a real resource (T-9-04, the 0-dangling
-- gate / XSUB-06 analogue). Idempotent via a guarded DO block so a re-apply on an
-- already-migrated schema is a no-op rather than an error (mirrors sql/003).
DO $$
BEGIN
    ALTER TABLE synthetic.cost_records
        ADD CONSTRAINT fk_cost_resource
        FOREIGN KEY (resource_id)
        REFERENCES synthetic.resources (id);
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;
