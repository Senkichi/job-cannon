"""Provider adapter for Claude via the Claude Code CLI (`claude -p`).

Uses the user's Claude.ai subscription — cost_usd is always 0.0 on the
cost-tracking path because the CLI bills against the subscription, not
per-token. Membership in claude_client.FREE_PROVIDERS ensures
`_maybe_record_cost()` writes 0.0 regardless of the envelope's
`total_cost_usd` field.

Phase 39 delegates the actual subprocess invocation to
`claude_client._run_oneshot()` to avoid duplicating ~90 lines of
subprocess + JSON-envelope parsing logic (CONTEXT.md D-04 + RESEARCH.md
§4 R-04 recommendation).

Phase 39 simplification: only `messages[-1]["content"]` is forwarded to
the CLI; multi-turn message history is not supported. The current
callers (model_provider.call_model) build single-turn prompts only.
"""

from __future__ import annotations

import json
import logging
import shutil

from job_finder.web.claude_client import _run_oneshot
from job_finder.web.model_provider import BaseProvider, ModelResult

logger = logging.getLogger(__name__)


class ClaudeCodeCLIProvider(BaseProvider):
    """BaseProvider adapter that shells out to `claude -p` headlessly.

    Args:
        config: Application config dict. Currently unused; accepted for
            _make_adapter() consistency. Phase 40 may use it for
            per-provider timeout overrides.

    Raises:
        RuntimeError: If `claude` is not on PATH at construction time.
    """

    def __init__(self, config: dict | None = None) -> None:
        bin_path = shutil.which("claude")
        if bin_path is None:
            raise RuntimeError(
                "claude CLI not found on PATH. "
                "Install: npm install -g @anthropic-ai/claude-code"
            )
        self._bin = bin_path

    def call(
        self,
        model: str,
        system: str,
        messages: list[dict],
        output_schema: dict | None = None,
        max_tokens: int = 1024,
        timeout: float | None = None,
    ) -> ModelResult:
        if not messages:
            raise ValueError("messages list must contain at least one message")
        user_message = messages[-1].get("content", "")

        envelope = _run_oneshot(
            model=model,
            system=system,
            user_message=user_message,
            json_schema=output_schema,
            timeout=timeout or 180.0,
        )

        # _run_oneshot already raises on is_error/credit-exhaustion, so
        # by this point envelope is a successful response.
        if output_schema is not None:
            # Prefer the CLI's native structured_output (Sonnet/Haiku JSON
            # mode); fall back to parsing the result string.
            structured = envelope.get("structured_output")
            if structured is not None and isinstance(structured, dict):
                data: dict = structured
            else:
                data = json.loads(envelope["result"])
            schema_valid = True
        else:
            # Freeform path: try JSON parse; on failure return {"text": ...}.
            raw = envelope.get("result", "")
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else raw
                data = parsed if isinstance(parsed, dict) else {"text": str(raw).strip()}
            except (json.JSONDecodeError, TypeError):
                data = {"text": str(raw).strip()}
            schema_valid = False

        usage = envelope.get("usage") or {}
        return ModelResult(
            data=data,
            cost_usd=0.0,
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            model=model,
            provider="claude_code_cli",
            schema_valid=schema_valid,
        )
