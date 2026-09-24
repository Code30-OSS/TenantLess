-- 012_arm_id_identity_cutover.sql: re-derive the arm_overlay identity CHECK onto the
-- canonical ARM-ID fold.
--
-- sql/009 shipped `ck_arm_overlay_id_lower CHECK (id_lower = lower(id))` — a locale-aware
-- derivation. The identity contract is the ASCII-only fold `synthetic.arm_id_key` (sql/011):
-- A-Z -> a-z and NOTHING else, byte-identical to the Rust and Python folds. This migration
-- moves the CHECK onto that derivation. sql/009 is NOT edited in place: an already-migrated
-- volume never re-adds a same-named constraint (its guarded ADD is a no-op on a duplicate),
-- so the derivation change must be its own migration.
--
-- pg_constraint-CONDITIONAL, never blind:
--   * overlay table absent (a bare `generate` volume)            -> NO-OP;
--   * the live CHECK already derives from arm_id_key(id)         -> NO-OP (the constraint is
--     NOT dropped and re-added, so a re-run on every boot takes no ACCESS EXCLUSIVE lock);
--   * otherwise                                                 -> DROP the old CHECK and
--     ADD the arm_id_key-derived CHECK, in one statement block.
--
-- AUDIT-GATED, fail-loud: before touching the CHECK the block re-verifies that EVERY stored
-- id_lower already equals synthetic.arm_id_key(id). For all-ASCII ids lower() and arm_id_key()
-- agree byte-for-byte, so an ordinary estate converts without rewriting a single row. A row
-- whose stored key was derived by locale lower() from a non-ASCII id (e.g. an accented
-- capital) is a real identity divergence: the block RAISES naming that id and changes
-- nothing. It NEVER rewrites id_lower and NEVER merges keys — resolving a divergence is an
-- explicit operator decision. The Python / Rust provisioning seams run the full identity
-- audit (baseline divergence + fold collisions too) before applying this file; this in-file
-- guard is the last line of defence for paths with no audit in front (Docker initdb).
--
-- Transaction-free DDL (a DO block): honest under Docker's autocommit initdb path. The bounded
-- lock_timeout + serializing advisory lock live in the two provisioning seams that guarantee
-- an explicit transaction (lib.rs ensure_arm_id_identity_cutover_schema and
-- writer.ensure_arm_id_identity_cutover_schema). arm_overlay is a small table, so the brief
-- ACCESS EXCLUSIVE taken by the one-time DROP/ADD CONSTRAINT is harmless. Requires the
-- sql/011 fold functions. The statement text is a STATIC project file — no injection surface.
-- PG11-safe (plpgsql, pg_get_constraintdef, position()).

DO $$
DECLARE
    live_def text;
    bad_id   text;
BEGIN
    IF to_regclass('synthetic.arm_overlay') IS NULL THEN
        RETURN;  -- no overlay substrate on this volume: nothing to re-derive
    END IF;

    SELECT pg_get_constraintdef(c.oid)
      INTO live_def
      FROM pg_constraint c
     WHERE c.conrelid = 'synthetic.arm_overlay'::regclass
       AND c.contype = 'c'
       AND c.conname = 'ck_arm_overlay_id_lower';

    IF live_def IS NOT NULL AND position('arm_id_key(id)' in live_def) > 0 THEN
        RETURN;  -- already on the canonical fold: NO-OP
    END IF;

    SELECT o.id
      INTO bad_id
      FROM synthetic.arm_overlay o
     WHERE o.id_lower IS DISTINCT FROM synthetic.arm_id_key(o.id)
     ORDER BY o.id
     LIMIT 1;
    IF bad_id IS NOT NULL THEN
        RAISE EXCEPTION
            'ARM-ID identity cutover refused: arm_overlay id % has a stored id_lower that differs from synthetic.arm_id_key(id); resolve the divergence before migrating identity',
            bad_id;
    END IF;

    IF live_def IS NOT NULL THEN
        ALTER TABLE synthetic.arm_overlay DROP CONSTRAINT ck_arm_overlay_id_lower;
    END IF;
    ALTER TABLE synthetic.arm_overlay
        ADD CONSTRAINT ck_arm_overlay_id_lower CHECK (id_lower = synthetic.arm_id_key(id));
END $$;
