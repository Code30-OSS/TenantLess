-- Synthetic tenant schema: core tables for Azure tenant simulation
-- Auto-applied on first Postgres start via docker-entrypoint-initdb.d

CREATE SCHEMA IF NOT EXISTS synthetic;

-- Tenant metadata
CREATE TABLE synthetic.tenant (
    tenant_id       UUID PRIMARY KEY,
    display_name    TEXT NOT NULL,
    generated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    profile_version TEXT NOT NULL,
    scale_params    JSONB NOT NULL
);

-- Subscriptions
CREATE TABLE synthetic.subscriptions (
    subscription_id     UUID PRIMARY KEY,
    tenant_id           UUID NOT NULL REFERENCES synthetic.tenant(tenant_id),
    display_name        TEXT NOT NULL,
    state               TEXT NOT NULL DEFAULT 'Enabled',
    archetype           TEXT NOT NULL,
    tags                JSONB NOT NULL DEFAULT '{}',
    authorization_source TEXT NOT NULL DEFAULT 'RoleBased',
    spending_limit      TEXT NOT NULL DEFAULT 'Off'
);
CREATE INDEX idx_subs_tenant ON synthetic.subscriptions(tenant_id);

-- Resource Groups
CREATE TABLE synthetic.resource_groups (
    id                  TEXT PRIMARY KEY,
    subscription_id     UUID NOT NULL REFERENCES synthetic.subscriptions(subscription_id),
    name                TEXT NOT NULL,
    location            TEXT NOT NULL,
    template_type       TEXT NOT NULL,
    tags                JSONB NOT NULL DEFAULT '{}',
    provisioning_state  TEXT NOT NULL DEFAULT 'Succeeded'
);
CREATE INDEX idx_rg_sub ON synthetic.resource_groups(subscription_id);

-- Resources (the big table: up to 500K rows)
CREATE TABLE synthetic.resources (
    id                  TEXT PRIMARY KEY,
    subscription_id     UUID NOT NULL,
    resource_group_name TEXT NOT NULL,
    name                TEXT NOT NULL,
    type                TEXT NOT NULL,
    location            TEXT NOT NULL,
    tags                JSONB NOT NULL DEFAULT '{}',
    sku                 JSONB,
    kind                TEXT,
    properties          JSONB NOT NULL DEFAULT '{}',
    provisioning_state  TEXT NOT NULL DEFAULT 'Succeeded',
    managed_by          TEXT
);
CREATE INDEX idx_res_sub ON synthetic.resources(subscription_id);
CREATE INDEX idx_res_rg ON synthetic.resources(subscription_id, resource_group_name);
CREATE INDEX idx_res_type ON synthetic.resources(type);
CREATE INDEX idx_res_location ON synthetic.resources(location);
