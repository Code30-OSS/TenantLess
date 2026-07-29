#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Bring up the demo stack from a generator image + a mock-server image, seed the
# seed-42 estate, and run the FULL estate assertion (identity + counts + served
# planes) against it. Shared by BOTH release.yml gates so the bytes tested on a PR
# and the bytes tested before publish exercise the identical stack (W3-2).
#
# Inputs (env):
#   GENERATOR_IMAGE  generator image ref (a local tag OR a ghcr @sha256 digest)
#   SERVER_IMAGE     mock-server image ref (a local tag OR a ghcr @sha256 digest)
#
# Postgres is pinned BY DIGEST (W3-6). Everything is loopback-bound and torn down on
# exit. Exit 0 = the served estate matched the seed-42 baseline; non-zero = a build,
# boot, or assertion failure.
# ---------------------------------------------------------------------------
set -euo pipefail

: "${GENERATOR_IMAGE:?set GENERATOR_IMAGE}"
: "${SERVER_IMAGE:?set SERVER_IMAGE}"

PGIMG='postgres:16-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777'
NET="tl-e2e-$$"
PG="tl-e2e-pg-$$"
SRV="tl-e2e-server-$$"
PG_HOST_PORT=55432
API_HOST_PORT=18080

DB_INTERNAL="postgres://tenantless:tenantless_dev@${PG}:5432/tenantless"
DB_HOST="postgres://tenantless:tenantless_dev@127.0.0.1:${PG_HOST_PORT}/tenantless"
API_BASE="http://127.0.0.1:${API_HOST_PORT}"

cleanup() {
  docker rm -f "$SRV" "$PG" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

echo "== e2e: network + Postgres (pinned) =="
docker network create "$NET" >/dev/null
docker run -d --name "$PG" --network "$NET" \
  -e POSTGRES_USER=tenantless -e POSTGRES_PASSWORD=tenantless_dev -e POSTGRES_DB=tenantless \
  -p "127.0.0.1:${PG_HOST_PORT}:5432" \
  -v "$PWD/sql:/docker-entrypoint-initdb.d:ro" \
  "$PGIMG" >/dev/null
for i in $(seq 1 30); do
  docker exec "$PG" pg_isready -U tenantless >/dev/null 2>&1 && break
  [ "$i" = "30" ] && { echo "::error::postgres never became ready"; docker logs "$PG"; exit 1; }
  sleep 2
done

echo "== e2e: seed the seed-42 demo estate (generator image) =="
# One-shot; the entrypoint's D-05 guard generates on the empty volume. A non-zero
# exit fails BEFORE any assertion (a silent green-but-empty is impossible).
docker run --rm --network "$NET" -e DATABASE_URL="$DB_INTERNAL" "$GENERATOR_IMAGE"

echo "== e2e: start the mock-server (serving the seeded estate) =="
docker run -d --name "$SRV" --network "$NET" \
  -e DATABASE_URL="$DB_INTERNAL" \
  -e HOST=0.0.0.0 -e PORT=8080 -e BASE_URL=http://localhost:8080 \
  -p "127.0.0.1:${API_HOST_PORT}:8080" \
  "$SERVER_IMAGE" >/dev/null
for i in $(seq 1 30); do
  curl -fsS -H "Authorization: Bearer health" "${API_BASE}/subscriptions" >/dev/null 2>&1 && break
  [ "$i" = "30" ] && { echo "::error::mock-server never served a 200"; docker logs "$SRV"; exit 1; }
  sleep 2
done

echo "== e2e: assert the served estate (identity + counts + planes) =="
DATABASE_URL="$DB_HOST" ASSERT_API_BASE="$API_BASE" uv run python scripts/assert_demo_estate.py
