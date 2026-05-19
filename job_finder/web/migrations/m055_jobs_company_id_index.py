"""Migration 55 — idx_jobs_company_id to fix writer-lock starvation.

The orphan-recalibration UPDATE in cleanup_orphan_companies is a correlated
subquery over jobs.company_id with no index. On the production DB (55k jobs,
3.5k companies) that single statement takes ~245s, holding the SQLite writer
lock the entire time. Any concurrent writer (reconcile_company, registry
hygiene, staleness check Phase B) hits the 30s busy_timeout and fails with
"database is locked".

Other queries that benefit from this index:
- cleanup_invalid_company_data's `JOIN companies c ON j.company_id = c.id`
- Any `WHERE company_id = ?` lookup (currently a full table scan)

CREATE INDEX IF NOT EXISTS makes the migration idempotent on DBs that
already have it (none in production, but defensively safe).
"""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=55,
    description="idx_jobs_company_id — fixes 245s correlated UPDATE in orphan recalibration",
    sql=[
        "CREATE INDEX IF NOT EXISTS idx_jobs_company_id ON jobs(company_id)",
    ],
)
