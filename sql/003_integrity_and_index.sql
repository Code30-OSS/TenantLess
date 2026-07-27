-- Referential integrity + read-path index (SEC-MED-3).
-- Auto-applied on first Postgres start via docker-entrypoint-initdb.d, and applied
-- by the testcontainers harness (mock-server/tests/common/mod.rs seed_fixture).
-- This file ONLY adds constraints/indexes; sql/001 and sql/002 are never edited.
--
-- FK disposition:
--   * synthetic.resources.subscription_id -> synthetic.subscriptions  : ADDED.
--       Every resource is minted under one of its tenant's subscriptions; the
--       generator resolves subscriptions before resources, so no dangling ref
--       exists by construction. The testcontainers fixture seeds all resources
--       under SUB_A (a seeded subscription), so the FK holds for the fixture too.
--   * synthetic.violations.resource_id -> synthetic.resources(id)     : ADDED.
--       Violations are recorded against an existing, just-minted resource (the
--       injector mutates a resource in place and records one row). The fixture
--       seeds zero violation rows, so the FK trivially holds for the fixture.
--   * synthetic.dependencies.source_resource_id / target_resource_id  : DEFERRED.
--       Dependencies INTENTIONALLY reference resources ACROSS subscriptions, and
--       the cross-sub topology is a synthetic-only artifact whose seeding order
--       and host-anchor minting are owned by the generator's cross_sub pass. A
--       hard FK here risks coupling to that pass's internal ordering and to the
--       shared fixture; per the data-boundary/fixture-coupling constraint we do
--       NOT add it here. Referential validity is already enforced at generation
--       time by the pre-COPY 0-dangling anti-join gate (XSUB-06). Revisit if/when
--       the dependency seeding is brought under the same FK-ordered guarantee.

-- (a) Functional index backing the case-insensitive resource-detail read path
-- (resource_detail.rs: lower(id) = lower($1)). The PK index on id is not usable
-- for the lower(id) predicate, so this functional index is what that lookup hits.
CREATE INDEX IF NOT EXISTS idx_res_lower_id
    ON synthetic.resources (lower(id));

-- (b) Safe FK constraints. Idempotent via a guarded DO block so a re-apply on an
-- already-migrated schema is a no-op rather than an error.
DO $$
BEGIN
    ALTER TABLE synthetic.resources
        ADD CONSTRAINT fk_resources_subscription
        FOREIGN KEY (subscription_id)
        REFERENCES synthetic.subscriptions (subscription_id);
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    ALTER TABLE synthetic.violations
        ADD CONSTRAINT fk_violations_resource
        FOREIGN KEY (resource_id)
        REFERENCES synthetic.resources (id);
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;
