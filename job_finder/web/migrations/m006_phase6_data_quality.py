"""Migration 6 — Phase 6 data quality: batch_score_sessions, merge_log, locations_raw, retroactive first_seen fix."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=6,
    description="Phase 6 data quality: batch_score_sessions, merge_log, locations_raw, retroactive first_seen fix",
    sql=[
        # batch_score_sessions: tracks async batch scoring runs (for progress UI)
        """CREATE TABLE IF NOT EXISTS batch_score_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            total INTEGER NOT NULL DEFAULT 0,
            scored INTEGER NOT NULL DEFAULT 0,
            skipped INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL,
            finished_at TEXT DEFAULT NULL,
            error_msg TEXT DEFAULT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_batch_score_sessions_status ON batch_score_sessions(status)",
        # merge_log: audit trail for deduplication merges
        """CREATE TABLE IF NOT EXISTS merge_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_key TEXT NOT NULL,
            merged_key TEXT NOT NULL,
            merge_source TEXT NOT NULL DEFAULT 'migration',
            merged_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_merge_log_canonical ON merge_log(canonical_key)",
        # New columns on jobs for Phase 6 enrichment
        "ALTER TABLE jobs ADD COLUMN locations_raw TEXT DEFAULT NULL",
        "ALTER TABLE jobs ADD COLUMN description_reformatted INTEGER DEFAULT 0",
        # Retroactive first_seen fix: update Gmail jobs' first_seen to the earliest
        # email_parse_log timestamp where the date matches (best-effort; per Research
        # Pitfall 5, matching is by date proximity since email_parse_log uses run-level
        # IDs not per-message IDs). SerpAPI jobs have no email date and are unaffected.
        """UPDATE jobs SET first_seen = (
            SELECT MIN(epl.processed_at)
            FROM email_parse_log epl
            WHERE DATE(epl.processed_at) = DATE(jobs.first_seen)
        ) WHERE EXISTS (
            SELECT 1 FROM email_parse_log epl
            WHERE DATE(epl.processed_at) = DATE(jobs.first_seen)
        )""",
    ],
)
