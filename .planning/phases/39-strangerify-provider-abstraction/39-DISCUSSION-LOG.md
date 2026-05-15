# Phase 39: Strangerify Provider Abstraction - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-05-14
**Phase:** 39-strangerify-provider-abstraction
**Mode:** autonomous (user invoked `/gsd-discuss-phase 39` with the "work without stopping for clarifying questions" directive; recommended-default decisions taken inline)
**Areas analyzed:** BaseProvider contract reconciliation, `_PROVIDER_DEFAULTS` shape & legacy boundary, liveness probe semantics, CLI subprocess invocation, local-bundled path resolution, registry updates, test strategy, subprocess security posture, Phase 38 dependency resolution

---

## Area 1: BaseProvider Contract Reconciliation (D-01)

| Option | Description | Selected |
|--------|-------------|----------|
| Adopt `PLAN-P1.md` draft API verbatim | Use `.generate(prompt=...)` + `ModelResult(text=...)` | |
| Translate draft to current `.call()` contract | New providers implement `.call(model, system, messages, output_schema, max_tokens, timeout)` returning current `ModelResult(data: dict, ..., provider, schema_valid)` | ✓ |
| Extend `BaseProvider` with both `.call()` and `.generate()` | Dual-mode contract | |

**Reasoning:** The draft predates the v3.0 BaseProvider contract change. Current production cascade (`call_model()` at `model_provider.py:502+`) calls `.call(...)` on every adapter. Adopting the draft verbatim would break cascade integration. Dual-mode adds maintenance burden with no benefit.

**Risk if wrong:** Planner copies the stale skeleton, ships providers that don't slot into the cascade, requires rework in Phase 40.

---

## Area 2: `_PROVIDER_DEFAULTS` Shape + Legacy Removal Boundary (D-02)

| Option | Description | Selected |
|--------|-------------|----------|
| Phase 39 drops `low/mid/high/scoring` from defaults dict AND renames callers | Combines Phase 39 and Phase 40 scope | |
| Phase 39 drops keys from defaults dict only; adds translation layer; Phase 40 renames callers | Matches ROADMAP wording "config-side keys still present until Phase 40" | ✓ |
| Defer the dict swap to Phase 40 entirely | Phase 39 ships providers only, no dict change | |

**Reasoning:** ROADMAP success criterion 1 explicitly says "legacy `low`/`mid`/`high` keys removed from the defaults dict (config-side keys still present until Phase 40)" — this requires the dict swap AND a temporary translation layer. The translation layer is a contained ~10-line addition to `resolve_provider_config()`. Deferring it would leave Phase 39 with no testable defaults-dict success criterion.

**Translation table:** `low→quick`, `mid→score`, `high→score`, `scoring→score`. Passthrough for `quick/score/triage`.

---

## Area 3: Liveness Probe Semantics (D-03)

| Option | Description | Selected |
|--------|-------------|----------|
| `--version` exit-code-only for all CLIs | Cheap; matches gemini draft | |
| `claude -p "ping"` for claude, `--version` for gemini, `ollama list` for ollama | Matches `PLAN-P1.md` draft internal contradiction | |
| Real liveness probe per CLI with per-session caching | `claude -p "ping"` + `gemini -p "ping"` (spike) + `ollama list` with ≥2 rows; cache result | ✓ |

**Reasoning:** ROADMAP criterion 2 explicitly says "real liveness probe (not just `--version` exit code)". Caching prevents repeated subscription-message consumption. The `gemini -p` spike is required (DESIGN.md §6.4) — defer concrete flags to research.

**Cache strategy:** Process-lifetime cache (no TTL); `refresh=True` kwarg for Settings page refresh button. Time-based eviction deferred until real friction is observed.

---

## Area 4: CLI Subprocess Invocation (D-04, D-05)

| Option | Description | Selected |
|--------|-------------|----------|
| 120s timeout per `PLAN-P1.md` | Draft value | |
| 180s timeout for inference, 10s for detection probes | Slow-subscription headroom; matches `claude -p` worst case for long JD scoring | ✓ |
| Configurable per-provider in Phase 39 | Full `providers.<name>.timeout_s` schema | |

**Reasoning:** 120s is tight for full JD scoring against a slow subscription. 180s provides headroom. Per-provider config keys deferred to Phase 40's config-schema rewrite — Phase 39 hardcodes.

**Exit handling:** Nonzero exit → `RuntimeError` with `stderr` quoted; cascade catches and falls through. No retry inside provider (cascade owns retries).

---

## Area 5: `local_bundled` Path Resolution + Lazy Import (D-06)

| Option | Description | Selected |
|--------|-------------|----------|
| Module-top `from llama_cpp import Llama` | Simplest | |
| Lazy import in `__init__` | Allows file to be importable even without `[local-ai]` extra | ✓ |
| Lazy import + module-level `_LLAMA_AVAILABLE` flag | Extra complexity | |

**Reasoning:** Phase 39 success criterion 4 says `pip install job-cannon[local-ai]` should enable the provider — but importing `local_bundled.py` without the extra installed must NOT crash. Lazy import in `__init__` is the cleanest path. `_make_adapter()` already catches `ImportError` (line 596) for cascade fallthrough.

**Path resolution:** Constructor arg → config → `FileNotFoundError`. Wizard-driven default lands in Phase 42.

---

## Area 6: Registry Updates (D-07)

| Option | Description | Selected |
|--------|-------------|----------|
| Add all 3 new providers to both registries | `_SUPPORTED_PROVIDERS` + `FREE_PROVIDERS` | ✓ |
| Add only to `_SUPPORTED_PROVIDERS`; treat `local_bundled` as paid for cost-tracking purposes | Edge case | |

**Reasoning:** All three are subscription-leveraged or local — cost is genuinely $0. Treating any as "paid" would trigger `cost_gate()` budget checks that have no meaning for them.

---

## Area 7: Test Strategy (D-08)

| Option | Description | Selected |
|--------|-------------|----------|
| Test only the 3 NEW providers' `.call()` shape | Minimal | |
| Cross-provider parametrized `.call()` shape test covering all 6 production providers | Catches drift before Phase 40 wires them in | ✓ |
| Add full integration tests with real subprocess invocation | Heavy; CI flakiness risk | |

**Reasoning:** ROADMAP criterion 5 says "all five providers" — the production set is 6. Parametrized shape test is the cheapest way to guarantee consistency. Real subprocess tests deferred to Phase 40 canary or Phase 45 cross-platform validation.

**Coverage extras:** `pytest.importorskip("llama_cpp")` gates `local_bundled` tests; `_PROVIDER_DEFAULTS` membership assertion in `test_model_provider.py`; legacy-tier-translation tests.

---

## Area 8: Subprocess Security Posture (D-09)

All defaults — no real alternatives considered:
- List-form args (no `shell=True`)
- `shutil.which()`-resolved binary path
- Internal prompt content (no user-input injection vector)
- Mandatory timeouts on every `subprocess.run`

---

## Area 9: Phase 38 Dependency Resolution

| Option | Description | Selected |
|--------|-------------|----------|
| Block Phase 39 until Phase 38 ships | Strict dependency reading | |
| Sidestep Phase 38 dependency; accept `model_path` from config; Phase 42 wizard wires user_data_dirs | Phase 39 success criteria require nothing from Phase 38 | ✓ |

**Reasoning:** None of the 5 success criteria touch `user_data_dirs`. The Phase 38 dependency is soft (eventual default-path resolution). Phase 39 can proceed standalone; Phase 42 wires the wizard.

---

## Claude's Discretion

- Module docstrings & inline comments (match existing brevity)
- Error message wording
- Test fixture organization (shared `_mock_run()` helper)
- Field assertion order in tests
- `gemini_cli` invocation specifics (deferred to spike outcome)
- Local-bundled `Llama` constructor kwargs beyond `model_path`/`n_ctx`

## Deferred Ideas

- Detection cache TTL eviction (process-lifetime only for now)
- Per-provider `timeout_s` config schema (Phase 40)
- Default GGUF auto-download (Phase 42 wizard)
- `user_data_dirs.cache_path()` as default GGUF location (Phase 42)
- `gemini-2.0-pro` availability under free CLI tier (resolve during spike)
- Removing the legacy-tier-name translation layer (Phase 40)
