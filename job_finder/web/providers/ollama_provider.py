"""Ollama provider adapter — local REST API with grammar-constrained structured output.

v3.0 upgrade (Phase 33 Plan 1): when a JSON schema dict is passed via
output_schema, it is forwarded directly to Ollama's `format=` parameter
(llama.cpp GBNF grammar enforcement). The legacy `format='json'` path
remains for backward compatibility but all v3.0 scoring calls use the
schema-dict path.

Default inference options are deterministic-and-reproducible (temperature=0,
seed=42, num_ctx=8192, top_p=0.9, num_predict from max_tokens,
repeat_penalty=1.05). Callers may override any option via the `options=`
kwarg without leaking state across calls.

Health check on init prevents silent failures when Ollama is not running.
"""

from __future__ import annotations

import json
import logging

import requests

from job_finder.web.model_provider import BaseProvider, ModelResult

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "http://localhost:11434"
_DEFAULT_TIMEOUT = 300.0
_HEALTH_CHECK_TIMEOUT = 2.0


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
        options: dict | None = None,
    ) -> ModelResult:
        """Make a chat completion call to Ollama /api/chat.

        CRITICAL: "stream" is always False — Ollama sends SSE chunks
        without this, making resp.json() hang or fail.

        Structured output strategy (v3.0):
          * When output_schema is a dict, it is forwarded unchanged via
            payload["format"] = <schema>. Ollama v0.5+ compiles the schema
            into a GBNF grammar (llama.cpp path), enforcing field names at
            the token level. No schema-to-field-instructions injection is
            needed — the prompt stays clean.
          * When output_schema is None, payload["format"] = "json" (legacy
            string path). This preserves backward compat for existing callers
            (haiku_scorer, sonnet_evaluator, enrich_job, etc.) that don't yet
            pass a schema dict.

        Default inference options are deterministic-and-reproducible:
            temperature=0, seed=42, num_ctx=8192, top_p=0.9,
            num_predict=max_tokens, repeat_penalty=1.05

        Args:
            model: Ollama model tag, e.g. "qwen2.5:32b" or "llama3.1:8b".
            system: System prompt string.
            messages: List of message dicts [{role, content}].
            output_schema: JSON schema dict for structured output (or None).
                When a dict, forwarded via payload.format (grammar-enforced).
                When None, payload.format = "json" (JSON-shape-only enforcement).
            max_tokens: Maximum output tokens (mapped to options.num_predict).
            timeout: Request timeout in seconds. Defaults to 300.0.
            options: Per-call overrides for any inference parameter. Merged
                INTO the deterministic defaults: caller-specified keys win,
                unspecified keys retain defaults. No state leaks across calls
                (a fresh dict is built per call). Example use:
                    options={"temperature": 0.8, "top_p": 1.0}

        Returns:
            ModelResult with cost_usd=0.0, provider="ollama", schema_valid=True.
            Token counts from prompt_eval_count/eval_count (0 if absent).

        Raises:
            requests.HTTPError: On non-2xx response from /api/chat.
            json.JSONDecodeError: If response content is not valid JSON
                (unexpected given format=json/schema, but possible if the
                model misbehaves).
        """
        effective_timeout = timeout if timeout is not None else _DEFAULT_TIMEOUT

        # Format handling — v3.0 schema-dict path vs legacy 'json' string path.
        # v3.0: forward schema dict unchanged. llama.cpp GBNF grammar enforces
        # field names at the token level — system prompt stays clean.
        # Legacy: format='json' string tells Ollama to produce valid JSON shape
        # only. If the caller needs field-name guidance, it must live in their
        # own system prompt (or upgrade to the schema-dict path).
        format_param: dict | str = output_schema if isinstance(output_schema, dict) else "json"

        # Deterministic default inference options. Built fresh every call —
        # no instance state, no cross-call leak. Per-call overrides merge in
        # via {**defaults, **overrides} so caller keys win.
        default_options = {
            "num_predict": max_tokens,
            "temperature": 0,
            "seed": 42,
            "num_ctx": 8192,
            "top_p": 0.9,
            "repeat_penalty": 1.05,
        }
        effective_options = {**default_options, **(options or {})}

        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system}] + messages,
            "format": format_param,
            "stream": False,
            "options": effective_options,
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
