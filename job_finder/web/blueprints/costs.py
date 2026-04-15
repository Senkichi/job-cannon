"""Costs blueprint — Cost Monitor page.

Routes:
    GET /costs   -- Cost Monitor page with 30-day chart, budget bar, stat cards, feature table
"""

from flask import (
    Blueprint,
    current_app,
    render_template,
)

from job_finder.config import DEFAULT_DAILY_BUDGET_USD
from job_finder.web.claude_client import (
    get_cost_stats,
    get_daily_cost_breakdown,
    get_monthly_feature_breakdown,
    get_monthly_provider_breakdown,
)
from job_finder.web.db_helpers import get_db

costs_bp = Blueprint("costs", __name__, url_prefix="/costs")

@costs_bp.route("/", strict_slashes=False)
def index():
    """Cost Monitor page — 30-day chart, budget bar, stat cards, feature table."""
    conn = get_db(current_app.config["DB_PATH"])

    # Read daily_budget_usd from config — matches what cost_gate() enforces
    budget_cap = (
        current_app.config.get("JF_CONFIG", {})
        .get("scoring", {})
        .get("daily_budget_usd", DEFAULT_DAILY_BUDGET_USD)
    )

    cost_stats = get_cost_stats(conn, budget_cap=budget_cap)
    daily_breakdown = get_daily_cost_breakdown(conn)
    monthly_breakdown = get_monthly_feature_breakdown(conn)
    monthly_provider_breakdown = get_monthly_provider_breakdown(conn)

    return render_template(
        "costs/index.html",
        cost_stats=cost_stats,
        daily_breakdown=daily_breakdown,
        monthly_breakdown=monthly_breakdown,
        monthly_provider_breakdown=monthly_provider_breakdown,
        budget_cap=budget_cap,
    )
