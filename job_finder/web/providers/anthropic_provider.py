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

Issue 303 (2026-06-10): when ``ANTHROPIC_API_KEY`` / ``JF_ANTHROPIC_API_KEY``
is present the ``claude -p`` CLI bills against the API key (per-token).
``_make_adapter`` now passes ``provider_name="anthropic_api"`` in that case;
``"anthropic_api"`` is NOT in ``FREE_PROVIDERS``, so ``cost_gate`` and the
daily budget cap apply.  Subscription-only OAuth login (no API key) keeps
``provider_name="anthropic"`` (FREE_PROVIDERS member, $0).

``call_claude`` remains in ``claude_client`` as a back-compat shim for
the small set of legacy direct callers (ai_career_navigator,
careers_scraper, description_reformatter); the cascade no longer routes
through it.

Phase 25 introduced the adapter; F2 (2026-05-26) slimmed it;
Issue 303 (2026-06-10) added transport-mode attribution.
"""

from __future__ import annotations

import dataclasses
import logging

from job_finder.web.claude_client import _run_oneshot, compute_cost
from job_finder.web.model_provider import BaseProvider, ModelResult
from job_finder.web.providers._cli_envelope import parse_oneshot_envelope

logger = logging.getLogger(__name__)

# Provider name constants — used by _make_adapter and tests.
# "anthropic"     → subscription OAuth transport (FREE_PROVIDERS member, $0).
# "anthropic_api" → API-key transport (NOT in FREE_PROVIDERS, billed per token).
ANTHROPIC_SUBSCRIPTION_PROVIDER = "anthropic"
ANTHROPIC_API_KEY_PROVIDER = "anthropic_api"


class AnthropicProvider(BaseProvider):
    """BaseProvider adapter that shells out to ``claude -p`` via _run_oneshot.

    Mirrors ``ClaudeCodeCLIProvider``'s envelope-parsing exactly: prefer
    the CLI's native ``structured_output`` for schema requests, fall back
    to ``json.loads(envelope["result"])``; freeform requests get the raw
    result wrapped as ``{"text": ...}`` when it isn't already a dict.

    F2 (commit c8e698d) collapsed the historical double-layer; U4 dropped
    the vestigial constructor parameters (conn / config / job_id / purpose)
    that F2 had kept for "symmetry". Issue 303 (2026-06-10) re-introduces
    a single ``provider_name`` constructor arg to distinguish transport mode:
    - ``"anthropic_api"`` (set when API key is present) — billed per token,
      NOT in FREE_PROVIDERS; cost_gate and budget accounting apply.
    - ``"anthropic"``     (default; when subscription OAuth only) — FREE_PROVIDERS
      member, $0, no budget gate.
    ``_make_adapter`` selects the correct name via ``is_anthropic_api_key_transport()``.
    """

    def __init__(self, provider_name: str = ANTHROPIC_SUBSCRIPTION_PROVIDER) -> None:
        self._timeout_default = 120.0
        self._provider_name = provider_name

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
            ModelResult with ``provider`` set to ``self._provider_name``
            (either ``"anthropic"`` for subscription transport or
            ``"anthropic_api"`` for API-key transport) and real token counts
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
        envelope = _run_oneshot(
            model=model,
            system=system,
            user_message=messages[-1].get("content", ""),
            json_schema=output_schema,
            timeout=timeout or self._timeout_default,
        )
        result = parse_oneshot_envelope(
            envelope, output_schema, model=model, provider=self._provider_name
        )
        # For API-key transport the CLI bills per token — compute real cost here
        # so _maybe_record_cost (in the cascade layer) records the true spend.
        # Subscription transport ("anthropic") is $0 and stays with cost_usd=0.0.
        if self._provider_name == ANTHROPIC_API_KEY_PROVIDER:
            real_cost = compute_cost(model, result.input_tokens, result.output_tokens)
            result = dataclasses.replace(result, cost_usd=real_cost)
        return result
