"""Migration 21 — clean stub jd_full title-restatements; null out scores for jobs whose JD is now NULL."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=21,
    description="clean stub jd_full title-restatements; null out scores for jobs whose JD is now NULL",
    sql=[
        """UPDATE jobs
           SET jd_full = NULL,
               enrichment_tier = NULL
           WHERE jd_full IS NOT NULL
             AND LENGTH(jd_full) < 200
             AND enrichment_tier IN ('haiku', 'sonnet', 'exhausted')""",
        """UPDATE jobs
           SET sonnet_score = NULL,
               fit_analysis = NULL
           WHERE jd_full IS NULL
             AND sonnet_score IS NOT NULL""",
    ],
)
