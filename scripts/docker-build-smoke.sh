#!/usr/bin/env bash
# docker-build-smoke.sh — UAT Gap 1 build assertion (INFRA-01 / INFRA-03, WEBUI-03).
#
# Reproducibly proves the production container builds from a clean checkout AND the
# resulting binary runs. The multi-stage build must:
#   * build the SPA in-image (npm ci && npm run build) and embed it under /ui,
#   * carry the crate-root build.rs freshness guard, frontend/dist, and workspace sql/
#     so include_dir!/include_str!/the build.rs panic-guard all resolve at COMPILE time,
#   * produce a runnable `tenantless-server` binary (clap `--help` exits 0).
#
# This is a real gate, NOT a skip: if docker is unavailable the script FAILS loudly.
# Build context is the repo root so frontend/, sql/, and Cargo.lock are reachable.
#
# Usage:  bash scripts/docker-build-smoke.sh
set -euo pipefail

# Resolve the repo root from this script's location so the build context is correct
# regardless of the caller's working directory.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
IMAGE_TAG="${IMAGE_TAG:-tenantless-server:smoke}"

echo "== docker build smoke (UAT Gap 1 / INFRA-01) =="
echo "repo root: ${REPO_ROOT}"
echo "image tag: ${IMAGE_TAG}"

# 1. Real gate: docker MUST be available. No silent skip.
if ! command -v docker >/dev/null 2>&1; then
  echo "FAIL: docker is not on PATH — this build assertion requires Docker (start Docker Desktop / install docker)." >&2
  exit 1
fi

# 2. Build the production image (context = repo root).
echo "-- building image (this compiles the SPA + the release binary) --"
docker build -f "${REPO_ROOT}/mock-server/Dockerfile" -t "${IMAGE_TAG}" "${REPO_ROOT}"

# 3. Assert the binary runs: `--help` exits 0 and prints the clap usage banner.
echo "-- asserting the binary runs (--help) --"
help_out="$(docker run --rm "${IMAGE_TAG}" --help)"
echo "${help_out}"
if ! printf '%s' "${help_out}" | grep -Eq 'Usage|tenantless-server'; then
  echo "FAIL: --help output did not contain the clap usage banner." >&2
  exit 1
fi

echo "DOCKER BUILD SMOKE: PASS"
