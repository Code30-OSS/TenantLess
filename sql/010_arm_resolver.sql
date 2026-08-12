-- 010_arm_resolver.sql: the liveness authority + unified resolver substrate.
--
-- This migration lands the SQL layer every ARM reader and the drift read-modify-write
-- will build on. It is PURELY ADDITIVE and touches NOTHING on the populated
-- synthetic.resources table (no ADD COLUMN, no index there) — the one hard
-- deadlock-safety invariant of this migration.
--
-- THREE concerns, all deadlock-safe (none locks the big synthetic.resources heap):
--
--   (a) storage_mode provenance marker on synthetic.drift_batches (a small table).
--       Legacy in-place batches read as 'synthetic'; the new overlay-writing drift path
--       stamps 'overlay'. The DEFAULT 'synthetic' is the FAIL-SAFE direction: an
--       unstamped (legacy) batch reads as legacy and TRIPS the boot fail-closed guard,
--       rather than being silently trusted as overlay.
--
--   (b) synthetic.arm_resolved_resources — the liveness authority. A per-kind
--       VIEW with an EXPLICIT enumerated column list (NEVER SELECT *) that resolves
--       `baseline (not shadowed / not tombstoned) UNION ALL overlay present`. A single
--       NOT EXISTS anti-join covers BOTH replace AND tombstone: a baseline row is live
--       iff no arm_overlay row shadows its id (a present=true overlay wins wholesale; a
--       present=false tombstone shadows the baseline row and contributes nothing, being
--       excluded from the present-only overlay branch).
--
--   (c) synthetic.arm_resolved_resource_groups — the RG resolved view. Baseline-only in
--       this migration (no RG overlay writer until a later release) but it EXPOSES the real
--       target_kind='resource_group' overlay branch so the later RG activation inherits a
--       proven shape; that branch is simply never populated here.
--
--   (d) idx_arm_overlay_kind_id on synthetic.arm_overlay (target_kind, id_lower). The
--       arm_overlay table is empty/tiny in this migration, so its ACCESS EXCLUSIVE index
--       build is harmless (no startup-deadlock hazard on the small table).
--
-- SCOPE DERIVATION (overlay-only rows) FAILS CLOSED. An overlay present row with no
-- baseline counterpart derives subscription_id / resource_group_name from its canonical
-- id (/subscriptions/{sub}/resourceGroups/{rg}/...). Derivation is case-normalized
-- consistently with id_lower and a malformed / out-of-scope id is EXCLUDED entirely by a
-- WHERE conjunct (a strict subscription-UUID + segment-literal check) — a row is NEVER
-- served with a NULL scope. The ::uuid cast is additionally CASE-guarded so it can never
-- raise on a non-UUID segment regardless of the query plan. In this migration drift only
-- shadows EXISTING baseline ids, so this overlay-only branch is TEST-exercised, not
-- drift-exercised, but the derivation ships here so it is proven for the write plane.
--
-- INTERNAL columns. provisioning_state / managed_by are carried on the resource view for
-- resolver consumers, but they are NOT ARM-served (the handlers project a fixed 8-column
-- list). An overlay row carries provisioning_state='Succeeded' and managed_by=NULL.
--
-- PORTABLE + HONEST across BOTH execution contexts. This file is ONLY pure, idempotent
-- DDL — it contains NO transaction-scoped statement. The bounded lock_timeout and the
-- serializing advisory lock that make concurrent first-boot applies safe live in the two
-- provisioning paths that GUARANTEE an explicit transaction — ensure_arm_resolver_schema
-- (mock-server/src/lib.rs, via apply_schema_batch) and writer.ensure_arm_resolver_schema
-- — because a transaction-scoped statement is a silent no-op (and emits a WARNING) under
-- Docker's docker-entrypoint-initdb.d autocommit path.
--
-- PG11-safe ONLY: the testcontainers fixture is PostgreSQL 11, so every projection uses
-- PG9.4+ operators (`->`, `->>`, `split_part`, `lower`, POSIX `~*`) and NEVER the PG12+
-- SQL/JSON-path operators (`@?`, `jsonb_path_*`).
--
-- Idempotency: CREATE OR REPLACE VIEW for both views, CREATE INDEX IF NOT EXISTS for the
-- overlay index, and a guarded `DO $$ ... EXCEPTION WHEN duplicate_column THEN NULL ... $$`
-- block for the storage_mode column (the sql/006 idiom) — so a re-apply is a clean no-op.

-- (a) storage_mode provenance marker on synthetic.drift_batches.
-- Guarded DO block (NOT `ADD COLUMN IF NOT EXISTS`) mirrors the sql/006 idiom: on a
-- re-apply the plain ADD COLUMN raises duplicate_column, which rolls the single statement
-- back — clean and idempotent on both PG11 and PG16. The constant DEFAULT back-fills
-- existing rows to 'synthetic' as a metadata-only change (no table rewrite).
DO $$
BEGIN
    ALTER TABLE synthetic.drift_batches
        ADD COLUMN storage_mode TEXT NOT NULL DEFAULT 'synthetic';
EXCEPTION
    WHEN duplicate_column THEN NULL;
END $$;

-- Guarded domain CHECK: storage_mode is one of the two provenance values. Added via a
-- guarded DO block (ADD CONSTRAINT has no IF NOT EXISTS — the project idiom) so a re-apply
-- is a no-op.
DO $$
BEGIN
    ALTER TABLE synthetic.drift_batches
        ADD CONSTRAINT ck_drift_batches_storage_mode
        CHECK (storage_mode IN ('synthetic', 'overlay'));
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

-- (b) synthetic.arm_resolved_resources — the liveness authority.
-- EXPLICIT enumerated columns (never SELECT *). The baseline branch is a pure passthrough
-- of the synthetic.resources column set (so an empty overlay is byte-identical to the
-- pre-v3 read by construction), excluded only where an overlay row shadows the id (the
-- single anti-join covering both replace and tombstone). The overlay branch projects the
-- complete snapshot body back into the same typed columns.
CREATE OR REPLACE VIEW synthetic.arm_resolved_resources AS
    -- Baseline branch: a synthetic.resources row is LIVE iff no arm_overlay resource row
    -- shadows its (case-folded) id — present=true overlay replaces it, present=false
    -- overlay (the tombstone) hides it; both are the SAME anti-join.
    SELECT
        b.id,
        b.name,
        b.type,
        b.location,
        b.tags,
        b.sku,
        b.kind,
        b.properties,
        b.subscription_id,
        b.resource_group_name,
        b.provisioning_state,
        b.managed_by
    FROM synthetic.resources b  -- SYNRES-ALLOW[schema/provisioning]: resolver view definition — the ONE definitional baseline read the resolver is built from
    WHERE NOT EXISTS (
        SELECT 1
        FROM synthetic.arm_overlay o
        WHERE o.id_lower = lower(b.id)
          AND o.target_kind = 'resource'
    )
    UNION ALL
    -- Overlay branch: a present=true resource overlay row wins wholesale. body -> ...
    -- yields jsonb (tags, sku, properties); body ->> ... yields text (id, name, type,
    -- location, kind). An ABSENT optional key (sku / kind) becomes SQL NULL (matching the
    -- baseline Option::None decode) — never a stored JSON null. subscription_id /
    -- resource_group_name are DERIVED from the canonical id and FAIL CLOSED (see WHERE).
    SELECT
        o.body ->> 'id'                                   AS id,
        o.body ->> 'name'                                 AS name,
        o.body ->> 'type'                                 AS type,
        o.body ->> 'location'                             AS location,
        o.body -> 'tags'                                  AS tags,
        o.body -> 'sku'                                   AS sku,
        o.body ->> 'kind'                                 AS kind,
        COALESCE(o.body -> 'properties', '{}'::jsonb)     AS properties,
        -- CASE-guarded cast: only evaluated when the subscription segment is a valid UUID,
        -- so the ::uuid cast can never raise regardless of plan; the WHERE below already
        -- excludes malformed ids, so this never emits a NULL scope into the result.
        (CASE
            WHEN split_part(o.id, '/', 3)
                 ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            THEN lower(split_part(o.id, '/', 3))::uuid
         END)                                             AS subscription_id,
        split_part(o.id, '/', 5)                          AS resource_group_name,
        'Succeeded'::text                                 AS provisioning_state,  -- INTERNAL
        NULL::text                                        AS managed_by           -- INTERNAL
    FROM synthetic.arm_overlay o
    WHERE o.target_kind = 'resource'
      AND o.present = true
      -- Fail-closed scope derivation: the canonical id MUST carry the exact
      -- /subscriptions/{uuid}/resourceGroups/{rg}/... shape or the row is EXCLUDED (never
      -- served with a NULL / out-of-scope subscription).
      AND lower(split_part(o.id, '/', 2)) = 'subscriptions'
      AND lower(split_part(o.id, '/', 4)) = 'resourcegroups'
      AND split_part(o.id, '/', 3)
          ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
      AND length(split_part(o.id, '/', 5)) > 0;

-- (c) synthetic.arm_resolved_resource_groups — the RG resolved view.
-- Baseline-only content in this migration (no RG overlay writer yet) but it exposes the REAL
-- target_kind='resource_group' overlay branch so a later RG activation inherits a proven
-- shape. Enumerated columns match ResourceGroupRow (id, name, location, tags,
-- provisioning_state) plus the INTERNAL subscription_id.
CREATE OR REPLACE VIEW synthetic.arm_resolved_resource_groups AS
    SELECT
        g.id,
        g.name,
        g.location,
        g.tags,
        g.provisioning_state,
        g.subscription_id
    FROM synthetic.resource_groups g
    WHERE NOT EXISTS (
        SELECT 1
        FROM synthetic.arm_overlay o
        WHERE o.id_lower = lower(g.id)
          AND o.target_kind = 'resource_group'
    )
    UNION ALL
    -- Overlay branch (never populated in this phase). An RG's served provisioningState
    -- lives at body.properties.provisioningState (ck_arm_overlay_rg_provisioning_state).
    -- Same fail-closed scope derivation as the resource view (RG id shape is
    -- /subscriptions/{uuid}/resourceGroups/{rg}).
    SELECT
        o.body ->> 'id'                                   AS id,
        o.body ->> 'name'                                 AS name,
        o.body ->> 'location'                             AS location,
        o.body -> 'tags'                                  AS tags,
        o.body -> 'properties' ->> 'provisioningState'    AS provisioning_state,
        (CASE
            WHEN split_part(o.id, '/', 3)
                 ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            THEN lower(split_part(o.id, '/', 3))::uuid
         END)                                             AS subscription_id  -- INTERNAL
    FROM synthetic.arm_overlay o
    WHERE o.target_kind = 'resource_group'
      AND o.present = true
      AND lower(split_part(o.id, '/', 2)) = 'subscriptions'
      AND lower(split_part(o.id, '/', 4)) = 'resourcegroups'
      AND split_part(o.id, '/', 3)
          ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
      AND length(split_part(o.id, '/', 5)) > 0;

-- (d) The overlay resolution index (target_kind, id_lower). Backs both views'
-- shadow anti-join predicate `o.id_lower = lower(b.id) AND o.target_kind = '<kind>'`. The
-- arm_overlay table is empty/tiny in this migration so the ACCESS EXCLUSIVE build is harmless.
CREATE INDEX IF NOT EXISTS idx_arm_overlay_kind_id
    ON synthetic.arm_overlay (target_kind, id_lower);
