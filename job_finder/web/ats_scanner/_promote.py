"""Source-URL based ATS promotion for miss/error companies.

Different strategy than probe_ats_slugs(): rather than guessing slugs
from the company name, extract platform+slug from the company's existing
job source_urls and verify with a single API call.

Extracted from ats_scanner/__init__.py during S7c (portfolio cleanup).
Re-exported from the package for backward compatibility.
"""

import json
import logging
from datetime import datetime

from job_finder.web.ats_detection import extract_ats_from_urls
from job_finder.web.ats_prober import (
    _probe_ashby,
    _probe_greenhouse,
    _probe_lever,
    _probe_smartrecruiters,
    _probe_workday,
)
from job_finder.web.db_helpers import standalone_connection

logger = logging.getLogger(__name__)


def promote_ats_from_source_urls(db_path: str, config: dict) -> dict:
    """Promote miss/error companies to ATS-hit using evidence from job source_urls.

    Separate from probe_ats_slugs() — different strategy (DB lookup, not name
    guessing) and different input set (miss/error, not pending).

    For each miss/error company with scan_enabled=1:
    1. Load source_urls from all linked jobs
    2. Extract ATS platform+slug via extract_ats_from_urls()
    3. Verify the slug is live (single API call)
    4. On verified hit, update company to ats_probe_status='hit'

    Thread-safe: creates own sqlite3 connection.

    Args:
        db_path: Absolute path to the SQLite database file.
        config: Application config dict. Reads TESTING flag.

    Returns:
        Dict with checked, promoted counts.
    """
    if config.get("TESTING"):
        return {"checked": 0, "promoted": 0}

    summary = {"checked": 0, "promoted": 0}

    with standalone_connection(db_path) as conn:
        missed = conn.execute(
            """SELECT id, name FROM companies
               WHERE ats_probe_status IN ('miss', 'error')
                 AND scan_enabled = 1""",
        ).fetchall()

        for company in missed:
            company_id = company["id"]
            summary["checked"] += 1

            rows = conn.execute(
                "SELECT source_urls FROM jobs WHERE company_id = ? AND source_urls IS NOT NULL",
                (company_id,),
            ).fetchall()

            all_urls = []
            for row in rows:
                try:
                    all_urls.extend(json.loads(row[0] or "[]"))
                except (json.JSONDecodeError, TypeError):
                    continue

            if not all_urls:
                continue

            platform, slug = extract_ats_from_urls(all_urls)
            if not slug:
                continue

            # Verify the slug is live with a single API call
            verified = False
            if platform == "lever":
                verified = _probe_lever(slug)
            elif platform == "greenhouse":
                verified = _probe_greenhouse(slug)
            elif platform == "ashby":
                verified = _probe_ashby(slug)
            elif platform == "workday":
                verified = _probe_workday(slug)
            elif platform == "smartrecruiters":
                verified = _probe_smartrecruiters(slug)

            if not verified:
                continue

            now = datetime.now().isoformat()
            conn.execute(
                """UPDATE companies
                   SET ats_platform = ?,
                       ats_slug = ?,
                       ats_probe_status = 'hit',
                       ats_probe_attempted_at = ?,
                       updated_at = ?
                   WHERE id = ?""",
                (platform, slug, now, now, company_id),
            )
            conn.commit()
            summary["promoted"] += 1
            logger.info(
                "promote_ats: %s -> %s:%s (from job source_urls)",
                company["name"],
                platform,
                slug,
            )

    logger.info(
        "promote_ats_from_source_urls: checked=%d, promoted=%d",
        summary["checked"],
        summary["promoted"],
    )
    return summary
