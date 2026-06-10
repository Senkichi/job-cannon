"""Migration 87 — autoheal heal state columns + audit table.

Adds ``heal_attempts`` and ``last_heal_at`` to ``source_health`` so the
heal pipeline can track attempts and enforce backoff.

Creates the ``heal_audit`` table for per-attempt structured logging
(outcome = ``candidate_generated`` | ``validated`` | ``adopted`` |
``rejected:<reason>`` | ``no_provider``).

The ALTER TABLE statements are not ``IF NOT EXISTS``-guardable in SQLite;
the migration runner's version gate ensures this runs exactly once.
See ``migrations/m084_parser_health.py`` for the ``Migration`` wrapper shape.
"""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=87,
    description="autoheal heal state: heal_attempts + last_heal_at + heal_audit table",
    sql=[
        "ALTER TABLE source_health ADD COLUMN heal_attempts INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE source_health ADD COLUMN last_heal_at TEXT",
        """CREATE TABLE IF NOT EXISTS heal_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            surface TEXT NOT NULL,
            outcome TEXT NOT NULL,
            detail TEXT,
            created_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_heal_audit_source ON heal_audit(source, created_at DESC)",
    ],
)
