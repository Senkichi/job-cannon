"""Migration 46 — heal Workday URL-template bug fallout.

scan_workday() prepended a static "/job/" before externalPath, but the CXS
API already returns externalPath starting with "/job/...". The resulting
"/job//job/..." source_urls 406'd at the API and fell through to
fetch_direct_jd, which extracted "<title>Workday</title>" as the page text
and persisted "Workday" as jd_full. Affected ~87% of Workday rows and
nullified scoring quality for them.

Heal pass:
  1. Repair source_urls: replace "/job//job/" with "/job/".
  2. Reset enrichment so the cascade can re-fetch via the corrected URL.
  3. Drop scoring derived from the bogus jd_full so the next batch re-scores.
"""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=46,
    description="heal Workday URL-template bug fallout: repair source_urls, reset enrichment, drop bogus scores",
    sql=[
        # 1. Fix the malformed source URLs so future fetches hit the right slot
        """UPDATE jobs
              SET source_urls = REPLACE(source_urls, '/job//job/', '/job/')
            WHERE source_urls LIKE '%/job//job/%'""",
        # 2. Reset jd_full + enrichment_tier on Workday rows that captured "Workday"
        """UPDATE jobs
              SET jd_full = NULL,
                  enrichment_tier = NULL
            WHERE TRIM(jd_full) = 'Workday'""",
        # 3. Drop classification + sub_scores derived from the corrupt jd_full so
        #    the next batch scoring run re-classifies these rows from scratch
        """UPDATE jobs
              SET classification = NULL,
                  sub_scores_json = NULL,
                  fit_analysis = NULL,
                  scoring_provider = NULL,
                  scoring_model = NULL
            WHERE jd_full IS NULL
              AND classification IS NOT NULL
              AND sources LIKE '%Workday%'""",
    ],
)
