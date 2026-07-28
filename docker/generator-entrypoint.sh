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

# D-05 non-empty guard with a BOUNDED connection wait (D-12). The slim image has
# no psql, so the probe uses the bundled psycopg; connect_timeout bounds each
# attempt and an overall deadline bounds the wait. It prints exactly SKIP or
# GENERATE on stdout, or exits non-zero (fatal) if Postgres never becomes
# reachable — `set -e` then aborts BEFORE any destructive generate.
decision="$(
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
        with psycopg.connect(dsn, connect_timeout=5) as conn, conn.cursor() as cur:
            # to_regclass() is NULL when the table is absent (fresh volume before
            # generate self-provisions the schema) -> treat as empty -> GENERATE.
            cur.execute("SELECT to_regclass('synthetic.resources')")
            reg = cur.fetchone()[0]
            n = 0
            if reg is not None:
                cur.execute("SELECT count(*) FROM synthetic.resources")
                n = int(cur.fetchone()[0])
        print("SKIP" if n > 0 else "GENERATE")
        sys.exit(0)
    except psycopg.OperationalError as exc:
        last = exc
        sys.stderr.write(f"[generator] waiting for Postgres: {exc}\n")
        sys.stderr.flush()
        time.sleep(3)
sys.stderr.write(f"[generator] FATAL: Postgres unreachable within 120s: {last}\n")
sys.exit(1)
PY
)"

case "$decision" in
  SKIP)
    echo "[generator] estate present -- skipping generate; existing volume preserved (D-05)."
    exit 0
    ;;
  GENERATE)
    echo "[generator] empty estate -- generating demo (profile=demo seed=42 cost-as-of=2026-01-01)."
    exec tenantless generate --profile demo --seed 42 --cost-as-of 2026-01-01 --force
    ;;
  *)
    echo "[generator] FATAL: unexpected probe decision '$decision'." >&2
    exit 1
    ;;
esac
