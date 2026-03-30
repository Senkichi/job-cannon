---
phase: 31-prompts-attribution
plan: "02"
subsystem: scoring-prompts
tags: [prompts, fewshot, cascade, model-routing, prompt-variants]
dependency_graph:
  requires: []
  provides: [fewshot-default-system-prompt, PROMPT_VARIANTS, cascade-prompt-variant-injection]
  affects: [sonnet_evaluator, model_provider, eval_provider]
tech_stack:
  added: []
  patterns: [lazy-import-to-avoid-circular-dependency, list-of-dicts-for-cascade-chain]
key_files:
  created:
    - tests/test_sonnet_evaluator.py
  modified:
    - job_finder/web/sonnet_evaluator.py
    - job_finder/web/model_provider.py
    - eval_provider.py
    - tests/test_model_provider.py
    - tests/test_eval_provider.py
decisions:
  - "_SYSTEM_PROMPT in sonnet_evaluator now includes fewshot examples by default (production default)"
  - "eval_provider 'default' variant maps to _BASE_SYSTEM_PROMPT to preserve legacy eval baseline"
  - "Lazy import of PROMPT_VARIANTS inside cascade loop avoids circular dependency"
  - "Cascade chain rebuilt as list[dict] to carry prompt_variant key per entry"
metrics:
  duration: "12 minutes"
  completed_date: "2026-03-30"
  tasks_completed: 2
  files_modified: 5
---

# Phase 31 Plan 02: Fewshot Prompts & Cascade Variant Injection Summary

Fewshot calibration examples moved from eval CLI into production sonnet evaluator as the default system prompt. Created PROMPT_VARIANTS dict exported from sonnet_evaluator. Cascade loop in model_provider.call_model() rebuilt as list[dict] to carry per-entry prompt_variant, with lazy import resolution to avoid circular dependency.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Move fewshot to sonnet_evaluator + PROMPT_VARIANTS + dedup eval_provider | a9c56ec | sonnet_evaluator.py, eval_provider.py, tests/test_sonnet_evaluator.py |
| 2 | Cascade loop prompt variant injection (CASC-05) | 4c2a5a6 | model_provider.py, tests/test_model_provider.py, tests/test_eval_provider.py |

## What Was Built

### Task 1: Fewshot as Production Default (PRMT-01, PRMT-02)

**sonnet_evaluator.py** — Added:
- `_BASE_SYSTEM_PROMPT`: original plain prompt (no fewshot) — used by eval_provider "default" variant
- `_FEWSHOT_EXAMPLES`: 5 calibration examples (scores 15, 38, 62, 78, 91)
- `_DISTRIBUTION_INSTRUCTIONS`: expected score distribution guidance
- `_SYSTEM_PROMPT = _BASE_SYSTEM_PROMPT + _FEWSHOT_EXAMPLES` — fewshot is now the production default
- `PROMPT_VARIANTS: dict[str, str]` with keys `"fewshot"` and `"fewshot-distribution"`

**eval_provider.py** — Updated to avoid double fewshot:
- Import now includes `_BASE_SYSTEM_PROMPT` and `PROMPT_VARIANTS as _PRODUCTION_VARIANTS`
- `_FEWSHOT_SYSTEM_PROMPT = _SYSTEM_PROMPT` (no longer `+ _FEWSHOT_EXAMPLES` which would double them)
- All `_FEWSHOT_*_PROMPT` compositions that used `_SYSTEM_PROMPT + _FEWSHOT_EXAMPLES + X` now use `_SYSTEM_PROMPT + X`
- `PROMPT_VARIANTS["default"]` maps to `_BASE_SYSTEM_PROMPT` (legacy eval baseline without fewshot)
- `PROMPT_VARIANTS["fewshot-distribution"]` imports from `_PRODUCTION_VARIANTS` for consistency

**tests/test_sonnet_evaluator.py** — New test file (5 tests):
- `test_system_prompt_includes_fewshot`: verifies _SYSTEM_PROMPT has calibration examples
- `test_prompt_variants_dict_exists`: verifies PROMPT_VARIANTS has required keys
- `test_fewshot_distribution_includes_distribution_instructions`: verifies superset content
- `test_base_system_prompt_has_no_fewshot`: verifies _BASE_SYSTEM_PROMPT is plain
- `test_eval_provider_fewshot_variant_not_doubled`: verifies no double-fewshot in eval_provider

### Task 2: Cascade Prompt Variant Injection (CASC-05)

**model_provider.py** — Cascade loop rebuilt:
- `chain` changed from `list[tuple[str, str]]` to `list[dict]` to carry `prompt_variant` key
- Primary entry synthesized as `{"provider": ..., "model": ..., "prompt_variant": None}`
- Loop variables renamed from `provider_name, model_id` to `entry_provider, entry_model, entry_variant`
- Added `effective_system` resolution: lazy import `PROMPT_VARIANTS` from sonnet_evaluator when `entry_variant` is set
- Lazy import pattern (`from job_finder.web.sonnet_evaluator import PROMPT_VARIANTS as _pv`) avoids circular dependency
- Both first and retry `adapter.call()` use `effective_system`
- RuntimeError message updated to use new dict structure

**tests/test_model_provider.py** — 2 new cascade tests:
- `test_cascade_prompt_variant_overrides_system`: forces cerebras exhaustion, verifies groq receives fewshot-distribution system prompt
- `test_cascade_primary_entry_uses_original_system`: verifies primary entry (no variant) passes caller's system unchanged

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed two test regressions in test_eval_provider.py**
- **Found during:** Task 2 full suite run
- **Issue:** `test_system_prompt_matches_sonnet_evaluator` and `test_default_returns_original_system_prompt` both asserted that `reconstruct_prompt("default")` returns the old `_SYSTEM_PROMPT`. After the plan change, `_SYSTEM_PROMPT` now includes fewshot, but `"default"` maps to `_BASE_SYSTEM_PROMPT`. Both assertions were stale.
- **Fix:** Updated both assertions to use `_BASE_SYSTEM_PROMPT`. Also added `_BASE_SYSTEM_PROMPT` to the import in test_eval_provider.py.
- **Files modified:** `tests/test_eval_provider.py`
- **Commit:** 4c2a5a6

## Verification Results

- `uv run pytest tests/test_sonnet_evaluator.py tests/test_model_provider.py -x -v` — 50 passed
- `uv run pytest tests/` — **1815 passed** (up from 1805; 10 new tests added)
- No circular import errors — lazy import pattern confirmed working
- No double fewshot — `PROMPT_VARIANTS["fewshot"].count("Score 15 (Poor fit)") == 1`

## Known Stubs

None — all data flows are wired. PROMPT_VARIANTS exported and consumed by cascade loop.

## Self-Check: PASSED

Files verified:
- `job_finder/web/sonnet_evaluator.py` — contains `_BASE_SYSTEM_PROMPT`, `_FEWSHOT_EXAMPLES`, `_DISTRIBUTION_INSTRUCTIONS`, `_SYSTEM_PROMPT`, `PROMPT_VARIANTS`
- `job_finder/web/model_provider.py` — contains `effective_system`, `entry_variant`, `PROMPT_VARIANTS` lazy import
- `eval_provider.py` — contains `_BASE_SYSTEM_PROMPT` import, updated compositions
- `tests/test_sonnet_evaluator.py` — created with 5 tests
- `tests/test_model_provider.py` — contains `test_cascade_prompt_variant_overrides_system`

Commits verified:
- a9c56ec — Task 1
- 4c2a5a6 — Task 2
