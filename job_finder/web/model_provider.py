"""Multi-provider model routing — types, config resolution, and dispatcher.

Phase 24 deliverables: ModelResult, BaseProvider, resolve_provider_config().
Phase 26 deliverable: call_model() dispatcher.
"""
from __future__ import annotations

import logging
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass
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
    """
    providers_cfg = config.get("providers", {})
    tier_cfg = providers_cfg.get(tier, {})

    scoring_model = config.get("scoring", {}).get("models", {}).get(tier)
    default_model = scoring_model or _TIER_DEFAULTS.get(tier, DEFAULT_MODEL_SONNET)

    provider = tier_cfg.get("provider", "anthropic")
    model = tier_cfg.get("model") or default_model
    fallback = tier_cfg.get("fallback", None)

    return {"provider": provider, "model": model, "fallback": fallback}


# ---------------------------------------------------------------------------
# call_model() dispatcher — Phase 26
# ---------------------------------------------------------------------------

# Providers that are free (no cost_gate needed, record cost via record_cost)
_FREE_PROVIDERS: frozenset[str] = frozenset({"gemini", "ollama"})


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
) -> "BaseProvider":
    """Instantiate the correct provider adapter.

    Args:
        provider_name: "anthropic", "gemini", or "ollama".
        client: Anthropic client (required for anthropic, unused for others).
        conn: Open SQLite connection (required for anthropic).
        config: Application config dict.

    Returns:
        Concrete BaseProvider instance.

    Raises:
        ValueError: If provider_name is unrecognised.
    """
    # Lazy imports to avoid circular import: providers import from model_provider
    from job_finder.web.providers.anthropic_provider import AnthropicProvider
    from job_finder.web.providers.gemini_provider import GeminiProvider
    from job_finder.web.providers.ollama_provider import OllamaProvider

    if provider_name == "anthropic":
        return AnthropicProvider(client=client, conn=conn, config=config)
    if provider_name == "gemini":
        return GeminiProvider(config=config)
    if provider_name == "ollama":
        return OllamaProvider(config=config)
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
    adapter = _make_adapter(provider_name, client, conn, config)
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

        fallback_adapter = AnthropicProvider(client=client, conn=conn, config=config)
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
