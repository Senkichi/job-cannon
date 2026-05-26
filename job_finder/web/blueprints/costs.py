"""Costs blueprint — API Activity page (toggle between Usage and Cost views).

Routes:
    GET /costs            -- defaults to ?view=usage (tokens in/out, all providers).
    GET /costs?view=cost  -- monthly $$$ rollup; FREE_PROVIDERS excluded so
                              subscription-funded ($0) rows don't pad the table.
"""

from flask import (
    Blueprint,
    current_app,
    render_template,
    request,
)

from job_finder.config import DEFAULT_DAILY_BUDGET_USD
from job_finder.web.claude_client import (
    get_cost_stats,
    get_daily_cost_breakdown,
    get_daily_usage_breakdown,
    get_monthly_feature_breakdown,
    get_monthly_feature_usage,
    get_monthly_provider_breakdown,
    get_monthly_provider_usage,
    get_usage_stats,
)
from job_finder.web.db_helpers import get_db

costs_bp = Blueprint("costs", __name__, url_prefix="/costs")

_VALID_VIEWS = ("usage", "cost")


@costs_bp.route("/", strict_slashes=False)
def index():
    """API Activity page — Usage (default) or Cost via ?view=cost."""
    conn = get_db(current_app.config["DB_PATH"])

    view = request.args.get("view", "usage")
    if view not in _VALID_VIEWS:
        view = "usage"

    if view == "usage":
        return render_template(
            "costs/index.html",
            view=view,
            usage_stats=get_usage_stats(conn),
            daily_usage=get_daily_usage_breakdown(conn),
            monthly_feature_usage=get_monthly_feature_usage(conn),
            monthly_provider_usage=get_monthly_provider_usage(conn),
        )

    # Cost view — budget cap drives the progress bar.
    budget_cap = (
        current_app.config.get("JF_CONFIG", {})
        .get("scoring", {})
        .get("daily_budget_usd", DEFAULT_DAILY_BUDGET_USD)
    )
    return render_template(
        "costs/index.html",
        view=view,
        budget_cap=budget_cap,
        cost_stats=get_cost_stats(conn, budget_cap=budget_cap),
        daily_breakdown=get_daily_cost_breakdown(conn),
        monthly_breakdown=get_monthly_feature_breakdown(conn),
        monthly_provider_breakdown=get_monthly_provider_breakdown(conn),
    )
