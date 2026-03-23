"""Thread-safe per-request SQLite connection helper for Flask.

Uses Flask's request context (g object) to maintain one connection per
request/thread. Register close_db with app.teardown_appcontext in create_app().

Usage in Flask app factory:
    from .db_helpers import get_db, close_db
    app.teardown_appcontext(close_db)
"""

import logging
import sqlite3

from flask import g

from job_finder.json_utils import safe_json_load  # noqa: F401 -- re-exported for backward compatibility

logger = logging.getLogger(__name__)


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
