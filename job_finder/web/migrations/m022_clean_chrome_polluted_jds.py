"""Migration 22 — clean chrome-polluted jd_full: LinkedIn walls, cookie banners, Built-In overviews, search results."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=22,
    description=(
        "clean chrome-polluted jd_full: LinkedIn walls, cookie banners, "
        "Built-In overviews, search results"
    ),
    sql=[
        # LinkedIn login wall pages (most common: 151 jobs)
        """UPDATE jobs
           SET jd_full = NULL, enrichment_tier = NULL,
               sonnet_score = NULL, fit_analysis = NULL
           WHERE jd_full IS NOT NULL
             AND (jd_full LIKE '%Agree & Join LinkedIn%'
               OR jd_full LIKE '%Join or sign in to find your next job%'
               OR jd_full LIKE '%Join to apply for the%')""",
        # Cookie banners in first 300 chars of jd_full
        """UPDATE jobs
           SET jd_full = NULL, enrichment_tier = NULL,
               sonnet_score = NULL, fit_analysis = NULL
           WHERE jd_full IS NOT NULL
             AND (SUBSTR(jd_full, 1, 300) LIKE '%cookie%'
               OR SUBSTR(jd_full, 1, 300) LIKE '%Close this dialog%'
               OR SUBSTR(jd_full, 1, 300) LIKE '%third-party partners%')""",
        # Built In company overview pages (not JDs)
        """UPDATE jobs
           SET jd_full = NULL, enrichment_tier = NULL,
               sonnet_score = NULL, fit_analysis = NULL
           WHERE jd_full IS NOT NULL
             AND (jd_full LIKE '%View All Jobs at%'
               OR jd_full LIKE '%Recently Posted Jobs at%'
               OR jd_full LIKE '%Similar Companies Hiring%')""",
        # LinkedIn search results pages (not individual JDs)
        """UPDATE jobs
           SET jd_full = NULL, enrichment_tier = NULL,
               sonnet_score = NULL, fit_analysis = NULL
           WHERE jd_full IS NOT NULL
             AND jd_full LIKE '%Past month%Past week%Past 24 hours%'""",
    ],
)
