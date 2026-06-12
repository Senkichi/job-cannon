"""Stale job detection and auto-archive logic.

Runs nightly (via APScheduler CronTrigger, as Phase A of the unified
staleness orchestrator) to:
1. Mark passive-stage jobs as stale when no liveness evidence for 14+ days.
2. Clear stale flag for jobs seen again (and for any job outside the
   passive stages — staleness is only meaningful pre-application).
3. Auto-archive discovered/reviewing jobs not seen for 30+ days.

"Seen" means any liveness evidence: a feed re-sighting (upsert touch), ATS
board presence (Phase B reconciler), or an HTTP live verdict (Phase C
cascade / scoring preflight via persist_job_expiry_state) — all of which
refresh last_seen.

CRITICAL: Jobs in active pipeline stages (applied, phone_screen, technical,
onsite, offer, accepted) are NEVER auto-archived — they require explicit
action. They are never marked stale either: an applied job naturally stops
being re-sighted, and the default jobs view hides stale rows, so marking
them stale silently hid active applications (21 such rows at the
2026-06-11 audit).
"""

import logging
from datetime import UTC, datetime, timedelta

from job_finder.json_utils import utc_now_iso
from job_finder.web.db_helpers import standalone_connection

logger = logging.getLogger(__name__)

# Default thresholds — days since last_seen before triggering each action.
# Overridable via config: staleness.stale_threshold_days / archive_threshold_days.
_STALE_THRESHOLD_DAYS = 14  # Mark job as stale after this many days without re-sighting
_ARCHIVE_THRESHOLD_DAYS = 30  # Auto-archive passive-stage jobs after this many days

# Stages where time-based staleness is meaningful. Active pipeline stages
# (applied onward) and user-resolved stages (dismissed, archived) are excluded.
_PASSIVE_STATUSES = ("discovered", "reviewing")


def run_stale_detection(db_path: str, config: dict | None = None) -> dict:
    """Run stale detection and auto-archive on the job database.

    Creates its own SQLite connection (thread-safe for background jobs).

    Rules:
    - Stale: passive-stage job with last_seen older than the stale threshold
      → set is_stale = 1.
    - Clear: job seen recently again, OR job no longer in a passive stage
      → set is_stale = 0.
    - Auto-archive: last_seen older than the archive threshold AND
      pipeline_status IN ('discovered', 'reviewing') → 'archived'
      (does NOT archive applied/phone_screen/technical/onsite/offer/accepted)

    Cutoffs are computed in Python with the canonical naive-UTC ISO-8601
    'T'-separator format. SQLite's datetime('now') emits a space separator,
    which string-compares against stored 'T' timestamps at date granularity
    only — the previous SQL-side cutoff was silently ~24h sloppy.

    Args:
        db_path: Path to the SQLite database file.
        config: Application config dict; reads staleness.stale_threshold_days
            and staleness.archive_threshold_days (defaults 14 / 30).

    Returns:
        dict with keys:
            stale_marked (int): Jobs newly marked as stale.
            stale_cleared (int): Jobs cleared from stale (re-seen or non-passive).
            archived (int): Jobs auto-archived.
    """
    staleness_cfg = (config or {}).get("staleness", {})
    stale_days = staleness_cfg.get("stale_threshold_days", _STALE_THRESHOLD_DAYS)
    archive_days = staleness_cfg.get("archive_threshold_days", _ARCHIVE_THRESHOLD_DAYS)

    now_naive_utc = datetime.now(UTC).replace(tzinfo=None)
    stale_cutoff = (now_naive_utc - timedelta(days=stale_days)).isoformat()
    archive_cutoff = (now_naive_utc - timedelta(days=archive_days)).isoformat()

    passive_placeholders = ",".join("?" * len(_PASSIVE_STATUSES))

    with standalone_connection(db_path) as conn:
        try:
            # Mark passive-stage jobs as stale: no liveness evidence since cutoff
            cursor = conn.execute(
                "UPDATE jobs SET is_stale = 1 "
                "WHERE last_seen < ? AND is_stale = 0 "
                f"AND pipeline_status IN ({passive_placeholders})",
                (stale_cutoff, *_PASSIVE_STATUSES),
            )
            stale_marked = cursor.rowcount

            # Clear stale flag for jobs seen recently again, and for jobs that
            # left the passive stages (staleness is meaningless post-application).
            cursor = conn.execute(
                "UPDATE jobs SET is_stale = 0 "
                "WHERE is_stale = 1 AND (last_seen >= ? "
                f"OR pipeline_status NOT IN ({passive_placeholders}))",
                (stale_cutoff, *_PASSIVE_STATUSES),
            )
            stale_cleared = cursor.rowcount

            conn.commit()

            # Auto-archive discovered/reviewing jobs not seen for the archive window.
            # CRITICAL: only archive passive stages, never active pipeline stages.
            # BATCH-03: batch UPDATE + executemany INSERT instead of per-row status calls
            rows_to_archive = conn.execute(
                "SELECT dedup_key, pipeline_status FROM jobs "
                "WHERE last_seen < ? "
                f"AND pipeline_status IN ({passive_placeholders})",
                (archive_cutoff, *_PASSIVE_STATUSES),
            ).fetchall()

            archived = 0
            if rows_to_archive:
                keys = [r["dedup_key"] for r in rows_to_archive]
                placeholders = ",".join("?" * len(keys))

                # Bulk UPDATE jobs to archived
                conn.execute(
                    f"UPDATE jobs SET pipeline_status = 'archived' WHERE dedup_key IN ({placeholders})",
                    keys,
                )

                # Bulk INSERT pipeline_events (audit trail)
                now = utc_now_iso()
                evidence = f"not_seen_{archive_days}_days"
                conn.executemany(
                    "INSERT INTO pipeline_events (job_id, from_status, to_status, timestamp, source, evidence) "
                    "VALUES (?, ?, 'archived', ?, 'stale_detector', ?)",
                    [
                        (r["dedup_key"], r["pipeline_status"], now, evidence)
                        for r in rows_to_archive
                    ],
                )
                conn.commit()
                archived = len(keys)

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
