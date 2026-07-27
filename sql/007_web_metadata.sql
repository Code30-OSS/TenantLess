-- Web Console metadata (WAPI-03 / D-14) — the generation-profile IDENTITY the
-- /_sim/summary aggregate surfaces as `profile`. Phase-14 gap closure GAP-14-01:
-- summary.profile previously returned `profile_version` (e.g. `1.2`); Phase-15
-- clients expect a profile NAME (e.g. `enterprise-eu`). This file ONLY adds the
-- nullable `synthetic.tenant.profile_name` column; sql/001..006 are never edited.
-- Auto-applied on first Postgres start via docker-entrypoint-initdb.d, applied by
-- the testcontainers harness (mock-server/tests/common/mod.rs) +
-- writer.ensure_web_metadata_schema + the Rust startup preflight
-- (tenantless_server::ensure_web_metadata_schema).
--
-- Design notes:
--   * `profile_name` is NULLABLE with NO default, so the additive ALTER is a
--     metadata-only fast path in PostgreSQL 16 (no table rewrite, minimal ACCESS
--     EXCLUSIVE window) — deliberate given the known startup-ALTER-lock fragility
--     (project memory "server-startup-ALTER-lock deadlock"). The ALTER targets
--     synthetic.tenant (tiny, 1 row), not synthetic.resources.
--   * Nullable means the shared testcontainers `seed_fixture` tenant insert (which
--     does not set profile_name) still succeeds, and summary.profile returns null
--     when the column is absent/NULL — the empty/legacy-tenant contract is intact.
--   * Idempotent (ADD COLUMN IF NOT EXISTS) so re-applying the migration on an
--     already-migrated schema is a no-op rather than an error (mirrors
--     sql/006_drift.sql's `drift_deleted_at` additive column).
--   * STATIC DDL only — no runtime/user/profile input is spliced; no injection surface.

ALTER TABLE synthetic.tenant
    ADD COLUMN IF NOT EXISTS profile_name TEXT;
