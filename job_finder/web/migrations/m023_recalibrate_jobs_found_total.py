"""Migration 23 — recalibrate companies.jobs_found_total from cumulative to current count + company_scan_log.jobs_matched."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=23,
    description=(
        "recalibrate companies.jobs_found_total from cumulative to current count "
        "+ company_scan_log.jobs_matched"
    ),
    sql=[
        """UPDATE companies SET jobs_found_total = (
            SELECT COUNT(*) FROM jobs WHERE company_id = companies.id
        )""",
        "ALTER TABLE company_scan_log ADD COLUMN jobs_matched INTEGER DEFAULT NULL",
    ],
)
