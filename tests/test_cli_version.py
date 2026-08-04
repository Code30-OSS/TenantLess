"""`tenantless --version` must exist and report the installed package version.

SECURITY.md asks reporters to include the output of `tenantless --version`, so the
command has to actually work and agree with the package metadata. This guards against
the option being dropped or drifting away from the distribution version.
"""

from __future__ import annotations

from importlib import metadata

from click.testing import CliRunner

from tenantless.cli import main


def test_version_option_reports_package_version() -> None:
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0, result.output
    version = metadata.version("tenantless")
    assert version in result.output
    assert "tenantless" in result.output.lower()


def test_version_matches_distribution_metadata() -> None:
    # The version the CLI prints must be the one packaging ships, not a hardcoded string.
    assert metadata.version("tenantless") == "1.1.8"
