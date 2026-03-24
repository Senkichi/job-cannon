"""Anthropic Claude API client wrapper with cost recording and budget gating.

Provides:
- compute_cost: Calculate USD cost from token counts and model pricing.
- record_cost: Insert a cost row into scoring_costs and return cost_usd.
- cost_gate: Check whether a model tier is allowed given the monthly budget.
- get_cost_stats: Aggregate cost data by time period and feature/purpose.
- call_claude: Convenience wrapper for API calls with automatic cost recording.
- ClaudeContext: Dataclass bundling the (client, conn, config) triple for call_claude.
- BudgetExceededError: Raised by call_claude when the budget cap is exceeded.

Model pricing (per million tokens):
  claude-haiku-4-5:  $1.00 input / $5.00 output
  claude-sonnet-4-6: $3.00 input / $15.00 output
"""

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from job_finder.config import DEFAULT_MONTHLY_BUDGET_USD
from job_finder.json_utils import utc_now_iso

logger = logging.getLogger(__name__)

DEFAULT_API_TIMEOUT_SECONDS: int = 120

# ---------------------------------------------------------------------------
# Pricing table — price per million tokens (USD)
# ---------------------------------------------------------------------------

MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-opus-4-6": {"input": 5.0, "output": 25.0},
}


class BudgetExceededError(Exception):
    """Raised when a non-Haiku Claude call is blocked by the monthly budget cap."""


@dataclass(frozen=True, slots=True)
class ClaudeContext:
    """Invariant triple threaded through every call_claude invocation.

    Bundles the Anthropic client, database connection, and app config that
    every caller assembles identically.  Passing a single ClaudeContext
    instead of three separate parameters reduces call_claude's argument
    count and eliminates repeated parameter threading.
    """

    client: Any
    conn: sqlite3.Connection
    config: dict


# ---------------------------------------------------------------------------
# Cost computation
# ---------------------------------------------------------------------------

def compute_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return cost in USD for a Claude API call.

    Args:
        model: Model identifier, e.g. "claude-haiku-4-5".
        input_tokens: Number of input tokens consumed.
        output_tokens: Number of output tokens generated.

    Returns:
        Cost in USD as a float.  Uses the most expensive known model pricing
        as a conservative fallback for unrecognised model identifiers.
    """
    pricing = MODEL_PRICING.get(model)
    if pricing is None:
        logger.warning(
            "Unknown model '%s' in compute_cost — using highest known pricing as fallback",
            model,
        )
        pricing = max(
            MODEL_PRICING.values(), key=lambda p: p["input"] + p["output"]
        )
    return (input_tokens / 1_000_000) * pricing["input"] + \
           (output_tokens / 1_000_000) * pricing["output"]


# ---------------------------------------------------------------------------
# Cost recording
# ---------------------------------------------------------------------------

def record_cost(
    conn: sqlite3.Connection,
    job_id: str | None,
    purpose: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """Insert a cost row into scoring_costs and return cost_usd.

    Args:
        conn: Open SQLite connection.
        job_id: Job dedup_key this call is associated with (nullable).
        purpose: Feature attribution label, e.g. "haiku_score", "sonnet_eval".
        model: Model identifier used for the call.
        input_tokens: Number of input tokens consumed.
        output_tokens: Number of output tokens generated.

    Returns:
        Computed cost in USD.
    """
    cost_usd = compute_cost(model, input_tokens, output_tokens)
    timestamp = utc_now_iso()
    conn.execute(
        "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp),
    )
    conn.commit()
    return cost_usd


# ---------------------------------------------------------------------------
# Budget gating
# ---------------------------------------------------------------------------

def cost_gate(
    conn: sqlite3.Connection,
    config: dict,
    model_tier: str = "sonnet",
) -> bool:
    """Check whether a model tier call is allowed under the monthly budget.

    Haiku calls are always allowed regardless of spend.
    Sonnet/Opus calls are blocked when monthly spend >= budget cap.

    Args:
        conn: Open SQLite connection with scoring_costs table.
        config: Application config dict (reads scoring.monthly_budget_usd).
        model_tier: "haiku" or "sonnet" (or "opus"). Defaults to "sonnet".

    Returns:
        True if the call is allowed, False if blocked.
    """
    if model_tier == "haiku":
        return True

    budget_cap: float = (
        config.get("scoring", {}).get("monthly_budget_usd", DEFAULT_MONTHLY_BUDGET_USD)
    )

    # Sum cost_usd for the current calendar month
    now = datetime.now(timezone.utc)
    month_start = now.strftime("%Y-%m-01T00:00:00Z")

    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0.0) AS monthly_spend "
        "FROM scoring_costs "
        "WHERE timestamp >= ?",
        (month_start,),
    ).fetchone()

    monthly_spend: float = row[0] if row else 0.0
    return monthly_spend < budget_cap


# ---------------------------------------------------------------------------
# Cost statistics
# ---------------------------------------------------------------------------

def get_cost_stats(conn: sqlite3.Connection, budget_cap: float | None = None) -> dict:
    """Return aggregated cost statistics.

    Returns a dict with keys:
        today (float): Total spend today (UTC).
        week (float): Total spend in last 7 days.
        month (float): Total spend this calendar month.
        projected_monthly (float): month_spend / days_elapsed * 30.
        by_feature (list[dict]): [{purpose, calls, spend}] grouped by purpose.
        budget_cap (float): The monthly budget cap.

    Args:
        conn: Open SQLite connection with scoring_costs table.
        budget_cap: Override the default monthly budget cap.  When None,
            uses DEFAULT_MONTHLY_BUDGET_USD from config.

    Returns:
        Stats dict as described above.
    """
    if budget_cap is None:
        budget_cap = DEFAULT_MONTHLY_BUDGET_USD
    now = datetime.now(timezone.utc)

    today_start = now.strftime("%Y-%m-%dT00:00:00Z")
    week_start = (now.replace(hour=0, minute=0, second=0, microsecond=0) -
                  timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    month_start = now.strftime("%Y-%m-01T00:00:00Z")

    def _sum(query: str, params: tuple) -> float:
        row = conn.execute(query, params).fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0

    today_spend = _sum(
        "SELECT COALESCE(SUM(cost_usd), 0.0) FROM scoring_costs WHERE timestamp >= ?",
        (today_start,),
    )
    week_spend = _sum(
        "SELECT COALESCE(SUM(cost_usd), 0.0) FROM scoring_costs WHERE timestamp >= ?",
        (week_start,),
    )
    month_spend = _sum(
        "SELECT COALESCE(SUM(cost_usd), 0.0) FROM scoring_costs WHERE timestamp >= ?",
        (month_start,),
    )

    # Projected monthly: month_spend / days_elapsed * 30
    days_elapsed = max(now.day, 1)  # day-of-month, at least 1 to avoid division by zero
    projected_monthly = (month_spend / days_elapsed) * 30.0

    # By-feature breakdown
    rows = conn.execute(
        "SELECT purpose, COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0.0) AS spend "
        "FROM scoring_costs "
        "GROUP BY purpose "
        "ORDER BY spend DESC",
    ).fetchall()
    by_feature = [
        {"purpose": row[0], "calls": row[1], "spend": float(row[2])}
        for row in rows
    ]

    return {
        "today": today_spend,
        "week": week_spend,
        "month": month_spend,
        "projected_monthly": projected_monthly,
        "by_feature": by_feature,
        "budget_cap": budget_cap,
    }


# ---------------------------------------------------------------------------
# Historical cost breakdown queries
# ---------------------------------------------------------------------------

def get_daily_cost_breakdown(conn: sqlite3.Connection, days: int = 30) -> list[dict]:
    """Return per-day, per-purpose cost breakdown for the last N days.

    Args:
        conn: Open SQLite connection with scoring_costs table.
        days: Number of days to look back (inclusive). Defaults to 30.

    Returns:
        List of dicts with keys: date (str YYYY-MM-DD), purpose (str), spend (float).
        Sorted ascending by date, then purpose.
    """
    now = datetime.now(timezone.utc)
    start_date = (now - timedelta(days=days - 1)).strftime("%Y-%m-%dT00:00:00Z")

    rows = conn.execute(
        "SELECT date(timestamp) AS day, purpose, COALESCE(SUM(cost_usd), 0.0) AS spend "
        "FROM scoring_costs "
        "WHERE timestamp >= ? "
        "GROUP BY date(timestamp), purpose "
        "ORDER BY day ASC, purpose ASC",
        (start_date,),
    ).fetchall()

    return [
        {"date": row[0], "purpose": row[1], "spend": float(row[2])}
        for row in rows
    ]


def get_monthly_feature_breakdown(conn: sqlite3.Connection) -> list[dict]:
    """Return per-feature cost breakdown scoped to the current calendar month.

    Args:
        conn: Open SQLite connection with scoring_costs table.

    Returns:
        List of dicts with keys: purpose (str), calls (int), spend (float).
        Sorted descending by spend.
    """
    now = datetime.now(timezone.utc)
    month_start = now.strftime("%Y-%m-01T00:00:00Z")

    rows = conn.execute(
        "SELECT purpose, COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0.0) AS spend "
        "FROM scoring_costs "
        "WHERE timestamp >= ? "
        "GROUP BY purpose "
        "ORDER BY spend DESC",
        (month_start,),
    ).fetchall()

    return [
        {"purpose": row[0], "calls": int(row[1]), "spend": float(row[2])}
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Main API call wrapper
# ---------------------------------------------------------------------------

def call_claude(
    client: Any | None = None,
    model: str = "",
    system: str = "",
    messages: list[dict] | None = None,
    output_schema: dict | None = None,
    conn: sqlite3.Connection | None = None,
    job_id: str | None = None,
    purpose: str = "",
    config: dict | None = None,
    max_tokens: int = 1024,
    timeout: float | None = None,
    *,
    ctx: ClaudeContext | None = None,
) -> tuple[dict, float]:
    """Call Claude API with cost gating and automatic cost recording.

    Accepts the Anthropic client, database connection, and config either as
    individual parameters (legacy) or bundled in a ``ClaudeContext`` via the
    keyword-only ``ctx`` argument.  When ``ctx`` is provided its fields take
    precedence over the corresponding positional parameters.

    Args:
        client: Anthropic client instance (injected for testability).
            Ignored when *ctx* is provided.
        model: Full model identifier, e.g. "claude-haiku-4-5".
        system: System prompt string.
        messages: List of message dicts [{role, content}].
        output_schema: JSON schema dict for structured output (or None).
        conn: Open SQLite connection for cost recording.
            Ignored when *ctx* is provided.
        job_id: Job dedup_key for cost attribution (nullable).
        purpose: Feature label for cost attribution.
        config: Application config dict.
            Ignored when *ctx* is provided.
        max_tokens: Maximum output tokens. Defaults to 1024.
        timeout: Request timeout in seconds.  Defaults to DEFAULT_API_TIMEOUT_SECONDS (120).
        ctx: ClaudeContext bundling (client, conn, config).  When supplied,
            the individual client/conn/config parameters are ignored.

    Returns:
        Tuple of (parsed_json_result: dict, cost_usd: float).

    Raises:
        BudgetExceededError: If cost_gate blocks the call.
    """
    # Resolve context: prefer ctx fields over individual params
    if ctx is not None:
        client = ctx.client
        conn = ctx.conn
        config = ctx.config

    # Determine model tier for gating
    if "haiku" in model.lower():
        tier = "haiku"
    else:
        tier = "sonnet"

    if not cost_gate(conn, config, tier):
        raise BudgetExceededError(
            f"Monthly budget cap reached. Sonnet calls paused. Model: {model}"
        )

    effective_timeout = timeout if timeout is not None else DEFAULT_API_TIMEOUT_SECONDS

    # Build API call kwargs
    call_kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
        "timeout": effective_timeout,
    }

    # Add structured output config if schema provided
    if output_schema is not None:
        call_kwargs["tools"] = [
            {
                "name": "output",
                "description": "Structured output",
                "input_schema": output_schema,
            }
        ]
        call_kwargs["tool_choice"] = {"type": "tool", "name": "output"}

    response = client.messages.create(**call_kwargs)

    # Extract token counts and record cost before parsing content,
    # so cost data is always tracked even if content parsing fails.
    input_tokens: int = response.usage.input_tokens
    output_tokens: int = response.usage.output_tokens
    cost_usd = record_cost(conn, job_id, purpose, model, input_tokens, output_tokens)

    # Guard against empty response content
    if not response.content:
        raise RuntimeError("Claude returned empty response content")

    # Parse response content
    content = response.content[0]
    if output_schema is not None and hasattr(content, "input"):
        result = content.input
    else:
        text = content.text
        try:
            result = json.loads(text)
        except (json.JSONDecodeError, AttributeError):
            if output_schema is not None:
                raise ValueError(
                    "Structured output expected but got unparseable text"
                )
            result = {"text": str(text)}

    return result, cost_usd
