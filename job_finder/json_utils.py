"""JSON deserialization utilities shared across persistence and web layers."""

import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def utc_now_iso() -> str:
    """Return the current UTC time as a naive ISO 8601 string.

    Produces timestamps like '2026-03-23T14:30:00' (no timezone suffix).
    All database timestamps should use this function so the codebase stores
    a consistent UTC baseline rather than mixing local time and UTC.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def safe_json_load(value: str | None, default: Any = None) -> Any:
    """Safely deserialize a JSON string from a SQLite TEXT column.

    Returns default on None, empty string, non-string input, or
    JSONDecodeError/TypeError. The caller controls the default type
    ([] for arrays, {} for objects, None for optional fields).

    Args:
        value: Raw value from SQLite TEXT column. May be None, "", or
               a valid JSON string.
        default: Value to return when deserialization fails. Default is None.

    Returns:
        Deserialized Python object, or default on any failure.
    """
    if not value:
        return default
    if not isinstance(value, str):
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.debug(
            "safe_json_load: failed to parse %r, returning default",
            value[:80] if len(value) > 80 else value,
        )
        return default
