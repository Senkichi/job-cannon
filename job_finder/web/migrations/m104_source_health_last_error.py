"""Migration 104 — source_health credential/error surface columns (#436).

Adds ``last_error`` + ``last_error_at`` to ``source_health`` so the Settings-UI
source-health banner has one durable reader for the per-source credential/error
string ingestion already collects (e.g. Thordata's expired-subscription error,
``serpapi: API key rejected (HTTP 401)``). Pure observability; nothing in the
parse hot path reads these.

The ALTER TABLE statements are not ``IF NOT EXISTS``-guardable in SQLite; the
migration runner's version gate ensures this runs exactly once. See
``m087_heal_state.py`` for the ``Migration`` wrapper shape.
"""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=104,
    description="source_health: add last_error + last_error_at for the Settings credential/degraded banner (#436)",
    sql=[
        "ALTER TABLE source_health ADD COLUMN last_error TEXT DEFAULT NULL",
        "ALTER TABLE source_health ADD COLUMN last_error_at TEXT DEFAULT NULL",
    ],
)
