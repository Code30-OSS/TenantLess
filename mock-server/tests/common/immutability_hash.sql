-- ===========================================================================
-- Canonical shared source for the immutability bracket AND the
-- estate hash -- edit in ONE place; both proofs read this file
-- verbatim. (Ensures the two "immutability"
-- proofs cannot measure different things if they share this exact expression.)
--
-- WHAT IT COMPUTES
--   A single deterministic scalar digest over the seeded synthetic baseline that
--   configuration drift can touch, so that hashing before/after a drift APPLY and
--   before/after a drift REVERT yields byte-identical digests iff `synthetic.*`
--   was never mutated in place (baseline immutability).
--
-- INCLUDED RELATIONS (the two relations drift can touch)
--   * synthetic.resources        -- full column set, sql/001 declared order
--   * synthetic.resource_groups  -- full column set, sql/001 declared order
--   Explicit column lists ONLY -- never SELECT *. Adding a column to
--   sql/001 requires a conscious edit here (this file is the single source).
--
-- DELIBERATELY OUT (drift-invariant, seeded-but-never-mutated) -- the agreed
-- boundary; the estate hash covers the SAME set:
--   * synthetic.tenant           -- generation metadata; drift never writes it
--   * synthetic.subscriptions    -- subscription directory; drift never writes it
--   These are enumerated here as documented-OUT so the boundary is unambiguous.
--   (Drift only ever mutates resources / resource_groups; subscriptions + tenant
--   are structurally outside every drift mutation path.)
--
-- EXCLUDED (mutable overlay + drift ledger -- MUST NOT appear below):
--   synthetic.arm_overlay, synthetic.arm_overlay_revision_seq,
--   synthetic.drift_batches, synthetic.drift_records.
--
-- CANONICALIZATION
--   * Row order: deterministic by the stable PK `id` (per relation), then a fixed
--     relation order (resources before resource_groups) so a global aggregate is
--     unambiguous.
--   * Preimage identity: each per-row preimage is PREFIXED with the relation name
--     AND the row id, so a row that hypothetically moved between relations cannot
--     alias to an equal digest.
--   * NULL sentinel: E'\x1e' (ASCII record-separator) -- a control byte that never
--     appears in ARM string data, so NULL is distinct from the empty string.
--   * Field separator: E'\x1f' (ASCII unit-separator) between columns.
--   * jsonb columns (`tags`, `sku`, `properties`) are cast with `::text`, which
--     emits Postgres' normalized jsonb form (canonical key order / whitespace /
--     number form). The bracket compares hashes computed on the SAME database, so
--     this normalization is internally consistent by construction.
--   * Each row is reduced to a fixed-width md5 hex BEFORE aggregation, so the
--     outer separator-free concatenation cannot be ambiguous.
--
-- OUTPUT: one row, one column `estate_hash` (md5 hex, lowercase). Empty relations
--   fold to the empty string (coalesced) rather than NULL, so the digest is always
--   well-defined.
-- ===========================================================================
SELECT md5(
    coalesce(
        string_agg(row_digest, '' ORDER BY rel_order, row_id),
        ''
    )
) AS estate_hash
FROM (
    -- synthetic.resources : id, subscription_id, resource_group_name, name, type,
    -- location, tags, sku, kind, properties, provisioning_state, managed_by
    SELECT
        0 AS rel_order,
        r.id AS row_id,
        md5(
            'synthetic.resources'  -- SYNRES-ALLOW[drift-hashing]: md5 domain-separation label (a string constant, not a table read)
            || E'\x1f' || r.id
            || E'\x1f' || coalesce(r.subscription_id::text, E'\x1e')
            || E'\x1f' || coalesce(r.resource_group_name, E'\x1e')
            || E'\x1f' || coalesce(r.name, E'\x1e')
            || E'\x1f' || coalesce(r.type, E'\x1e')
            || E'\x1f' || coalesce(r.location, E'\x1e')
            || E'\x1f' || coalesce(r.tags::text, E'\x1e')
            || E'\x1f' || coalesce(r.sku::text, E'\x1e')
            || E'\x1f' || coalesce(r.kind, E'\x1e')
            || E'\x1f' || coalesce(r.properties::text, E'\x1e')
            || E'\x1f' || coalesce(r.provisioning_state, E'\x1e')
            || E'\x1f' || coalesce(r.managed_by, E'\x1e')
        ) AS row_digest
    FROM synthetic.resources r  -- SYNRES-ALLOW[drift-hashing]: estate hash must digest the raw immutable seeded baseline

    UNION ALL

    -- synthetic.resource_groups : id, subscription_id, name, location,
    -- template_type, tags, provisioning_state
    SELECT
        1 AS rel_order,
        g.id AS row_id,
        md5(
            'synthetic.resource_groups'
            || E'\x1f' || g.id
            || E'\x1f' || coalesce(g.subscription_id::text, E'\x1e')
            || E'\x1f' || coalesce(g.name, E'\x1e')
            || E'\x1f' || coalesce(g.location, E'\x1e')
            || E'\x1f' || coalesce(g.template_type, E'\x1e')
            || E'\x1f' || coalesce(g.tags::text, E'\x1e')
            || E'\x1f' || coalesce(g.provisioning_state, E'\x1e')
        ) AS row_digest
    FROM synthetic.resource_groups g
) AS estate_rows;
