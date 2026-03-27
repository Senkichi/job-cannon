"""Gemini provider adapter — google-genai SDK with response_json_schema.

Phase 25 deliverable — part of the multi-provider routing system.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from job_finder.web.model_provider import BaseProvider, ModelResult

logger = logging.getLogger(__name__)


class GeminiProvider(BaseProvider):
    """Provider adapter for Google Gemini via google-genai SDK.

    Uses response_json_schema (dict path) for structured output with
    response_mime_type="application/json".  Automatically retries once
    on HTTP 429 rate-limit errors with a configurable sleep duration
    (default 15.0s for the 5 RPM free tier introduced in Dec 2025).

    Args:
        config: Application config dict.  Reads ``providers.gemini.*``.
        client: Optional pre-built genai.Client for testing.  When provided,
            the GEMINI_API_KEY env-var check is bypassed entirely.
    """

    def __init__(self, config: dict, *, client: Any | None = None) -> None:
        provider_cfg = config.get("providers", {}).get("gemini", {})
        self._retry_sleep: float = provider_cfg.get("retry_sleep_seconds", 15.0)
        if client is not None:
            self._client = client
        else:
            api_key_env = provider_cfg.get("api_key_env", "GEMINI_API_KEY")
            api_key = os.environ.get(api_key_env)
            if not api_key:
                raise ValueError(
                    f"Gemini API key not set — expected env var {api_key_env!r}"
                )
            self._client = genai.Client(api_key=api_key)

    def call(
        self,
        model: str,
        system: str,
        messages: list[dict],
        output_schema: dict | None = None,
        max_tokens: int = 1024,
        timeout: float | None = None,
    ) -> ModelResult:
        """Make a Gemini model call and return a ModelResult.

        Args:
            model: Gemini model identifier, e.g. "gemini-2.0-flash".
            system: System prompt string (passed as system_instruction).
            messages: List of message dicts [{role, content}].
            output_schema: JSON schema dict for structured output. When provided,
                sets response_mime_type="application/json" and passes the schema
                via response_json_schema.  When None, raw text is returned as
                {"text": response.text}.
            max_tokens: Maximum output tokens.  Defaults to 1024.
            timeout: Unused (google-genai SDK handles timeouts internally).

        Returns:
            ModelResult with provider="gemini", cost_usd=0.0 (free tier),
            schema_valid=True.

        Raises:
            genai_errors.ClientError: On 429 after both attempts, or any other
                API error immediately.
        """
        contents = self._build_contents(messages)
        gen_config_kwargs: dict[str, Any] = {
            "system_instruction": system,
            "max_output_tokens": max_tokens,
        }
        if output_schema is not None:
            gen_config_kwargs["response_mime_type"] = "application/json"
            gen_config_kwargs["response_json_schema"] = output_schema
        gen_config = types.GenerateContentConfig(**gen_config_kwargs)

        response = None
        for attempt in range(2):
            try:
                response = self._client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=gen_config,
                )
                break
            except genai_errors.ClientError as exc:
                if exc.code == 429 and attempt == 0:
                    logger.warning(
                        "Gemini 429 rate limit on attempt 1, retrying in %.1fs",
                        self._retry_sleep,
                    )
                    time.sleep(self._retry_sleep)
                    continue
                raise

        if output_schema is not None:
            data = json.loads(response.text)
        else:
            data = {"text": response.text}

        return ModelResult(
            data=data,
            cost_usd=0.0,
            input_tokens=response.usage_metadata.prompt_token_count or 0,
            output_tokens=response.usage_metadata.candidates_token_count or 0,
            model=model,
            provider="gemini",
            schema_valid=True,
        )

    def _build_contents(self, messages: list[dict]) -> list[dict]:
        """Translate Anthropic-style messages to Gemini contents format."""
        return [
            {"role": msg.get("role", "user"), "parts": [{"text": msg["content"]}]}
            for msg in messages
        ]
