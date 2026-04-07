"""Opus-powered rejection analysis batch job.

Analyzes all unreviewed rejected jobs in a single Opus call to identify
cross-rejection patterns and actionable recommendations.

Provides:
- run_rejection_analysis: Core batch analysis function (own DB connection,
  thread-safe for APScheduler background use).

Report structure covers four factors:
  1. Profile-to-JD match quality
  2. Resume tailoring effectiveness
  3. Company/role competitiveness
  4. Timing and pipeline signals

Reports are stored in rejection_reports. Analyzed jobs are marked
rejection_reviewed=1 so they are not re-included in future batches.
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone

import anthropic

from job_finder.web.claude_client import BudgetExceededError
from job_finder.web.model_provider import call_model
from job_finder.web.db_helpers import standalone_connection

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Structured output schema
# ---------------------------------------------------------------------------

REJECTION_ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "patterns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Cross-rejection trend observed",
                    },
                    "frequency": {
                        "type": "string",
                        "description": "How many rejections show this pattern",
                    },
                    "factor": {
                        "type": "string",
                        "enum": [
                            "profile_match",
                            "resume_tailoring",
                            "competitiveness",
                            "timing",
                        ],
                    },
                },
                "required": ["pattern", "frequency", "factor"],
                "additionalProperties": False,
            },
        },
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "Concrete action the user should take",
                    },
                    "impact": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                    "details": {"type": "string"},
                },
                "required": ["action", "impact", "details"],
                "additionalProperties": False,
            },
        },
        "summary": {
            "type": "string",
            "description": "2-3 sentence executive summary of rejection patterns",
        },
    },
    "required": ["patterns", "recommendations", "summary"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = (
    "You are analyzing a batch of job rejections for a senior data scientist. "
    "Identify cross-rejection patterns across four factors: "
    "(1) Profile-to-JD match quality, "
    "(2) Resume tailoring effectiveness, "
    "(3) Company/role competitiveness, "
    "(4) Timing and pipeline signals. "
    "Lead with patterns, not individual breakdowns. "
    "End with concrete actionable recommendations linking to profile improvements."
)


# ---------------------------------------------------------------------------
# Core analysis function
# ---------------------------------------------------------------------------

def run_rejection_analysis(db_path: str, config: dict) -> dict:
    """Run Opus batch rejection analysis on all unreviewed rejected jobs.

    Opens its own sqlite3 connection (thread-safe for APScheduler background use).

    Args:
        db_path: Path to the SQLite database file.
        config: Application config dict (reads scoring.daily_budget_usd and
                scoring.models.opus).

    Returns:
        dict with keys:
            rejections_analyzed (int): Number of rejections included in the batch.
            report_id (int | None): ID of the stored rejection_reports row, or None.
            cost_usd (float): Opus API cost for this run (0.0 if skipped).
            budget_exceeded (bool): True if budget gate blocked the call.
    """
    with standalone_connection(db_path) as conn:
        return _run_analysis(conn, config)


def _run_analysis(conn: sqlite3.Connection, config: dict) -> dict:
    """Internal: run analysis within an open connection."""

    # Query all unreviewed rejected jobs
    rows = conn.execute(
        """
        SELECT j.dedup_key, j.title, j.company, j.haiku_score, j.sonnet_score,
               j.fit_analysis, j.jd_full, j.pipeline_status
        FROM jobs j
        WHERE j.pipeline_status = 'rejected'
          AND j.rejection_reviewed = 0
        ORDER BY j.last_seen DESC
        """
    ).fetchall()

    if not rows:
        logger.info("Rejection analysis: no unreviewed rejections found, skipping")
        return {"rejections_analyzed": 0, "report_id": None, "cost_usd": 0.0}

    # Build batch input for Opus
    job_summaries = []
    dedup_keys = []
    for row in rows:
        job = dict(row)
        dedup_keys.append(job["dedup_key"])
        summary = {
            "title": job["title"],
            "company": job["company"],
            "haiku_score": job["haiku_score"],
            "sonnet_score": job["sonnet_score"],
        }
        if job.get("fit_analysis"):
            try:
                summary["fit_analysis"] = json.loads(job["fit_analysis"])
            except (json.JSONDecodeError, TypeError):
                summary["fit_analysis"] = job["fit_analysis"]
        if job.get("jd_full"):
            summary["jd_snippet"] = job["jd_full"][:2000]
        job_summaries.append(summary)

    user_message = json.dumps(
        {"rejected_jobs": job_summaries, "total_count": len(job_summaries)},
        indent=2,
    )

    # Single Opus call for ALL rejections
    client = anthropic.Anthropic()
    try:
        result_obj = call_model(
            tier="opus",
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
            output_schema=REJECTION_ANALYSIS_SCHEMA,
            conn=conn,
            job_id=None,
            purpose="opus_rejection_analysis",
            config=config,
            max_tokens=4096,
            client=client,
        )
        result = result_obj.data
        cost_usd = result_obj.cost_usd
    except BudgetExceededError:
        logger.info("Rejection analysis: monthly budget cap reached, skipping Opus call")
        return {
            "rejections_analyzed": 0,
            "report_id": None,
            "cost_usd": 0.0,
            "budget_exceeded": True,
        }
    except Exception as exc:
        logger.error(
            "Rejection analysis Opus call failed (%d rejections): %s",
            len(dedup_keys),
            exc,
        )
        return {
            "rejections_analyzed": 0,
            "report_id": None,
            "cost_usd": 0.0,
            "error": str(exc),
        }

    # Store report
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cursor = conn.execute(
        """
        INSERT INTO rejection_reports (report_text, rejections_analyzed, generated_at, cost_usd)
        VALUES (?, ?, ?, ?)
        """,
        (json.dumps(result), len(dedup_keys), generated_at, cost_usd),
    )
    report_id = cursor.lastrowid

    # Mark analyzed jobs as reviewed
    placeholders = ",".join("?" * len(dedup_keys))
    conn.execute(
        f"UPDATE jobs SET rejection_reviewed = 1 WHERE dedup_key IN ({placeholders})",
        dedup_keys,
    )
    conn.commit()

    logger.info(
        "Rejection analysis complete: %d rejections analyzed, report_id=%s, cost=$%.4f",
        len(dedup_keys),
        report_id,
        cost_usd,
    )

    return {
        "rejections_analyzed": len(dedup_keys),
        "report_id": report_id,
        "cost_usd": cost_usd,
    }
