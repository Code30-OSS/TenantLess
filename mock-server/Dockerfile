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
# Base pinned BY DIGEST (W3-6): a mutable tag would let two builds of the same
# version tag differ, which — combined with tag republish — is how a "reproducible"
# release silently drifts. Refresh these digests deliberately, never implicitly.
#   node:22-bookworm-slim  resolved 2026-07-28
FROM node:22-bookworm-slim@sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3 AS frontend
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
# rust:1-slim-bookworm  resolved 2026-07-28 (W3-6: pin by digest)
FROM rust:1-slim-bookworm@sha256:99e09cb2284e2ddbb73a995deee3e91783fd04d177602ccf6eab326d778ee777 AS builder
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
# Stage 3 — runtime.
#
# SCOPE (known limitation, deliberate): this slim image carries ONLY the compiled
# Rust binary, so it serves the ARM API but CANNOT run the control plane. The
# control-plane jobs (generate / analyze / snapshot / restore) shell out to
# `uv run tenantless …` (the Python layer — see generator.Dockerfile) and to
# `pg_dump`/`pg_restore`, none of which are installed here. Arming control against
# this image (setting the control token) therefore fails when a job spawns its
# child process; run generation via the generator image and snapshots from a host
# that has the Postgres client tools. Keeping this image toolchain-free is what
# keeps the served-API attack surface minimal.
# ---------------------------------------------------------------------------
# debian:bookworm-slim  resolved 2026-07-28 (W3-6: pin by digest)
FROM debian:bookworm-slim@sha256:7b140f374b289a7c2befc338f42ebe6441b7ea838a042bbd5acbfca6ec875818 AS runtime
# ca-certificates for any TLS; curl for the container HEALTHCHECK.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /build/target/release/tenantless-server /usr/local/bin/tenantless-server
# Run as a NON-ROOT user (mirrors generator.Dockerfile's uid 10001). The binary is
# world-executable and binds 8080 (> 1024, so no privileged-port capability is
# needed); serving reads only env + the embedded SPA/SQL and writes nothing to the
# filesystem, so an unprivileged UID is sufficient for the ARM-serving role. A
# `nologin` shell + no home directory keeps the account inert.
RUN useradd --system --uid 10001 --shell /usr/sbin/nologin tenantless
USER tenantless
EXPOSE 8080
# PORT / BASE_URL / DATABASE_URL are read from env (clap `#[arg(env = ...)]`).
ENTRYPOINT ["tenantless-server"]
