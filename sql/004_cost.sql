-- FinOps cost fact table (COST-01/02/03) — the narrow per-resource monthly cost
-- Auto-applied on first Postgres start via docker-entrypoint-initdb.d

CREATE TABLE IF NOT EXISTS synthetic.cost_records (
    resource_id     VARCHAR(512) NOT NULL,
    subscription_id UUID NOT NULL,
    billing_period  DATE NOT NULL,
    -- NUMERIC(15,4) guarantees exact financial precision and prevents floating-point rounding errors
    cost_amount     NUMERIC(15, 4) NOT NULL,
    -- Restrict currency to 3-letter ISO code format (e.g., 'USD', 'EUR')
    currency        VARCHAR(3) NOT NULL,
    
    PRIMARY KEY (resource_id, billing_period),

    -- Prevent invalid negative monetary amounts from API ingestion
    CONSTRAINT chk_cost_amount_positive CHECK (cost_amount >= 0),
    CONSTRAINT chk_currency_format CHECK (length(currency) = 3)
);

-- Note: The original 'idx_cost_resource' index on (resource_id) was removed because
-- the Primary Key (resource_id, billing_period) already covers queries filtering by resource_id.

-- Backs timeframe range scans filtering on billing_period and joining on resource_id
CREATE INDEX IF NOT EXISTS idx_cost_period_resource
    ON synthetic.cost_records (billing_period, resource_id);

-- Safe FK constraints with explicit ON DELETE CASCADE behavior
DO $$
BEGIN
    ALTER TABLE synthetic.cost_records
        ADD CONSTRAINT fk_cost_resource
        FOREIGN KEY (resource_id)
        REFERENCES synthetic.resources (id)
        ON DELETE CASCADE,

        ADD CONSTRAINT fk_cost_subscription
        FOREIGN KEY (subscription_id)
        REFERENCES synthetic.subscriptions (subscription_id)
        ON DELETE CASCADE;
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;
