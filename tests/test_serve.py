"""Unit tests for the `tenantless serve` delegation seam (Phase 07, Plan 01).

These tests exercise the pure helpers in ``tenantless.serve`` plus the Click
group wiring. They NEVER launch a real Rust server or open a real Postgres
connection: ``serve.shutil.which``, ``serve.subprocess.run``, and
``serve.psycopg.connect`` are all monkeypatched (mirrors the project's
``monkeypatch.setattr`` precedent at ``test_reader.py:129-133``). This module is
NOT marked ``integration`` -- it runs inside the default ``-m 'not integration'``
suite.

Parity (RESEARCH Pitfall 3): the three Python defaults below MUST equal the Rust
clap source of truth in ``mock-server/src/config.rs:15-28``; the same
DATABASE_URL literal already lives in ``writer.py:27-30`` and ``conftest.py``.
``test_defaults_parity_*`` pins that so the Python and Rust layers never diverge.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import click
import pytest

import scrub_tokens

from tenantless import serve as serve_mod
from tenantless.cli import main

_REPO_ROOT = Path(__file__).resolve().parents[1]


# --- D-02 / Pitfall 3: default parity with the Rust clap source of truth -------


def test_defaults_parity_against_config_rs():
    """serve.py defaults must equal mock-server/src/config.rs:15-28 verbatim."""
    assert serve_mod.DEFAULT_PORT == 8080
    assert serve_mod.DEFAULT_BASE_URL == "http://localhost:8080"
    assert (
        serve_mod.DEFAULT_DATABASE_URL
        == "postgres://tenantless:tenantless_dev@localhost:5433/tenantless"
    )
    assert serve_mod.BINARY_NAME == "tenantless-server"


def test_env_example_password_matches_local_default():
    """A fresh `cp .env.example .env` must Just Work on a local dev box (SEC-HIGH-2).

    The example file's POSTGRES_PASSWORD and the password embedded in its
    DATABASE_URL must equal the local dev credential baked into the Python/Rust
    defaults (`tenantless_dev`), so the quickstart needs zero credential edits.
    """
    from tenantless.generator import writer

    # The local dev password baked into the Python/Rust default DATABASE_URL.
    local_default = writer._DEFAULT_DATABASE_URL
    m = re.search(r"postgres://[^:]+:([^@]+)@", local_default)
    assert m, f"could not parse password from default URL {local_default!r}"
    expected_password = m.group(1)
    assert expected_password == "tenantless_dev"

    env_example = (_REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    values: dict[str, str] = {}
    for line in env_example.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip()

    assert values.get("POSTGRES_PASSWORD") == expected_password, (
        ".env.example POSTGRES_PASSWORD must equal the local dev default "
        f"({expected_password!r}); got {values.get('POSTGRES_PASSWORD')!r}"
    )
    db_url = values.get("DATABASE_URL", "")
    m2 = re.search(r"postgres://[^:]+:([^@]+)@", db_url)
    assert m2, f"could not parse password from .env.example DATABASE_URL {db_url!r}"
    assert m2.group(1) == expected_password, (
        ".env.example DATABASE_URL password must equal the local dev default"
    )
    # The full DATABASE_URL must match the Python/Rust default literal verbatim.
    assert db_url == local_default


def test_real_denylist_is_git_ignored():
    """SEC-MED-1: a source-named real denylist under profiles/ is git-ignored.

    A real-source denylist filename embeds the name of the source it was built
    from, so the ``profiles/.*-denylist.json`` glob has to cover ANY such name --
    that generality is the thing under test, and an invented source name exercises
    it exactly as well as a real one. (This literal used to be assembled from
    string fragments to smuggle the real name past the scrub gate; that defeated
    the public/private token split and is what the Stage 3 review rejected.)

    Skips cleanly if not inside a git work tree (e.g. an exported tarball).
    """
    real_denylist = "profiles/.acme-denylist.json"

    try:
        inside = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        pytest.skip("git not available")
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        pytest.skip("not inside a git work tree")

    # check-ignore returns 0 when the path IS ignored.
    result = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "check-ignore", "-q", real_denylist],
        capture_output=True,
    )
    assert result.returncode == 0, (
        f"{real_denylist} must be git-ignored "
        "(broaden .gitignore to profiles/.*-denylist.json)"
    )


def test_default_database_url_matches_writer():
    """The serve default DATABASE_URL is the same literal writer.py falls back to.

    Compares against writer's default *literal* (env-independent), not the
    env-resolved ``writer.DATABASE_URL`` — otherwise an exported ``DATABASE_URL``
    (as the integration/scale tests set) would fail this parity check spuriously
    (WR-01).
    """
    from tenantless.generator import writer

    assert serve_mod.DEFAULT_DATABASE_URL == writer._DEFAULT_DATABASE_URL


# --- Click group membership ----------------------------------------------------


def test_main_group_registers_analyze_generate_serve():
    """analyze, generate, AND serve are all live commands on the main group."""
    assert set(main.commands) >= {"analyze", "generate", "serve"}


# --- D-01: binary discovery order ----------------------------------------------


def test_discover_prefers_path(monkeypatch, tmp_path):
    """When shutil.which finds the binary on PATH, that wins outright."""
    monkeypatch.setattr(serve_mod.shutil, "which", lambda name: "/usr/bin/tenantless-server")
    cmd = serve_mod._discover_command(tmp_path)
    assert cmd == ["/usr/bin/tenantless-server"]


def test_discover_falls_back_to_release(monkeypatch, tmp_path):
    """which->None: target/release is preferred over target/debug and cargo."""
    monkeypatch.setattr(serve_mod.shutil, "which", lambda name: None)
    exe = serve_mod.BINARY_NAME + (".exe" if serve_mod.os.name == "nt" else "")
    release = tmp_path / "target" / "release" / exe
    release.parent.mkdir(parents=True)
    release.write_text("")
    cmd = serve_mod._discover_command(tmp_path)
    assert cmd == [str(release)]


def test_discover_falls_back_to_debug(monkeypatch, tmp_path):
    """which->None and no release build: target/debug is next."""
    monkeypatch.setattr(serve_mod.shutil, "which", lambda name: None)
    exe = serve_mod.BINARY_NAME + (".exe" if serve_mod.os.name == "nt" else "")
    debug = tmp_path / "target" / "debug" / exe
    debug.parent.mkdir(parents=True)
    debug.write_text("")
    cmd = serve_mod._discover_command(tmp_path)
    assert cmd == [str(debug)]


def test_discover_falls_back_to_cargo(monkeypatch, tmp_path):
    """which(binary)->None + no built binary BUT a real workspace + cargo on PATH:
    the cargo run fallback is returned (the zero-setup dev checkout, T-07-03)."""
    # Real source-checkout anchors: root workspace + the mock-server member.
    (tmp_path / "Cargo.toml").write_text('[workspace]\nmembers = ["mock-server"]\n')
    (tmp_path / "mock-server").mkdir()
    (tmp_path / "mock-server" / "Cargo.toml").write_text("[package]\nname = 'x'\n")

    def which(name):
        # The server binary is NOT on PATH, but cargo IS (a dev toolchain).
        return "/usr/bin/cargo" if name == "cargo" else None

    monkeypatch.setattr(serve_mod.shutil, "which", which)
    cmd = serve_mod._discover_command(tmp_path)
    assert cmd == ["cargo", "run", "-p", serve_mod.BINARY_NAME, "--"]


def test_discover_fails_fast_without_workspace(monkeypatch, tmp_path):
    """No binary anywhere AND no Rust workspace (the installed-wheel case): discovery
    raises an actionable ClickException naming the binary, the GHCR image, and the
    build-from-source remedy — NEVER a cryptic `cargo run`."""
    monkeypatch.setattr(serve_mod.shutil, "which", lambda name: None)
    # tmp_path is bare: no Cargo.toml, no mock-server/.
    with pytest.raises(click.ClickException) as exc:
        serve_mod._discover_command(tmp_path)
    msg = str(exc.value)
    assert serve_mod.BINARY_NAME in msg
    assert "ghcr.io/code30-oss/tenantless-mock-server" in msg
    # The build-from-source remedy is named (a cargo BUILD, not a bare cargo run).
    assert "cargo build" in msg
    # The old cryptic fallback must be gone from the guidance.
    assert "cargo run" not in msg


def test_discover_fails_fast_when_cargo_absent(monkeypatch, tmp_path):
    """A real workspace is present but cargo is NOT on PATH (a checkout without the
    Rust toolchain): discovery cannot build, so it fails fast with a ClickException."""
    (tmp_path / "Cargo.toml").write_text('[workspace]\nmembers = ["mock-server"]\n')
    (tmp_path / "mock-server").mkdir()
    (tmp_path / "mock-server" / "Cargo.toml").write_text("[package]\nname = 'x'\n")
    monkeypatch.setattr(serve_mod.shutil, "which", lambda name: None)
    with pytest.raises(click.ClickException) as exc:
        serve_mod._discover_command(tmp_path)
    assert "cargo run" not in str(exc.value)


def test_discover_exe_suffix_is_platform_guarded(monkeypatch, tmp_path):
    """The .exe suffix is appended only when os.name == 'nt'."""
    monkeypatch.setattr(serve_mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(serve_mod.os, "name", "nt")
    win = tmp_path / "target" / "release" / "tenantless-server.exe"
    win.parent.mkdir(parents=True)
    win.write_text("")
    cmd = serve_mod._discover_command(tmp_path)
    assert cmd == [str(win)]

    monkeypatch.setattr(serve_mod.os, "name", "posix")
    nix = tmp_path / "target" / "debug" / "tenantless-server"
    nix.parent.mkdir(parents=True)
    nix.write_text("")
    # release/tenantless-server (no .exe) does not exist -> debug path chosen.
    cmd = serve_mod._discover_command(tmp_path)
    assert cmd == [str(nix)]


# --- D-03: Postgres preflight --------------------------------------------------


def test_preflight_raises_clickexception_with_container_hint(monkeypatch):
    """When psycopg.connect raises, a ClickException with the 'container up' hint is raised."""

    def boom(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(serve_mod.psycopg, "connect", boom)
    with pytest.raises(click.ClickException) as exc:
        serve_mod._preflight_postgres("postgres://x@localhost:5433/tenantless")
    msg = str(exc.value)
    assert "container up" in msg.lower()


def test_preflight_passes_when_connect_succeeds(monkeypatch):
    """A successful connect closes the probe and returns without raising."""
    closed = {"called": False}

    class FakeConn:
        def close(self):
            closed["called"] = True

    monkeypatch.setattr(serve_mod.psycopg, "connect", lambda *a, **k: FakeConn())
    serve_mod._preflight_postgres("postgres://x@localhost:5433/tenantless")
    assert closed["called"] is True


# --- D-03 / T-07-01: foreground argv-list launch + exit-code propagation --------


class _FakeCompleted:
    def __init__(self, returncode):
        self.returncode = returncode


def test_launch_forwards_flags_as_argv_list(monkeypatch, tmp_path):
    """The launch builds an argv LIST carrying --port/--base-url/--database-url; never shell=True."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeCompleted(0)

    # The server binary is discoverable on PATH so discovery short-circuits there
    # (the guarded cargo/fail-fast branches are covered by the discovery tests).
    monkeypatch.setattr(
        serve_mod.shutil,
        "which",
        lambda name: "/usr/bin/tenantless-server" if name == serve_mod.BINARY_NAME else None,
    )
    monkeypatch.setattr(serve_mod.subprocess, "run", fake_run)

    with pytest.raises(SystemExit) as exc:
        serve_mod._launch_server(
            tmp_path,
            port=9999,
            base_url="http://localhost:9999",
            database_url="postgres://u:p@localhost:5433/tenantless",
        )
    assert exc.value.code == 0

    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    # Flags forwarded as adjacent list elements (never a joined shell string).
    assert "--port" in cmd and cmd[cmd.index("--port") + 1] == "9999"
    assert "--base-url" in cmd and cmd[cmd.index("--base-url") + 1] == "http://localhost:9999"
    assert (
        "--database-url" in cmd
        and cmd[cmd.index("--database-url") + 1] == "postgres://u:p@localhost:5433/tenantless"
    )
    # T-07-01: shell must default off (never passed as True).
    assert captured["kwargs"].get("shell", False) is False


def test_launch_propagates_child_returncode(monkeypatch, tmp_path):
    """A child returncode of 7 becomes SystemExit(7)."""
    monkeypatch.setattr(
        serve_mod.shutil,
        "which",
        lambda name: "/usr/bin/tenantless-server" if name == serve_mod.BINARY_NAME else None,
    )
    monkeypatch.setattr(serve_mod.subprocess, "run", lambda cmd, **k: _FakeCompleted(7))

    with pytest.raises(SystemExit) as exc:
        serve_mod._launch_server(
            tmp_path,
            port=8080,
            base_url="http://localhost:8080",
            database_url="postgres://u:p@localhost:5433/tenantless",
        )
    assert exc.value.code == 7


def test_launch_filenotfound_becomes_clickexception(monkeypatch, tmp_path):
    """A missing binary/cargo (FileNotFoundError) surfaces as a ClickException."""

    def boom(cmd, **kwargs):
        raise FileNotFoundError("cargo not found")

    monkeypatch.setattr(
        serve_mod.shutil,
        "which",
        lambda name: "/usr/bin/tenantless-server" if name == serve_mod.BINARY_NAME else None,
    )
    monkeypatch.setattr(serve_mod.subprocess, "run", boom)

    with pytest.raises(click.ClickException):
        serve_mod._launch_server(
            tmp_path,
            port=8080,
            base_url="http://localhost:8080",
            database_url="postgres://u:p@localhost:5433/tenantless",
        )


# --- PLAT-05 / D-17: serve --tls forwards "--tls" to the Rust child argv --------


def _capture_launch_cmd(monkeypatch, **launch_kwargs):
    """Run _launch_server with subprocess mocked; return the captured argv list."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeCompleted(0)

    monkeypatch.setattr(
        serve_mod.shutil,
        "which",
        lambda name: "/usr/bin/tenantless-server" if name == serve_mod.BINARY_NAME else None,
    )
    monkeypatch.setattr(serve_mod.subprocess, "run", fake_run)

    with pytest.raises(SystemExit):
        serve_mod._launch_server(
            Path("."),
            port=8080,
            base_url="http://localhost:8080",
            database_url="postgres://u:p@localhost:5433/tenantless",
            **launch_kwargs,
        )
    return captured


def test_launch_appends_tls_flag_when_enabled(monkeypatch):
    """tls=True appends the literal "--tls" token to the argv LIST (never shell=True)."""
    captured = _capture_launch_cmd(monkeypatch, tls=True)
    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert "--tls" in cmd
    # Forwarded as a literal token, never a joined shell string.
    assert captured["kwargs"].get("shell", False) is False


def test_launch_omits_tls_flag_by_default(monkeypatch):
    """Without tls (default False), "--tls" is absent from the argv."""
    captured = _capture_launch_cmd(monkeypatch)  # no tls kwarg -> default off
    assert "--tls" not in captured["cmd"]


def test_launch_omits_tls_flag_when_false(monkeypatch):
    """Explicit tls=False also leaves "--tls" out of the argv."""
    captured = _capture_launch_cmd(monkeypatch, tls=False)
    assert "--tls" not in captured["cmd"]


def test_serve_command_has_tls_flag():
    """The serve Click command exposes a --tls flag (is_flag, default off)."""
    params = {p.name: p for p in main.commands["serve"].params}
    assert "tls" in params
    assert params["tls"].is_flag is True
    assert params["tls"].default is False


# --- IAM-05 / D-11: serve --enforce-auth forwards "--enforce-auth" to the child ---


def test_launch_appends_enforce_auth_flag_when_enabled(monkeypatch):
    """enforce_auth=True appends the literal "--enforce-auth" token (never shell=True)."""
    captured = _capture_launch_cmd(monkeypatch, enforce_auth=True)
    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert "--enforce-auth" in cmd
    # Forwarded as a literal token, never a joined shell string (T-07-01).
    assert captured["kwargs"].get("shell", False) is False


def test_launch_omits_enforce_auth_flag_by_default(monkeypatch):
    """Without enforce_auth (default False), "--enforce-auth" is absent from the argv."""
    captured = _capture_launch_cmd(monkeypatch)  # no enforce_auth kwarg -> default off
    assert "--enforce-auth" not in captured["cmd"]


def test_launch_omits_enforce_auth_flag_when_false(monkeypatch):
    """Explicit enforce_auth=False also leaves "--enforce-auth" out of the argv."""
    captured = _capture_launch_cmd(monkeypatch, enforce_auth=False)
    assert "--enforce-auth" not in captured["cmd"]


def test_serve_command_has_enforce_auth_flag():
    """The serve Click command exposes an --enforce-auth flag (is_flag, default off)."""
    params = {p.name: p for p in main.commands["serve"].params}
    assert "enforce_auth" in params
    assert params["enforce_auth"].is_flag is True
    assert params["enforce_auth"].default is False


# --- CTRL-05 / P1-A gap: serve forwards the Phase-17 control-plane flags ----------
# The Rust binary defines --enable-control-plane / --control-token / --control-data-dir
# (config.rs:61-80), but the Python `serve` wrapper never exposed or forwarded them, so
# arming via the documented CLI was impossible (17-UAT P1-A). These pin the passthrough:
# the boolean flag + data-dir go on the argv LIST (like --tls), but the SECRET token is
# forwarded via the child ENV (TENANTLESS_CONTROL_TOKEN) and NEVER placed on argv (so it
# never appears in the process list / shell history).


def test_serve_command_has_control_plane_flags():
    """serve exposes --enable-control-plane (flag), --control-token, and --control-data-dir."""
    params = {p.name: p for p in main.commands["serve"].params}
    assert "enable_control_plane" in params
    assert params["enable_control_plane"].is_flag is True
    assert params["enable_control_plane"].default is False
    assert "control_token" in params
    # The token can come from the environment instead of shell history (secret).
    assert params["control_token"].envvar == "TENANTLESS_CONTROL_TOKEN"
    # Default None so the Rust default applies when omitted.
    assert params["control_token"].default is None
    assert "control_data_dir" in params
    assert params["control_data_dir"].default is None


def test_launch_appends_enable_control_plane_flag_when_enabled(monkeypatch):
    """enable_control_plane=True appends the literal "--enable-control-plane" argv token."""
    captured = _capture_launch_cmd(monkeypatch, enable_control_plane=True)
    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert "--enable-control-plane" in cmd
    # T-07-01: never a joined shell string.
    assert captured["kwargs"].get("shell", False) is False


def test_launch_omits_enable_control_plane_flag_by_default(monkeypatch):
    """Without enable_control_plane (default False), the flag is absent from the argv."""
    captured = _capture_launch_cmd(monkeypatch)
    assert "--enable-control-plane" not in captured["cmd"]


def test_launch_forwards_control_data_dir_on_argv_when_provided(monkeypatch):
    """control_data_dir is forwarded as "--control-data-dir <path>" adjacent argv tokens."""
    captured = _capture_launch_cmd(monkeypatch, control_data_dir="/tmp/control-data")
    cmd = captured["cmd"]
    assert "--control-data-dir" in cmd
    assert cmd[cmd.index("--control-data-dir") + 1] == "/tmp/control-data"


def test_launch_omits_control_data_dir_when_none(monkeypatch):
    """Without control_data_dir (default None), the flag is absent (Rust default applies)."""
    captured = _capture_launch_cmd(monkeypatch)
    assert "--control-data-dir" not in captured["cmd"]


def test_launch_forwards_control_token_via_env_not_argv(monkeypatch):
    """The control token is forwarded through the child ENV, NEVER on argv (secret hygiene)."""
    secret = "super-secret-control-token"
    captured = _capture_launch_cmd(monkeypatch, control_token=secret)
    cmd = captured["cmd"]
    # The secret must NOT appear anywhere on the argv list (no process-list / history leak).
    assert secret not in cmd
    assert "--control-token" not in cmd
    # It IS threaded via the child env under the clap env-var name.
    env = captured["kwargs"]["env"]
    assert env["TENANTLESS_CONTROL_TOKEN"] == secret


def test_launch_omits_control_token_env_when_none(monkeypatch):
    """Without a control token, no TENANTLESS_CONTROL_TOKEN is injected into the child env."""
    captured = _capture_launch_cmd(monkeypatch)
    env = captured["kwargs"]["env"]
    assert "TENANTLESS_CONTROL_TOKEN" not in env
