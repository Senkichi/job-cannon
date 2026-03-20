"""Thread-safe per-request SQLite connection helper for Flask.

Uses Flask's request context (g object) to maintain one connection per
request/thread. Register close_db with app.teardown_appcontext in create_app().

Usage in Flask app factory:
    from .db_helpers import get_db, close_db
    app.teardown_appcontext(close_db)
"""

import json
import logging
import sqlite3
from typing import Any

from flask import g

logger = logging.getLogger(__name__)


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


def get_db(db_path: str) -> sqlite3.Connection:
    """Return the per-request SQLite connection, creating it if needed.

    The connection uses sqlite3.Row as its row_factory, enabling column
    access by name (e.g., row["title"]) in addition to index.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        An open sqlite3.Connection scoped to the current Flask request context.
    """
    if "db" not in g:
        g.db = sqlite3.connect(db_path, check_same_thread=False)
        g.db.row_factory = sqlite3.Row
    return g.db


def close_db(e=None) -> None:
    """Close the per-request SQLite connection on request teardown.

    Registered as an app teardown handler so Flask calls it automatically
    at the end of each request context.

    Args:
        e: Optional exception from the request context (unused, required by Flask).
    """
    db = g.pop("db", None)
    if db is not None:
        db.close()
