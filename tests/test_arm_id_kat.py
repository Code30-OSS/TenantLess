"""Cross-engine ARM-ID fold known-answer tests (INV-01, D-01/D-02/D-05).

ONE shared corpus (``tests/kat/arm_id_kat.json``) is asserted against BOTH the
Python ``arm_id_key`` (``tenantless.identity``) AND the live-PostgreSQL
``synthetic.arm_id_key`` function, so a drift in EITHER engine fails here. The Rust
``ArmId`` twin asserts the SAME corpus via ``include_str!`` in
``mock-server/src/arm_id.rs``.

The fold is ASCII ``A-Z -> a-z`` and NOTHING else (D-01): non-ASCII bytes, doubled
/ trailing slashes, and percent-encoded text all pass through unchanged. The
Turkish dotted-I and sharp-s rows are load-bearing — they PROVE the contract
diverges from ``str.lower()`` / locale ``lower()`` (which the gate forbids, D-02).

The Python KATs are pure and always run. The PG KATs use the autocommit ``pg_conn``
skip fixture (SP-5) and self-provision the ``synthetic`` schema + ``sql/011``
functions, so they exercise a live PG16 when present and skip cleanly when absent.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tenantless.identity import arm_id_key, ascii_fold

_CORPUS_PATH = Path(__file__).parent / "kat" / "arm_id_kat.json"

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgres://tenantless:tenantless_dev@localhost:5433/tenantless",
)


def _load_corpus() -> list[dict[str, str]]:
    rows = json.loads(_CORPUS_PATH.read_text(encoding="utf-8"))
    assert rows, "KAT corpus must be non-empty"
    return rows


_CORPUS = _load_corpus()

# The two load-bearing divergence rows MUST be present (else the corpus is vacuous
# against str.lower()): a Turkish dotted-I whose i-dot expansion / an ß whose
# casefold expansion would differ from the ASCII-only fold.
def test_corpus_contains_divergence_rows():
    inputs = [r["input"] for r in _CORPUS]
    assert any("İ" in i for i in inputs), "corpus must include the Turkish dotted-I row"
    assert any("ß" in i for i in inputs), "corpus must include the sharp-s row"
    assert any("//" in i for i in inputs), "corpus must include a doubled-slash row"
    assert any("%" in i for i in inputs), "corpus must include a percent-encoded row"


@pytest.mark.parametrize("row", _CORPUS, ids=[r["input"] for r in _CORPUS])
def test_python_arm_id_key_matches_corpus(row):
    """Python ``arm_id_key`` folds every corpus row byte-for-byte to its key."""
    assert arm_id_key(row["input"]) == row["key"]


def test_python_arm_id_key_is_ascii_fold():
    """``arm_id_key(id) == ascii_fold(id)`` (D-28: whole-id wrapper over the primitive)."""
    for row in _CORPUS:
        assert arm_id_key(row["input"]) == ascii_fold(row["input"])


def test_python_fold_never_uses_str_lower():
    """The Turkish-I / sharp-s rows prove the fold is NOT ``str.lower()``."""
    assert ascii_fold("/İSTANBUL") != "/İSTANBUL".lower()
    assert ascii_fold("/İSTANBUL") == "/İstanbul"
    # str.lower() leaves ß, but the point is the fold must NOT casefold-expand it.
    assert ascii_fold("/Straße") == "/straße"


# --------------------------------------------------------------------------- #
# Live-PostgreSQL KATs — self-provisioning, skip cleanly when PG is absent
# --------------------------------------------------------------------------- #
@pytest.fixture
def pg_conn():
    """Yield an autocommit psycopg connection with sql/011 applied, or skip.

    ``autocommit=True`` is REQUIRED (SP-5): a non-autocommit read holds ACCESS
    SHARE and would deadlock the schema-ensure DDL. Self-provisions the
    ``synthetic`` schema + the ``sql/011`` fold functions so the corpus can be
    evaluated against a freshly-created (possibly empty) database.
    """
    psycopg = pytest.importorskip("psycopg")
    try:
        conn = psycopg.connect(DATABASE_URL, connect_timeout=3, autocommit=True)
    except Exception as exc:  # noqa: BLE001 - any connection failure -> skip
        pytest.skip(f"Postgres on 5433 unavailable: {exc}")
    try:
        from tenantless.generator import writer

        conn.execute("CREATE SCHEMA IF NOT EXISTS synthetic")
        sql_path = writer.resource_path("sql", "011_arm_id_key.sql")
        conn.execute(sql_path.read_text(encoding="utf-8"))
        yield conn
    finally:
        conn.close()


def test_pg_arm_id_key_matches_corpus(pg_conn):
    """Live ``synthetic.arm_id_key`` folds every corpus row identically to the key."""
    with pg_conn.cursor() as cur:
        for row in _CORPUS:
            cur.execute("SELECT synthetic.arm_id_key(%s)", (row["input"],))
            got = cur.fetchone()[0]
            assert got == row["key"], f"PG arm_id_key({row['input']!r}) = {got!r}"


def test_pg_ascii_fold_matches_corpus(pg_conn):
    """Live ``synthetic.ascii_fold`` agrees with the whole-id wrapper on the corpus."""
    with pg_conn.cursor() as cur:
        for row in _CORPUS:
            cur.execute("SELECT synthetic.ascii_fold(%s)", (row["input"],))
            got = cur.fetchone()[0]
            assert got == row["key"], f"PG ascii_fold({row['input']!r}) = {got!r}"


def test_pg_equals_python_for_every_row(pg_conn):
    """The cross-engine proof: PG == Python for every corpus row (D-05)."""
    with pg_conn.cursor() as cur:
        for row in _CORPUS:
            cur.execute("SELECT synthetic.arm_id_key(%s)", (row["input"],))
            pg_val = cur.fetchone()[0]
            assert pg_val == arm_id_key(row["input"]), (
                f"engine divergence on {row['input']!r}: "
                f"PG={pg_val!r} Python={arm_id_key(row['input'])!r}"
            )


def test_pg_functions_are_immutable_strict(pg_conn):
    """``ascii_fold`` / ``arm_id_key`` are IMMUTABLE STRICT (so they are indexable)."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT p.proname, p.provolatile, p.proisstrict "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'synthetic' AND p.proname IN ('ascii_fold', 'arm_id_key') "
            "ORDER BY p.proname"
        )
        rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    assert set(rows) == {"arm_id_key", "ascii_fold"}, rows
    for name, (volatile, strict) in rows.items():
        assert volatile == "i", f"{name} must be IMMUTABLE (provolatile='i'), got {volatile!r}"
        assert strict is True, f"{name} must be STRICT"
