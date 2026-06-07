"""Cerebras provider adapter — OpenAI-compatible chat completions.

Cerebras hosts open-weight models on its custom silicon via an OpenAI-compatible
REST API at https://api.cerebras.ai/v1. Default models are configured via
``_PROVIDER_DEFAULTS`` in ``model_provider.py`` (quick: ``llama3.1-8b``,
score: ``llama-3.3-70b``).

Phase 153 deliverable: re-implements the Cerebras adapter (previously built in
commit 2585f43, deleted in 38ea791) using the openrouter_provider template.
"""

from __future__ import annotations

import json
import logging

import requests

from job_finder.secrets import get_secret
from job_finder.web.model_provider import BaseProvider, ModelResult

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.cerebras.ai/v1"
_DEFAULT_TIMEOUT = 300.0


class CerebrasProvider(BaseProvider):
    """Provider adapter for Cerebras API (OpenAI-compatible).

    Resolves the API key via the standard secret-precedence stack
    (``CEREBRAS_API_KEY`` env var → OS keyring → ``providers.api_keys.cerebras``
    in config.yaml). Raises ``ValueError`` immediately when no key is found
    so the cascade can skip the provider gracefully.

    Reports ``cost_usd=0.0``; Cerebras has both free and paid tiers but the
    provider is intentionally kept out of ``FREE_PROVIDERS`` — callers that
    add real cost tracking later can update this field without changing the
    gating logic.

    Args:
        config: Application config dict.

    Raises:
        ValueError: If no Cerebras API key is available.
    """

    def __init__(self, config: dict) -> None:
        self._api_key = get_secret("providers.api_keys.cerebras", config=config)
        if not self._api_key:
            raise ValueError(
                "Cerebras API key not set. Set CEREBRAS_API_KEY env var, "
                "store it in the OS keyring, or add providers.api_keys.cerebras "
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
        """Make a chat completion call to Cerebras /v1/chat/completions.

        Args:
            model: Cerebras model identifier, e.g. ``"llama3.1-8b"``.
            system: System prompt string.
            messages: List of message dicts ``[{role, content}]``.
            output_schema: JSON schema dict for structured output (or None).
                When provided, requests JSON output via
                ``response_format={"type": "json_object"}``.  Cerebras does not
                enforce the schema at the token level — application-side
                validation still applies.
            max_tokens: Maximum output tokens.  Defaults to 1024.
            timeout: Request timeout in seconds.  Defaults to 300.0.

        Returns:
            ``ModelResult`` with ``provider="cerebras"``, ``cost_usd=0.0``,
            and ``schema_valid=True``.

        Raises:
            requests.HTTPError: On non-2xx response from the Cerebras API.
            json.JSONDecodeError: If the response content is not valid JSON.
        """
        effective_timeout = timeout if timeout is not None else _DEFAULT_TIMEOUT

        payload: dict = {
            "model": model,
            "messages": [{"role": "system", "content": system}] + messages,
            "temperature": 0,
            "max_tokens": max_tokens,
        }

        if output_schema is not None:
            payload["response_format"] = {"type": "json_object"}

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

        body = resp.json()
        content = body["choices"][0]["message"]["content"]
        data = json.loads(content)

        usage = body.get("usage", {})
        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)

        return ModelResult(
            data=data,
            cost_usd=0.0,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model,
            provider="cerebras",
            schema_valid=True,
        )
