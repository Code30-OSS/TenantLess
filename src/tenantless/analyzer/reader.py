"""Read-only DuckDB reader (DuckDB-specific seam).

This is the ONLY module in the analyzer package that imports ``duckdb``.
Phase 6 replaces it with a ConnectorX/Postgres reader exposing the same
aggregation-helper surface, leaving the extractor and privacy layers untouched.

All connections are opened ``read_only=True``. Aggregation (COUNT / GROUP BY)
is pushed into SQL so we never ``SELECT *`` the full 96K-row ``resources`` table
into memory; only small pre-aggregated frames cross into Polars.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import duckdb
import polars as pl


@contextmanager
def open_duckdb(path: str) -> Iterator["DuckDBReader"]:
    """Open a read-only DuckDB connection and yield a :class:`DuckDBReader`.

    The connection is always opened ``read_only=True`` so the external
    multi-gigabyte scanner.duckdb is never mutated or copied.
    """
    conn = duckdb.connect(path, read_only=True)
    try:
        yield DuckDBReader(conn)
    finally:
        conn.close()


class DuckDBReader:
    """Thin aggregation surface over a read-only DuckDB connection.

    Each method pushes COUNT / GROUP BY into SQL and returns a small Polars
    DataFrame. No method loads raw per-resource rows.
    """

    def __init__(self, conn: "duckdb.DuckDBPyConnection") -> None:
        self._conn = conn

    def _agg(self, sql: str, params: "list | None" = None) -> pl.DataFrame:
        """Run an aggregation query and return a Polars frame.

        We build the frame from the cursor's small result set (rows + column
        names) rather than DuckDB's ``.pl()``/``.arrow()`` path, which would
        require pyarrow. Aggregation is pushed into SQL so the result is tiny;
        no per-resource rows are materialized.

        ``params`` are passed through as DuckDB bind parameters for any ``?``
        placeholders in ``sql`` -- never string-spliced into the query text.
        """
        cur = self._conn.execute(sql, params) if params else self._conn.execute(sql)
        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
        return pl.DataFrame(rows, schema=columns, orient="row")

    def source_stats(self) -> dict[str, int]:
        """Return total subscription / resource-group / resource counts."""
        subs = self._conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
        rgs = self._conn.execute("SELECT COUNT(*) FROM resource_groups").fetchone()[0]
        res = self._conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0]
        return {
            "total_subscriptions": int(subs),
            "total_resource_groups": int(rgs),
            "total_resources": int(res),
        }

    def resource_type_counts(self) -> pl.DataFrame:
        """Return a (type, count) frame aggregated SQL-side.

        Uses GROUP BY -- never ``SELECT *`` over the resources table.
        """
        return self._agg(
            "SELECT type AS type, COUNT(*) AS count "
            "FROM resources GROUP BY type ORDER BY count DESC"
        )

    def location_counts(self) -> pl.DataFrame:
        """Return a (location, count) frame over all resources, SQL-side.

        Used for the GLOBAL location distribution. Per-archetype affinity is
        derived from :meth:`subscription_features` instead.
        """
        return self._agg(
            "SELECT location AS location, COUNT(*) AS count "
            "FROM resources GROUP BY location ORDER BY count DESC"
        )

    def subscription_features(self) -> pl.DataFrame:
        """Return one row per subscription with archetype feature columns.

        Columns:
            ``subscription_id``      -- raw id (used only to join location mix;
                                        never crosses into the output profile).
            ``resource_count``       -- total resources in the subscription.
            ``rg_count``             -- distinct resource groups.
            ``tag_density``          -- avg number of tag KEYS per resource.
            ``loc__<location>``      -- per-subscription COUNT of resources in
                                        each location (a location-mix vector;
                                        the columns are the union of locations).

        Everything is computed SQL-side: per-subscription GROUP BY plus a pivot
        of the location histogram. No per-resource rows are materialized into
        Python -- only the small per-subscription result set crosses the seam.
        """
        # Base per-subscription aggregates. tag_density = average count of JSON
        # tag keys per resource (json_keys length); empty/absent tags count 0.
        base = self._agg(
            """
            SELECT
                subscription_id AS subscription_id,
                COUNT(*) AS resource_count,
                COUNT(DISTINCT resource_group) AS rg_count,
                AVG(
                    CASE
                        WHEN tags IS NULL THEN 0
                        ELSE COALESCE(LEN(json_keys(tags)), 0)
                    END
                ) AS tag_density
            FROM resources
            GROUP BY subscription_id
            """
        )

        # Per-(subscription, location) histogram, then pivot to wide loc__ cols.
        loc_long = self._agg(
            """
            SELECT
                subscription_id AS subscription_id,
                location AS location,
                COUNT(*) AS loc_count
            FROM resources
            GROUP BY subscription_id, location
            """
        )

        if loc_long.is_empty():
            # Deterministic row order so downstream KMeans++ init is reproducible
            # (DuckDB GROUP BY returns rows in arbitrary, thread-dependent order).
            return base.sort("subscription_id")

        loc_wide = loc_long.pivot(
            on="location",
            index="subscription_id",
            values="loc_count",
            aggregate_function="sum",
        ).fill_null(0)
        # Prefix location columns so they are unambiguous downstream.
        loc_wide = loc_wide.rename(
            {
                c: f"loc__{c}"
                for c in loc_wide.columns
                if c != "subscription_id"
            }
        )

        # Determinism: pivot emits loc__ columns in arbitrary, thread-dependent
        # order, and DuckDB's GROUP BY/join row order is likewise arbitrary.
        # Sort BOTH the columns and the rows so the feature matrix fed to
        # KMeans (and thus subscription_archetypes) is reproducible across runs.
        joined = base.join(loc_wide, on="subscription_id", how="left").fill_null(0)
        fixed_cols = ["subscription_id", "resource_count", "rg_count", "tag_density"]
        loc_cols = sorted(c for c in joined.columns if c not in fixed_cols)
        ordered = [c for c in fixed_cols if c in joined.columns] + loc_cols
        return joined.select(ordered).sort("subscription_id")

    def tag_key_counts(self) -> pl.DataFrame:
        """Return a ``(tag_key, count)`` frame: #resources carrying each tag key.

        Unnests the resource ``tags`` JSON object keys SQL-side and counts how
        many resources carry each key. Tag KEYS are generic schema (Environment,
        BU, ...) and may cross the boundary; tag VALUES are handled separately
        and bucketed. Returns columns ``tag_key`` and ``count``.
        """
        return self._agg(
            """
            SELECT
                json_keys_unnest AS tag_key,
                COUNT(*) AS count
            FROM (
                SELECT unnest(json_keys(tags)) AS json_keys_unnest
                FROM resources
                WHERE tags IS NOT NULL
            )
            GROUP BY tag_key
            ORDER BY count DESC
            """
        )

    def tag_value_counts(self) -> pl.DataFrame:
        """Return a ``(tag_key, tag_value, count)`` frame over resource tags.

        For each resource tag key, counts the occurrences of each value. Values
        are min-bucket merged and denylist-scanned downstream so real values
        (country codes, owner names, ...) never leak. Returns columns
        ``tag_key``, ``tag_value`` and ``count``.
        """
        return self._agg(
            """
            SELECT
                kv.key AS tag_key,
                json_extract_string(tags, kv.key) AS tag_value,
                COUNT(*) AS count
            FROM resources,
                 (SELECT unnest(json_keys(tags)) AS key) AS kv
            WHERE tags IS NOT NULL
            GROUP BY tag_key, tag_value
            ORDER BY count DESC
            """
        )

    def total_resources(self) -> int:
        """Return the total resource count (for normalizing rates SQL-side)."""
        return int(
            self._conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0]
        )

    def type_property_value_counts(self, rtype: str) -> pl.DataFrame:
        """Return ``(field, value, count)`` over the ``properties`` JSON for a type.

        Unnests the top-level keys of each resource's ``properties`` JSON for the
        given raw resource ``type`` and counts (field, value) pairs. Only
        top-level scalar fields are surfaced (objects/arrays are stringified by
        ``json_extract_string`` and bucketed downstream). Returns columns
        ``field``, ``value`` and ``count``.
        """
        return self._agg(
            """
            SELECT
                kv.key AS field,
                json_extract_string(properties, kv.key) AS value,
                COUNT(*) AS count
            FROM resources,
                 (SELECT unnest(json_keys(properties)) AS key) AS kv
            WHERE type = ? AND properties IS NOT NULL
            GROUP BY field, value
            ORDER BY count DESC
            """,
            [rtype],
        )

    def type_sku_value_counts(self, rtype: str) -> pl.DataFrame:
        """Return ``(field, value, count)`` over the ``sku`` JSON for a type.

        Same shape as :meth:`type_property_value_counts` but over the ``sku``
        JSON. Returns columns ``field``, ``value`` and ``count``.
        """
        return self._agg(
            """
            SELECT
                kv.key AS field,
                json_extract_string(sku, kv.key) AS value,
                COUNT(*) AS count
            FROM resources,
                 (SELECT unnest(json_keys(sku)) AS key) AS kv
            WHERE type = ? AND sku IS NOT NULL
            GROUP BY field, value
            ORDER BY count DESC
            """,
            [rtype],
        )

    def type_kind_counts(self, rtype: str) -> pl.DataFrame:
        """Return ``(field, value, count)`` over the ``kind`` column for a type.

        In the DuckDB scan ``kind`` is a dedicated column. The shape mirrors
        :meth:`type_sku_value_counts` (one synthetic field ``kind``) so it flows
        through ``type_shapes.extract_into`` unchanged.
        """
        return self._agg(
            """
            SELECT 'kind' AS field, kind AS value, COUNT(*) AS count
            FROM resources
            WHERE type = ? AND kind IS NOT NULL
            GROUP BY value
            ORDER BY count DESC
            """,
            [rtype],
        )

    def resource_name_samples(self) -> pl.DataFrame:
        """Return a ``(name, type)`` frame of resource names (ANLZ-08).

        Twin of ``PostgresReader.resource_name_samples``. The naming extractor
        tokenizes these into structural classes; no verbatim name is emitted.
        """
        return self._agg(
            "SELECT name AS name, type AS type "
            "FROM resources WHERE name IS NOT NULL"
        )

    def cross_subscription_reference_counts(self) -> dict[str, int]:
        """Return cross-subscription reference signal counts.

        Counts how many resources carry, in their ``properties`` JSON text, a
        resource-id reference whose ``/subscriptions/<id>/`` segment differs from
        the resource's own ``subscription_id`` (a cross-sub reference). Also
        returns the number of subscriptions that originate at least one such
        reference (``spoke_subscriptions``) and the number of distinct target
        subscriptions (``hub_subscriptions``). Returns a dict with keys
        ``cross_ref_resources``, ``spoke_subscriptions``, ``hub_subscriptions``,
        and ``total_resources``.

        The detection is a TEXT heuristic (regex over the JSON) so it stays
        source-agnostic and cheap; no per-resource rows cross the seam -- only
        the small aggregate dict.
        """
        # Resources whose properties reference a /subscriptions/<other>/ id.
        row = self._conn.execute(
            r"""
            WITH refs AS (
                SELECT
                    subscription_id,
                    regexp_extract(
                        CAST(properties AS VARCHAR),
                        '/subscriptions/([0-9a-zA-Z-]+)/',
                        1
                    ) AS ref_sub
                FROM resources
                WHERE properties IS NOT NULL
                  AND CAST(properties AS VARCHAR) LIKE '%/subscriptions/%'
            ),
            cross_refs AS (
                SELECT subscription_id, ref_sub
                FROM refs
                WHERE ref_sub IS NOT NULL
                  AND ref_sub != ''
                  AND ref_sub != subscription_id
            )
            SELECT
                (SELECT COUNT(*) FROM cross_refs) AS cross_ref_resources,
                (SELECT COUNT(DISTINCT subscription_id) FROM cross_refs)
                    AS spoke_subscriptions,
                (SELECT COUNT(DISTINCT ref_sub) FROM cross_refs)
                    AS hub_subscriptions
            """
        ).fetchone()
        return {
            "cross_ref_resources": int(row[0] or 0),
            "spoke_subscriptions": int(row[1] or 0),
            "hub_subscriptions": int(row[2] or 0),
            "total_resources": self.total_resources(),
        }

    def finding_type_counts(self) -> pl.DataFrame:
        """Return a ``(finding_type, count)`` frame from the findings table.

        Aggregates the real findings table by ``finding_type`` SQL-side. The
        violations extractor maps these to the simulator violation vocabulary and
        normalizes by total resources. Returns columns ``finding_type`` and
        ``count``. If the findings table is absent, returns an empty frame.
        """
        try:
            return self._agg(
                """
                SELECT finding_type AS finding_type, COUNT(*) AS count
                FROM findings
                GROUP BY finding_type
                ORDER BY count DESC
                """
            )
        except duckdb.CatalogException:  # no findings table in this source
            return pl.DataFrame(
                {"finding_type": [], "count": []},
                schema={"finding_type": pl.Utf8, "count": pl.Int64},
            )

    def resource_cost_samples(self) -> pl.DataFrame:
        """Return a privacy-safe ``(type, monthly_cost)`` cost-sample frame (COST-01).

        Joins the seed's ``resource_costs`` table to ``resources`` and aggregates
        one monthly-cost sample per ``(resource, billing_month)``: the real seed
        carries ~19% of (resource, month) pairs as 2-3 separate meter rows, so the
        amounts are ``SUM``-ed into a single per-resource-month figure (never two
        rows, never max). The join is CASE-INSENSITIVE (``lower(id)=lower(id)``):
        a raw-equality join silently recovers 0% on this seed where casing differs
        between the two tables (Pitfall 4); the case-folded join recovers ~99.1%.

        Only ``(type, monthly_cost)`` crosses the seam -- the GROUP BY keys
        (``resource_id``, ``billing_month``) and ``subscription_id`` stay inside
        the SQL and never reach Python, so no real identifier can leak from this
        path (the outer projection drops them explicitly).

        Currency note (D-11): the seed amounts are EUR (``amortized_cost_eur``);
        the fitted magnitudes carry over relabeled as USD for v2.0 (no FX applied).

        If the source has no ``resource_costs`` table (e.g. a non-FinOps scan),
        returns an empty ``{"type": [], "monthly_cost": []}`` frame so the
        generator simply zero-fills cost (D-02 back-compat) -- never an exception.
        """
        try:
            return self._agg(
                """
                SELECT type, monthly_cost
                FROM (
                    SELECT
                        r.type AS type,
                        SUM(rc.amortized_cost_eur) AS monthly_cost
                    FROM resource_costs rc
                    JOIN resources r
                      ON lower(r.resource_id) = lower(rc.resource_id)
                    GROUP BY rc.resource_id, rc.billing_month, r.type
                )
                """
            )
        except duckdb.CatalogException:  # no resource_costs table in this source
            return pl.DataFrame(
                {"type": [], "monthly_cost": []},
                schema={"type": pl.Utf8, "monthly_cost": pl.Float64},
            )

    def rg_type_sets(self) -> pl.DataFrame:
        """Return one row per resource group with its type-set composition.

        Grain is one row per ACTUAL resource group — ``(subscription_id, name)`` —
        not per bare name, so two same-named RGs in different subscriptions are
        distinct RGs (a name-only grain merged them, distorting compositions and
        hiding per-sub duplicates).

        TRUE-EMPTY RGs: the frame is a FULL OUTER JOIN of the resources-derived
        compositions against the source's ``resource_groups`` table, so a resource
        group that EXISTS but holds zero resources appears with an empty
        ``type_set`` and ``resource_count`` 0. This lets ``rg_templates`` fold those
        genuine empties into ``__misc__`` and derive a real ``empty_share`` — a
        resources-only frame can never see an empty RG (it has no resource rows),
        so the generator would otherwise model 0% empties for a tenant that has
        some. Names are matched case-insensitively (Azure RG names are
        case-insensitive) so a casing skew never fabricates a phantom empty RG.

        Columns:
            ``resource_group``  -- RG name (used only for grouping; never output).
            ``type_set``        -- sorted distinct list of resource ``type``
                                   strings present in the RG (empty for a true-empty RG).
            ``resource_count``  -- total resources in the RG (0 for a true-empty RG).

        Falls back to a resources-only (still per-(sub,name)) frame when the source
        has no ``resource_groups`` table (e.g. a minimal fixture / some live scans).
        """
        try:
            return self._agg(
                """
                WITH res AS (
                    SELECT subscription_id AS sub, lower(resource_group) AS rg_key,
                           any_value(resource_group) AS resource_group,
                           list_sort(array_agg(DISTINCT type)) AS type_set,
                           COUNT(*) AS resource_count
                    FROM resources
                    GROUP BY subscription_id, lower(resource_group)
                ),
                rgs AS (
                    SELECT subscription_id AS sub, lower(name) AS rg_key,
                           any_value(name) AS name
                    FROM resource_groups
                    GROUP BY subscription_id, lower(name)
                )
                SELECT
                    COALESCE(res.resource_group, rgs.name) AS resource_group,
                    COALESCE(res.type_set, CAST([] AS VARCHAR[])) AS type_set,
                    COALESCE(res.resource_count, 0) AS resource_count
                FROM rgs
                FULL OUTER JOIN res
                  ON rgs.sub = res.sub AND rgs.rg_key = res.rg_key
                """
            )
        except duckdb.CatalogException:  # no resource_groups table in this source
            return self._agg(
                """
                SELECT
                    resource_group AS resource_group,
                    list_sort(array_agg(DISTINCT type)) AS type_set,
                    COUNT(*) AS resource_count
                FROM resources
                GROUP BY subscription_id, resource_group
                """
            )

    def rg_type_pairs(self) -> pl.DataFrame:
        """Return ``(type_a, type_b, cooccur)`` resource-type co-occurrence (ANLZ-04).

        Self-join over DISTINCT (subscription_id, resource_group, type): ``cooccur``
        is the number of RGs each UNORDERED type pair (``a.type < b.type``) shares.
        Twin of ``PostgresReader.rg_type_pairs`` (DuckDB dialect).
        """
        return self._agg(
            """
            WITH rg_types AS (
                SELECT DISTINCT subscription_id, resource_group, type
                FROM resources
            )
            SELECT a.type AS type_a, b.type AS type_b, COUNT(*) AS cooccur
            FROM rg_types a
            JOIN rg_types b
              ON a.subscription_id = b.subscription_id
             AND a.resource_group  = b.resource_group
             AND a.type < b.type
            GROUP BY a.type, b.type
            ORDER BY cooccur DESC
            """
        )

    def tag_key_pair_counts(self) -> pl.DataFrame:
        """Return ``(key_a, key_b, count)`` tag-key co-occurrence (ANLZ-07).

        Per resource, all tag keys are self-joined (``ka < kb``); ``count`` is the
        number of resources carrying BOTH keys. Twin of
        ``PostgresReader.tag_key_pair_counts`` (DuckDB dialect).
        """
        return self._agg(
            """
            WITH keyed AS (
                SELECT rowid AS rid, unnest(json_keys(tags)) AS k
                FROM resources
                WHERE tags IS NOT NULL
            )
            SELECT a.k AS key_a, b.k AS key_b, COUNT(*) AS count
            FROM keyed a
            JOIN keyed b ON a.rid = b.rid AND a.k < b.k
            GROUP BY a.k, b.k
            ORDER BY count DESC
            """
        )

    def type_tag_coverage(self) -> pl.DataFrame:
        """Return ``(type, total, tagged)`` per resource type (ANLZ-07).

        ``total`` is the resource count for the type; ``tagged`` counts those with
        a NON-EMPTY tags object. Twin of ``PostgresReader.type_tag_coverage``.
        """
        return self._agg(
            """
            SELECT
                type AS type,
                COUNT(*) AS total,
                COUNT(*) FILTER (
                    WHERE tags IS NOT NULL
                      AND json_keys(tags) != '[]'
                ) AS tagged
            FROM resources
            GROUP BY type
            """
        )
