# syntax=docker/dockerfile:1
# Multi-stage build for the tenantless-server ARM mock server (INFRA-01 / INFRA-03).
# The crate uses runtime sqlx queries (no compile-time `query!` macros), so the build
# needs no DATABASE_URL and no live database.
#
# Three stages:
#   1. frontend — build the React/Vite SPA (npm ci && npm run build) => /fe/dist
#   2. builder  — compile the release binary; the SPA is embedded at COMPILE time via
#      src/ui.rs `include_dir!("$CARGO_MANIFEST_DIR/../frontend/dist")` and the crate-root
#      build.rs freshness guard, and the runtime SQL via lib.rs `include_str!("../../sql/...")`.
#   3. runtime  — slim image carrying only the binary.

# ---------------------------------------------------------------------------
# Stage 1 — frontend SPA build (WEBUI-03). Lockfile-exact install (D-07 / T-15-01-SC):
# `npm ci` honors the committed package-lock.json + `.npmrc` save-exact — never `npm install`.
# ---------------------------------------------------------------------------
FROM node:22-bookworm-slim AS frontend
WORKDIR /fe
# Install deps first (layer cache) — only re-runs when the lockfile/manifest changes.
COPY frontend/package.json frontend/package-lock.json frontend/.npmrc ./
RUN npm ci
# Then the SPA source; `npm run build` = `tsc -b && vite build` => /fe/dist.
COPY frontend/ ./
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2 — rust release build. Copies the three COMPILE-time inputs the release
# needs beyond src/: the crate-root build.rs, the built SPA (from stage 1), and the
# workspace-root sql/ (005/006/007 are include_str!'d by lib.rs).
# ---------------------------------------------------------------------------
FROM rust:1-slim-bookworm AS builder
WORKDIR /build
# Workspace manifests + lockfile first (reproducible deps), then the crate inputs.
COPY Cargo.toml Cargo.lock ./
COPY mock-server/Cargo.toml mock-server/Cargo.toml
COPY mock-server/build.rs mock-server/build.rs
COPY mock-server/src mock-server/src
# Workspace-root SQL for the include_str! at lib.rs:192,213,231.
COPY sql sql
# The built SPA — lands at /build/frontend/dist = `../frontend/dist` from the crate
# manifest, satisfying both the build.rs `dist/index.html` guard and ui.rs include_dir!.
COPY --from=frontend /fe/dist frontend/dist
RUN cargo build --release -p tenantless-server --bin tenantless-server

# ---------------------------------------------------------------------------
# Stage 3 — runtime (unchanged).
# ---------------------------------------------------------------------------
FROM debian:bookworm-slim AS runtime
# ca-certificates for any TLS; curl for the container HEALTHCHECK.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /build/target/release/tenantless-server /usr/local/bin/tenantless-server
EXPOSE 8080
# PORT / BASE_URL / DATABASE_URL are read from env (clap `#[arg(env = ...)]`).
ENTRYPOINT ["tenantless-server"]
