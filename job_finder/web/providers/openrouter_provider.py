"""OpenRouter provider adapter — OpenAI-compatible multi-model API.

Uses the OpenRouter API (https://openrouter.ai/api/v1/chat/completions).
OpenAI-compatible format with Bearer token authentication.
Provides access to many models (including free tiers) through a single API.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import requests

from job_finder.web.model_provider import BaseProvider, ModelResult

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_TIMEOUT = 120.0


class OpenRouterProvider(BaseProvider):
    """Provider adapter for OpenRouter API.

    Reads the API key from the OPENROUTER_API_KEY environment variable
    (configurable via providers.openrouter.api_key_env in config).

    Args:
        config: Application config dict. Reads providers.openrouter.api_key_env
                (default: "OPENROUTER_API_KEY") and providers.openrouter.base_url
                (default: "https://openrouter.ai/api/v1").
    """

    def __init__(self, config: dict) -> None:
        provider_cfg = config.get("providers", {}).get("openrouter", {})
        self._base_url = provider_cfg.get("base_url", _DEFAULT_BASE_URL).rstrip("/")
        api_key_env = provider_cfg.get("api_key_env", "OPENROUTER_API_KEY")
        self._api_key = os.environ.get(api_key_env)
        if not self._api_key:
            raise ValueError(
                f"OpenRouter API key not set — expected env var {api_key_env!r}"
            )

    def call(
        self,
        model: str,
        system: str,
        messages: list[dict],
        output_schema: dict | None = None,
        max_tokens: int = 1024,
        timeout: float | None = None,
    ) -> ModelResult:
        """Make a chat completion call to OpenRouter /v1/chat/completions.

        Embeds schema in system prompt and requests JSON via
        response_format for structured output.

        Args:
            model: OpenRouter model identifier (e.g. "google/gemma-3-27b-it:free").
            system: System prompt string.
            messages: List of message dicts [{role, content}].
            output_schema: JSON schema dict for structured output (or None).
            max_tokens: Maximum output tokens.
            timeout: Request timeout in seconds. Defaults to 120.0.

        Returns:
            ModelResult with provider="openrouter", schema_valid=True.

        Raises:
            requests.HTTPError: On non-2xx response.
            json.JSONDecodeError: If response content is not valid JSON when
                schema was requested.
        """
        effective_timeout = timeout if timeout is not None else _DEFAULT_TIMEOUT

        # Embed schema in system prompt when provided
        system_with_schema = system
        if output_schema is not None:
            schema_str = json.dumps(output_schema, indent=2)
            system_with_schema = (
                f"{system}\n\nRespond with valid JSON matching this schema:\n{schema_str}"
            )

        openai_messages = [{"role": "system", "content": system_with_schema}] + [
            {"role": msg.get("role", "user"), "content": msg["content"]}
            for msg in messages
        ]

        payload: dict[str, Any] = {
            "model": model,
            "messages": openai_messages,
            "max_tokens": max_tokens,
        }

        if output_schema is not None:
            payload["response_format"] = {"type": "json_object"}

        resp = requests.post(
            f"{self._base_url}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=effective_timeout,
        )
        resp.raise_for_status()

        body = resp.json()
        content = body["choices"][0]["message"]["content"]

        if output_schema is not None:
            data = json.loads(content)
        else:
            data = {"text": content}

        usage = body.get("usage", {})

        return ModelResult(
            data=data,
            cost_usd=0.0,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            model=model,
            provider="openrouter",
            schema_valid=True,
        )
