"""Source-agnostic extractors.

Each extractor consumes a pre-aggregated Polars DataFrame (or row iterable)
and returns a fragment of the statistical profile. Extractors MUST NOT import
``duckdb`` or any reader-specific type -- they operate purely on Polars frames
so that Phase 6 can swap the reader for ConnectorX/Postgres unchanged.
"""

from .cost import extract_cost_distributions

__all__ = ["extract_cost_distributions"]
