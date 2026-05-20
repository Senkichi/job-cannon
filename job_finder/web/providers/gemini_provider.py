"""Gemini provider adapter — google-generativeai SDK with response_json_schema.

Phase 25 deliverable — part of the multi-provider routing system.

NOTE: This implementation requires google-generativeai >= 0.8.5.
The API may differ from earlier versions; ensure compatibility.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

try:
    import google.generativeai as genai
    from google.generativeai import types

    _GENAI_AVAILABLE = True
except ImportError:
    _GENAI_AVAILABLE = False

from job_finder.secrets import get_secret
from job_finder.web.model_provider import BaseProvider, ModelResult

logger = logging.getLogger(__name__)


class GeminiProvider(BaseProvider):
    """Provider adapter for Google Gemini via google-generativeai SDK.

    Uses response_json_schema for structured output with
    response_mime_type="application/json". Automatically retries once
    on transient errors with a configurable sleep duration
    (default 15.0s for the 5 RPM free tier).

    Args:
        config: Application config dict. Reads providers.gemini.*.
        client: Optional pre-built genai.GenerativeModel for testing.
                When provided, skips API key configuration.
    """

    def __init__(self, config: dict, *, client: Any | None = None) -> None:
        if not _GENAI_AVAILABLE:
            raise ImportError(
                "google-generativeai is required for Gemini provider. "
                "Install with: pip install google-generativeai~=0.8.5"
            )
        provider_cfg = config.get("providers", {}).get("gemini", {})
        self._retry_sleep: float = provider_cfg.get("retry_sleep_seconds", 15.0)

        if client is not None:
            self._client = client
        else:
            # Honor the custom api_key_env override first (lets users point at
            # a non-default env var name); fall back to the canonical
            # precedence stack (GEMINI_API_KEY env, keyring, config.yaml).
            api_key_env = provider_cfg.get("api_key_env", "GEMINI_API_KEY")
            api_key = os.environ.get(api_key_env) or get_secret(
                "providers.api_keys.gemini", config=config
            )
            if not api_key:
                raise ValueError(
                    f"Gemini API key not set — expected env var {api_key_env!r}, "
                    "keyring entry providers.api_keys.gemini, or config.yaml."
                )
            genai.configure(api_key=api_key)
            # Defer model instantiation to call() so we can use the specified model
            self._api_key = api_key

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
                via response_json_schema. When None, raw text is returned.
            max_tokens: Maximum output tokens. Defaults to 1024.
            timeout: Unused (google-generativeai SDK handles timeouts internally).

        Returns:
            ModelResult with provider="gemini", cost_usd=0.0 (free tier),
            schema_valid=True if output_schema was provided.

        Raises:
            Exception: On any API error (rate limits, validation, etc).
        """
        client = genai.GenerativeModel(model)
        contents = self._build_contents(messages)

        gen_config_kwargs: dict[str, Any] = {
            "max_output_tokens": max_tokens,
        }

        # Set response format for structured output
        if output_schema is not None:
            gen_config_kwargs["response_mime_type"] = "application/json"
            gen_config_kwargs["response_json_schema"] = output_schema

        # Add system instruction
        generation_config = types.GenerationConfig(**gen_config_kwargs)

        response = None
        last_exception = None

        for attempt in range(2):  # Retry once on transient errors
            try:
                response = client.generate_content(
                    contents,
                    generation_config=generation_config,
                    system_instruction=system,
                    stream=False,
                )
                break
            except Exception as exc:
                last_exception = exc
                # Retry on transient errors (rate limits, timeouts)
                # Check for common transient error indicators
                error_str = str(exc).lower()
                if attempt == 0 and (
                    "429" in error_str or "rate" in error_str or "timeout" in error_str
                ):
                    logger.warning(
                        "Gemini transient error on attempt 1, retrying in %.1fs: %s",
                        self._retry_sleep,
                        exc,
                    )
                    time.sleep(self._retry_sleep)
                    continue
                # Non-transient error, raise immediately
                raise

        if response is None:
            raise last_exception or Exception("Gemini API returned no response")

        # Parse response
        if output_schema is not None:
            try:
                data = json.loads(response.text)
            except json.JSONDecodeError as e:
                logger.error("Gemini returned invalid JSON: %s", response.text[:200])
                raise ValueError(f"Invalid JSON from Gemini: {e}") from e
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
