-- Identity & RBAC tables (IAM-01/IAM-02) — the synthetic principal directory +
-- the role_assignments fact table read by Microsoft.Authorization endpoints.
-- Auto-applied on first Postgres start via docker-entrypoint-initdb.d

-- Enum type for strict principal type validation
CREATE TYPE synthetic.principal_type_enum AS ENUM ('User', 'Group', 'ServicePrincipal');

CREATE TABLE IF NOT EXISTS synthetic.principals (
    oid             UUID PRIMARY KEY,
    principal_type  synthetic.principal_type_enum NOT NULL,
    display_name    VARCHAR(255),
    app_id          UUID,

    -- Enforce conditional consistency: app_id MUST be set for ServicePrincipals and MUST be NULL otherwise
    CONSTRAINT chk_app_id_service_principal CHECK (
        (principal_type = 'ServicePrincipal' AND app_id IS NOT NULL) OR
        (principal_type <> 'ServicePrincipal' AND app_id IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS synthetic.role_assignments (
    assignment_id      UUID PRIMARY KEY,
    subscription_id    UUID NOT NULL,
    principal_oid      UUID NOT NULL,
    principal_type     synthetic.principal_type_enum NOT NULL,
    role_definition_id VARCHAR(512) NOT NULL,
    scope              VARCHAR(512) NOT NULL,

    -- Prevent duplicate role assignments for the same principal, role, and scope
    CONSTRAINT unq_role_assignment UNIQUE (subscription_id, principal_oid, role_definition_id, scope)
);

-- Backs per-subscription role assignments listing
CREATE INDEX IF NOT EXISTS idx_ra_sub
    ON synthetic.role_assignments (subscription_id);

-- Backs direct permission checks (e.g., "Does this principal have access to this specific scope?")
CREATE INDEX IF NOT EXISTS idx_ra_principal_scope
    ON synthetic.role_assignments (principal_oid, scope);

-- Safe FK constraints with explicit CASCADE behavior
DO $$
BEGIN
    ALTER TABLE synthetic.role_assignments
        ADD CONSTRAINT fk_ra_principal
        FOREIGN KEY (principal_oid)
        REFERENCES synthetic.principals (oid)
        ON DELETE CASCADE,

        ADD CONSTRAINT fk_ra_subscription
        FOREIGN KEY (subscription_id)
        REFERENCES synthetic.subscriptions (subscription_id)
        ON DELETE CASCADE;
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;
