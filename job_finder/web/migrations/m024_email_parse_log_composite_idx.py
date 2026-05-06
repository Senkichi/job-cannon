"""Migration 24 — composite index on email_parse_log(sender, processed_at) for per-message Gmail dedup query."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=24,
    description="composite index on email_parse_log(sender, processed_at) for per-message Gmail dedup query",
    sql=[
        "CREATE INDEX IF NOT EXISTS idx_email_parse_log_sender_processed_at"
        " ON email_parse_log(sender, processed_at)",
    ],
)
