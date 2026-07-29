-- Web Console metadata (WAPI-03 / D-14) — Additive migration for profile_name.
-- Auto-applied on first Postgres start via docker-entrypoint-initdb.d.

-- Add profile_name as VARCHAR(100) (Nullable, metadata-only fast path ALTER in PG 16)
ALTER TABLE synthetic.tenant
    ADD COLUMN IF NOT EXISTS profile_name VARCHAR(100);

-- Guarded addition of a CHECK constraint to enforce strict naming format when set
DO $$
BEGIN
    ALTER TABLE synthetic.tenant
        ADD CONSTRAINT chk_tenant_profile_name_format
        CHECK (
            profile_name IS NULL OR 
            (length(trim(profile_name)) > 0 AND profile_name ~ '^[a-zA-Z0-9._-]+$')
        );
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;
