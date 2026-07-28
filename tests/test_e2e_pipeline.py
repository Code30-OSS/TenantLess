"""E2E pipeline smoke test (D-07/D-08, INFRA-05/INFRA-03).

A single ``-m integration`` test that proves the three stages compose end-to-end:

    generate (in memory) -> COPY into a throwaway :5433 DB -> launch the real
    Rust ``tenantless-server`` via the Plan 01 serve seam -> drive it over plain
    HTTP (stdlib ``urllib.request`` — ZERO new deps; RESEARCH A2) asserting ARM
    response shapes, a 401 on a missing Bearer, an ABSOLUTE ``nextLink``, and
    full single-visit pagination.

NO external-scanner dependency: the HTTP side is a black-box ARM scan with a
plain client. The test does NOT assert cross-subscription topology over ARM —
that lives only in ``synthetic.dependencies`` and is invisible to an ARM scan
(cross-sub-risk tooling only; the RESEARCH cross-sub anti-pattern).

Safety (T-07-04): truncating the shared :5433 synthetic schema is gated by BOTH
the ``integration`` marker AND an explicit env opt-in
(``TENANTLESS_E2E_ALLOW_TRUNCATE=1``) so a dev dataset is never silently wiped.
The ``pg_conn`` fixture (tests/conftest.py) skips cleanly when Postgres is down.

The launched server (T-07-05) is started via the Plan 01 discovery seam
(``serve._discover_command``) as an argv-LIST child (never ``shell=True``),
pinned to a chosen free port, and ALWAYS terminated in a ``finally`` block.
The full ``DATABASE_URL`` is never printed (T-07-06) — only the bound port.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import pytest

# Opt-in env gate for the destructive truncate of the shared :5433 schema (T-07-04).
_ALLOW_TRUNCATE_ENV = "TENANTLESS_E2E_ALLOW_TRUNCATE"

# A small tenant — this is a SMOKE test, not the scale benchmark (Plan 03 owns scale).
_N_SUBS = 20
_N_RESOURCES = 3000
_SEED = 42

# Small page size so a busy subscription guarantees MULTIPLE pages to walk.
_PAGE_TOP = 50

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgres://tenantless:tenantless_dev@localhost:5433/tenantless",
)


def _free_port() -> int:
    """Bind to port 0 to let the OS hand back a currently-free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http_get(url: str, *, bearer: str | None = "Bearer e2e-smoke") -> tuple[int, dict]:
    """GET ``url`` with an optional Bearer header; return ``(status, json_body)``.

    A non-2xx status is surfaced via ``urllib``'s ``HTTPError`` whose ``.code`` and
    JSON body we return verbatim — so a 401 CloudError is asserted as data, not an
    exception. ``bearer=None`` omits the ``Authorization`` header entirely.
    """
    req = urllib.request.Request(url, method="GET")
    if bearer is not None:
        req.add_header("Authorization", bearer)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"_raw": raw}
        return exc.code, body


def _wait_ready(base_url: str, proc: subprocess.Popen, *, deadline_s: float = 30.0) -> None:
    """Poll ``GET /subscriptions`` until the server answers (any HTTP status) or the
    deadline elapses; fail fast if the child has already exited."""
    sub_url = f"{base_url}/subscriptions"
    end = time.monotonic() + deadline_s
    last_err: Exception | None = None
    while time.monotonic() < end:
        if proc.poll() is not None:
            raise AssertionError(
                f"server child exited early with code {proc.returncode} before readiness"
            )
        try:
            # A non-5xx response proves the listener is up AND serving (200/401/
            # 404 all qualify). A 5xx means it bound but is still warming up — keep
            # polling instead of falsely declaring readiness (WR-02).
            status, _ = _http_get(sub_url)
            if status < 500:
                return
            last_err = AssertionError(f"server warming up: HTTP {status}")
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_err = exc
        time.sleep(0.25)
    raise AssertionError(f"server did not become ready within {deadline_s}s: {last_err}")


@pytest.mark.integration
def test_e2e_generate_serve_http_scan(generator_profile, pg_conn):
    """generate -> serve -> HTTP ARM-contract scan, end-to-end behind -m integration.

    Proves the three stages compose and that the server is ARM-contract compatible
    without any external-scanner dependency: a plain stdlib HTTP client asserts the ARM
    envelope, a 401 CloudError on a missing Bearer, an ABSOLUTE ``nextLink``, and
    full single-visit pagination. Cross-sub topology is NOT asserted over ARM.
    """
    from tenantless.generator.pipeline import generate_tenant
    from tenantless.generator import writer
    from tenantless import serve

    # T-07-04: the destructive truncate must be explicitly opted into. The marker
    # already gates this off the default suite; the env gate is the second lock so a
    # real dev dataset on :5433 is NEVER silently wiped.
    if os.environ.get(_ALLOW_TRUNCATE_ENV) not in ("1", "true", "yes"):
        pytest.skip(
            f"set {_ALLOW_TRUNCATE_ENV}=1 to allow this test to TRUNCATE + rewrite the "
            "synthetic schema on :5433 (regeneratable synthetic data only)"
        )

    # --- Stage 1: generate a small synthetic tenant in memory ----------------------
    result = generate_tenant(
        generator_profile,
        seed=_SEED,
        n_subs=_N_SUBS,
        n_resources=_N_RESOURCES,
        inject_violations=True,
        inject_cross_sub=True,
    )
    tenant = result.tenant
    violation_rows = result.violations
    dependency_rows = result.dependencies
    assert len(tenant.subscriptions) > 0

    # --- Stage 2: COPY it into the throwaway DB (FK order) -------------------------
    writer.truncate_synthetic(pg_conn)
    writer.write_tenant(
        pg_conn, tenant, dependencies=dependency_rows, violations=violation_rows
    )
    pg_conn.commit()

    # Pick the subscription with the most resources so a small $top guarantees
    # multiple pages to walk (this is what makes the pagination assertion meaningful).
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT subscription_id::text, count(*) AS n "
            "FROM synthetic.resources GROUP BY subscription_id "
            "ORDER BY n DESC LIMIT 1"
        )
        busiest_sub, expected_res_count = cur.fetchone()
    assert expected_res_count > _PAGE_TOP, (
        "the busiest subscription must hold more than one page of resources for the "
        f"pagination walk to be meaningful (had {expected_res_count}, top={_PAGE_TOP})"
    )

    # --- Stage 3: launch the real server on a free port via the Plan 01 seam -------
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    cmd = serve._discover_command(_REPO_ROOT) + [
        "--port",
        str(port),
        "--base-url",
        base_url,
        "--database-url",
        _DATABASE_URL,
    ]
    env = {
        **os.environ,
        "DATABASE_URL": _DATABASE_URL,
        "BASE_URL": base_url,
        "PORT": str(port),
    }
    # The SELECT opened a transaction and holds ACCESS SHARE until it ends.
    # Release it before server startup, whose schema preflight needs an exclusive lock.
    pg_conn.rollback()

    # argv LIST, never shell=True (T-07-05). Bind to a chosen free port.
    proc = subprocess.Popen(cmd, env=env)  # noqa: S603 - argv list, trusted discovery
    try:
        _wait_ready(base_url, proc)

        # --- HTTP assertion 1: missing Bearer -> 401 ARM CloudError ----------------
        status, body = _http_get(f"{base_url}/subscriptions", bearer=None)
        assert status == 401, f"missing Bearer must 401, got {status}"
        assert body["error"]["code"] == "MissingAuthenticationToken", body

        # --- HTTP assertion 2: GET /subscriptions ARM envelope ---------------------
        status, body = _http_get(f"{base_url}/subscriptions")
        assert status == 200
        assert isinstance(body["value"], list) and body["value"], "non-empty subs list"
        sub0 = body["value"][0]
        for field in ("id", "subscriptionId", "displayName", "state", "tenantId"):
            assert field in sub0, f"ARM subscription envelope missing {field!r}: {sub0}"
        assert sub0["id"] == f"/subscriptions/{sub0['subscriptionId']}"
        served_sub_ids = {s["subscriptionId"] for s in body["value"]}
        assert busiest_sub in served_sub_ids, "busiest sub must be served by ARM"

        # --- HTTP assertion 3: paginate the busiest sub's resources ----------------
        # Walk every nextLink, collecting ids; assert ABSOLUTE nextLinks and that
        # each resource is visited EXACTLY once (full, single-visit pagination).
        url = f"{base_url}/subscriptions/{busiest_sub}/resources?$top={_PAGE_TOP}"
        seen_ids: set[str] = set()
        pages = 0
        saw_next_link = False
        while url is not None:
            status, body = _http_get(url)
            assert status == 200, f"page GET failed: {status}"
            assert isinstance(body["value"], list)
            for res in body["value"]:
                # ARM resource shape (MOCK-03): id/name/type/location/properties.
                for field in ("id", "name", "type", "location", "properties"):
                    assert field in res, f"ARM resource missing {field!r}: {res}"
                assert res["id"] not in seen_ids, f"resource visited twice: {res['id']}"
                seen_ids.add(res["id"])
            pages += 1
            next_link = body.get("nextLink")
            if next_link is not None:
                saw_next_link = True
                # MOCK-08: nextLink is ABSOLUTE against the configured base_url.
                parsed = urlparse(next_link)
                assert parsed.scheme in ("http", "https"), f"nextLink not absolute: {next_link}"
                assert parsed.netloc == f"127.0.0.1:{port}", (
                    f"nextLink host must match base_url: {next_link}"
                )
                assert next_link.startswith(base_url), next_link
            url = next_link
            assert pages <= 1000, "pagination did not terminate (runaway nextLink)"

        # Multiple pages were walked and at least one absolute nextLink was emitted.
        assert saw_next_link, "expected at least one nextLink for the busiest subscription"
        assert pages >= 2, f"expected >=2 pages walking the busiest sub, got {pages}"
        # Full single-visit pagination: every resource seen exactly once, count matches DB.
        assert len(seen_ids) == expected_res_count, (
            f"pagination visited {len(seen_ids)} resources; DB has {expected_res_count}"
        )

        # NOTE: cross-sub topology (synthetic.dependencies) is intentionally NOT
        # asserted over ARM — it is not ARM-visible (cross-sub-risk tooling only;
        # RESEARCH anti-pattern).
    finally:
        # T-07-05: ALWAYS tear the child down deterministically.
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
