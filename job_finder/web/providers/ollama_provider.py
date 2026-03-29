"""Ollama provider adapter — local REST API with JSON format.

Phase 25 deliverable — part of the multi-provider routing system.

Uses the Ollama local REST API via the `requests` library. Structured
output is achieved by:
  1. Setting "format": "json" in the request payload (guarantees valid JSON)
  2. Embedding the output_schema in the system prompt as instructions
     (Ollama does not have native schema enforcement, so this is best-effort)

Health check on init prevents silent failures when Ollama is not running.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import requests

from job_finder.web.model_provider import BaseProvider, ModelResult

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "http://localhost:11434"
_DEFAULT_TIMEOUT = 300.0
_HEALTH_CHECK_TIMEOUT = 5.0


class OllamaProvider(BaseProvider):
    """Provider adapter for locally-hosted models via Ollama REST API.

    Verifies Ollama is reachable on initialization via GET /api/tags.
    Raises RuntimeError immediately if the service is unavailable so callers
    know early rather than at inference time.

    All calls are free (cost_usd=0.0) — Ollama runs locally.

    Args:
        config: Application config dict. Reads providers.ollama.base_url
                (default: "http://localhost:11434").
    """

    def __init__(self, config: dict) -> None:
        provider_cfg = config.get("providers", {}).get("ollama", {})
        self._base_url = provider_cfg.get("base_url", _DEFAULT_BASE_URL).rstrip("/")
        self._check_health()

    def _check_health(self) -> None:
        """Verify Ollama service is reachable via GET /api/tags.

        Raises:
            RuntimeError: If Ollama is unreachable for any reason (connection
                refused, timeout, or non-2xx HTTP status).
        """
        try:
            resp = requests.get(
                f"{self._base_url}/api/tags",
                timeout=_HEALTH_CHECK_TIMEOUT,
            )
            resp.raise_for_status()
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
            raise RuntimeError(
                f"Ollama service unreachable at {self._base_url}. "
                f"Start Ollama with 'ollama serve'. Error: {exc}"
            ) from exc

    def call(
        self,
        model: str,
        system: str,
        messages: list[dict],
        output_schema: dict | None = None,
        max_tokens: int = 1024,
        timeout: float | None = None,
    ) -> ModelResult:
        """Make a chat completion call to Ollama /api/chat.

        CRITICAL: "stream" is always False — Ollama sends SSE chunks
        without this, making resp.json() hang or fail.

        Schema is embedded in the system prompt as instructions because
        Ollama does not support native schema enforcement. "format": "json"
        guarantees the response is valid JSON; schema adherence is best-effort.

        Args:
            model: Ollama model tag, e.g. "qwen2.5:32b" or "llama3.1:8b".
            system: System prompt string.
            messages: List of message dicts [{role, content}].
            output_schema: JSON schema dict for structured output (or None).
            max_tokens: Maximum output tokens (mapped to options.num_predict).
            timeout: Request timeout in seconds. Defaults to 120.0.

        Returns:
            ModelResult with cost_usd=0.0, provider="ollama", schema_valid=True.
            Token counts from prompt_eval_count/eval_count (0 if absent).

        Raises:
            requests.HTTPError: On non-2xx response from /api/chat.
            json.JSONDecodeError: If response content is not valid JSON
                (unexpected given format=json, but possible if model misbehaves).
        """
        effective_timeout = timeout if timeout is not None else _DEFAULT_TIMEOUT

        # Embed schema in system prompt when provided
        system_with_schema = system
        if output_schema is not None:
            schema_str = json.dumps(output_schema, indent=2)
            system_with_schema = (
                f"{system}\n\nRespond with valid JSON matching this schema:\n{schema_str}"
            )

        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system_with_schema}] + messages,
            "format": "json",
            "stream": False,
            "options": {"num_predict": max_tokens},
        }

        resp = requests.post(
            f"{self._base_url}/api/chat",
            json=payload,
            timeout=effective_timeout,
        )
        resp.raise_for_status()

        body = resp.json()
        content = body["message"]["content"]
        data = json.loads(content)

        return ModelResult(
            data=data,
            cost_usd=0.0,
            input_tokens=body.get("prompt_eval_count", 0),
            output_tokens=body.get("eval_count", 0),
            model=model,
            provider="ollama",
            schema_valid=True,
        )
