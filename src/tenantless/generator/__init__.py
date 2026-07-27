"""Synthetic Tenant Generator (inverse of the analyzer).

Layering (mirror of analyzer/__init__.py, LOCKED — see 02-CONTEXT.md):
- ``writer``    Postgres-specific seam. The ONLY module that imports ``psycopg``.
                Mirror-image of the analyzer's ``reader`` (the only duckdb seam).
- ``rng``       The single seeded source of randomness (numpy Generator(PCG64)
                + Faker.seed_instance). EVERY other module draws from an injected
                ``SeededContext`` — no bare ``random`` / ``np.random`` / ``Faker()``
                anywhere else (D-03). Determinism is provable the same way the
                analyzer's is (fixed seed + sorted keys before any draw).
- ``sampling``/``naming``
                Source-agnostic samplers. Operate on the profile dict + the
                injected RNG — mirror-image of extractors operating on Polars
                frames. They sort every probability vector before sampling so the
                seed→outcome mapping is stable (D-01).
- ``profile_input``
                Loads + jsonschema-validates the INPUT profile before sampling
                (V5 input validation) and defaults targets from ``source_stats``.
- ``pipeline``  Assembles a full synthetic tenant (DB-free) and hands rows to
                ``writer`` (inverse of analyzer ``profile.build_profile``).
"""
