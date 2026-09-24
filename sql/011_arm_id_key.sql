-- 011_arm_id_key.sql: the canonical ARM-ID identity fold functions (INV-01).
--
-- Defines the two-layer fold primitive (D-01/D-02/D-28) as schema-qualified,
-- IMMUTABLE STRICT PARALLEL SAFE SQL functions so they are usable in expression
-- indexes, keep parallel plans available, and match the query expressions the
-- identity seams bind:
--   * synthetic.ascii_fold(text)  — the low-level primitive: ASCII A-Z -> a-z and
--                                    NOTHING else (non-ASCII, slashes, percent-
--                                    encoding all pass through unchanged);
--   * synthetic.arm_id_key(text)  — arm_id_key(id) = ascii_fold(id), the whole-ID
--                                    identity wrapper.
--
-- The fold is lower($1 COLLATE "C") — NEVER a bare or locale lower(). Locale-aware
-- lowercasing diverges on Turkish dotted-I / sharp-s / across Unicode versions, exactly
-- the cross-engine drift INV-01 eliminates. Under the C collation PostgreSQL's lower()
-- maps ASCII A-Z to a-z and leaves every other byte untouched, and because the
-- collation is written explicitly the result is independent of the database locale and
-- of any caller-side collation. It is the same ASCII-only map translate() over the 26
-- letters computes, at a fraction of the per-row cost. PARALLEL SAFE (the body calls
-- only lower()) so the resolver views' per-row shadow anti-join keeps parallel plans.
-- These are byte-identical to Rust ArmId (to_ascii_lowercase) + Python identity.py
-- (str.translate), pinned by tests/kat/arm_id_kat.json; tests/test_identity_gate.py
-- sanctions this exact body line and no other lower() in the identity paths.
--
-- Redefinition safety: CREATE OR REPLACE swaps the body in place on an already-migrated
-- volume. The outputs are identical for every input, so the existing expression indexes
-- (idx_res_arm_id_key / idx_res_rg_ascii_fold) and the arm_overlay identity CHECK stay
-- valid without a rebuild.
--
-- This file defines ONLY the two functions (D-22a). It changes NO CHECK constraint,
-- creates NO index, edits NO view, and cuts over NO predicate; the consumers (the
-- stateful seams, the arm_overlay CHECK, the sql/010 joins, the RG-name predicates and
-- the fold expression indexes) are provisioned by sql/012, sql/010 and the concurrent
-- index build.
--
-- Idempotency: the whole file is safe to run on every boot / init-db — CREATE OR
-- REPLACE FUNCTION is a no-op-equivalent re-definition on an already-migrated
-- schema. It still rewrites the pg_proc row, so two sessions redefining at once
-- would fail the second with "tuple concurrently updated": every provisioning path
-- (Rust boot, Python writer twins) takes the shared transaction-scoped advisory lock
-- hashtext('synthetic.arm_id_fold') before applying this file or the sql/010 prelude.
-- The lock lives in those callers, not here, so the file stays honest under Docker
-- initdb autocommit. IMMUTABLE STRICT: a NULL argument yields NULL without invoking the body,
-- and the result depends only on the argument (so a functional index is valid).
-- Requires the `synthetic` schema to already exist (the caller confirms a tenant
-- first). The statement text is a STATIC project file, never user/profile input —
-- no injection surface.
--
-- PG11-safe: COLLATE "C", PARALLEL SAFE and plain SQL functions are PG9.6+ (the
-- testcontainers fixture is PostgreSQL 11).

CREATE OR REPLACE FUNCTION synthetic.ascii_fold(t text)
    RETURNS text
    LANGUAGE sql
    IMMUTABLE STRICT PARALLEL SAFE
    AS $$
    SELECT lower($1 COLLATE "C")
$$;

CREATE OR REPLACE FUNCTION synthetic.arm_id_key(id text)
    RETURNS text
    LANGUAGE sql
    IMMUTABLE STRICT PARALLEL SAFE
    AS $$
    SELECT synthetic.ascii_fold($1)
$$;
