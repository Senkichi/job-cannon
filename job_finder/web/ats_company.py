"""ATS company registry CRUD operations.

Provides upsert and find-or-create for the companies table.
Extracted from ats_scanner.py (Plan 02 split).
"""

import logging
import sqlite3
from datetime import datetime
from typing import Optional

from job_finder.web.dedup_normalizer import normalize_company
from job_finder.web.ats_prober import _PROBE_STATUS_PRECEDENCE

logger = logging.getLogger(__name__)


def upsert_company(
    conn: sqlite3.Connection,
    name: str,
    ats_platform: Optional[str] = None,
    ats_slug: Optional[str] = None,
    ats_probe_status: str = "pending",
    homepage_url: Optional[str] = None,
) -> Optional[int]:
    """Create or update a company record in the companies table.

    Looks up by normalized company name. If the company exists, updates
    ats_platform, ats_slug, and ats_probe_status only when the new info
    is better (hit > pending > miss — never downgrade from hit to pending).

    Args:
        conn: Open SQLite connection with Migration 7 schema applied.
        name: Raw company name string (will be normalized for lookup).
        ats_platform: ATS platform name ('lever', 'greenhouse', 'ashby', or None).
        ats_slug: ATS slug string, or None if not yet known.
        ats_probe_status: Probe status ('pending', 'hit', or 'miss').
        homepage_url: Company homepage URL, or None.

    Returns:
        The company_id (integer) for the upserted record, or None on error.
    """
    now = datetime.now().isoformat()
    normalized_name = normalize_company(name)

    try:
        # Look up by normalized name
        existing = conn.execute(
            "SELECT id, ats_probe_status FROM companies WHERE name = ?",
            (normalized_name,),
        ).fetchone()

        if existing is None:
            # INSERT new company
            cursor = conn.execute(
                """INSERT INTO companies
                   (name, name_raw, homepage_url, ats_platform, ats_slug,
                    ats_probe_status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    normalized_name,
                    name,
                    homepage_url,
                    ats_platform,
                    ats_slug,
                    ats_probe_status,
                    now,
                    now,
                ),
            )
            conn.commit()
            return cursor.lastrowid
        else:
            # UPDATE only if new info is better
            company_id = existing[0]
            current_status = existing[1] or "pending"
            current_rank = _PROBE_STATUS_PRECEDENCE.get(current_status, 0)
            new_rank = _PROBE_STATUS_PRECEDENCE.get(ats_probe_status, 0)

            # Only update ATS fields if new status is higher precedence
            if new_rank >= current_rank:
                conn.execute(
                    """UPDATE companies
                       SET ats_platform = COALESCE(?, ats_platform),
                           ats_slug = COALESCE(?, ats_slug),
                           ats_probe_status = ?,
                           homepage_url = COALESCE(?, homepage_url),
                           updated_at = ?
                       WHERE id = ?""",
                    (
                        ats_platform,
                        ats_slug,
                        ats_probe_status,
                        homepage_url,
                        now,
                        company_id,
                    ),
                )
            else:
                # Still update non-ATS fields (homepage, timestamp)
                conn.execute(
                    """UPDATE companies
                       SET homepage_url = COALESCE(?, homepage_url),
                           updated_at = ?
                       WHERE id = ?""",
                    (homepage_url, now, company_id),
                )
            conn.commit()
            return company_id

    except Exception as e:
        logger.warning("upsert_company failed for '%s' (non-fatal): %s", name, e)
        return None


def find_or_create_company(
    conn: sqlite3.Connection,
    name: str,
    ats_platform: Optional[str] = None,
    ats_slug: Optional[str] = None,
    homepage_url: Optional[str] = None,
) -> Optional[int]:
    """Find existing company by normalized name or fuzzy match, or create new.

    Lookup order:
    1. Exact normalized name match
    2. Fuzzy match with threshold=85 (token_set_ratio via backfill_companies)
    3. INSERT new company via upsert_company

    Prevents duplicate company creation across the three code paths
    (probe_ats_slugs, backfill UI add route, link_jobs_to_companies).

    Args:
        conn: Open SQLite connection.
        name: Raw company name string.
        ats_platform: Optional ATS platform for new records.
        ats_slug: Optional ATS slug for new records.
        homepage_url: Optional homepage URL for new records.

    Returns:
        company_id integer, or None on error.
    """
    normalized_name = normalize_company(name)

    # 1. Exact normalized match
    existing = conn.execute(
        "SELECT id FROM companies WHERE name = ?", (normalized_name,)
    ).fetchone()
    if existing:
        if homepage_url:
            conn.execute(
                "UPDATE companies SET homepage_url = COALESCE(homepage_url, ?), updated_at = datetime('now') WHERE id = ?",
                (homepage_url, existing[0]),
            )
        return existing[0]

    # 2. Fuzzy match against all existing companies
    try:
        from job_finder.web.company_resolver import fuzzy_match_company
        all_rows = conn.execute("SELECT id, name FROM companies").fetchall()
        company_list = [(r["id"], r["name"]) for r in all_rows]
        matched_id, _score = fuzzy_match_company(name, company_list)
        if matched_id is not None:
            if homepage_url:
                conn.execute(
                    "UPDATE companies SET homepage_url = COALESCE(homepage_url, ?), updated_at = datetime('now') WHERE id = ?",
                    (homepage_url, matched_id),
                )
            return matched_id
    except Exception as e:
        logger.debug("find_or_create_company fuzzy match failed: %s", e)

    # 3. Create new company record
    return upsert_company(
        conn, name,
        ats_platform=ats_platform,
        ats_slug=ats_slug,
        homepage_url=homepage_url,
    )
