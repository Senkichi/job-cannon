"""Dashboard blueprint — overview stats, activity feed, pipeline summary."""

import logging

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from job_finder.db import (
    get_dashboard_stats,
    get_pending_detections,
    get_pipeline_summary,
    get_recent_activity,
    get_recent_pipeline_events,
    get_recent_runs,
)
from job_finder.config import DEFAULT_HAIKU_THRESHOLD, get_company_denylist
from job_finder.web.claude_client import DEFAULT_DAILY_BUDGET_USD, get_cost_stats
from job_finder.web.db_helpers import get_db
from job_finder.web.model_provider import tier_has_configured_provider

logger = logging.getLogger(__name__)

dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/dashboard")

def _get_ats_context(conn):
    """Query ATS scan stat card data for the Dashboard.

    Returns dict with last_scan info, company counts.
    Handles missing companies table gracefully (pre-migration or error cases).
    """
    try:
        # Most recent scan summary
        last_scan = conn.execute(
            """SELECT scanned_at, SUM(jobs_found) as total_found, COUNT(*) as companies_scanned
               FROM company_scan_log
               WHERE scanned_at = (SELECT MAX(scanned_at) FROM company_scan_log)
               GROUP BY scanned_at"""
        ).fetchone()

        # Company counts
        counts = conn.execute(
            """SELECT COUNT(*) as total,
                      SUM(CASE WHEN ats_probe_status='hit' THEN 1 ELSE 0 END) as ats_tracked
               FROM companies"""
        ).fetchone()

    except Exception:
        last_scan = None
        counts = None

    return {
        "last_scan": last_scan,
        "company_count": (counts["total"] or 0) if counts else 0,
        "ats_tracked_count": (counts["ats_tracked"] or 0) if counts else 0,
    }

def _get_rejection_context(conn):
    """Query rejection insights context for the Dashboard.

    Returns dict with latest_report (sqlite3.Row or None) and
    unreviewed_rejection_count (int).
    Handles missing table gracefully (pre-migration or error cases).
    """
    try:
        latest_report = conn.execute(
            "SELECT id, report_text, rejections_analyzed, generated_at, cost_usd "
            "FROM rejection_reports ORDER BY generated_at DESC LIMIT 1"
        ).fetchone()
        unreviewed_count = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE pipeline_status='rejected' AND rejection_reviewed=0"
        ).fetchone()[0]
    except Exception:
        latest_report = None
        unreviewed_count = 0
    return {
        "latest_report": latest_report,
        "unreviewed_rejection_count": unreviewed_count,
    }

def _count_haiku_scorable(conn, config: dict) -> int:
    """Count unscored jobs that would pass the exclusion filter.

    Replicates the three exclusion checks from exclusion_filter.should_exclude()
    in SQL so the dashboard button count accurately reflects scorable jobs:
    1. Title keyword exclusions (case-insensitive substring)
    2. Company denylist + config exclusions (case-insensitive, trimmed)
    3. Salary floor (salary_max < min_salary * 0.85)
    """
    try:
        conditions = [
            "haiku_score IS NULL",
            "pipeline_status NOT IN ('dismissed', 'archived')",
        ]
        params: list = []

        exclusions = config.get("profile", {}).get("exclusions", {})

        # Title keyword exclusions
        for keyword in exclusions.get("title_keywords", []):
            if keyword:
                conditions.append("LOWER(title) NOT LIKE ?")
                params.append(f"%{keyword.lower()}%")

        # Company exclusions (config list + denylist)
        excluded_companies = {c.lower().strip() for c in exclusions.get("companies", []) if c}
        excluded_companies |= get_company_denylist(config)
        if excluded_companies:
            placeholders = ",".join("?" * len(excluded_companies))
            conditions.append(f"LOWER(TRIM(company)) NOT IN ({placeholders})")
            params.extend(sorted(excluded_companies))

        # Salary floor (min_salary * 0.85)
        min_salary = config.get("profile", {}).get("min_salary")
        if min_salary is not None:
            floor = min_salary * 0.85
            conditions.append(
                "NOT (salary_max IS NOT NULL AND salary_max > 0 AND salary_max < ?)"
            )
            params.append(floor)

        where = " AND ".join(conditions)
        return conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {where}", params).fetchone()[0]
    except Exception:
        return 0

@dashboard_bp.route("/", strict_slashes=False)
def index():
    """Dashboard landing page — stat cards, activity feed, pipeline summary."""
    db_path = current_app.config["DB_PATH"]
    conn = get_db(db_path)

    stats = get_dashboard_stats(conn)
    recent_runs = get_recent_runs(conn, limit=10)
    user_activity = get_recent_activity(conn, limit=15)
    pipeline_summary = get_pipeline_summary(conn)
    pending_detections = get_pending_detections(conn)
    pipeline_events = get_recent_pipeline_events(conn, limit=10)
    config = current_app.config.get("JF_CONFIG", {})
    budget_cap = config.get("scoring", {}).get("daily_budget_usd", DEFAULT_DAILY_BUDGET_USD)
    cost_stats = get_cost_stats(conn, budget_cap=budget_cap)
    pending_count = stats.get("pending_detections", 0)
    rejection_ctx = _get_rejection_context(conn)
    ats_ctx = _get_ats_context(conn)

    # Count jobs eligible for Haiku scoring — mirrors exclusion_filter.should_exclude()
    # so the dashboard button only shows when there are actually scorable jobs.
    haiku_scorable_count = _count_haiku_scorable(conn, config)

    # Count jobs eligible for Sonnet evaluation (haiku_score >= threshold, no sonnet_score)
    threshold = config.get("scoring", {}).get("haiku_threshold", DEFAULT_HAIKU_THRESHOLD)
    try:
        sonnet_eligible_count = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE haiku_score IS NOT NULL AND haiku_score >= ? "
            "AND sonnet_score IS NULL AND jd_full IS NOT NULL",
            (threshold,),
        ).fetchone()[0]
    except Exception:
        sonnet_eligible_count = 0

    # Check tier availability for batch scoring buttons
    try:
        import anthropic as _anthropic
    except ImportError:
        _anthropic = None
    client = None
    if _anthropic is not None:
        try:
            client = _anthropic.Anthropic()
        except Exception:
            pass
    haiku_available = tier_has_configured_provider("haiku", config, client)
    sonnet_available = tier_has_configured_provider("sonnet", config, client)

    return render_template(
        "dashboard/index.html",
        stats=stats,
        recent_runs=recent_runs,
        user_activity=user_activity,
        pipeline_summary=pipeline_summary,
        cost_stats=cost_stats,
        budget_cap=budget_cap,
        pending_detections=pending_detections,
        pending_count=pending_count,
        pipeline_events=pipeline_events,
        latest_report=rejection_ctx["latest_report"],
        unreviewed_rejection_count=rejection_ctx["unreviewed_rejection_count"],
        haiku_scorable_count=haiku_scorable_count,
        sonnet_eligible_count=sonnet_eligible_count,
        haiku_available=haiku_available,
        sonnet_available=sonnet_available,
        ats_last_scan=ats_ctx["last_scan"],
        company_count=ats_ctx["company_count"],
        ats_tracked_count=ats_ctx["ats_tracked_count"],
    )

@dashboard_bp.route("/cost-detail", strict_slashes=False)
def cost_detail():
    """HTMX partial — returns cost breakdown panel."""
    if not request.headers.get("HX-Request"):
        return redirect(url_for("dashboard.index"))
    db_path = current_app.config["DB_PATH"]
    conn = get_db(db_path)
    config = current_app.config.get("JF_CONFIG", {})
    budget_cap = config.get("scoring", {}).get("daily_budget_usd", DEFAULT_DAILY_BUDGET_USD)
    cost_stats = get_cost_stats(conn, budget_cap=budget_cap)

    return render_template(
        "dashboard/_cost_detail.html",
        cost_stats=cost_stats,
        budget_cap=budget_cap,
    )

@dashboard_bp.route("/rejection-analysis", methods=["POST"], strict_slashes=False)
def rejection_analysis():
    """On-demand rejection analysis trigger.

    Calls run_rejection_analysis synchronously and flashes a result message.
    Redirects back to the Dashboard index.
    """
    from job_finder.web.rejection_analyzer import run_rejection_analysis

    db_path = current_app.config["DB_PATH"]
    config = current_app.config.get("JF_CONFIG", {})
    try:
        result = run_rejection_analysis(db_path, config)
        count = result.get("rejections_analyzed", 0)
        if result.get("budget_exceeded"):
            flash("Rejection analysis skipped: monthly budget cap reached.", "warning")
        elif count == 0:
            flash("No unreviewed rejections to analyze.", "info")
        else:
            flash(f"Rejection analysis complete: {count} rejections analyzed.", "success")
    except Exception as e:
        logger.error("On-demand rejection analysis failed: %s", e)
        flash(f"Rejection analysis failed: {e}", "error")
    return redirect(url_for("dashboard.index"))
