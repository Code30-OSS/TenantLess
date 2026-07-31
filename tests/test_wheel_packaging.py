"""Wheel-packaging + shared resource-resolver + init-db honesty.

Context: the built wheel omitted ``profiles/schema.json`` and ``sql/*.sql``, and
runtime code resolved them by repo-relative ``parents[3]`` paths, so an installed
wheel raised ``FileNotFoundError`` on every ``load_profile`` (schema validation)
and every generator/init-db migration lookup — while ``init-db`` still printed
"Provisioned schema 001..007" (false success).

These tests are DB-free (mirror ``tests/test_cli_generate_telemetry.py``): the
Postgres writer seam (``open_writer`` + the five ``ensure_*`` migrations) is
monkeypatched so nothing touches a live database. The wheel-contents test (G)
inspects a build artifact and skips cleanly until a wheel is built (Task 3).
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from tenantless import _resources
from tenantless.analyzer import schema_validate
from tenantless.cli import main
from tenantless.generator import writer as writer_mod


# --------------------------------------------------------------------------- #
# Test A — resolver: packaged location wins when .is_file() is True
# --------------------------------------------------------------------------- #
def test_resolver_prefers_packaged_when_present(monkeypatch):
    """When ``files("tenantless").joinpath(*parts).is_file()`` is True, the
    packaged Traversable is returned verbatim (installed-wheel branch)."""

    class _FakePackaged:
        # Identity-distinguishable sentinel object.
        marker = "PACKAGED-SENTINEL"

        def is_file(self):
            return True

    packaged = _FakePackaged()

    class _FakeAnchor:
        def joinpath(self, *parts):
            return packaged

    monkeypatch.setattr(_resources, "files", lambda pkg: _FakeAnchor())

    got = _resources.resource_path("sql", "004_cost.sql")
    assert got is packaged, "packaged location must win when .is_file() is True"


# --------------------------------------------------------------------------- #
# Test B — resolver: repo-root fallback in the editable checkout
# --------------------------------------------------------------------------- #
def test_resolver_falls_back_to_repo_root():
    """In the editable checkout there is NO ``src/tenantless/sql/``, so the
    packaged lookup .is_file() is False and the resolver returns a repo-root
    Path that exists."""
    sql = _resources.resource_path("sql", "001_synthetic_tenant.sql")
    assert sql.is_file(), f"expected repo-root sql to exist, got {sql!r}"

    schema = _resources.resource_path("profiles", "schema.json")
    assert schema.is_file(), f"expected repo-root schema.json to exist, got {schema!r}"


# --------------------------------------------------------------------------- #
# Test C — schema_validate resolves schema.json without FileNotFoundError
# --------------------------------------------------------------------------- #
def test_schema_validate_resolves_via_resolver():
    """``_load_schema()`` and ``validate_profile({})`` must resolve schema.json
    via the shared resolver and NOT raise FileNotFoundError."""
    schema_validate._load_schema.cache_clear()
    schema_validate._validator.cache_clear()
    loaded = schema_validate._load_schema()
    assert isinstance(loaded, dict) and loaded, "schema.json must load to a dict"

    # An empty profile is invalid content, but must fail on VALIDATION, never on
    # a missing schema file.
    from jsonschema.exceptions import ValidationError

    try:
        schema_validate.validate_profile({})
    except FileNotFoundError as exc:  # pragma: no cover - the bug we are fixing
        pytest.fail(f"schema resolution raised FileNotFoundError: {exc}")
    except ValidationError:
        pass  # expected — empty dict violates the schema


# --------------------------------------------------------------------------- #
# Test D — writer sql lookups resolve to existing files
# --------------------------------------------------------------------------- #
def test_writer_sql_lookups_resolve_to_existing_files():
    """All 3 base-schema files plus the four twin migrations (004..007) resolve
    to existing ``.is_file()`` resources in the dev checkout."""
    base = writer_mod._base_schema_sql_files()
    assert len(base) == 3
    for p in base:
        assert p.is_file(), f"base schema file missing: {p!r}"

    for name in (
        "004_cost.sql",
        "005_identity.sql",
        "006_drift.sql",
        "007_web_metadata.sql",
    ):
        resolved = _resources.resource_path("sql", name)
        assert resolved.is_file(), f"twin migration missing: {resolved!r}"


# --------------------------------------------------------------------------- #
# DB-free init-db harness
# --------------------------------------------------------------------------- #
@pytest.fixture
def fake_open_writer(monkeypatch):
    """Stub ``writer.open_writer`` to a DB-free context manager."""

    class _FakeConn:
        pass

    @contextlib.contextmanager
    def _fake(*a, **k):
        yield _FakeConn()

    monkeypatch.setattr(writer_mod, "open_writer", _fake)
    return monkeypatch


# --------------------------------------------------------------------------- #
# Test E — init-db honesty: a twin returning False fails, naming the migration
# --------------------------------------------------------------------------- #
def test_init_db_fails_and_names_missing_migration(fake_open_writer):
    monkeypatch = fake_open_writer
    monkeypatch.setattr(writer_mod, "ensure_base_schema", lambda conn: True)
    # The 004 twin reports the file ABSENT (the packaging bug) -> failure.
    monkeypatch.setattr(writer_mod, "ensure_cost_schema", lambda conn: False)
    monkeypatch.setattr(writer_mod, "ensure_identity_schema", lambda conn: True)
    monkeypatch.setattr(writer_mod, "ensure_drift_schema", lambda conn: True)
    monkeypatch.setattr(writer_mod, "ensure_web_metadata_schema", lambda conn: True)

    runner = CliRunner()
    result = runner.invoke(main, ["init-db"])

    assert result.exit_code != 0, "a missing twin migration must exit nonzero"
    combined = (result.output or "") + (result.stderr or "")
    assert "004" in combined, f"missing migration 004 not named: {combined!r}"
    assert "Provisioned schema 001..007" not in combined, (
        "false-success line must not print when a migration is missing"
    )


# --------------------------------------------------------------------------- #
# Test F — init-db success: host-only status line, no DSN/password leak
# --------------------------------------------------------------------------- #
def test_init_db_success_prints_host_only(fake_open_writer):
    monkeypatch = fake_open_writer
    monkeypatch.setattr(writer_mod, "ensure_base_schema", lambda conn: True)
    monkeypatch.setattr(writer_mod, "ensure_cost_schema", lambda conn: True)
    monkeypatch.setattr(writer_mod, "ensure_identity_schema", lambda conn: True)
    monkeypatch.setattr(writer_mod, "ensure_drift_schema", lambda conn: True)
    monkeypatch.setattr(writer_mod, "ensure_web_metadata_schema", lambda conn: True)

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["init-db", "--database-url", "postgres://u:secretpw@example-host:5433/db"],
    )

    assert result.exit_code == 0, f"all-present init-db must succeed: {result.output!r}"
    out = result.output or ""
    assert "example-host" in out, "host-only status line must name the host"
    assert "secretpw" not in out, "password must never be echoed (T-07-02)"
    assert "postgres://u:" not in out, "full DSN must never be echoed (T-07-02)"


# --------------------------------------------------------------------------- #
# Hermetic wheel gate — builds the CURRENT tree and inspects it.
#
# UNMARKED ON PURPOSE: the only registered pytest markers are ``integration`` and
# ``scale``, and pyproject addopts run ``-m 'not integration and not scale'`` — so
# ANY marker would silently deselect these from the release gate ``uv run pytest``,
# reproducing the exact P1b vacuousness bug. These two tests stay in the default
# suite and share the module-scoped ``built_wheel`` fixture so the build cost is
# paid once. They NEVER ``pytest.skip`` — a release packaging gate that can go
# green-by-skip is no gate at all.
# --------------------------------------------------------------------------- #
_EXPECTED_SQL = (
    "001_synthetic_tenant.sql",
    "002_cross_sub_dependencies.sql",
    "003_integrity_and_index.sql",
    "004_cost.sql",
    "005_identity.sql",
    "006_drift.sql",
    "007_web_metadata.sql",
)


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory):
    """Build the CURRENT tree into a fresh tmp dir ONCE; yield the built ``.whl``.

    Tries ``uv build --wheel`` first (uv is always present in CI); if the ``uv``
    executable is absent (FileNotFoundError) falls back to ``python -m build
    --wheel``. If BOTH backends are unavailable, ``pytest.fail`` — NEVER
    ``pytest.skip`` (a silently-skipped packaging gate is the P1b bug). Asserts a
    zero returncode and exactly one produced wheel.
    """
    repo_root = Path(__file__).resolve().parents[1]
    out_dir = tmp_path_factory.mktemp("wheelbuild")

    def _run(cmd):
        return subprocess.run(cmd, capture_output=True, text=True, cwd=str(repo_root))

    proc = None
    try:
        proc = _run(["uv", "build", "--wheel", "--out-dir", str(out_dir)])
    except FileNotFoundError:
        try:
            proc = _run(
                [sys.executable, "-m", "build", "--wheel", "--outdir", str(out_dir)]
            )
        except FileNotFoundError:
            pytest.fail(
                "no wheel build backend (uv/build) available — release gate cannot "
                "verify packaging"
            )
    assert proc.returncode == 0, (
        f"wheel build failed (returncode {proc.returncode}):\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    wheels = sorted(out_dir.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one built wheel, got {wheels}"
    yield wheels[0]


def test_wheel_contents_ship_data_files(built_wheel):
    """The freshly built wheel must physically contain schema.json + all seven sql
    migrations under the package tree — asserted by EXACT namelist membership (not a
    ``00{i}_`` prefix substring), so a truncated / renamed entry fails loudly."""
    namelist = zipfile.ZipFile(built_wheel).namelist()
    required = ["tenantless/profiles/schema.json"] + [
        f"tenantless/sql/{name}" for name in _EXPECTED_SQL
    ]
    missing = [k for k in required if k not in namelist]
    assert not missing, f"wheel {built_wheel} is missing: {missing}\nnamelist={namelist}"


def test_installed_wheel_resolves_data_files(built_wheel, tmp_path):
    """Install the built wheel into a DISPOSABLE venv and prove a DB-free data-file
    resolution: ``resource_path`` finds schema.json + a sql file, and the schema
    loads to a non-empty dict — the site-packages path an installed wheel actually
    uses (the FileNotFoundError bug the force-include fix closed)."""
    venv_dir = tmp_path / "venv"
    create = subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        capture_output=True,
        text=True,
    )
    assert create.returncode == 0, (
        f"venv create failed:\nSTDOUT:\n{create.stdout}\nSTDERR:\n{create.stderr}"
    )
    venvpy = venv_dir / ("Scripts" if os.name == "nt" else "bin") / (
        "python.exe" if os.name == "nt" else "python"
    )
    install = subprocess.run(
        [str(venvpy), "-m", "pip", "install", str(built_wheel)],
        capture_output=True,
        text=True,
    )
    assert install.returncode == 0, (
        f"pip install of the wheel failed:\nSTDOUT:\n{install.stdout}\n"
        f"STDERR:\n{install.stderr}"
    )
    script = (
        "import tenantless._resources as r\n"
        "assert r.resource_path('profiles', 'schema.json').is_file(), 'schema.json unresolved'\n"
        "assert r.resource_path('sql', '001_synthetic_tenant.sql').is_file(), 'sql unresolved'\n"
        "from tenantless.analyzer.schema_validate import _load_schema\n"
        "d = _load_schema()\n"
        "assert isinstance(d, dict) and d, 'schema.json did not load to a non-empty dict'\n"
        "print('RESOLVE-OK')\n"
    )
    check = subprocess.run(
        [str(venvpy), "-c", script],
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, (
        f"installed-wheel data-file resolution failed:\nSTDOUT:\n{check.stdout}\n"
        f"STDERR:\n{check.stderr}"
    )
    assert "RESOLVE-OK" in check.stdout
