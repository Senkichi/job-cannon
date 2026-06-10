"""Migration 89 — add ``error_details`` JSON column to ``batch_score_sessions``.

Stores the per-source error messages collected during a sync run as a JSON
array of strings (e.g. ``["imap: Authentication failed", "serpapi: API key
rejected (HTTP 401)"]``).  The existing ``error_msg`` column holds a single
fatal-exception string; ``error_details`` holds the soft per-source errors
that used to be silently dropped.
"""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=89,
    description="add error_details JSON column to batch_score_sessions",
    sql=[
        "ALTER TABLE batch_score_sessions ADD COLUMN error_details TEXT DEFAULT NULL",
    ],
)
