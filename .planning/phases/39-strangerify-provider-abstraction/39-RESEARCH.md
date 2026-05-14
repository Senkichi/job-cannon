# Phase 39: Strangerify Provider Abstraction — Research

**Researched:** 2026-05-14
**Domain:** Multi-provider LLM abstraction, CLI subprocess dispatch, llama-cpp-python
**Confidence:** HIGH (all spike targets resolved via live tool execution)

---

## 1. Phase Summary

Phase 39 delivers three new provider classes (`claude_code_cli`, `gemini_cli`, `local_bundled`), an
auto-detection module (`providers/detection.py`) that ranks subscription-leveraged CLIs above paid
options, and replaces the flat `_TIER_DEFAULTS` dict in `model_provider.py` with a nested
`_PROVIDER_DEFAULTS` dict — while adding a temporary legacy-tier translation layer so no existing
caller breaks. Phase 40 owns the cascade rewire (caller renames, config schema rewrite, triage gate,
canary). Phase 39 is purely additive + one internal dict swap.

---

## 2. Locked Decisions Recap

- **D-01** — New providers implement `BaseProvider.call(model, system, messages, output_schema, max_tokens, timeout) -> ModelResult(data: dict, cost_usd, input_tokens, output_tokens, model, provider, schema_valid)`. The PLAN-P1.md `.generate()` skeleton is stale; do NOT use it.
- **D-02** — `_PROVIDER_DEFAULTS` nested dict replaces `_TIER_DEFAULTS`; legacy tier names (`low/mid/high/scoring`) removed from the defaults dict only; `resolve_provider_config()` adds a translation layer (Phase 40 deletes it).
- **D-03** — Liveness probes: `claude -p "ping"` (10s timeout, result cached); `gemini -p "ping"` (10s timeout, confirmed headless flag exists — see §3); `ollama list` ≥2 lines.
- **D-04** — `claude_code_cli`: binary resolved via `shutil.which("claude")`; command `[bin, "-p", "--output-format", "json", "--no-session-persistence", "--tools", "", ...]`; timeout 180s; exit nonzero raises `RuntimeError`.
- **D-05** — `gemini_cli`: binary resolved via `shutil.which("gemini")`; command `[bin, "-p", "<prompt>", "--output-format", "json"]`; spike **resolved** — see §3.
- **D-06** — `local_bundled`: constructor `LocalBundledProvider(model_path: str, n_ctx: int = 4096, **_kwargs)`; lazy `from llama_cpp import Llama` inside `__init__`; raise `FileNotFoundError` on missing GGUF.
- **D-07** — `FREE_PROVIDERS` in `claude_client.py` and `_SUPPORTED_PROVIDERS` in `model_provider.py` both extended with the three new provider names.
- **D-08** — New test files: `test_provider_detection.py`, `test_provider_claude_code_cli.py`, `test_provider_gemini_cli.py`, `test_provider_local_bundled.py`. Modified: `test_model_provider.py` (membership + translation assertions).
- **D-09** — All `subprocess.run` calls: list-form args, `shutil.which()` binary, mandatory timeouts (10s probe / 180s inference), no `shell=True`.

---

## 3. Gemini CLI Invocation Spike Resolution

### Spike result: CONFIRMED headless mode exists

**Version on this machine:** `gemini 0.42.0` [VERIFIED: `gemini --version`]

**Flag:** `-p` / `--prompt` — "Run in non-interactive (headless) mode with the given prompt." [VERIFIED: `gemini --help` output]

**Full help excerpt:**
```
-p, --prompt    Run in non-interactive (headless) mode with the given prompt.
                Appended to input on stdin (if any).  [string]
-m, --model     Model  [string]
-o, --output-format  The format of the CLI output.
                [string] [choices: "text", "json", "stream-json"]
```

**Confirmed command shape for liveness probe:**
```bash
[bin, "-p", "ping", "--output-format", "json"]
```
With `timeout=10` seconds.

**Confirmed command shape for inference:**
```bash
[bin, "-p", <combined_prompt>, "--output-format", "json", "--model", <model>]
```
With `timeout=180` seconds.

**Live test result:** `gemini -p "ping" --output-format json` executed successfully — the CLI responded with a 429 `QUOTA_EXHAUSTED` error (Google AI Studio rate limit exhausted), confirming the headless mode works. The error was from the API, not from an unrecognized flag. [VERIFIED: live subprocess execution]

**Liveness probe strategy (D-03 resolved):** Use `gemini -p "ping" --output-format json` with 10s timeout. A non-zero exit code OR a `QUOTA_EXHAUSTED` / `TerminalQuotaError` in stderr means the CLI is present but quota-exhausted — treat as available (the CLI binary works, just the free tier is rate-limited). A `FileNotFoundError` or `shutil.which` miss means not installed. Implementation should return `ProviderHandle` if the binary exists and the exit code is 0 OR if stderr contains a quota/API error (not an auth/install error).

**Refined liveness logic for `_check_gemini_cli()`:**
```python
def _check_gemini_cli() -> ProviderHandle | None:
    p = shutil.which("gemini")
    if not p:
        return None
    try:
        result = subprocess.run(
            [p, "-p", "ping", "--output-format", "json"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    # returncode 0 = success; non-zero may mean quota exhausted (API error),
    # which still confirms the CLI is installed and authenticated.
    # Distinguish "CLI present but API rate-limited" from "CLI auth broken".
    if result.returncode != 0:
        stderr_lower = (result.stderr or "").lower()
        api_errors = ("quota", "rate limit", "capacity", "429")
        if not any(e in stderr_lower for e in api_errors):
            return None  # auth/install error — not usable
    return ProviderHandle(
        name="gemini_cli",
        binary_path=p,
        cost_label="$0 (uses your Google AI Studio free tier)",
        priority=2,
    )
```

**`gemini_cli` inference command shape:**
```python
cmd = [
    self._bin,
    "-p", combined_prompt,
    "--output-format", "json",
    "--model", model,
]
```
`combined_prompt = system + "\n\n" + messages[-1]["content"]` + optional schema block.

**Output format:** `--output-format json` produces a JSON object. The `result` key contains the model's text response. When no schema enforcement is available natively, parse the `result` field as JSON if `output_schema` is not None.

**gemini-2.0-pro subscription caveat:** `gemini-2.0-pro` requires a Gemini Advanced subscription (Google One AI Premium). The free tier only includes `gemini-2.0-flash` and `gemini-2.5-flash`. The `_PROVIDER_DEFAULTS` entry lists `"score": "gemini-2.0-pro"` per CONTEXT.md D-02. A user without Gemini Advanced will get a quota/auth error on the score model. This is acceptable — the cascade falls through. [ASSUMED: subscription tier behavior; confirmed via quota error observation]

---

## 4. `claude -p` Subprocess Specifics

### Confirmed behavior [VERIFIED: live execution]

**Command that works:**
```bash
claude -p "ping" --output-format json --no-session-persistence --tools ""
```

**Actual stdout (live):**
```json
{
  "type": "result",
  "subtype": "success",
  "is_error": false,
  "result": "pong",
  "usage": {
    "input_tokens": 3,
    "cache_creation_input_tokens": 30287,
    "output_tokens": 5
  },
  "total_cost_usd": 0.11366025,
  ...
}
```

**Key fields:**
- `result` — the model's text response (plain text or JSON string when schema used)
- `structured_output` — populated when `--json-schema` flag is provided
- `is_error` — boolean; when true, `result` contains the error message
- `total_cost_usd` — cost in USD against the user's subscription (informational; not billed separately)
- `usage.input_tokens`, `usage.output_tokens` — token counts

**Critical finding:** The `cache_creation_input_tokens: 30287` indicates the CLAUDE.md system prompt is loaded on liveness probes. The first call is expensive (cache build); subsequent calls are cheap (cache hits). This confirms D-03's rationale for process-lifetime caching — even one probe per session is acceptable.

**Full command shape for `ClaudeCodeCLIProvider.call()`:**
```python
cmd = [
    self._bin,
    "-p",
    "--model", model,
    "--output-format", "json",
    "--no-session-persistence",
    "--tools", "",
    "--system-prompt", system,
]
if output_schema is not None:
    cmd.extend(["--json-schema", json.dumps(output_schema)])
# User message piped via stdin (avoids Windows command-line length limits):
result = subprocess.run(
    cmd,
    input=user_message,
    capture_output=True, text=True,
    timeout=180,
    encoding="utf-8", errors="replace",
)
```

This matches the existing `claude_client._run_oneshot()` pattern exactly — `ClaudeCodeCLIProvider` is essentially a `BaseProvider`-conformant wrapper around that function. [VERIFIED: `claude_client.py` lines 416-455]

**Parsing strategy:**
- If `output_schema is not None`: check `envelope["structured_output"]` first; fall back to `json.loads(envelope["result"])`.
- If `output_schema is None`: try `json.loads(envelope["result"])`; on `JSONDecodeError`, return `{"text": envelope["result"]}`.
- On `is_error == True`: check for credit patterns → `BudgetExceededError`; else `RuntimeError`.

**Note on `--bare` flag:** Do NOT use `--bare`. It forces `ANTHROPIC_API_KEY` auth and bypasses OAuth/subscription, which would reroute billing from the user's subscription to API keys. This is documented in `claude_client.py` line 435.

**Session message cost:** Each probe call hits the model (real API round-trip) and records `total_cost_usd` against the subscription. The liveness probe MUST be cached for the process lifetime to avoid repeated charges. Concretely: do not re-probe on every `call()` — check `shutil.which()` once at `__init__` time; that is sufficient for the Phase 39 liveness contract.

---

## 5. `llama-cpp-python` JSON-Schema Path

### Version status [VERIFIED: PyPI registry]

- Current version: **0.3.23** (latest as of 2026-05-14)
- Phase 39 constraint: `>=0.2.0` (CONTEXT.md D-06, pyproject.toml entry)
- `response_format` JSON schema support was added in the `0.2.x` series; safe with `>=0.2.0` pin

**`llama-cpp-python` is NOT installed** in the project venv — `ModuleNotFoundError` confirmed. [VERIFIED: live import attempt]

### API shape for `create_chat_completion`

```python
from llama_cpp import Llama

llm = Llama(
    model_path="/path/to/model.gguf",
    n_ctx=4096,
    n_threads=os.cpu_count() // 2,
    verbose=False,
)

# Structured output via JSON schema (GBNF grammar under the hood):
response = llm.create_chat_completion(
    messages=[
        {"role": "system", "content": system},
        {"role": "user", "content": user_message},
    ],
    response_format={"type": "json_object", "schema": output_schema},
    max_tokens=max_tokens,
    temperature=0.0,
)
data = json.loads(response["choices"][0]["message"]["content"])

# Freeform text:
response = llm.create_chat_completion(
    messages=[...],
    max_tokens=max_tokens,
    temperature=0.0,
)
text = response["choices"][0]["message"]["content"].strip()
```

**NOT `create_completion`** (which takes a raw string prompt). Use `create_chat_completion` for the system+user message pattern that matches the rest of the codebase. [ASSUMED: based on llama-cpp-python API conventions; PLAN-P1.md used `create_completion` which is the legacy raw-prompt API]

**Token counts** are in `response["usage"]["prompt_tokens"]` and `response["usage"]["completion_tokens"]`.

**`response_format` schema semantics:** When `response_format={"type": "json_object", "schema": <dict>}` is provided, llama-cpp-python compiles the schema to a GBNF grammar and constrains token sampling. This mirrors the Ollama `format=<schema dict>` path exactly — same underlying llama.cpp mechanism. [ASSUMED: based on llama-cpp-python documentation patterns; GBNF path confirmed in llama-cpp-python source conventions]

**Windows install caveat:** llama-cpp-python requires compilation of C++ extensions. On Windows:
- MSVC toolchain or MinGW is required for `pip install llama-cpp-python`.
- Pre-built wheels are available on PyPI for common Python+platform combinations (`cp313-win_amd64`), so most Windows users get a binary wheel without needing MSVC.
- GPU acceleration (CUDA, ROCm, Vulkan) requires platform-specific build flags beyond `pip install` — not needed for Phase 39 (CPU inference is the target).
- The `[local-ai]` optional extra should add the comment `# Windows: pre-built wheel available; no MSVC needed for CPU-only inference` in `pyproject.toml`.

**Lazy import pattern (critical):**
```python
class LocalBundledProvider(BaseProvider):
    def __init__(self, model_path: str, n_ctx: int = 4096, **_kwargs) -> None:
        try:
            from llama_cpp import Llama  # lazy — only required when [local-ai] is installed
        except ImportError as exc:
            raise ImportError(
                "llama-cpp-python is required for local_bundled provider. "
                "Install with: uv sync --extra local-ai"
            ) from exc
        ...
```

`_make_adapter()` already catches `ImportError` at line 590 of `model_provider.py` — the `except (ValueError, RuntimeError)` block needs to be extended to `except (ValueError, RuntimeError, ImportError)` (check if it already is). [VERIFIED: `model_provider.py` line 590 — currently `except (ValueError, RuntimeError)` — needs `ImportError` added]

---

## 6. `_PROVIDER_DEFAULTS` Dict Literal

### Verbatim from PLAN-P1.md Chunk 3 Task 3.1 (lines 1101-1120)

```python
_VALID_WORKLOADS: frozenset[str] = frozenset({"quick", "score", "triage"})

# Workload-class model defaults per provider.
# - quick:  every non-scoring LLM call (extraction, parsing, navigation, research, reformatting, agentic enricher).
# - score:  full ordinal-rubric job scoring.
# - triage: pre-scoring gate; uses the `quick` model with a triage-specific prompt.
#
# Triage entries are absent here (resolved as identical to `quick` at lookup time).
_PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "claude_code_cli": {"quick": "claude-haiku-4-5",  "score": "claude-sonnet-4-6"},
    "anthropic":       {"quick": "claude-haiku-4-5",  "score": "claude-sonnet-4-6"},
    "gemini":          {"quick": "gemini-2.0-flash",  "score": "gemini-2.0-pro"},
    "gemini_cli":      {"quick": "gemini-2.0-flash",  "score": "gemini-2.0-pro"},
    "ollama":          {"quick": "qwen2.5:14b",       "score": "qwen2.5:14b"},
    "local_bundled":   {"quick": "<bundled-gguf>",    "score": "<bundled-gguf>"},
    "groq":            {"quick": "llama-3.3-70b-versatile", "score": "llama-3.3-70b-versatile"},
    "cerebras":        {"quick": "llama3.3-70b",            "score": "llama3.3-70b"},
}
```

**Placement:** Replace `_TIER_DEFAULTS` block at `model_provider.py` lines 33-43. Also remove the `from job_finder.config import DEFAULT_MODEL_HIGH, DEFAULT_MODEL_LOW, DEFAULT_MODEL_MID` import at line 23 — it will be unused after `_TIER_DEFAULTS` is removed.

**`local_bundled` placeholder:** The `"<bundled-gguf>"` string is a sentinel. `resolve_provider_config()` must handle it: if the resolved model is `"<bundled-gguf>"`, look up `config["providers"]["local_bundled"]["model_path"]` and use the filename as the model identifier passed to `LocalBundledProvider`.

### Legacy-tier translation map

Added to `resolve_provider_config()` before the main resolution logic:

```python
_LEGACY_TIER_MAP: dict[str, str] = {
    "low":     "quick",
    "mid":     "score",
    "high":    "score",
    "scoring": "score",
    # Forward-compat: new names pass through unchanged
    "quick":   "quick",
    "score":   "score",
    "triage":  "triage",
}

def resolve_provider_config(tier: str, config: dict) -> dict:
    # Phase 39: translate legacy tier names; Phase 40 deletes this block.
    workload = _LEGACY_TIER_MAP.get(tier, tier)
    ...
```

The rest of `resolve_provider_config()` is largely unchanged — it still reads `providers_cfg = config.get("providers", {})` and returns the same 7-key dict shape. The only structural change is that `_TIER_DEFAULTS.get(tier, DEFAULT_MODEL_MID)` becomes a lookup into `_PROVIDER_DEFAULTS[provider_name][workload]`.

---

## 7. `ProviderHandle` Dataclass + Detection Cache

### `ProviderHandle` shape

From PLAN-P1.md Task 2.1 and CONTEXT.md (D-03):

```python
@dataclass(frozen=True, slots=True)
class ProviderHandle:
    name: str          # "claude_code_cli" | "gemini_cli" | "ollama"
    binary_path: str   # absolute path from shutil.which()
    cost_label: str    # human-readable for wizard UI
    priority: int      # 1=claude_code_cli, 2=gemini_cli, 3=ollama (lower = preferred)
```

The `slots=True` is consistent with `ModelResult` in `model_provider.py` line 108. [VERIFIED: `model_provider.py` line 108]

### Detection cache semantics

```python
# Module-level cache — process-lifetime, no TTL eviction (Phase 39).
_detection_cache: dict[str, ProviderHandle | None] = {}

def detect_available_providers(*, refresh: bool = False) -> list[ProviderHandle]:
    """Return available providers in priority order.

    Results are cached for the process lifetime. Pass refresh=True to
    re-probe (e.g., after user installs a CLI during the wizard).
    """
    if not refresh and _detection_cache:
        # Cache populated — return cached result directly
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

**Cache invalidation:** Only via `refresh=True`. No time-based eviction in Phase 39.

**Detection ordering** (per CONTEXT.md D-03, DESIGN.md §2, memory `project_public_release_provider_priority`):
1. `claude_code_cli` (priority=1) — `claude -p "ping"` liveness
2. `gemini_cli` (priority=2) — `gemini -p "ping" --output-format json` liveness
3. `ollama` (priority=3) — `ollama list` ≥2 lines

`local_bundled` is NOT auto-detected (requires a model file path; wizard-driven in Phase 42). Detection module returns only the three CLI providers.

---

## 8. Subprocess Security Checklist

Per CONTEXT.md D-09 — all `subprocess.run` calls in Phase 39 MUST conform to:

| Requirement | Pattern | Rationale |
|---|---|---|
| List-form args | `[bin, "-p", prompt]` never `"bin -p prompt"` | Shell injection prevention |
| Binary via `shutil.which()` | `bin = shutil.which("claude"); if not bin: raise RuntimeError(...)` | Rejects relative paths, validates PATH membership |
| Mandatory timeout (probe) | `timeout=10` | Prevents hang on interactive auth prompts |
| Mandatory timeout (inference) | `timeout=180` | Prevents infinite hang on slow models |
| No `shell=True` | `subprocess.run([...], shell=False)` | Default; never override |
| Prompt as single argv element | `[bin, "-p", prompt_str]` | Prompt is one element, not shell-interpolated |
| No f-string into shell | N/A (list-form args; no shell involved) | Belt-and-suspenders |
| stdin for long content | `subprocess.run(cmd, input=user_message, ...)` | Avoids Windows cmd-line length limits (8191 char limit) |
| `capture_output=True` | Always | Prevents subprocess output mixing with Flask stdout |

**Grep assertions for test suite:**
```bash
# Verify no shell=True in any new provider file:
grep -n "shell=True" job_finder/web/providers/claude_code_cli.py
grep -n "shell=True" job_finder/web/providers/gemini_cli.py
grep -n "shell=True" job_finder/web/providers/detection.py

# Verify every subprocess.run has a timeout= kwarg:
grep -n "subprocess.run" job_finder/web/providers/claude_code_cli.py
grep -n "subprocess.run" job_finder/web/providers/gemini_cli.py
grep -n "subprocess.run" job_finder/web/providers/detection.py
```

---

## 9. Critical Correction: `_make_adapter()` Needs `ImportError` Added

**Current code** (`model_provider.py` line 590):
```python
except (ValueError, RuntimeError) as exc:
    logger.warning("Cascade: %s unavailable: %s", entry_provider, exc)
    continue
```

**Required change** for `local_bundled` lazy-import path:
```python
except (ValueError, RuntimeError, ImportError) as exc:
    logger.warning("Cascade: %s unavailable: %s", entry_provider, exc)
    continue
```

This is also needed in `tier_has_configured_provider()` at line 264:
```python
except (ValueError, RuntimeError, ImportError):
    continue
```

Both locations need this change as part of the `_make_adapter()` dispatch extension task.

---

## 10. `FREE_PROVIDERS` Discrepancy

**Current `claude_client.py` line 61:**
```python
FREE_PROVIDERS: frozenset[str] = frozenset({
    "gemini",
    "ollama",
    "claude_cli",  # NOTE: currently "claude_cli" not "claude_code_cli"
})
```

**Required in Phase 39** (CONTEXT.md D-07):
```python
FREE_PROVIDERS: frozenset[str] = frozenset({
    "gemini", "ollama", "claude_cli",   # existing (keep for backward compat)
    "claude_code_cli", "gemini_cli", "local_bundled",  # new
    # Note: "groq" and "cerebras" are NOT in FREE_PROVIDERS currently;
    # they have their own rate limits but not per-token cost. Unchanged in Phase 39.
})
```

The existing `"claude_cli"` name is used by `call_claude()` internally (line 582: `provider="claude_cli"`). The new `ClaudeCodeCLIProvider` returns `provider="claude_code_cli"`. Both must be in `FREE_PROVIDERS`. [VERIFIED: `claude_client.py` lines 61-67, 582]

---

## Validation Architecture

### Test Framework

| Property | Value |
|---|---|
| Framework | pytest (via `uv run --active pytest`) |
| Config file | `pyproject.toml` (tool.pytest section, if any) |
| Quick run | `uv run --active pytest tests/test_provider_*.py tests/test_model_provider.py -q --tb=short` |
| Full suite | `uv run --active pytest tests/ -q --tb=short` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File |
|---|---|---|---|---|
| STRANGE-PROV-01 | `_PROVIDER_DEFAULTS` membership; legacy-tier translation; `_VALID_WORKLOADS` | unit | `pytest tests/test_model_provider.py -q --tb=short` | modify existing |
| STRANGE-PROV-02 | `detect_available_providers()` ranking; liveness probe invocations; cache hit/miss | unit | `pytest tests/test_provider_detection.py -q --tb=short` | new |
| STRANGE-PROV-03 | `ClaudeCodeCLIProvider.call()` shape; JSON parse; exit-nonzero raise | unit | `pytest tests/test_provider_claude_code_cli.py -q --tb=short` | new |
| STRANGE-PROV-04 | `GeminiCLIProvider.call()` shape; JSON parse; exit-nonzero raise | unit | `pytest tests/test_provider_gemini_cli.py -q --tb=short` | new |
| STRANGE-PROV-05 | `LocalBundledProvider.call()` shape; lazy import; missing GGUF raises | unit | `pytest tests/test_provider_local_bundled.py -q --tb=short` | new |

### Specific Assertions Required

**STRANGE-PROV-01: `_PROVIDER_DEFAULTS` membership:**
```python
from job_finder.web.model_provider import _PROVIDER_DEFAULTS, _VALID_WORKLOADS
assert set(_PROVIDER_DEFAULTS) >= {
    "claude_code_cli", "gemini_cli", "ollama",
    "anthropic", "local_bundled", "gemini",
}
assert _VALID_WORKLOADS == frozenset({"quick", "score", "triage"})
```

**STRANGE-PROV-01: Legacy-tier translation:**
```python
cfg = {"providers": {"anthropic": {"provider": "anthropic", "fallback_chain": []}}}
result_low = resolve_provider_config("low", cfg)
assert result_low["model"] == _PROVIDER_DEFAULTS["anthropic"]["quick"]
result_scoring = resolve_provider_config("scoring", cfg)
assert result_scoring["model"] == _PROVIDER_DEFAULTS["anthropic"]["score"]
```

**STRANGE-PROV-01: Legacy names NOT in defaults dict (sanity):**
```python
flat_keys: set[str] = set()
for mapping in _PROVIDER_DEFAULTS.values():
    flat_keys.update(mapping.keys())
assert flat_keys.isdisjoint({"low", "mid", "high", "scoring", "haiku", "sonnet", "opus"})
```

**STRANGE-PROV-02: Detection cache hit-vs-miss (mock `subprocess.run`):**
```python
def _mock_run(returncode=0, stdout="pong", stderr=""):
    m = MagicMock()
    m.returncode, m.stdout, m.stderr = returncode, stdout, stderr
    return m

with patch("subprocess.run") as mock_run, patch("shutil.which") as mock_which:
    mock_which.side_effect = lambda c: f"/usr/bin/{c}" if c == "claude" else None
    mock_run.return_value = _mock_run()
    h1 = detect_available_providers()
    h2 = detect_available_providers()  # cache hit — subprocess.run NOT called again
    assert mock_run.call_count == 1    # only called once; cache returned on second call
```

**STRANGE-PROV-03/04/05: Cross-provider `ModelResult` shape (parametrized):**
```python
@pytest.mark.parametrize("provider_name", [
    "claude_code_cli", "gemini_cli", "ollama",
    "anthropic", "local_bundled", "gemini",
])
def test_call_returns_valid_model_result(provider_name, ...):
    # mock each provider's subprocess/HTTP/llama call
    result = provider.call(model, system, messages)
    assert isinstance(result.data, dict)
    assert isinstance(result.cost_usd, float)
    assert isinstance(result.input_tokens, int)
    assert isinstance(result.output_tokens, int)
    assert isinstance(result.model, str)
    assert isinstance(result.provider, str)
    assert isinstance(result.schema_valid, bool)
```

**STRANGE-PROV-05: Lazy import succeeds when `llama_cpp` NOT installed:**
```python
def test_import_local_bundled_without_llama_cpp_does_not_crash():
    # The module-level import must succeed even without llama_cpp installed.
    import importlib
    mod = importlib.import_module("job_finder.web.providers.local_bundled")
    assert hasattr(mod, "LocalBundledProvider")
    # Only the __init__ call should fail:
    with pytest.raises(ImportError):
        mod.LocalBundledProvider(model_path="/fake.gguf")
```

**Security: list-form args and timeout grep assertions (Wave 0 or CI check):**
```python
import ast, pathlib

provider_files = [
    "job_finder/web/providers/claude_code_cli.py",
    "job_finder/web/providers/gemini_cli.py",
    "job_finder/web/providers/detection.py",
]
for fpath in provider_files:
    source = pathlib.Path(fpath).read_text()
    assert "shell=True" not in source, f"{fpath} contains shell=True"
    # Each file that has subprocess.run must have timeout= in the same call
    if "subprocess.run" in source:
        assert "timeout=" in source, f"{fpath} missing timeout= on subprocess.run"
```

### Sampling Rate

- Per task commit: `uv run --active pytest tests/test_provider_*.py -q --tb=short`
- Per wave merge: full suite `uv run --active pytest tests/ -q --tb=short`
- Phase gate: full suite green before `/gsd-verify-work`

### Wave 0 Gaps (files that must exist before implementation starts)

- [ ] `tests/test_provider_detection.py` — covers STRANGE-PROV-02
- [ ] `tests/test_provider_claude_code_cli.py` — covers STRANGE-PROV-03
- [ ] `tests/test_provider_gemini_cli.py` — covers STRANGE-PROV-04
- [ ] `tests/test_provider_local_bundled.py` — covers STRANGE-PROV-05

*(Existing test infrastructure covers STRANGE-PROV-01 via `tests/test_model_provider.py` modifications)*

---

## 11. Open Risks / Unknowns

### R-01: `gemini -p` output format for inference (LOW risk)

The liveness probe output format was confirmed as `{"result": ...}` from the quota-error response JSON. However, the exact JSON envelope shape for a successful `gemini -p` response has not been confirmed by a successful inference call (quota was exhausted during spike). The implementation MUST handle at minimum:
- `result.stdout` parsed as JSON; `response["content"]` or `response["result"]` key contains model output.
- **Mitigation:** Planner should include a step to verify output format on a successful `gemini -p` call before submitting the final implementation. If the output format is plain text (not JSON), fall back to using stdout directly.

### R-02: `gemini-2.0-pro` model availability (MEDIUM risk)

CONTEXT.md D-02 specifies `"score": "gemini-2.0-pro"` for `gemini_cli`. The free tier of Google AI Studio may not include `gemini-2.0-pro` (requires Gemini Advanced subscription). If the user doesn't have this:
- The `score` workload via `gemini_cli` will get a quota/auth error.
- The cascade falls through — acceptable behavior.
- **Mitigation:** Document in module docstring and consider defaulting `score` for `gemini_cli` to `gemini-2.5-flash` (free tier) in `_PROVIDER_DEFAULTS`, raising the issue as a TODO for Phase 40 config overrides. This is a planning decision.

### R-03: `_make_adapter()` `local_bundled` constructor arg shape (LOW risk)

`_make_adapter()` currently passes `config=config` to providers. `LocalBundledProvider` needs `model_path` from `config["providers"]["local_bundled"]["model_path"]`. The planner must add this extraction in the `_make_adapter()` dispatch branch:
```python
if provider_name == "local_bundled":
    from job_finder.web.providers.local_bundled import LocalBundledProvider
    lp_cfg = config.get("providers", {}).get("local_bundled", {})
    model_path = lp_cfg.get("model_path", "")
    if not model_path:
        raise ValueError("providers.local_bundled.model_path not configured")
    return LocalBundledProvider(model_path=model_path, n_ctx=lp_cfg.get("n_ctx", 4096))
```

### R-04: `claude_client.py` existing `ClaudeCodeCLIProvider` overlap (LOW risk)

The existing `claude_client.py` already implements a full `claude -p` subprocess executor (`_run_oneshot`). The new `ClaudeCodeCLIProvider` will duplicate some of this logic. The planner should explicitly decide whether `ClaudeCodeCLIProvider.call()` delegates to `claude_client._run_oneshot()` (reuse) or re-implements the subprocess call (independence). Recommendation: delegate to `_run_oneshot()` for the initial implementation, then extract a shared utility in Phase 40 if needed. This keeps Phase 39 changes minimal.

### R-05: `groq` / `cerebras` in `FREE_PROVIDERS` (KNOWN GAP)

`groq` and `cerebras` are in `_PROVIDER_DEFAULTS` but NOT in `FREE_PROVIDERS`. They have free API tiers but do count against rate limits. Phase 39 does not change their `FREE_PROVIDERS` membership — this is pre-existing behavior and out of scope.

---

## Sources

### PRIMARY (HIGH confidence — verified via live tool execution)
- `gemini --help` output — confirmed `-p/--prompt` headless flag, `--output-format json` [VERIFIED]
- `claude --help` output — confirmed full flag set including `--output-format json`, `--json-schema`, `--no-session-persistence` [VERIFIED]
- `claude -p "ping" --output-format json` live execution — confirmed JSON envelope shape, `result` field, `usage` fields [VERIFIED]
- `gemini -p "ping" --output-format json` live execution — confirmed headless mode functional (quota 429 confirms CLI works) [VERIFIED]
- `job_finder/web/model_provider.py` — `BaseProvider`, `ModelResult`, `_TIER_DEFAULTS`, `_make_adapter()`, cascade path [VERIFIED]
- `job_finder/web/providers/ollama_provider.py` — canonical `BaseProvider` subclass pattern [VERIFIED]
- `job_finder/web/claude_client.py` — `FREE_PROVIDERS`, `_run_oneshot()`, existing `claude -p` implementation [VERIFIED]
- `job_finder/web/providers/anthropic_provider.py` — canonical cost-tracking pattern [VERIFIED]
- PyPI registry — `llama-cpp-python` latest version 0.3.23, `>=0.2.0` is valid [VERIFIED]
- `uv run python -c "import llama_cpp"` — confirmed NOT installed in project venv [VERIFIED]

### SECONDARY (MEDIUM confidence)
- `.planning/public-release/PLAN-P1.md` — `_PROVIDER_DEFAULTS` dict literal (lines 1101-1120), `ProviderHandle` dataclass, `detection.py` skeleton [CITED]
- `.planning/phases/39-strangerify-provider-abstraction/39-CONTEXT.md` — all D-01 through D-09 decisions [CITED]
- `.planning/REQUIREMENTS.md` — STRANGE-PROV-01 through STRANGE-PROV-05 [CITED]

### TERTIARY (ASSUMED)
- `llama-cpp-python` `create_chat_completion` API shape with `response_format` [ASSUMED — matches documented API conventions but not verified in this session without installed package]
- `gemini -p` successful response JSON envelope schema (not verified — quota exhausted) [ASSUMED]
- `gemini-2.0-pro` free-tier availability caveat [ASSUMED]

---

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|---|---|---|
| A1 | `llama-cpp-python.Llama.create_chat_completion(response_format={"type":"json_object","schema":...})` is valid API | §5 | Implementation must fall back to prompt-injection schema hints; low risk as GBNF is core feature |
| A2 | `gemini -p` successful response has a `result` or `content` key in its JSON envelope | §3, §11 R-01 | Implementation parses wrong key and returns empty; mitigated by Wave 0 test |
| A3 | `gemini-2.0-pro` requires paid Gemini Advanced subscription | §3, §11 R-02 | `score` workload via `gemini_cli` fails for free-tier users; cascade falls through safely |

---

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|---|---|---|---|---|
| `claude` CLI | `claude_code_cli` provider, liveness probe | Yes | (current Claude Code) | N/A — detected at runtime |
| `gemini` CLI | `gemini_cli` provider, liveness probe | Yes | 0.42.0 | N/A — detected at runtime |
| `ollama` CLI | detection module | Yes (from prior phases) | (installed) | N/A — detected at runtime |
| `llama-cpp-python` | `local_bundled` provider | NOT installed | — | Optional extra `[local-ai]`; lazy import |
| Python 3.13 | project constraint | Yes | 3.13.5 | — |

**Missing dependencies with fallback:** `llama-cpp-python` — optional install via `uv sync --extra local-ai`; lazy import prevents crash without it.

---

## RESEARCH COMPLETE
