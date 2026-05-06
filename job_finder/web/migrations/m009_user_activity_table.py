"""Migration 9 — user_activity table for activity analytics and audit trails (INST-01, Phase 16)."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=9,
    description="user_activity table for activity analytics and audit trails (INST-01, Phase 16)",
    sql=[
        """CREATE TABLE IF NOT EXISTS user_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            entity_id TEXT DEFAULT NULL,
            metadata TEXT DEFAULT '{}',
            occurred_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_user_activity_action ON user_activity(action)",
        "CREATE INDEX IF NOT EXISTS idx_user_activity_occurred_at ON user_activity(occurred_at DESC)",
    ],
)
