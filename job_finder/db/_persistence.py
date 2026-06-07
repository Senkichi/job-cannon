"""DB write paths — single-row updates, run log, pipeline state machine.

All functions take an open `sqlite3.Connection` and commit themselves
(CLI-era pattern, distinct from `web/db_helpers.py`'s per-request `g.db`).

`persist_job_assessment` now lives in `_assessment_writer.py` (Phase 49.04, the
sole sanctioned writer of the scoring tuple) and is re-exported here for
back-compat. The remaining write paths (`log_run`, `persist_job_expiry_state`,
`update_pipeline_status`) are stdlib + `job_finder.json_utils`.

Re-exported via `job_finder.db.__init__` so existing
`from job_finder.db import persist_job_assessment` (etc.) paths keep working.
"""

from __future__ import annotations

import logging
import sqlite3
import time

from job_finder.json_utils import utc_now_iso

# persist_job_assessment moved to _assessment_writer.py (Phase 49.04) — the sole
# sanctioned writer of the scoring tuple. Re-exported here so existing
# `from job_finder.db._persistence import persist_job_assessment` paths keep working.
from ._assessment_writer import persist_job_assessment as persist_job_assessment

_log = logging.getLogger(__name__)


def log_run(conn: sqlite3.Connection, source: str, fetched: int, new: int, scored: int) -> None:
    """Log a pipeline run for auditing.

    Args:
        conn: Open sqlite3 connection.
        source: Source label (e.g., "gmail", "serpapi").
        fetched: Number of jobs fetched.
        new: Number of new jobs inserted.
        scored: Number of jobs scored.
    """
    conn.execute(
        "INSERT INTO runs (timestamp, source, jobs_fetched, jobs_new, jobs_scored) VALUES (?, ?, ?, ?, ?)",
        (utc_now_iso(), source, fetched, new, scored),
    )
    conn.commit()


def persist_job_expiry_state(
    conn: sqlite3.Connection,
    dedup_key: str,
    expiry_status: str,
    checked_at: str,
) -> None:
    """Persist job expiry verdict and timestamp atomically.

    Single write path for expiry_status and expiry_checked_at. Called by
    the scoring preflight (per-job liveness check) and the nightly batch
    expiry runner.

    Retries on 'database is locked' (3 attempts, exponential backoff).
    On 2026-05-01 the day-1 monthly hygiene jobs collided with the daily
    agentic_backfill at 03:30, exhausting the standalone_connection's 30s
    busy_timeout 113 times in this function and aborting the reconciler
    mid-batch. The cron decoupling fix (scheduler.py: agentic moved to
    04:15) is the primary defense; this retry is belt-and-suspenders for
    any future writer contention spike.

    Args:
        conn: Open sqlite3 connection.
        dedup_key: The job's primary key.
        expiry_status: One of 'expired', 'live', or 'inconclusive'.
        checked_at: ISO 8601 timestamp string of when the check ran.
    """
    last_err: sqlite3.OperationalError | None = None
    for attempt in range(3):
        try:
            conn.execute(
                "UPDATE jobs SET expiry_status = ?, expiry_checked_at = ? WHERE dedup_key = ?",
                (expiry_status, checked_at, dedup_key),
            )
            conn.commit()
            return
        except sqlite3.OperationalError as e:
            if "database is locked" not in str(e).lower():
                raise
            last_err = e
            # Backoff: 0.5s, 1.0s. busy_timeout (30s) already kicked in inside
            # sqlite before we got here, so any sleep here is on top of that.
            if attempt < 2:
                time.sleep(0.5 * (2**attempt))
                _log.warning(
                    "persist_job_expiry_state: database locked, retry %d/2 (dedup_key=%s)",
                    attempt + 1,
                    dedup_key,
                )
    # Exhausted retries — re-raise so the caller's outer try/except records the error.
    assert last_err is not None
    raise last_err


def persist_job_notes(conn: sqlite3.Connection, dedup_key: str, notes: str) -> None:
    """Persist user-written notes for a job (single-column UPDATE).

    Empty string is a valid value — it clears the column.  The caller is
    responsible for length-capping before passing ``notes`` in.

    Args:
        conn: Open sqlite3 connection.
        dedup_key: The job's primary key.
        notes: Note text to persist (may be empty string to clear).
    """
    conn.execute(
        "UPDATE jobs SET notes = ? WHERE dedup_key = ?",
        (notes, dedup_key),
    )
    conn.commit()


def update_pipeline_status(
    conn: sqlite3.Connection,
    dedup_key: str,
    new_status: str,
    source: str = "manual",
    evidence: str = "",
) -> None:
    """Update a job's pipeline_status and log a pipeline_events record.

    Args:
        conn: Open sqlite3 connection.
        dedup_key: The job's primary key.
        new_status: The target pipeline status to move the job to.
        source: Who triggered the move ('manual', 'email', 'ai', etc.).
        evidence: Optional evidence string describing what triggered the change
            (e.g., "lever_api 404"). Defaults to empty string.

    Raises:
        ValueError: If new_status is not a recognized pipeline status.
    """
    from job_finder.constants import VALID_PIPELINE_STATUSES

    if new_status not in VALID_PIPELINE_STATUSES:
        raise ValueError(
            f"Invalid pipeline status: {new_status!r}. "
            f"Must be one of: {sorted(VALID_PIPELINE_STATUSES)}"
        )

    row = conn.execute(
        "SELECT pipeline_status FROM jobs WHERE dedup_key = ?",
        (dedup_key,),
    ).fetchone()
    if row is None:
        return  # Job not found — no-op

    from_status = row["pipeline_status"]
    if from_status == new_status:
        return  # Already at this status — skip duplicate event insertion

    now = utc_now_iso()

    conn.execute(
        "UPDATE jobs SET pipeline_status = ? WHERE dedup_key = ?",
        (new_status, dedup_key),
    )
    conn.execute(
        """INSERT INTO pipeline_events
               (job_id, from_status, to_status, timestamp, source, evidence)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (dedup_key, from_status, new_status, now, source, evidence),
    )
    conn.commit()
