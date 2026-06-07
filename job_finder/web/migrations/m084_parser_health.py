"""Migration 84 — parser auto-heal Phase A: corpus_sample + source_health.

corpus_sample is a per-source rolling buffer of PII-scrubbed raw extractor
inputs plus a snapshot of what the live extractor produced. source_health holds
one current-state row per source for the dashboard DEGRADED surface. Both are
pure observability; nothing reads them in the parse hot path.
"""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=84,
    description="parser auto-heal Phase A: corpus_sample + source_health tables",
    sql=[
        """CREATE TABLE IF NOT EXISTS corpus_sample (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            surface TEXT NOT NULL,
            raw_text TEXT NOT NULL,
            output_json TEXT NOT NULL DEFAULT '{}',
            captured_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_corpus_sample_source ON corpus_sample(source, captured_at DESC)",
        """CREATE TABLE IF NOT EXISTS source_health (
            source TEXT PRIMARY KEY,
            surface TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'healthy',
            consecutive_breaks INTEGER NOT NULL DEFAULT 0,
            baseline_yield REAL NOT NULL DEFAULT 0,
            last_signal TEXT DEFAULT NULL,
            last_break_at TEXT DEFAULT NULL,
            updated_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_source_health_status ON source_health(status)",
    ],
)
