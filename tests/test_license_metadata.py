"""PLAT-03 / Pitfall 3+6: a real wheel must carry the Apache-2.0 license metadata,
the LICENSE file, and the bundled named profiles.

This builds an actual wheel with ``uv build`` (not a repo-checkout import, which would
mask a packaging bug), unzips it, and asserts:

  (a) the wheel METADATA declares Apache-2.0 (PEP 639 SPDX ``License-Expression`` or the
      legacy ``License`` / classifier form),
  (b) the wheel contains a LICENSE entry (PEP 639 ``.dist-info/licenses/LICENSE``),
  (c) the wheel contains ``profiles/enterprise.json`` + ``profiles/small.json``
      (confirming the Plan 03 bundled data ships — Pitfall 3),
  (d) the ``uv build`` output emits no license-expression deprecation/error (Pitfall 6).

NOTE: the wheel paths below match on ``profiles/`` and ``LICENSE`` regardless of the
top-level package name (now ``tenantless`` after the Plan 05 rename), so this test is
package-name-agnostic.

A full ``uv build`` (Python wheel only) runs in a few seconds, comfortably inside the
default suite budget — no opt-in marker needed.
"""

from __future__ import annotations

import glob
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

# tests/test_license_metadata.py -> parents[1] == repo root
REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory) -> tuple[Path, str]:
    """Build a wheel into a temp dist dir with ``uv build``; return (wheel_path, stderr+stdout).

    Skips cleanly if ``uv`` is not on PATH (keeps DB-less / tool-less CI green).
    """
    if not _has_uv():
        pytest.skip("uv not available on PATH")

    dist_dir = tmp_path_factory.mktemp("dist")
    proc = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dist_dir)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    assert proc.returncode == 0, f"uv build failed:\n{combined}"

    wheels = sorted(glob.glob(str(dist_dir / "*.whl")))
    assert wheels, f"uv build produced no wheel in {dist_dir}:\n{combined}"
    return Path(wheels[-1]), combined


def _has_uv() -> bool:
    try:
        subprocess.run(
            ["uv", "--version"], capture_output=True, text=True, check=True
        )
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def _read_metadata(wheel: Path) -> str:
    with zipfile.ZipFile(wheel) as zf:
        meta_names = [
            n for n in zf.namelist() if n.endswith(".dist-info/METADATA")
        ]
        assert meta_names, f"no METADATA in wheel: {zf.namelist()}"
        return zf.read(meta_names[0]).decode("utf-8")


def test_wheel_metadata_declares_apache_2_0(built_wheel) -> None:
    """(a) The wheel METADATA must declare the Apache-2.0 license."""
    wheel, _ = built_wheel
    metadata = _read_metadata(wheel)
    assert "Apache-2.0" in metadata or "Apache Software License" in metadata, (
        f"Apache-2.0 license not found in wheel METADATA:\n{metadata}"
    )


def test_wheel_contains_license_file(built_wheel) -> None:
    """(b) The wheel must bundle a LICENSE entry."""
    wheel, _ = built_wheel
    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
    assert any(n.rsplit("/", 1)[-1] == "LICENSE" for n in names), (
        f"no LICENSE entry in wheel:\n{names}"
    )


def test_wheel_bundles_named_profiles(built_wheel) -> None:
    """(c) Pitfall 3: the bundled named profiles must ship in the wheel."""
    wheel, _ = built_wheel
    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
    for profile in ("profiles/enterprise.json", "profiles/small.json"):
        assert any(n.endswith(profile) for n in names), (
            f"bundled {profile} missing from wheel (Pitfall 3):\n{names}"
        )


def test_uv_build_emits_no_license_deprecation(built_wheel) -> None:
    """(d) Pitfall 6: the build must not emit a license-expression deprecation/error."""
    _, build_output = built_wheel
    lowered = build_output.lower()
    assert "deprecat" not in lowered or "license" not in lowered, (
        f"uv build emitted a license-related deprecation:\n{build_output}"
    )
    # PEP 639 SPDX form on a recent hatchling should not warn about the expression.
    assert "license expression" not in lowered, (
        f"uv build warned about the license expression (Pitfall 6):\n{build_output}"
    )
