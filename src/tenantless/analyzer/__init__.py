"""Seed Analyzer.

Layered statistical-profile extraction package.

Layering (LOCKED, see 01.1-CONTEXT.md):
- ``reader``        DuckDB-specific seam. The ONLY module that imports ``duckdb``.
                    An alternate scan reader can be slotted in here without
                    touching the layers below.
- ``extractors``    Source-agnostic. Operate ONLY on Polars DataFrames / row
                    iterables produced by the reader.
- ``privacy``       Source-agnostic. Denylist scan + minimum-aggregation bucket
                    merging. Enforces the zero-real-identifier data boundary.
- ``schema_validate`` jsonschema validation against ``profiles/schema.json``.
- ``profile``       Assembles a full schema-valid profile dict and writes it.
"""
