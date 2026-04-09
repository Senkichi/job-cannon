"""Companies blueprint — Company registry management routes.

Routes:
    GET  /companies                   -- Companies list with search and ATS filter
    GET  /companies/<id>/expand       -- HTMX: expand company row with jobs + scan history
    GET  /companies/<id>/collapse     -- HTMX: collapse company row back to compact
    POST /companies/add               -- Create new company record
    POST /companies/<id>/toggle       -- Toggle scan_enabled between 0 and 1
    POST /companies/<id>/update-slug  -- Update ATS platform/slug manually
    POST /companies/scan              -- Trigger immediate ATS scan (synchronous)
"""

import logging

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from job_finder.web.ats_prober import probe_single_company
from job_finder.web.ats_scanner import probe_ats_slugs, run_ats_scan
from job_finder.web.db_helpers import get_db

logger = logging.getLogger(__name__)

companies_bp = Blueprint("companies", __name__, url_prefix="/companies")

# Validated allowlist for sort_by (no parameterized column names in SQLite)
_SORT_ALLOWLIST = {"name", "ats_platform", "last_scanned_at", "jobs_found_total"}
_ATS_PLATFORM_FILTER_VALUES = {"lever", "greenhouse", "ashby", "none", ""}


def _companies_health(conn) -> dict:
    """Compute companies pipeline health metrics for dashboard card.

    Uses a single CTE query to avoid 6 serial round-trips to SQLite.
    """
    from datetime import datetime as _dt

    row = conn.execute(
        """WITH
             c_totals AS (
               SELECT
                 COUNT(*)                                                     AS total,
                 COUNT(*) FILTER (WHERE ats_probe_status = 'pending')         AS pending,
                 COUNT(*) FILTER (WHERE homepage_url IS NOT NULL)             AS with_homepage,
                 COUNT(*) FILTER (WHERE company_size IS NOT NULL
                                     OR industry IS NOT NULL)                 AS with_enrichment
               FROM companies
             ),
             j_unlinked AS (
               SELECT COUNT(*) AS cnt FROM jobs WHERE company_id IS NULL
             ),
             last_scan AS (
               SELECT MAX(scanned_at) AS ts FROM company_scan_log
             )
           SELECT
             c_totals.total,
             c_totals.pending,
             c_totals.with_homepage,
             c_totals.with_enrichment,
             j_unlinked.cnt      AS unlinked_jobs,
             last_scan.ts        AS last_scan_at
           FROM c_totals, j_unlinked, last_scan"""
    ).fetchone()

    total = row["total"] or 0
    last_scan_at = row["last_scan_at"]

    last_scan_age_days = None
    if last_scan_at:
        try:
            scan_dt = _dt.fromisoformat(last_scan_at)
            last_scan_age_days = (_dt.now() - scan_dt).days
        except (ValueError, TypeError):
            pass

    return {
        "total": total,
        "pending_probe": row["pending"],
        "homepage_pct": round(row["with_homepage"] / max(total, 1) * 100, 1),
        "enrichment_pct": round(row["with_enrichment"] / max(total, 1) * 100, 1),
        "unlinked_jobs": row["unlinked_jobs"],
        "last_scan_at": last_scan_at,
        "last_scan_age_days": last_scan_age_days,
    }


@companies_bp.route("/", strict_slashes=False)
def index():
    """Companies list page with sortable table, search, and ATS filter."""
    db_path = current_app.config["DB_PATH"]
    conn = get_db(db_path)

    search = request.args.get("search", "").strip()
    page = request.args.get("page", 1, type=int)
    if page < 1:
        page = 1
    per_page = 50
    offset = (page - 1) * per_page
    ats_platform = request.args.get("ats_platform", "").strip().lower()
    sort_by = request.args.get("sort_by", "name")

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

    total_count = conn.execute(
        f"SELECT COUNT(*) FROM companies c {where_sql}",
        params,
    ).fetchone()[0]

    # Fetch per_page+1 rows to detect whether a next page exists (avoids false
    # positive when total is exactly divisible by per_page).
    companies = conn.execute(
        f"""SELECT c.*,
               COUNT(j.dedup_key) as job_count_live
            FROM companies c
            LEFT JOIN jobs j ON j.company_id = c.id
            {where_sql}
            GROUP BY c.id
            ORDER BY c.{sort_by} ASC NULLS LAST
            LIMIT ? OFFSET ?""",
        params + [per_page + 1, offset],
    ).fetchall()

    has_more = len(companies) > per_page
    if has_more:
        companies = companies[:per_page]

    is_htmx = request.headers.get("HX-Request")
    if is_htmx:
        # page > 1 means the sentinel fired — return rows-only partial so the
        # response doesn't nest an outer container + duplicate header inside the
        # existing page-1 container.  Sentinel uses hx-swap="outerHTML" so it
        # replaces itself with the new rows + next-page sentinel.
        template = (
            "companies/_rows_partial.html" if page > 1 else "companies/_table.html"
        )
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

    return render_template(
        "companies/index.html",
        companies=companies,
        search=search,
        ats_platform=ats_platform,
        sort_by=sort_by,
        page=page,
        has_more=has_more,
        total_count=total_count,
        health=_companies_health(conn),
    )


@companies_bp.route("/<int:company_id>/expand", strict_slashes=False)
def expand(company_id):
    """HTMX: expand company row with jobs and scan history."""
    db_path = current_app.config["DB_PATH"]
    conn = get_db(db_path)

    company = conn.execute(
        "SELECT * FROM companies WHERE id = ?", (company_id,)
    ).fetchone()

    if company is None:
        return "Company not found", 404

    # Jobs for this company (limit 20, ordered by score DESC)
    jobs = conn.execute(
        """SELECT *, COALESCE(sonnet_score, haiku_score, score) as effective_score
           FROM jobs
           WHERE company_id = ?
           ORDER BY COALESCE(sonnet_score, haiku_score, score) DESC
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

    return render_template(
        "companies/_row_expanded.html",
        company=company,
        jobs=jobs,
        scan_history=scan_history,
    )


@companies_bp.route("/<int:company_id>/collapse", strict_slashes=False)
def collapse(company_id):
    """HTMX: collapse company row back to compact view."""
    db_path = current_app.config["DB_PATH"]
    conn = get_db(db_path)

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
    db_path = current_app.config["DB_PATH"]
    conn = get_db(db_path)

    company_name = request.form.get("company_name", "").strip()
    homepage_url = request.form.get("homepage_url", "").strip() or None

    if not company_name:
        flash("Company name is required.", "error")
        return redirect(url_for("companies.index"))

    try:
        from job_finder.web.ats_company import find_or_create_company
        company_id = find_or_create_company(
            conn,
            name=company_name,
            homepage_url=homepage_url,
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
    db_path = current_app.config["DB_PATH"]
    conn = get_db(db_path)

    company = conn.execute(
        "SELECT * FROM companies WHERE id = ?", (company_id,)
    ).fetchone()

    if company is None:
        return "Company not found", 404

    new_enabled = 0 if company["scan_enabled"] else 1
    from datetime import datetime
    now = datetime.now().isoformat()
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
    db_path = current_app.config["DB_PATH"]
    conn = get_db(db_path)

    company = conn.execute(
        "SELECT * FROM companies WHERE id = ?", (company_id,)
    ).fetchone()

    if company is None:
        return "Company not found", 404

    ats_platform = request.form.get("ats_platform", "").strip() or None
    ats_slug = request.form.get("ats_slug", "").strip() or None

    from datetime import datetime
    now = datetime.now().isoformat()
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

    # Reload company data
    updated_company = conn.execute(
        "SELECT * FROM companies WHERE id = ?", (company_id,)
    ).fetchone()
    jobs = conn.execute(
        """SELECT *, COALESCE(sonnet_score, haiku_score, score) as effective_score
           FROM jobs WHERE company_id = ? ORDER BY COALESCE(sonnet_score, haiku_score, score) DESC LIMIT 20""",
        (company_id,),
    ).fetchall()
    scan_history = conn.execute(
        "SELECT * FROM company_scan_log WHERE company_id = ? ORDER BY scanned_at DESC LIMIT 5",
        (company_id,),
    ).fetchall()

    return render_template(
        "companies/_row_expanded.html",
        company=updated_company,
        jobs=jobs,
        scan_history=scan_history,
    )


@companies_bp.route("/scan", methods=["POST"], strict_slashes=False)
def scan():
    """Trigger immediate ATS scan synchronously. Returns _scan_result.html fragment.

    Two-layer exception handling:
    - Inner try: scan logic errors (probe + run_ats_scan). Caught and shown as scan failure.
    - render_template is OUTSIDE the try block: template errors propagate as 500 with traceback.
    """
    db_path = current_app.config["DB_PATH"]
    config = current_app.config.get("JF_CONFIG", {})

    scan_error = None
    result = None
    try:
        probe_result = probe_ats_slugs(db_path, config)
        logger.info("ATS probe before scan: %s", probe_result)
        result = run_ats_scan(db_path, config)
        result["probe"] = probe_result
    except Exception as e:
        logger.error("ATS scan failed: %s", e)
        scan_error = str(e)

    # render_template is OUTSIDE the try block — TemplateErrors propagate as 500
    return render_template("companies/_scan_result.html", result=result, error=scan_error)


@companies_bp.route("/<int:company_id>/retry", methods=["POST"], strict_slashes=False)
def retry(company_id):
    """Immediately re-probe a company in error or unreachable state.

    Only valid for companies with ats_probe_status='error' or
    ats_probe_status='miss' AND miss_reason='unreachable'. Returns 400 for
    all other statuses (hit, pending, regular miss).

    Returns updated _row.html fragment (innerHTML swap into #company-{id}).
    """
    db_path = current_app.config["DB_PATH"]
    config = current_app.config.get("JF_CONFIG", {})
    conn = get_db(db_path)

    company = conn.execute(
        "SELECT * FROM companies WHERE id = ?", (company_id,)
    ).fetchone()

    if company is None:
        return "Company not found", 404

    status = company["ats_probe_status"]
    miss_reason = company["miss_reason"] if "miss_reason" in company.keys() else None

    # Only allow retry for error or unreachable companies
    is_retryable = (
        status == "error"
        or (status == "miss" and miss_reason == "unreachable")
    )
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
# Company research routes
# ---------------------------------------------------------------------------


@companies_bp.route("/<int:company_id>/research", methods=["POST"], strict_slashes=False)
def start_research(company_id):
    """Start on-demand company research. Returns generating fragment for polling."""
    from job_finder.web.company_research import get_cached_company_research, start_company_research

    db_path = current_app.config["DB_PATH"]
    config = current_app.config.get("JF_CONFIG", {})
    conn = get_db(db_path)

    company = conn.execute(
        "SELECT * FROM companies WHERE id = ?", (company_id,)
    ).fetchone()
    if company is None:
        return "Company not found", 404

    # Check for cached or in-progress research
    cached = get_cached_company_research(conn, company_id)
    if cached and cached["status"] == "done":
        return render_template(
            "companies/_research_section.html",
            research=cached, company=company,
        )
    if cached and cached["status"] in ("pending", "generating"):
        return render_template(
            "companies/_research_generating.html",
            research_id=cached["id"], company_id=company_id,
        )

    research_id = start_company_research(conn, company_id, db_path, config)
    return render_template(
        "companies/_research_generating.html",
        research_id=research_id, company_id=company_id,
    )


@companies_bp.route(
    "/<int:company_id>/research/status/<int:research_id>",
    strict_slashes=False,
)
def research_status(company_id, research_id):
    """Poll research status. Returns generating fragment or final section."""
    from datetime import datetime as _dt, timezone as _tz

    db_path = current_app.config["DB_PATH"]
    conn = get_db(db_path)

    company = conn.execute(
        "SELECT * FROM companies WHERE id = ?", (company_id,)
    ).fetchone()
    if company is None:
        return "Company not found", 404

    row = conn.execute(
        "SELECT * FROM company_research WHERE id = ? AND company_id = ?",
        (research_id, company_id),
    ).fetchone()
    if row is None:
        return "Research not found", 404

    research = dict(row)

    # Timeout safety net: mark stale generating rows as error
    if research["status"] in ("pending", "generating") and research.get("requested_at"):
        try:
            requested = _dt.fromisoformat(research["requested_at"])
            age_minutes = (
                _dt.now(_tz.utc) - requested.replace(tzinfo=_tz.utc)
            ).total_seconds() / 60
            if age_minutes > 10:
                now = _dt.now(_tz.utc).isoformat()
                conn.execute(
                    "UPDATE company_research SET status = 'error', "
                    "error_msg = 'Generation timed out', completed_at = ? WHERE id = ?",
                    (now, research_id),
                )
                conn.commit()
                research["status"] = "error"
                research["error_msg"] = "Generation timed out"
        except (ValueError, TypeError):
            pass

    if research["status"] in ("done", "error"):
        return render_template(
            "companies/_research_section.html",
            research=research, company=company,
        )

    # Still generating — return polling fragment
    return render_template(
        "companies/_research_generating.html",
        research_id=research_id, company_id=company_id,
    )
