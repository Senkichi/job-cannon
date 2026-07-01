"""Migration 114 — re-tag first_seen-copy posted_dates mislabeled exact -> proxy (m095 backfill bug).

m095's backfill tagged rows 'exact' purely on source membership (i.e. the row's
sources JSON contains an exact-class source like Greenhouse/Lever/Ashby/Workday/
SmartRecruiters/dataforseo/portal_himalayas) — regardless of whether posted_date
was an actual ATS timestamp or a first_seen copy left by the retired backfill_v1
path.

Result: 1,282 of 2,754 rows tagged 'exact' had posted_date identical to
first_seen to the microsecond — i.e. they are first_seen copies, definitionally
'proxy', mislabeled 'exact'.

This is a correctness bug because precision drives resolution precedence on
merge (exact > approximate > proxy). A first_seen-copy mistagged 'exact' can
outrank and suppress a genuine date arriving from another source on a later
merge. It also poisons every precision='exact' consumer (e.g. the crawl-latency
metric reads these as fake 0-day lags).

This migration re-tags the mislabeled rows to their true class:

  posted_date_precision = 'exact' AND posted_date = first_seen → 'proxy'

'proxy' is exactly m095's definition for a detection-time stand-in / first_seen
copy. Demoting to 'proxy' (not NULL) preserves the stored date value and lets a
later genuine 'exact' sighting correctly overwrite it — which converges to correct.

Idempotent: the UPDATE matches nothing once tags are corrected. The re-tag keeps
both posted_date and posted_date_precision non-NULL (pairing holds) and 'proxy'
is in the allowed domain, so the m095 tg_jobs_posted_date_precision_pairing_upd
trigger does not abort.
"""

from __future__ import annotations

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=114,
    description="data-integrity: re-tag first_seen-copy posted_dates mislabeled exact -> proxy (m095 backfill bug)",
    sql=[
        "UPDATE jobs SET posted_date_precision = 'proxy' "
        "WHERE posted_date_precision = 'exact' AND posted_date IS NOT NULL "
        "AND first_seen IS NOT NULL AND posted_date = first_seen",
    ],
)
