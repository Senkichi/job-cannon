"""Provider adapter wrapping llama-cpp-python for CPU-local GGUF inference.

Requires the [local-ai] optional extra:
    uv sync --extra local-ai

The `llama_cpp` import is LAZY (inside __init__, NOT at module top). This
means `import job_finder.web.providers.local_bundled` always succeeds — only
instantiation raises ImportError when the extra is not installed. The
_make_adapter() cascade catches ImportError (Plan 01 / RESEARCH.md §9) and
falls through to the next provider.

Windows install: pre-built wheels exist for cp313-win_amd64 on PyPI; no
MSVC required for CPU-only inference. GPU acceleration (CUDA/ROCm/Vulkan)
requires platform-specific build flags and is out of scope for Phase 39.

Default model recommendation: Qwen2.5-3B-Instruct-Q4_K_M (~2GB GGUF).
Phase 39 accepts the path from constructor/config only — the wizard-driven
auto-download lives in Phase 42.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib

from job_finder.web.model_provider import BaseProvider, ModelResult

logger = logging.getLogger(__name__)


class LocalBundledProvider(BaseProvider):
    """BaseProvider adapter that runs a GGUF model in-process via llama-cpp-python.

    Args:
        model_path: Absolute (or working-dir-relative) path to a GGUF file.
        n_ctx: Context window size. Defaults to 4096.
        **_kwargs: Accepted for forward-compat with _make_adapter dispatch;
            currently ignored.

    Raises:
        ImportError: If llama-cpp-python is not installed. Caller should run
            `uv sync --extra local-ai`.
        FileNotFoundError: If model_path is empty or the GGUF file does not
            exist on disk.
    """

    def __init__(self, model_path: str, n_ctx: int = 4096, **_kwargs) -> None:
        try:
            from llama_cpp import Llama  # lazy — only required with [local-ai].
        except ImportError as exc:
            raise ImportError(
                "llama-cpp-python is required for local_bundled provider. "
                "Install with: uv sync --extra local-ai"
            ) from exc

        if not model_path:
            raise FileNotFoundError(
                "providers.local_bundled.model_path not configured"
            )
        if not pathlib.Path(model_path).exists():
            raise FileNotFoundError(f"GGUF model not found: {model_path!r}")

        self._llm = Llama(
            model_path=model_path,
            n_ctx=n_ctx,
            n_threads=max(1, (os.cpu_count() or 2) // 2),
            verbose=False,
        )
        self._model_path = model_path

    def call(
        self,
        model: str,
        system: str,
        messages: list[dict],
        output_schema: dict | None = None,
        max_tokens: int = 1024,
        timeout: float | None = None,
    ) -> ModelResult:
        # `model` arg is accepted for BaseProvider signature parity, but the
        # actual model identity is the loaded GGUF path. We expose
        # self._model_path in the returned ModelResult.model field.
        llm_messages: list[dict] = [{"role": "system", "content": system}] + list(messages)

        kwargs: dict = {
            "messages": llm_messages,
            "max_tokens": max_tokens,
            "temperature": 0.0,
        }
        if output_schema is not None:
            kwargs["response_format"] = {
                "type": "json_object",
                "schema": output_schema,
            }

        response = self._llm.create_chat_completion(**kwargs)
        content = response["choices"][0]["message"]["content"]
        if output_schema is not None:
            data = json.loads(content)
            schema_valid = True
        else:
            try:
                parsed = json.loads(content)
                data = parsed if isinstance(parsed, dict) else {"text": str(content).strip()}
            except (json.JSONDecodeError, TypeError):
                data = {"text": str(content).strip()}
            schema_valid = False

        usage = response.get("usage") or {}
        return ModelResult(
            data=data,
            cost_usd=0.0,
            input_tokens=int(usage.get("prompt_tokens", 0) or 0),
            output_tokens=int(usage.get("completion_tokens", 0) or 0),
            model=self._model_path,
            provider="local_bundled",
            schema_valid=schema_valid,
        )
