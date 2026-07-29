-- Synthetic tenant schema: core tables for Azure tenant simulation
-- Auto-applied on first Postgres start via docker-entrypoint-initdb.d

CREATE SCHEMA IF NOT EXISTS synthetic;

-- Enum types for strict state validation
CREATE TYPE synthetic.subscription_state AS ENUM ('Enabled', 'Disabled', 'Warned');
CREATE TYPE synthetic.provisioning_state AS ENUM ('Succeeded', 'Failed', 'Canceled', 'Updating', 'Deleting');

-- Tenant metadata
CREATE TABLE synthetic.tenant (
    tenant_id       UUID PRIMARY KEY,
    display_name    VARCHAR(255) NOT NULL,
    generated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    profile_version VARCHAR(50) NOT NULL,
    scale_params    JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Prevent non-object payload injection (e.g., arrays or raw primitives)
    CONSTRAINT chk_tenant_params_is_object CHECK (jsonb_typeof(scale_params) = 'object')
);

-- Subscriptions
CREATE TABLE synthetic.subscriptions (
    subscription_id      UUID PRIMARY KEY,
    tenant_id            UUID NOT NULL REFERENCES synthetic.tenant(tenant_id) ON DELETE CASCADE,
    display_name         VARCHAR(255) NOT NULL,
    state                synthetic.subscription_state NOT NULL DEFAULT 'Enabled',
    archetype            VARCHAR(100) NOT NULL,
    tags                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    authorization_source VARCHAR(50) NOT NULL DEFAULT 'RoleBased',
    spending_limit       VARCHAR(50) NOT NULL DEFAULT 'Off',
    -- Prevent non-object payload injection
    CONSTRAINT chk_subs_tags_is_object CHECK (jsonb_typeof(tags) = 'object')
);
CREATE INDEX idx_subs_tenant ON synthetic.subscriptions(tenant_id);

-- Resource Groups
CREATE TABLE synthetic.resource_groups (
    id                  VARCHAR(512) PRIMARY KEY,
    subscription_id     UUID NOT NULL REFERENCES synthetic.subscriptions(subscription_id) ON DELETE CASCADE,
    name                VARCHAR(90) NOT NULL,
    location            VARCHAR(50) NOT NULL,
    template_type       VARCHAR(100) NOT NULL,
    tags                JSONB NOT NULL DEFAULT '{}'::jsonb,
    provisioning_state  synthetic.provisioning_state NOT NULL DEFAULT 'Succeeded',
    -- Ensure resource group names are unique within a subscription
    CONSTRAINT unq_sub_rg_name UNIQUE (subscription_id, name),
    -- Prevent non-object payload injection
    CONSTRAINT chk_rg_tags_is_object CHECK (jsonb_typeof(tags) = 'object')
);

-- Resources
CREATE TABLE synthetic.resources (
    id                  VARCHAR(512) PRIMARY KEY,
    subscription_id     UUID NOT NULL REFERENCES synthetic.subscriptions(subscription_id) ON DELETE CASCADE,
    resource_group_name VARCHAR(90) NOT NULL,
    name                VARCHAR(90) NOT NULL,
    type                VARCHAR(150) NOT NULL,
    location            VARCHAR(50) NOT NULL,
    tags                JSONB NOT NULL DEFAULT '{}'::jsonb,
    sku                 JSONB,
    kind                VARCHAR(100),
    properties          JSONB NOT NULL DEFAULT '{}'::jsonb,
    provisioning_state  synthetic.provisioning_state NOT NULL DEFAULT 'Succeeded',
    managed_by          VARCHAR(512),
    
    -- Prevent orphan resources by enforcing existence of the parent resource group
    CONSTRAINT fk_resources_rg FOREIGN KEY (subscription_id, resource_group_name) 
        REFERENCES synthetic.resource_groups(subscription_id, name) ON DELETE CASCADE,
    
    -- Ensure resource name uniqueness per type within a resource group
    CONSTRAINT unq_resource_identity UNIQUE (subscription_id, resource_group_name, type, name),
    
    -- Prevent non-object payload injection
    CONSTRAINT chk_res_tags_is_object CHECK (jsonb_typeof(tags) = 'object'),
    CONSTRAINT chk_res_props_is_object CHECK (jsonb_typeof(properties) = 'object')
);

-- Optimized Indexes
CREATE INDEX idx_res_rg ON synthetic.resources(subscription_id, resource_group_name);
CREATE INDEX idx_res_type_loc ON synthetic.resources(type, location);
-- GIN index required for fast JSON queries at scale (500k+ rows)
CREATE INDEX idx_res_tags ON synthetic.resources USING GIN (tags);
