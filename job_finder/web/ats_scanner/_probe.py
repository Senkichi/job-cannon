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
    _probe_greenhouse,
    _probe_jazzhr,
    _probe_lever,
    _probe_pinpoint,
    _probe_teamtailor,
)
from job_finder.web.brand_blocklist import is_blocked_brand
from job_finder.web.db_helpers import standalone_connection

logger = logging.getLogger(__name__)

# Platforms excluded from the speculative ladder due to a 100% false-positive
# rate empirically observed in the 2026-05-27 ATS coverage audit. Each of these
# four platforms had every single speculative-probe hit (18 + 6 + 8 + 8 = 40
# rows) come back with `ats_evidence_trigger IS NULL` — i.e. no corroborating
# job-URL evidence. Famous-brand names (Microsoft, Amazon, Meta, YouTube,
# Accenture, EY, Leidos, IQVIA, ...) collide with real SMB tenants that
# registered the same {slug}={normalized_name} on these platforms, and the
# probe returns a true 200 for the wrong company. F8 brand_blocklist catches
# some but not all of the cohort.
#
# These platforms can still be PROMOTED via the evidence-based reconcile path
# (job_finder/web/ats_identity_reconcile.reconcile_company_ats), which requires
# corroborating job-URL evidence before writing `hit`. The per-platform probe
# functions remain available and are used by reconcile's _verify_live step.
#
# This was the corollary of the v2 audit at .planning/ATS-COVERAGE-AUDIT-2026-05-27.md.
_FP_PRONE_PLATFORMS: frozenset[str] = frozenset(
    {"bamboohr", "personio", "recruitee", "breezy"}
)

# (platform, probe_fn) pairs. Ordering matches the historical ladder:
# original three (Lever / Greenhouse / Ashby) first because they have the
# longest track record; surviving Stage 4 additions follow, with the
# Pinpoint/Teamtailor/JazzHR block ordered fastest-JSON-first so cheap
# probes short-circuit before slower variants pay their cost.
#
# bamboohr / personio / recruitee / breezy are deliberately excluded —
# see _FP_PRONE_PLATFORMS above for the 100% FP rate finding.
_PROBES: list[tuple[str, Callable[[str], bool]]] = [
    ("lever", _probe_lever),
    ("greenhouse", _probe_greenhouse),
    ("ashby", _probe_ashby),
    ("jazzhr", _probe_jazzhr),
    ("pinpoint", _probe_pinpoint),
    ("teamtailor", _probe_teamtailor),
]

# Invariant: speculative ladder must not include any FP-prone platform.
# Tests assert this stays true under future edits.
assert _FP_PRONE_PLATFORMS.isdisjoint({name for name, _ in _PROBES}), (
    "speculative _PROBES ladder must not include any platform in "
    "_FP_PRONE_PLATFORMS; only the evidence-based reconcile path may "
    "promote to these platforms"
)


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

            # F8 — brand blocklist gate. Famous-brand names (Shopify, Walmart,
            # Canva, ...) produce a high-rate collision with small companies
            # that have registered the same slug on a small ATS (BambooHR,
            # Recruitee, Pinpoint, ...). Empirically the tenants self-identify
            # with the same name, so name-matching can't disambiguate; only
            # a curated blocklist works for this cohort. See
            # job_finder/web/brand_blocklist.py for the rationale and seed list.
            if is_blocked_brand(company_name):
                logger.info(
                    "probe_ats_slugs: skipped %s (id=%d) — blocked brand",
                    company_name,
                    company_id,
                )
                conn.execute(
                    """UPDATE companies
                       SET ats_probe_status = 'miss',
                           miss_reason = 'blocked_brand',
                           ats_probe_attempted_at = ?,
                           updated_at = ?
                       WHERE id = ?""",
                    (now, now, company_id),
                )
                conn.commit()
                summary["misses"] += 1
                summary["probed"] += 1
                continue

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
