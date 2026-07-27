"""Profile schema validation against ``profiles/schema.json``.

Uses the Draft 2020-12 jsonschema validator with the format checker enabled so
``extracted_at`` is checked against ``format: date-time``. Raises on any error,
including stray keys (the schema sets ``additionalProperties: false`` throughout).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

# profiles/schema.json relative to the repo root:
# src/tenantless/analyzer/schema_validate.py -> parents[3] == repo root
_SCHEMA_PATH = Path(__file__).resolve().parents[3] / "profiles" / "schema.json"


@lru_cache(maxsize=1)
def _load_schema() -> dict[str, Any]:
    with _SCHEMA_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


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
