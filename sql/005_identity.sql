-- Identity & RBAC tables (IAM-01/IAM-02) — the synthetic principal directory +
-- the role_assignments fact table the Microsoft.Authorization endpoints
-- (handlers/authorization.rs, Plan 10-03) read. Auto-applied on first Postgres
-- start via docker-entrypoint-initdb.d, and applied by the testcontainers harness
-- (mock-server/tests/common/mod.rs seed_identity_rows) + writer.ensure_identity_schema.
-- This file ONLY adds the identity tables; sql/001..004 are never edited.
--
-- Design notes:
--   * Principals are ARM-opaque GUIDs (IAM-01, D-01): `oid` is the only required
--     field. `display_name` is nullable (kept NULL to stay ARM-opaque — no
--     real-identifier-shaped strings, Pitfall 5); `app_id` is the ServicePrincipal
--     application id (NULL for User/Group).
--   * role_assignments references a real principal `oid` + a built-in
--     roleDefinition GUID (stored TENANT-scoped in `role_definition_id`:
--     `/providers/Microsoft.Authorization/roleDefinitions/{guid}`) + a real `scope`
--     (a subscription / RG / resource id that exists in the tenant). 0-dangling
--     (D-07) is enforced for `principal_oid` by the guarded FK below; the `scope`
--     0-dangling check is a UNION anti-join TEST (sub/RG/resource id sets), NOT a
--     single FK — `scope` is free-form text spanning three id namespaces.
--   * Column contracts match writer.copy_principals / copy_role_assignments exactly:
--       principals          (oid, principal_type, display_name, app_id)
--                           → (uuid, text, text, uuid)
--       role_assignments    (assignment_id, subscription_id, principal_oid,
--                            principal_type, role_definition_id, scope)
--                           → (uuid, uuid, uuid, text, text, text)
--   * Idempotent (CREATE ... IF NOT EXISTS + guarded FK DO block) so re-applying the
--     migration on an already-migrated schema is a no-op rather than an error.

CREATE TABLE IF NOT EXISTS synthetic.principals (
    oid             UUID PRIMARY KEY,
    principal_type  TEXT NOT NULL,          -- User | Group | ServicePrincipal
    display_name    TEXT,                   -- optional synthetic; NULL keeps it ARM-opaque
    app_id          UUID                    -- ServicePrincipal only (NULL for User/Group)
);

CREATE TABLE IF NOT EXISTS synthetic.role_assignments (
    assignment_id      UUID PRIMARY KEY,
    subscription_id    UUID NOT NULL,
    principal_oid      UUID NOT NULL,
    principal_type     TEXT NOT NULL,
    role_definition_id TEXT NOT NULL,       -- /providers/.../roleDefinitions/{guid}
    scope              TEXT NOT NULL         -- sub | RG | resource id (all real)
);

-- Backs the per-subscription roleAssignments read (handlers/authorization.rs). The
-- listing is KEYSET-paginated (v1.1.11): the query is
--   WHERE subscription_id = $1 AND assignment_id > $2 ORDER BY assignment_id LIMIT $3.
-- The single-key idx_ra_sub can serve only the subscription equality prefix, leaving
-- Postgres to sort/scan the subscription's whole assignment set on every page; the
-- composite idx_ra_sub_assignment (subscription_id, assignment_id) turns the seek +
-- ORDER BY into an index range scan (the assignment_id keyset rides the second key).
-- idx_ra_sub is retained (harmless prefix subset) rather than dropped, to keep this an
-- additive migration. Both are CREATE INDEX IF NOT EXISTS, so re-applying sql/005
-- (writer.ensure_identity_schema, run UNCONDITIONALLY by generate/init-db) UPGRADES an
-- existing database automatically — a DB provisioned before v1.1.11 gains the composite
-- index on its next run, with no manual step and no re-provision.
CREATE INDEX IF NOT EXISTS idx_ra_sub
    ON synthetic.role_assignments (subscription_id);

CREATE INDEX IF NOT EXISTS idx_ra_sub_assignment
    ON synthetic.role_assignments (subscription_id, assignment_id);

-- Safe FK: every assignment references a real principal (IAM-02/D-07, the
-- 0-dangling gate / XSUB-06 analogue). Idempotent via a guarded DO block so a
-- re-apply on an already-migrated schema is a no-op rather than an error
-- (mirrors sql/004_cost.sql). NOTE: no FK on `scope` — it is free-form text
-- spanning sub/RG/resource id namespaces; its 0-dangling check is a UNION
-- anti-join test, not a single FK.
DO $$
BEGIN
    ALTER TABLE synthetic.role_assignments
        ADD CONSTRAINT fk_ra_principal
        FOREIGN KEY (principal_oid)
        REFERENCES synthetic.principals (oid);
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;
