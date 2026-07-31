"""Shared packaged-or-repo resource resolver.

Runtime code needs two kinds of STATIC project data files at runtime:
``profiles/schema.json`` (analyzer schema validation) and ``sql/001..007_*.sql``
(generator + ``init-db`` migrations). These live at the REPO ROOT — the single
source of truth shared with the Rust crate's ``include_str!`` and several
Dockerfiles / CI path-filters — so they are NOT moved.

The bug this closes: the wheel omitted those files and callers resolved them by
repo-relative ``parents[3]`` paths, so an INSTALLED wheel raised
``FileNotFoundError`` on every schema load and every migration lookup. The fix
has two halves:

1. ``pyproject.toml`` force-include COPIES the repo-root ``sql/`` and
   ``profiles/schema.json`` INTO the wheel under the package tree
   (``tenantless/sql/…`` and ``tenantless/profiles/schema.json``).
2. This resolver looks them up PACKAGED-FIRST (the installed wheel), then falls
   back to the repo root (editable dev, where there is no ``src/tenantless/sql/``).

Both return types — a :class:`~importlib.abc.Traversable` (importlib.resources)
and a :class:`~pathlib.Path` — expose ``.is_file()`` and ``.read_text(...)``, so
callers keep their existing guards regardless of which branch resolved the path.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from typing import Any

# Kept as a module-level import so tests can monkeypatch ``_resources.files`` to
# exercise the packaged branch without a real installed wheel.
__all__ = ["resource_path", "files"]


def resource_path(*parts: str) -> Any:
    """Resolve a bundled resource by its ``sql``/``profiles`` path parts.

    Packaged-first, repo-root fallback:

    1. Try the PACKAGED location ``files("tenantless").joinpath(*parts)`` and
       return it if ``.is_file()`` is True (installed wheel — force-include put
       ``sql/`` and ``profiles/schema.json`` under the ``tenantless`` package).
    2. Otherwise return the repo-root fallback
       ``Path(__file__).resolve().parents[2].joinpath(*parts)`` — NOTE
       ``parents[2]`` because ``src/tenantless/_resources.py`` is one level
       shallower than ``analyzer/schema_validate.py``:
       ``parents[0]=tenantless``, ``[1]=src``, ``[2]=repo root``.

    The fallback Path is returned UNCONDITIONALLY (not existence-asserted) so
    callers keep their own ``.is_file()`` guard — the generator's ``ensure_*``
    twins depend on a False ``.is_file()`` to detect the docker-initdb-only
    deployment where no bundled ``sql/`` exists on disk.

    ``parts`` are STATIC code literals (fixed sql filenames / ``"profiles"``,
    ``"schema.json"``), never user/profile input — no path-traversal surface.
    """
    packaged = files("tenantless").joinpath(*parts)
    if packaged.is_file():
        return packaged
    return Path(__file__).resolve().parents[2].joinpath(*parts)
