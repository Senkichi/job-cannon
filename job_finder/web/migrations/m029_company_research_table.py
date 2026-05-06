"""Migration 29 — company_research async-state table for on-demand company research."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=29,
    description="company_research async-state table for on-demand company research",
    sql=[
        """CREATE TABLE IF NOT EXISTS company_research (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL REFERENCES companies(id),
            status TEXT NOT NULL DEFAULT 'pending',
            research_json TEXT DEFAULT NULL,
            error_msg TEXT DEFAULT NULL,
            requested_at TEXT NOT NULL,
            completed_at TEXT DEFAULT NULL,
            cost_usd REAL DEFAULT 0.0
        )""",
        "CREATE INDEX IF NOT EXISTS idx_company_research_company_id ON company_research(company_id)",
    ],
)
