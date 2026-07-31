"""Process-orchestration helpers for the ``tenantless serve`` command (INFRA-03).

``serve`` delegates to the Rust ``tenantless-server`` binary: it discovers the
binary (D-01), runs a Postgres :5433 preflight (D-03), then launches the server
as a blocking FOREGROUND child that shares the parent's console so Ctrl+C reaches
both and the child's exit code propagates (D-03).

The module-level defaults are copied VERBATIM from the Rust clap source of truth
(``mock-server/src/config.rs:15-28``) per D-02 / RESEARCH Pitfall 3, so the
Python and Rust layers never diverge. ``DEFAULT_DATABASE_URL`` is the same
literal already in ``writer.py:27-30`` and ``conftest.py``.

Security posture:
- T-07-01: the server is launched with an argv LIST, NEVER ``shell=True``; all
  args are typed Click values (port int, URL strings), not free-form shell.
- T-07-02: the full DATABASE_URL is never echoed on the success path; the
  preflight relies on the connect error to surface reachability problems.
- T-07-03: binary discovery is bounded to trusted locations only (PATH, the
  repo's ``target/{release,debug}``, then a GUARDED ``cargo run`` that is offered
  ONLY inside a real Rust source checkout with cargo on PATH) — no user-supplied
  path. Everywhere else (e.g. an installed wheel) discovery fails fast with an
  actionable, DSN-free ``ClickException`` rather than a cryptic ``cargo run``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import click
import psycopg

# --- Rust clap defaults, copied verbatim (mock-server/src/config.rs:15-28) ------
# Pitfall 3 / D-02: tests/test_serve.py pins these against config.rs and writer.py.
DEFAULT_PORT = 8080  # config.rs:15
DEFAULT_BASE_URL = "http://localhost:8080"  # config.rs:19
DEFAULT_DATABASE_URL = (
    "postgres://tenantless:tenantless_dev@localhost:5433/tenantless"  # config.rs:26
)
BINARY_NAME = "tenantless-server"  # config.rs:12 / mock-server/Cargo.toml [package].name


def _discover_command(repo_root: Path) -> list[str]:
    """D-01 discovery order: PATH -> target/release -> target/debug -> GUARDED cargo.

    Returns the argv prefix that runs the server (a single-element ``[path]`` for
    a discovered binary, or the multi-element ``cargo run`` fallback). The ``.exe``
    suffix is appended only on Windows (``os.name == "nt"``). Discovery is bounded
    to trusted locations only (T-07-03) — never an arbitrary user-supplied path.

    T-07-03 (fail-fast): the ``cargo run`` fallback is a
    CHECKOUT-ONLY convenience — it is returned ONLY when a real Rust workspace is
    present (a root ``Cargo.toml`` or the ``mock-server`` member) AND ``cargo`` is
    on PATH. Everywhere else — most importantly an installed pure-Python wheel with
    no Rust toolchain — there is nothing to run, so we raise an actionable,
    DSN-free ``ClickException`` naming the two real remedies (build/install the
    binary, or run the published container image) instead of shelling out to a
    ``cargo run`` that would fail cryptically deep in a child process.
    """
    on_path = shutil.which(BINARY_NAME)
    if on_path:
        return [on_path]
    exe = BINARY_NAME + (".exe" if os.name == "nt" else "")
    for profile in ("release", "debug"):
        candidate = repo_root / "target" / profile / exe
        if candidate.is_file():
            return [str(candidate)]
    # Guarded zero-setup dev path: build+run via cargo, but ONLY inside a real
    # source checkout (workspace anchor present) with cargo on PATH.
    has_workspace = (repo_root / "Cargo.toml").is_file() or (
        repo_root / "mock-server" / "Cargo.toml"
    ).is_file()
    has_cargo = shutil.which("cargo") is not None
    if has_workspace and has_cargo:
        return ["cargo", "run", "-p", BINARY_NAME, "--"]
    # Nothing to run and nothing to build from — fail fast with actionable remedies.
    raise click.ClickException(
        f"No `{BINARY_NAME}` binary was found on PATH or in "
        "target/release / target/debug, and no Rust workspace is available to "
        "build one from (an installed wheel ships no Rust binary).\n"
        "To run the mock server, either:\n"
        f"  1. Build/install the binary from a source checkout — "
        f"`cargo build --release -p {BINARY_NAME}` — then put "
        f"target/release/{BINARY_NAME} on your PATH; or\n"
        "  2. Run the published container image "
        "`ghcr.io/code30-oss/tenantless-mock-server` (choose the tag for your "
        "release from the GitHub Releases page / release notes — there is no "
        "floating :latest), or `docker compose up mock-server`."
    )


def _preflight_postgres(database_url: str, timeout: int = 3) -> None:
    """D-03: probe Postgres before launching; raise an actionable hint on failure.

    Mirrors the project's own ``psycopg.connect(..., connect_timeout=3)`` skip
    idiom (``conftest.py``). On any failure, raise a ``click.ClickException`` whose
    message includes the "is the container up?" hint. The raw password is never
    re-printed beyond what the connect error itself surfaces (T-07-02).
    """
    try:
        conn = psycopg.connect(database_url, connect_timeout=timeout)
        conn.close()
    except Exception as exc:  # any failure -> actionable hint, then non-zero exit
        raise click.ClickException(
            f"Cannot reach Postgres for the mock server: {exc}\n"
            "Is the synthetic DB container up?  Try:  docker compose up -d\n"
            "(Expected on host port 5433 — see docker-compose.yml.)"
        ) from exc


def _launch_server(
    repo_root: Path,
    *,
    port: int,
    base_url: str,
    database_url: str,
    tls: bool = False,
    enforce_auth: bool = False,
    enable_control_plane: bool = False,
    control_token: str | None = None,
    control_data_dir: str | None = None,
) -> None:
    """Run the discovered server as a blocking foreground child; propagate its code.

    Builds the argv LIST ``_discover_command(...) + [--port, --base-url,
    --database-url]`` (T-07-01: a list, NEVER ``shell=True``). When ``tls`` is
    truthy the literal ``"--tls"`` token is appended so the Rust child dual-binds
    HTTPS on :8443 (PLAT-05 / D-17); the flag is a plain argv element, never a
    shell-interpolated string. Env mirrors the clap ``#[arg(env=...)]`` fallbacks
    so both wiring paths agree. No ``creationflags`` / ``start_new_session`` is
    passed, so the child shares this console and a Ctrl+C reaches both processes
    (D-03). ``check=False`` plus an explicit ``sys.exit(returncode)`` propagates
    the child's exit code; a missing binary/cargo becomes a ``click.ClickException``
    and Ctrl+C exits 130.

    Phase-17 control-plane passthrough (CTRL-05, config.rs:61-80): ``--enable-control-plane``
    and ``--control-data-dir <path>`` follow the ``--tls`` idiom — literal argv tokens, never
    ``shell=True``. The ``control_token`` is a SECRET, so it is forwarded via the CHILD ENV
    (``TENANTLESS_CONTROL_TOKEN``), NEVER on argv — keeping it out of the process list / shell
    history — and its value is never echoed or logged (T-07-02 / T-17-05).
    """
    cmd = _discover_command(repo_root) + [
        "--port",
        str(port),
        "--base-url",
        base_url,
        "--database-url",
        database_url,
    ]
    if tls:
        # T-08-03-T: literal flag token on the argv LIST — never shell=True.
        cmd.append("--tls")
    if enforce_auth:
        # IAM-05 / D-11: opt-in real-JWT enforcement. Literal argv token, never
        # shell=True (T-07-01) — mirrors the --tls passthrough above.
        cmd.append("--enforce-auth")
    if enable_control_plane:
        # CTRL-05 / D-02: opt-in control-plane arming. Literal argv token — mirrors --tls.
        cmd.append("--enable-control-plane")
    if control_data_dir is not None:
        # D-03: server-owned control-data root. Adjacent argv tokens (never shell=True);
        # omitted entirely when None so the Rust clap default (./control-data) applies.
        cmd.extend(["--control-data-dir", control_data_dir])
    env = {
        **os.environ,
        "DATABASE_URL": database_url,
        "BASE_URL": base_url,
        "PORT": str(port),
    }
    if control_token is not None:
        # T-17-05 / T-07-02: the secret goes through the child ENV, NEVER argv — so it
        # never lands in the process list or shell history. Its value is never logged.
        env["TENANTLESS_CONTROL_TOKEN"] = control_token
    try:
        completed = subprocess.run(cmd, env=env, check=False)
    except FileNotFoundError as exc:  # e.g. cargo not installed for the fallback
        raise click.ClickException(
            f"Could not launch the mock server: {exc}"
        ) from exc
    except KeyboardInterrupt:
        # Ctrl+C already reached the child via the shared console; it is shutting
        # down. Exit with the conventional 130 (128 + SIGINT).
        sys.exit(130)
    sys.exit(completed.returncode)  # propagate the child's exit code (D-03)
