"""Thread-safe per-request SQLite connection helper for Flask.

Uses Flask's request context (g object) to maintain one connection per
request/thread. Register close_db with app.teardown_appcontext in create_app().

Usage in Flask app factory:
    from .db_helpers import get_db, close_db
    app.teardown_appcontext(close_db)
"""

import copy
import logging
import sqlite3
from contextlib import contextmanager

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

    Thread-safety contract:
        check_same_thread=False is intentional — Flask's g object ensures this
        connection is used only within a single request thread. Background jobs
        (APScheduler, stale_detector) MUST create their own sqlite3.connect()
        calls and MUST NOT share or reference g.db across thread boundaries.
        Violating this contract causes silent data corruption under concurrent load.
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

@contextmanager
def standalone_connection(db_path: str):
    """Context manager for background/CLI sqlite3 connections.

    Sets row_factory=Row and WAL mode. NOT for Flask request handlers (use
    get_db() via g.db instead).

    Usage:
        with standalone_connection(db_path) as conn:
            rows = conn.execute("SELECT ...").fetchall()
            conn.commit()
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # WAL mode ensures concurrent read/write safety for background jobs
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
    finally:
        conn.close()

def get_config_snapshot(app) -> dict:
    """Return a frozen deep copy of JF_CONFIG for use in background threads.

    Background threads (APScheduler jobs) call this at job-start to get an
    immutable snapshot, rather than reading individual keys from the shared
    dict across multiple statements.

    Args:
        app: Flask application instance.

    Returns:
        Deep copy of app.config["JF_CONFIG"], or empty dict if not set.
    """
    return copy.deepcopy(app.config.get("JF_CONFIG", {}))
