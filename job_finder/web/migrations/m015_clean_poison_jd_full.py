"""Migration 15 — Phase 40 data quality: clean LinkedIn login pages, garbage notification rows, promote long descriptions to jd_full."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=15,
    description=(
        "Phase 40 data quality: clean LinkedIn login pages, garbage notification "
        "rows, promote long descriptions to jd_full"
    ),
    sql=[
        # Fix A: Null out LinkedIn login page jd_full values
        "UPDATE jobs SET jd_full = NULL, enrichment_tier = 'ddg' "
        "WHERE jd_full LIKE '%signing you in%' OR jd_full LIKE '%sign in or join%'",
        # Fix B: Delete garbage notification rows
        "DELETE FROM jobs WHERE title LIKE '%receive notifications%'",
        # Fix C: Promote long descriptions to jd_full
        "UPDATE jobs SET jd_full = SUBSTR(description, 1, 8000) "
        "WHERE jd_full IS NULL AND description IS NOT NULL AND LENGTH(description) > 200",
    ],
)
