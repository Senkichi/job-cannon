"""Migration 118 — submit_attempts ledger for auto-submit audit trail and rate limiting (issue #604)."""

from __future__ import annotations

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=118,
    description="submit_attempts ledger for auto-submit audit trail and rate limiting (issue #604)",
    sql=[
        """CREATE TABLE IF NOT EXISTS submit_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            mechanism TEXT,
            apply_url TEXT,
            target_confidence TEXT,
            outcome TEXT NOT NULL,
            detail TEXT DEFAULT '',
            occurred_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_submit_attempts_job_id ON submit_attempts(job_id)",
        "CREATE INDEX IF NOT EXISTS idx_submit_attempts_occurred_at ON submit_attempts(occurred_at DESC)",
    ],
)
