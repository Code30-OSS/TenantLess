"""Profile schema validation against ``profiles/schema.json``.

Uses the Draft 2020-12 jsonschema validator with the format checker enabled so
``extracted_at`` is checked against ``format: date-time``. Raises on any error,
including stray keys (the schema sets ``additionalProperties: false`` throughout).
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from tenantless._resources import resource_path


@lru_cache(maxsize=1)
def _load_schema() -> dict[str, Any]:
    # Resolve profiles/schema.json via the shared packaged-or-repo resolver
    # (packaged wheel first, repo-root fallback for editable dev). ``.read_text``
    # works on both a Path and an importlib.resources Traversable.
    schema = resource_path("profiles", "schema.json")
    return json.loads(schema.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    schema = _load_schema()
    # format_checker enables date-time format validation on extracted_at.
    return Draft202012Validator(
        schema, format_checker=Draft202012Validator.FORMAT_CHECKER
    )


def validate_profile(profile: dict[str, Any]) -> None:
    """Validate ``profile`` against profiles/schema.json; raise on first error.

    Raises :class:`jsonschema.exceptions.ValidationError` on any violation,
    including ``additionalProperties: false`` (stray keys) and a malformed
    ``extracted_at`` date-time.
    """
    errors = sorted(_validator().iter_errors(profile), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        location = "/".join(str(p) for p in first.absolute_path) or "<root>"
        raise ValidationError(
            f"Profile failed schema validation at {location}: {first.message}"
        )
