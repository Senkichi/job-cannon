---
phase: 30-cascade-execution
plan: "01"
subsystem: infra
tags: [cascade, model-routing, rate-limiting, provider-fallback, requests, pytest, tdd]

# Dependency graph
requires:
  - phase: 29-cascade-config-rate-limiting
    provides: "_check_daily_limit, _increment_usage, _ensure_usage_current, _init_usage_from_db helpers; fallback_chain and daily_limits keys in resolve_provider_config() return dict"
provides:
  - "Cascade dispatch loop in call_model() — iterates primary + fallback_chain providers"
  - "Per-provider daily limit skipping (CASC-03)"
  - "429 HTTPError detection and exhaustion marking (CASC-04)"
  - "RuntimeError when all providers exhausted (CASC-07)"
  - "5 cascade execution tests covering all cascade behaviors (TEST-02)"
affects: [31-prompt-variants, 32-provider-attribution, sonnet-evaluator, haiku-scorer]

# Tech tracking
tech-stack:
  added: ["requests (imported in model_provider.py for HTTPError catch)"]
  patterns:
    - "Cascade dispatch: build chain list from primary + fallback_chain, iterate with per-provider skip conditions"
    - "429 exhaustion marker: _daily_usage[provider] = daily_limits.get(provider, 999999)"
    - "Original messages preserved per-provider iteration (augmented is local variable, messages never reassigned)"
    - "_ensure_usage_current(conn) called first, before any _check_daily_limit invocations"

key-files:
  created: []
  modified:
    - job_finder/web/model_provider.py
    - tests/test_model_provider.py

key-decisions:
  - "Cascade path guarded by `if fallback_chain:` — empty list preserves existing single-fallback behavior exactly"
  - "Schema failure after retry inside cascade loop does continue (not raise) — try next provider before giving up"
  - "Both ValueError and RuntimeError caught from _make_adapter (ValueError = missing API key, RuntimeError = Ollama health check)"
  - "999999 sentinel for 429 exhaustion on providers without configured daily_limits (acceptable: all cascade providers have limits)"

patterns-established:
  - "Cascade loop pattern: build flat chain, skip on 3 conditions (limit/missing-key/budget), catch 429 separately, RuntimeError after loop"
  - "TDD for dispatch logic: write failing tests first, confirm RED, then implement GREEN"

requirements-completed: [CASC-03, CASC-04, CASC-07, TEST-02]

# Metrics
duration: 6min
completed: 2026-03-29
---

# Phase 30 Plan 01: Cascade Execution Summary

**Cascade dispatch loop in call_model() — iterates fallback chain skipping exhausted/unavailable providers, marks 429s as exhausted, raises RuntimeError when all providers fail**

## Performance

- **Duration:** 6 min
- **Started:** 2026-03-29T23:32:47Z
- **Completed:** 2026-03-29T23:38:28Z
- **Tasks:** 2 (TDD: RED commit + GREEN commit)
- **Files modified:** 2

## Accomplishments

- `call_model()` now walks the `fallback_chain` when non-empty: cerebras -> groq -> ollama -> anthropic with automatic skip on daily limit exhaustion, missing API key, or 429 rate limit
- All four cascade skip conditions implemented: exhausted limit (`_check_daily_limit`), missing key (`ValueError`/`RuntimeError` from `_make_adapter`), budget gate for paid providers, 429 HTTPError marking exhausted
- Per-provider schema validation + retry loop uses original `messages` (not augmented) for each new provider — Pitfall 2 guard confirmed by `test_cascade_preserves_original_messages`
- 5 new cascade tests cover all CASC-03/04/07/TEST-02 requirements; full suite at 1805 tests, all passing

## Task Commits

1. **Task 1: Write cascade execution tests (TDD RED)** - `185cc2e` (test)
2. **Task 2: Implement cascade dispatch loop** - `033271e` (feat)

## Files Created/Modified

- `job_finder/web/model_provider.py` - Added `import requests`; cascade dispatch loop in `call_model()` (if fallback_chain path); backward-compat path unchanged
- `tests/test_model_provider.py` - Added `import requests`; 5 cascade test functions; shared `_CASCADE_CONFIG` fixture data

## Decisions Made

- **Backward-compat guard:** `if fallback_chain:` branch — existing single-fallback path runs unchanged when `fallback_chain=[]`, preserving all 38 prior tests exactly
- **Schema failure continues:** Inside cascade loop, schema-invalid-after-retry does `continue` to next provider rather than raising, maximizing cascade utility
- **Both ValueError and RuntimeError caught from `_make_adapter`:** `ValueError` = missing API key (Cerebras/Groq pattern); `RuntimeError` = Ollama health check failure — both result in silent skip with warning log
- **`_ensure_usage_current(conn)` is the first statement** in the cascade path — before any `_check_daily_limit` call, guarding Pitfall 3

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None. The implementation was straightforward wire-up of Phase 29 helpers.

## Known Stubs

None.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- Cascade execution is complete and tested. Phase 31 (per-model prompt variants) can thread variant keys through cascade config entries.
- Phase 32 (provider attribution) will wire `scoring_provider` column using the provider from the successful `ModelResult` returned by the cascade.
- All callers of `call_model()` (haiku_scorer, sonnet_evaluator, profile extractor) automatically benefit from cascading without any changes.

---
*Phase: 30-cascade-execution*
*Completed: 2026-03-29*
