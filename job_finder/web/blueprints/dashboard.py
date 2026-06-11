"""Dashboard blueprint — overview stats, activity feed, pipeline summary."""

import logging

from flask import Blueprint, current_app, redirect, render_template, request, url_for

from job_finder.config import DEFAULT_DAILY_BUDGET_USD
from job_finder.db import (
    get_dashboard_stats,
    get_pending_detections,
    get_pipeline_summary,
    get_recent_activity,
    get_recent_pipeline_events,
    get_recent_runs,
)
from job_finder.web.autoheal.health_monitor import degraded_sources
from job_finder.web.claude_client import get_cost_stats
from job_finder.web.db_helpers import get_db
from job_finder.web.exclusion_filter import count_scorable
from job_finder.web.provider_status import cached_tier_available

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


def _get_stats_context(conn, config):
    """Build template context for dashboard stat cards.

    Used by both the full page render and the HTMX stats fragment.
    """
    stats = get_dashboard_stats(conn)
    budget_cap = config.get("scoring", {}).get("daily_budget_usd", DEFAULT_DAILY_BUDGET_USD)
    cost_stats = get_cost_stats(conn, budget_cap=budget_cap)
    pending_count = stats.get("pending_detections", 0)
    ats_ctx = _get_ats_context(conn)
    return {
        "stats": stats,
        "cost_stats": cost_stats,
        "budget_cap": budget_cap,
        "pending_count": pending_count,
        "ats_last_scan": ats_ctx["last_scan"],
        "company_count": ats_ctx["company_count"],
        "ats_tracked_count": ats_ctx["ats_tracked_count"],
    }


def _get_degraded_sources_context(conn) -> dict:
    """Return dict with list of currently-degraded parser sources (dashboard widget)."""
    return {"degraded": degraded_sources(conn)}


# GitHub truncates very long GET URLs; keep the prefilled-issue link bounded.
_MAX_ISSUE_BODY_CHARS = 5_500
_MAX_ISSUE_URL_CHARS = 8_000


def _bundle_issue_url(repo: str, bundle: dict) -> str:
    """Pre-filled new-issue URL for a contribution bundle, capped at 8 000 chars.

    The body carries the recipe + a clipped scrubbed sample; the full bundle
    is pasted by the user from the dashboard textarea ("full bundle below").
    """
    import json as _json
    from urllib.parse import quote

    source = str(bundle.get("source") or "unknown")
    title = quote(f"heal({bundle.get('surface')}): contribution for {source}")
    recipe_json = _json.dumps(bundle.get("recipe") or {}, indent=2)
    sample = str(bundle.get("failing_sample") or "")

    def _build(sample_clip: int) -> str:
        body = (
            f"Automated heal contribution for `{source}`.\n\n"
            f"Drift: `{_json.dumps(bundle.get('drift') or {})}`\n"
            f"App version: {bundle.get('app_version')}\n\n"
            f"Recipe:\n````json\n{recipe_json}\n````\n\n"
            f"PII-scrubbed failing sample (clipped — full bundle below):\n\n"
            f"````\n{sample[:sample_clip]}\n````\n"
        )[:_MAX_ISSUE_BODY_CHARS]
        return f"https://github.com/{repo}/issues/new?title={title}&body={quote(body)}"

    clip = 2_000
    url = _build(clip)
    while len(url) > _MAX_ISSUE_URL_CHARS and clip > 0:
        clip = clip // 2
        url = _build(clip)
    return url


def _get_heal_activity_context(conn, config) -> dict:
    """Heal-audit trail + pending contribution bundles (Phase D / D5 panel).

    ``no_provider`` rows are excluded — keyless instances retry every backoff
    window and would drown the panel in noise.
    """
    import json as _json

    from job_finder.web.autoheal import upstream_reporter

    autoheal_cfg = (config or {}).get("autoheal", {}) or {}
    repo = str(autoheal_cfg.get("upstream_repo") or "Senkichi/job-cannon")

    try:
        audit_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT source, surface, outcome, created_at FROM heal_audit "
                "WHERE outcome != 'no_provider' ORDER BY id DESC LIMIT 10"
            ).fetchall()
        ]
    except Exception:
        audit_rows = []

    bundles = []
    for b in upstream_reporter.pending_bundles():
        payload = {k: v for k, v in b.items() if k != "filename"}
        bundles.append(
            {
                "source": b.get("source"),
                "created_at": b.get("created_at"),
                "filename": b.get("filename"),
                "issue_url": _bundle_issue_url(repo, b),
                "bundle_json": _json.dumps(payload, indent=2),
            }
        )

    return {"heal_audit": audit_rows, "heal_bundles": bundles}


def _get_quick_actions_context(conn, config):
    """Build template context for quick actions section.

    v3.0: the pre-unified-scorer stat blocks are merged into a single
    ``unscored_count`` + ``scoring_available`` pair. Active sessions
    collapse to {sync, scoring}. Legacy ``session_type`` values
    (``'haiku'`` / ``'sonnet'``) written pre-Plan-3 are folded into the
    unified ``'scoring'`` session so the UI reflects the live pipeline
    regardless of which historical route created the row.
    """
    # Detect active (non-terminal) sessions — unified {sync, scoring} semantics
    active_sync = None
    active_scoring = None
    try:
        active_sessions = conn.execute(
            "SELECT id, session_type, status, total, scored, skipped "
            "FROM batch_score_sessions "
            "WHERE status NOT IN ('done', 'error', 'cancelled') "
            "ORDER BY id DESC"
        ).fetchall()
        for s in active_sessions:
            stype = s["session_type"]
            if stype == "sync" and active_sync is None:
                active_sync = s
            elif stype in ("scoring", "haiku", "sonnet") and active_scoring is None:
                # 'haiku'/'sonnet' rows written before the migration fold into
                # the unified 'scoring' session.
                active_scoring = s
    except Exception:
        pass

    # Single count — jobs with classification IS NULL that would pass the exclusion filter.
    unscored_count = 0
    if not active_scoring:
        unscored_count = count_scorable(conn, config)

    scoring_available = cached_tier_available("score", config)

    return {
        "active_sync": active_sync,
        "active_scoring": active_scoring,
        "unscored_count": unscored_count,
        "scoring_available": scoring_available,
    }


@dashboard_bp.route("/", strict_slashes=False)
def index():
    """Dashboard landing page — stat cards, activity feed, pipeline summary."""
    conn = get_db()
    config = current_app.config.get("JF_CONFIG", {})

    stats_ctx = _get_stats_context(conn, config)
    qa_ctx = _get_quick_actions_context(conn, config)

    recent_runs = get_recent_runs(conn, limit=10)
    user_activity = get_recent_activity(conn, limit=15)
    pipeline_summary = get_pipeline_summary(conn)
    pending_detections = get_pending_detections(conn)
    pipeline_events = get_recent_pipeline_events(conn, limit=10)
    inbox_banner = _get_inbox_banner(config, conn)

    return render_template(
        "dashboard/index.html",
        **stats_ctx,
        **qa_ctx,
        **_get_degraded_sources_context(conn),
        **_get_heal_activity_context(conn, config),
        recent_runs=recent_runs,
        user_activity=user_activity,
        pipeline_summary=pipeline_summary,
        pending_detections=pending_detections,
        pipeline_events=pipeline_events,
        inbox_banner=inbox_banner,
    )


def _get_inbox_banner(config: dict, conn):
    """Return banner context if inbox wiring is broken, else None.

    Trigger rule (tightened 2026-05-22 per user choice — was 168h status==red):

    1. Any configured source's auth probe fails (Gmail OAuth or IMAP login).
       Even one failed source banners, even if the other source is healthy
       and activity is fine. Auth failure is high-signal — it never resolves
       on its own.
    2. OR ``run_inbox_check(window_hours=24).status == 'red'`` — i.e. all
       configured sources' auth failed, or zero alert emails in the last
       24 hours. The 24h window catches "ingestion stopped" within one day
       of breakage instead of the prior 7-day buffer.

    Unconfigured installs (source_kind == 'none') never banner — there's
    nothing to be broken. Wrapped in try/except so a check failure never
    breaks the dashboard.
    """
    try:
        from job_finder.web.onboarding.inbox_check import run_inbox_check

        result = run_inbox_check(config, conn, window_hours=24)
        if result.source_kind == "none":
            return None

        any_auth_failed = (result.gmail_auth is not None and not result.gmail_auth.ok) or (
            result.imap_auth is not None and not result.imap_auth.ok
        )

        if result.status == "red" or any_auth_failed:
            if any_auth_failed and result.status != "red":
                failed = (
                    result.gmail_auth
                    if (result.gmail_auth and not result.gmail_auth.ok)
                    else result.imap_auth
                )
                source_name = (
                    "Gmail" if (result.gmail_auth and not result.gmail_auth.ok) else "IMAP"
                )
                return {
                    "summary": f"{source_name} authentication failed",
                    "reason": failed.message
                    if failed
                    else "Auth probe returned not-ok with no message.",
                }
            return {
                "summary": result.summary,
                "reason": result.reason,
            }
        return None
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("inbox banner check failed: %s", type(exc).__name__)
        return None


@dashboard_bp.route("/stats", strict_slashes=False)
def stats_fragment():
    """HTMX fragment — returns refreshed stat cards.

    Triggered by dashboard-refresh event after sync/batch scoring completes.
    """
    conn = get_db()
    config = current_app.config.get("JF_CONFIG", {})
    ctx = _get_stats_context(conn, config)
    return render_template("dashboard/_stats_cards.html", **ctx)


@dashboard_bp.route("/history", strict_slashes=False)
def history_fragment():
    """HTMX fragment — pipeline summary + the activity/history tables.

    Polled every 30s by the ``#dashboard-history`` wrapper (and re-fetched on
    ``dashboard-refresh`` after sync/scoring/ATS runs) so Pipeline Summary,
    Recent Activity, User Activity, and Pipeline Changes live-update without a
    full page reload. Reuses the same query helpers + partial as the full-page
    render in ``index()`` so the two never drift.
    """
    conn = get_db()
    return render_template(
        "dashboard/_dashboard_history.html",
        recent_runs=get_recent_runs(conn, limit=10),
        user_activity=get_recent_activity(conn, limit=15),
        pipeline_summary=get_pipeline_summary(conn),
        pipeline_events=get_recent_pipeline_events(conn, limit=10),
    )


@dashboard_bp.route("/review-queue", strict_slashes=False)
def review_queue_fragment():
    """HTMX fragment — pending-detection cards + an OOB header-badge refresh.

    Refetched on ``sse:detections-changed`` so detections surfaced by the
    background pipeline-detection job fill the review queue live, without a
    page reload. User confirm/dismiss keeps its own card-level swap path (see
    blueprints/detections.py) — this route only serves the background-driven
    refresh, so the two never fight over the same DOM.
    """
    conn = get_db()
    pending_detections = get_pending_detections(conn)
    pending_count = get_dashboard_stats(conn).get("pending_detections", 0)
    queue_html = render_template(
        "dashboard/_review_queue.html", pending_detections=pending_detections
    )
    header_oob = render_template(
        "dashboard/_pipeline_review_header.html", pending_count=pending_count, oob=True
    )
    return queue_html + header_oob


@dashboard_bp.route("/quick-actions", strict_slashes=False)
def quick_actions_fragment():
    """HTMX fragment — returns refreshed quick actions with active session detection.

    Triggered by dashboard-refresh event (with 5s delay) after sync/batch scoring completes.
    Detects active sessions and shows progress bars or fresh buttons with updated counts.
    """
    conn = get_db()
    config = current_app.config.get("JF_CONFIG", {})
    ctx = _get_quick_actions_context(conn, config)
    return render_template("dashboard/_quick_actions.html", **ctx)


@dashboard_bp.route("/cost-detail", strict_slashes=False)
def cost_detail():
    """HTMX partial — returns cost breakdown panel."""
    if not request.headers.get("HX-Request"):
        return redirect(url_for("dashboard.index"))
    conn = get_db()
    config = current_app.config.get("JF_CONFIG", {})
    budget_cap = config.get("scoring", {}).get("daily_budget_usd", DEFAULT_DAILY_BUDGET_USD)
    cost_stats = get_cost_stats(conn, budget_cap=budget_cap)

    return render_template(
        "dashboard/_cost_detail.html",
        cost_stats=cost_stats,
        budget_cap=budget_cap,
    )


@dashboard_bp.route("/degraded-sources", strict_slashes=False)
def degraded_sources_fragment():
    """HTMX fragment — parser-health widget showing currently-degraded sources.

    Non-HTMX direct browser hits redirect to the dashboard index so the widget
    is never rendered as a bare standalone page.
    """
    if not request.headers.get("HX-Request"):
        return redirect(url_for("dashboard.index"))
    conn = get_db()
    return render_template(
        "dashboard/_degraded_sources.html",
        **_get_degraded_sources_context(conn),
    )
