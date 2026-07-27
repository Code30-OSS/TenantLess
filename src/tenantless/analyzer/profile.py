"""Profile assembler -- the analyzer's top-level orchestration.

``build_profile`` runs the thinnest genuinely end-to-end slice:

    DuckDB read  ->  resource-type extraction  ->  min-aggregation
                 ->  denylist scan  ->  schema validation  ->  write JSON

As of Plan 03 every schema-required section is derived from REAL data: resource
type frequencies + per-type property/sku shapes (top 15), k-means subscription
archetypes with per-archetype location distributions, RG architecture templates,
tag key frequencies + denylist-safe bucketed values, cross-subscription
dependencies (conservative defaults where the signal is weak), and governance
violations mapped from the findings table. The only remaining defensive
fallbacks are for sources that yield no subscriptions/templates at all.

MANUAL REAL-DB RUN (do NOT run automatically; the real source DB is external
and read-only -- this is a human checkpoint):

    uv run tenantless analyze \
      --source duckdb:/path/to/your-scan.duckdb \
      --out profiles/derived.json \
      --min-bucket-size 5 \
      --denylist profiles/.scan-denylist.json
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import orjson

from . import privacy, review, schema_validate
from .extractors import archetypes as archetypes_extractor
from .extractors import cooccurrence as cooccurrence_extractor
from .extractors import cost as cost_extractor
from .extractors import cross_sub as cross_sub_extractor
from .extractors import naming as naming_extractor
from .extractors import resource_types
from .extractors import rg_templates as rg_templates_extractor
from .extractors import tags as tags_extractor
from .extractors import type_shapes as type_shapes_extractor
from .extractors import violations as violations_extractor
from .reader import open_duckdb

DEFAULT_K = archetypes_extractor.DEFAULT_K


class DenylistRequiredError(RuntimeError):
    """Raised when a real-derived source is profiled with no usable denylist.

    Fail-closed data-boundary guard (SEC-HIGH-1): profiling a real source with
    no denylist (``None``), a missing denylist path, or a denylist that yields
    zero non-empty terms aborts the run BEFORE any profile is written. The only
    sanctioned bypass is the explicit ``allow_no_denylist=True`` escape hatch,
    reserved for sample/test sources whose data carries no real identifiers.
    """

# v1.2: the profile now also carries the optional cost_distributions section
# (COST-01, additive bump). v1.0/v1.1 reference profiles still validate (version
# enum); the generator ignores unknown sections and zero-fills cost when the
# section is absent, so emitting v1.2 is backward-safe for the shared schema.
PROFILE_VERSION = "1.2"


def _utc_now_rfc3339() -> str:
    """Return current UTC time as an RFC3339 / schema date-time string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _extracted_by() -> str:
    """Tool + version string recorded in provenance (never a host/secret)."""
    from tenantless import __version__

    return f"tenantless/{__version__}"


def _coverage_verdict(section: Any) -> str:
    """Map a derived optional section to its provenance coverage verdict (D-03).

    A non-empty section cleared ``--min-bucket-size`` -> ``derived``; an empty one
    was skipped below threshold -> ``insufficient_coverage``. This is the explicit,
    auditable record of a skip (never a silent drop). ``api_version`` is recorded
    separately as ``no_source`` (the source scan never captures it).
    """
    return "derived" if section else "insufficient_coverage"


def _load_denylist(denylist: str | Path | None) -> list[str]:
    """Load NON-EMPTY denylist terms from a JSON file (list[str] or {"terms": [...]}).

    Returns ``[]`` when the denylist is ``None``, the path is missing, or the file
    yields no non-empty terms. A purely whitespace term contributes nothing, so a
    denylist of ``["", "   "]`` reads as empty (no leak protection) — the caller
    (:func:`build_profile`) treats an empty result as "no usable denylist" and
    fails closed unless the explicit escape hatch is set.
    """
    if denylist is None:
        return []
    path = Path(denylist)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        data = data.get("terms", [])
    return [str(t) for t in data if str(t).strip()]


def _parse_source(source: str) -> tuple[str, str]:
    """Parse a source string into a ``(scheme, target)`` pair.

    Recognized schemes:

    - ``duckdb:<path>``      -> ``("duckdb", <path>)``  (read via :func:`open_duckdb`)
    - ``azure:[<subId,...>]`` -> ``("azure", <sub-filter csv>)``  (direct ARG scan)

    The ``azure:`` target is the optional comma-separated subscription filter
    (an empty target means "the enumerated default scope"); the live ARG path is
    wired in :func:`build_profile` (lazy, optional-extra-guarded).

    A bare value (no recognized scheme) is treated as a DuckDB path for
    convenience. The live database-scan reader was dropped from this build; a
    profile is fitted from a DuckDB/file source (the direct live-tenant scan is
    a separate, optional source added in a later phase).
    """
    if source.startswith("duckdb:"):
        return ("duckdb", source[len("duckdb:"):])
    if source.startswith("azure:"):
        return ("azure", source[len("azure:"):])
    if (
        source.startswith("postgres:")
        or source.startswith("postgresql:")
    ):
        raise ValueError(
            "the live database-scan source was removed; use a DuckDB/file "
            "profile source (e.g. duckdb:<path>)"
        )
    return ("duckdb", source)


@contextlib.contextmanager
def _duckdb_reader_ctx(target: str) -> Iterator[tuple[Any, set[str]]]:
    """Adapt :func:`open_duckdb` to the ``(reader, derived_terms)`` seam contract.

    A DuckDB/file source derives NO auto-denylist of its own (the denylist for a
    duckdb scan comes solely from ``--denylist``), so the derived-term set is
    always empty -- this keeps the duckdb fail-closed behavior byte-identical
    after the gate moves below the file∪derived union.
    """
    with open_duckdb(target) as reader:
        yield reader, set()


def _placeholder_rg_template() -> dict[str, Any]:
    """A single schema-valid resource-group template placeholder."""
    return {
        "id": "placeholder",
        "weight": 1.0,
        "type_set": ["placeholder"],
        "resource_count": {"mean": 0.0, "std": 0.0, "min": 0.0, "max": 0.0},
    }


def assemble_profile(
    source_stats: dict[str, int],
    resource_type_distributions: dict[str, dict],
    subscription_archetypes: list[dict[str, Any]],
    resource_group_templates: list[dict[str, Any]] | None = None,
    tag_distributions: dict[str, Any] | None = None,
    cross_subscription_dependencies: dict[str, Any] | None = None,
    governance_violations_type_frequencies: dict[str, float] | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble a full schema-valid profile dict from real extracted parts.

    Real (Plan 01): ``source_stats``, ``resource_type_distributions`` frequencies.
    Real (Plan 02): ``subscription_archetypes`` (k-means, with per-archetype
    ``location_distribution``) and ``resource_group_templates`` (top 30 +
    ``__misc__``).
    Real (Plan 03): per-type property/sku shapes (already merged into
    ``resource_type_distributions``), ``tag_distributions``,
    ``cross_subscription_dependencies``, and ``governance_violations``.

    Any section passed as ``None`` falls back to a schema-valid default so the
    assembler stays robust for sources with no signal.
    """
    if not subscription_archetypes:
        subscription_archetypes = [_placeholder_archetype()]
    if not resource_group_templates:
        resource_group_templates = [_placeholder_rg_template()]
    if tag_distributions is None:
        tag_distributions = {"key_frequencies": {}, "value_distributions": {}}
    if cross_subscription_dependencies is None:
        cross_subscription_dependencies = cross_sub_extractor.extract(None)
    if governance_violations_type_frequencies is None:
        governance_violations_type_frequencies = {}
    profile: dict[str, Any] = {
        "version": PROFILE_VERSION,
        "extracted_at": _utc_now_rfc3339(),
        "source_stats": source_stats,
        "subscription_archetypes": subscription_archetypes,
        "resource_group_templates": resource_group_templates,
        "resource_type_distributions": resource_type_distributions,
        "tag_distributions": tag_distributions,
        "cross_subscription_dependencies": cross_subscription_dependencies,
        "governance_violations": {
            "type_frequencies": governance_violations_type_frequencies
        },
    }
    # ANLZ-11 (D-03/D-04): the provenance block is OPTIONAL in the schema, so it
    # is attached only when build_profile supplies it (callers that assemble a
    # bare profile -- e.g. fixtures -- still produce a valid v1.1 profile).
    if provenance is not None:
        profile["provenance"] = provenance
    return profile


def _placeholder_archetype() -> dict[str, Any]:
    """A single schema-valid subscription archetype placeholder.

    Used only as a defensive fallback when the source yields no subscriptions;
    real k-means archetypes come from ``archetypes_extractor.extract``.
    """
    return {
        "id": "placeholder",
        "weight": 1.0,
        "resource_group_count": {"mean": 0.0, "std": 0.0, "min": 0.0, "max": 0.0},
        "resource_count": {"mean": 0.0, "std": 0.0, "min": 0.0, "max": 0.0},
        "location_distribution": {"__other__": 1.0},
        "tag_density": {"mean": 0.0, "std": 0.0},
    }


def build_profile(
    source: str,
    out: str | Path,
    min_bucket_size: int = 5,
    denylist: str | Path | None = None,
    k: int | None = None,
    allow_no_denylist: bool = False,
    _executor: Any = None,
) -> dict[str, Any]:
    """End-to-end: read source, extract, privacy-filter, validate, write.

    Parameters
    ----------
    source:
        ``duckdb:<path>`` (or a bare DuckDB path) to fit a profile from a
        DuckDB/file scan.
    out:
        Output JSON path (e.g. ``profiles/derived.json``).
    min_bucket_size:
        Buckets observed fewer than this many times are dropped before
        normalization (privacy min-aggregation).
    denylist:
        Optional path to a JSON denylist of real identifiers; when provided the
        assembled profile is scanned and the run fails loudly on any leak.
    k:
        Number of subscription archetypes for k-means. ``None`` uses the default
        (:data:`DEFAULT_K`). The value is honored EXACTLY -- passing ``--k N``
        produces exactly ``N`` archetypes (when ``N <= number of subscriptions``).
    allow_no_denylist:
        Escape hatch (SEC-HIGH-1, default ``False``). When ``False`` and the
        loaded denylist yields zero non-empty terms (``denylist`` is ``None``, the
        path is missing, or the file holds no real terms), the run aborts with
        :class:`DenylistRequiredError` BEFORE anything is written — fail-closed for
        real-derived sources. Set ``True`` ONLY for a sample/test source whose data
        carries no real identifiers; NEVER for a real scan.

    Returns the assembled profile dict (also written to ``out``).
    """
    # SEC-HIGH-1 fail-closed gate (RELOCATED below the reader seam, D-05/D-12):
    # the file denylist is loaded here, but the gate now fires AFTER these terms are
    # unioned with the reader's auto-derived terms. The azure path derives a
    # non-empty denylist from the tenant's OWN identifiers (open_azure), so that
    # union is the authoritative backstop; the duckdb path derives no terms, so its
    # fail-closed behavior is byte-preserved.
    file_terms = _load_denylist(denylist)

    scheme, target = _parse_source(source)
    # The reader seam: dispatch on scheme. Both branches yield a
    # ``(reader, derived_terms)`` pair so EVERYTHING from ``source_stats()`` onward
    # stays reader-agnostic and byte-identical (D-10).
    if scheme == "azure":
        # Lazy, branch-LOCAL imports keep the core install azure-free (D-08):
        # neither module pulls an ``azure-*`` package at import time; the optional
        # extra is guarded at call time inside ``make_arg_executor``.
        from .azure.arg_client import make_arg_executor, resolve_subscriptions
        from .azure.materialize import open_azure

        subs_filter = [s for s in target.split(",") if s.strip()] or None
        executor = _executor if _executor is not None else make_arg_executor()
        # A2 RESOLVED (D-05): an explicit ``azure:<subId,...>`` filter is honored
        # verbatim; an empty filter enumerates the default scope through the SAME
        # executor BEFORE the resource scan (arg order: subscription_filter, executor).
        resolved = resolve_subscriptions(subs_filter, executor)
        reader_ctx = open_azure(executor, resolved)
    else:
        reader_ctx = _duckdb_reader_ctx(target)

    with reader_ctx as (reader, derived_terms):
        # Union the file denylist with the reader's auto-derived terms, THEN apply
        # the fail-closed gate -- still before any extraction or write.
        terms = sorted(set(file_terms) | derived_terms)
        if not terms and not allow_no_denylist:
            raise DenylistRequiredError(
                "refusing to profile a real-derived source without a denylist: "
                "pass --denylist <file> with real-identifier terms, or "
                "--allow-no-denylist ONLY for a sample/test source with no real data."
            )

        source_stats = reader.source_stats()
        type_counts = reader.resource_type_counts()
        sub_features = reader.subscription_features()
        rg_sets = reader.rg_type_sets()

        # Privacy: drop low-count buckets BEFORE normalization.
        surviving = privacy.merge_min_buckets(type_counts, min_bucket_size)

        # Extract: normalized frequencies over surviving buckets.
        rtd = resource_types.extract(surviving)

        # Plan 03: attach per-type property/sku shapes for the top 15 types.
        # The reader handle is borrowed via the per-type frame callables so the
        # extractor itself stays source-agnostic.
        # Plan 06-03 (ANLZ-05): kind derives from properties->>'kind' via the new
        # reader callable; sku already reads properties->'sku' (Plan 02).
        # api-version is NOT read (no source) -- recorded no_source by Plan 04.
        type_shapes_extractor.extract_into(
            rtd,
            property_frame_for=reader.type_property_value_counts,
            sku_frame_for=reader.type_sku_value_counts,
            kind_frame_for=reader.type_kind_counts,
            min_bucket_size=min_bucket_size,
        )

        # Plan 03: tag key frequencies + denylist-safe, min-bucketed values.
        tag_key_counts = reader.tag_key_counts()
        tag_value_counts = reader.tag_value_counts()
        total_resources = source_stats["total_resources"]
        tag_distributions = tags_extractor.extract(
            tag_key_counts,
            tag_value_counts,
            total_resources,
            min_bucket_size=min_bucket_size,
        )

        # Plan 09-02 (COST-01): per-resource monthly-cost samples for lognormal
        # fitting. Only (type, monthly_cost) crosses the reader seam -- identifiers
        # stay inside the SQL GROUP BY. An empty frame (no resource_costs table)
        # records `no_source` below and attaches no cost section (D-02 back-compat).
        cost_samples = reader.resource_cost_samples()
        cost_source_empty = cost_samples.is_empty()

        # Plan 03: cross-subscription dependency signals (finite, defaulted).
        xsub_signal = reader.cross_subscription_reference_counts()

        # Plan 03: governance violations from the real findings table.
        finding_counts = reader.finding_type_counts()

        # Plan 06-03 (ANLZ-04): resource-type co-occurrence within an RG. Derived
        # only when pairs clear --min-bucket-size; below threshold -> {} (the
        # optional section is simply omitted, never a failure, D-03).
        type_cooccurrence = cooccurrence_extractor.extract_from_pairs(
            reader.rg_type_pairs(), min_bucket_size=min_bucket_size
        )

        # Plan 06-03 (ANLZ-07): tag-key co-occurrence + value cardinality +
        # untagged-rate-by-type, all min-bucket gated (D-03 hybrid coverage).
        tag_key_cooccurrence = cooccurrence_extractor.tag_key_cooccurrence(
            reader.tag_key_pair_counts(), min_bucket_size=min_bucket_size
        )
        tag_value_cardinality = cooccurrence_extractor.tag_value_cardinality(
            tag_value_counts, min_bucket_size=min_bucket_size
        )
        untagged_rate_by_type = cooccurrence_extractor.untagged_rate_by_type(
            reader.type_tag_coverage()
        )

        # Plan 06-03 (ANLZ-08): privacy-first tokenized naming conventions. The
        # extractor emits ONLY structural class patterns + positional class
        # distributions -- never a verbatim name. Min-bucket gated; below
        # threshold the optional section is omitted (D-03), never a failure.
        naming_conventions = naming_extractor.extract(
            reader.resource_name_samples(), min_bucket_size=min_bucket_size
        )

    # k-means subscription archetypes (per-archetype location_distribution is
    # privacy-min-bucket-merged inside the extractor). --k is honored exactly.
    archetypes = archetypes_extractor.extract(
        sub_features, k=k, min_bucket_size=min_bucket_size
    )

    # RG architecture templates: top 30 + __misc__ (sub-threshold + long tail).
    # type_set casing matches resource_type_distributions keys via normalize_type_key.
    rg_templates = rg_templates_extractor.extract(
        rg_sets, min_bucket_size=min_bucket_size
    )

    # Cross-sub dependencies: real signal where present, conservative defaults
    # (mirroring test-small.json) where absent; every stat is finite.
    cross_subscription_dependencies = cross_sub_extractor.extract(xsub_signal)

    # Governance violations: finding_type -> VIOL_* vocabulary, normalized rates.
    governance_type_frequencies = violations_extractor.extract(
        finding_counts, total_resources, min_bucket_size=min_bucket_size
    )

    # Plan 09-02 (COST-01): fit per-type lognormal monthly-cost distributions.
    # The min-bucket privacy floor drops any type with < min_bucket_size samples
    # BEFORE fitting, so only aggregated per-type numeric params (mu/sigma/n) keyed
    # by a canonical Microsoft.* type ever cross the boundary (COST-05 leak test).
    cost_distributions = cost_extractor.extract_cost_distributions(
        cost_samples, min_bucket_size=min_bucket_size
    )

    # Plan 06-03 (ANLZ-07): fold the optional tag extras into tag_distributions
    # only when non-empty (D-03: a sparse section is omitted, never empty-stubbed).
    if tag_key_cooccurrence:
        tag_distributions["key_cooccurrence"] = tag_key_cooccurrence
    if tag_value_cardinality:
        tag_distributions["value_cardinality"] = tag_value_cardinality
    if untagged_rate_by_type:
        tag_distributions["untagged_rate_by_type"] = untagged_rate_by_type

    naming_present = bool(
        naming_conventions.get("pattern_frequencies")
        or naming_conventions.get("position_token_classes")
    )

    # ANLZ-11 (D-03/D-04): record an EXPLICIT, auditable coverage verdict for every
    # pending-extractor decision instead of silently omitting a section. `derived`
    # means the section cleared --min-bucket-size; `insufficient_coverage` means it
    # was skipped below threshold (D-03); `api_version` is `no_source` because
    # the source scan never captures it (ANLZ-05 -- the skip is recorded, not
    # dropped). The `source` is the SCHEME ONLY (never the raw URI/password).
    coverage: dict[str, str] = {
        # ANLZ-04
        "resource_type_cooccurrence": _coverage_verdict(type_cooccurrence),
        # ANLZ-07
        "tag_key_cooccurrence": _coverage_verdict(tag_key_cooccurrence),
        "tag_value_cardinality": _coverage_verdict(tag_value_cardinality),
        "untagged_rate_by_type": _coverage_verdict(untagged_rate_by_type),
        # ANLZ-08
        "naming_conventions": _coverage_verdict(naming_present),
        # ANLZ-05: api-version is never captured by the source scan -> no_source.
        "api_version": "no_source",
        # COST-01: `derived` when fitted, `insufficient_coverage` when the source
        # carried cost rows but none cleared the min-bucket floor, `no_source` when
        # the source has no resource_costs table at all (the explicit skip record).
        "cost_distributions": (
            "no_source"
            if cost_source_empty
            else _coverage_verdict(cost_distributions)
        ),
    }
    provenance = {
        "reviewed": False,  # D-04: unreviewed until a human signs off.
        "coverage": coverage,
        "source": scheme,  # scheme only -- NEVER the URI/password (T-06-15).
        "extracted_by": _extracted_by(),
    }

    profile = assemble_profile(
        source_stats,
        rtd,
        archetypes,
        rg_templates,
        tag_distributions=tag_distributions,
        cross_subscription_dependencies=cross_subscription_dependencies,
        governance_violations_type_frequencies=governance_type_frequencies,
        provenance=provenance,
    )

    # Plan 06-03 (ANLZ-04): attach the optional co-occurrence matrix when derived.
    if type_cooccurrence:
        profile["resource_type_cooccurrence"] = type_cooccurrence

    # Plan 06-03 (ANLZ-08): attach tokenized naming conventions when any
    # structural pattern survived the min-bucket floor (D-03 skip otherwise).
    if naming_present:
        profile["naming_conventions"] = naming_conventions

    # Plan 09-02 (COST-01): attach the fitted cost distributions only when at
    # least one type cleared the min-bucket floor (a cost-less / sub-threshold
    # source omits the optional section, never empty-stubs it -- D-02 back-compat).
    if cost_distributions:
        profile["cost_distributions"] = cost_distributions

    # Schema gate (raises on any violation incl. stray keys / bad date-time).
    schema_validate.validate_profile(profile)

    # Privacy gate: loud failure if any real identifier leaked. `terms` was loaded
    # (and the fail-closed gate enforced) at the top of build_profile.
    if terms:
        privacy.scan_denylist(profile, terms)

    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(orjson.dumps(profile, option=orjson.OPT_INDENT_2))

    # ANLZ-10 (D-04): always write the <out>_review.txt companion after the
    # profile is assembled + privacy-cleared. Report-only -- never blocks; the CLI
    # decides whether to ALSO echo it to stdout (interactive mode).
    review.write_review(profile, out)

    return profile
