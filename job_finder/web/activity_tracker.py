"""Activity tracker module — records user actions to the user_activity table.

Provides log_activity() helper and ACTION_* constants for all call sites.

Design constraints:
- Creates its own SQLite connection (thread-safe for APScheduler threads)
- Never raises to caller — all exceptions are silently swallowed
- Works without Flask application context (safe for background jobs)
"""

import json
import logging
from datetime import UTC, datetime

from job_finder.web.db_helpers import standalone_connection

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ACTION constants — one per instrumented user/system action
# ---------------------------------------------------------------------------

ACTION_SYNC = "sync"
ACTION_SCHEDULED_SYNC = "scheduled_sync"
ACTION_EXPAND_JOB = "expand_job"
ACTION_STATUS_CHANGE = "status_change"
ACTION_PASTE_JD = "paste_jd"
ACTION_RESCORE = "rescore"
ACTION_BATCH_SCORE = "batch_score"

ACTION_SCHEDULED_ATS_SCAN = "scheduled_ats_scan"
ACTION_SAVE_JD = "save_jd"
ACTION_EDIT_NOTES = "edit_notes"
ACTION_SCHEDULED_PIPELINE_DETECTION = "scheduled_pipeline_detection"
ACTION_SCHEDULED_AGENTIC_BACKFILL = "scheduled_agentic_backfill"
ACTION_SCHEDULED_ENRICHMENT_BACKFILL = "scheduled_enrichment_backfill"
ACTION_SCHEDULED_CAREERS_CRAWL = "scheduled_careers_crawl"
# Unified staleness orchestrator — replaces the legacy SCHEDULED_EXPIRY_CHECK
# and 'scheduled_stale_detection' actions (both removed). Historical
# user_activity rows with the old action strings remain in the DB as
# read-only history.
ACTION_SCHEDULED_STALENESS = "scheduled_staleness"
ACTION_SOURCE_DEGRADED = "source_degraded"
ACTION_SCHEDULED_HEALTH = "scheduled_health"
ACTION_SCHEDULED_PRIMARY_SOURCE = "scheduled_primary_source"

# ---------------------------------------------------------------------------
# Core helper
# ---------------------------------------------------------------------------


def log_activity(
    db_path: str,
    action: str,
    entity_id: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Insert a row into user_activity recording a user or system action.

    Fault-tolerant: any exception is caught and logged at WARNING level.
    Thread-safe: opens its own sqlite3 connection, independent of the caller.
    Context-free: does not require a Flask application context.

    Args:
        db_path: Absolute path to the SQLite database file.
        action: One of the ACTION_* constants (string identifier).
        entity_id: Optional job dedup_key or other entity identifier.
        metadata: Optional dict with additional context. Stored as JSON.
    """
    try:
        occurred_at = datetime.now(UTC).replace(tzinfo=None).isoformat()
        metadata_json = json.dumps(metadata or {})

        with standalone_connection(db_path) as conn:
            conn.execute(
                "INSERT INTO user_activity (action, entity_id, metadata, occurred_at) "
                "VALUES (?, ?, ?, ?)",
                (action, entity_id, metadata_json, occurred_at),
            )
            conn.commit()

    except Exception:
        # Intentional fault-tolerance: activity logging must never crash the caller.
        # All call sites (Flask routes, APScheduler jobs) rely on this no-raise contract.
        # Failures are surfaced via WARNING log with full traceback for observability.
        logger.warning("log_activity failed for action=%s", action, exc_info=True)
