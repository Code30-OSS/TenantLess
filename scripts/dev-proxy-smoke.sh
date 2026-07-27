#!/usr/bin/env bash
# dev-proxy-smoke.sh — WEBUI-02 / D-05 reachability check (semi-automated).
#
# Proves the Vite dev-server proxy forwards an API call to the running axum server SAME-ORIGIN:
# a request to the Vite origin (:5173) for /_sim/summary must be answered by axum (:8080) with
# HTTP 200 — i.e. the browser never leaves localhost:5173 and axum needs no CORS.
#
# This is intentionally a documented, semi-automated check (not a CI gate): it needs both a running
# Postgres-backed axum server and the Vite dev server. Run it locally while iterating on the UI.
#
# Usage:
#   1. Start the server:   tenantless serve            # (or cargo run -p mock-server) → :8080
#   2. Start the UI:       (cd frontend && npm run dev) # → :5173
#   3. Run this script:    scripts/dev-proxy-smoke.sh
set -euo pipefail

VITE_ORIGIN="${VITE_ORIGIN:-http://localhost:5173}"
AXUM_ORIGIN="${AXUM_ORIGIN:-http://127.0.0.1:8080}"

echo "== dev-proxy smoke (WEBUI-02) =="
echo "Vite origin: ${VITE_ORIGIN}   axum origin: ${AXUM_ORIGIN}"

# 1. axum must be up directly (control).
direct=$(curl -s -o /dev/null -w '%{http_code}' "${AXUM_ORIGIN}/_sim/summary" || echo "000")
echo "direct  axum  /_sim/summary -> ${direct}"
if [ "${direct}" != "200" ]; then
  echo "FAIL: axum server is not answering /_sim/summary on ${AXUM_ORIGIN} (start it first)." >&2
  exit 1
fi

# 2. The proxied call through the Vite origin must ALSO be 200 — same-origin, no CORS.
proxied=$(curl -s -o /dev/null -w '%{http_code}' "${VITE_ORIGIN}/_sim/summary" || echo "000")
echo "proxied vite  /_sim/summary -> ${proxied}"
if [ "${proxied}" != "200" ]; then
  echo "FAIL: Vite proxy did not forward /_sim/summary (is 'npm run dev' running on ${VITE_ORIGIN}?)." >&2
  exit 1
fi

echo "PASS: Vite dev proxy reaches axum same-origin (D-05, no CORS needed)."
