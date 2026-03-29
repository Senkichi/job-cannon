"""Mistral provider adapter — OpenAI-compatible API with native JSON schema support.

Uses the Mistral AI API (https://api.mistral.ai/v1/chat/completions).
Unlike Ollama/oLLM, Mistral supports native JSON schema enforcement via
response_format: {"type": "json_schema", "json_schema": {"schema": ...}}.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import requests

from job_finder.web.model_provider import BaseProvider, ModelResult

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.mistral.ai"
_DEFAULT_TIMEOUT = 120.0


class MistralProvider(BaseProvider):
    """Provider adapter for Mistral AI API.

    Reads the API key from the MISTRAL_API_KEY environment variable
    (configurable via providers.mistral.api_key_env in config).

    Args:
        config: Application config dict. Reads providers.mistral.api_key_env
                (default: "MISTRAL_API_KEY") and providers.mistral.base_url
                (default: "https://api.mistral.ai").
    """

    def __init__(self, config: dict) -> None:
        provider_cfg = config.get("providers", {}).get("mistral", {})
        self._base_url = provider_cfg.get("base_url", _DEFAULT_BASE_URL).rstrip("/")
        api_key_env = provider_cfg.get("api_key_env", "MISTRAL_API_KEY")
        self._api_key = os.environ.get(api_key_env)
        if not self._api_key:
            raise ValueError(
                f"Mistral API key not set — expected env var {api_key_env!r}"
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
        """Make a chat completion call to Mistral /v1/chat/completions.

        Uses native response_format with json_schema type when output_schema
        is provided — Mistral enforces schema compliance server-side, unlike
        Ollama/oLLM which rely on prompt-based schema embedding.

        Args:
            model: Mistral model identifier (e.g. "mistral-small-latest").
            system: System prompt string.
            messages: List of message dicts [{role, content}].
            output_schema: JSON schema dict for structured output (or None).
            max_tokens: Maximum output tokens.
            timeout: Request timeout in seconds. Defaults to 120.0.

        Returns:
            ModelResult with provider="mistral", schema_valid=True.

        Raises:
            requests.HTTPError: On non-2xx response.
            json.JSONDecodeError: If response content is not valid JSON when
                schema was requested.
        """
        effective_timeout = timeout if timeout is not None else _DEFAULT_TIMEOUT

        openai_messages = [{"role": "system", "content": system}] + [
            {"role": msg.get("role", "user"), "content": msg["content"]}
            for msg in messages
        ]

        payload: dict[str, Any] = {
            "model": model,
            "messages": openai_messages,
            "max_tokens": max_tokens,
        }

        if output_schema is not None:
            # Mistral requires name, strict, additionalProperties in the schema
            schema_copy = {**output_schema, "additionalProperties": False}
            if "title" not in schema_copy:
                schema_copy["title"] = "Response"
            if "required" not in schema_copy:
                schema_copy["required"] = list(schema_copy.get("properties", {}).keys())
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "schema": schema_copy,
                    "name": schema_copy.get("title", "Response").lower(),
                    "strict": True,
                },
            }

        resp = requests.post(
            f"{self._base_url}/v1/chat/completions",
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
            provider="mistral",
            schema_valid=True,
        )
