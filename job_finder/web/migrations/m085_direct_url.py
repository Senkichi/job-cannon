"""Migration 85 — direct_url + direct_url_confidence columns.

Adds the canonical company-posting link captured by enrichment (ATS scan /
careers scrape) and a confidence tag distinguishing a strict (unique exact-
title) match from a loose (first-match) one. Both nullable; existing rows get
NULL and are backfilled separately.

The runner swallows 'duplicate column name' so a re-run is idempotent.

(Version 85, not the 84 the original issue assumed — m084 was taken by the
parser-health migration that landed first.)
"""

from __future__ import annotations

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=85,
    description="add direct_url + direct_url_confidence columns",
    sql=[
        "ALTER TABLE jobs ADD COLUMN direct_url TEXT DEFAULT NULL",
        (
            "ALTER TABLE jobs ADD COLUMN direct_url_confidence TEXT DEFAULT NULL "
            "CHECK (direct_url_confidence IN ('strict','loose') "
            "OR direct_url_confidence IS NULL)"
        ),
    ],
)
