"""DB write paths — single-row updates, run log, pipeline state machine.

All functions take an open `sqlite3.Connection` and commit themselves
(CLI-era pattern, distinct from `web/db_helpers.py`'s per-request `g.db`).

`persist_job_assessment` depends on the v3.0 scoring-rule cluster
(`_SUB_SCORE_KEYS`, `derive_classification`) — that's the only intra-package
import; everything else is stdlib + `job_finder.json_utils`.

Re-exported via `job_finder.db.__init__` so existing
`from job_finder.db import persist_job_assessment` (etc.) paths keep working.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time

from job_finder.json_utils import utc_now_iso

from ._classification import _SUB_SCORE_KEYS, JobAssessment, derive_classification

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


def persist_job_assessment(
    conn: sqlite3.Connection,
    dedup_key: str,
    assessment: JobAssessment,
    provider: str | None = None,
    model: str | None = None,
    *,
    config: dict | None = None,
) -> None:
    """Persist a v3.0 JobAssessment. Replaces persist_haiku_score + persist_sonnet_score.

    Writes classification (derived at persist time), sub_scores_json (JSON),
    fit_analysis (rationale payload — D-08 reuse), scoring_provider, scoring_model.
    Plan 5 (Migration 41) dropped the legacy haiku_score/haiku_summary/sonnet_score
    columns; this function now writes only the v3.0 surface.

    legitimacy_note sourcing (CONTEXT D-07): read from the existing jobs row,
    NOT from the assessment. derive_classification uses this value to compute
    the authoritative classification — any classification field on the passed
    assessment is ignored (anti-pattern 3 defense).

    Phase 2d sub-fix 2-3/4: also reads enrichment_tier and LENGTH(jd_full) from
    the row so derive_classification can compute the low_signal verdict. The
    threshold (default 1500 chars) is sourced from config.scoring.low_signal_jd_chars
    when config is provided; callers that pass config=None get the default,
    preserving back-compat with direct test/script invocations.

    No-op on missing dedup_key (SQLite UPDATE with no matching row is a silent
    no-op; we also short-circuit before the UPDATE to avoid COALESCE no-ops).

    Args:
        conn: Open sqlite3 connection.
        dedup_key: The job's primary key.
        assessment: JobAssessment with sub_scores + rationale.
        provider: Cascade-attribution string; None preserves the existing value.
        model: Model identifier (e.g., "qwen2.5:14b"); None preserves existing.
        config: Optional application config dict. When provided, reads
            scoring.low_signal_jd_chars to set the low_signal threshold;
            otherwise the default (1500 chars) is used.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT legitimacy_note, enrichment_tier, COALESCE(LENGTH(jd_full), 0) AS jd_len "
        "FROM jobs WHERE dedup_key = ?",
        (dedup_key,),
    )
    row = cur.fetchone()
    if row is None:
        # Silent no-op matches SQLite UPDATE-no-match semantics.
        return
    legitimacy_note, enrichment_tier, jd_full_length = row[0], row[1], row[2] or 0

    # Resolve low_signal threshold from config (Phase 2d sub-fix 3/4). Default
    # 1500 chars matches scoring.low_signal_jd_chars.example. None config keeps
    # backwards compatibility for tests/scripts that call directly.
    threshold = 1500
    if config is not None:
        scoring_cfg = config.get("scoring") or {}
        threshold = int(scoring_cfg.get("low_signal_jd_chars", 1500))

    final_classification = derive_classification(
        assessment.sub_scores,
        legitimacy_note,
        enrichment_tier=enrichment_tier,
        jd_full_length=jd_full_length,
        low_signal_threshold=threshold,
    )

    # Serialize sub_scores with stable key order for diff-friendliness.
    ordered_sub_scores = {
        k: assessment.sub_scores[k] for k in _SUB_SCORE_KEYS if k in assessment.sub_scores
    }

    cur.execute(
        """
        UPDATE jobs
           SET classification   = ?,
               sub_scores_json  = ?,
               fit_analysis     = ?,
               scoring_provider = COALESCE(?, scoring_provider),
               scoring_model    = COALESCE(?, scoring_model)
         WHERE dedup_key = ?
        """,
        (
            final_classification,
            json.dumps(ordered_sub_scores),
            json.dumps(assessment.rationale),
            provider or assessment.provider,
            model,
            dedup_key,
        ),
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
