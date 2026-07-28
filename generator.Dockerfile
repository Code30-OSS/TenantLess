# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# Generator image (D-06 / Wave 2). A one-shot Python 3.11 + uv image that bundles
# the `tenantless` package + bundled profiles (incl. demo.json) + sql/, so
# `docker compose --profile demo up` can seed the synthetic estate BEFORE the Rust
# mock-server serves it. The Rust mock-server image cannot generate — generation is
# the Python layer, hence a SEPARATE image.
#
# Reproducibility (D-12): base pinned BY DIGEST (same python:3.11-slim digest the
# Wave-1 canonical builder uses), uv pinned, deps installed with `uv sync --locked`.
# Runs as a NON-ROOT user. No secrets are baked in; DATABASE_URL is read from env.
#
#   python:3.11-slim @ sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93
# ---------------------------------------------------------------------------
FROM python@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93

ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# uv pinned to the version used by the Wave-1 canonical builder; the resolved
# dependency graph is frozen by uv.lock, so the toolchain is reproducible.
RUN pip install --no-cache-dir uv==0.10.8

WORKDIR /app

# 1) Dependency layer (cache-friendly): resolve the LOCKED graph from the manifests
#    only. --no-install-project installs the third-party deps but NOT the project,
#    so this layer re-runs only when pyproject.toml / uv.lock change.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project

# 2) Project inputs. `src/` carries the tenantless package + bundled profiles
#    (importlib.resources reads demo.json/enterprise.json/small.json from here);
#    `sql/` is resolved by writer.ensure_*_schema() at `parents[3]/sql`, which an
#    EDITABLE install keeps at /app/sql (the layout is preserved). LICENSE is
#    required by hatchling (`license-files = ["LICENSE"]`) at project-install time.
COPY LICENSE ./
COPY sql ./sql
COPY src ./src
# The profile JSON-schema (analyzer/schema_validate.py resolves it at
# parents[3]/profiles/schema.json = /app/profiles/schema.json). ONLY the schema is
# copied — the rest of the top-level profiles/ (real-tenant denylists) is kept out
# of the build context by generator.Dockerfile.dockerignore (data boundary).
COPY profiles/schema.json ./profiles/schema.json

# 3) Install the project itself against the locked graph (editable — the default
#    for the workspace root — so writer.py's `Path(__file__).parents[3]/"sql"`
#    resolves to /app/sql and the base schema self-provisions on a bare volume).
RUN uv sync --locked

# 4) Entrypoint: the D-05 non-empty guard + bounded-timeout generate.
COPY docker/generator-entrypoint.sh /usr/local/bin/generator-entrypoint.sh
RUN chmod +x /usr/local/bin/generator-entrypoint.sh

# Non-root runtime (D-12). The venv + source are read-only for this user, which is
# all the guard/generate path needs (it writes only to Postgres, never the FS).
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin tenantless
USER tenantless

# NO args -> guard + generate (compose one-shot). Explicit args (e.g.
# `docker run <img> tenantless --version`) are exec'd verbatim by the entrypoint.
ENTRYPOINT ["/usr/local/bin/generator-entrypoint.sh"]
