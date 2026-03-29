"""Multi-provider model routing — types, config resolution, and dispatcher.

Phase 24 deliverables: ModelResult, BaseProvider, resolve_provider_config().
Phase 26 deliverable: call_model() dispatcher.
Phase 29 deliverable: daily rate limit tracker (_check_daily_limit, _increment_usage,
    _init_usage_from_db, _ensure_usage_current).
"""
from __future__ import annotations

import logging
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date as _date
from typing import Any

from jsonschema import ValidationError, validate

from job_finder.config import DEFAULT_MODEL_HAIKU, DEFAULT_MODEL_OPUS, DEFAULT_MODEL_SONNET
from job_finder.web.claude_client import BudgetExceededError, cost_gate, record_cost

logger = logging.getLogger(__name__)

_TIER_DEFAULTS: dict[str, str] = {
    "haiku": DEFAULT_MODEL_HAIKU,
    "sonnet": DEFAULT_MODEL_SONNET,
    "opus": DEFAULT_MODEL_OPUS,
}

# Daily usage tracking — module-level state for rate limiting.
# Resets automatically on date rollover; bootstraps from scoring_costs DB.
_daily_usage: dict[str, int] = {}
_usage_date: str = ""


def _check_daily_limit(provider: str, daily_limits: dict[str, int]) -> bool:
    """Return True if provider is under its daily limit or has no configured limit.

    Args:
        provider: Provider name (e.g., "cerebras", "groq").
        daily_limits: Dict of {provider_name: max_requests_per_day}.

    Returns:
        True if the provider may be used, False if exhausted.
    """
    if provider not in daily_limits:
        return True
    return _daily_usage.get(provider, 0) < daily_limits[provider]


def _increment_usage(provider: str) -> None:
    """Increment the daily usage counter for a provider by 1."""
    global _daily_usage
    _daily_usage[provider] = _daily_usage.get(provider, 0) + 1


def _init_usage_from_db(conn: sqlite3.Connection) -> None:
    """Bootstrap _daily_usage from scoring_costs for today.

    Called on date rollover to recover counts for providers that were
    already used today (e.g., after an app restart mid-day).

    Args:
        conn: Open SQLite connection.
    """
    global _daily_usage, _usage_date
    _daily_usage = {}
    rows = conn.execute(
        "SELECT provider, COUNT(*) as cnt "
        "FROM scoring_costs "
        "WHERE date(timestamp) = date('now') "
        "GROUP BY provider"
    ).fetchall()
    for row in rows:
        _daily_usage[row[0]] = row[1]
    _usage_date = _date.today().isoformat()


def _ensure_usage_current(conn: sqlite3.Connection) -> None:
    """Reset and bootstrap daily usage counters if the date has rolled over.

    Should be called at the start of call_model() so _check_daily_limit
    and _increment_usage operate on today's data.

    Args:
        conn: Open SQLite connection for bootstrap query.
    """
    today = _date.today().isoformat()
    if _usage_date != today:
        _init_usage_from_db(conn)


@dataclass(frozen=True, slots=True)
class ModelResult:
    """Result from a provider adapter call."""

    data: dict
    cost_usd: float
    input_tokens: int
    output_tokens: int
    model: str
    provider: str
    schema_valid: bool


class BaseProvider(ABC):
    """Abstract base for provider adapters."""

    @abstractmethod
    def call(
        self,
        model: str,
        system: str,
        messages: list[dict],
        output_schema: dict | None = None,
        max_tokens: int = 1024,
        timeout: float | None = None,
    ) -> ModelResult:
        """Make a model call and return structured result."""
        ...


def resolve_provider_config(tier: str, config: dict) -> dict:
    """Resolve logical tier name to provider + model + fallback.

    Args:
        tier: Logical tier name: "sonnet", "haiku", or "opus".
        config: Full application config dict.

    Returns:
        Dict with keys:
            provider (str): "anthropic" | "gemini" | "ollama"
            model (str): Provider-specific model identifier
            fallback (str | None): Fallback provider name, or None
            fallback_chain (list[dict]): Ordered list of {provider, model} dicts for cascade, or []
            daily_limits (dict[str, int]): Per-provider daily request caps, or {}
    """
    providers_cfg = config.get("providers", {})
    tier_cfg = providers_cfg.get(tier, {})

    scoring_model = config.get("scoring", {}).get("models", {}).get(tier)
    default_model = scoring_model or _TIER_DEFAULTS.get(tier, DEFAULT_MODEL_SONNET)

    provider = tier_cfg.get("provider", "anthropic")
    model = tier_cfg.get("model") or default_model
    fallback = tier_cfg.get("fallback", None)
    fallback_chain = tier_cfg.get("fallback_chain", [])
    daily_limits = providers_cfg.get("daily_limits", {})

    return {
        "provider": provider,
        "model": model,
        "fallback": fallback,
        "fallback_chain": fallback_chain,
        "daily_limits": daily_limits,
    }


# ---------------------------------------------------------------------------
# call_model() dispatcher — Phase 26
# ---------------------------------------------------------------------------

# Providers that are free (no cost_gate needed, record cost via record_cost)
_FREE_PROVIDERS: frozenset[str] = frozenset({"gemini", "ollama", "ollm", "openrouter", "sambanova", "groq", "cerebras"})


def _validate_schema(data: dict, schema: dict | None) -> list[str]:
    """Return a list of validation error messages; empty list means valid.

    Guards against schema=None before calling jsonschema.validate (Pitfall 4).

    Args:
        data: The dict to validate.
        schema: JSON schema dict, or None (no validation).

    Returns:
        List of error message strings.  Empty if valid or schema is None.
    """
    if schema is None:
        return []
    try:
        validate(instance=data, schema=schema)
        return []
    except ValidationError as exc:
        return [exc.message]


def _augment_with_errors(messages: list[dict], errors: list[str]) -> list[dict]:
    """Return a NEW messages list with schema errors appended to the last message.

    CRITICAL: Does NOT mutate the input list (Pitfall 2).

    Args:
        messages: Original messages list.
        errors: List of validation error strings from _validate_schema.

    Returns:
        New list where the last message content has schema errors appended.
    """
    error_text = (
        "\n\nSchema validation errors from previous attempt:\n"
        + "\n".join(f"- {e}" for e in errors)
    )
    return messages[:-1] + [
        {**messages[-1], "content": messages[-1]["content"] + error_text}
    ]


def _make_adapter(
    provider_name: str,
    client: Any | None,
    conn: sqlite3.Connection,
    config: dict,
    job_id: str | None = None,
    purpose: str = "",
) -> "BaseProvider":
    """Instantiate the correct provider adapter.

    Args:
        provider_name: "anthropic", "gemini", or "ollama".
        client: Anthropic client (required for anthropic, unused for others).
        conn: Open SQLite connection (required for anthropic).
        config: Application config dict.
        job_id: Job dedup_key for cost attribution (nullable, Anthropic only).
        purpose: Feature attribution label for cost rows (Anthropic only).

    Returns:
        Concrete BaseProvider instance.

    Raises:
        ValueError: If provider_name is unrecognised.
    """
    # Lazy imports to avoid circular import: providers import from model_provider
    from job_finder.web.providers.anthropic_provider import AnthropicProvider
    from job_finder.web.providers.cerebras_provider import CerebrasProvider
    from job_finder.web.providers.cohere_provider import CohereProvider
    from job_finder.web.providers.groq_provider import GroqProvider
    from job_finder.web.providers.openrouter_provider import OpenRouterProvider
    from job_finder.web.providers.sambanova_provider import SambanovaProvider
    from job_finder.web.providers.gemini_provider import GeminiProvider
    from job_finder.web.providers.mistral_provider import MistralProvider
    from job_finder.web.providers.ollama_provider import OllamaProvider
    from job_finder.web.providers.ollm_provider import OllmProvider

    if provider_name == "anthropic":
        return AnthropicProvider(
            client=client, conn=conn, config=config, job_id=job_id, purpose=purpose
        )
    if provider_name == "cerebras":
        return CerebrasProvider(config=config)
    if provider_name == "cohere":
        return CohereProvider(config=config)
    if provider_name == "gemini":
        return GeminiProvider(config=config)
    if provider_name == "groq":
        return GroqProvider(config=config)
    if provider_name == "mistral":
        return MistralProvider(config=config)
    if provider_name == "ollama":
        return OllamaProvider(config=config)
    if provider_name == "ollm":
        return OllmProvider(config=config)
    if provider_name == "openrouter":
        return OpenRouterProvider(config=config)
    if provider_name == "sambanova":
        return SambanovaProvider(config=config)
    raise ValueError(f"Unknown provider: {provider_name!r}")


def _maybe_record_cost(
    result: "ModelResult",
    conn: sqlite3.Connection,
    job_id: str | None,
    purpose: str,
) -> None:
    """Record cost for non-Anthropic providers.

    AnthropicProvider delegates to call_claude() which records cost internally.
    Calling record_cost() again for Anthropic would double-count (Pitfall 1).

    Args:
        result: ModelResult from a provider adapter call.
        conn: Open SQLite connection.
        job_id: Job dedup_key for cost attribution (nullable).
        purpose: Feature attribution label.
    """
    if result.provider == "anthropic":
        return  # call_claude() already recorded it
    record_cost(
        conn,
        job_id,
        purpose,
        result.model,
        result.input_tokens,
        result.output_tokens,
        provider=result.provider,
    )


def call_model(
    tier: str,
    system: str,
    messages: list[dict],
    conn: sqlite3.Connection,
    config: dict,
    output_schema: dict | None = None,
    job_id: str | None = None,
    purpose: str = "",
    max_tokens: int = 1024,
    timeout: float | None = None,
    client: Any | None = None,
) -> "ModelResult":
    """Dispatch a model call to the configured provider for the given tier.

    Routes by tier, validates output schema with jsonschema, retries once with
    augmented prompt on schema failure, falls back to Anthropic when retry
    fails, bypasses budget gate for free providers, and records cost only for
    non-Anthropic providers (avoiding double-recording).

    Args:
        tier: Logical tier name: "sonnet", "haiku", or "opus".
        system: System prompt string.
        messages: List of message dicts [{role, content}].
        conn: Open SQLite connection for budget gating and cost recording.
        config: Application config dict.
        output_schema: JSON schema dict for structured output (or None).
        job_id: Job dedup_key for cost attribution (nullable).
        purpose: Feature attribution label for cost rows.
        max_tokens: Maximum output tokens. Defaults to 1024.
        timeout: Request timeout in seconds. Defaults to provider default.
        client: Anthropic client instance — required when provider is
            "anthropic" or fallback is "anthropic".

    Returns:
        ModelResult from the successful adapter call.

    Raises:
        BudgetExceededError: If cost_gate blocks an Anthropic call.
        RuntimeError: If schema validation fails after retry and no fallback
            is configured.
    """
    resolved = resolve_provider_config(tier, config)
    provider_name: str = resolved["provider"]
    model: str = resolved["model"]
    fallback: str | None = resolved["fallback"]

    # Budget gate — skip entirely for free providers (INFRA-04)
    if provider_name not in _FREE_PROVIDERS:
        if not cost_gate(conn, config, tier):
            raise BudgetExceededError(f"Budget cap reached. Tier: {tier}")

    # Instantiate and make first attempt
    adapter = _make_adapter(provider_name, client, conn, config, job_id=job_id, purpose=purpose)
    result = adapter.call(model, system, messages, output_schema, max_tokens, timeout)

    # Schema validation — attempt 1
    errors = _validate_schema(result.data, output_schema)
    if not errors:
        _maybe_record_cost(result, conn, job_id, purpose)
        return result

    # Retry with augmented prompt (INFRA-02)
    augmented = _augment_with_errors(messages, errors)
    result = adapter.call(model, system, augmented, output_schema, max_tokens, timeout)
    errors = _validate_schema(result.data, output_schema)
    if not errors:
        _maybe_record_cost(result, conn, job_id, purpose)
        return result

    # Fallback to Anthropic when retry fails (INFRA-03)
    if fallback and client is not None:
        from job_finder.web.providers.anthropic_provider import AnthropicProvider

        fallback_adapter = AnthropicProvider(
            client=client, conn=conn, config=config, job_id=job_id, purpose=purpose
        )
        # Use empty config to get Anthropic default model (not the Gemini/Ollama model name)
        fallback_model = resolve_provider_config(tier, {})["model"]
        # AnthropicProvider handles cost_gate + record_cost internally via call_claude()
        result = fallback_adapter.call(
            fallback_model, system, messages, output_schema, max_tokens, timeout
        )
        return result

    raise RuntimeError(
        f"Schema validation failed after retry and no fallback available for tier: {tier}"
    )
