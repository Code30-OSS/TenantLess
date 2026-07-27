-- Cross-subscription dependencies and governance violations
-- Auto-applied on first Postgres start via docker-entrypoint-initdb.d

-- Cross-subscription dependencies
CREATE TABLE synthetic.dependencies (
    id                  SERIAL PRIMARY KEY,
    dependency_type     TEXT NOT NULL,
    source_resource_id  TEXT NOT NULL,
    target_resource_id  TEXT NOT NULL,
    source_subscription UUID NOT NULL,
    target_subscription UUID NOT NULL
);
CREATE INDEX idx_dep_source_sub ON synthetic.dependencies(source_subscription);
CREATE INDEX idx_dep_target_sub ON synthetic.dependencies(target_subscription);

-- Governance violations (injected for governance rule testing)
CREATE TABLE synthetic.violations (
    id                  SERIAL PRIMARY KEY,
    resource_id         TEXT NOT NULL,
    violation_type      TEXT NOT NULL,
    severity            TEXT NOT NULL,
    detail              JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_viol_resource ON synthetic.violations(resource_id);
CREATE INDEX idx_viol_type ON synthetic.violations(violation_type);
