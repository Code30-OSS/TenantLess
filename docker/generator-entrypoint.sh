#!/bin/sh
# ---------------------------------------------------------------------------
# Generator one-shot entrypoint (D-04 / D-05 / D-12).
#
# Compose runs this with NO arguments: it waits for Postgres, then decides
# GENERATE-vs-SKIP by the D-05 non-empty guard and NEVER truncates a populated
# volume. Explicit arguments (e.g. `docker run <img> tenantless --version`) are
# exec'd verbatim and skip the guard entirely.
# ---------------------------------------------------------------------------
set -eu

# Passthrough: any explicit command replaces the guard/generate path.
if [ "$#" -gt 0 ]; then
  exec "$@"
fi

# Container<->container traffic uses the compose SERVICE NAME on :5432 (NOT the
# host 127.0.0.1:5433 mapping). Overridable via the env, but never `localhost`
# for a host IPv4 mapping — Wave 1 showed `localhost` can hang before Postgres
# is reached on this host (D-12).
: "${DATABASE_URL:=postgres://tenantless:tenantless_dev@postgres-sim:5432/tenantless}"
export DATABASE_URL

# BOUNDED wait for Postgres CONNECTIVITY only (D-12). The slim image has no psql, so
# the probe uses the bundled psycopg; connect_timeout bounds each attempt and an
# overall deadline bounds the wait. It exits non-zero (fatal) if Postgres never
# becomes reachable — `set -e` then aborts BEFORE the generate.
#
# The destructive-safety decision (generate vs preserve) is NO LONGER made here
# (Wave2 #1). It lives in the generator under an advisory lock: `--only-if-empty`
# inspects the ENTIRE estate and either generates (empty) or preserves + exits 0
# (populated), atomically with the write — so this entrypoint can never truncate a
# populated volume, and there is no check-then-generate race across two connections.
python - <<'PY'
import os
import sys
import time

import psycopg

dsn = os.environ["DATABASE_URL"]
deadline = time.monotonic() + 120.0  # bounded overall wait
last = None
while time.monotonic() < deadline:
    try:
        with psycopg.connect(dsn, connect_timeout=5):
            sys.exit(0)
    except psycopg.OperationalError as exc:
        last = exc
        sys.stderr.write(f"[generator] waiting for Postgres: {exc}\n")
        sys.stderr.flush()
        time.sleep(3)
sys.stderr.write(f"[generator] FATAL: Postgres unreachable within 120s: {last}\n")
sys.exit(1)
PY

echo "[generator] Postgres reachable -- seeding demo ONLY IF the estate is empty (advisory-locked, non-destructive)."
exec tenantless generate --profile demo --seed 42 --cost-as-of 2026-01-01 --only-if-empty
