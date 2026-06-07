"""One-time backfill of jobs.direct_url for the existing backlog.

Resolves the direct company-posting link for every job where direct_url IS
NULL, using ONLY the free path: existing-source-url promotion, then the ATS
scan (query_ats_api) and careers scrape (scrape_careers). No DDG, SerpAPI,
agentic tier, or jd_full writes. NULL-guarded => idempotent and re-runnable.

Operationally: pause the enrichment_backfill scheduler job before a large run
so the worker and this pass don't both write the same column concurrently
(benign — same value — but keeps the run clean).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from job_finder.db._direct_link import set_direct_url
from job_finder.web.direct_link import pick_direct_link
from job_finder.web.enrichment_tiers import query_ats_api, scrape_careers

logger = logging.getLogger(__name__)


def _parse_source_urls(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [u for u in raw if isinstance(u, str)]
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [u for u in parsed if isinstance(u, str)] if isinstance(parsed, list) else []


def backfill_direct_links(conn: Any, config: dict) -> dict:
    """Resolve direct_url for all NULL rows. Returns {scanned, resolved, strict, loose}."""
    rows = conn.execute(
        "SELECT dedup_key, title, company_id, source_urls FROM jobs WHERE direct_url IS NULL"
    ).fetchall()

    scanned = resolved = strict = loose = 0
    for row in rows:
        scanned += 1
        job_row = {
            "dedup_key": row["dedup_key"],
            "title": row["title"],
            "company_id": row["company_id"],
        }
        source_urls = _parse_source_urls(row["source_urls"])

        ats_result: dict = {}
        careers_result: dict = {}
        if job_row["company_id"]:
            try:
                ats_result = query_ats_api(job_row, conn, config) or {}
            except Exception as e:
                logger.debug("backfill ats query failed for %s: %s", job_row["dedup_key"], e)
            try:
                careers_result = scrape_careers(job_row, conn, config) or {}
            except Exception as e:
                logger.debug("backfill careers scrape failed for %s: %s", job_row["dedup_key"], e)

        direct = pick_direct_link(source_urls, ats_result, careers_result)
        if direct and set_direct_url(conn, job_row["dedup_key"], direct[0], direct[1]):
            resolved += 1
            if direct[1] == "strict":
                strict += 1
            else:
                loose += 1

    logger.info(
        "backfill_direct_links: scanned=%d resolved=%d (strict=%d loose=%d)",
        scanned,
        resolved,
        strict,
        loose,
    )
    return {"scanned": scanned, "resolved": resolved, "strict": strict, "loose": loose}
