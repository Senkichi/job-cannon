"""Claude Code CLI oneshot wrapper with cost recording and budget gating.

Dispatches all Anthropic model calls through ``claude -p`` CLI subprocesses
instead of the ``anthropic`` Python SDK.  Each call is a strictly configured
oneshot: minimal context (temp-dir cwd), defined output (--json-schema),
no tools, no session persistence.

Provides:
- _run_oneshot: Low-level CLI subprocess executor.
- compute_cost: Calculate USD cost from token counts and model pricing.
- record_cost: Insert a cost row into scoring_costs and return cost_usd.
- cost_gate: Check whether a model tier is allowed given the monthly budget.
- get_cost_stats: Aggregate cost data by time period and feature/purpose.
- call_claude: Convenience wrapper for CLI oneshots with automatic cost recording.
- ClaudeContext: Dataclass bundling the (conn, config) pair for call_claude.
- BudgetExceededError: Raised by call_claude when the budget cap is exceeded.

Model pricing (per million tokens) — informational, for cost tracking:
  claude-haiku-4-5:  $1.00 input / $5.00 output
  claude-sonnet-4-6: $3.00 input / $15.00 output
"""

import json
import logging
import os
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from job_finder.config import DEFAULT_DAILY_BUDGET_USD
from job_finder.json_utils import utc_now_iso

try:
    from jsonschema import ValidationError as _ValidationError
    from jsonschema import validate as _jsonschema_validate
except ImportError:
    _ValidationError = None  # type: ignore[assignment,misc]
    _jsonschema_validate = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_DEFAULT_API_TIMEOUT_SECONDS: int = 120

# ---------------------------------------------------------------------------
# Pricing table — price per million tokens (USD)
# ---------------------------------------------------------------------------

MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-opus-4-6": {"input": 5.0, "output": 25.0},
}

# Providers that incur no per-call cost.  Used by cost_gate() to exclude
# free/subscription spend from budget calculations, and by record_cost()
# to record $0 for these providers.
# - "claude_cli" = legacy call_claude() internal path (kept for backward compat).
# - "claude_code_cli" / "gemini_cli" / "local_bundled" = Phase 39 new providers.
FREE_PROVIDERS: frozenset[str] = frozenset(
    {
        "gemini",
        "ollama",
        "claude_cli",        # existing — internal call_claude() path
        "claude_code_cli",   # NEW — ClaudeCodeCLIProvider (Plan 02)
        "gemini_cli",        # NEW — GeminiCLIProvider (Plan 03)
        "local_bundled",     # NEW — LocalBundledProvider (Plan 04)
    }
)


def is_anthropic_available() -> bool:
    """Return True if Anthropic CLI fallback is configured.

    Phase M-2 (2026-05-20) confirmed every Anthropic dispatch routes through
    the ``claude -p`` subprocess, not the Python SDK. The CLI's auth check
    honors ``ANTHROPIC_API_KEY`` and the project-namespaced
    ``JF_ANTHROPIC_API_KEY``. When neither is set, the CLI rejects the call
    during cascade execution, so the cascade should skip this hop preemptively.
    """
    return bool(
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("JF_ANTHROPIC_API_KEY")
    )


class BudgetExceededError(Exception):
    """Raised when a non-Haiku Claude call is blocked by the monthly budget cap."""


@dataclass(frozen=True, slots=True)
class ClaudeContext:
    """Invariant pair threaded through every call_claude invocation.

    Bundles the database connection and app config that every caller
    assembles identically. The CLI handles its own authentication via env
    vars — no Python SDK client is constructed.
    """

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
        pricing = max(MODEL_PRICING.values(), key=lambda p: p["input"] + p["output"])
    return (input_tokens / 1_000_000) * pricing["input"] + (output_tokens / 1_000_000) * pricing[
        "output"
    ]


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
    provider: str = "anthropic",
    schema_valid: bool = True,
) -> float:
    """Insert a cost row into scoring_costs and return cost_usd.

    Args:
        conn: Open SQLite connection.
        job_id: Job dedup_key this call is associated with (nullable).
        purpose: Feature attribution label, e.g. "haiku_score", "sonnet_eval".
        model: Model identifier used for the call.
        input_tokens: Number of input tokens consumed.
        output_tokens: Number of output tokens generated.
        provider: Provider name for attribution (default "anthropic").
        schema_valid: Whether schema validation succeeded (default True).

    Returns:
        Computed cost in USD (0.0 for free/subscription providers).
    """
    cost_usd = (
        0.0 if provider in FREE_PROVIDERS else compute_cost(model, input_tokens, output_tokens)
    )
    timestamp = utc_now_iso()
    conn.execute(
        "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp, provider, schema_valid) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp, provider, int(schema_valid)),
    )
    conn.commit()
    return cost_usd


# ---------------------------------------------------------------------------
# Provider breakdown
# ---------------------------------------------------------------------------


def get_monthly_provider_breakdown(conn: sqlite3.Connection) -> list[dict]:
    """Return per-provider cost breakdown scoped to the current calendar month.

    Args:
        conn: Open SQLite connection with scoring_costs table.

    Returns:
        List of dicts with keys: provider (str), calls (int), spend (float).
        Sorted descending by spend.
    """
    now = datetime.now(UTC)
    month_start = now.strftime("%Y-%m-01T00:00:00Z")

    rows = conn.execute(
        "SELECT provider, COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0.0) AS spend "
        "FROM scoring_costs "
        "WHERE timestamp >= ? "
        "GROUP BY provider "
        "ORDER BY spend DESC",
        (month_start,),
    ).fetchall()

    return [{"provider": row[0], "calls": int(row[1]), "spend": float(row[2])} for row in rows]


# ---------------------------------------------------------------------------
# Budget gating
# ---------------------------------------------------------------------------


def cost_gate(
    conn: sqlite3.Connection,
    config: dict,
    model_tier: str = "score",
) -> bool:
    """Check whether a model tier call is allowed under the daily budget.

    Applies to NON-FREE BYO-key providers only. The Anthropic CLI fallback,
    Ollama, the two Cloud CLIs, Groq, and Cerebras all live in FREE_PROVIDERS
    (claude_client.FREE_PROVIDERS) and never count against the budget — the
    spend sum below explicitly excludes them. The remaining real-cost
    provider in the v5.0 cascade is OpenRouter (used as the cascade-audit
    judge); that's the tripwire this gate exists for. M-2 (2026-05-20).

    Quick-tier calls are always allowed regardless of spend.
    Score/triage-tier calls are blocked when daily spend >= budget cap.

    Args:
        conn: Open SQLite connection with scoring_costs table.
        config: Application config dict (reads scoring.daily_budget_usd).
        model_tier: "quick", "score", or "triage". Defaults to "score".

    Returns:
        True if the call is allowed, False if blocked.
    """
    if model_tier == "quick":
        return True

    scoring_cfg = config.get("scoring", {})
    daily_cap: float = scoring_cfg.get("daily_budget_usd", DEFAULT_DAILY_BUDGET_USD)

    now = datetime.now(UTC)

    # Only count spend from per-call billed providers (exclude free/subscription)
    free = tuple(FREE_PROVIDERS)
    free_placeholders = ",".join("?" * len(free))

    # Daily check
    day_start = now.strftime("%Y-%m-%dT00:00:00Z")
    row = conn.execute(
        f"SELECT COALESCE(SUM(cost_usd), 0.0) "
        f"FROM scoring_costs WHERE timestamp >= ? "
        f"AND provider NOT IN ({free_placeholders})",
        (day_start, *free),
    ).fetchone()
    return not (row[0] if row else 0.0) >= daily_cap


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
        budget_cap (float): The daily budget cap.

    Args:
        conn: Open SQLite connection with scoring_costs table.
        budget_cap: Override the default daily budget cap.  When None,
            uses DEFAULT_DAILY_BUDGET_USD from config.

    Returns:
        Stats dict as described above.
    """
    if budget_cap is None:
        budget_cap = DEFAULT_DAILY_BUDGET_USD
    now = datetime.now(UTC)

    today_start = now.strftime("%Y-%m-%dT00:00:00Z")
    week_start = (
        now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=7)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    month_start = now.strftime("%Y-%m-01T00:00:00Z")

    # Single query computes all three time-window sums in one index scan
    row = conn.execute(
        "SELECT "
        "  COALESCE(SUM(CASE WHEN timestamp >= ? THEN cost_usd END), 0.0), "
        "  COALESCE(SUM(CASE WHEN timestamp >= ? THEN cost_usd END), 0.0), "
        "  COALESCE(SUM(CASE WHEN timestamp >= ? THEN cost_usd END), 0.0) "
        "FROM scoring_costs WHERE timestamp >= ?",
        (today_start, week_start, month_start, month_start),
    ).fetchone()
    today_spend = float(row[0])
    week_spend = float(row[1])
    month_spend = float(row[2])

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
    by_feature = [{"purpose": row[0], "calls": row[1], "spend": float(row[2])} for row in rows]

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
    now = datetime.now(UTC)
    start_date = (now - timedelta(days=days - 1)).strftime("%Y-%m-%dT00:00:00Z")

    rows = conn.execute(
        "SELECT date(timestamp) AS day, purpose, COALESCE(SUM(cost_usd), 0.0) AS spend "
        "FROM scoring_costs "
        "WHERE timestamp >= ? "
        "GROUP BY date(timestamp), purpose "
        "ORDER BY day ASC, purpose ASC",
        (start_date,),
    ).fetchall()

    return [{"date": row[0], "purpose": row[1], "spend": float(row[2])} for row in rows]


def get_monthly_feature_breakdown(conn: sqlite3.Connection) -> list[dict]:
    """Return per-feature cost breakdown scoped to the current calendar month.

    Args:
        conn: Open SQLite connection with scoring_costs table.

    Returns:
        List of dicts with keys: purpose (str), calls (int), spend (float).
        Sorted descending by spend.
    """
    now = datetime.now(UTC)
    month_start = now.strftime("%Y-%m-01T00:00:00Z")

    rows = conn.execute(
        "SELECT purpose, COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0.0) AS spend "
        "FROM scoring_costs "
        "WHERE timestamp >= ? "
        "GROUP BY purpose "
        "ORDER BY spend DESC",
        (month_start,),
    ).fetchall()

    return [{"purpose": row[0], "calls": int(row[1]), "spend": float(row[2])} for row in rows]


# ---------------------------------------------------------------------------
# CLI model aliases — map full SDK model names to short CLI aliases
# ---------------------------------------------------------------------------

_CLI_MODEL_ALIASES: dict[str, str] = {
    "claude-haiku-4-5": "haiku",
    "claude-sonnet-4-6": "sonnet",
    "claude-opus-4-6": "opus",
}

_CREDIT_PATTERNS: tuple[str, ...] = (
    "credit balance",
    "spending limit",
    "insufficient credits",
    "credit balance too low",
    "out of credits",
)


# ---------------------------------------------------------------------------
# CLI oneshot executor
# ---------------------------------------------------------------------------


def _run_oneshot(
    model: str,
    system: str,
    user_message: str,
    json_schema: dict | None = None,
    timeout: float = 120,
) -> dict:
    """Run a ``claude -p`` oneshot and return the parsed JSON envelope.

    Each call is strictly configured: temp-dir cwd (no CLAUDE.md), no tools,
    no session persistence, user prompt piped via stdin.

    Args:
        model: Model identifier (e.g. "claude-haiku-4-5" or "haiku").
        system: System prompt string.
        user_message: User message string (piped via stdin to avoid
            Windows command-line length limits).
        json_schema: JSON schema dict for structured output, or None
            for freeform text responses.
        timeout: Subprocess timeout in seconds. Defaults to 120.

    Returns:
        Parsed CLI JSON envelope dict.

    Raises:
        FileNotFoundError: If ``claude`` CLI is not on PATH.
        TimeoutError: If the subprocess exceeds *timeout*.
        RuntimeError: On non-zero exit code or CLI-reported error.
        BudgetExceededError: On credit-exhaustion errors from the CLI.
    """
    cli_model = _CLI_MODEL_ALIASES.get(model, model)

    cmd: list[str] = [
        "claude",
        "-p",
        "--model",
        cli_model,
        "--output-format",
        "json",
        "--no-session-persistence",
        "--tools",
        "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--system-prompt",
        system,
    ]

    if json_schema is not None:
        cmd.extend(["--json-schema", json.dumps(json_schema)])

    # Cold-start tuning. --bare is intentionally avoided: it forces ANTHROPIC_API_KEY
    # auth and bypasses the OAuth/subscription path, which would reroute billing.
    cli_env = {
        **os.environ,
        "MCP_CONNECTION_NONBLOCKING": "true",
        "MAX_STRUCTURED_OUTPUT_RETRIES": "1",
    }

    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            result = subprocess.run(
                cmd,
                input=user_message,
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
                cwd=tmpdir,
                env=cli_env,
            )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"Claude CLI timed out after {timeout}s (model={cli_model})") from exc
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            "claude CLI not found on PATH. Install: npm install -g @anthropic-ai/claude-code"
        ) from exc

    if result.returncode != 0:
        stderr = result.stderr.strip()[:300] if result.stderr else "unknown error"
        raise RuntimeError(f"Claude CLI failed (rc={result.returncode}): {stderr}")

    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON from Claude CLI: {result.stdout[:300]}") from exc

    if envelope.get("is_error"):
        error_msg = str(envelope.get("result", "unknown error"))[:300]
        if any(p in error_msg.lower() for p in _CREDIT_PATTERNS):
            raise BudgetExceededError(error_msg)
        raise RuntimeError(f"Claude CLI error: {error_msg}")

    return envelope


# ---------------------------------------------------------------------------
# Main call wrapper
# ---------------------------------------------------------------------------


def call_claude(
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
) -> tuple[dict, float, bool]:
    """Run a Claude CLI oneshot with budget gating and cost recording.

    Dispatches to ``_run_oneshot()`` which invokes ``claude -p`` as a
    subprocess. The CLI handles its own authentication via the
    ``ANTHROPIC_API_KEY`` / ``JF_ANTHROPIC_API_KEY`` env vars; no Python
    SDK client is constructed.

    Args:
        model: Full model identifier, e.g. "claude-haiku-4-5".
        system: System prompt string.
        messages: List of message dicts [{role, content}].
        output_schema: JSON schema dict for structured output (or None).
        conn: Open SQLite connection for cost recording.
        job_id: Job dedup_key for cost attribution (nullable).
        purpose: Feature label for cost attribution.
        config: Application config dict.
        max_tokens: Ignored by CLI (no --max-tokens flag). Kept for
            interface compatibility.
        timeout: Subprocess timeout in seconds. Defaults to 120.
        ctx: ClaudeContext bundling (conn, config). When supplied,
            the individual conn/config parameters are ignored.

    Returns:
        Tuple of (parsed_json_result: dict, cost_usd: float, schema_valid: bool).

    Raises:
        BudgetExceededError: If cost_gate blocks the call.
        ValueError: If conn is None.
    """
    if ctx is not None:
        conn = ctx.conn
        config = ctx.config

    if config is None:
        config = {}

    # Determine model tier for budget gating
    matching_pricing_key = next((k for k in MODEL_PRICING if model.startswith(k)), None)
    if matching_pricing_key:
        tier = "quick" if "haiku" in matching_pricing_key else "score"
    else:
        tier = "quick" if "haiku" in model.lower() else "score"

    if conn is None:
        raise ValueError("call_claude requires a database connection (conn is None)")

    logger.info(
        "call_claude START: purpose=%s model=%s job_id=%s tier=%s",
        purpose,
        model,
        job_id,
        tier,
    )
    schema_valid = True  # Default to valid if no schema

    if not cost_gate(conn, config, tier):
        raise BudgetExceededError(
            f"Monthly budget cap reached. Sonnet calls paused. Model: {model}"
        )

    effective_timeout = timeout if timeout is not None else _DEFAULT_API_TIMEOUT_SECONDS

    # Extract user message from messages list
    user_message = ""
    if messages:
        user_message = messages[-1].get("content", "")

    # Run CLI oneshot
    envelope = _run_oneshot(
        model=model,
        system=system,
        user_message=user_message,
        json_schema=output_schema,
        timeout=effective_timeout,
    )

    # Extract token counts and record cost
    usage = envelope.get("usage", {})
    input_tokens: int = usage.get("input_tokens", 0)
    output_tokens: int = usage.get("output_tokens", 0)
    cost_usd = record_cost(
        conn, job_id, purpose, model, input_tokens, output_tokens, provider="claude_cli"
    )

    # Parse result — structured_output when schema was provided,
    # otherwise parse the text result as JSON with fallback.
    if output_schema is not None:
        result = envelope.get("structured_output")
        if result is None:
            raw = envelope.get("result", "")
            try:
                result = json.loads(raw)
            except (json.JSONDecodeError, TypeError) as exc:
                raise ValueError(
                    "Structured output expected but not found in CLI response"
                ) from exc
    else:
        raw = envelope.get("result", "")
        try:
            result = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            result = {"text": str(raw)}

    # --- Post-parse schema validation with one retry ---
    if output_schema is not None and _jsonschema_validate is not None and isinstance(result, dict):
        try:
            _jsonschema_validate(instance=result, schema=output_schema)
            schema_valid = True
        except _ValidationError as _val_err:
            logger.warning(
                "call_claude schema validation failed: purpose=%s model=%s error=%s — retrying once",
                purpose,
                model,
                _val_err.message,
            )
            schema_valid = False
            retry_user_message = (
                user_message
                + f"\n\nSchema validation error from previous attempt:\n- {_val_err.message}\n\n"
                "Please provide a response that satisfies all schema constraints."
            )
            try:
                retry_envelope = _run_oneshot(
                    model=model,
                    system=system,
                    user_message=retry_user_message,
                    json_schema=output_schema,
                    timeout=effective_timeout,
                )
            except Exception as retry_exc:
                raise ValueError(
                    f"Schema validation retry API call failed: {retry_exc}"
                ) from retry_exc

            # Record cost for the retry call
            retry_usage = retry_envelope.get("usage", {})
            record_cost(
                conn,
                job_id,
                purpose,
                model,
                retry_usage.get("input_tokens", 0),
                retry_usage.get("output_tokens", 0),
                provider="claude_cli",
                schema_valid=schema_valid,
            )

            # Parse retry result
            if output_schema is not None:
                result = retry_envelope.get("structured_output")
                if result is None:
                    raw = retry_envelope.get("result", "")
                    try:
                        result = json.loads(raw)
                    except (json.JSONDecodeError, TypeError) as exc:
                        raise ValueError(
                            "Schema validation retry returned unparseable response"
                        ) from exc
            else:
                raw = retry_envelope.get("result", "")
                try:
                    result = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    result = {"text": str(raw)}

            try:
                _jsonschema_validate(instance=result, schema=output_schema)
                schema_valid = True
            except _ValidationError as _retry_err:
                raise ValueError(
                    f"Schema validation failed after retry: {_retry_err.message} "
                    f"(purpose={purpose}, model={model})"
                ) from _retry_err

    return result, cost_usd, schema_valid
