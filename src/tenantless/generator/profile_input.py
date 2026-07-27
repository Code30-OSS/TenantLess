"""Profile INPUT loading + validation (the inverse of the analyzer's output gate).

The analyzer validates the profile it *writes*; the generator validates the
profile it *reads* BEFORE any sampling (V5 input validation — fail fast on a
structurally-drifted profile crossing the untrusted boundary). Validation reuses
the analyzer's :func:`schema_validate.validate_profile`, which checks against
``profiles/schema.json`` (``additionalProperties: false`` throughout).
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from typing import Any

import click
import orjson

from tenantless.analyzer.schema_validate import validate_profile

# PLAT-04: bundled named profiles live in this package. The package argument is
# updated to ``tenantless.profiles`` in Plan 05 when the whole src/ tree moves.
_BUNDLED_PACKAGE = "tenantless.profiles"
_BUNDLED_NAMES = ("enterprise", "small")


def _is_bare_stem(value: str) -> bool:
    """True if ``value`` is a bundled-name stem with NO path syntax (V5).

    Rejects empty strings, anything containing a path separator (``/`` or
    ``\\``), a drive/scheme colon, or a ``..`` segment — i.e. anything that
    importlib.resources' ``joinpath`` could resolve OUT of the profiles package.
    """
    if not value or value in (".", ".."):
        return False
    if "/" in value or "\\" in value or ":" in value:
        return False
    if ".." in value:
        return False
    return True


def resolve_profile(value: str) -> Any:
    """Resolve a ``--profile`` value to a readable source (D-12).

    Resolution order (path-if-exists -> bundled-name -> error):

    1. If ``value`` is an existing file path, return it as a :class:`Path`
       (backward-compatible with ``--profile path/to.json`` usage).
    2. Otherwise treat ``value`` as a bundled profile NAME and look up
       ``files("tenantless.profiles") / f"{value}.json"`` via
       :mod:`importlib.resources` (works after a pip/uv install, not only from a
       repo checkout). Returns the :class:`~importlib.abc.Traversable`.
    3. If neither resolves, raise :class:`click.UsageError` naming the available
       bundled profiles.

    Returns either a :class:`Path` or a Traversable — both expose ``read_bytes``,
    which is all :func:`load_profile` needs.

    V5 (T-08-03-I): the bundled branch joins ONLY a bare stem
    (``f"{value}.json"``). A path-shaped or ``..`` value never matches step 1
    (it does not exist) and is NOT joined into the package in step 2 — so it can
    never traverse out of the profiles package into arbitrary files; it falls
    straight through to the UsageError in step 3.
    """
    # 1. Back-compat: an existing file path wins outright.
    if Path(value).is_file():
        return Path(value)

    # 2. Bundled-name lookup — bare stem ONLY (V5 / T-08-03-I). A value carrying
    #    a path separator or ".." is NOT a bundled name; importlib.resources'
    #    joinpath resolves "../profiles/enterprise.json" against the real
    #    filesystem (traversing OUT of the package), so we must reject any
    #    path-shaped value here and let it fall through to the error branch
    #    (it already failed the Path.is_file() check in step 1).
    if not _is_bare_stem(value):
        raise click.UsageError(
            f"--profile {value!r} is neither an existing file nor a bundled "
            f"profile (available: {', '.join(_BUNDLED_NAMES)})."
        )
    bundled = files(_BUNDLED_PACKAGE).joinpath(f"{value}.json")
    if bundled.is_file():
        return bundled

    # 3. Neither a path nor a known bundled name.
    raise click.UsageError(
        f"--profile {value!r} is neither an existing file nor a bundled "
        f"profile (available: {', '.join(_BUNDLED_NAMES)})."
    )


def load_profile(path: str | Path | Any) -> dict[str, Any]:
    """Read + schema-validate a profile JSON, returning the parsed dict.

    Accepts a filesystem path (``str`` / :class:`Path`) OR any
    :class:`~importlib.abc.Traversable` (e.g. the bundled-profile resource from
    :func:`resolve_profile`) — both expose ``read_bytes()``.

    Raises :class:`jsonschema.exceptions.ValidationError` if the profile does
    not conform to ``profiles/schema.json`` (validated before sampling).
    """
    # A Traversable (importlib.resources) and a Path both expose read_bytes();
    # only a bare str needs wrapping in Path first.
    reader = Path(path) if isinstance(path, (str, Path)) else path
    raw = reader.read_bytes()
    profile = orjson.loads(raw)
    validate_profile(profile)  # V5: validate the untrusted input first
    return profile


def resolve_targets(
    profile: dict[str, Any],
    resources: int | None = None,
    subscriptions: int | None = None,
) -> tuple[int, int]:
    """Resolve (n_subscriptions, n_resources), defaulting from source_stats (D-05).

    ``--resources`` is the primary scale knob (D-04); either target defaults from
    the profile's ``source_stats`` when omitted.
    """
    stats = profile["source_stats"]
    n_subs = (
        int(subscriptions)
        if subscriptions is not None
        else int(stats["total_subscriptions"])
    )
    n_resources = (
        int(resources)
        if resources is not None
        else int(stats["total_resources"])
    )
    return n_subs, n_resources
