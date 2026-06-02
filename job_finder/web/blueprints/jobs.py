"""Jobs blueprint -- full Job Board routes with HTMX partials."""

import logging
import time as _time
from datetime import UTC, datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)

from job_finder.db import (
    get_distinct_country_codes,
    get_distinct_locations,
    get_distinct_sources,
    get_distinct_workplace_types,
    get_filtered_jobs,
    get_job,
    get_pipeline_events,
    load_job_context,
    update_pipeline_status,
    upsert_job,
)
from job_finder.models import Job
from job_finder.secrets import get_secret
from job_finder.web._http_constants import _HEADERS, _TIMEOUT
from job_finder.web.activity_tracker import (
    ACTION_EXPAND_JOB,
    ACTION_PASTE_JD,
    ACTION_RESCORE,
    ACTION_SAVE_JD,
    ACTION_STATUS_CHANGE,
    log_activity,
)


def _get_stale_count(conn) -> int:
    """Return count of jobs with is_stale = 1."""
    row = conn.execute("SELECT COUNT(*) FROM jobs WHERE is_stale = 1").fetchone()
    return row[0] if row else 0


from job_finder.web.blueprints import PIPELINE_STATUSES
from job_finder.web.data_enricher import enrich_job
from job_finder.web.db_helpers import get_db

logger = logging.getLogger(__name__)

jobs_bp = Blueprint("jobs", __name__, url_prefix="/jobs")


def _host_as_company_label(netloc: str) -> str:
    """Derive a readable fallback company label from a URL host."""
    h = (netloc or "").lower().removeprefix("www.")
    parts = [p for p in h.split(".") if p]
    if len(parts) >= 2:
        base = f"{parts[-2]} {parts[-1]}"
    elif parts:
        base = parts[0]
    else:
        base = "employer"
    return base.replace("-", " ").title()


def infer_title_company_from_listing_url(url: str) -> tuple[str, str]:
    """Best-effort (title, company) from listing HTML for bootstrap rows.

    Used when the operator pastes a listing URL without title/company.
    Never raises — returns safe non-empty strings suitable for ``Job()``.
    """
    parsed = urlparse(url)
    host_fallback = _host_as_company_label(parsed.hostname or "")

    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
    except Exception as exc:
        logger.debug("infer listing: fetch failed for %s: %s", url[:120], exc)
        return ("Job listing", host_fallback)

    try:
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        logger.debug("infer listing: parse failed for %s: %s", url[:120], exc)
        return ("Job listing", host_fallback)

    raw = ""
    if soup.title and soup.title.string:
        raw = soup.title.string.strip()

    og_company = ""
    og_site = soup.find("meta", attrs={"property": "og:site_name"})
    if og_site and og_site.get("content"):
        og_company = str(og_site["content"]).strip()

    title_guess = raw or "Job listing"
    company_guess = og_company

    for sep in (" | ", " – ", " - ", " · ", " :: ", " — "):
        if sep in raw:
            parts = [p.strip() for p in raw.split(sep, 1)]
            if len(parts) == 2 and parts[0] and parts[1]:
                title_guess, company_guess = parts[0], parts[1] or company_guess
                break

    title_guess = (title_guess or "Job listing")[:300]
    if not (company_guess or "").strip():
        company_guess = host_fallback
    company_guess = (company_guess or host_fallback)[:200]
    return title_guess, company_guess


def _safe_float(raw: str, param_name: str) -> float | None:
    """Coerce a query-string value to float, or abort 400 on malformed input."""
    if not raw:
        return None
    try:
        return float(raw)
    except (ValueError, TypeError):
        abort(400, description=f"Invalid value for {param_name}: {raw!r}")


def _safe_int(raw: str, param_name: str) -> int | None:
    """Coerce a query-string value to int, or abort 400 on malformed input."""
    if not raw:
        return None
    try:
        return int(raw)
    except (ValueError, TypeError):
        abort(400, description=f"Invalid value for {param_name}: {raw!r}")


def _get_filter_kwargs() -> dict:
    """Extract and coerce filter query parameters from request.args."""
    args = request.args

    # Multi-select status: getlist returns [] when absent, or [""] for a blank submit
    statuses = [s for s in args.getlist("status") if s]

    return {
        "status": statuses if len(statuses) > 1 else (statuses[0] if statuses else None),
        "location": args.get("location") or None,
        "min_score": _safe_float(args.get("min_score", ""), "min_score"),
        "max_score": _safe_float(args.get("max_score", ""), "max_score"),
        "salary_min": _safe_int(args.get("salary_min", ""), "salary_min"),
        "source": args.get("source") or None,
        "posted_within": args.get("posted_within") or None,
        "freshness": args.get("freshness") or None,
        "date_from": args.get("date_from") or None,
        "date_to": args.get("date_to") or None,
        "country": args.get("country") or None,
        "workplace_type": args.get("workplace_type") or None,
        "sort_by": args.get("sort_by", "score"),
        "sort_dir": args.get("sort_dir", "DESC"),
        "limit": 200,
        "hide_stale": args.get("hide_stale") == "on" if args else True,
        "show_hidden": args.get("show_hidden") == "on",
    }


def _parse_stored_ts_as_local(iso_str: str) -> datetime | None:
    """Parse a stored ISO timestamp and return it as naive OS-local datetime.

    Storage contract (see ``arch-store-utc-render-local``): all DB-stored
    timestamps are naive UTC. Some legacy rows / external-API responses may
    carry an explicit ``+00:00`` offset. Both shapes are coerced to naive
    local for display math.
    """
    if not iso_str or not isinstance(iso_str, str):
        return None
    try:
        parsed = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        # Fall back to the first 19 chars (no tz), still treated as UTC below.
        try:
            parsed = datetime.fromisoformat(iso_str[:19])
        except (ValueError, TypeError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone().replace(tzinfo=None)


def relative_date(iso_str):
    """Format date as 'Mar 3 (1w ago)' — absolute then relative.

    Per locked user decision: format MUST be 'Mar 3 (1w ago)'
    (absolute date then relative in parentheses).

    Storage is naive UTC (see ``arch-store-utc-render-local``); we convert
    to OS-local before formatting so jobs ingested earlier the same local
    day don't display as 'future'.
    """
    if not iso_str:
        return "---"
    dt = _parse_stored_ts_as_local(iso_str)
    if dt is None:
        return iso_str[:10] if iso_str else "---"

    # Absolute part: "Mar 3" — handle Windows (%#d) vs Unix (%-d)
    try:
        abs_part = dt.strftime("%b %-d")
    except ValueError:
        abs_part = dt.strftime("%b %#d")

    now = datetime.now()
    delta = now - dt
    days = delta.days

    if days < 0:
        rel = "future"
    elif days == 0:
        rel = "today"
    elif days == 1:
        rel = "1d ago"
    elif days < 7:
        rel = f"{days}d ago"
    elif days < 30:
        weeks = days // 7
        rel = f"{weeks}w ago"
    elif days < 365:
        months = days // 30
        rel = f"{months}mo ago"
    else:
        years = days // 365
        rel = f"{years}y ago"

    return f"{abs_part} ({rel})"


def local_date(iso_str):
    """Render a stored UTC ISO timestamp as a local YYYY-MM-DD string.

    Replaces template ``iso_str[:10]`` slices that incorrectly assumed local
    time. A job posted at 23:30 PT (=07:30 UTC next day) now renders as the
    user-local calendar date instead of the UTC date.
    """
    if not iso_str:
        return ""
    dt = _parse_stored_ts_as_local(iso_str)
    if dt is None:
        return iso_str[:10] if isinstance(iso_str, str) else ""
    return dt.strftime("%Y-%m-%d")


@jobs_bp.record_once
def _register_filters(state):
    """Register the relative_date / local_date Jinja2 filters when blueprint is registered."""
    state.app.jinja_env.filters["relative_date"] = relative_date
    state.app.jinja_env.filters["local_date"] = local_date


@jobs_bp.route("/", strict_slashes=False)
def index():
    """Job Board landing page -- full page render with filter bar."""
    conn = get_db()

    filters = _get_filter_kwargs()
    jobs = get_filtered_jobs(conn, **filters)
    locations = get_distinct_locations(conn)
    sources = get_distinct_sources(conn)
    countries = get_distinct_country_codes(conn)
    workplace_types = get_distinct_workplace_types(conn)
    stale_count = _get_stale_count(conn)
    archived_count = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE pipeline_status = 'archived'"
    ).fetchone()[0]
    hidden_count = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE pipeline_status IN ('archived', 'withdrawn', 'dismissed', 'rejected')"
    ).fetchone()[0]

    return render_template(
        "jobs/index.html",
        jobs=jobs,
        filters=request.args,
        pipeline_statuses=PIPELINE_STATUSES,
        locations=locations,
        sources=sources,
        countries=countries,
        workplace_types=workplace_types,
        stale_count=stale_count,
        archived_count=archived_count,
        hidden_count=hidden_count,
    )


@jobs_bp.route("/table", strict_slashes=False)
def table():
    """HTMX partial -- returns only the table body rows (no full page)."""
    if not request.headers.get("HX-Request"):
        return redirect(url_for("jobs.index"))
    conn = get_db()

    filters = _get_filter_kwargs()
    jobs = get_filtered_jobs(conn, **filters)

    return render_template(
        "jobs/_table.html",
        jobs=jobs,
        pipeline_statuses=PIPELINE_STATUSES,
    )


@jobs_bp.route("/archived-table", strict_slashes=False)
def archived_table():
    """HTMX partial -- archived job rows for the collapsible section."""
    if not request.headers.get("HX-Request"):
        return redirect(url_for("jobs.index"))
    conn = get_db()
    jobs = get_filtered_jobs(
        conn, status="archived", sort_by="first_seen", sort_dir="DESC", limit=200
    )
    return render_template(
        "jobs/_table.html",
        jobs=jobs,
        pipeline_statuses=PIPELINE_STATUSES,
    )


@jobs_bp.route("/add-from-listing", methods=["POST"], strict_slashes=False)
def add_from_listing():
    """HTMX POST — create a job from a listing URL + optional fields, then enrich.

    Intended for the dashboard modal: returns an HTML fragment (not a full
    page). Registered before ``/<path:dedup_key>/…`` routes.

    Non-HTMX callers are redirected to the dashboard with a flash message.
    """
    if not request.headers.get("HX-Request"):
        flash('Add a job from the Dashboard using the "Add Job Manually" dialog.', "info")
        return redirect(url_for("dashboard.index"))

    conn = get_db()
    config = current_app.config.get("JF_CONFIG", {}) or {}

    listing_url = (request.form.get("listing_url") or "").strip()
    if not listing_url:
        return (
            render_template(
                "jobs/_add_listing_manual_result.html",
                error="Listing URL is required.",
            ),
            200,
        )

    if not (listing_url.startswith("https://") or listing_url.startswith("http://")):
        return (
            render_template(
                "jobs/_add_listing_manual_result.html",
                error="URL must start with http:// or https://.",
            ),
            200,
        )

    user_title = (request.form.get("job_title") or "").strip()
    user_company = (request.form.get("company") or "").strip()
    location = (request.form.get("location") or "").strip()
    description_raw = (request.form.get("description") or "").strip()
    description = description_raw or None

    if user_title and user_company:
        title, company = user_title, user_company
    else:
        inferred_title, inferred_company = infer_title_company_from_listing_url(listing_url)
        title = user_title or inferred_title
        company = user_company or inferred_company

    salary_min: int | None = None
    salary_max: int | None = None
    for field_name, raw in (
        ("salary_min", request.form.get("salary_min", "").strip()),
        ("salary_max", request.form.get("salary_max", "").strip()),
    ):
        if not raw:
            continue
        try:
            val = int(raw)
        except ValueError:
            return (
                render_template(
                    "jobs/_add_listing_manual_result.html",
                    error=f"Invalid {field_name.replace('_', ' ')}: use a whole number.",
                ),
                200,
            )
        if val < 0:
            return (
                render_template(
                    "jobs/_add_listing_manual_result.html",
                    error="Salary values cannot be negative.",
                ),
                200,
            )
        if field_name == "salary_min":
            salary_min = val
        else:
            salary_max = val

    if salary_min is not None and salary_max is not None and salary_min > salary_max:
        return (
            render_template(
                "jobs/_add_listing_manual_result.html",
                error="Minimum salary cannot be greater than maximum salary.",
            ),
            200,
        )

    try:
        job = Job(
            title=title,
            company=company,
            location=location,
            source="manual",
            source_url=listing_url,
            source_id="",
            description=description,
            salary_min=salary_min,
            salary_max=salary_max,
        )
    except ValueError as e:
        return (
            render_template(
                "jobs/_add_listing_manual_result.html",
                error=str(e),
            ),
            200,
        )

    try:
        upsert_result = upsert_job(conn, job)
    except Exception as e:
        logger.error("add_from_listing: upsert failed: %s", e, exc_info=True)
        return (
            render_template(
                "jobs/_add_listing_manual_result.html",
                error="Could not save the job. Check logs for details.",
            ),
            200,
        )

    if upsert_result.unresolved_reasons:
        logger.info(
            "add_from_listing: job %s saved with unresolved reasons: %s",
            job.dedup_key,
            upsert_result.unresolved_reasons,
        )

    dedup_key = job.dedup_key
    row = get_job(conn, dedup_key)
    if row is None:
        return (
            render_template(
                "jobs/_add_listing_manual_result.html",
                error="Job was not found after save (unexpected).",
            ),
            200,
        )

    job_row = dict(row)
    serpapi_key = get_secret("sources.serpapi.api_key", config=config)

    enriched: dict = {}
    enrich_error = None
    try:
        enriched = (
            enrich_job(
                job_row,
                serpapi_key=serpapi_key,
                conn=conn,
                config=config,
            )
            or {}
        )
    except Exception as e:
        enrich_error = str(e)
        logger.warning("add_from_listing: enrich_job failed for %s: %s", dedup_key, e)

    row = get_job(conn, dedup_key)
    if row is None:
        return (
            render_template(
                "jobs/_add_listing_manual_result.html",
                error="Job row disappeared after enrichment.",
            ),
            200,
        )
    job_row = dict(row)

    jd_len = len((job_row.get("jd_full") or "").strip())
    if enriched:
        enrich_summary = (
            "Enrichment ran: filled gaps from the listing URL and cheaper tiers where available."
        )
    elif jd_len >= 200:
        enrich_summary = (
            "A full job description is already available from your notes or the listing page."
        )
    else:
        enrich_summary = (
            "Job saved. The listing page did not yield a long description yet — "
            "open the job on the board to paste a JD, or wait for a later "
            "enrichment pass."
        )
    if enrich_error:
        enrich_summary += f" (enrichment error: {enrich_error})"

    score_note = None
    try:
        from job_finder.web.claude_client import cost_gate
        from job_finder.web.scoring_orchestrator import score_and_persist_job

        if job_row.get("jd_full") and cost_gate(conn, config, "scoring"):
            score_and_persist_job(job_row, conn, config)
            score_note = "Scoring was attempted when a JD was available."
        elif job_row.get("jd_full"):
            score_note = "Budget cap reached — scoring skipped; JD is saved."
    except ImportError as e:
        logger.warning("add_from_listing: scorer not available: %s", e)
        score_note = "Scoring unavailable in this environment."
    except Exception as e:
        logger.error("add_from_listing: scoring failed for %s: %s", dedup_key, e)
        score_note = "Scoring failed; you can re-score from the job card."

    return render_template(
        "jobs/_add_listing_manual_result.html",
        dedup_key=dedup_key,
        enrich_summary=enrich_summary,
        score_note=score_note,
    )


@jobs_bp.route("/<path:dedup_key>/expand", strict_slashes=False)
def expand(dedup_key: str):
    """HTMX partial -- returns accordion expansion row for a job."""
    if not request.headers.get("HX-Request"):
        return redirect(url_for("jobs.index"))
    conn = get_db()

    ctx = load_job_context(conn, dedup_key)
    if ctx is None:
        return "", 404

    job = ctx["job"]

    try:
        log_activity(
            current_app.config["DB_PATH"],
            ACTION_EXPAND_JOB,
            entity_id=dedup_key,
            metadata={
                "title": job.get("title"),
                "company": job.get("company"),
                "status": "success",
            },
        )
    except Exception:
        logger.debug("log_activity failed in expand", exc_info=True)

    return render_template(
        "jobs/_row_expanded.html",
        job=job,
        pipeline_statuses=PIPELINE_STATUSES,
    )


@jobs_bp.route("/<path:dedup_key>/collapse", strict_slashes=False)
def collapse(dedup_key: str):
    """HTMX partial -- returns hidden placeholder <tr> to restore pre-expansion DOM state.

    Emits HX-Trigger-After-Settle with the dedup_key so the index page's
    global listener can smooth-scroll back to the compact row regardless
    of which collapse path the user took (compact-row click or bottom
    Collapse button). The previous inline hx-on::after-request approach
    relied on `this.closest('tr').previousElementSibling` which is
    fragile once the expanded row is swapped out — moving the scroll to
    an HX-Trigger-After-Settle event from the response side makes the
    behavior bulletproof.
    """
    if not request.headers.get("HX-Request"):
        return redirect(url_for("jobs.index"))
    conn = get_db()
    job = get_job(conn, dedup_key)
    if job is None:
        return "", 404

    import json as _json

    response = make_response(
        render_template(
            "jobs/_row_collapse_response.html",
            job=job,
        )
    )
    response.headers["HX-Trigger-After-Settle"] = _json.dumps(
        {"job-collapsed": {"dedup_key": dedup_key}}
    )
    return response


@jobs_bp.route("/<path:dedup_key>/status", methods=["POST"], strict_slashes=False)
def update_status(dedup_key: str):
    """HTMX POST -- change pipeline status and return updated status cell."""
    conn = get_db()

    new_status = request.form.get("pipeline_status", "")
    if new_status not in PIPELINE_STATUSES:
        return "Invalid status", 400

    # Capture old status before update for activity metadata
    old_job = get_job(conn, dedup_key)
    old_status = old_job.get("pipeline_status") if old_job else None

    update_pipeline_status(conn, dedup_key, new_status, source="manual")

    job = get_job(conn, dedup_key)
    if job is None:
        return "", 404

    try:
        log_activity(
            current_app.config["DB_PATH"],
            ACTION_STATUS_CHANGE,
            entity_id=dedup_key,
            metadata={
                "old_status": old_status,
                "new_status": new_status,
                "title": (old_job.get("title") if old_job else None),
                "company": (old_job.get("company") if old_job else None),
            },
        )
    except Exception:
        logger.debug("log_activity failed in update_status", exc_info=True)

    status_html = render_template(
        "jobs/_status_cell.html",
        job=job,
        pipeline_statuses=PIPELINE_STATUSES,
    )

    if new_status == "archived":
        # OOB update: refresh the archived count badge.
        # Do NOT set HX-Trigger: jobs-updated — it causes tbody refetch
        # that kills the in-flight archive fadeout animation.
        archived_count = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE pipeline_status = 'archived'"
        ).fetchone()[0]
        oob_counter = f'<span id="archived-count" hx-swap-oob="innerHTML">{archived_count}</span>'
        resp = make_response(status_html + oob_counter)
    elif new_status in ("dismissed", "withdrawn", "rejected"):
        # Trigger table re-fetch so the row disappears from the filtered view.
        # Unlike archived (which has a fadeout animation), these statuses
        # simply remove the row immediately.
        resp = make_response(status_html)
        resp.headers["HX-Trigger-After-Settle"] = "jobs-updated"
    else:
        resp = make_response(status_html)

    return resp


@jobs_bp.route("/<path:dedup_key>/detail-inline", strict_slashes=False)
def detail_inline(dedup_key: str):
    """HTMX partial -- returns full detail as inline table row."""
    if not request.headers.get("HX-Request"):
        return redirect(url_for("jobs.index"))
    conn = get_db()
    job = get_job(conn, dedup_key)
    if job is None:
        return "", 404
    events = get_pipeline_events(conn, dedup_key)
    return render_template(
        "jobs/_row_detail.html",
        job=job,
        events=events,
        pipeline_statuses=PIPELINE_STATUSES,
    )


@jobs_bp.route("/<path:dedup_key>/paste-jd", methods=["POST"], strict_slashes=False)
def paste_jd(dedup_key: str):
    """HTMX POST -- accept pasted JD text, store it, trigger score-tier eval.

    Stores jd_text in jobs.jd_full, then routes through the v3.0 unified
    scorer. Budget-gated via cost_gate. Returns updated expanded row partial.
    """
    conn = get_db()

    ctx = load_job_context(conn, dedup_key)
    if ctx is None:
        return "", 404

    job = ctx["job"]

    jd_text = request.form.get("jd_text", "").strip()
    if not jd_text:
        return render_template(
            "jobs/_row_expanded.html",
            job=job,
            pipeline_statuses=PIPELINE_STATUSES,
            error="Please provide a job description.",
        )

    # Cap at 8000 chars — same limit applied by upsert_job during ingestion.
    jd_text = jd_text[:8000]

    # Store the JD text
    conn.execute(
        "UPDATE jobs SET jd_full = ? WHERE dedup_key = ?",
        (jd_text, dedup_key),
    )
    conn.commit()

    try:
        log_activity(
            current_app.config["DB_PATH"],
            ACTION_PASTE_JD,
            entity_id=dedup_key,
            metadata={
                "title": job.get("title"),
                "company": job.get("company"),
                "jd_length": len(jd_text),
                "status": "success",
            },
        )
    except Exception:
        logger.debug("log_activity failed in paste_jd", exc_info=True)

    # Attempt v3 unified scoring (budget-gated)
    error = None
    try:
        from job_finder.web.claude_client import cost_gate
        from job_finder.web.scoring_orchestrator import score_and_persist_job

        config = current_app.config.get("JF_CONFIG", {})
        if cost_gate(conn, config, "scoring"):
            # Refresh job row with jd_full
            refreshed = get_job(conn, dedup_key)
            if refreshed is not None:
                score_and_persist_job(refreshed, conn, config)
            else:
                logger.warning("paste-jd: row vanished mid-request for %s", dedup_key)
                error = "Job no longer exists. Scoring skipped."
        else:
            logger.info("paste-jd: budget cap reached, scoring skipped for %s", dedup_key)
            error = "Budget cap reached. Scoring skipped."

    except ImportError as e:
        logger.warning("paste-jd: scorer not available: %s", e)
        error = "Scoring unavailable. JD saved for later."
    except Exception as e:
        logger.error("paste-jd: scoring failed for %s: %s", dedup_key, e)
        error = "Re-scoring failed. Try again later."

    # Return updated expanded row + OOB score cell (updates compact row in-place)
    ctx = load_job_context(conn, dedup_key)
    if ctx is None:
        return "", 404
    expanded = render_template(
        "jobs/_row_expanded.html",
        job=ctx["job"],
        pipeline_statuses=PIPELINE_STATUSES,
        error=error,
    )
    oob_score = render_template("jobs/_score_cell.html", job=ctx["job"], oob=True)
    return make_response(expanded + "<template>" + oob_score + "</template>")


@jobs_bp.route("/<path:dedup_key>/rescore", methods=["POST"], strict_slashes=False)
def rescore(dedup_key: str):
    """HTMX POST -- re-trigger score-tier evaluation for a job that already has jd_full.

    Returns updated expanded row partial.
    """
    db_path = current_app.config["DB_PATH"]
    conn = get_db(db_path)

    ctx = load_job_context(conn, dedup_key)
    if ctx is None:
        return "", 404

    job = ctx["job"]

    if not job.get("jd_full"):
        return render_template(
            "jobs/_row_expanded.html",
            job=job,
            pipeline_statuses=PIPELINE_STATUSES,
            error="No JD available for re-scoring. Paste a JD first.",
        )

    # Capture old classification before re-evaluation
    old_classification = job.get("classification")

    # Attempt v3 re-evaluation (budget-gated)
    error = None
    t0 = _time.time()
    try:
        from job_finder.web.claude_client import cost_gate
        from job_finder.web.scoring_orchestrator import score_and_persist_job

        config = current_app.config.get("JF_CONFIG", {})
        if cost_gate(conn, config, "scoring"):
            result = score_and_persist_job(job, conn, config)
            if result and getattr(result, "status", None) == "ok":
                # Re-query to get the persisted classification (derived
                # at persist time from sub_scores + legitimacy_note).
                refreshed = get_job(conn, dedup_key)
                new_classification = (refreshed or {}).get("classification")
                try:
                    log_activity(
                        db_path,
                        ACTION_RESCORE,
                        entity_id=dedup_key,
                        metadata={
                            "old_classification": old_classification,
                            "new_classification": new_classification,
                            "duration_seconds": round(_time.time() - t0, 2),
                            "status": "success",
                        },
                    )
                except Exception:
                    pass
        else:
            logger.info("rescore: budget cap reached, scoring skipped for %s", dedup_key)
            error = "Budget cap reached. Scoring skipped."

    except ImportError as e:
        logger.warning("rescore: scorer not available: %s", e)
        error = "Re-scoring failed. Try again later."
        try:
            log_activity(
                db_path,
                ACTION_RESCORE,
                entity_id=dedup_key,
                metadata={
                    "status": "failed",
                    "error": "ImportError",
                    "duration_seconds": round(_time.time() - t0, 2),
                },
            )
        except Exception:
            pass
    except Exception as e:
        logger.error("rescore: scoring failed for %s: %s", dedup_key, e)
        error = "Re-scoring failed. Try again later."
        try:
            log_activity(
                db_path,
                ACTION_RESCORE,
                entity_id=dedup_key,
                metadata={
                    "status": "failed",
                    "error": type(e).__name__,
                    "duration_seconds": round(_time.time() - t0, 2),
                },
            )
        except Exception:
            pass

    ctx = load_job_context(conn, dedup_key)
    if ctx is None:
        return "", 404
    expanded = render_template(
        "jobs/_row_expanded.html",
        job=ctx["job"],
        pipeline_statuses=PIPELINE_STATUSES,
        error=error,
    )
    oob_score = render_template("jobs/_score_cell.html", job=ctx["job"], oob=True)
    return make_response(expanded + "<template>" + oob_score + "</template>")


@jobs_bp.route("/<path:dedup_key>/score-cell", strict_slashes=False)
def score_cell(dedup_key: str):
    """HTMX partial -- returns just the score <td> for a single job."""
    conn = get_db()
    job = get_job(conn, dedup_key)
    if job is None:
        return "", 404
    return render_template("jobs/_score_cell.html", job=job)


@jobs_bp.route("/<path:dedup_key>/save-jd", methods=["POST"], strict_slashes=False)
def save_jd(dedup_key: str):
    """HTMX POST -- save jd_full without triggering scoring."""
    db_path = current_app.config["DB_PATH"]
    conn = get_db(db_path)

    ctx = load_job_context(conn, dedup_key)
    if ctx is None:
        return "", 404

    job = ctx["job"]
    jd_text = request.form.get("jd_text", "").strip()
    if not jd_text:
        return render_template(
            "jobs/_row_expanded.html",
            job=job,
            pipeline_statuses=PIPELINE_STATUSES,
            error="Please provide a job description.",
        )

    # Cap at 8000 chars — same limit applied by upsert_job during ingestion.
    jd_text = jd_text[:8000]

    conn.execute(
        "UPDATE jobs SET jd_full = ? WHERE dedup_key = ?",
        (jd_text, dedup_key),
    )
    conn.commit()

    try:
        log_activity(
            db_path,
            ACTION_SAVE_JD,
            entity_id=dedup_key,
            metadata={
                "title": job.get("title"),
                "company": job.get("company"),
                "jd_length": len(jd_text),
                "status": "success",
            },
        )
    except Exception:
        logger.debug("log_activity failed in save_jd", exc_info=True)

    ctx = load_job_context(conn, dedup_key)
    if ctx is None:
        return "", 404
    return render_template(
        "jobs/_row_expanded.html",
        job=ctx["job"],
        pipeline_statuses=PIPELINE_STATUSES,
        jd_saved=True,
    )


@jobs_bp.route("/<path:dedup_key>/jd-edit-form", strict_slashes=False)
def jd_edit_form(dedup_key: str):
    """HTMX GET -- return the JD paste form pre-filled with existing jd_full."""
    conn = get_db()
    job = get_job(conn, dedup_key)
    if job is None:
        return "", 404
    return render_template("jobs/_jd_edit_form.html", job=job)


@jobs_bp.route("/<path:dedup_key>", strict_slashes=False)
def detail(dedup_key: str):
    """Full job detail page at /jobs/<dedup_key>."""
    conn = get_db()

    job = get_job(conn, dedup_key)
    if job is None:
        return render_template("jobs/detail.html", job=None), 404

    events = get_pipeline_events(conn, dedup_key)

    return render_template(
        "jobs/detail.html",
        job=job,
        events=events,
        pipeline_statuses=PIPELINE_STATUSES,
    )
