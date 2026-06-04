"""Companies blueprint — Company registry management routes.

Routes:
    GET  /companies                            -- Companies list with search and ATS filter
    GET  /companies/<id>/expand                -- HTMX: expand company row with jobs + scan history
    GET  /companies/<id>/collapse              -- HTMX: collapse company row back to compact
    POST /companies/add                        -- Create new company record
    POST /companies/<id>/toggle                -- Toggle scan_enabled between 0 and 1
    POST /companies/<id>/update-slug           -- Update ATS platform/slug manually
    POST /companies/scan                       -- Start async ATS scan; returns polling progress fragment
    GET  /companies/scan/status/<id>           -- Poll route for scan progress / terminal result
    POST /companies/<id>/research              -- Start or return cached company research
    GET  /companies/<id>/research/status/<rid> -- Poll research status
"""

import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from job_finder.json_utils import utc_now_iso
from job_finder.web.ats_prober import probe_single_company
from job_finder.web.ats_scanner import probe_ats_slugs, run_ats_scan
from job_finder.web.db_helpers import (
    _POLLING_TIMEOUT_MINUTES,
    PollingSessionConfig,
    get_db,
    render_polling_status,
    standalone_connection,
)
from job_finder.web.live_events import COMPANIES_CHANGED, JOBS_CHANGED
from job_finder.web.live_events import publish as publish_live

logger = logging.getLogger(__name__)

companies_bp = Blueprint("companies", __name__, url_prefix="/companies")

# Validated allowlist for sort_by (no parameterized column names in SQLite)
_SORT_ALLOWLIST = {"name", "ats_platform", "last_scanned_at", "jobs_found_total"}
_ATS_PLATFORM_FILTER_VALUES = {"lever", "greenhouse", "ashby", "none", ""}

_PAGE_SIZE = 50


@companies_bp.route("/", strict_slashes=False)
def index():
    """Companies list page with sortable table, search, and ATS filter."""
    conn = get_db()

    search = request.args.get("search", "").strip()
    ats_platform = request.args.get("ats_platform", "").strip().lower()
    sort_by = request.args.get("sort_by", "name")
    page = request.args.get("page", 1, type=int)
    if page < 1:
        page = 1

    # Validate sort_by against allowlist
    if sort_by not in _SORT_ALLOWLIST:
        sort_by = "name"

    # Build query
    where_clauses = []
    params = []

    if search:
        where_clauses.append("(c.name LIKE ? OR c.name_raw LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])

    if ats_platform == "none":
        where_clauses.append("c.ats_platform IS NULL")
    elif ats_platform in ("lever", "greenhouse", "ashby"):
        where_clauses.append("c.ats_platform = ?")
        params.append(ats_platform)

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    # Total count for display
    total_count = conn.execute(f"SELECT COUNT(*) FROM companies c {where_sql}", params).fetchone()[
        0
    ]

    # Paginated query
    offset = (page - 1) * _PAGE_SIZE
    companies = conn.execute(
        f"""SELECT c.*,
               COUNT(j.dedup_key) as job_count_live
            FROM companies c
            LEFT JOIN jobs j ON j.company_id = c.id
            {where_sql}
            GROUP BY c.id
            ORDER BY c.{sort_by} ASC NULLS LAST
            LIMIT ? OFFSET ?""",
        params + [_PAGE_SIZE, offset],
    ).fetchall()

    has_more = (offset + len(companies)) < total_count

    is_htmx = request.headers.get("HX-Request")
    if is_htmx:
        # Page 2+ returns rows-only partial (no container/header)
        template = "companies/_rows_partial.html" if page > 1 else "companies/_table.html"
        return render_template(
            template,
            companies=companies,
            search=search,
            ats_platform=ats_platform,
            sort_by=sort_by,
            page=page,
            has_more=has_more,
            total_count=total_count,
        )

    # Compute health metrics for full page
    health = _compute_health_metrics(conn)

    # Detect any in-flight ATS scan so the polling progress fragment renders
    # on a fresh page load. Without this, clicking 'Scan ATS' then navigating
    # away and back hides all scan progress until the next manual click,
    # which was reported as confusing UX (no indication a scan is running).
    running_row = _find_running_scan_session(conn)
    running_scan = (
        {
            "session_id": running_row["id"],
            "total": running_row["total"] or 0,
            "scanned": running_row["scored"] or 0,
        }
        if running_row
        else None
    )

    return render_template(
        "companies/index.html",
        companies=companies,
        search=search,
        ats_platform=ats_platform,
        sort_by=sort_by,
        page=page,
        has_more=has_more,
        total_count=total_count,
        health=health,
        running_scan=running_scan,
    )


@companies_bp.route("/health", strict_slashes=False)
def health_fragment():
    """HTMX fragment — the 5 pipeline-health stat cards.

    Refetched on ``sse:companies-changed`` / ``sse:jobs-changed`` so the cards
    reflect background ATS scan / slug probe / linkage / hygiene runs live.
    Reuses ``_compute_health_metrics`` + the ``_health_metrics.html`` partial
    shared with the full-page render so the two never drift.
    """
    conn = get_db()
    return render_template("companies/_health_metrics.html", health=_compute_health_metrics(conn))


@companies_bp.route("/<int:company_id>/expand", strict_slashes=False)
def expand(company_id):
    """HTMX: expand company row with jobs and scan history."""
    conn = get_db()

    company = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()

    if company is None:
        return "Company not found", 404

    # Jobs for this company (limit 20, ordered by classification_rank DESC,
    # sub_score_sum DESC — v3.0 Phase 34 Plan 3 Commit A)
    jobs = conn.execute(
        """SELECT *,
                  CASE classification
                      WHEN 'apply'    THEN 4
                      WHEN 'consider' THEN 3
                      WHEN 'skip'     THEN 2
                      WHEN 'reject'   THEN 1
                      ELSE 0
                  END AS classification_rank,
                  (COALESCE(json_extract(sub_scores_json, '$.title_fit'), 0) +
                   COALESCE(json_extract(sub_scores_json, '$.location_fit'), 0) +
                   COALESCE(json_extract(sub_scores_json, '$.comp_fit'), 0) +
                   COALESCE(json_extract(sub_scores_json, '$.domain_match'), 0) +
                   COALESCE(json_extract(sub_scores_json, '$.seniority_match'), 0) +
                   COALESCE(json_extract(sub_scores_json, '$.skills_match'), 0))
                  AS effective_score
           FROM jobs
           WHERE company_id = ?
           ORDER BY classification_rank DESC, effective_score DESC
           LIMIT 20""",
        (company_id,),
    ).fetchall()

    # Last 5 scan log entries
    scan_history = conn.execute(
        """SELECT * FROM company_scan_log
           WHERE company_id = ?
           ORDER BY scanned_at DESC
           LIMIT 5""",
        (company_id,),
    ).fetchall()

    # Most recent research row (for showing cached results inline)
    research = conn.execute(
        "SELECT * FROM company_research WHERE company_id = ? ORDER BY requested_at DESC LIMIT 1",
        (company_id,),
    ).fetchone()

    return render_template(
        "companies/_row_expanded.html",
        company=company,
        jobs=jobs,
        scan_history=scan_history,
        research=research,
    )


@companies_bp.route("/<int:company_id>/collapse", strict_slashes=False)
def collapse(company_id):
    """HTMX: collapse company row back to compact view."""
    conn = get_db()

    company = conn.execute(
        """SELECT c.*, COUNT(j.dedup_key) as job_count_live
           FROM companies c
           LEFT JOIN jobs j ON j.company_id = c.id
           WHERE c.id = ?
           GROUP BY c.id""",
        (company_id,),
    ).fetchone()

    if company is None:
        return "Company not found", 404

    return render_template("companies/_row.html", company=company)


@companies_bp.route("/add", methods=["POST"], strict_slashes=False)
def add():
    """Create a new company record and trigger ATS probe."""
    conn = get_db()

    company_name = request.form.get("company_name", "").strip()
    homepage_url = request.form.get("homepage_url", "").strip() or None

    if not company_name:
        flash("Company name is required.", "error")
        return redirect(url_for("companies.index"))

    try:
        from job_finder.web.ats_scanner import upsert_company

        company_id = upsert_company(
            conn,
            name=company_name,
            homepage_url=homepage_url,
            ats_probe_status="pending",
        )
        conn.commit()

        if company_id:
            flash(f"Company '{company_name}' added. ATS probe scheduled.", "success")
        else:
            flash(f"Company '{company_name}' already exists or could not be created.", "info")

    except Exception as e:
        logger.error("Failed to add company '%s': %s", company_name, e)
        flash(f"Error adding company: {e}", "error")

    return redirect(url_for("companies.index"))


@companies_bp.route("/<int:company_id>/toggle", methods=["POST"], strict_slashes=False)
def toggle(company_id):
    """Toggle scan_enabled for a company. Returns updated _row.html fragment."""
    conn = get_db()

    company = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()

    if company is None:
        return "Company not found", 404

    new_enabled = 0 if company["scan_enabled"] else 1
    now = utc_now_iso()
    conn.execute(
        "UPDATE companies SET scan_enabled = ?, updated_at = ? WHERE id = ?",
        (new_enabled, now, company_id),
    )
    conn.commit()

    # Fetch updated company with job count
    updated_company = conn.execute(
        """SELECT c.*, COUNT(j.dedup_key) as job_count_live
           FROM companies c
           LEFT JOIN jobs j ON j.company_id = c.id
           WHERE c.id = ?
           GROUP BY c.id""",
        (company_id,),
    ).fetchone()

    return render_template("companies/_row.html", company=updated_company)


@companies_bp.route("/<int:company_id>/update-slug", methods=["POST"], strict_slashes=False)
def update_slug(company_id):
    """Update ATS platform and slug manually. Returns updated expanded view."""
    conn = get_db()

    company = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()

    if company is None:
        return "Company not found", 404

    ats_platform = request.form.get("ats_platform", "").strip() or None
    ats_slug = request.form.get("ats_slug", "").strip() or None

    now = utc_now_iso()
    try:
        conn.execute(
            """UPDATE companies
               SET ats_platform = ?,
                   ats_slug = ?,
                   ats_probe_status = 'pending',
                   updated_at = ?
               WHERE id = ?""",
            (ats_platform, ats_slug, now, company_id),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        # m076's UNIQUE(ats_platform, ats_slug) gate. Another company
        # already owns this pair. Surface the conflict to the operator
        # via flash + form re-render — never silently overwrite the
        # legitimate owner.
        owner = conn.execute(
            "SELECT id, name_raw FROM companies "
            "WHERE ats_platform = ? AND ats_slug = ? AND id != ?",
            (ats_platform, ats_slug, company_id),
        ).fetchone()
        owner_id = owner["id"] if owner else None
        owner_name = owner["name_raw"] if owner else None
        logger.warning(
            "update_slug: admin override blocked for company id=%d on "
            "%s/%s — already owned by id=%s (%r). exc=%s",
            company_id,
            ats_platform,
            ats_slug,
            owner_id,
            owner_name,
            exc,
        )
        flash(
            f"Cannot set ATS to {ats_platform}/{ats_slug} — already owned "
            f"by company id={owner_id} ({owner_name!r})",
            "error",
        )
        # Re-render the (unchanged) expanded row so the operator sees the
        # flash and the current state. Skip the commit — the original
        # values are preserved.
        unchanged_company = conn.execute(
            "SELECT * FROM companies WHERE id = ?", (company_id,)
        ).fetchone()
        return render_template(
            "companies/_row_expanded.html",
            company=unchanged_company,
            jobs=[],
            scan_history=[],
            research=None,
        )

    # Reload company data
    updated_company = conn.execute(
        "SELECT * FROM companies WHERE id = ?", (company_id,)
    ).fetchone()
    jobs = conn.execute(
        """SELECT *,
                  CASE classification
                      WHEN 'apply'    THEN 4
                      WHEN 'consider' THEN 3
                      WHEN 'skip'     THEN 2
                      WHEN 'reject'   THEN 1
                      ELSE 0
                  END AS classification_rank,
                  (COALESCE(json_extract(sub_scores_json, '$.title_fit'), 0) +
                   COALESCE(json_extract(sub_scores_json, '$.location_fit'), 0) +
                   COALESCE(json_extract(sub_scores_json, '$.comp_fit'), 0) +
                   COALESCE(json_extract(sub_scores_json, '$.domain_match'), 0) +
                   COALESCE(json_extract(sub_scores_json, '$.seniority_match'), 0) +
                   COALESCE(json_extract(sub_scores_json, '$.skills_match'), 0))
                  AS effective_score
           FROM jobs WHERE company_id = ?
           ORDER BY classification_rank DESC, effective_score DESC LIMIT 20""",
        (company_id,),
    ).fetchall()
    scan_history = conn.execute(
        "SELECT * FROM company_scan_log WHERE company_id = ? ORDER BY scanned_at DESC LIMIT 5",
        (company_id,),
    ).fetchall()

    research = conn.execute(
        "SELECT * FROM company_research WHERE company_id = ? ORDER BY requested_at DESC LIMIT 1",
        (company_id,),
    ).fetchone()

    return render_template(
        "companies/_row_expanded.html",
        company=updated_company,
        jobs=jobs,
        scan_history=scan_history,
        research=research,
    )


_SESSION_TYPE_ATS_SCAN = "ats_scan"


def _find_running_scan_session(conn):
    """Return the most recent *live* in-flight ats_scan session row, or None.

    Used by index() so the polling progress fragment auto-mounts when the
    user navigates back to /companies during a scan.

    Liveness is defined by heartbeat freshness, NOT by status='running' alone.
    The bg thread's daemon dies with the Flask process, so a process that
    crashes mid-scan leaves an orphan 'running' row behind. Returning such a
    row would re-mount a phantom progress banner that the poller immediately
    flips to "No progress in >30 min". We therefore apply the SAME staleness
    predicate render_polling_status uses (COALESCE(last_tick_at, started_at)
    within _POLLING_TIMEOUT_MINUTES of now) so a dead orphan is never surfaced
    as in flight. A startup reaper (db_helpers.reap_orphan_sessions) also flips
    these to 'error', but this predicate is the defense that matters per-request.

    SQLite ``datetime()`` normalizes the stored naive-UTC ISO timestamps
    (with 'T' separator + fractional seconds) so the string comparison against
    ``datetime('now', ...)`` — also naive UTC — is valid. No timezone mismatch.
    """
    try:
        return conn.execute(
            "SELECT id, total, scored FROM batch_score_sessions "
            "WHERE session_type = ? AND status = 'running' "
            "AND datetime(COALESCE(last_tick_at, started_at)) "
            f"    > datetime('now', '-{_POLLING_TIMEOUT_MINUTES} minutes') "
            "ORDER BY id DESC LIMIT 1",
            (_SESSION_TYPE_ATS_SCAN,),
        ).fetchone()
    except Exception:
        return None


def _scannable_count(conn, config: dict) -> int:
    """Estimate the total companies run_ats_scan will iterate (Phase A + Phase C).

    Used only for the initial UI render before the bg-thread's progress
    callback ticks the first time. The first tick corrects ``total`` on the
    session row from the live count computed inside run_ats_scan, so this
    estimate being slightly off is harmless.
    """
    from job_finder.web.ats_scanner._run import (
        _DEFAULT_HIGH_SCORE_THRESHOLD,
        _count_phase_a_eligible,
        _count_phase_c_eligible,
    )

    try:
        threshold = int(
            config.get("ats", {}).get(
                "high_score_history_threshold", _DEFAULT_HIGH_SCORE_THRESHOLD
            )
        )
        return _count_phase_a_eligible(conn, threshold) + _count_phase_c_eligible(conn, threshold)
    except Exception:
        return 0


@companies_bp.route("/scan", methods=["POST"], strict_slashes=False)
def scan():
    """Start an async ATS scan; return the polling progress fragment.

    Mirrors the batch_scoring pattern (session row + bg thread + polling
    endpoint + done fragment). Replaces the previous synchronous route
    that blocked the request until the scan finished (multi-minute wait
    with no progress feedback).

    Returns the progress fragment that HTMX-polls /companies/scan/status
    every 2s until terminal.
    """
    db_path = current_app.config["DB_PATH"]
    config = current_app.config.get("JF_CONFIG", {})
    testing = current_app.config.get("TESTING", False)

    with standalone_connection(db_path) as conn:
        total = _scannable_count(conn, config)

        if total == 0:
            return render_template(
                "companies/_scan_ats_done.html",
                status="done",
                result=None,
                message="No companies eligible to scan (need ats_probe_status='hit' AND scan_enabled=1).",
                error_msg=None,
            )

        now = utc_now_iso()
        conn.execute(
            "INSERT INTO batch_score_sessions (session_type, status, total, scored, started_at) "
            "VALUES (?, 'running', ?, 0, ?)",
            (_SESSION_TYPE_ATS_SCAN, total, now),
        )
        conn.commit()
        session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    if not testing:
        t = threading.Thread(
            target=_run_ats_scan_bg,
            args=(db_path, session_id, config),
            daemon=True,
        )
        t.start()

    return render_template(
        "companies/_scan_ats_progress.html",
        session_id=session_id,
        total=total,
        scanned=0,
    )


def _scan_progress_ctx(session) -> dict:
    # Per-company progress: the bg thread's callback updates ``scored`` and
    # ``total`` after each company scan, so the fragment renders the live
    # "Scanned X of N" count on each 2s poll.
    return {
        "session_id": session["id"],
        "total": session["total"],
        "scanned": session["scored"] or 0,
    }


def _scan_done_ctx(session, status: str, error_msg: str | None) -> dict:
    """Reconstruct the run_ats_scan summary dict from the session row.

    The bg thread serializes the summary into ``batch_score_sessions.error_msg``
    when status='done' (the column is reused for both purposes; status is
    the discriminator). The shared ``render_polling_status`` helper only
    surfaces ``error_msg`` to ``done_ctx`` for status='error', so we read
    the column straight off the ``session`` row when status='done'.
    """
    result = None
    msg = None
    err = None
    if status == "done":
        raw_summary = session["error_msg"]
        if raw_summary:
            try:
                result = json.loads(raw_summary)
            except (json.JSONDecodeError, TypeError):
                msg = raw_summary
    elif status == "error":
        err = error_msg
    elif status == "cancelled":
        msg = "Scan cancelled."

    return {
        "status": status,
        "result": result,
        "message": msg,
        "error_msg": err,
    }


_ATS_SCAN_HX_TRIGGER = {"dashboard-refresh": None, "jobs-updated": None}


@companies_bp.route("/scan/status/<int:session_id>", strict_slashes=False)
def scan_status(session_id):
    """Poll route for ATS scan progress (mirrors batch_scoring.batch_score_status)."""
    return render_polling_status(
        current_app.config["DB_PATH"],
        session_id,
        PollingSessionConfig(
            progress_template="companies/_scan_ats_progress.html",
            done_template="companies/_scan_ats_done.html",
            progress_ctx=_scan_progress_ctx,
            done_ctx=_scan_done_ctx,
            not_found_ctx={
                "status": "error",
                "result": None,
                "message": None,
                "error_msg": "Scan session not found.",
            },
            hx_trigger_after_settle=_ATS_SCAN_HX_TRIGGER,
            session_label="ATS scan",
        ),
    )


def _run_ats_scan_bg(db_path: str, session_id: int, config: dict) -> None:
    """Background thread: run probe + scan, serialize summary into the session.

    On success, the summary dict is JSON-serialized into
    ``batch_score_sessions.error_msg`` (the column is used for both
    structured-success payloads and error strings; ``status`` is the
    discriminator).

    The session's ``scored`` column tracks ``companies_scanned`` so the
    progress fragment's percent-complete bar lines up with the planned
    total. ``total`` was set by the POST route from ``_scannable_count``.
    """

    def _tick(scanned: int, total: int) -> None:
        """Persist live (scanned, total) so the polling fragment shows N of M.

        Opens a transient connection per tick — runs inside the scanner's
        per-company loop which already sleeps 0.5-1.0s between companies,
        so the per-write cost is negligible. Tick failures must never abort
        the scan, so the connection helper's exceptions are swallowed.
        """
        try:
            with standalone_connection(db_path) as tick_conn:
                tick_conn.execute(
                    "UPDATE batch_score_sessions SET scored=?, total=?, last_tick_at=? WHERE id=?",
                    (scanned, total, utc_now_iso(), session_id),
                )
                tick_conn.commit()
        except Exception:
            logger.debug("scan-progress tick failed for session %s", session_id, exc_info=True)

    try:
        with standalone_connection(db_path) as conn:
            probe_result = probe_ats_slugs(db_path, config)
            logger.info("ATS probe before scan: %s", probe_result)
            result = run_ats_scan(db_path, config, progress_callback=_tick)
            result["probe"] = probe_result

            conn.execute(
                "UPDATE batch_score_sessions "
                "SET status='done', scored=?, error_msg=?, finished_at=? "
                "WHERE id=?",
                (
                    result.get("companies_scanned", 0),
                    json.dumps(result),
                    utc_now_iso(),
                    session_id,
                ),
            )
            conn.commit()

        # ATS scan discovered jobs and updated company ATS state — push live
        # events so the companies health cards, dashboard, and job board reflect
        # it on every open page (the polling tab also gets HX-Trigger).
        for _ev in (COMPANIES_CHANGED, JOBS_CHANGED):
            publish_live(_ev)
    except Exception as e:
        logger.error("ATS scan background thread failed: %s", e)
        try:
            with standalone_connection(db_path) as conn:
                conn.execute(
                    "UPDATE batch_score_sessions "
                    "SET status='error', error_msg=?, finished_at=? "
                    "WHERE id=?",
                    (str(e)[:500], utc_now_iso(), session_id),
                )
                conn.commit()
        except Exception:
            logger.exception("Failed to record ATS scan error for session %s", session_id)


@companies_bp.route("/<int:company_id>/retry", methods=["POST"], strict_slashes=False)
def retry(company_id):
    """Immediately re-probe a company in error or unreachable state.

    Only valid for companies with ats_probe_status='error' or
    ats_probe_status='miss' AND miss_reason='unreachable'. Returns 400 for
    all other statuses (hit, pending, regular miss).

    Returns updated _row.html fragment (innerHTML swap into #company-{id}).
    """
    config = current_app.config.get("JF_CONFIG", {})
    conn = get_db()

    company = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()

    if company is None:
        return "Company not found", 404

    status = company["ats_probe_status"]
    miss_reason = dict(company).get("miss_reason")

    # Only allow retry for error or unreachable companies
    is_retryable = status == "error" or (status == "miss" and miss_reason == "unreachable")
    if not is_retryable:
        return "Company is not in error or unreachable state", 400

    try:
        probe_single_company(company_id, conn, config)
    except Exception as e:
        logger.error("Retry probe failed for company %d: %s", company_id, e)

    # Fetch updated company with job count for re-rendering
    updated_company = conn.execute(
        """SELECT c.*, COUNT(j.dedup_key) as job_count_live
           FROM companies c
           LEFT JOIN jobs j ON j.company_id = c.id
           WHERE c.id = ?
           GROUP BY c.id""",
        (company_id,),
    ).fetchone()

    return render_template("companies/_row.html", company=updated_company)


# ---------------------------------------------------------------------------
# Health metrics helper
# ---------------------------------------------------------------------------


def _compute_health_metrics(conn) -> dict:
    """Compute pipeline health metrics for the companies index page."""
    total = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]

    pending_probe = conn.execute(
        "SELECT COUNT(*) FROM companies WHERE ats_probe_status = 'pending'"
    ).fetchone()[0]

    homepage_count = conn.execute(
        "SELECT COUNT(*) FROM companies WHERE homepage_url IS NOT NULL AND homepage_url != ''"
    ).fetchone()[0]
    homepage_pct = round(100 * homepage_count / total) if total else 0

    # Enrichment: companies with industry or company_size populated
    enriched_count = conn.execute(
        "SELECT COUNT(*) FROM companies WHERE industry IS NOT NULL OR company_size IS NOT NULL"
    ).fetchone()[0]
    enrichment_pct = round(100 * enriched_count / total) if total else 0

    # Unlinked jobs: jobs with no company_id
    unlinked_jobs = conn.execute("SELECT COUNT(*) FROM jobs WHERE company_id IS NULL").fetchone()[
        0
    ]

    # Last scan age
    last_scan_row = conn.execute(
        "SELECT MAX(scanned_at) as last_scan FROM company_scan_log"
    ).fetchone()
    last_scan_at = last_scan_row["last_scan"] if last_scan_row else None
    last_scan_age_days = None
    if last_scan_at:
        try:
            last_dt = datetime.fromisoformat(last_scan_at)
            # last_scan_at is stored as naive UTC (see arch-store-utc-render-local);
            # compare against UTC-now to avoid an N-hour-tz-offset skew.
            last_scan_age_days = (datetime.now(UTC).replace(tzinfo=None) - last_dt).days
        except (ValueError, TypeError):
            pass

    return {
        "pending_probe": pending_probe,
        "homepage_pct": homepage_pct,
        "enrichment_pct": enrichment_pct,
        "unlinked_jobs": unlinked_jobs,
        "last_scan_at": last_scan_at,
        "last_scan_age_days": last_scan_age_days,
    }


# ---------------------------------------------------------------------------
# Company research routes
# ---------------------------------------------------------------------------


@companies_bp.route("/<int:company_id>/research", methods=["POST"], strict_slashes=False)
def research(company_id):
    """Start or return cached company research.

    If a recent done/generating research row exists, return it immediately.
    Otherwise, insert a new generating row and launch background research.
    Returns an HTMX fragment (polling or final section).
    """
    db_path = current_app.config["DB_PATH"]
    config = current_app.config.get("JF_CONFIG", {})
    conn = get_db(db_path)

    company = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()

    if company is None:
        return "Company not found", 404

    from job_finder.web.company_research import (
        get_cached_company_research,
        start_company_research,
    )

    cached = get_cached_company_research(conn, company_id)
    if cached and cached["status"] == "done":
        return render_template(
            "companies/_research_section.html",
            research=cached,
            company=company,
        )
    if cached and cached["status"] in ("generating", "pending"):
        return render_template(
            "companies/_research_generating.html",
            company_id=company_id,
            research_id=cached["id"],
        )

    research_id = start_company_research(conn, company_id, db_path, config)
    return render_template(
        "companies/_research_generating.html",
        company_id=company_id,
        research_id=research_id,
    )


@companies_bp.route(
    "/<int:company_id>/research/status/<int:research_id>",
    strict_slashes=False,
)
def research_status(company_id, research_id):
    """Poll research generation status. Returns final section or keeps polling."""
    conn = get_db()

    company = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()

    if company is None:
        return "Company not found", 404

    row = conn.execute(
        "SELECT * FROM company_research WHERE id = ? AND company_id = ?",
        (research_id, company_id),
    ).fetchone()

    if row is None:
        return "Research not found", 404

    research = dict(row)

    # Check for timeout on generating rows (>10 min)
    if research["status"] == "generating":
        try:
            requested = datetime.fromisoformat(research["requested_at"])
            if requested.tzinfo is not None:
                requested = requested.astimezone(UTC).replace(tzinfo=None)
            age_minutes = (datetime.now(UTC).replace(tzinfo=None) - requested).total_seconds() / 60
            if age_minutes > 10:
                now = utc_now_iso()
                conn.execute(
                    "UPDATE company_research SET status = 'error', error_msg = ?, completed_at = ? WHERE id = ?",
                    ("Research timed out", now, research_id),
                )
                conn.commit()
                research["status"] = "error"
                research["error_msg"] = "Research timed out"
        except (ValueError, TypeError):
            pass

    if research["status"] in ("done", "error"):
        return render_template(
            "companies/_research_section.html",
            research=research,
            company=company,
        )

    # Still generating — keep polling
    return render_template(
        "companies/_research_generating.html",
        company_id=company_id,
        research_id=research_id,
    )
