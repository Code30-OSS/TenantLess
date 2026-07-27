"""Pure ARG -> scan-schema materializer (azure-free heart of the direct scan).

A single ``$skipToken`` paging pass normalizes each Azure Resource Graph (ARG)
ObjectArray row, inserts it into ONE in-memory DuckDB table whose schema matches
the existing scan schema, derives the auto-denylist from the tenant's own
identifiers in the SAME loop, and yields the EXISTING ``DuckDBReader`` (D-10/D-12)
plus the derived term set.

Below the reader seam *nothing* is new (RESEARCH "Don't Hand-Roll"): the value is
a faithful ARG -> scan-schema adapter + a denylist accumulator that hands off to
the proven, privacy-reviewed aggregation SQL. This module imports only ``duckdb``,
``orjson`` and ``contextlib`` -- NEVER any ``azure-*`` package, so the whole
paging/materialize/denylist path runs on the core CI install (D-08).
"""

from __future__ import annotations

import contextlib
from typing import Iterator

import duckdb
import orjson

# Azure-free import (tags extractor pulls only re/polars/privacy): the auto-
# denylist must mirror the tags extractor's positive VALUE allowlist so it never
# denylists a value the pipeline deliberately PUBLISHES (see ``_row_terms``).
from tenantless.analyzer.extractors.tags import (
    VALUE_ALLOWLIST_KEYS,
    _value_allowed_for_key,
)

# --------------------------------------------------------------------------- #
# P1-b: live-scan tag-KEY allowlist (Azure-INGESTION-LOCAL policy, by decision).
#
# The shared tags extractor publishes EVERY non-identifier-shaped tag KEY into
# ``key_frequencies`` -- a safe assumption for the curated DuckDB seed, but on an
# arbitrary LIVE tenant a custom key (``AcmeProjectKey``, ``ContosoCostCode``)
# embeds tenant-identifying tokens. So on the azure path ONLY (no change to the
# shared extractor / DuckDB goldens) we retain just KNOWN-GENERIC governance/ops
# key names in the materialized tags; every other key is DROPPED before it
# reaches the reader AND (with its value) added to the auto-denylist as a
# backstop. Conservative: a key not on this list is treated as tenant-specific.
# Superset of ``VALUE_ALLOWLIST_KEYS`` (a key whose VALUE we publish must itself
# be publishable). Case-insensitive membership via :func:`_key_is_generic`.
# --------------------------------------------------------------------------- #
_GENERIC_KEY_NAMES: frozenset[str] = frozenset(
    k.lower()
    for k in {
        "Owner", "Owners", "Application", "App", "AppName", "ApplicationName",
        "Project", "ProjectName", "Team", "Department", "Dept", "Service",
        "ServiceName", "Component", "Workload", "Role", "Function", "Stage",
        "Lifecycle", "Purpose", "Description", "Contact", "Email", "CreatedBy",
        "CreatedDate", "CreationDate", "Version", "Name", "DisplayName",
        "Product", "Platform", "Group", "Schedule", "Expiry", "ExpiryDate",
        "AutoShutdown", "Monitoring", "Logging", "Project Code", "ProjectCode",
    }
)
GENERIC_TAG_KEY_ALLOWLIST: frozenset[str] = _GENERIC_KEY_NAMES | VALUE_ALLOWLIST_KEYS


def _key_is_generic(key: str) -> bool:
    """True if a tag KEY is a known-generic name safe to publish (azure path).

    Case-insensitive membership in :data:`GENERIC_TAG_KEY_ALLOWLIST`. A key not
    on the allowlist is treated as tenant-specific: dropped from the materialized
    tags and added (with its value) to the auto-denylist.
    """
    return key.strip().lower() in GENERIC_TAG_KEY_ALLOWLIST


def _generic_tags_only(tags: object) -> dict | None:
    """Keep only generic-allowlisted KEYS from an ARG ``tags`` dict (P1-b).

    Returns the filtered dict, or ``None`` when the input is not a dict or
    nothing survives (so ``_json_or_null`` stores SQL NULL just like ``{}``).
    """
    if not isinstance(tags, dict):
        return None
    kept = {k: v for k, v in tags.items() if _key_is_generic(str(k))}
    return kept or None

# --------------------------------------------------------------------------- #
# ARG projection (KQL). STATIC const -- the subscription filter is bound as
# QueryRequest.subscriptions (12-03), never spliced into this text.
#   * `order by id asc` is MANDATORY for stable, non-overlapping $skipToken pages
#     (Pitfall 1).
#   * scalar columns are projected so ARG emits $skipToken (Pitfall 3).
#   * NO limit/take/sample -- those suppress $skipToken and silently truncate
#     the scan to the first 1000 rows.
# --------------------------------------------------------------------------- #
ARG_PROJECTION = (
    "Resources "
    "| project id, name, type, location, resourceGroup, subscriptionId, "
    "tags, properties, sku, kind "
    "| order by id asc"
)

# Static RG-enumeration query (P2-a). ``Resources`` never returns a resource
# group that holds zero resources, so an empty RG would be invisible to a count
# built purely from the resources rows. ``ResourceContainers`` lists EVERY RG in
# scope (empty or not), giving ``total_resource_groups`` the SAME meaning as the
# DuckDB scan's ``resource_groups`` table. STATIC const -- the subscription
# filter is bound as ``QueryRequest.subscriptions``, never spliced. ``order by``
# a total key for stable, non-overlapping ``$skipToken`` pages.
RESOURCE_GROUP_ENUM_QUERY = (
    "ResourceContainers "
    "| where type =~ 'microsoft.resources/subscriptions/resourcegroups' "
    "| project subscriptionId, name "
    "| order by subscriptionId asc, name asc"
)

# The three-table scan schema the EXISTING DuckDBReader reads. ``subscriptions``
# and ``resource_groups`` are COUNT-only for source_stats; ``resources`` carries
# the columns referenced by all 17 reader methods (JSON for tags/properties/sku).
_DDL = """
CREATE TABLE subscriptions   (subscription_id VARCHAR);
CREATE TABLE resource_groups (subscription_id VARCHAR, name VARCHAR);
CREATE TABLE resources (
    scan_id VARCHAR, resource_id VARCHAR, name VARCHAR, type VARCHAR,
    location VARCHAR, resource_group VARCHAR, subscription_id VARCHAR,
    properties JSON, sku JSON, tags JSON, kind VARCHAR
);
"""

# Bound-parameter INSERT (project SQL-injection bar, MEMORY mock-server-sql-
# injection-bar): every ARG value is bound as a ``?`` placeholder via
# ``executemany`` -- never string-spliced/formatted.
_INSERT_RESOURCES = "INSERT INTO resources VALUES (?,?,?,?,?,?,?,?,?,?,?)"


def _json_or_null(v: object) -> str | None:
    """Coerce an ARG dynamic column (dict/list/None) for the DuckDB JSON column.

    DuckDB's ``JSON`` column accepts a JSON *string* on INSERT (exactly what the
    fixture builder does). Serialize with ``orjson.dumps(v).decode()``; return
    ``None`` for ``None``/``{}`` so ``tags IS NULL`` / ``WHERE properties IS NOT
    NULL`` predicates behave identically to the DuckDB-file path (Pitfall 4).
    """
    if v is None or v == {}:
        return None
    return orjson.dumps(v).decode()


def _normalize(row: dict) -> tuple:
    """Map a camelCase ARG ObjectArray row to the 11-col ``resources`` tuple.

    Positions match ``resources(scan_id, resource_id, name, type, location,
    resource_group, subscription_id, properties, sku, tags, kind)``. ``scan_id``
    is the constant literal ``"azure"``; tags/properties/sku dicts are serialized
    to JSON strings (or NULL); ``kind`` is a top-level scalar.

    P1-b: ``tags`` is filtered to GENERIC-ALLOWLISTED keys BEFORE serialization,
    so a tenant-specific custom key never reaches ``key_frequencies`` (the
    dropped key + its value are captured into the denylist by :func:`_row_terms`,
    which reads the RAW row).
    """
    return (
        "azure",
        row.get("id"),
        row.get("name"),
        row.get("type"),
        row.get("location"),
        row.get("resourceGroup"),
        row.get("subscriptionId"),
        _json_or_null(row.get("properties")),
        _json_or_null(row.get("sku")),
        _json_or_null(_generic_tags_only(row.get("tags"))),
        row.get("kind"),
    )


def _row_terms(row: dict) -> tuple[set[str], set[str]]:
    """Return ``(identifier_terms, published_enum_values)`` for one ARG row.

    ``identifier_terms`` -- the tenant identifiers for the auto-denylist (D-01):
    the ``subscriptionId``, ``resourceGroup`` and ``name`` scalars plus the
    IDENTIFIER-bearing ``tags`` VALUE strings (values of NON-allowlisted keys,
    which the tags extractor DROPS). Accumulated per page into an in-memory
    ``set[str]``; never persisted (D-11).

    ``published_enum_values`` -- tag VALUES whose KEY is on the tags extractor's
    positive VALUE allowlist (``Environment``, ``BU``, ``Criticality``, ...).
    Those bounded governance enums are deliberately PUBLISHED into the profile,
    so the caller SUBTRACTS them from the final denylist. This is what stops a
    generic enum value (``Environment=prod``) from tripping the unchanged
    ``scan_denylist`` backstop when the SAME token ``prod`` also happens to be a
    real resource/RG NAME on some other resource (P1-c collision) -- the earlier
    fix only excluded the value at its own source, not the name-sourced twin.
    """
    idents: set[str] = set()
    published: set[str] = set()
    for key in ("subscriptionId", "resourceGroup", "name"):
        v = row.get(key)
        if isinstance(v, str) and v.strip():
            idents.add(v)
    tags = row.get("tags") or {}
    if isinstance(tags, dict):
        for tag_key, val in tags.items():
            k = str(tag_key)
            s = str(val)
            if not _key_is_generic(k):
                # P1-b: a custom (non-generic) key is DROPPED from the output, so
                # both the key and its value are tenant-identifier candidates.
                if k.strip():
                    idents.add(k)
                if s.strip():
                    idents.add(s)
                continue
            # Generic key: published as a key_frequencies key -> subtract it from
            # the denylist (P1-c: a real RG/resource name equal to a generic key
            # token must not trip the backstop on the published key).
            if k.strip():
                published.add(k)
            if not s.strip():
                continue
            if _value_allowed_for_key(k):
                published.add(s)  # deliberately published governance enum value
            else:
                idents.add(s)  # value dropped by the extractor -> identifier
    return idents, published


def _identifiers(row: dict) -> set[str]:
    """Backward-compatible identifier-only view of :func:`_row_terms` (D-01)."""
    return _row_terms(row)[0]


def _enumerate_resource_groups(
    executor, subscriptions: list[str] | None
) -> set[tuple[str, str]]:
    """Page :data:`RESOURCE_GROUP_ENUM_QUERY` to a deduped ``{(sub, rg)}`` set.

    Paged + progress-guarded exactly like the subscription enumeration: the loop
    advances ``$skipToken`` and breaks on ``None``; a repeated continuation token
    aborts with a FIXED, identifier-free error rather than looping forever (P2-b).
    Deduplication is by ``(subscription_id, resource_group)`` (the set itself).
    An EMPTY result is valid (a scope may have no resource groups) -- it is NOT
    an error, unlike the subscription enumeration.
    """
    rgs: set[tuple[str, str]] = set()
    skip_token: str | None = None
    seen_tokens: set[str] = set()
    while True:
        page = executor.run(RESOURCE_GROUP_ENUM_QUERY, subscriptions, skip_token)
        for row in page.rows:
            sub = row.get("subscriptionId")
            name = row.get("name")
            if (
                isinstance(sub, str)
                and sub.strip()
                and isinstance(name, str)
                and name.strip()
            ):
                rgs.add((sub, name))
        next_token = page.skip_token
        if next_token is None:
            break
        if next_token in seen_tokens:
            raise RuntimeError(
                "Azure Resource Graph resource-group enumeration did not progress "
                "(a continuation token repeated); aborting to avoid a loop."
            )
        seen_tokens.add(next_token)
        skip_token = next_token
    return rgs


def _new_conn() -> "duckdb.DuckDBPyConnection":
    """Open a writable in-memory DuckDB and create the scan schema (``_DDL``)."""
    conn = duckdb.connect(":memory:")
    conn.execute(_DDL)
    return conn


def _insert_page(conn: "duckdb.DuckDBPyConnection", rows: list[dict]) -> None:
    """Normalize and bound-INSERT one ARG page's rows into ``resources``.

    Every value is bound via ``executemany`` -- never spliced (project SQL bar).
    A None/empty page is a no-op.
    """
    if rows:
        conn.executemany(_INSERT_RESOURCES, [_normalize(r) for r in rows])


@contextlib.contextmanager
def open_azure(
    executor, subscriptions: list[str] | None
) -> Iterator[tuple[object, set[str]]]:
    """Materialize an ARG scan into in-memory DuckDB + the auto-denylist.

    Pages ``executor.run(ARG_PROJECTION, subscriptions, skip_token)`` exactly once
    per resource page, normalizing+INSERTing each page into one ``:memory:``
    DuckDB ``resources`` table and unioning each row's identifiers into an
    in-memory ``derived`` set. The loop advances ``skip_token`` and breaks ONLY
    when the response ``skip_token`` is ``None`` (D-02/D-03 -- a single full pass
    over the resource set; a non-None first token never truncates the scan).

    After the resource pass a SECOND paged enumeration
    (:func:`_enumerate_resource_groups`, ``ResourceContainers``) lists every
    resource group in scope so EMPTY RGs are counted too (P2-a). The
    ``subscriptions`` table is seeded from the RESOLVED scope (so empty
    subscriptions count); the ``resource_groups`` table is the enumerated set
    unioned with resource-borne RGs, deduped by ``(subscription_id, name)``.

    Yields ``(DuckDBReader(conn), derived)`` -- the EXISTING reader over the
    materialized snapshot (so all 17 aggregation methods run verbatim) plus the
    auto-derived denylist term set. The connection is closed in ``finally``;
    nothing is ever written to disk (the ``:memory:`` DuckDB IS the spool, D-11).
    """
    # Imported lazily and locally: azure-free at module import time (D-08).
    from tenantless.analyzer.reader import DuckDBReader

    conn = _new_conn()
    derived: set[str] = set()
    published: set[str] = set()
    try:
        skip_token: str | None = None
        seen_tokens: set[str] = set()  # P2-b: non-progress / cycle guard
        while True:  # D-02: single pass
            page = executor.run(ARG_PROJECTION, subscriptions, skip_token)
            _insert_page(conn, page.rows)
            for r in page.rows:
                idents, pub = _row_terms(r)  # D-11: per-page, in-memory only
                derived |= idents
                published |= pub
            next_token = page.skip_token
            if next_token is None:  # exhausted (D-03)
                break
            # P2-b: ARG must hand back a NEW continuation token each page. A
            # repeated/cyclic token means no forward progress -- abort instead of
            # re-ingesting the same page forever (unbounded memory). Identifier-free.
            if next_token in seen_tokens:
                raise RuntimeError(
                    "Azure Resource Graph paging did not progress (a continuation "
                    "token repeated); aborting to avoid an unbounded ingest loop."
                )
            seen_tokens.add(next_token)
            skip_token = next_token

        # P2-a (empty RGs): enumerate EVERY resource group in scope via
        # ResourceContainers (a second paged pass) so an RG holding zero resources
        # is still counted -- giving total_resource_groups the same meaning as the
        # DuckDB scan. Each RG name is a tenant identifier -> denylist backstop.
        enumerated_rgs = _enumerate_resource_groups(executor, subscriptions)
        for _sub, rg_name in enumerated_rgs:
            derived.add(rg_name)

        # P1-c: a deliberately-published governance enum value (Environment=prod)
        # must never sit in the denylist -- otherwise the unchanged scan_denylist
        # backstop trips on that legitimate output when the same token is also a
        # real RG/resource NAME elsewhere. Subtract the published set (after the
        # enumerated RG names are folded in, so an RG literally named like an enum
        # value is covered too).
        derived -= published

        # P2-a: count subscriptions from the RESOLVED scan scope, not only from
        # the resources rows -- a resolved subscription that holds ZERO resources
        # must still be counted (it vanished when the table was built purely from
        # DISTINCT resources.subscription_id). Union with any subscription_id seen
        # in resources (defensive: a resource outside the resolved list still counts).
        if subscriptions:
            conn.executemany(
                "INSERT INTO subscriptions VALUES (?)",
                [(s,) for s in dict.fromkeys(subscriptions)],
            )
            conn.execute(
                "INSERT INTO subscriptions "
                "SELECT DISTINCT subscription_id FROM resources "
                "WHERE subscription_id IS NOT NULL "
                "AND subscription_id NOT IN (SELECT subscription_id FROM subscriptions)"
            )
        else:
            conn.execute(
                "INSERT INTO subscriptions "
                "SELECT DISTINCT subscription_id FROM resources "
                "WHERE subscription_id IS NOT NULL"
            )

        # resource_groups = enumerated RGs UNION any RG seen in resources, deduped
        # by (subscription_id, resource_group). Insert the enumerated set first,
        # then supplement with resource-borne RGs NOT already present. Dedup is
        # CASE-INSENSITIVE on the name to absorb ARG's resourceGroup-vs-container
        # casing quirk (mirrors the cost join's lower()), so a resource-bearing RG
        # is never double-counted against its enumerated twin.
        if enumerated_rgs:
            conn.executemany(
                "INSERT INTO resource_groups VALUES (?, ?)", sorted(enumerated_rgs)
            )
            conn.execute(
                "INSERT INTO resource_groups "
                "SELECT DISTINCT r.subscription_id, r.resource_group FROM resources r "
                "WHERE r.resource_group IS NOT NULL AND r.subscription_id IS NOT NULL "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM resource_groups g "
                "  WHERE g.subscription_id = r.subscription_id "
                "  AND lower(g.name) = lower(r.resource_group)"
                ")"
            )
        else:
            # No enumeration result (e.g. a source that cannot answer
            # ResourceContainers) -> fall back to resources-derived RGs (at least
            # as complete as before the empty-RG fix).
            conn.execute(
                "INSERT INTO resource_groups "
                "SELECT DISTINCT subscription_id, resource_group FROM resources "
                "WHERE resource_group IS NOT NULL"
            )
        yield DuckDBReader(conn), derived
    finally:
        conn.close()
