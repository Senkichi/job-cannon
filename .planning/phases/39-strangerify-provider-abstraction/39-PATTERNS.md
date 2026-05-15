# Phase 39: Strangerify Provider Abstraction - Pattern Map

**Mapped:** 2026-05-14
**Files analyzed:** 12 (4 new providers, 4 new tests, 4 modified files)
**Analogs found:** 12 / 12

---

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|---|---|---|---|---|
| `job_finder/web/providers/detection.py` | utility | request-response (probe) | `job_finder/web/providers/ollama_provider.py` `_check_health()` | role-match |
| `job_finder/web/providers/claude_code_cli.py` | provider | request-response (subprocess) | `job_finder/web/claude_client.py` `_run_oneshot()` + `ollama_provider.py` class shape | exact |
| `job_finder/web/providers/gemini_cli.py` | provider | request-response (subprocess) | `job_finder/web/providers/claude_code_cli.py` (parallel structure) | exact |
| `job_finder/web/providers/local_bundled.py` | provider | request-response (in-process) | `job_finder/web/providers/ollama_provider.py` (grammar-constrained JSON) | role-match |
| `tests/test_provider_detection.py` | test | — | `tests/test_ollama_provider.py` (subprocess mock helpers) | exact |
| `tests/test_provider_claude_code_cli.py` | test | — | `tests/test_ollama_provider.py` + `tests/test_anthropic_provider.py` | exact |
| `tests/test_provider_gemini_cli.py` | test | — | `tests/test_provider_claude_code_cli.py` (parallel structure) | exact |
| `tests/test_provider_local_bundled.py` | test | — | `tests/test_ollama_provider.py` (helper pattern + init tests) | role-match |
| `job_finder/web/model_provider.py` (modify) | dispatcher | request-response | itself | exact |
| `job_finder/web/claude_client.py` (modify) | utility | request-response | itself | exact |
| `pyproject.toml` (modify) | config | — | itself — `[project.optional-dependencies]` eval/dev pattern | exact |
| `tests/test_model_provider.py` (modify) | test | — | itself | exact |

---

## Pattern Assignments

### `job_finder/web/providers/detection.py` (utility, request-response probe)

**Analog 1:** `job_finder/web/providers/ollama_provider.py` — `_check_health()` pattern (init-time probe, raises `RuntimeError` on failure)
**Analog 2:** `job_finder/web/claude_client.py` — `_run_oneshot()` subprocess invocation shape

**Module docstring pattern** (matches `ollama_provider.py` lines 1-16 style — brief, phase-tagged, bullet list of behaviors):
```python
"""Provider auto-detection — liveness probes for subscription-leveraged CLIs.

Probes claude, gemini, and ollama in priority order and returns a ranked
list of ProviderHandle instances.  Results are cached for the process
lifetime; pass refresh=True to re-probe (e.g., after the wizard installs
a CLI mid-session).

Detection ordering (CONTEXT.md D-03, memory project_public_release_provider_priority):
  1. claude_code_cli (priority=1) — claude -p "ping" with 10s timeout
  2. gemini_cli     (priority=2) — gemini -p "ping" --output-format json with 10s timeout
  3. ollama         (priority=3) — ollama list with >=2 lines
"""
```

**Imports pattern** (matches `ollama_provider.py` lines 17-26 — minimal, absolute from `job_finder`):
```python
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

import logging

logger = logging.getLogger(__name__)
```

**`ProviderHandle` dataclass** (RESEARCH.md §7; frozen+slots matches `ModelResult` at `model_provider.py` lines 108-118):
```python
@dataclass(frozen=True, slots=True)
class ProviderHandle:
    name: str          # "claude_code_cli" | "gemini_cli" | "ollama"
    binary_path: str   # absolute path from shutil.which()
    cost_label: str    # human-readable for wizard UI
    priority: int      # 1=claude_code_cli, 2=gemini_cli, 3=ollama (lower = preferred)
```

**Module-level cache** (RESEARCH.md §7 — process-lifetime, no TTL):
```python
_detection_cache: dict[str, ProviderHandle | None] = {}
```

**Per-probe function pattern** — each `_check_*()` function follows this exact shape (RESEARCH.md §3 shows the full `_check_gemini_cli()` implementation; replicate for claude and ollama):
```python
def _check_claude_code() -> ProviderHandle | None:
    p = shutil.which("claude")
    if not p:
        return None
    try:
        result = subprocess.run(
            [p, "-p", "ping", "--output-format", "json",
             "--no-session-persistence", "--tools", ""],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None  # auth failure or binary broken
    return ProviderHandle(
        name="claude_code_cli",
        binary_path=p,
        cost_label="$0 (uses your Claude.ai subscription)",
        priority=1,
    )
```

**Gemini probe** uses quota-tolerant logic from RESEARCH.md §3 lines 69-93 — non-zero returncode is allowed if stderr contains quota/rate/capacity/429 keywords (CLI installed but free tier exhausted = available).

**`detect_available_providers()` pattern** (RESEARCH.md §7):
```python
def detect_available_providers(*, refresh: bool = False) -> list[ProviderHandle]:
    if not refresh and _detection_cache:
        return [h for h in _detection_cache.values() if h is not None]
    _detection_cache.clear()
    for check_fn, key in [
        (_check_claude_code, "claude_code_cli"),
        (_check_gemini_cli, "gemini_cli"),
        (_check_ollama, "ollama"),
    ]:
        _detection_cache[key] = check_fn()
    return sorted(
        [h for h in _detection_cache.values() if h is not None],
        key=lambda h: h.priority,
    )
```

**Security invariants (CONTEXT.md D-09):** All `subprocess.run` calls: list-form args, `shutil.which()` binary, no `shell=True`, mandatory `timeout=10`.

---

### `job_finder/web/providers/claude_code_cli.py` (provider, request-response subprocess)

**Primary analog:** `job_finder/web/claude_client.py` `_run_oneshot()` — lines 384-478. `ClaudeCodeCLIProvider.call()` is a `BaseProvider`-conformant wrapper around this existing function. RESEARCH.md §4 confirms delegation is the correct strategy (R-04).

**Secondary analog:** `job_finder/web/providers/ollama_provider.py` — class shape, `__init__` pattern, `call()` signature, `ModelResult` construction.

**Imports pattern** (matches `ollama_provider.py` lines 17-26):
```python
from __future__ import annotations

import json
import logging
import shutil

from job_finder.web.model_provider import BaseProvider, ModelResult

logger = logging.getLogger(__name__)
```

**Class + `__init__` pattern** (binary resolved at init, raises `RuntimeError` on miss — matches `OllamaProvider._check_health()` at `ollama_provider.py` lines 142-159):
```python
class ClaudeCodeCLIProvider(BaseProvider):
    """Provider adapter for Claude via the Claude Code CLI (claude -p).

    Uses the user's Claude.ai subscription — no per-token billing.
    Binary resolved via shutil.which() at constructor time.

    Args:
        config: Application config dict (unused in Phase 39; accepted for
                _make_adapter() consistency).
    """

    def __init__(self, config: dict | None = None) -> None:
        bin_path = shutil.which("claude")
        if bin_path is None:
            raise RuntimeError(
                "claude CLI not found on PATH. "
                "Install: npm install -g @anthropic-ai/claude-code"
            )
        self._bin = bin_path
```

**`call()` signature** — must match `BaseProvider.call()` at `model_provider.py` lines 124-135 exactly:
```python
    def call(
        self,
        model: str,
        system: str,
        messages: list[dict],
        output_schema: dict | None = None,
        max_tokens: int = 1024,
        timeout: float | None = None,
    ) -> ModelResult:
```

**Subprocess invocation** — copy from `claude_client._run_oneshot()` lines 416-455, adapted for `BaseProvider.call()` parameter names. Key flags from RESEARCH.md §4:
```python
        cmd = [
            self._bin,
            "-p",
            "--model", model,
            "--output-format", "json",
            "--no-session-persistence",
            "--tools", "",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--system-prompt", system,
        ]
        if output_schema is not None:
            cmd.extend(["--json-schema", json.dumps(output_schema)])
        # User message via stdin — avoids Windows 8191-char cmd-line limit.
        result = subprocess.run(
            cmd,
            input=messages[-1]["content"],
            capture_output=True, text=True,
            timeout=timeout or 180,
            encoding="utf-8", errors="replace",
            cwd=tmpdir,  # temp dir — no CLAUDE.md pollution
        )
```

**Envelope parsing** (RESEARCH.md §4, JSON envelope shape confirmed):
```python
        envelope = json.loads(result.stdout)
        if envelope.get("is_error"):
            raise RuntimeError(f"claude CLI error: {envelope.get('result', '')[:300]}")
        # Structured output: check structured_output first, then parse result field.
        if output_schema is not None:
            data = envelope.get("structured_output") or json.loads(envelope["result"])
            schema_valid = True
        else:
            try:
                data = json.loads(envelope["result"])
            except json.JSONDecodeError:
                data = {"text": envelope["result"]}
            schema_valid = False
```

**`ModelResult` construction** — cost_usd=0.0, provider="claude_code_cli" (CONTEXT.md D-04, D-07):
```python
        return ModelResult(
            data=data,
            cost_usd=0.0,
            input_tokens=envelope.get("usage", {}).get("input_tokens", 0),
            output_tokens=envelope.get("usage", {}).get("output_tokens", 0),
            model=model,
            provider="claude_code_cli",
            schema_valid=schema_valid,
        )
```

**Error handling** (matches `ollama_provider.py` lines 252-272 — raise so cascade catches `RuntimeError`):
- Exit nonzero → `RuntimeError(f"claude CLI failed (rc={result.returncode}): {stderr[:300]}")`
- `subprocess.TimeoutExpired` → `TimeoutError` (or re-raise; cascade catches `RuntimeError`)
- JSON parse failure → `RuntimeError`

**Do NOT use `--bare` flag** (RESEARCH.md §4 explicitly documents this — forces ANTHROPIC_API_KEY, bypasses subscription).

---

### `job_finder/web/providers/gemini_cli.py` (provider, request-response subprocess)

**Analog:** `job_finder/web/providers/claude_code_cli.py` — parallel structure throughout. All structural patterns identical; only the binary name, command flags, and output parsing differ.

**Imports pattern:** Identical to `claude_code_cli.py`.

**`__init__` pattern:** Same `shutil.which("gemini")` + `RuntimeError` on miss.

**Command shape** (RESEARCH.md §3, confirmed via live spike):
```python
        cmd = [
            self._bin,
            "-p", combined_prompt,
            "--output-format", "json",
            "--model", model,
        ]
        # combined_prompt = system + "\n\n" + messages[-1]["content"] + optional schema block
        # (gemini CLI has no --system-prompt or --json-schema flags)
```

**Schema injection** — gemini CLI has no native `--json-schema` flag; use prompt-injection fallback from CONTEXT.md D-01:
```python
        schema_block = ""
        if output_schema is not None:
            schema_block = (
                "\n\nRespond ONLY with JSON conforming to this schema:\n"
                + json.dumps(output_schema)
            )
        combined_prompt = system + "\n\n" + messages[-1]["content"] + schema_block
```

**Output parsing** (RESEARCH.md §3, R-01 — envelope key is `result`; confirmed by quota-error response shape):
```python
        envelope = json.loads(result.stdout)
        raw = envelope.get("result", "")
        if output_schema is not None:
            data = json.loads(raw)
            schema_valid = True
        else:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {"text": raw}
            schema_valid = False
```

**`ModelResult` construction:** Same as `claude_code_cli.py` but `provider="gemini_cli"`, `cost_usd=0.0`, `input_tokens=0`, `output_tokens=0` (CLI does not expose token counts).

---

### `job_finder/web/providers/local_bundled.py` (provider, request-response in-process)

**Analog:** `job_finder/web/providers/ollama_provider.py` — grammar-constrained JSON path, `call()` signature, `ModelResult` construction. The underlying mechanism is the same (GBNF grammar via llama.cpp) but the API is `llama-cpp-python` instead of the Ollama REST endpoint.

**Secondary analog:** `job_finder/web/providers/gemini_provider.py` lines 17-23 — lazy `try/except ImportError` guard at module top for optional dependency.

**Imports pattern** — lazy import inside `__init__`, NOT module-top (CONTEXT.md D-06, anti-pattern listed in CONTEXT.md §code_context):
```python
from __future__ import annotations

import json
import logging
import os

from job_finder.web.model_provider import BaseProvider, ModelResult

logger = logging.getLogger(__name__)
```

**Class + lazy import `__init__`** (RESEARCH.md §5 — critical that module-level import is absent):
```python
class LocalBundledProvider(BaseProvider):
    """Provider adapter wrapping llama-cpp-python for CPU-local inference.

    Requires the [local-ai] optional extra: uv sync --extra local-ai
    Requires a GGUF model file; default recommendation: Qwen2.5-3B-Instruct-Q4_K_M.

    Args:
        model_path: Absolute path to a GGUF model file.
        n_ctx: Context window size. Defaults to 4096.
    """

    def __init__(self, model_path: str, n_ctx: int = 4096, **_kwargs) -> None:
        try:
            from llama_cpp import Llama  # lazy — only required with [local-ai] installed
        except ImportError as exc:
            raise ImportError(
                "llama-cpp-python is required for local_bundled provider. "
                "Install with: uv sync --extra local-ai"
            ) from exc
        if not model_path:
            raise FileNotFoundError("providers.local_bundled.model_path not configured")
        import pathlib
        if not pathlib.Path(model_path).exists():
            raise FileNotFoundError(f"GGUF model not found: {model_path!r}")
        self._llm = Llama(
            model_path=model_path,
            n_ctx=n_ctx,
            n_threads=max(1, (os.cpu_count() or 2) // 2),
            verbose=False,
        )
        self._model_path = model_path
```

**`call()` pattern** — `create_chat_completion` (NOT `create_completion`) to match the system+messages convention (RESEARCH.md §5):
```python
    def call(
        self,
        model: str,
        system: str,
        messages: list[dict],
        output_schema: dict | None = None,
        max_tokens: int = 1024,
        timeout: float | None = None,
    ) -> ModelResult:
        llm_messages = [{"role": "system", "content": system}] + messages
        kwargs: dict = {"messages": llm_messages, "max_tokens": max_tokens, "temperature": 0.0}
        if output_schema is not None:
            kwargs["response_format"] = {"type": "json_object", "schema": output_schema}
        response = self._llm.create_chat_completion(**kwargs)
        content = response["choices"][0]["message"]["content"]
        if output_schema is not None:
            data = json.loads(content)
            schema_valid = True
        else:
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                data = {"text": content.strip()}
            schema_valid = False
        usage = response.get("usage", {})
        return ModelResult(
            data=data,
            cost_usd=0.0,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            model=self._model_path,   # path IS the model identifier for local files
            provider="local_bundled",
            schema_valid=schema_valid,
        )
```

---

### `tests/test_provider_detection.py` (test)

**Analog:** `tests/test_ollama_provider.py` — helper factory functions, `patch` + `MagicMock`, alphabetical field assertion order.

**Module-level `_mock_run()` helper pattern** (matches `test_ollama_provider.py` lines 36-47 `_make_response()` shape):
```python
from unittest.mock import MagicMock, patch
import pytest
from job_finder.web.providers.detection import ProviderHandle, detect_available_providers

def _mock_run(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m
```

**Cache invalidation between tests** — each test must call `detect_available_providers(refresh=True)` or clear `_detection_cache` via the module reference to avoid inter-test cache pollution:
```python
import job_finder.web.providers.detection as _det_mod

@pytest.fixture(autouse=True)
def clear_detection_cache():
    _det_mod._detection_cache.clear()
    yield
    _det_mod._detection_cache.clear()
```

**Key test assertions** (RESEARCH.md §11 Validation Architecture, STRANGE-PROV-02):
- `shutil.which` returns None → provider absent from result list
- Subprocess returns 0 → provider present with correct `priority`
- Cache hit: `subprocess.run` called once even after two `detect_available_providers()` calls
- `refresh=True` bypasses cache, calls subprocess again
- Results sorted by `priority` (claude_code_cli=1 before gemini_cli=2 before ollama=3)

---

### `tests/test_provider_claude_code_cli.py` (test)

**Analog:** `tests/test_ollama_provider.py` — `_make_provider()` helper, field assertion order, patch target.

**`_mock_subprocess_run()` helper:**
```python
def _mock_run(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m

def _make_provider() -> ClaudeCodeCLIProvider:
    with patch("shutil.which", return_value="/usr/bin/claude"):
        return ClaudeCodeCLIProvider(config={})
```

**Key test assertions** (STRANGE-PROV-03, CONTEXT.md D-08):
- `shutil.which("claude")` returns None → `RuntimeError` at `__init__`
- `call()` passes `input=messages[-1]["content"]` to `subprocess.run`
- Exit nonzero → `RuntimeError` from `call()`
- `is_error=True` in envelope → `RuntimeError`
- `output_schema` present → `cmd` contains `--json-schema` flag
- `output_schema` absent → `data == {"text": ...}` when result is plain text
- `ModelResult.provider == "claude_code_cli"`
- `ModelResult.cost_usd == 0.0`
- `ModelResult.data` is `dict`
- `ModelResult.schema_valid` is `bool`
- `--bare` flag NOT present in any `subprocess.run` call

**Parametrized cross-provider shape test** (RESEARCH.md Validation Architecture, STRANGE-PROV-03/04/05):
```python
@pytest.mark.parametrize("provider_name", [
    "claude_code_cli", "gemini_cli", "local_bundled",
])
def test_call_returns_valid_model_result_shape(provider_name, ...):
    result = provider.call(model, system, messages)
    assert isinstance(result.data, dict)
    assert isinstance(result.cost_usd, float)
    assert isinstance(result.input_tokens, int)
    assert isinstance(result.output_tokens, int)
    assert isinstance(result.model, str)
    assert isinstance(result.provider, str)
    assert isinstance(result.schema_valid, bool)
```

---

### `tests/test_provider_gemini_cli.py` (test)

**Analog:** `tests/test_provider_claude_code_cli.py` — identical structure.

**Differences from claude_code_cli tests:**
- Binary is `"gemini"`, not `"claude"`
- `shutil.which("gemini")` mock path
- Gemini quota-error tolerance: non-zero returncode with `"quota"` in stderr → `ProviderHandle` returned (detection test), but inference `call()` still raises `RuntimeError` on non-zero exit
- No `--json-schema` flag; prompt-injection schema block tested instead
- Envelope parse key is `result`, not `structured_output`
- `ModelResult.provider == "gemini_cli"`

---

### `tests/test_provider_local_bundled.py` (test)

**Analog:** `tests/test_ollama_provider.py` — `_make_provider()` helper with mocked init, field assertion order.

**`pytest.importorskip` pattern at module top** (CONTEXT.md D-08):
```python
llama_cpp = pytest.importorskip(
    "llama_cpp",
    reason="llama-cpp-python not installed; skipping local_bundled tests",
)
```

**Lazy-import behavior test** — must run even without llama_cpp installed (RESEARCH.md §11, STRANGE-PROV-05):
```python
def test_import_local_bundled_module_does_not_crash_without_llama_cpp():
    """Module-level import succeeds; only __init__ call raises ImportError."""
    import importlib
    mod = importlib.import_module("job_finder.web.providers.local_bundled")
    assert hasattr(mod, "LocalBundledProvider")
    with pytest.raises(ImportError, match="llama-cpp-python"):
        mod.LocalBundledProvider(model_path="/fake.gguf")
```

Note: this test should NOT be guarded by `pytest.importorskip` — it must run in environments where `llama_cpp` is absent.

**`_make_provider()` helper with mocked Llama:**
```python
def _make_provider(model_path: str = "/fake/model.gguf") -> LocalBundledProvider:
    with patch("llama_cpp.Llama") as mock_llama_cls, \
         patch("pathlib.Path.exists", return_value=True):
        provider = LocalBundledProvider(model_path=model_path)
    provider._llm = mock_llama_cls.return_value
    return provider
```

**Key assertions:** `FileNotFoundError` on missing GGUF, `data` is `dict`, `cost_usd == 0.0`, `provider == "local_bundled"`.

---

### `job_finder/web/model_provider.py` (modify — 5 distinct changes)

**Analog:** itself — all edits are additive to existing structure.

**Change 1 — Remove `_TIER_DEFAULTS`, add `_VALID_WORKLOADS` + `_PROVIDER_DEFAULTS`** at lines 33-43.
Replace the block with the verbatim dict from RESEARCH.md §6. Also remove the `from job_finder.config import DEFAULT_MODEL_HIGH, DEFAULT_MODEL_LOW, DEFAULT_MODEL_MID` import at line 23 (unused after removal).

**Change 2 — Remove unused import at line 23:**
```python
# DELETE this line:
from job_finder.config import DEFAULT_MODEL_HIGH, DEFAULT_MODEL_LOW, DEFAULT_MODEL_MID
```

**Change 3 — Extend `_SUPPORTED_PROVIDERS`** at lines 206-212 (CONTEXT.md D-07):
```python
_SUPPORTED_PROVIDERS: frozenset[str] = frozenset(
    {
        "anthropic",
        "gemini",
        "ollama",
        "claude_code_cli",  # NEW
        "gemini_cli",       # NEW
        "local_bundled",    # NEW
    }
)
```

**Change 4 — Extend `_make_adapter()` dispatch** at lines 395-449. Add three new `if` branches after the existing `ollama` branch, following the lazy-import pattern at lines 428-429:
```python
    if provider_name == "claude_code_cli":
        from job_finder.web.providers.claude_code_cli import ClaudeCodeCLIProvider
        return ClaudeCodeCLIProvider(config=config)
    if provider_name == "gemini_cli":
        from job_finder.web.providers.gemini_cli import GeminiCLIProvider
        return GeminiCLIProvider(config=config)
    if provider_name == "local_bundled":
        from job_finder.web.providers.local_bundled import LocalBundledProvider
        lp_cfg = (config or {}).get("providers", {}).get("local_bundled", {})
        model_path = lp_cfg.get("model_path", "")
        if not model_path:
            raise ValueError("providers.local_bundled.model_path not configured")
        return LocalBundledProvider(
            model_path=model_path,
            n_ctx=lp_cfg.get("n_ctx", 4096),
        )
```

**Change 5 — Add `ImportError` to cascade exception tuples** (RESEARCH.md §9). Two locations:
- Line 264 (`tier_has_configured_provider`): `except (ValueError, RuntimeError, ImportError):`
- Line 590 (`call_model` cascade): `except (ValueError, RuntimeError, ImportError) as exc:`

**Change 6 — Add legacy-tier translation layer to `resolve_provider_config()`** (CONTEXT.md D-02, RESEARCH.md §6). Insert at the top of `resolve_provider_config()` body (line 138), before `providers_cfg = config.get(...)`:
```python
    # Phase 39: translate legacy tier names to workload labels.
    # Deleted in Phase 40 after all callers are renamed.
    _LEGACY_TIER_MAP: dict[str, str] = {
        "low": "quick", "mid": "score", "high": "score", "scoring": "score",
        "quick": "quick", "score": "score", "triage": "triage",
    }
    workload = _LEGACY_TIER_MAP.get(tier, tier)
```

Then use `workload` (not `tier`) when looking up `_PROVIDER_DEFAULTS[provider_name][workload]` for the default model. The config-key lookup (`providers_cfg.get(tier, {})`) still uses the original `tier` string — config.yaml retains legacy key names until Phase 40.

---

### `job_finder/web/claude_client.py` (modify — one line)

**Analog:** itself — `FREE_PROVIDERS` at lines 61-67.

**Change:** Extend the frozenset (CONTEXT.md D-07, RESEARCH.md §10):
```python
FREE_PROVIDERS: frozenset[str] = frozenset(
    {
        "gemini",
        "ollama",
        "claude_cli",       # existing — internal call_claude() path
        "claude_code_cli",  # NEW — ClaudeCodeCLIProvider
        "gemini_cli",       # NEW — GeminiCLIProvider
        "local_bundled",    # NEW — LocalBundledProvider
    }
)
```

Note: `"groq"` and `"cerebras"` are intentionally NOT added (RESEARCH.md §11 R-05 — out of scope).

---

### `pyproject.toml` (modify — additive)

**Analog:** itself — `[project.optional-dependencies]` block at lines 69-92. Follows the same comment + entry pattern as `eval` and `dev` extras.

**Change:** Add `local-ai` extra after existing extras:
```toml
# CPU-local inference via llama-cpp-python.
# Windows: pre-built wheel available for cp313-win_amd64; no MSVC needed for CPU-only.
# GPU acceleration (CUDA/ROCm/Vulkan) requires platform-specific build flags.
# Default model: Qwen2.5-3B-Instruct-Q4_K_M (~2GB GGUF); wizard-driven download in Phase 42.
local-ai = [
    "llama-cpp-python>=0.2.0",
]
```

---

### `tests/test_model_provider.py` (modify — additive)

**Analog:** itself — existing `resolve_provider_config` tests at lines 84-160. New tests follow the same config-dict construction pattern.

**New test group 1 — `_PROVIDER_DEFAULTS` membership** (RESEARCH.md §11, STRANGE-PROV-01):
```python
from job_finder.web.model_provider import _PROVIDER_DEFAULTS, _VALID_WORKLOADS

def test_provider_defaults_contains_all_providers():
    assert set(_PROVIDER_DEFAULTS) >= {
        "claude_code_cli", "gemini_cli", "ollama",
        "anthropic", "local_bundled", "gemini",
        "groq", "cerebras",
    }

def test_valid_workloads_set():
    assert _VALID_WORKLOADS == frozenset({"quick", "score", "triage"})

def test_provider_defaults_has_no_legacy_tier_keys():
    flat_keys: set[str] = set()
    for mapping in _PROVIDER_DEFAULTS.values():
        flat_keys.update(mapping.keys())
    assert flat_keys.isdisjoint({"low", "mid", "high", "scoring"})
```

**New test group 2 — legacy-tier translation** (RESEARCH.md §11, STRANGE-PROV-01):
```python
def test_resolve_low_tier_uses_quick_workload_model():
    cfg = {"providers": {"anthropic": {"provider": "anthropic", "fallback_chain": []}}}
    result = resolve_provider_config("low", cfg)
    assert result["model"] == _PROVIDER_DEFAULTS["anthropic"]["quick"]

def test_resolve_scoring_tier_uses_score_workload_model():
    cfg = {"providers": {"anthropic": {"provider": "anthropic", "fallback_chain": []}}}
    result = resolve_provider_config("scoring", cfg)
    assert result["model"] == _PROVIDER_DEFAULTS["anthropic"]["score"]

def test_resolve_mid_tier_uses_score_workload_model():
    cfg = {"providers": {"anthropic": {"provider": "anthropic", "fallback_chain": []}}}
    result = resolve_provider_config("mid", cfg)
    assert result["model"] == _PROVIDER_DEFAULTS["anthropic"]["score"]
```

**Existing tests that must NOT break:** `test_resolve_provider_scoring_tier_default`, `test_resolve_provider_high_tier`, `test_resolve_provider_no_providers_section` — the legacy-tier translation layer keeps these passing by routing `"scoring"` → `"score"`, `"high"` → `"score"`, etc.

---

## Shared Patterns

### BaseProvider Contract
**Source:** `job_finder/web/model_provider.py` lines 121-135
**Apply to:** All three new provider classes
```python
@abstractmethod
def call(
    self,
    model: str,
    system: str,
    messages: list[dict],
    output_schema: dict | None = None,
    max_tokens: int = 1024,
    timeout: float | None = None,
) -> ModelResult:
    ...
```

### ModelResult Construction (free providers)
**Source:** `job_finder/web/providers/ollama_provider.py` lines 264-272
**Apply to:** `claude_code_cli`, `gemini_cli`, `local_bundled` — all must return `cost_usd=0.0` and populate all 7 fields
```python
return ModelResult(
    data=data,          # always dict
    cost_usd=0.0,       # free providers
    input_tokens=...,   # int (0 if CLI doesn't expose)
    output_tokens=...,  # int (0 if CLI doesn't expose)
    model=model,        # str — model identifier passed to call()
    provider="<name>",  # str — matches FREE_PROVIDERS key
    schema_valid=True,  # or derived from parse success
)
```

### Lazy Import Pattern (optional deps)
**Source:** `job_finder/web/providers/gemini_provider.py` lines 17-23 (module-top guard) + `job_finder/web/model_provider.py` lines 428-429 (lazy inside function)
**Apply to:** `local_bundled.py` `__init__` must use lazy import; module-top must NOT import `llama_cpp`
```python
# WRONG (module top — crashes without [local-ai]):
from llama_cpp import Llama

# CORRECT (lazy inside __init__):
try:
    from llama_cpp import Llama
except ImportError as exc:
    raise ImportError("Install with: uv sync --extra local-ai") from exc
```

### Subprocess Security Posture
**Source:** `job_finder/web/claude_client.py` lines 416-455
**Apply to:** `claude_code_cli.py`, `gemini_cli.py`, `detection.py` — all `subprocess.run` calls
- List-form args (`[bin, "-p", prompt]`, never string)
- Binary via `shutil.which()`, stored at `__init__` time
- `capture_output=True`, `text=True`, `encoding="utf-8"`, `errors="replace"`
- `timeout=10` for probes, `timeout=180` for inference
- NO `shell=True`
- User content via `input=` kwarg for long prompts (avoids Windows cmd-line limit)

### `_make_adapter()` Lazy Import Pattern
**Source:** `job_finder/web/model_provider.py` lines 427-448
**Apply to:** All three new `if provider_name == ...` branches in `_make_adapter()`
```python
    # Pattern: lazy import to avoid circular import (providers import BaseProvider from here)
    if provider_name == "gemini":
        from job_finder.web.providers.gemini_provider import GeminiProvider
        return GeminiProvider(config=config)
```

### Test Helper Factory Pattern
**Source:** `tests/test_ollama_provider.py` lines 36-68
**Apply to:** All four new test files — define module-level `_mock_run()` and `_make_provider()` helpers
```python
def _make_response(...) -> MagicMock:   # http analog
def _make_chat_response(...) -> MagicMock:
def _make_provider(config=None) -> OllamaProvider:
    with patch("requests.get", return_value=...):
        return OllamaProvider(config=config or {})
```

### Docstring Style
**Source:** `job_finder/web/providers/ollama_provider.py` lines 1-16 and `job_finder/web/providers/anthropic_provider.py` lines 1-9
**Apply to:** All new provider modules — brief module docstring (one paragraph), class docstring (1-2 sentences + Args block), method docstrings (Args + Returns + Raises).

---

## No Analog Found

All files have close analogs. No entries in this section.

---

## Metadata

**Analog search scope:** `job_finder/web/providers/`, `job_finder/web/claude_client.py`, `job_finder/web/model_provider.py`, `tests/test_*provider*.py`, `pyproject.toml`
**Files scanned:** 9 source files, 3 test files
**Pattern extraction date:** 2026-05-14
