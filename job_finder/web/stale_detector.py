"""Stale job detection and auto-archive logic.

Runs nightly (via APScheduler CronTrigger) to:
1. Mark jobs as stale when not seen for 14+ days.
2. Clear stale flag for jobs seen again.
3. Auto-archive discovered/reviewing jobs not seen for 30+ days.

CRITICAL: Jobs in active pipeline stages (applied, phone_screen, technical,
onsite, offer, accepted) are NEVER auto-archived — they require explicit action.
"""

import sqlite3
import logging

from job_finder.db import update_pipeline_status

logger = logging.getLogger(__name__)


def run_stale_detection(db_path: str) -> dict:
    """Run stale detection and auto-archive on the job database.

    Creates its own SQLite connection (thread-safe for background jobs).

    Rules:
    - Stale: last_seen < 14 days ago → set is_stale = 1
    - Re-seen: last_seen >= 14 days ago and was stale → set is_stale = 0
    - Auto-archive: last_seen < 30 days ago AND pipeline_status IN
      ('discovered', 'reviewing') → set pipeline_status = 'archived'
      (does NOT archive applied/phone_screen/technical/onsite/offer/accepted)

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        dict with keys:
            stale_marked (int): Jobs newly marked as stale.
            stale_cleared (int): Jobs cleared from stale (re-seen).
            archived (int): Jobs auto-archived.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # Mark jobs as stale: not seen for 14+ days
        cursor = conn.execute(
            "UPDATE jobs SET is_stale = 1 "
            "WHERE last_seen < datetime('now', '-14 days') AND is_stale = 0"
        )
        stale_marked = cursor.rowcount

        # Clear stale flag for jobs seen recently again
        cursor = conn.execute(
            "UPDATE jobs SET is_stale = 0 "
            "WHERE last_seen >= datetime('now', '-14 days') AND is_stale = 1"
        )
        stale_cleared = cursor.rowcount

        conn.commit()

        # Auto-archive discovered/reviewing jobs not seen for 30+ days
        # CRITICAL: only archive passive stages, never active pipeline stages.
        # Use update_pipeline_status() so each archive transition is recorded
        # in pipeline_events (audit trail).
        rows_to_archive = conn.execute(
            "SELECT dedup_key FROM jobs "
            "WHERE last_seen < datetime('now', '-30 days') "
            "AND pipeline_status IN ('discovered', 'reviewing')"
        ).fetchall()
        archived = 0
        for row in rows_to_archive:
            update_pipeline_status(
                conn, row["dedup_key"], "archived",
                source="stale_detector", evidence="not_seen_30_days",
            )
            archived += 1

        result = {
            "stale_marked": stale_marked,
            "stale_cleared": stale_cleared,
            "archived": archived,
        }
        logger.info("Stale detection complete: %s", result)
        return result

    except Exception:
        conn.rollback()
        logger.exception("Stale detection failed")
        raise
    finally:
        conn.close()
