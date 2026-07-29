-- Cross-subscription dependencies and governance violations
-- Auto-applied on first Postgres start via docker-entrypoint-initdb.d

-- Enum type for governance severity control
CREATE TYPE synthetic.violation_severity AS ENUM ('Low', 'Medium', 'High', 'Critical');

-- Cross-subscription dependencies
CREATE TABLE synthetic.dependencies (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dependency_type     VARCHAR(100) NOT NULL,
    source_resource_id  VARCHAR(512) NOT NULL REFERENCES synthetic.resources(id) ON DELETE CASCADE,
    target_resource_id  VARCHAR(512) NOT NULL REFERENCES synthetic.resources(id) ON DELETE CASCADE,
    source_subscription UUID NOT NULL REFERENCES synthetic.subscriptions(subscription_id) ON DELETE CASCADE,
    target_subscription UUID NOT NULL REFERENCES synthetic.subscriptions(subscription_id) ON DELETE CASCADE,

    -- Prevent self-referencing dependency loops
    CONSTRAINT chk_no_self_dependency CHECK (source_resource_id <> target_resource_id),

    -- Prevent duplicate dependency registrations
    CONSTRAINT unq_dependency_pair UNIQUE (source_resource_id, target_resource_id, dependency_type)
);

CREATE INDEX idx_dep_source_sub ON synthetic.dependencies(source_subscription);
CREATE INDEX idx_dep_target_sub ON synthetic.dependencies(target_subscription);
CREATE INDEX idx_dep_source_res ON synthetic.dependencies(source_resource_id);
CREATE INDEX idx_dep_target_res ON synthetic.dependencies(target_resource_id);

-- Governance violations (injected for governance rule testing)
CREATE TABLE synthetic.violations (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    resource_id         VARCHAR(512) NOT NULL REFERENCES synthetic.resources(id) ON DELETE CASCADE,
    violation_type      VARCHAR(150) NOT NULL,
    severity            synthetic.violation_severity NOT NULL,
    detail              JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Prevent non-object payload injection
    CONSTRAINT chk_viol_detail_is_object CHECK (jsonb_typeof(detail) = 'object'),

    -- Prevent duplicate active violations of the same type on a single resource
    CONSTRAINT unq_resource_violation UNIQUE (resource_id, violation_type)
);

CREATE INDEX idx_viol_resource ON synthetic.violations(resource_id);
CREATE INDEX idx_viol_type ON synthetic.violations(violation_type);
-- GIN index for fast filtering on JSON details
CREATE INDEX idx_viol_detail ON synthetic.violations USING GIN (detail);
