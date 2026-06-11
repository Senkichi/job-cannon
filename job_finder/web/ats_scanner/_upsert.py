"""Company-table upsert helper for the ATS scanner.

Extracted from ats_scanner/__init__.py during S7c (portfolio cleanup).
Re-exported from the package for backward compatibility.
"""

import logging
import sqlite3

from job_finder.json_utils import utc_now_iso
from job_finder.web.ats_prober import _PROBE_STATUS_PRECEDENCE
from job_finder.web.dedup_normalizer import normalize_company

logger = logging.getLogger(__name__)


def is_company_tracked(conn: sqlite3.Connection, name: str) -> bool:
    """True when a company is actively tracked for ATS scanning (WP6).

    "Tracked" == a companies row exists (matched by normalized name, with a
    raw-name fallback mirroring the ``_high_score_history_clause`` precedent)
    AND ``scan_enabled = 1``. A row the user disabled shows as untracked so
    the Track action can re-enable it.
    """
    if not name:
        return False
    row = conn.execute(
        """SELECT 1 FROM companies
           WHERE (name = ? OR name_raw = ?) AND scan_enabled = 1
           LIMIT 1""",
        (normalize_company(name), name),
    ).fetchone()
    return row is not None


def upsert_company(
    conn: sqlite3.Connection,
    name: str,
    ats_platform: str | None = None,
    ats_slug: str | None = None,
    ats_probe_status: str = "pending",
    homepage_url: str | None = None,
) -> int | None:
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
    now = utc_now_iso()
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
                try:
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
                except sqlite3.IntegrityError as exc:
                    # m076's UNIQUE(ats_platform, ats_slug) gate. Another
                    # company already owns the pair. Leave the ATS fields
                    # untouched (NULL or pre-existing) — this is a tight
                    # upsert loop, so swallowing here is safer than
                    # re-raising. The collision will be logged for audit.
                    logger.warning(
                        "upsert_company: ATS collision for %r on %s/%s — "
                        "leaving ATS fields untouched. exc=%s",
                        name,
                        ats_platform,
                        ats_slug,
                        exc,
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
