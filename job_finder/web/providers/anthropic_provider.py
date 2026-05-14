"""Anthropic provider adapter.

Wraps the existing call_claude() function as a thin facade implementing
BaseProvider.  All budget gating, cost recording, and token extraction
are handled internally by call_claude() — this adapter does NOT call
cost_gate() or record_cost() directly.

Phase 25 deliverable — part of the multi-provider routing system.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from job_finder.web.claude_client import call_claude
from job_finder.web.model_provider import BaseProvider, ModelResult

logger = logging.getLogger(__name__)


class AnthropicProvider(BaseProvider):
    """Provider adapter that delegates to call_claude().

    Wraps the existing Anthropic client + call_claude() path so it can
    participate in the generic provider dispatch interface.  Token counts
    are not re-exposed (call_claude records them internally to scoring_costs).

    Args:
        client: Anthropic API client instance.
        conn: Open SQLite connection for cost recording.
        config: Application config dict.
        job_id: Job dedup_key for cost attribution (nullable).
        purpose: Feature attribution label for cost rows.
    """

    def __init__(
        self,
        client: Any,
        conn: sqlite3.Connection,
        config: dict,
        job_id: str | None = None,
        purpose: str = "",
    ) -> None:
        self._client = client
        self._conn = conn
        self._config = config
        self._job_id = job_id
        self._purpose = purpose

    def call(
        self,
        model: str,
        system: str,
        messages: list[dict],
        output_schema: dict | None = None,
        max_tokens: int = 1024,
        timeout: float | None = None,
    ) -> ModelResult:
        """Delegate to call_claude() and return a ModelResult.

        Budget gating, cost recording, and schema enforcement via tool-choice
        are all handled inside call_claude().  BudgetExceededError propagates
        unchanged — callers must decide how to handle it.

        Args:
            model: Full model identifier, e.g. "claude-haiku-4-5".
            system: System prompt string.
            messages: List of message dicts [{role, content}].
            output_schema: JSON schema dict for structured output (or None).
            max_tokens: Maximum output tokens. Defaults to 1024.
            timeout: Request timeout in seconds.

        Returns:
            ModelResult with provider="anthropic", schema_valid reflecting actual validation outcome.
            input_tokens and output_tokens are 0 — call_claude records
            them internally to scoring_costs.

        Raises:
            BudgetExceededError: Propagated from call_claude() when budget cap hit.
            RuntimeError: Propagated from call_claude() on API errors.
        """
        data, cost_usd, schema_valid = call_claude(
            client=self._client,
            model=model,
            system=system,
            messages=messages,
            output_schema=output_schema,
            conn=self._conn,
            job_id=self._job_id,
            purpose=self._purpose,
            config=self._config,
            max_tokens=max_tokens,
            timeout=timeout,
        )
        return ModelResult(
            data=data,
            cost_usd=cost_usd,
            input_tokens=0,  # call_claude records internally; not re-exposed
            output_tokens=0,
            model=model,
            provider="anthropic",
            schema_valid=schema_valid,  # Use actual validation outcome from call_claude
        )
