"""Migration 92 — direct_url resolution state: checked_at + attempts + index.

Attempt semantics for the scheduled primary-source resolver (Phase 3 of
.planning/PRIMARY_SOURCE_RESOLUTION_PLAN.md):

  - ``direct_url_checked_at`` — naive UTC ISO of the last board-match attempt
    (store-UTC-render-local rule). NULL = never attempted.
  - ``direct_url_attempts`` — cumulative attempts. The resolver skips rows at
    max attempts (config ``direct_link.resolver.max_attempts``), with decay
    re-eligibility once ``checked_at`` ages past
    ``direct_link.resolver.recheck_days`` — so a company whose ATS is
    discovered (or re-keyed) later picks its exhausted jobs back up without
    any probe-transition hooks.

The partial index serves the resolver's candidate query: unresolved jobs
grouped by company. Existing rows get NULL / 0 — the resolver treats both as
"never tried".

The runner swallows 'duplicate column name' so a re-run is idempotent;
CREATE INDEX uses IF NOT EXISTS for the same reason.
"""

from __future__ import annotations

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=92,
    description="direct_url resolution state: checked_at + attempts + unresolved index",
    sql=[
        "ALTER TABLE jobs ADD COLUMN direct_url_checked_at TEXT DEFAULT NULL",
        "ALTER TABLE jobs ADD COLUMN direct_url_attempts INTEGER NOT NULL DEFAULT 0",
        (
            "CREATE INDEX IF NOT EXISTS idx_jobs_direct_unresolved "
            "ON jobs(company_id) WHERE direct_url IS NULL"
        ),
    ],
)
