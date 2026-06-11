"""Migration 94 — clear provably-wrong posted_date values.

A true first-posted date cannot postdate the row's own first detection,
yet at audit time (2026-06-11) 655 of 1,848 non-NULL ``posted_date``
values sat more than a day after ``first_seen``. Root causes, both fixed
upstream before this migration lands:

  - Greenhouse mapped ``updated_at`` (last-modified; edits and repost
    bumps move it) instead of ``first_published`` (#360) — 482 of the
    655 rows.
  - The upsert's NULL-fill ``COALESCE`` backfilled merge-time
    ``updated_at`` values into rows that were NULL at insert, e.g. a job
    first seen 2025-09-09 carrying a "posted" date of 2026-06-01.

Because the COALESCE never overwrites a non-NULL value, these rows can
never self-heal — hence the one-shot clear. Cleared rows are eligible
for NULL-fill again on their next re-detection, now sourced from the
corrected scanner fields.

``first_seen`` is system-owned ground truth and is NOT touched.

The +1 day tolerance mirrors the m078 I-12 future-date trigger: same-day
skew between a source's posting clock and our detection clock is normal;
anything beyond it is semantically impossible.

Idempotent: cleared rows no longer match the predicate. Ordered after
m093 so comparisons run on normalized naive-UTC strings.
"""

from __future__ import annotations

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=94,
    description="clear posted_date values that postdate first_seen (updated_at contamination)",
    sql=[
        (
            "UPDATE jobs SET posted_date = NULL "
            "WHERE posted_date IS NOT NULL "
            "AND datetime(posted_date) > datetime(first_seen, '+1 day')"
        ),
    ],
)
