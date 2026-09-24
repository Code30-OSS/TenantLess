"""Unit tests for the live-conformance harness plumbing in ``tests/conftest.py``.

These run in the DEFAULT suite (no live infra needed): they pin the non-skip gate and
the dev-tenant isolation guard that the live conformance machine depends on.

Given a gate flag / a database URL, When the harness helper runs, Then it fails or
skips exactly as the contract says.
"""

from __future__ import annotations

import pytest
from _pytest.outcomes import Failed, Skipped

from conftest import (
    CONFORMANCE_DB_ENV,
    REQUIRE_LIVE_ENV,
    conformance_database_url,
    refuse_dev_tenant,
    require_live,
)


def test_require_live_fails_when_the_gate_flag_is_set(monkeypatch):
    # Given the CI gate flag is set
    monkeypatch.setenv(REQUIRE_LIVE_ENV, "1")
    # When live infra is reported unavailable, Then the test FAILS (never skips)
    with pytest.raises(Failed, match="live infra required"):
        require_live("postgres down")


def test_require_live_skips_when_the_gate_flag_is_unset(monkeypatch):
    # Given no gate flag (local dev)
    monkeypatch.delenv(REQUIRE_LIVE_ENV, raising=False)
    # When live infra is unavailable, Then the test skips cleanly
    with pytest.raises(Skipped, match="live infra unavailable"):
        require_live("postgres down")


@pytest.mark.parametrize("value", ["0", "", "true-ish", "yes"])
def test_require_live_only_the_exact_flag_value_arms_the_gate(monkeypatch, value):
    monkeypatch.setenv(REQUIRE_LIVE_ENV, value)
    with pytest.raises(Skipped):
        require_live("x")


def test_conformance_url_defaults_to_a_dedicated_database(monkeypatch):
    # Given no override, When the URL is resolved, Then it names a dedicated database,
    # never the shared dev tenant database.
    monkeypatch.delenv(CONFORMANCE_DB_ENV, raising=False)
    url = conformance_database_url()
    assert url.rsplit("/", 1)[-1] == "tenantless_conformance"


def test_conformance_url_honours_the_override(monkeypatch):
    monkeypatch.setenv(CONFORMANCE_DB_ENV, "postgres://u:p@127.0.0.1:5999/iso")
    assert conformance_database_url() == "postgres://u:p@127.0.0.1:5999/iso"


@pytest.mark.parametrize(
    "url",
    [
        "postgres://tenantless:tenantless_dev@localhost:5433/tenantless",
        "postgres://tenantless:tenantless_dev@127.0.0.1:5433/tenantless?sslmode=disable",
    ],
)
def test_refuse_dev_tenant_rejects_the_dev_database(monkeypatch, url):
    # Given a URL that points at the shared dev tenant database
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # When the guard runs, Then it fails hard (a full reset would wipe the dev tenant)
    with pytest.raises(Failed, match="dev tenant"):
        refuse_dev_tenant(url)


def test_refuse_dev_tenant_rejects_the_configured_database_url(monkeypatch):
    # Given the conformance URL equals the suite-wide DATABASE_URL
    monkeypatch.setenv("DATABASE_URL", "postgres://a:b@127.0.0.1:5999/shared")
    with pytest.raises(Failed, match="dev tenant"):
        refuse_dev_tenant("postgres://a:b@127.0.0.1:5999/shared")


def test_refuse_dev_tenant_accepts_an_isolated_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://a:b@127.0.0.1:5433/tenantless")
    # Does not raise.
    refuse_dev_tenant("postgres://a:b@127.0.0.1:5448/tenantless_conformance")


def test_conformance_marker_is_deselected_by_default(pytestconfig):
    # Given the project pytest config, Then the default addopts deselect the live
    # conformance machine alongside integration + scale.
    markers = "\n".join(pytestconfig.getini("markers"))
    assert "conformance:" in markers
    addopts = " ".join(pytestconfig.getini("addopts"))
    assert "not conformance" in addopts
