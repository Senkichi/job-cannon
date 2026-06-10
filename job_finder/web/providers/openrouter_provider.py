"""OpenRouter provider adapter for cascade-audit judge calls.

Implements BaseProvider to enable OpenRouter-hosted models (currently
DeepSeek-V4-Flash via `deepseek/deepseek-v4-flash:free`) for the cascade
audit judge protocol. The provider itself is model-agnostic — `model=` is
passed through to OpenRouter unchanged; see `evals/cascade_audit/judge.py`
for the judge's chosen model id. No Anthropic spend incurred.

Phase 36 deliverable — part of the cascade audit eval harness.

Issue 292 (2026-06-10): compute real ``cost_usd`` by preferring the API-
reported ``usage.cost`` field (OpenRouter returns the actual provider charge
in the response body).  Falls back to the static pricing table when the field
is absent or None.  The current production model (``deepseek-v4-flash:free``)
is $0 — the :free suffix on OpenRouter models always means $0/token.
"""

from __future__ import annotations

import json
import logging

import requests

from job_finder.secrets import get_secret
from job_finder.web.model_provider import BaseProvider, ModelResult

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_TIMEOUT = 300.0

# ---------------------------------------------------------------------------
# Pricing table — price per million tokens (USD)
# Source: https://openrouter.ai/deepseek/deepseek-v4-flash  last_verified: 2026-06-10
#
# The :free suffix models (e.g. deepseek/deepseek-v4-flash:free) are always $0.
# Paid variants are listed here for the static-table fallback path.
# ---------------------------------------------------------------------------
_OPENROUTER_PRICING: dict[str, dict[str, float]] = {
    # :free variants — OpenRouter free tier, genuinely $0/token
    "deepseek/deepseek-v4-flash:free": {"input": 0.0, "output": 0.0},
    # paid variant
    # Source: https://openrouter.ai/deepseek/deepseek-v4-flash  last_verified: 2026-06-10
    "deepseek/deepseek-v4-flash": {"input": 0.0983, "output": 0.1966},
}


def _openrouter_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return cost in USD for an OpenRouter call using the static pricing table.

    Falls back to the most expensive known entry for unrecognised model IDs
    (conservative — gate trips early rather than never).
    """
    pricing = _OPENROUTER_PRICING.get(model)
    if pricing is None:
        logger.warning(
            "Unknown OpenRouter model '%s' in _openrouter_cost — using highest known pricing as fallback",
            model,
        )
        pricing = max(_OPENROUTER_PRICING.values(), key=lambda p: p["input"] + p["output"])
    return (input_tokens / 1_000_000) * pricing["input"] + (output_tokens / 1_000_000) * pricing[
        "output"
    ]


class OpenRouterProvider(BaseProvider):
    """Provider adapter for OpenRouter API (cascade-audit judge — currently DeepSeek-V4-Flash).

    Uses OPENROUTER_API_KEY environment variable. Prefers the API-reported
    ``usage.cost`` field for ``cost_usd``; falls back to ``_openrouter_cost()``
    when the field is absent (Issue 292, 2026-06-10).  The current production
    model (``deepseek/deepseek-v4-flash:free``) returns $0 — the ``:free``
    suffix on OpenRouter models always means $0/token.

    Args:
        config: Application config dict (unused for OpenRouter, kept for
                interface consistency with other providers).

    Raises:
        ValueError: If OPENROUTER_API_KEY environment variable is not set.
    """

    def __init__(self, config: dict) -> None:
        self._api_key = get_secret("providers.api_keys.openrouter", config=config)
        if not self._api_key:
            raise ValueError(
                "OpenRouter API key not set. Set OPENROUTER_API_KEY env var, "
                "store it in the OS keyring, or add providers.api_keys.openrouter "
                "to config.yaml."
            )
        self._base_url = _DEFAULT_BASE_URL

    def call(
        self,
        model: str,
        system: str,
        messages: list[dict],
        output_schema: dict | None = None,
        max_tokens: int = 1024,
        timeout: float | None = None,
    ) -> ModelResult:
        """Make a chat completion call to OpenRouter /api/v1/chat/completions.

        Args:
            model: Model identifier, e.g. "deepseek/deepseek-v4-flash:free".
            system: System prompt string.
            messages: List of message dicts [{role, content}].
            output_schema: JSON schema dict for structured output (or None).
                When provided, added to request via response_format.
            max_tokens: Maximum output tokens. Defaults to 1024.
            timeout: Request timeout in seconds. Defaults to 300.0.

        Returns:
            ModelResult with provider="openrouter", real ``cost_usd``
            (API-reported when available, otherwise ``_openrouter_cost()``),
            and schema_valid=True (assume valid for judge output).

        Raises:
            requests.HTTPError: On non-2xx response from OpenRouter API.
            json.JSONDecodeError: If response content is not valid JSON.
        """
        effective_timeout = timeout if timeout is not None else _DEFAULT_TIMEOUT

        # Build request payload
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system}] + messages,
            "temperature": 0,  # Deterministic for judge consistency
            "max_tokens": max_tokens,
        }

        # Add response_format if output_schema provided
        if output_schema is not None:
            payload["response_format"] = {
                "type": "json_object",
                "json_schema": output_schema,
            }

        # Make HTTP POST request
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        resp = requests.post(
            f"{self._base_url}/chat/completions",
            json=payload,
            headers=headers,
            timeout=effective_timeout,
        )
        resp.raise_for_status()

        # Parse response
        body = resp.json()
        content = body["choices"][0]["message"]["content"]
        data = json.loads(content)

        # Extract token counts from usage
        usage = body.get("usage", {})
        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)

        # Prefer API-reported cost (usage.cost) over static table — OpenRouter
        # returns the actual provider charge in the response body, which is more
        # accurate than our static table for dynamic/new models.
        api_cost = usage.get("cost")
        if api_cost is not None:
            cost_usd = float(api_cost)
        else:
            cost_usd = _openrouter_cost(model, input_tokens, output_tokens)

        return ModelResult(
            data=data,
            cost_usd=cost_usd,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model,
            provider="openrouter",
            schema_valid=True,  # Assume valid for judge output
        )
