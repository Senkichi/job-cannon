"""Multi-provider model routing — types and config resolution.

Phase 24 deliverables: ModelResult, BaseProvider, resolve_provider_config().
call_model() dispatcher added in Phase 26.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from job_finder.config import DEFAULT_MODEL_HAIKU, DEFAULT_MODEL_OPUS, DEFAULT_MODEL_SONNET

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
