"""Batch scoring blueprint -- Haiku/Sonnet batch scoring start, status, cancel routes."""

import logging
import threading
from datetime import datetime, timezone

from flask import Blueprint, current_app, render_template

from job_finder.db import JOBS_ALL_COLUMNS
from job_finder.config import DEFAULT_HAIKU_THRESHOLD
from job_finder.json_utils import utc_now_iso
from job_finder.web.ai_route_responses import tier_unavailable_message
from job_finder.web.claude_client import BudgetExceededError
from job_finder.web.exclusion_filter import should_exclude
from job_finder.web.db_helpers import standalone_connection
from job_finder.web.model_provider import tier_has_configured_provider

logger = logging.getLogger(__name__)

try:
    import anthropic as _anthropic
except ImportError:
    _anthropic = None

batch_scoring_bp = Blueprint("batch_scoring", __name__, url_prefix="/dashboard")


def _try_anthropic_client():
    """Return an Anthropic client if available, else None."""
    if _anthropic is None:
        return None
    try:
        return _anthropic.Anthropic()
    except Exception:
        return None


@batch_scoring_bp.route("/batch-score/haiku/start", methods=["POST"], strict_slashes=False)
def batch_score_haiku_start():
    """Start async Haiku batch scoring — returns HTMX polling fragment.

    Counts unscored jobs and either returns a done fragment immediately
    (nothing to score) or inserts a batch_score_sessions row and starts a
    daemon thread, returning a progress fragment that polls every 2s.
    """
    db_path = current_app.config["DB_PATH"]
    config = current_app.config.get("JF_CONFIG", {})
    testing = current_app.config.get("TESTING", False)

    # Early provider check — avoid creating a session if no provider is routable
    client = _try_anthropic_client()
    if not tier_has_configured_provider("haiku", config, client):
        return render_template(
            "dashboard/_batch_score_done.html",
            label="Haiku",
            scored=0,
            skipped=0,
            status="error",
            message=None,
            error_msg=tier_unavailable_message("haiku", "Batch scoring"),
        )

    with standalone_connection(db_path) as conn:
        total_unscored = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE haiku_score IS NULL"
        ).fetchone()[0]

        if total_unscored == 0:
            return render_template(
                "dashboard/_batch_score_done.html",
                label="Haiku",
                scored=0,
                skipped=0,
                status="done",
                message="All jobs already scored — nothing to do.",
                error_msg=None,
            )

        # Pre-filter estimate: the background thread will compute the exact
        # scorable count after enrichment, but give a quick initial estimate
        # so the progress UI starts with a reasonable total.
        exclusions = config.get("profile", {}).get("exclusions", {})
        profile_min_salary = config.get("profile", {}).get("min_salary")
        rows = conn.execute(
            f"SELECT {JOBS_ALL_COLUMNS} FROM jobs WHERE haiku_score IS NULL"
        ).fetchall()
        scorable = sum(
            1 for r in rows
            if not should_exclude(dict(r), exclusions, profile_min_salary, config=config)[0]
        )

        if scorable == 0:
            return render_template(
                "dashboard/_batch_score_done.html",
                label="Haiku",
                scored=0,
                skipped=0,
                status="done",
                message=f"All {total_unscored} unscored jobs are excluded by filters — nothing to score.",
                error_msg=None,
            )

        now = utc_now_iso()
        cursor = conn.execute(
            "INSERT INTO batch_score_sessions (session_type, status, total, scored, started_at) "
            "VALUES ('haiku', 'running', ?, 0, ?)",
            (scorable, now),
        )
        conn.commit()
        session_id = cursor.lastrowid

    if not testing:
        t = threading.Thread(
            target=_run_batch_haiku_bg,
            args=(db_path, session_id, config),
            daemon=True,
        )
        t.start()

    return render_template(
        "dashboard/_batch_score_progress.html",
        label="Haiku",
        session_id=session_id,
        total=scorable,
        scored=0,
        skipped=0,
        cancelling=False,
    )


@batch_scoring_bp.route("/batch-score/sonnet/start", methods=["POST"], strict_slashes=False)
def batch_score_sonnet_start():
    """Start async Sonnet batch evaluation — returns HTMX polling fragment.

    Counts jobs qualifying for Sonnet (haiku_score >= threshold, no sonnet_score,
    jd_full present). Returns done fragment if none qualify.
    """
    db_path = current_app.config["DB_PATH"]
    config = current_app.config.get("JF_CONFIG", {})
    testing = current_app.config.get("TESTING", False)
    threshold = config.get("scoring", {}).get("haiku_threshold", DEFAULT_HAIKU_THRESHOLD)

    # Early provider check — avoid creating a session if no provider is routable
    client = _try_anthropic_client()
    if not tier_has_configured_provider("sonnet", config, client):
        return render_template(
            "dashboard/_batch_score_done.html",
            label="Sonnet",
            scored=0,
            skipped=0,
            status="error",
            message=None,
            error_msg=tier_unavailable_message("sonnet", "Batch scoring"),
        )

    with standalone_connection(db_path) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE haiku_score IS NOT NULL AND haiku_score >= ? "
            "AND sonnet_score IS NULL AND jd_full IS NOT NULL",
            (threshold,),
        ).fetchone()[0]

        if total == 0:
            return render_template(
                "dashboard/_batch_score_done.html",
                label="Sonnet",
                scored=0,
                skipped=0,
                status="done",
                message="No qualifying jobs for Sonnet evaluation.",
                error_msg=None,
            )

        now = utc_now_iso()
        cursor = conn.execute(
            "INSERT INTO batch_score_sessions (session_type, status, total, scored, started_at) "
            "VALUES ('sonnet', 'running', ?, 0, ?)",
            (total, now),
        )
        conn.commit()
        session_id = cursor.lastrowid

    if not testing:
        t = threading.Thread(
            target=_run_batch_sonnet_bg,
            args=(db_path, session_id, config),
            daemon=True,
        )
        t.start()

    return render_template(
        "dashboard/_batch_score_progress.html",
        label="Sonnet",
        session_id=session_id,
        total=total,
        scored=0,
        skipped=0,
        cancelling=False,
    )


@batch_scoring_bp.route("/batch-score/status/<int:session_id>", strict_slashes=False)
def batch_score_status(session_id):
    """Poll route for batch scoring progress.

    Returns _batch_score_progress.html (WITH hx-trigger) when still running.
    Returns _batch_score_done.html (WITHOUT hx-trigger) when done/error/cancelled.
    Uses own sqlite3 connection — safe for HTMX polling outside request context.
    """
    db_path = current_app.config["DB_PATH"]

    with standalone_connection(db_path) as conn:
        session = conn.execute(
            "SELECT * FROM batch_score_sessions WHERE id = ?", (session_id,)
        ).fetchone()

    if session is None:
        return render_template(
            "dashboard/_batch_score_done.html",
            label="Unknown",
            scored=0,
            skipped=0,
            status="error",
            message=None,
            error_msg="Session not found.",
        )

    label = "Haiku" if session["session_type"] == "haiku" else "Sonnet"
    status = session["status"]

    # Timeout safety net: if session has been running for >30 minutes, auto-mark as error
    if status in ("running", "cancelling") and session["started_at"]:
        try:
            started = datetime.fromisoformat(session["started_at"])
            elapsed_minutes = (datetime.now(timezone.utc).replace(tzinfo=None) - started).total_seconds() / 60
            if elapsed_minutes > 30:
                logger.warning("Batch session %s timed out after %.1f minutes", session_id, elapsed_minutes)
                with standalone_connection(db_path) as timeout_conn:
                    timeout_conn.execute(
                        "UPDATE batch_score_sessions SET status = 'error', error_msg = ?, finished_at = ? "
                        "WHERE id = ? AND status IN ('running', 'cancelling')",
                        ("Session timed out (>30 min)", utc_now_iso(), session_id),
                    )
                    timeout_conn.commit()
                return render_template(
                    "dashboard/_batch_score_done.html",
                    label=label,
                    scored=session["scored"],
                    skipped=session["skipped"],
                    status="error",
                    message=None,
                    error_msg="Session timed out (>30 min)",
                )
        except (ValueError, TypeError):
            pass

    # Terminal states: done, error, cancelled — return done fragment (NO hx-trigger)
    if status in ("done", "error", "cancelled"):
        return render_template(
            "dashboard/_batch_score_done.html",
            label=label,
            scored=session["scored"],
            skipped=session["skipped"],
            status=status,
            message=None,
            error_msg=session["error_msg"] if status == "error" else None,
        )

    # Still running (running or cancelling) — return progress fragment (WITH hx-trigger)
    return render_template(
        "dashboard/_batch_score_progress.html",
        label=label,
        session_id=session_id,
        total=session["total"],
        scored=session["scored"],
        skipped=session["skipped"],
        cancelling=(status == "cancelling"),
    )


@batch_scoring_bp.route("/batch-score/cancel/<int:session_id>", methods=["POST"], strict_slashes=False)
def batch_score_cancel(session_id):
    """Cancel a running batch score session.

    Sets status='cancelling' in DB. The background thread checks status
    before each job and will set status='cancelled' when it sees 'cancelling'.
    Returns a progress fragment that keeps polling until the thread finishes.
    """
    db_path = current_app.config["DB_PATH"]

    with standalone_connection(db_path) as conn:
        conn.execute(
            "UPDATE batch_score_sessions SET status = 'cancelling' WHERE id = ? AND status = 'running'",
            (session_id,),
        )
        conn.commit()
        session = conn.execute(
            "SELECT * FROM batch_score_sessions WHERE id = ?", (session_id,)
        ).fetchone()

    if session is None:
        return render_template(
            "dashboard/_batch_score_done.html",
            label="Unknown",
            scored=0,
            skipped=0,
            status="error",
            message=None,
            error_msg="Session not found.",
        )

    label = "Haiku" if session["session_type"] == "haiku" else "Sonnet"

    # Return progress fragment with cancelling=True — polling continues until
    # the background thread sets status='cancelled'
    return render_template(
        "dashboard/_batch_score_progress.html",
        label=label,
        session_id=session_id,
        total=session["total"],
        scored=session["scored"],
        skipped=session["skipped"],
        cancelling=True,
    )


def _run_batch_haiku_bg(db_path: str, session_id: int, config: dict) -> None:
    """Background thread: run Haiku scoring for all unscored jobs.

    Enriches each job before scoring (matching the run_haiku_scoring pipeline
    behavior). Delegates per-job scoring + persistence to
    scoring_orchestrator.score_and_persist_haiku.

    Args:
        db_path: Absolute path to the SQLite database.
        session_id: ID of the batch_score_sessions row to update.
        config: Application config dict.
    """
    try:
        import anthropic as _anthropic
    except ImportError:
        _anthropic = None

    from job_finder.web.model_provider import tier_has_configured_provider
    from job_finder.web.scoring_orchestrator import load_scoring_profile, score_and_persist_haiku

    client = None
    if _anthropic is not None:
        try:
            client = _anthropic.Anthropic()
        except Exception:
            pass

    if not tier_has_configured_provider("haiku", config, client):
        _mark_session_error(db_path, session_id, "No routable haiku provider")
        return

    profile = load_scoring_profile(config)

    # Lazy import enrichment (matches scoring_runner pattern)
    try:
        from job_finder.web.data_enricher import enrich_job, is_stub_jd
    except ImportError:
        enrich_job = None
        is_stub_jd = None

    try:
        with standalone_connection(db_path) as conn:
            rows = conn.execute(
                f"SELECT {JOBS_ALL_COLUMNS} FROM jobs WHERE haiku_score IS NULL ORDER BY score DESC"
            ).fetchall()

            # --- Pre-filter: remove jobs that fail the exclusion filter ---
            # Excluded jobs are not scorable — they should never count toward
            # the batch total or appear as "skipped" in the progress UI.
            exclusions = config.get("profile", {}).get("exclusions", {})
            profile_min_salary = config.get("profile", {}).get("min_salary")
            scorable_rows = []
            excluded_count = 0
            for row in rows:
                job_row = dict(row)
                excluded, reason = should_exclude(job_row, exclusions, profile_min_salary, config=config)
                if excluded:
                    excluded_count += 1
                else:
                    scorable_rows.append(job_row)

            if excluded_count > 0:
                logger.info("Batch Haiku: %d/%d jobs excluded by filter", excluded_count, len(rows))

            # Update session total to reflect only scorable jobs
            conn.execute(
                "UPDATE batch_score_sessions SET total = ? WHERE id = ?",
                (len(scorable_rows), session_id),
            )
            conn.commit()

            if not scorable_rows:
                _finish_session(conn, db_path, session_id, "done", "haiku")
                return

            scored_count = 0
            skipped_count = 0

            for job_row in scorable_rows:
                # Per-job cancellation check
                status_row = conn.execute(
                    "SELECT status FROM batch_score_sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if status_row and status_row["status"] == "cancelling":
                    conn.execute(
                        "UPDATE batch_score_sessions SET status = 'cancelled', scored = ?, skipped = ?, finished_at = ? WHERE id = ?",
                        (scored_count, skipped_count, utc_now_iso(), session_id),
                    )
                    conn.commit()
                    return

                # --- Enrichment FIRST (before scoring) ---
                # Matches run_haiku_scoring behavior: enrich sparse jobs before Haiku
                if enrich_job is not None and is_stub_jd is not None and (
                    is_stub_jd(job_row.get("jd_full"), job_row.get("title", ""), job_row.get("company", ""))
                    or job_row.get("salary_min") is None
                ):
                    try:
                        serpapi_key = config.get("sources", {}).get("serpapi", {}).get("api_key")
                        enriched = enrich_job(
                            job_row,
                            serpapi_key=serpapi_key,
                            anthropic_client=client,
                            conn=conn,
                            config=config,
                        )
                        if enriched:
                            job_row.update(enriched)
                    except Exception as enrich_err:
                        logger.debug(
                            "Batch Haiku: enrichment failed for '%s' (non-fatal): %s",
                            job_row.get("dedup_key"), enrich_err,
                        )

                try:
                    result = score_and_persist_haiku(conn, job_row, config, client, profile)
                    if result is not None:
                        scored_count += 1
                    else:
                        skipped_count += 1
                except BudgetExceededError as e:
                    logger.error(
                        "Batch Haiku: API budget exceeded after %d scored — aborting: %s",
                        scored_count, e,
                    )
                    conn.execute(
                        "UPDATE batch_score_sessions SET status = 'error', error_msg = ?, "
                        "scored = ?, skipped = ?, finished_at = ? WHERE id = ?",
                        (f"API budget exceeded: {e}", scored_count, skipped_count,
                         utc_now_iso(), session_id),
                    )
                    conn.commit()
                    return
                except Exception as e:
                    logger.warning(
                        "Batch Haiku: error scoring job '%s': %s -- continuing",
                        job_row.get("dedup_key"), e,
                    )
                    skipped_count += 1

                # Flush progress after each job so polling shows real-time updates
                conn.execute(
                    "UPDATE batch_score_sessions SET scored = ?, skipped = ? WHERE id = ?",
                    (scored_count, skipped_count, session_id),
                )
                conn.commit()

            # All jobs processed — mark done
            _finish_session(conn, db_path, session_id, "done", "haiku")

    except Exception as e:
        logger.error("Batch Haiku background thread failed: %s", e)
        _mark_session_error(db_path, session_id, str(e)[:500])


def _run_batch_sonnet_bg(db_path: str, session_id: int, config: dict) -> None:
    """Background thread: run Sonnet evaluation for qualifying jobs.

    Delegates per-job scoring + persistence to scoring_orchestrator.score_and_persist_sonnet.
    This function handles thread-own DB connection, cancellation checks, session progress
    tracking, and activity logging.

    Args:
        db_path: Absolute path to the SQLite database.
        session_id: ID of the batch_score_sessions row to update.
        config: Application config dict.
    """
    try:
        import anthropic as _anthropic
    except ImportError:
        _anthropic = None

    from job_finder.web.model_provider import tier_has_configured_provider
    from job_finder.web.scoring_orchestrator import load_scoring_profile, score_and_persist_sonnet

    client = None
    if _anthropic is not None:
        try:
            client = _anthropic.Anthropic()
        except Exception:
            pass

    if not tier_has_configured_provider("sonnet", config, client):
        _mark_session_error(db_path, session_id, "No routable sonnet provider")
        return

    threshold = config.get("scoring", {}).get("haiku_threshold", DEFAULT_HAIKU_THRESHOLD)
    profile = load_scoring_profile(config)

    try:
        with standalone_connection(db_path) as conn:
            rows = conn.execute(
                f"SELECT {JOBS_ALL_COLUMNS} FROM jobs WHERE haiku_score IS NOT NULL AND haiku_score >= ? "
                "AND sonnet_score IS NULL AND jd_full IS NOT NULL ORDER BY haiku_score DESC",
                (threshold,),
            ).fetchall()

            scored_count = 0
            skipped_count = 0

            for row in rows:
                # Per-job cancellation check
                status_row = conn.execute(
                    "SELECT status FROM batch_score_sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if status_row and status_row["status"] == "cancelling":
                    conn.execute(
                        "UPDATE batch_score_sessions SET status = 'cancelled', scored = ?, skipped = ?, finished_at = ? WHERE id = ?",
                        (scored_count, skipped_count, utc_now_iso(), session_id),
                    )
                    conn.commit()
                    return

                job_row = dict(row)
                try:
                    result = score_and_persist_sonnet(conn, job_row, config, client, profile)
                    if result is not None:
                        scored_count += 1
                    else:
                        skipped_count += 1
                except Exception as e:
                    logger.warning(
                        "Batch Sonnet: error evaluating job '%s': %s -- continuing",
                        job_row.get("dedup_key"), e,
                    )
                    skipped_count += 1

                # Flush progress after each job so polling shows real-time updates
                conn.execute(
                    "UPDATE batch_score_sessions SET scored = ?, skipped = ? WHERE id = ?",
                    (scored_count, skipped_count, session_id),
                )
                conn.commit()

            # All jobs processed — mark done
            _finish_session(conn, db_path, session_id, "done", "sonnet")

    except Exception as e:
        logger.error("Batch Sonnet background thread failed: %s", e)
        _mark_session_error(db_path, session_id, str(e)[:500])


def _finish_session(conn, db_path: str, session_id: int, status: str, session_type: str) -> None:
    """Mark a batch session as done and log the activity."""
    conn.execute(
        "UPDATE batch_score_sessions SET status = ?, finished_at = ? WHERE id = ?",
        (status, utc_now_iso(), session_id),
    )
    conn.commit()

    try:
        from job_finder.web.activity_tracker import (
            ACTION_BATCH_SCORE_HAIKU,
            ACTION_BATCH_SCORE_SONNET,
            log_activity,
        )
        action = ACTION_BATCH_SCORE_HAIKU if session_type == "haiku" else ACTION_BATCH_SCORE_SONNET
        session_row = conn.execute(
            "SELECT scored, skipped, total FROM batch_score_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        log_activity(
            db_path,
            action,
            metadata={
                "session_type": session_type,
                "scored": session_row["scored"] if session_row else 0,
                "skipped": session_row["skipped"] if session_row else 0,
                "total": session_row["total"] if session_row else 0,
                "status": "success",
            },
        )
    except Exception:
        logger.warning("_finish_session: failed to log activity for session %s", session_id, exc_info=True)


def _mark_session_error(db_path: str, session_id: int, error_msg: str) -> None:
    """Mark a batch session as errored. Used for background thread import failures."""
    try:
        with standalone_connection(db_path) as conn:
            conn.execute(
                "UPDATE batch_score_sessions SET status = 'error', error_msg = ?, finished_at = ? WHERE id = ?",
                (error_msg, utc_now_iso(), session_id),
            )
            conn.commit()
    except Exception:
        logger.warning("Failed to mark session %s as error: %s", session_id, error_msg, exc_info=True)
