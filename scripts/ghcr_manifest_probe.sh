#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Robust GHCR manifest existence probe (W3-round2 #3 — the preflight must FAIL
# CLOSED). Distinguishes an EXPLICIT registry 404 (the only "absent" signal) from
# every other outcome — auth error, timeout, rate-limit, GHCR 5xx — which must NEVER
# be read as "absent" and so allow an existing tag to be overwritten.
#
# Prints exactly one line on success:
#   ABSENT                      the registry returned an explicit 404
#   PRESENT <sha256:...>        the manifest exists; its content digest follows
# and exits 0. On any ambiguous/error response it exits non-zero AFTER bounded
# retries — the caller must treat a non-zero exit as "unknown", never as absent.
#
# Usage:  ghcr_manifest_probe.sh <repo-path> <tag-or-digest>
#   repo-path e.g. code30-oss/tenantless-mock-server
# Env:  GHCR_USER, GHCR_TOKEN  (a GHCR pull-capable credential, e.g. GITHUB_TOKEN)
# ---------------------------------------------------------------------------
set -euo pipefail

NAME=${1:?repo path (e.g. code30-oss/tenantless-mock-server)}
REF=${2:?tag or digest}
: "${GHCR_USER:?set GHCR_USER}"
: "${GHCR_TOKEN:?set GHCR_TOKEN}"

ACCEPTS=(
  -H 'Accept: application/vnd.oci.image.index.v1+json'
  -H 'Accept: application/vnd.docker.distribution.manifest.list.v2+json'
  -H 'Accept: application/vnd.docker.distribution.manifest.v2+json'
  -H 'Accept: application/vnd.oci.image.manifest.v1+json'
)

MAX=4
attempt=0
while :; do
  attempt=$((attempt + 1))
  basic=$(printf '%s:%s' "$GHCR_USER" "$GHCR_TOKEN" | base64 | tr -d '\n')
  token=$(curl -fsS --max-time 20 -H "Authorization: Basic $basic" \
    "https://ghcr.io/token?service=ghcr.io&scope=repository:${NAME}:pull" 2>/dev/null \
    | jq -r '.token // empty' 2>/dev/null || true)

  if [ -n "$token" ]; then
    hdr=$(mktemp)
    # A GET (not HEAD) so the Docker-Content-Digest header is always returned.
    code=$(curl -s --max-time 30 -o /dev/null -D "$hdr" -w '%{http_code}' \
      -X GET -H "Authorization: Bearer $token" "${ACCEPTS[@]}" \
      "https://ghcr.io/v2/${NAME}/manifests/${REF}" 2>/dev/null || echo 000)
    case "$code" in
      200)
        digest=$(grep -i '^docker-content-digest:' "$hdr" | tr -d '\r' | awk '{print $2}')
        rm -f "$hdr"
        if [ -n "$digest" ]; then echo "PRESENT $digest"; exit 0; fi
        # 200 with no digest header is anomalous → ambiguous, retry.
        echo "probe: ${NAME}:${REF} HTTP 200 without a digest header (attempt ${attempt}/${MAX})" >&2
        ;;
      404)
        rm -f "$hdr"; echo "ABSENT"; exit 0 ;;
      *)
        rm -f "$hdr"
        echo "probe: ${NAME}:${REF} HTTP ${code} (attempt ${attempt}/${MAX})" >&2 ;;
    esac
  else
    echo "probe: token fetch failed for ${NAME} (attempt ${attempt}/${MAX})" >&2
  fi

  if [ "$attempt" -ge "$MAX" ]; then
    echo "::error::GHCR probe for ${NAME}:${REF} inconclusive after ${MAX} attempts — refusing to assume absent" >&2
    exit 2
  fi
  sleep $((attempt * 3))
done
