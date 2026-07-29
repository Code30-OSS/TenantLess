-- Referential integrity + read-path index hardening (SEC-MED-3).
-- Auto-applied on first Postgres start via docker-entrypoint-initdb.d

-- 1. Case-insensitive uniqueness and index backing for read-path query: lower(id) = lower($1)
-- Converting to a UNIQUE INDEX prevents inserting duplicate IDs that differ only by case (e.g., 'res-1' vs 'RES-1')
CREATE UNIQUE INDEX IF NOT EXISTS idx_res_lower_id
    ON synthetic.resources (lower(id));

-- 2. Subscription FK on resources with explicit cascade deletion
DO $$
BEGIN
    ALTER TABLE synthetic.resources
        ADD CONSTRAINT fk_resources_subscription
        FOREIGN KEY (subscription_id)
        REFERENCES synthetic.subscriptions (subscription_id)
        ON DELETE CASCADE;
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

-- 3. Resource FK on violations with explicit cascade deletion
DO $$
BEGIN
    ALTER TABLE synthetic.violations
        ADD CONSTRAINT fk_violations_resource
        FOREIGN KEY (resource_id)
        REFERENCES synthetic.resources (id)
        ON DELETE CASCADE;
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

-- 4. Cross-subscription dependencies FKs
-- Using DEFERRABLE INITIALLY DEFERRED solves the generator order dependency during bulk COPY seeding,
-- while still guaranteeing strict API referential integrity at transaction commit time.
DO $$
BEGIN
    ALTER TABLE synthetic.dependencies
        ADD CONSTRAINT fk_dep_source_resource
        FOREIGN KEY (source_resource_id)
        REFERENCES synthetic.resources (id)
        ON DELETE CASCADE
        DEFERRABLE INITIALLY DEFERRED,

        ADD CONSTRAINT fk_dep_target_resource
        FOREIGN KEY (target_resource_id)
        REFERENCES synthetic.resources (id)
        ON DELETE CASCADE
        DEFERRABLE INITIALLY DEFERRED;
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;
