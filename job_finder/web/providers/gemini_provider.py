"""Gemini provider adapter -- google-genai SDK (v1+).

Ports the provider from the legacy google-generativeai SDK
(google.generativeai) to the shipped google-genai>=1.0.0 SDK
(google.genai).  The old SDK is not installed in this project; its
import google.generativeai raised ModuleNotFoundError at construction
time, silently killing the provider in the cascade.

API used:
- genai.Client(api_key=...)  -- pure-local construction, no network call.
- client.models.generate_content(model, contents, config)
- types.GenerateContentConfig(system_instruction, max_output_tokens,
  response_mime_type, response_json_schema)
- response.usage_metadata.prompt_token_count /
  response.usage_metadata.candidates_token_count
- errors.APIError -- base class for all SDK-level API errors.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

try:
    from google import genai
    from google.genai import errors as genai_errors
    from google.genai import types as genai_types

    _GENAI_AVAILABLE = True
except ImportError:
    _GENAI_AVAILABLE = False

from job_finder.secrets import get_secret
from job_finder.web.model_provider import BaseProvider, ModelResult

logger = logging.getLogger(__name__)


class GeminiProvider(BaseProvider):
    """Provider adapter for Google Gemini via the google-genai SDK (v1+).

    Uses response_json_schema inside GenerateContentConfig for structured
    output.  Automatically retries once on transient errors (HTTP 429 /
    rate-limit / timeout) with a configurable sleep duration (default 15 s
    for the free-tier 5 RPM limit).

    Args:
        config: Application config dict. Reads providers.gemini.*.
        client: Optional pre-built genai.Client for testing.
                When provided, skips API key resolution entirely.

    Raises:
        ImportError: When google-genai is not installed.
        ValueError: When no Gemini API key can be resolved.
    """

    def __init__(self, config: dict, *, client: Any | None = None) -> None:
        if not _GENAI_AVAILABLE:
            raise ImportError(
                "google-genai is required for GeminiProvider. "
                "Install with: pip install google-genai>=1.0.0"
            )
        provider_cfg = config.get("providers", {}).get("gemini", {})
        self._retry_sleep: float = provider_cfg.get("retry_sleep_seconds", 15.0)

        if client is not None:
            self._client: Any = client
        else:
            # Honor the custom api_key_env override first; fall back to the
            # canonical precedence stack (env var -> keyring -> config.yaml).
            api_key_env = provider_cfg.get("api_key_env", "GEMINI_API_KEY")
            api_key = os.environ.get(api_key_env) or get_secret(
                "providers.api_keys.gemini", config=config
            )
            if not api_key:
                raise ValueError(
                    f"Gemini API key not set -- expected env var {api_key_env!r}, "
                    "keyring entry providers.api_keys.gemini, or config.yaml."
                )
            # Client construction is pure-local; no network call at this point.
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
            model: Gemini model identifier, e.g. "gemini-2.5-flash".
            system: System prompt (passed as system_instruction).
            messages: List of message dicts [{role, content}].
            output_schema: JSON schema dict for structured output. When
                provided, sets response_mime_type="application/json" and
                passes the schema via response_json_schema.  When None,
                raw text is returned wrapped as {"text": ...}.
            max_tokens: Maximum output tokens. Defaults to 1024.
            timeout: Unused -- the SDK handles timeouts internally.

        Returns:
            ModelResult with provider="gemini", cost_usd=0.0 (Gemini is in
            FREE_PROVIDERS), and schema_valid=True.

        Raises:
            ValueError: If the response body is not valid JSON when
                output_schema is provided.
            google.genai.errors.APIError: On non-transient API errors.
        """
        contents = self._build_contents(messages)

        config_kwargs: dict[str, Any] = {
            "system_instruction": system,
            "max_output_tokens": max_tokens,
        }
        if output_schema is not None:
            config_kwargs["response_mime_type"] = "application/json"
            config_kwargs["response_json_schema"] = output_schema

        gen_config = genai_types.GenerateContentConfig(**config_kwargs)

        response = None
        last_exception: Exception | None = None

        for attempt in range(2):  # one retry on transient errors
            try:
                response = self._client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=gen_config,
                )
                break
            except Exception as exc:
                last_exception = exc
                error_str = str(exc).lower()
                is_rate_limit = (
                    isinstance(exc, genai_errors.APIError) and getattr(exc, "code", 0) == 429
                )
                is_transient = (
                    is_rate_limit
                    or "429" in error_str
                    or "rate" in error_str
                    or "timeout" in error_str
                )
                if attempt == 0 and is_transient:
                    logger.warning(
                        "Gemini transient error on attempt 1, retrying in %.1fs: %s",
                        self._retry_sleep,
                        exc,
                    )
                    time.sleep(self._retry_sleep)
                    continue
                raise

        if response is None:
            raise last_exception or Exception("Gemini API returned no response")

        if output_schema is not None:
            try:
                data = json.loads(response.text)
            except json.JSONDecodeError as exc:
                logger.error("Gemini returned invalid JSON: %s", (response.text or "")[:200])
                raise ValueError(f"Invalid JSON from Gemini: {exc}") from exc
        else:
            data = {"text": response.text}

        usage = response.usage_metadata
        input_tokens = (usage.prompt_token_count or 0) if usage else 0
        output_tokens = (usage.candidates_token_count or 0) if usage else 0

        return ModelResult(
            data=data,
            cost_usd=0.0,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model,
            provider="gemini",
            schema_valid=True,
        )

    def _build_contents(self, messages: list[dict]) -> list[genai_types.Content]:
        """Translate adapter-style messages to genai.types.Content objects.

        The new SDK requires role to be "user" or "model" (not
        "assistant").  Map "assistant" -> "model" for callers that pass
        OpenAI-style role names.
        """
        role_map = {"assistant": "model"}
        return [
            genai_types.Content(
                role=role_map.get(msg.get("role", "user"), msg.get("role", "user")),
                parts=[genai_types.Part.from_text(text=msg["content"])],
            )
            for msg in messages
        ]
