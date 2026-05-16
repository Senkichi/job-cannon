"""Provider adapter for the local `gemini` CLI (Gemini headless mode).

Confirmed CLI invocation (RESEARCH.md §3 live spike):
    gemini -p "<prompt>" --output-format json --model <model>

The CLI has no native --json-schema flag, so structured-output requests are
enforced via prompt-injection: a "Respond ONLY with JSON conforming to this
schema: ..." block is appended to the prompt when output_schema is not None.

cost_usd is always 0.0 (uses the user's Google AI Studio free tier; the
cascade falls through on 429/quota exhaustion). Membership in
claude_client.FREE_PROVIDERS (Plan 02 Task 3) ensures _maybe_record_cost
writes 0.0 regardless.

Phase 39 simplification: only messages[-1]["content"] is forwarded; multi-
turn history is not supported. The current callers (model_provider
.call_model) build single-turn prompts only.

Security invariants (CONTEXT.md D-09):
    - subprocess.run uses list-form argv, never shell=True
    - binary path resolved via shutil.which("gemini") at __init__
    - explicit timeout=180s on every subprocess.run call
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess

from job_finder.web.model_provider import BaseProvider, ModelResult

logger = logging.getLogger(__name__)


class GeminiCLIProvider(BaseProvider):
    """BaseProvider adapter that shells out to `gemini -p` headlessly.

    Args:
        config: Application config dict. Currently unused; accepted for
            _make_adapter() consistency.

    Raises:
        RuntimeError: If `gemini` is not on PATH at construction time.
    """

    def __init__(self, config: dict | None = None) -> None:
        bin_path = shutil.which("gemini")
        if bin_path is None:
            raise RuntimeError(
                "gemini CLI not found on PATH. "
                "Install: npm install -g @google/generative-ai-cli"
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

        # Combine system + last user message + optional schema block.
        user_message = messages[-1].get("content", "")
        schema_block = ""
        if output_schema is not None:
            schema_block = (
                "\n\nRespond ONLY with JSON conforming to this schema:\n"
                + json.dumps(output_schema)
            )
        combined_prompt = f"{system}\n\n{user_message}{schema_block}"

        cmd: list[str] = [
            self._bin,
            "-p", combined_prompt,
            "--output-format", "json",
            "--model", model,
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout or 180.0,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                f"gemini CLI timed out after {timeout or 180.0}s (model={model})"
            ) from exc
        except FileNotFoundError as exc:
            # Binary disappeared between __init__ and call. Treat as RuntimeError
            # so the cascade falls through.
            raise RuntimeError(
                "gemini CLI not found at invocation time (PATH changed?)"
            ) from exc

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()[:300] or "unknown error"
            raise RuntimeError(f"gemini CLI failed (rc={result.returncode}): {stderr}")

        try:
            envelope = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Invalid JSON from gemini CLI: {(result.stdout or '')[:300]}"
            ) from exc

        raw = envelope.get("result", "")
        if output_schema is not None:
            try:
                data = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"gemini CLI returned non-JSON despite schema request: {str(raw)[:300]}"
                ) from exc
            if not isinstance(data, dict):
                raise RuntimeError(
                    f"gemini CLI schema response is not a dict: {type(data).__name__}"
                )
            schema_valid = True
        else:
            if isinstance(raw, dict):
                data = raw
            else:
                try:
                    parsed = json.loads(raw)
                    data = parsed if isinstance(parsed, dict) else {"text": str(raw).strip()}
                except (json.JSONDecodeError, TypeError):
                    data = {"text": str(raw).strip()}
            schema_valid = False

        return ModelResult(
            data=data,
            cost_usd=0.0,
            input_tokens=0,   # gemini CLI does not expose token counts
            output_tokens=0,
            model=model,
            provider="gemini_cli",
            schema_valid=schema_valid,
        )
