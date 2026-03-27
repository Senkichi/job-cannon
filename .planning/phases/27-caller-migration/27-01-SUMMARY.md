---
phase: 27-caller-migration
plan: "01"
subsystem: ai-scoring
tags: [anthropic, call_model, haiku, sonnet, cost-tracking, model-provider]

requires:
  - phase: 26-dispatcher-cost-tracking
    provides: call_model() dispatcher and AnthropicProvider adapter

provides:
  - haiku_scorer.score_job_haiku() routes through call_model(tier="haiku")
  - sonnet_evaluator.evaluate_job_sonnet() routes through call_model(tier="sonnet")
  - AnthropicProvider forwards job_id and purpose to call_claude for correct cost attribution

affects: [28-evaluation-framework, scoring tests, cost tracking]

tech-stack:
  added: []
  patterns:
    - "call_model(tier=) replaces direct call_claude() in scoring hot path"
    - "AnthropicProvider receives job_id/purpose at init, forwards to call_claude for cost attribution"

key-files:
  created: []
  modified:
    - job_finder/web/haiku_scorer.py
    - job_finder/web/sonnet_evaluator.py
    - job_finder/web/model_provider.py
    - job_finder/web/providers/anthropic_provider.py

key-decisions:
  - "AnthropicProvider stores job_id and purpose at init and forwards to call_claude — required for correct cost attribution since call_claude records costs internally"
  - "ctx parameter removed from score_job_haiku and evaluate_job_sonnet — no external callers used it; eliminates dead code"
  - "DEFAULT_MODEL_HAIKU/SONNET config lookups removed from haiku_scorer/sonnet_evaluator — call_model resolves model names internally via resolve_provider_config"
  - "_make_adapter accepts job_id/purpose and forwards to AnthropicProvider only — other providers record cost via _maybe_record_cost after the call"

patterns-established:
  - "Scoring callers pass tier name to call_model, never provider-specific model IDs"
  - "cost attribution (job_id, purpose) flows through _make_adapter to AnthropicProvider init, not through BaseProvider.call() interface"

requirements-completed: [MIGR-01, MIGR-02]

duration: 8min
completed: 2026-03-27
---

# Phase 27 Plan 01: Caller Migration - Core Scoring Path Summary

**Haiku and Sonnet scoring hot path migrated from call_claude() to call_model(tier=) dispatcher, with AnthropicProvider fixed to correctly forward cost attribution metadata**

## Performance

- **Duration:** ~8 min
- **Started:** 2026-03-27T20:51:00Z
- **Completed:** 2026-03-27T20:59:26Z
- **Tasks:** 2 (1 code change, 1 verification)
- **Files modified:** 4

## Accomplishments

- `haiku_scorer.score_job_haiku()` now calls `call_model(tier="haiku", ...)` instead of `call_claude()`
- `sonnet_evaluator.evaluate_job_sonnet()` now calls `call_model(tier="sonnet", ...)` instead of `call_claude()`
- `ClaudeContext` and direct `model` config lookups removed from both files
- `ctx` parameter removed from both function signatures (was dead code — no callers used it)
- Fixed `AnthropicProvider` to forward `job_id` and `purpose` to `call_claude()` for correct cost recording

## Task Commits

1. **Task 1: Migrate haiku_scorer and sonnet_evaluator to call_model** - `26cc048` (feat)
2. **Task 2: Verify scoring_runner, batch_scoring, test_scoring** - no commit needed (no changes required)

## Files Created/Modified

- `job_finder/web/haiku_scorer.py` - Replaced call_claude with call_model(tier="haiku"), removed ClaudeContext/ctx/model-lookup
- `job_finder/web/sonnet_evaluator.py` - Replaced call_claude with call_model(tier="sonnet"), removed ClaudeContext/ctx/model-lookup
- `job_finder/web/model_provider.py` - Updated _make_adapter to accept and pass job_id/purpose to AnthropicProvider
- `job_finder/web/providers/anthropic_provider.py` - Added job_id/purpose init params, forwards them to call_claude()

## Decisions Made

- `AnthropicProvider` now receives `job_id` and `purpose` at init time (not via `BaseProvider.call()` interface) — keeping the `call()` signature clean while still routing cost attribution through `call_claude()` which records internally
- `ctx` parameter removed from both function signatures — it was only used internally and had zero external callers

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed missing cost attribution in AnthropicProvider**
- **Found during:** Task 1 (migration of haiku_scorer)
- **Issue:** `AnthropicProvider.call()` called `call_claude()` without `job_id` or `purpose`, so cost rows were recorded with `purpose=""` and `job_id=None`. The test `test_score_job_haiku_purpose_is_haiku_score` caught this — it checks that `scoring_costs` has a row with `purpose='haiku_score'`.
- **Fix:** Added `job_id` and `purpose` parameters to `AnthropicProvider.__init__()`, stored on the instance, forwarded to `call_claude()` in `call()`. Updated `_make_adapter()` in `model_provider.py` to accept and pass these values.
- **Files modified:** `job_finder/web/providers/anthropic_provider.py`, `job_finder/web/model_provider.py`
- **Verification:** `test_score_job_haiku_purpose_is_haiku_score` passes; all 97 scoring tests pass
- **Committed in:** `26cc048` (Task 1 commit)

---

**Total deviations:** 1 auto-fixed (Rule 1 - Bug)
**Impact on plan:** The fix was required for correct cost attribution — without it, all costs from the migrated path would be recorded with empty purpose. No scope creep; the fix is localized to the provider layer.

## Issues Encountered

- `AnthropicProvider` had a pre-existing bug where `job_id` and `purpose` were not forwarded to `call_claude()`. This was masked when callers called `call_claude()` directly (old path). The migration exposed it because now the adapter is the call boundary.

## Next Phase Readiness

- Core scoring path (Haiku + Sonnet) migrated and verified with 97 passing tests
- `scoring_runner.py` and `batch_scoring.py` correctly thread the `client` parameter through to the migrated functions
- Ready for Phase 27 Plans 02-04 to migrate remaining callers

---
*Phase: 27-caller-migration*
*Completed: 2026-03-27*
