"""oLLM provider adapter — OpenAI-compatible API via ollm-tools server.

Phase 25+ deliverable — part of the multi-provider routing system.

Uses the ollm-tools local server (OpenAI-compatible /v1/chat/completions).
Structured output is achieved by embedding the output_schema in the system
prompt as instructions (same approach as Ollama — no native schema enforcement).

Health check on init prevents silent failures when the server is not running.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import requests

from job_finder.web.model_provider import BaseProvider, ModelResult

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "http://localhost:8000"
_DEFAULT_TIMEOUT = 300.0
_HEALTH_CHECK_TIMEOUT = 5.0


class OllmProvider(BaseProvider):
    """Provider adapter for locally-hosted models via ollm-tools OpenAI-compatible server.

    Verifies the server is reachable on initialization via GET /v1/models.
    Raises RuntimeError immediately if the service is unavailable so callers
    know early rather than at inference time.

    All calls are free (cost_usd=0.0) — oLLM runs locally.

    Args:
        config: Application config dict. Reads providers.ollm.base_url
                (default: "http://localhost:8000").
    """

    def __init__(self, config: dict) -> None:
        provider_cfg = config.get("providers", {}).get("ollm", {})
        self._base_url = provider_cfg.get("base_url", _DEFAULT_BASE_URL).rstrip("/")
        self._check_health()

    def _check_health(self) -> None:
        """Verify ollm-tools server is reachable via GET /v1/models.

        Raises:
            RuntimeError: If the server is unreachable for any reason (connection
                refused, timeout, or non-2xx HTTP status).
        """
        try:
            resp = requests.get(
                f"{self._base_url}/v1/models",
                timeout=_HEALTH_CHECK_TIMEOUT,
            )
            resp.raise_for_status()
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
            raise RuntimeError(
                f"oLLM server unreachable at {self._base_url}. "
                f"Start it with 'ollm-server --model <model>'. Error: {exc}"
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
        """Make a chat completion call to ollm-tools /v1/chat/completions.

        Schema is embedded in the system prompt as instructions because
        ollm-tools does not support native schema enforcement.

        Args:
            model: Model identifier (ignored by server — uses the model loaded
                at startup — but included in the request for compatibility).
            system: System prompt string.
            messages: List of message dicts [{role, content}].
            output_schema: JSON schema dict for structured output (or None).
            max_tokens: Maximum output tokens.
            timeout: Request timeout in seconds. Defaults to 300.0 (oLLM
                inference on consumer GPUs can be slow for large contexts).

        Returns:
            ModelResult with cost_usd=0.0, provider="ollm", schema_valid=True.

        Raises:
            requests.HTTPError: On non-2xx response from /v1/chat/completions.
            json.JSONDecodeError: If response content is not valid JSON.
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
            "stream": False,
        }

        resp = requests.post(
            f"{self._base_url}/v1/chat/completions",
            json=payload,
            timeout=effective_timeout,
        )
        resp.raise_for_status()

        body = resp.json()
        content = body["choices"][0]["message"]["content"]
        data = json.loads(content)

        usage = body.get("usage", {})

        return ModelResult(
            data=data,
            cost_usd=0.0,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            model=model,
            provider="ollm",
            schema_valid=True,
        )
