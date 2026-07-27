"""Privacy layer (source-agnostic): min-aggregation + denylist scan.

Enforces the HARD data-boundary constraint from CLAUDE.md:
    "Statistical profiles must contain zero real tenant identifiers -- enforced
    by automated denylist scan and minimum aggregation thresholds."

Two independent controls:

1. ``merge_min_buckets`` -- drops any statistical bucket observed fewer than
   ``min_bucket_size`` times BEFORE normalization, so no low-count value can
   fingerprint a real tenant.
2. ``scan_denylist`` -- recursively walks the assembled profile and raises
   loudly if any real-identifier string appears in any key or string value.

Neither function imports ``duckdb``; both operate on Polars frames / plain
Python dict-list structures so they are reusable by the Phase 6 reader.
"""

from __future__ import annotations

from typing import Any, Iterable

import polars as pl


class DenylistLeakError(ValueError):
    """Raised when a denylisted real identifier appears in the profile output.

    This is the loud-failure signal for the privacy acceptance gate.
    """


def merge_min_buckets(
    counts_frame: pl.DataFrame,
    min_bucket_size: int,
    *,
    count_col: str = "count",
) -> pl.DataFrame:
    """Drop buckets with ``count < min_bucket_size`` before any normalization.

    A type seen 4 times with ``min_bucket_size=5`` must not survive. The frame
    is returned with the same columns, filtered to surviving buckets only.
    """
    if counts_frame.is_empty():
        return counts_frame
    return counts_frame.filter(pl.col(count_col) >= min_bucket_size)


def scan_denylist(obj: Any, denylist_terms: Iterable[str]) -> None:
    """Recursively walk ``obj`` and raise if any denylist term leaks.

    Checks every dict key and every string value for a denylist term appearing
    as a whole identifier TOKEN (case-sensitive). A term matches only when it is
    not flanked by an identifier character (``[A-Za-z0-9_]``) on either side, so
    a real identifier embedded in a value (``/subscriptions/<uuid>/...``,
    ``.../virtualmachines``) is caught, while a term that is merely a substring of
    a larger word is NOT a false positive: ``subscriptions`` does not trip on the
    structural key ``total_subscriptions``, nor ``ers`` on ``providers``.

    Numbers, booleans and ``None`` are inert. Empty / whitespace-only terms are
    ignored so an empty denylist file never trips the gate.
    """
    # On the live-scan path the auto-derived denylist can hold 100K-500K tenant
    # identifiers, but the profile OUTPUT is small. So we INVERT the search
    # (P2-c): keep the denylist as a plain ``set`` -- O(terms) memory, just the
    # strings, NO per-term trie/automaton (which is O(nodes) and blows up to GiB
    # on realistic unique names) -- and test each token-boundary-valid substring
    # of each output string for membership. Memory is ~O(terms + output) and
    # match cost is independent of the term count (set membership is O(1)).
    terms = {t for t in denylist_terms if t and t.strip()}
    if not terms:
        return
    matcher = (terms, max(len(t) for t in terms))
    _walk(obj, matcher)


def _is_ident_char(ch: str) -> bool:
    """True if ``ch`` is an identifier character (letter, digit, or underscore)."""
    return ch.isalnum() or ch == "_"


def _raise_leak(length: int) -> None:
    """Raise the WR-02 length-only leak error (never echoes the term value)."""
    raise DenylistLeakError(
        "A denylisted real identifier leaked into the profile output. The "
        "value is withheld here because it is a real tenant identifier "
        f"(length {length})."
    )


def _check_string(s: str, terms: list[str]) -> None:
    """REFERENCE matcher -- the exact token-boundary semantics spec (P2-c).

    Kept as the differential-test oracle for the inverted set-membership path
    used by :func:`scan_denylist`. Not called in production;
    ``tests/test_privacy.py`` asserts the two agree (raise-vs-not) across random
    inputs.
    """
    for term in terms:
        # Fast substring pre-filter (C-level); only candidates pay for the
        # token-boundary check below.
        start = s.find(term)
        if start == -1:
            continue
        n = len(term)
        while start != -1:
            before = s[start - 1] if start > 0 else ""
            after = s[start + n] if start + n < len(s) else ""
            if not _is_ident_char(before) and not _is_ident_char(after):
                _raise_leak(len(term))
            start = s.find(term, start + 1)


def _scan_string(s: str, matcher: tuple) -> None:
    """Enumerate token-boundary-valid substrings of ``s``; raise on a set hit.

    Inverted matcher (P2-c memory fix): ``matcher`` is ``(term_set, max_len)``.
    Match semantics are byte-identical to :func:`_check_string` -- a candidate
    substring ``s[start:end]`` is a leak only when it is in ``term_set`` AND not
    flanked by an identifier char on either side (``start==0`` / ``end==len(s)``
    count as boundaries, matching the reference's empty-flank handling).

    A valid START is index 0 or any index right after a non-identifier char; a
    valid END (exclusive) is ``len(s)`` or any index whose char is a
    non-identifier. Candidate length is capped at ``max_len`` (the longest term):
    a longer substring cannot be in the set, so it is never sliced -- keeping the
    work bounded by ``output_len x max_len`` and the memory at O(terms + output).
    """
    term_set, max_len = matcher
    n = len(s)
    for start in range(n):
        # Skip starts that are NOT at a token boundary.
        if start > 0 and _is_ident_char(s[start - 1]):
            continue
        limit = min(n, start + max_len)
        for end in range(start + 1, limit + 1):
            # Only consider ends that are at a token boundary.
            if end < n and _is_ident_char(s[end]):
                continue
            if s[start:end] in term_set:
                _raise_leak(end - start)


def _walk(node: Any, matcher: tuple) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str):
                _scan_string(key, matcher)
            _walk(value, matcher)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _walk(item, matcher)
    elif isinstance(node, str):
        _scan_string(node, matcher)
    # ints / floats / bool / None: inert
