# Phase 39: Strangerify Provider Abstraction - Context

**Gathered:** 2026-05-14
**Mode:** autonomous (user invoked `/gsd-discuss-phase 39` with no clarifying questions; recommended-default decisions taken inline, flagged for redirect)
**Status:** Ready for planning

<domain>
## Phase Boundary

Phase 39 delivers three new provider classes (`claude_code_cli`, `gemini_cli`, `local_bundled`), an auto-detection module that ranks subscription-leveraged CLIs above local/paid options, and the swap of the flat `_TIER_DEFAULTS` dict for the nested `_PROVIDER_DEFAULTS` dict — without touching call-sites or `config.yaml`. Phase 40 owns the cascade rewire (caller `tier="low"` → `tier="quick"` rewrites, config schema rewrite, triage gate, canary).

**In scope:**
- `job_finder/web/providers/detection.py` — `detect_available_providers()` + `ProviderHandle` dataclass
- `job_finder/web/providers/claude_code_cli.py` — `ClaudeCodeCLIProvider(BaseProvider)` shelling out to `claude -p`
- `job_finder/web/providers/gemini_cli.py` — `GeminiCLIProvider(BaseProvider)` shelling out to `gemini` CLI
- `job_finder/web/providers/local_bundled.py` — `LocalBundledProvider(BaseProvider)` wrapping `llama-cpp-python`
- `pyproject.toml` — add `[project.optional-dependencies] local-ai = ["llama-cpp-python>=0.2.0"]`
- `_PROVIDER_DEFAULTS` nested dict added to `model_provider.py`; legacy `low`/`mid`/`high`/`scoring` keys removed from the **defaults dict**
- `_SUPPORTED_PROVIDERS` frozenset extended to register the 3 new providers
- `FREE_PROVIDERS` frozenset extended to include the 3 new providers ($0 cost recording path)
- `_make_adapter()` dispatch chain extended for the 3 new providers
- Unit tests covering `.call()` signature consistency across all production providers

**Out of scope (deferred):**
- Caller rewires (`tier="low"` → `tier="quick"`, `tier="scoring"` → `tier="score"`) — **Phase 40**
- `config.yaml` / `config.example.yaml` schema rewrite (`providers.primary`, `providers.overrides`, `providers.triage`) — **Phase 40**
- Triage gate + module — **Phase 40**
- Anthropic-tail spend reduction canary — **Phase 40**
- Settings-page UI for picking primary provider, triage toggle — **Phase 42 (Onboarding Wizard)**
- Wizard-driven GGUF download for `local_bundled` — **Phase 42**
- `user_data_dirs.cache_path()` integration for default GGUF location — **Phase 42** (Phase 39 accepts `model_path` from constructor/config only)

</domain>

<dependency_note>
## Phase 38 Dependency Resolution

ROADMAP says Phase 39 depends on Phase 38 (`user_data_dirs must exist for provider state`). Phase 38 has not shipped as of 2026-05-14 (current STATE.md: "Phase 35 complete, ready for Phase 36").

**Decision:** Phase 39 is structurally independent of Phase 38. None of the success criteria require `user_data_dirs`:
- `claude_code_cli` / `gemini_cli`: PATH lookup + subprocess; no user-data-dir touch.
- `local_bundled`: accepts `model_path` from constructor/config; default-path-via-user-data-dirs is Phase 42's wizard responsibility.
- `_PROVIDER_DEFAULTS`: pure in-memory dict.
- `detection.py`: PATH probes only.

**How to apply:** Planner can sequence Phase 39 ahead of Phase 38 if scheduling demands. If Phase 38 ships first, no rework needed — `local_bundled`'s `model_path` resolution can later be migrated to read from `user_data_dirs.cache_path()` in Phase 42 without changing the Phase 39 constructor signature.

</dependency_note>

<decisions>
## Implementation Decisions

### D-01: BaseProvider Contract (CRITICAL — `PLAN-P1.md` draft is stale)

`PLAN-P1.md` Chunk 2 uses a `.generate(prompt=...)` + `ModelResult(text=, cost_usd=, input_tokens=, output_tokens=, model=)` shape. **This is stale.** Current production `BaseProvider.call(model, system, messages, output_schema, max_tokens, timeout) -> ModelResult` and current `ModelResult(data: dict, cost_usd, input_tokens, output_tokens, model, provider, schema_valid)` are the locked contract.

**Decision:** The three new providers MUST implement `BaseProvider.call()` (NOT `.generate()`) and MUST return `ModelResult` with the current field set including `data: dict`, `provider: str`, and `schema_valid: bool`. Planner must NOT copy the `.generate()` skeleton from `PLAN-P1.md` verbatim — translate it to `.call()` first.

**Concretely for CLI providers:**
- Accept `messages: list[dict]` and inline them into a single prompt string before subprocess invocation (system prompt + last user message). Match the convention `ollama_provider.py` uses for prompt assembly.
- When `output_schema is not None`: append a `"Respond ONLY with JSON conforming to this schema: <json.dumps(schema)>"` block to the prompt (CC CLI and gemini CLI have no native schema flag yet).
- Parse `subprocess.stdout` as JSON when `output_schema is not None`. On parse failure, raise so the dispatcher's retry loop catches it.
- When `output_schema is None`: return `ModelResult(data={"text": stdout.strip()}, ...)` — this matches the current `data: dict` contract and lets non-structured callers pull `.data["text"]`.

**Concretely for `local_bundled`:**
- Use `llama-cpp-python`'s `response_format={"type": "json_object", "schema": <schema>}` for structured output (it has native GBNF grammar support, mirroring how `ollama_provider.py` uses `format=<schema dict>`).
- Return `data` as the parsed JSON dict directly (no extra wrapping needed when grammar-constrained).

### D-02: `_PROVIDER_DEFAULTS` Shape + Legacy Removal Boundary

Adopt `PLAN-P1.md`'s shape verbatim:

```python
_VALID_WORKLOADS: frozenset[str] = frozenset({"quick", "score", "triage"})

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

**Legacy-removal boundary for Phase 39:** drop `low`/`mid`/`high`/`scoring` keys from the **DEFAULTS DICT** only. Callers (`enrichment_tiers.py`, `job_scorer.py`, etc.) still pass `tier="low"`, `tier="scoring"` etc. — Phase 39's `resolve_provider_config()` MUST add a temporary translation layer:

| Caller `tier=` | Internal workload lookup |
|---|---|
| `"low"` | `"quick"` |
| `"mid"` | `"score"` |
| `"high"` | `"score"` |
| `"scoring"` | `"score"` |
| `"quick"` / `"score"` / `"triage"` | passthrough (forward-compat) |

This translation layer ships in Phase 39 and is **deleted in Phase 40** after all callers are renamed.

`config.yaml` / `config.example.yaml` are **untouched** in Phase 39 — they retain the `providers.low.*`, `providers.scoring.*` schema. Phase 40 rewrites them.

### D-03: Liveness Probe Semantics

ROADMAP criterion 2: "real liveness probe (not just `--version` exit code)". `PLAN-P1.md` draft uses `claude -p "ping"` for claude but `gemini --version` for gemini — internally contradictory.

**Decision:**
- `claude` CLI: `claude -p "ping"` with 10s timeout. Cost: one short turn against the user's Claude.ai subscription, $0 directly but consumes a session message. Acceptable because:
  - Detection runs on demand (onboarding, Settings refresh button), not on every request.
  - Probe result MUST be cached per-session: module-level `_detection_cache: dict[str, ProviderHandle | None]` with explicit `refresh=True` kwarg to bypass.
- `gemini` CLI: planner must spike during research (DESIGN.md §6.4 flags this as unknown). Expected: `gemini -p "ping"` if supported, else `echo ping | gemini` via stdin, else fall back to `gemini --version` and document the gap. If spike confirms no headless prompt mode, use `--version` and add a TODO comment citing this CONTEXT.md.
- `ollama`: `ollama list` with stdout having ≥2 non-empty lines (header + ≥1 model). Matches `PLAN-P1.md` draft. Daemon-down case (DESIGN.md §6.5) is handled by `subprocess.run` returning non-zero or empty stdout.

**Cache TTL:** Process-lifetime cache (no time-based eviction). Wizard / Settings page expose `refresh=True` to re-probe. Rationale: cheap to keep around; users physically install/uninstall CLIs rarely; the cost of a stale "not available" is low (user re-clicks refresh).

### D-04: `claude_code_cli` Subprocess Invocation

- Binary: `shutil.which("claude")` resolved at constructor time. Raise `RuntimeError` if not on PATH.
- Command shape: `[bin, "-p", <combined-prompt>]` as a list (no `shell=True`). Combined prompt = `system + "\n\n" + last user message` + optional schema block.
- Timeout: 180s (raised from `PLAN-P1.md`'s 120s; full JD scoring with a slow subscription can run long). Configurable via `providers.claude_code_cli.timeout_s` future-proof but Phase 39 hardcodes 180.
- Exit nonzero → `RuntimeError(f"claude CLI failed: {stderr.strip()}")`. Dispatcher's cascade catches this and falls to the next provider — same path as auth-error fall-through.
- Cost recording: `ModelResult(cost_usd=0.0, input_tokens=0, output_tokens=0, model="claude-<model>", provider="claude_code_cli", schema_valid=<derived from parse success>)`. The `_FREE_PROVIDERS` membership ensures `_maybe_record_cost` writes `0.0`.

### D-05: `gemini_cli` Subprocess Invocation

Same structural pattern as D-04. Concrete flags require a spike (see D-03). Planner must explicitly call out the spike step in `RESEARCH.md` and may not implement until the spike resolves.

### D-06: `local_bundled` — Model Path Resolution + Lazy Import

- Constructor signature: `LocalBundledProvider(model_path: str, n_ctx: int = 4096, **_kwargs)`.
- Path resolution priority (Phase 39): explicit constructor arg → `config["providers"]["local_bundled"]["model_path"]` → raise `FileNotFoundError`. The wizard-driven default-path-via-`user_data_dirs` lands in Phase 42.
- `from llama_cpp import Llama` is **lazy** inside `__init__` (NOT module-top). This is critical so importing `local_bundled.py` doesn't crash when the `[local-ai]` extra is not installed. The `_make_adapter()` dispatch must handle `ImportError` from this path and let the cascade fall through (matches existing pattern for `OllamaProvider` health-check failures).
- Default GGUF: Qwen2.5-3B-Instruct-Q4_K_M (~2GB). Phase 39 documents the recommendation in `pyproject.toml` comment / README addendum but does NOT bundle / download it. Wizard owns the download.
- `n_ctx=4096` matches `PLAN-P1.md` draft. Conservative; can be raised in `providers.local_bundled.n_ctx` config later.

### D-07: `FREE_PROVIDERS` + `_SUPPORTED_PROVIDERS` Registry Updates

`job_finder/web/claude_client.py`:
```python
FREE_PROVIDERS: frozenset[str] = frozenset({
    "gemini", "ollama", "groq", "cerebras",  # existing
    "claude_code_cli", "gemini_cli", "local_bundled",  # NEW
})
```

`job_finder/web/model_provider.py`:
```python
_SUPPORTED_PROVIDERS: frozenset[str] = frozenset({
    "anthropic", "gemini", "ollama", "openrouter",  # existing
    "claude_code_cli", "gemini_cli", "local_bundled",  # NEW
})
```

`_make_adapter()` dispatch chain extended with three `if provider_name == "<x>"` branches that lazy-import the provider module (preserves the existing pattern; avoids circular imports).

### D-08: Test Strategy

ROADMAP criterion 5 says "all five providers' `.call()` signatures returning a consistent `ModelResult` shape". The production set is actually **six** (anthropic, gemini, ollama, claude_code_cli, gemini_cli, local_bundled) plus `openrouter` exists for the audit eval harness — Phase 39 will test all production providers (treat the ROADMAP "five" as approximate).

**Test files (new):**
- `tests/test_provider_detection.py` — mock `subprocess.run` + `shutil.which` per `PLAN-P1.md` Task 2.1 (translate API names where the draft drifted from current code).
- `tests/test_provider_claude_code_cli.py` — mock `subprocess.run`; assert `.call()` signature, `ModelResult` field shape, schema-JSON parsing path, exit-nonzero raise behavior.
- `tests/test_provider_gemini_cli.py` — parallel structure; gated on the spike outcome.
- `tests/test_provider_local_bundled.py` — `pytest.importorskip("llama_cpp")` + mock `llama_cpp.Llama`; assert `.call()` signature, `data: dict` shape, `FileNotFoundError` on missing GGUF, lazy-import behavior.

**Test files (modify):**
- `tests/test_model_provider.py` — add tests for `_PROVIDER_DEFAULTS` membership (`assert set(_PROVIDER_DEFAULTS) >= {"claude_code_cli","gemini_cli","ollama","anthropic","local_bundled","gemini"}`); add tests that the legacy-tier-name translation layer works (`tier="low"` resolves to a `quick` model; `tier="scoring"` resolves to a `score` model); add `_VALID_WORKLOADS` assertion. **DO NOT** mark old tests as `xfail` unless they truly break — Phase 39 is additive, the translation layer keeps existing tests passing.

**Cross-provider shape consistency test:** parametrize a single test with all 6 provider names; mock each provider's I/O; assert the returned `ModelResult` has all required fields and correct types. This catches drift before Phase 40 wires them into the cascade.

### D-09: Subprocess Security Posture

All `subprocess.run` invocations:
- List-form args, never `shell=True`.
- Binary path is `shutil.which()` output (validates PATH membership; rejects relative-path attacks).
- Prompt content is internal (model_provider.py passes from internal callers); no user-input direct injection. Still, when building the prompt+schema block, no f-string interpolation into shell metacharacters because the prompt is a single argv element.
- Timeouts mandatory on every `subprocess.run` to prevent hangs (10s for detection probes, 180s for inference calls).

### Claude's Discretion

- **Module docstrings & inline comments:** Match the brevity / structure of existing `ollama_provider.py` and `anthropic_provider.py`. No multi-paragraph docstrings.
- **Error message wording:** Pick clear short messages; quote the failing binary/path.
- **Test fixture organization:** Use module-level `_mock_run()` helpers like `PLAN-P1.md` draft suggests; share via `tests/conftest.py` only if used by ≥3 test files.
- **Field assertion order in tests:** Use existing convention (alphabetical or call-order — match `tests/test_ollama_provider.py` if it has one).
- **`gemini_cli` invocation specifics:** Defer entirely to the spike outcome — planner / researcher returns concrete flags before implementation begins.
- **Local-bundled `Llama` constructor kwargs beyond `model_path` / `n_ctx`:** Sensible defaults (`verbose=False`, `n_threads=os.cpu_count() // 2` rounded if relevant); document in module docstring.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents (researcher, planner, executor) MUST read these before working on Phase 39.**

### Phase 39 contract / requirements
- `.planning/ROADMAP.md` — Phase 39 section (Goal, Depends on, Requirements list, 5 Success Criteria). The success-criteria text is the contract.
- `.planning/REQUIREMENTS.md` — STRANGE-PROV-01 through STRANGE-PROV-05 (the 5 forward requirements mapped to Phase 39).
- `.planning/STATE.md` — Accumulated Context section, especially "Key Sequencing Decisions (v5.0, 2026-05-13)" explaining why Phase 39 ships before the Phase 40 caller rewires.

### Strangerify design (full P1 design across phases 38–43)
- `.planning/public-release/DESIGN.md` §2 (provider abstraction summary), §6.4 (gemini CLI invocation unknown — spike required), §6.5 (ollama daemon detection edge case). The design doc is the single source of truth for Strangerify intent.
- `.planning/public-release/PLAN-P1.md` Chunk 2 (Provider abstraction; Tasks 2.1–2.5 are the most-developed drafts in the milestone). **CAUTION:** the draft uses a stale `.generate(prompt=...)` / `ModelResult(text=...)` API. Translate to current `BaseProvider.call(...)` / `ModelResult(data: dict, ...)` contract — see D-01 above.
- `.planning/public-release/PLAN-P1.md` Chunk 3, Task 3.1 — `_PROVIDER_DEFAULTS` and `_VALID_WORKLOADS` shape (lines ~1102–1160). Phase 39 ships the dict; Phase 40 rewires callers.

### Current code (canonical contracts to preserve)
- `job_finder/web/model_provider.py` — `BaseProvider`, `ModelResult`, `_SUPPORTED_PROVIDERS`, `_FREE_PROVIDERS`, `_make_adapter()`, `resolve_provider_config()`, `call_model()` cascade path. **The .call() signature here is the contract; the draft's .generate() is stale.**
- `job_finder/web/providers/ollama_provider.py` — canonical reference for grammar-constrained JSON output, prompt assembly from system+messages, `BaseProvider` subclass shape, health-check-on-init pattern.
- `job_finder/web/providers/anthropic_provider.py` — canonical reference for cost tracking and `call_claude()` delegation.
- `job_finder/web/claude_client.py` — `FREE_PROVIDERS` frozenset, `cost_gate()`, `record_cost()`, `compute_cost()`. New providers must be added to `FREE_PROVIDERS` (D-07).

### Phase 33 (v3.0 scoring) — locked decisions still relevant
- `CLAUDE.md` — "Scoring" section: `(provider, model)` persisted identity; grammar-constrained decoding via Ollama `format=<schema dict>`; shared `call_model(tier=...)` dispatcher pattern. Same dispatcher serves Phase 39's new providers — they must integrate with it, not bypass it.

### Memory — cross-conversation context
- `[[project_public_release_provider_priority]]` — Subscription-leveraged CLIs (Claude Code, gemini, ollama) rank above BYO API keys in detection/onboarding order.
- `[[project_phase33_model_selection]]` — qwen2.5:14b is the production scoring primary on this machine.
- `[[project_tier_rename_planned]]` — `low/mid/high` → `quick/score/triage` is a coordinated future phase (Phase 40 is that phase).

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `BaseProvider` ABC at `job_finder/web/model_provider.py:121-135` — new providers subclass this.
- `ModelResult` dataclass at `job_finder/web/model_provider.py:108-118` — frozen, 7 fields. New providers construct this directly.
- `_make_adapter()` dispatch at `job_finder/web/model_provider.py:396-453` — extend with 3 new `if provider_name == ...` branches; preserve lazy-import pattern to avoid circular deps.
- `_FREE_PROVIDERS` frozenset (alias for `FREE_PROVIDERS` from `claude_client.py`) — new providers added here get $0 cost recording for free via `_maybe_record_cost()`.
- `OllamaProvider`'s schema-handling logic (`_schema_to_example`, `_schema_to_field_instructions`, grammar-constrained `format=<schema dict>` path) — pattern to mirror in `local_bundled` via `llama-cpp-python`'s `response_format`.
- `cost_gate()` + budget tracking — new free providers SKIP this entirely (see `call_model()` cascade path line 601: `if entry_provider not in _FREE_PROVIDERS: if not cost_gate(...)`).

### Established Patterns
- **Lazy imports inside `_make_adapter()`** — every provider class is imported at point of dispatch, never at module top of `model_provider.py`. New providers must follow this to avoid the circular-import trap (providers import `BaseProvider` from `model_provider`, which can't import providers at top).
- **Health check at provider `__init__`** — `OllamaProvider` checks daemon reachability; `AnthropicProvider` rejects clients with no API key. New CLI providers check `shutil.which()` returns non-None and raise `RuntimeError` on miss. `local_bundled` raises `FileNotFoundError` on missing GGUF.
- **Health-check failures → `_make_adapter()` raises → cascade catches `(ValueError, RuntimeError, ImportError)` at line 596 → falls through to next provider.** New providers must use this exception family.
- **Prompt assembly:** existing providers concatenate `system + "\n\n" + messages[-1]["content"]` (Ollama) or pass system + messages separately (Anthropic). CLI subprocess providers will use the Ollama pattern.
- **Frozen dataclasses for value objects** — `ModelResult`, `ProviderHandle` (new) both use `@dataclass(frozen=True, slots=True)`.
- **Test mocking pattern** — patch `subprocess.run` and `shutil.which` together via `unittest.mock.patch`; provide a `_mock_run()` helper.

### Integration Points
- `_make_adapter()` dispatch — new branches added here.
- `_SUPPORTED_PROVIDERS` + `FREE_PROVIDERS` registries — new entries added.
- `resolve_provider_config()` — extended with the legacy-tier-name translation layer (D-02); reads from new `_PROVIDER_DEFAULTS` dict instead of `_TIER_DEFAULTS`.
- `call_model()` cascade — no changes (new providers slot in automatically once registered).
- `tier_has_configured_provider()` at line 233 — no changes (uses `_make_adapter()` indirectly).

### Anti-patterns to avoid
- **Module-top `from llama_cpp import Llama`** — crashes when `[local-ai]` extra not installed. Always lazy in `__init__`.
- **`subprocess.run(shell=True)` or string-form commands** — security smell. Always list-form.
- **Mutating `messages` list inside provider** — `model_provider._augment_with_errors()` already returns a new list. Providers must not mutate input.
- **Bypassing `_maybe_record_cost()`** — every successful provider call must round-trip through the cost recorder (writes $0 for FREE_PROVIDERS, full cost for paid). Direct `conn.execute("INSERT INTO scoring_costs ...")` in a provider is wrong.
- **Returning `ModelResult(data="...text...")`** — `data` is typed `dict`. Wrap text in `{"text": ...}` when no schema.

</code_context>

<specifics>
## Specific Ideas

- **`PLAN-P1.md` Chunk 2 (Tasks 2.1–2.5)** is the most-developed draft anywhere in the milestone, but it predates the v3.0 BaseProvider contract change and uses a stale `.generate()` API. Treat it as **strong reference**, NOT verbatim copy. Translate to current `.call()` shape.
- **`PLAN-P1.md` Chunk 3 (Task 3.1)** has the exact `_PROVIDER_DEFAULTS` dict literal (lines ~1110–1120) — use it verbatim for the dict shape, with Phase 39's caveat that callers still use legacy tier names until Phase 40.
- **DESIGN.md §6.4 explicitly flags the `gemini` CLI invocation as unknown.** Planner must include a research spike before implementation.
- **The "five providers" wording in ROADMAP criterion 5 is approximate** — the production set is six. Tests cover all six.
- **Phase 39 ships the registry-and-dict additions; Phase 40 ships the rewires.** This split is the single most important structural decision because it lets Phase 39 land in main without breaking any caller, which means it can be reviewed, merged, and validated in isolation before Phase 40 starts.

</specifics>

<deferred>
## Deferred Ideas

- **Detection result caching with TTL eviction** — Phase 39 uses process-lifetime cache only. Time-based eviction (e.g., re-probe every 24h) deferred until real usage data shows whether stale "not available" entries cause user friction.
- **Per-provider `timeout_s` config** — Phase 39 hardcodes 180s for CLI inference, 10s for detection probes. Per-provider config keys deferred until Phase 40 (config schema rewrite) or later.
- **Default GGUF auto-download in `local_bundled`** — explicitly Phase 42 (Wizard).
- **`user_data_dirs.cache_path()` as default GGUF location** — explicitly Phase 42 / depends on Phase 38.
- **`gemini-2.0-pro` model availability via free Gemini CLI** — possibly subject to subscription tiering; spike result may force a fallback model. Capture in research, not here.
- **Removing the legacy-tier-name translation layer from `resolve_provider_config()`** — explicitly Phase 40, after all callers are renamed.

</deferred>

---

*Phase: 39-strangerify-provider-abstraction*
*Context gathered: 2026-05-14*
*Mode: autonomous (recommended-default decisions; user may redirect any D-XX before planning runs)*
