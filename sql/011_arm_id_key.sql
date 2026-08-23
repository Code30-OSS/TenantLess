-- 011_arm_id_key.sql: the canonical ARM-ID identity fold functions (INV-01).
--
-- Defines the two-layer fold primitive (D-01/D-02/D-28) as schema-qualified,
-- IMMUTABLE STRICT SQL functions so they are usable in expression indexes and
-- match the query expressions the seam cutover (00a-ii) will bind:
--   * synthetic.ascii_fold(text)  — the low-level primitive: ASCII A-Z -> a-z and
--                                    NOTHING else (non-ASCII, slashes, percent-
--                                    encoding all pass through unchanged);
--   * synthetic.arm_id_key(text)  — arm_id_key(id) = ascii_fold(id), the whole-ID
--                                    identity wrapper.
--
-- The fold uses translate($1, 'A..Z', 'a..z') — DELIBERATELY NOT lower(): locale
-- lower() diverges on Turkish dotted-I / sharp-s / across Unicode versions, exactly
-- the cross-engine drift INV-01 eliminates. The 52-character literal is static so
-- 00a-ii's expression index / predicates can match one canonical symbol. These are
-- byte-identical to Rust ArmId (to_ascii_lowercase) + Python identity.py
-- (str.translate), pinned by tests/kat/arm_id_kat.json.
--
-- ADDITIVE + behaviour-neutral (D-22a): this file defines ONLY the two functions.
-- It changes NO CHECK constraint, creates NO index, edits NO view, and cuts over NO
-- predicate. Nothing in the running system consumes these functions yet — the five
-- stateful seams, the arm_overlay CHECK, the sql/010 joins, and the RG-name
-- predicates are migrated atomically in 00a-ii.
--
-- Idempotency: the whole file is safe to run on every boot / init-db — CREATE OR
-- REPLACE FUNCTION is a no-op-equivalent re-definition on an already-migrated
-- schema. IMMUTABLE STRICT: a NULL argument yields NULL without invoking the body,
-- and the result depends only on the argument (so a functional index is valid).
-- Requires the `synthetic` schema to already exist (the caller confirms a tenant
-- first). The statement text is a STATIC project file, never user/profile input —
-- no injection surface.
--
-- PG11-safe: translate() + plain SQL functions are PG9.4+ (the testcontainers
-- fixture is PostgreSQL 11).

CREATE OR REPLACE FUNCTION synthetic.ascii_fold(t text)
    RETURNS text
    LANGUAGE sql
    IMMUTABLE STRICT
    AS $$
    SELECT translate($1,
        'ABCDEFGHIJKLMNOPQRSTUVWXYZ',
        'abcdefghijklmnopqrstuvwxyz')
$$;

CREATE OR REPLACE FUNCTION synthetic.arm_id_key(id text)
    RETURNS text
    LANGUAGE sql
    IMMUTABLE STRICT
    AS $$
    SELECT synthetic.ascii_fold($1)
$$;
