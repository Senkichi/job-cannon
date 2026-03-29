"""Cohere provider adapter — Cohere v2 Chat API with JSON schema support.

Uses the Cohere v2 API (https://api.cohere.com/v2/chat).
Custom format (not OpenAI-compatible): role-specific message structures,
response_format with json_schema for structured output.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import requests

from job_finder.web.model_provider import BaseProvider, ModelResult

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.cohere.com"
_DEFAULT_TIMEOUT = 120.0


class CohereProvider(BaseProvider):
    """Provider adapter for Cohere v2 Chat API.

    Reads the API key from the CO_API_KEY environment variable
    (configurable via providers.cohere.api_key_env in config).

    Args:
        config: Application config dict. Reads providers.cohere.api_key_env
                (default: "CO_API_KEY") and providers.cohere.base_url
                (default: "https://api.cohere.com").
    """

    def __init__(self, config: dict) -> None:
        provider_cfg = config.get("providers", {}).get("cohere", {})
        self._base_url = provider_cfg.get("base_url", _DEFAULT_BASE_URL).rstrip("/")
        api_key_env = provider_cfg.get("api_key_env", "CO_API_KEY")
        self._api_key = os.environ.get(api_key_env)
        if not self._api_key:
            raise ValueError(
                f"Cohere API key not set — expected env var {api_key_env!r}"
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
        """Make a chat call to Cohere v2 /v2/chat.

        Uses response_format with json_schema for structured output when
        output_schema is provided.

        Args:
            model: Cohere model identifier (e.g. "command-a-03-2025").
            system: System prompt string.
            messages: List of message dicts [{role, content}].
            output_schema: JSON schema dict for structured output (or None).
            max_tokens: Maximum output tokens.
            timeout: Request timeout in seconds. Defaults to 120.0.

        Returns:
            ModelResult with provider="cohere", schema_valid=True.

        Raises:
            requests.HTTPError: On non-2xx response.
            json.JSONDecodeError: If response content is not valid JSON when
                schema was requested.
        """
        effective_timeout = timeout if timeout is not None else _DEFAULT_TIMEOUT

        # Cohere v2 uses role-specific message format
        cohere_messages = [{"role": "system", "content": system}] + [
            {"role": msg.get("role", "user"), "content": msg["content"]}
            for msg in messages
        ]

        payload: dict[str, Any] = {
            "model": model,
            "messages": cohere_messages,
            "max_tokens": max_tokens,
            "stream": False,
        }

        if output_schema is not None:
            payload["response_format"] = {
                "type": "json_object",
                "json_schema": output_schema,
            }

        resp = requests.post(
            f"{self._base_url}/v2/chat",
            json=payload,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            timeout=effective_timeout,
        )
        resp.raise_for_status()

        body = resp.json()

        # Cohere v2 response: message.content is a list of content blocks
        content_blocks = body.get("message", {}).get("content", [])
        raw_text = ""
        for block in content_blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                raw_text = block.get("text", "")
                break
            elif isinstance(block, str):
                raw_text = block
                break

        if output_schema is not None:
            data = json.loads(raw_text)
        else:
            data = {"text": raw_text}

        usage = body.get("usage", {})

        return ModelResult(
            data=data,
            cost_usd=0.0,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            model=model,
            provider="cohere",
            schema_valid=True,
        )
