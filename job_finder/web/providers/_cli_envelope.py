"""Shared envelope parser for CLI providers (AnthropicProvider, ClaudeCodeCLIProvider).

Both adapters dispatch through ``claude_client._run_oneshot`` and parse the
returned envelope identically: prefer the CLI's native ``structured_output``
for schema requests, fall back to ``json.loads(envelope["result"])``;
freeform requests get the raw result wrapped as ``{"text": ...}`` when it
isn't already a dict.

``cost_usd=0.0`` and ``schema_valid=True`` are correct for both adapters:
the transports are subscription/quota-funded ($0 via ``FREE_PROVIDERS``),
and schema validation lives in the cascade layer (``call_model`` /
``_maybe_record_cost``).
"""

from __future__ import annotations

import json

from job_finder.web.model_provider import ModelResult


def parse_oneshot_envelope(
    envelope: dict,
    output_schema: dict | None,
    *,
    model: str,
    provider: str,
) -> ModelResult:
    """Convert a ``_run_oneshot`` envelope into a ``ModelResult``.

    Args:
        envelope: Dict returned by ``claude_client._run_oneshot``. Expected
            keys: ``result`` (str), ``structured_output`` (dict, optional),
            ``usage`` (dict with ``input_tokens`` / ``output_tokens``).
        output_schema: JSON schema dict passed to the CLI, or None for
            freeform requests. Controls which parse path is taken.
        model: Full model identifier (e.g. ``"claude-haiku-4-5"``).
        provider: Provider name used for ModelResult attribution
            (e.g. ``"anthropic"`` or ``"claude_code_cli"``).

    Returns:
        ModelResult with ``cost_usd=0.0`` and ``schema_valid=True``;
        token counts come from ``envelope["usage"]``.
    """
    if output_schema is not None:
        structured = envelope.get("structured_output")
        if isinstance(structured, dict):
            data: dict = structured
        else:
            data = json.loads(envelope["result"])
    else:
        raw = envelope.get("result", "")
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            data = parsed if isinstance(parsed, dict) else {"text": str(raw).strip()}
        except (json.JSONDecodeError, TypeError):
            data = {"text": str(raw).strip()}

    usage = envelope.get("usage") or {}
    return ModelResult(
        data=data,
        cost_usd=0.0,
        input_tokens=int(usage.get("input_tokens", 0) or 0),
        output_tokens=int(usage.get("output_tokens", 0) or 0),
        model=model,
        provider=provider,
        schema_valid=True,
    )
