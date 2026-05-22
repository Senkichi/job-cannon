"""Speculative ATS-API slug probing for companies with pending probe_status.

Extracted from ats_scanner/__init__.py during S7c (portfolio cleanup).
Re-exported from the package for backward compatibility.
"""

import logging
import time
from collections.abc import Callable
from datetime import datetime

from job_finder.web.ats_detection import (
    derive_slug_candidates,
    probe_hit_consistent_or_dead_url,
)
from job_finder.web.ats_prober import (
    _probe_ashby,
    _probe_bamboohr,
    _probe_breezy,
    _probe_greenhouse,
    _probe_jazzhr,
    _probe_lever,
    _probe_personio,
    _probe_pinpoint,
    _probe_recruitee,
    _probe_teamtailor,
)
from job_finder.web.db_helpers import standalone_connection

logger = logging.getLogger(__name__)

# (platform, probe_fn) pairs. Ordering matches the historical ladder:
# original three (Lever / Greenhouse / Ashby) first because they have the
# longest track record; Stage 4 additions follow, with the Pinpoint/
# Teamtailor/Personio/BambooHR block ordered fastest-JSON-first so cheap
# probes short-circuit before the XML and HTML variants pay their cost.
_PROBES: list[tuple[str, Callable[[str], bool]]] = [
    ("lever", _probe_lever),
    ("greenhouse", _probe_greenhouse),
    ("ashby", _probe_ashby),
    ("recruitee", _probe_recruitee),
    ("breezy", _probe_breezy),
    ("jazzhr", _probe_jazzhr),
    ("pinpoint", _probe_pinpoint),
    ("teamtailor", _probe_teamtailor),
    ("personio", _probe_personio),
    ("bamboohr", _probe_bamboohr),
]


def probe_ats_slugs(db_path: str, config: dict) -> dict:
    """Probe ATS APIs speculatively for companies with pending probe status.

    Thread-safe: opens own sqlite3 connection (same pattern as stale_detector.py).
    TESTING guard: returns early when config.get('TESTING') is True.

    For each pending company:
    1. Derive slug candidates from company name
    2. Try Lever, Greenhouse, Ashby, Recruitee, Breezy, JazzHR, Pinpoint,
       Teamtailor, Personio, BambooHR APIs for each candidate (in that order;
       first hit wins). New platforms are appended after the established
       three; fastest probes go earlier within the new block so we
       short-circuit before paying the cost of slower ones.
    3. F6 consistency gate (augmented with liveness check): if a hit's
       platform disagrees with the platform inferred from the company's
       `careers_url` AND that careers_url is still live (not 404/410),
       reject the hit and keep trying. Catches brand-name-collision false
       positives (e.g. 'Shopify' → Pinpoint tenant of a different small
       company) without rejecting legitimate ATS migrations where the old
       careers_url now 404s and the live probe correctly rediscovers the
       new platform.
    4. Set ats_probe_status='hit' when API returns valid postings
    5. Set ats_probe_status='miss' when all APIs fail/return empty
    6. Empty-postings 200 responses stay as 'miss' (never 'hit') per
       Lever Research Pitfall 2 — same dynamic affects every Stage 4 platform

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
            "SELECT id, name_raw, careers_url FROM companies WHERE ats_probe_status = 'pending'"
        ).fetchall()

        for company in pending:
            company_id = company["id"]
            company_name = company["name_raw"]
            careers_url = company["careers_url"]
            now = datetime.now().isoformat()

            candidates = derive_slug_candidates(company_name)
            hit_platform = None
            hit_slug = None

            for slug in candidates:
                for platform, probe in _PROBES:
                    if not probe(slug):
                        continue
                    if not probe_hit_consistent_or_dead_url(platform, careers_url):
                        logger.info(
                            "probe_ats_slugs: rejected %s/%s for company %s — "
                            "careers_url %s infers a different platform and is live",
                            platform,
                            slug,
                            company_name,
                            careers_url,
                        )
                        continue
                    hit_platform = platform
                    hit_slug = slug
                    break
                if hit_platform:
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
