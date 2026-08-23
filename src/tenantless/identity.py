"""Canonical ARM-ID identity fold (INV-01, D-01/D-02/D-28).

The single documented normalization contract, shared byte-for-byte with the Rust
``ArmId`` newtype (``mock-server/src/arm_id.rs``) and the PostgreSQL
``synthetic.ascii_fold`` / ``synthetic.arm_id_key`` functions
(``sql/011_arm_id_key.sql``). All three engines are pinned identical by the shared
KAT corpus (``tests/kat/arm_id_kat.json``).

Two semantic layers (D-28):

* :func:`ascii_fold` — the low-level primitive: map ASCII ``A-Z -> a-z`` and
  NOTHING else. Non-ASCII bytes, doubled / trailing slashes, and percent-encoded
  text all pass through UNCHANGED. Used for identity COMPONENTS (resource-group
  names, resource names, provider / type segments).
* :func:`arm_id_key` — ``arm_id_key(id) == ascii_fold(id)`` — the whole-ID wrapper,
  used whenever the value is a COMPLETE ARM id (the single identity for equality /
  collision / lookup / dedup / ownership).

Implemented via :meth:`str.translate` over a fixed 26-character table — explicitly
NOT :meth:`str.lower` (D-02). ``str.lower()`` and locale ``lower()`` diverge on
Turkish dotted-I, sharp-s, and across Unicode versions; the ASCII-only translate
table is deterministic across Rust / Python / PostgreSQL and OSes.
"""

from __future__ import annotations

# A fixed ASCII A-Z -> a-z translation table. Only these 26 code points are
# remapped; EVERY other character (all non-ASCII, slashes, percent-encoding,
# digits, punctuation) is left untouched. This is intentionally NOT str.lower().
_ASCII_FOLD_TABLE = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "abcdefghijklmnopqrstuvwxyz",
)


def ascii_fold(text: str) -> str:
    """Fold ASCII ``A-Z`` to ``a-z``; leave every other character unchanged.

    The low-level identity primitive (D-01/D-28). Byte-identical to Rust
    ``str::to_ascii_lowercase`` and PostgreSQL ``translate($1, 'A..Z', 'a..z')``.
    """
    return text.translate(_ASCII_FOLD_TABLE)


def arm_id_key(rid: str) -> str:
    """Return the canonical identity ``key`` for a COMPLETE ARM id (D-01/D-28).

    ``arm_id_key(id) == ascii_fold(id)``. ``key`` is the single identity used for
    equality, collision, lookup, dedup, and ownership; the ``raw`` id preserves the
    original wire casing and is served verbatim (D-25, owned by the write path).
    """
    return ascii_fold(rid)
