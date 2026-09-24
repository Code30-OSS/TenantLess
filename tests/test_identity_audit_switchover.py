"""The fail-loud ARM-ID identity audit (D-04) runs only DURING the identity switchover.

Before the cutover (the overlay identity CHECK still derives from ``lower(id)``, or a
retired ``lower()`` identity index still exists) a non-ASCII id whose locale ``lower()``
differs from ``synthetic.arm_id_key`` is a real divergence, and ``generate`` / ``init-db``
must refuse it, naming the id. After the cutover the same id is a legitimate identity:
``init-db`` and ``generate --force`` must accept the estate instead of bricking it.

Each test runs in its OWN scratch database (created and dropped here) on the server
``DATABASE_URL`` points at, so nothing shared is truncated. The scratch database uses the
ICU root collation when available, where ``lower('À') = 'à'`` — the divergence the audit
exists to catch. DB-backed + marked ``integration`` (live PG16).
"""

from __future__ import annotations

import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import pytest
from click.testing import CliRunner

from tenantless.cli import main
from tenantless.generator import writer

pytestmark = pytest.mark.integration

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgres://tenantless:tenantless_dev@localhost:5433/tenantless",
)

_TENANT = str(uuid.UUID(int=0xA5D1))
_SUB = str(uuid.UUID(int=0xA5D10001))
_RG = "rg-switchover"
# Escaped so the source stays ASCII-clean: an accented capital the ASCII-only key keeps.
_DIVERGENT_ID = f"/subscriptions/{_SUB}/resourceGroups/{_RG}/providers/x/y/ÀLPHA"


def _with_db(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{name}"))


@pytest.fixture
def scratch_url():
    psycopg = pytest.importorskip("psycopg")
    try:
        admin = psycopg.connect(DATABASE_URL, connect_timeout=3, autocommit=True)
    except Exception as exc:  # noqa: BLE001 - any connection failure -> skip
        pytest.skip(f"Postgres unavailable: {exc}")
    name = f"tl_switchover_{uuid.uuid4().hex[:10]}"
    try:
        try:
            admin.execute(
                f'CREATE DATABASE "{name}" TEMPLATE template0 ENCODING \'UTF8\' '
                "LOCALE_PROVIDER icu ICU_LOCALE 'und' LOCALE 'C'"
            )
        except Exception:  # noqa: BLE001 - no ICU: fall back to the server default
            admin.execute(f'CREATE DATABASE "{name}"')
        url = _with_db(DATABASE_URL, name)
        with psycopg.connect(url, autocommit=True) as probe:
            diverges = probe.execute("SELECT lower(%s) <> %s", (_DIVERGENT_ID,
                                                                 _DIVERGENT_ID)).fetchone()[0]
        if not diverges:
            pytest.skip("this server's collation does not fold the non-ASCII capital")
        yield url
    finally:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


def _init_db(url: str):
    return CliRunner().invoke(main, ["init-db", "--database-url", url])


def _generate(url: str, monkeypatch):
    monkeypatch.setattr(writer, "DATABASE_URL", url)
    return CliRunner().invoke(
        main,
        ["generate", "--profile", "small", "--seed", "11", "--subscriptions", "1",
         "--resources", "10", "--force", "--no-violations", "--no-cross-sub",
         "--no-identity", "--jobs", "1"],
    )


def _seed_divergent(url: str) -> None:
    import psycopg

    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO synthetic.tenant (tenant_id, display_name, profile_version, "
            "scale_params) VALUES (%s, 'sw', 'v0', '{}'::jsonb) "
            "ON CONFLICT (tenant_id) DO NOTHING",
            (_TENANT,),
        )
        conn.execute(
            "INSERT INTO synthetic.subscriptions (subscription_id, tenant_id, display_name, "
            "archetype) VALUES (%s, %s, 'sw', 'test') ON CONFLICT DO NOTHING",
            (_SUB, _TENANT),
        )
        conn.execute(
            "INSERT INTO synthetic.resources "
            "(id, subscription_id, resource_group_name, name, type, location) "
            "VALUES (%s, %s, %s, 'n', 'x/y', 'eastus')",
            (_DIVERGENT_ID, _SUB, _RG),
        )


def _resource_ids(url: str) -> list[str]:
    import psycopg

    with psycopg.connect(url, autocommit=True) as conn:
        return [r[0] for r in conn.execute(
            "SELECT id FROM synthetic.resources"  # SYNRES-ALLOW[test-oracle]: estate contents before/after a refused generate
        ).fetchall()]


def _output(result) -> str:
    return result.output + (str(result.exception) if result.exception else "")


def test_post_cutover_non_ascii_estate_is_accepted(scratch_url, monkeypatch):
    # Given a fully cut-over volume (init-db dropped the legacy lower() indexes and
    # re-derived the overlay CHECK) holding a legitimate non-ASCII id
    first = _init_db(scratch_url)
    assert first.exit_code == 0, _output(first)
    import psycopg

    with psycopg.connect(scratch_url, autocommit=True) as conn:
        assert not writer.arm_id_identity_switchover_active(conn)
    _seed_divergent(scratch_url)

    # When init-db runs again, Then it does not audit (and so does not refuse)
    again = _init_db(scratch_url)
    assert again.exit_code == 0, _output(again)

    # When generate --force replaces the estate, Then it succeeds and replaces it
    gen = _generate(scratch_url, monkeypatch)
    assert gen.exit_code == 0, _output(gen)
    assert _DIVERGENT_ID not in _resource_ids(scratch_url)


def test_pre_cutover_divergence_still_fails_loud(scratch_url, monkeypatch):
    # Given a volume mid-switchover: a legacy lower() identity index still exists
    first = _init_db(scratch_url)
    assert first.exit_code == 0, _output(first)
    _seed_divergent(scratch_url)
    import psycopg

    with psycopg.connect(scratch_url, autocommit=True) as conn:
        conn.execute("CREATE INDEX idx_res_lower_id ON synthetic.resources (lower(id))")
        assert writer.arm_id_identity_switchover_active(conn)

    # When init-db runs, Then the audit refuses, naming the divergent id
    refused = _init_db(scratch_url)
    assert refused.exit_code != 0
    assert _DIVERGENT_ID in _output(refused)

    # When generate --force runs, Then it refuses BEFORE replacing anything
    gen = _generate(scratch_url, monkeypatch)
    assert gen.exit_code != 0
    assert _DIVERGENT_ID in _output(gen)
    assert _DIVERGENT_ID in _resource_ids(scratch_url)
