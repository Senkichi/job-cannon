"""Migration 56 — clear default-leaked scoring_provider='anthropic' tags.

Migration 20 (`m020_scoring_provider_attribution.py`) added
`scoring_provider TEXT DEFAULT 'anthropic'` to the jobs table back when
anthropic was the only scorer. Since the multi-provider cascade landed,
`upsert_job` (job_finder/db/_jobs.py) has not specified `scoring_provider`
in its INSERT column list, so SQLite applies the column DEFAULT and every
new row enters the table tagged `scoring_provider='anthropic'` even though
no anthropic call has occurred.

The legitimate scoring writer (`persist_job_assessment` in
job_finder/db/_persistence.py) uses an atomic `COALESCE` UPDATE that sets
`scoring_provider` and `scoring_model` together. Therefore every row with
`scoring_provider='anthropic' AND scoring_model IS NULL` is a default leak,
not a real attribution. Empirically (2026-05-22): 1876 such rows existed
across 11,550 total, and zero rows had a legitimate anthropic score
(`scoring_provider='anthropic' AND classification IS NOT NULL`).

Heal pass:
  1. Clear the leaked tag on rows that demonstrably never reached the
     scoring writer (scoring_model NULL is the discriminator).

Defense-in-depth at the INSERT site (passing `scoring_provider=NULL`
explicitly in `upsert_job`) ships alongside this migration to prevent
new leaks from accruing.
"""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=56,
    description="clear default-leaked scoring_provider='anthropic' on rows that never reached the scoring writer",
    sql=[
        """UPDATE jobs
              SET scoring_provider = NULL
            WHERE scoring_provider = 'anthropic'
              AND scoring_model IS NULL""",
    ],
)
