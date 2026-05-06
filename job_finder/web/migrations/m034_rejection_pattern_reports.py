"""Migration 34 — rejection_pattern_reports table for zero-LLM-cost mechanical analysis."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=34,
    description="rejection_pattern_reports table for zero-LLM-cost mechanical analysis",
    sql=[
        """CREATE TABLE IF NOT EXISTS rejection_pattern_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_json TEXT NOT NULL,
            period_days INTEGER NOT NULL,
            total_rejections INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )""",
    ],
)
