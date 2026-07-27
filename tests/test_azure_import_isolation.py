"""Core-install import-isolation gate for the ARG scan path (T-12-03).

The direct-tenant-scan path must stay runnable on the BARE core install: the
pure materializer, the seam Protocol, and the profile orchestrator may pull in
``duckdb``/``polars``/``orjson`` but must NEVER drag any ``azure-*`` package into
``sys.modules``. ``azure-identity`` / ``azure-mgmt-resourcegraph`` live behind the
optional ``[azure]`` extra and are imported only by ``arg_client`` (Wave 1's
guarded boundary), never by the modules under test here.

The check runs in a FRESH subprocess interpreter so it observes a clean
``sys.modules`` (a truly fresh import, not the suite's already-warmed cache) AND
leaves this process's import state byte-for-byte unchanged — popping/re-importing
``profile`` in-process would swap in a new module object and break sibling tests
that hold references to the original. Must pass under a plain ``uv run pytest``
(no ``--extra azure``).
"""

from __future__ import annotations

import subprocess
import sys

_TARGETS = (
    "tenantless.analyzer.azure.materialize",
    "tenantless.analyzer.azure.executor",
    "tenantless.analyzer.profile",
)

# Imports the targets in a clean interpreter, then prints any leaked azure*
# module keys. Exit 0 + empty stdout == no leak.
_PROBE = (
    "import sys\n"
    f"for name in {_TARGETS!r}:\n"
    "    __import__(name)\n"
    "leaked = [m for m in sys.modules if m == 'azure' or m.startswith('azure.')]\n"
    "print('\\n'.join(leaked))\n"
)


def test_pure_path_imports_pull_no_azure_module():
    """Fresh-importing materialize/executor/profile leaks no azure* module."""
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"probe failed to import the pure path:\n{proc.stderr}"
    )
    leaked = [line for line in proc.stdout.splitlines() if line.strip()]
    assert not leaked, f"core-install import pulled azure-*: {leaked}"
