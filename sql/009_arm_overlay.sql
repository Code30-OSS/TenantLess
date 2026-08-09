-- 009_arm_overlay.sql: the ARM overlay/tombstone/revision substrate.
--
-- ONE unified table is the physical form of THREE logical concepts:
--   * overlay snapshots  — a `present=true` row stores the COMPLETE copy-on-write resolved
--                          body and wins wholesale over baseline for that id;
--   * tombstones         — `present=false` IS the tombstone (body NULL);
--   * revisions          — a standalone unowned BIGINT sequence + a BEFORE trigger assign a
--                          fresh, ever-advancing revision on EVERY write.
--
-- Boundary: no reader consults this table, no drift path writes it, no ETag
-- header is emitted. Behaviour only ever writes/resolves `resource` rows; the
-- `resource_group` row shape is DEFINED and CHECK-tested here but never activated until
-- a later phase.
--
-- Idempotency: the whole file is safe to run on every boot — `CREATE ... IF NOT EXISTS`
-- for the table/sequence, `CREATE OR REPLACE FUNCTION` + a guarded (existence-checked)
-- `CREATE TRIGGER` for the trigger, and guarded `DO $$ ... EXCEPTION WHEN duplicate_object
-- THEN NULL ... $$` blocks for every NAMED CHECK constraint (the sql/003 / sql/006 idiom).
--
-- PORTABLE + HONEST across BOTH execution contexts. This file is ONLY pure,
-- idempotent DDL — it contains NO transaction-scoped statement. The bounded `lock_timeout`
-- and the serializing advisory lock that make concurrent first-boot applies safe live in the
-- two provisioning paths that GUARANTEE an explicit transaction — `ensure_arm_overlay_schema`
-- (mock-server/src/lib.rs, via `apply_schema_batch`, a tx that also sets `statement_timeout=0`)
-- and `writer.ensure_arm_overlay_schema` — because a transaction-scoped statement is a silent
-- no-op (and emits a WARNING) under Docker's `docker-entrypoint-initdb.d` autocommit path.
--
-- PG11-safe ONLY: the testcontainers fixture is PostgreSQL 11 (sql/006 documents this), so
-- every body-shape CHECK uses PG9.4+ operators (`->`, `->>`, `<>`, `jsonb_typeof`) and
-- NEVER the PG12+ SQL/JSON-path operators (`@?`, `jsonb_path_*`).

-- Concurrency + lock-bounding NOTE: the transaction-scoped `SET LOCAL
-- lock_timeout` + `pg_advisory_xact_lock(hashtext('synthetic.arm_overlay:009'))` that
-- serialize concurrent first-boot applies and bound any ACCESS-EXCLUSIVE wait are issued by
-- the provisioning paths (which guarantee an explicit transaction) BEFORE this DDL — never in
-- this file, so it stays honest under Docker initdb autocommit. `CREATE ... IF NOT EXISTS` +
-- the guarded `DO` blocks make a redundant apply a no-op even absent that lock.

-- (a) The unified overlay table. `id_lower` is the inline PRIMARY KEY — this
-- auto-provides the unique B-tree lookup index + NOT NULL, matching the sql/003 lower(id)
-- functional-index normalization the resolver will join on. No CONCURRENTLY.
CREATE TABLE IF NOT EXISTS synthetic.arm_overlay (
    id_lower     TEXT PRIMARY KEY,   -- = lower(id); inline PK => unique btree + NOT NULL
    id           TEXT    NOT NULL,   -- canonical id, echoed verbatim in responses
    target_kind  TEXT    NOT NULL,   -- {resource, resource_group} discriminator
    source       TEXT    NOT NULL,   -- write authority {user, drift}
    present      BOOLEAN NOT NULL,   -- present=false IS the tombstone
    body         JSONB,              -- complete snapshot iff present; NULL iff tombstone
    revision     BIGINT  NOT NULL    -- assigned by the BEFORE trigger; CHECK > 0
);

-- (b) Standalone UNOWNED BIGINT NO CYCLE revision sequence. Unowned so
-- `TRUNCATE ... RESTART IDENTITY` (reset / restore) can NOT rewind it —
-- the next nextval() is strictly greater. NOT `GENERATED ... AS IDENTITY` (which
-- fires only on INSERT and hits the PG11 double-owned-sequence gotcha sql/006 documents).
CREATE SEQUENCE IF NOT EXISTS synthetic.arm_overlay_revision_seq AS BIGINT NO CYCLE;

-- (c) Revision trigger. A BEFORE INSERT OR UPDATE trigger unconditionally overwrites
-- any caller-supplied `revision` with a fresh nextval() — so EVERY write (INSERT, UPDATE,
-- delete-marker/resurrect via ON CONFLICT DO UPDATE, and future drift) advances the
-- revision, and a caller can never forge or freeze it. BEFORE UPDATE is what makes
-- ON CONFLICT DO UPDATE advance (IDENTITY could not).
CREATE OR REPLACE FUNCTION synthetic.arm_overlay_set_revision() RETURNS trigger AS $$
BEGIN
    NEW.revision := nextval('synthetic.arm_overlay_revision_seq');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Conditional CREATE: an unconditional DROP/CREATE takes an ACCESS EXCLUSIVE lock
-- on the table on EVERY boot — once the resolver populates and serves writes, a concurrent boot's
-- DROP/CREATE would contend with in-flight writes (the server-startup ALTER-lock hazard).
-- Create the trigger ONLY if absent; a definition change must ship as a future numbered
-- migration (not an every-boot re-create). `CREATE OR REPLACE FUNCTION` above needs no table
-- lock, so the trigger's behaviour can still evolve via the function body if ever required.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgrelid = 'synthetic.arm_overlay'::regclass
          AND tgname = 'trg_arm_overlay_revision'
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_arm_overlay_revision
            BEFORE INSERT OR UPDATE ON synthetic.arm_overlay
            FOR EACH ROW EXECUTE FUNCTION synthetic.arm_overlay_set_revision();
    END IF;
END $$;

-- (d) Twelve NAMED row-model CHECK constraints. Each is added via a guarded DO
-- block so a re-apply is a no-op (ADD CONSTRAINT has no IF NOT EXISTS — the project idiom).
-- NAMED so the structural inventory can inspect each DEFINITION and reject a
-- same-named `CHECK (true)` stub. Each constraint rejects its own violation independently.

-- 1. id_lower is the case-folded id.
DO $$
BEGIN
    ALTER TABLE synthetic.arm_overlay
        ADD CONSTRAINT ck_arm_overlay_id_lower CHECK (id_lower = lower(id));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- 2. target_kind discriminator domain.
DO $$
BEGIN
    ALTER TABLE synthetic.arm_overlay
        ADD CONSTRAINT ck_arm_overlay_kind
        CHECK (target_kind IN ('resource', 'resource_group'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- 3. source authority domain.
DO $$
BEGIN
    ALTER TABLE synthetic.arm_overlay
        ADD CONSTRAINT ck_arm_overlay_source
        CHECK (source IN ('user', 'drift'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- 4. revision is strictly positive — belt-and-suspenders behind the trigger.
DO $$
BEGIN
    ALTER TABLE synthetic.arm_overlay
        ADD CONSTRAINT ck_arm_overlay_revision_pos CHECK (revision > 0);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- 5. present <=> body presence: a present row has a body; a tombstone has none.
DO $$
BEGIN
    ALTER TABLE synthetic.arm_overlay
        ADD CONSTRAINT ck_arm_overlay_present_body
        CHECK ((present AND body IS NOT NULL) OR (NOT present AND body IS NULL));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- 6. a `{}` body is NOT a complete snapshot.
DO $$
BEGIN
    ALTER TABLE synthetic.arm_overlay
        ADD CONSTRAINT ck_arm_overlay_body_nonempty
        CHECK (body IS NULL OR body <> '{}'::jsonb);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- 7. canonical id <-> body id agreement.
DO $$
BEGIN
    ALTER TABLE synthetic.arm_overlay
        ADD CONSTRAINT ck_arm_overlay_body_id_agree
        CHECK (body IS NULL OR body ->> 'id' = id);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- 8. required ARM-envelope field types on a present row: id/name/type/location are
-- strings, properties is an object. PG11-safe jsonb_typeof only.
DO $$
BEGIN
    ALTER TABLE synthetic.arm_overlay
        ADD CONSTRAINT ck_arm_overlay_envelope
        CHECK (
            body IS NULL OR (
                -- coalesce so a MISSING key (jsonb_typeof -> SQL NULL) yields FALSE and is
                -- REJECTED; a bare `... = 'string'` would be NULL and a CHECK passes on NULL.
                coalesce(jsonb_typeof(body -> 'id') = 'string', false)
                AND coalesce(jsonb_typeof(body -> 'name') = 'string', false)
                AND coalesce(jsonb_typeof(body -> 'type') = 'string', false)
                AND coalesce(jsonb_typeof(body -> 'location') = 'string', false)
                AND coalesce(jsonb_typeof(body -> 'properties') = 'object', false)
            )
        );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- 9. distinct resource vs resource_group body shape, tested bidirectionally now so
-- RG activation inherits a proven CHECK: a resource_group body's `type` MUST be the
-- ARM RG constant, a resource body's `type` must NOT be.
DO $$
BEGIN
    ALTER TABLE synthetic.arm_overlay
        ADD CONSTRAINT ck_arm_overlay_kind_shape
        CHECK (
            body IS NULL
            OR (target_kind = 'resource_group'
                AND body ->> 'type' = 'Microsoft.Resources/resourceGroups')
            OR (target_kind = 'resource'
                AND body ->> 'type' <> 'Microsoft.Resources/resourceGroups')
        );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- 10. required served `tags` object on a present body. Both a resource
-- and a resource_group ALWAYS serve `tags` (an object, possibly `{}`), so a present snapshot
-- that omits it — or carries a non-object — is structurally incomplete and is rejected.
DO $$
BEGIN
    ALTER TABLE synthetic.arm_overlay
        ADD CONSTRAINT ck_arm_overlay_tags
        CHECK (body IS NULL OR coalesce(jsonb_typeof(body -> 'tags') = 'object', false));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- 11. valid optional-field TYPES when present: `sku` (served only when
-- set) must be an object; `kind` (served only when set) must be a string. Absent keys are
-- allowed (they are `skip_serializing_if` on the served DTO). `jsonb_exists` is the function
-- form of `?` (PG9.4+), used to distinguish an ABSENT key from a present-but-typed value.
DO $$
BEGIN
    ALTER TABLE synthetic.arm_overlay
        ADD CONSTRAINT ck_arm_overlay_optional_types
        CHECK (
            body IS NULL OR (
                (NOT jsonb_exists(body, 'sku') OR jsonb_typeof(body -> 'sku') = 'object')
                AND (NOT jsonb_exists(body, 'kind') OR jsonb_typeof(body -> 'kind') = 'string')
            )
        );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- 12. resource_group provisioning-state shape: an RG's served
-- representation is `properties = { "provisioningState": <string> }`, so a resource_group
-- snapshot MUST carry a string `properties.provisioningState`. Defined now (the RG body shape
-- is locked) even though RG rows are not ACTIVATED until a later phase.
DO $$
BEGIN
    ALTER TABLE synthetic.arm_overlay
        ADD CONSTRAINT ck_arm_overlay_rg_provisioning_state
        CHECK (
            body IS NULL
            OR target_kind <> 'resource_group'
            OR coalesce(
                   jsonb_typeof(body -> 'properties' -> 'provisioningState') = 'string',
                   false)
        );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
