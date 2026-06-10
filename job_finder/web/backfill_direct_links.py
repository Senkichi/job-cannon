"""Manual backfill of jobs.direct_url — delegates to the primary-source resolver.

Kept as a separate module so the admin route's import surface
(`backfill_direct_links(conn, config)`) and response keys stay stable. Since
Phase 3 this is a thin wrapper over
job_finder.web.primary_source_resolver.resolve_primary_sources, which gives
the manual route the same semantics as the nightly scheduled run: free
source_url promotion, one board fetch per company across the full
PlatformScanner registry (16 platforms vs the original 3), strict-gated data
merge, and m092 attempt/checked_at stamping. NULL-guarded => idempotent and
re-runnable.

Two deliberate differences from the original per-job implementation:
  - No per-job careers-page scrape — that N-fetches-per-company shape is what
    the resolver replaces. Careers-page resolution still happens
    opportunistically in the free enrichment tier.
  - max_companies=None: a manual backlog drain visits every eligible company
    in one pass instead of the scheduler's per-run cap.

Operationally: pause the enrichment_backfill scheduler job before a large run
so the worker and this pass don't both write the same column concurrently
(benign — same value — but keeps the run clean).
"""

from __future__ import annotations

import logging
from typing import Any

from job_finder.web.primary_source_resolver import resolve_primary_sources

logger = logging.getLogger(__name__)


def backfill_direct_links(conn: Any, config: dict) -> dict:
    """Resolve direct_url for all eligible rows. Returns resolver counters
    ({scanned, resolved, strict, loose, ...} — superset of the legacy keys)."""
    summary = resolve_primary_sources(conn, config, max_companies=None)
    logger.info("backfill_direct_links: %s", summary)
    return summary
