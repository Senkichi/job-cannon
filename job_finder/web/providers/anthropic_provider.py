"""Anthropic provider adapter — thin wrapper over the Claude CLI.

Polish-review F2 (2026-05-26) collapsed the historical double-validation
/ double-retry / double-cost-recording layering. AnthropicProvider now
talks directly to ``claude_client._run_oneshot`` (the same transport
``ClaudeCodeCLIProvider`` uses) and lets the cascade layer
(``model_provider._maybe_record_cost`` / ``call_model``) handle cost
recording, budget gating, schema validation, and the schema-failure
retry. Provider attribution flips from ``"claude_cli"`` to
``"anthropic"``; ``"anthropic"`` is added to ``FREE_PROVIDERS`` so the
budget gate continues to treat the CLI-subscription transport as $0
(per CLAUDE.md M-2).

``call_claude`` remains in ``claude_client`` as a back-compat shim for
the small set of legacy direct callers (ai_career_navigator,
careers_scraper, description_reformatter); the cascade no longer routes
through it.

Phase 25 introduced the adapter; F2 (2026-05-26) slimmed it.
"""

from __future__ import annotations

import json
import logging
import sqlite3

from job_finder.web.claude_client import _run_oneshot
from job_finder.web.model_provider import BaseProvider, ModelResult

logger = logging.getLogger(__name__)


class AnthropicProvider(BaseProvider):
    """BaseProvider adapter that shells out to ``claude -p`` via _run_oneshot.

    Mirrors ``ClaudeCodeCLIProvider``'s envelope-parsing exactly: prefer
    the CLI's native ``structured_output`` for schema requests, fall back
    to ``json.loads(envelope["result"])``; freeform requests get the raw
    result wrapped as ``{"text": ...}`` when it isn't already a dict.

    ``conn`` / ``job_id`` / ``purpose`` are accepted for ``_make_adapter``
    signature symmetry but are no longer used here — the cascade layer
    owns cost recording.

    Args:
        conn: Open SQLite connection (unused after F2; kept for the
            ``_make_adapter`` calling convention).
        config: Application config dict (unused after F2; kept for the
            calling convention).
        job_id: Job dedup_key for cost attribution (unused after F2).
        purpose: Feature attribution label (unused after F2).
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        config: dict,
        job_id: str | None = None,
        purpose: str = "",
    ) -> None:
        # All four args retained for _make_adapter symmetry — the cascade
        # constructs every adapter with the same kwargs and would have to
        # gain Anthropic-specific branching otherwise.
        del conn, config, job_id, purpose
        self._timeout_default = 120.0

    def call(
        self,
        model: str,
        system: str,
        messages: list[dict],
        output_schema: dict | None = None,
        max_tokens: int = 1024,
        timeout: float | None = None,
    ) -> ModelResult:
        """Run a single Anthropic CLI dispatch and return a ModelResult.

        Schema validation, retry, and cost recording all live in the
        cascade layer (``call_model`` / ``_maybe_record_cost``);
        ``schema_valid=True`` here is the "no error" telemetry value
        (matches ``ClaudeCodeCLIProvider``).

        Args:
            model: Full model identifier, e.g. ``"claude-haiku-4-5"``.
            system: System prompt string.
            messages: List of message dicts ``[{role, content}]``. Only
                ``messages[-1]["content"]`` is forwarded — Phase 39 simplified
                multi-turn handling out of the CLI dispatch path.
            output_schema: JSON schema dict for structured output (or None).
            max_tokens: Maximum output tokens. Currently unused — the CLI
                does not expose a max-tokens knob; kept for signature
                consistency with the rest of the cascade.
            timeout: Subprocess timeout in seconds. Defaults to 120.

        Returns:
            ModelResult with ``provider="anthropic"`` and real token counts
            from ``envelope["usage"]``.

        Raises:
            BudgetExceededError: Propagated from ``_run_oneshot`` when the
                CLI reports credit-exhaustion.
            RuntimeError: Propagated from ``_run_oneshot`` on non-zero exit
                code or CLI-reported error.
            TimeoutError: Propagated from ``_run_oneshot`` on subprocess
                timeout.
            ValueError: When ``messages`` is empty.
        """
        del max_tokens  # CLI has no max-tokens knob; accepted for symmetry
        if not messages:
            raise ValueError("messages list must contain at least one message")
        user_message = messages[-1].get("content", "")

        envelope = _run_oneshot(
            model=model,
            system=system,
            user_message=user_message,
            json_schema=output_schema,
            timeout=timeout or self._timeout_default,
        )

        # Parse identically to ClaudeCodeCLIProvider for symmetry.
        if output_schema is not None:
            structured = envelope.get("structured_output")
            if structured is not None and isinstance(structured, dict):
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
            # Cost is computed by _maybe_record_cost; for anthropic
            # (now in FREE_PROVIDERS) that resolves to 0.0 regardless of
            # what we return here. Keep this 0.0 so ModelResult.cost_usd
            # matches the row that lands in scoring_costs.
            cost_usd=0.0,
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            model=model,
            provider="anthropic",
            schema_valid=True,
        )
