"""Speculative ATS-API slug probing for companies with pending probe_status.

Extracted from ats_scanner/__init__.py during S7c (portfolio cleanup).
Re-exported from the package for backward compatibility.
"""

import logging
import time
from datetime import datetime

from job_finder.web.ats_detection import derive_slug_candidates
from job_finder.web.ats_prober import _probe_ashby, _probe_greenhouse, _probe_lever
from job_finder.web.db_helpers import standalone_connection

logger = logging.getLogger(__name__)


def probe_ats_slugs(db_path: str, config: dict) -> dict:
    """Probe ATS APIs speculatively for companies with pending probe status.

    Thread-safe: opens own sqlite3 connection (same pattern as stale_detector.py).
    TESTING guard: returns early when config.get('TESTING') is True.

    For each pending company:
    1. Derive slug candidates from company name
    2. Try Lever, Greenhouse, and Ashby APIs for each candidate
    3. Set ats_probe_status='hit' when API returns valid postings
    4. Set ats_probe_status='miss' when all APIs fail/return empty
    5. Lever 200+empty list stays as 'miss' (never 'hit') per Research Pitfall 2

    Args:
        db_path: Absolute path to the SQLite database file.
        config: Application config dict. Reads TESTING flag.

    Returns:
        Dict with probed, hits, misses counts.
    """
    # TESTING guard: skip real API calls during tests
    if config.get("TESTING"):
        logger.debug("probe_ats_slugs: TESTING mode — skipping API calls")
        return {"probed": 0, "hits": 0, "misses": 0}

    summary = {"probed": 0, "hits": 0, "misses": 0}

    with standalone_connection(db_path) as conn:
        # Only probe companies with pending status
        pending = conn.execute(
            "SELECT id, name_raw FROM companies WHERE ats_probe_status = 'pending'"
        ).fetchall()

        for company in pending:
            company_id = company["id"]
            company_name = company["name_raw"]
            now = datetime.now().isoformat()

            candidates = derive_slug_candidates(company_name)
            hit_platform = None
            hit_slug = None

            for slug in candidates:
                # Try Lever first
                if _probe_lever(slug):
                    hit_platform = "lever"
                    hit_slug = slug
                    break

                # Try Greenhouse
                if _probe_greenhouse(slug):
                    hit_platform = "greenhouse"
                    hit_slug = slug
                    break

                # Try Ashby
                if _probe_ashby(slug):
                    hit_platform = "ashby"
                    hit_slug = slug
                    break

            # Update company record based on probe result
            if hit_platform:
                conn.execute(
                    """UPDATE companies
                       SET ats_platform = ?,
                           ats_slug = ?,
                           ats_probe_status = 'hit',
                           ats_probe_attempted_at = ?,
                           updated_at = ?
                       WHERE id = ?""",
                    (hit_platform, hit_slug, now, now, company_id),
                )
                summary["hits"] += 1
            else:
                conn.execute(
                    """UPDATE companies
                       SET ats_probe_status = 'miss',
                           ats_probe_attempted_at = ?,
                           updated_at = ?
                       WHERE id = ?""",
                    (now, now, company_id),
                )
                summary["misses"] += 1

            conn.commit()
            summary["probed"] += 1

            # Polite delay between companies (0.5s per Research Open Question 2)
            time.sleep(0.5)

    logger.info(
        "probe_ats_slugs: probed=%d, hits=%d, misses=%d",
        summary["probed"],
        summary["hits"],
        summary["misses"],
    )
    return summary
