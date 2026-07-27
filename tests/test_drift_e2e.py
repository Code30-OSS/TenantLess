"""Phase-11 capstone: the drift round-trip over the REAL stack (DRIFT-03 + D-04).

A single ``-m integration`` test that proves configuration drift is detected by a
plain ARM re-scan of the normal endpoints and is recovered byte-for-byte by
``revert-drift`` — across the real Python CLI (``generate`` / ``apply-drift`` /
``revert-drift`` as subprocesses) AND the real Rust ``tenantless-server`` driven
over plain HTTP (stdlib ``urllib`` — ZERO new deps, the Phase-7 07-02 idiom):

    generate (clean baseline)                                  ── subprocess CLI
      -> launch the real server on a free port                ── Phase-7 seam
      -> GET each resource's ARM detail  (baseline)           ── ARM source of truth
      -> apply-drift --type temporal     (parse batch_id)     ── subprocess CLI
      -> GET list/detail: mutated field VISIBLE; a disappeared
         resource ABSENT from the list AND 404 on detail      ── DRIFT-03 (D-11)
      -> revert-drift --batch-id <parsed>                     ── subprocess CLI
      -> GET detail: every baseline resource restored
         BYTE-FOR-BYTE; the disappeared resource is back;
         the minted "appear" leaves are gone                  ── D-04 (Pitfall 4)

ARM is proven the *only* drift source of truth: every assertion reads a normal
``/subscriptions/.../resources`` or resource-detail endpoint — the test NEVER
touches ``/simulator/drift*`` (D-17). "byte-for-byte" is asserted at the served
ARM-response level (canonical JSON of the parsed body), not raw JSONB column
bytes, because Postgres JSONB does not preserve key order/whitespace (Pitfall 4).

Safety: the ``generate`` subprocess TRUNCATES + rewrites the shared :5433
synthetic schema, so the destructive run is double-gated by BOTH the
``integration`` marker AND an explicit env opt-in
(``TENANTLESS_E2E_ALLOW_TRUNCATE=1``), exactly like ``test_e2e_pipeline.py``. The
``pg_conn`` fixture skips cleanly when Postgres is down. The launched server is an
argv-LIST child (never ``shell=True``) on a chosen free port, ALWAYS terminated in
a ``finally`` block; the full ``DATABASE_URL`` is never printed.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

# Opt-in env gate for the destructive truncate of the shared :5433 schema (reused
# verbatim from test_e2e_pipeline.py so the two E2E tests share one safety latch).
_ALLOW_TRUNCATE_ENV = "TENANTLESS_E2E_ALLOW_TRUNCATE"

# A small CLEAN tenant: no violations / cross-sub / identity so leaf resources
# carry no inbound references and are therefore disappear-eligible (D-10) — the
# round-trip needs at least one genuine disappear to exercise (D-11/D-13).
_N_SUBS = 6
_N_RESOURCES = 300
_SEED = 1234
_PAGE_TOP = 100

# Drift knobs. A temporal apply drives BOTH paths in one batch: field mutators
# (e.g. properties.provisioningState -> "Updating") AND the lifecycle pass
# (disappear soft-delete + appear mint). A fractional intensity mutates ~half of
# the in-scope population so survivors with a visible field change comfortably
# outnumber any incidental mutate∩disappear overlap.
_INTENSITY = "0.5"

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgres://tenantless:tenantless_dev@localhost:5433/tenantless",
)

# Run the Click CLI in a child interpreter (the console-script may not be on PATH
# inside the pytest subprocess on Windows). ``main()`` parses ``sys.argv[1:]``, so
# the args after the ``-c`` program string become the command line.
_CLI_RUNNER = "from tenantless.cli import main; main()"

_BATCH_RE = re.compile(r"apply-drift batch ([0-9a-fA-F-]{36})")


def _free_port() -> int:
    """Bind to port 0 to let the OS hand back a currently-free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_cli(*args: str) -> str:
    """Run ``tenantless <args>`` as a subprocess against the test DB; return stdout.

    The full DSN is passed via ``DATABASE_URL`` in the child env (never logged).
    A non-zero exit raises with the captured stderr so a CLI failure is legible.
    """
    env = {**os.environ, "DATABASE_URL": _DATABASE_URL}
    proc = subprocess.run(  # noqa: S603 - argv list, trusted interpreter
        [sys.executable, "-c", _CLI_RUNNER, *args],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"CLI {args!r} failed (exit {proc.returncode}):\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    return proc.stdout


def _http_get(url: str, *, bearer: str | None = "Bearer drift-e2e") -> tuple[int, dict]:
    """GET ``url`` with an optional Bearer; return ``(status, json_body)``.

    A non-2xx is surfaced via ``urllib``'s ``HTTPError`` whose ``.code`` and JSON
    body are returned verbatim — so a 404 CloudError is asserted as data, not an
    exception.
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
    """Poll ``GET /subscriptions`` until the server answers (<500) or the deadline
    elapses; fail fast if the child has already exited."""
    sub_url = f"{base_url}/subscriptions"
    end = time.monotonic() + deadline_s
    last_err: Exception | None = None
    while time.monotonic() < end:
        if proc.poll() is not None:
            raise AssertionError(
                f"server child exited early with code {proc.returncode} before readiness"
            )
        try:
            status, _ = _http_get(sub_url)
            if status < 500:
                return
            last_err = AssertionError(f"server warming up: HTTP {status}")
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_err = exc
        time.sleep(0.25)
    raise AssertionError(f"server did not become ready within {deadline_s}s: {last_err}")


def _canonical(body: dict) -> str:
    """Canonical JSON of a parsed ARM body (sorted keys, no whitespace).

    The D-04 "byte-for-byte" contract is at the SERVED-RESPONSE level (Pitfall 4):
    JSONB does not preserve key order, so we compare the canonicalised parsed body,
    which is exactly what an ARM scanner consumes.
    """
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def _list_resources(base_url: str, sub: str) -> dict[str, dict]:
    """Walk every page of the subscription's ARM resource list; return ``{id: res}``."""
    url = f"{base_url}/subscriptions/{sub}/resources?$top={_PAGE_TOP}"
    out: dict[str, dict] = {}
    pages = 0
    while url is not None:
        status, body = _http_get(url)
        assert status == 200, f"list page failed: {status} {body}"
        for res in body["value"]:
            out[res["id"]] = res
        pages += 1
        url = body.get("nextLink")
        assert pages <= 1000, "pagination did not terminate (runaway nextLink)"
    return out


def _detail(base_url: str, resource_id: str) -> tuple[int, dict]:
    """GET a single resource's ARM detail by its full id path."""
    return _http_get(f"{base_url}{resource_id}")


@pytest.mark.integration
def test_drift_round_trip(pg_conn):
    """generate -> apply-drift -> ARM shows drift -> revert-drift -> ARM restored.

    The capstone round-trip (DRIFT-03 + D-04): every assertion is an ARM GET (no
    /simulator dependency, D-17). Proves (1) the mutated served field is visible
    after apply, (2) a disappeared resource is absent from the list AND 404s on
    detail, (3) after revert every baseline resource is restored byte-for-byte and
    the disappeared resource returns while the minted appear-leaves are gone.
    """
    # Double-gate the destructive truncate (the marker is the first lock; this env
    # opt-in is the second so a real dev dataset on :5433 is never silently wiped).
    if os.environ.get(_ALLOW_TRUNCATE_ENV) not in ("1", "true", "yes"):
        pytest.skip(
            f"set {_ALLOW_TRUNCATE_ENV}=1 to allow this test to TRUNCATE + rewrite "
            "the synthetic schema on :5433 (regeneratable synthetic data only)"
        )

    # --- Stage 1: generate a small CLEAN tenant (subprocess CLI) -------------------
    # No violations/cross-sub/identity -> leaf resources carry no inbound refs and
    # are disappear-eligible (D-10), so the temporal lifecycle has a population.
    _run_cli(
        "generate",
        "--profile", "small",
        "--seed", str(_SEED),
        "--subscriptions", str(_N_SUBS),
        "--resources", str(_N_RESOURCES),
        "--force",
        "--no-violations",
        "--no-cross-sub",
        "--no-identity",
    )

    # Pick the busiest subscription so the round-trip exercises a populated sub.
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT subscription_id::text, count(*) AS n "
            "FROM synthetic.resources GROUP BY subscription_id "
            "ORDER BY n DESC, subscription_id LIMIT 1"
        )
        busiest_sub, sub_count = cur.fetchone()
    # The connection only READ — commit so it holds no lock while the CLI children
    # run their own ALTER/UPDATE transactions against the same schema.
    pg_conn.commit()
    assert sub_count > 0, "busiest subscription must hold resources"

    # --- Stage 2: launch the real server on a free port (Phase-7 seam) -------------
    from tenantless import serve

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    cmd = serve._discover_command(_REPO_ROOT) + [
        "--port", str(port),
        "--base-url", base_url,
        "--database-url", _DATABASE_URL,
    ]
    env = {
        **os.environ,
        "DATABASE_URL": _DATABASE_URL,
        "BASE_URL": base_url,
        "PORT": str(port),
    }
    proc = subprocess.Popen(cmd, env=env)  # noqa: S603 - argv list, trusted discovery
    try:
        _wait_ready(base_url, proc)

        # --- Stage 3: BASELINE scan — capture every resource's ARM detail ----------
        baseline_list = _list_resources(base_url, busiest_sub)
        baseline_ids = set(baseline_list)
        assert baseline_ids, "baseline ARM list must be non-empty"
        baseline_detail: dict[str, str] = {}
        for rid in baseline_ids:
            status, body = _detail(base_url, rid)
            assert status == 200, f"baseline detail GET failed for {rid}: {status}"
            baseline_detail[rid] = _canonical(body)

        # --- Stage 4: apply temporal drift to this subscription (subprocess) -------
        apply_out = _run_cli(
            "apply-drift",
            "--type", "temporal",
            "--seed", str(_SEED),
            "--intensity", _INTENSITY,
            "--subscription", busiest_sub,
        )
        m = _BATCH_RE.search(apply_out)
        assert m is not None, f"could not parse batch_id from apply-drift stdout:\n{apply_out}"
        batch_id = m.group(1)

        # --- Stage 5: re-scan — drift is VISIBLE over the normal ARM endpoints -----
        post_list = _list_resources(base_url, busiest_sub)
        post_ids = set(post_list)

        # (a) field drift: at least one resource present in BOTH scans now serves
        #     the mutated provisioningState (proves ARM detects drift after apply).
        survivors = baseline_ids & post_ids
        assert survivors, "some resources must survive the lifecycle pass"
        mutated_visible = [
            rid
            for rid in survivors
            if post_list[rid].get("properties", {}).get("provisioningState") == "Updating"
        ]
        assert mutated_visible, (
            "apply-drift must make a mutated field visible on a surviving resource "
            "(expected properties.provisioningState == 'Updating' via ARM re-scan)"
        )
        # The same id's detail body must differ from its baseline (drift is real).
        changed_id = mutated_visible[0]
        status, changed_body = _detail(base_url, changed_id)
        assert status == 200
        assert _canonical(changed_body) != baseline_detail[changed_id], (
            "the post-apply ARM detail must differ from the baseline (drift applied)"
        )

        # (b) disappear: at least one baseline resource is now ABSENT from the list
        #     AND 404s on detail (soft-delete excluded, D-11) — yet the row remains.
        disappeared = baseline_ids - post_ids
        assert disappeared, (
            "the temporal lifecycle must hide >=1 disappear-eligible leaf "
            f"(baseline {len(baseline_ids)} / post {len(post_ids)})"
        )
        gone_id = sorted(disappeared)[0]
        status, gone_body = _detail(base_url, gone_id)
        assert status == 404, f"disappeared resource must 404 on detail, got {status}"
        assert gone_body["error"]["code"] == "ResourceNotFound", gone_body

        # (c) appear: any minted leaves are NEW ids not in the baseline.
        appeared = post_ids - baseline_ids

        # --- Stage 6: revert the batch (subprocess) --------------------------------
        _run_cli("revert-drift", "--batch-id", batch_id)

        # --- Stage 7: re-scan — restored BYTE-FOR-BYTE (D-04) ----------------------
        revert_list = _list_resources(base_url, busiest_sub)
        revert_ids = set(revert_list)

        # The full baseline id-set is back (disappeared unhidden, appear deleted).
        assert revert_ids == baseline_ids, (
            "after revert the served id-set must equal the baseline "
            f"(missing={baseline_ids - revert_ids}, extra={revert_ids - baseline_ids})"
        )
        # The previously-disappeared resource is served again (detail 200).
        status, _ = _detail(base_url, gone_id)
        assert status == 200, f"disappeared resource must return after revert, got {status}"
        # The minted appear-leaves are gone (deleted on revert, D-13).
        for aid in appeared:
            status, _ = _detail(base_url, aid)
            assert status == 404, f"appear leaf {aid} must be deleted after revert"

        # Every baseline resource's ARM detail is restored byte-for-byte (D-04).
        mismatches: list[str] = []
        for rid in baseline_ids:
            status, body = _detail(base_url, rid)
            assert status == 200, f"post-revert detail GET failed for {rid}: {status}"
            if _canonical(body) != baseline_detail[rid]:
                mismatches.append(rid)
        assert not mismatches, (
            "post-revert ARM detail must equal the baseline byte-for-byte for every "
            f"resource; {len(mismatches)} mismatched (e.g. {mismatches[:3]})"
        )
        # The mutated resource specifically is back to its exact baseline body.
        status, restored_body = _detail(base_url, changed_id)
        assert status == 200
        assert _canonical(restored_body) == baseline_detail[changed_id], (
            "the drifted resource must be restored byte-for-byte by revert (D-04)"
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
