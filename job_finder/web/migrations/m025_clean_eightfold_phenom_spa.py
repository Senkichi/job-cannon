"""Migration 25 — clean Eightfold/Phenom PCS SPA shell garbage in jd_full (themeOptions JSON)."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=25,
    description="clean Eightfold/Phenom PCS SPA shell garbage in jd_full (themeOptions JSON)",
    sql=[
        # Fix A: exhausted-tier jobs — agentic enricher picks up jd_full IS NULL
        """UPDATE jobs
           SET jd_full = NULL,
               sonnet_score = NULL,
               fit_analysis = NULL
           WHERE jd_full LIKE '%"themeOptions"%'
             AND enrichment_tier = 'exhausted'""",
        # Fix B: non-exhausted jobs — reset to re-run free tier cleanly
        """UPDATE jobs
           SET jd_full = NULL,
               enrichment_tier = NULL,
               sonnet_score = NULL,
               fit_analysis = NULL
           WHERE jd_full LIKE '%"themeOptions"%'
             AND (enrichment_tier IS NULL OR enrichment_tier != 'exhausted')""",
    ],
)
