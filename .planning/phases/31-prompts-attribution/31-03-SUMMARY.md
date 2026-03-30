---
phase: 31-prompts-attribution
plan: 03
subsystem: database
tags: [provider-attribution, scoring, sonnet, model-provider]

# Dependency graph
requires:
  - phase: 31-prompts-attribution/31-01
    provides: persist_sonnet_score() with provider parameter added to DB
  - phase: 31-prompts-attribution/31-02
    provides: evaluate_job_sonnet() using call_model() which returns ModelResult.provider
provides:
  - Provider attribution threading: evaluate_job_sonnet injects provider into ScoringResult.data
  - score_and_persist_sonnet extracts and forwards provider= to persist_sonnet_score
  - scoring_provider DB column populated on every normal scoring run (ATTR-03 closed)
affects: [scoring_orchestrator, pipeline_runner, dashboard batch scoring]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Provider injection via data dict: inject ModelResult.provider into result dict without changing ScoringResult NamedTuple signature"

key-files:
  created: []
  modified:
    - job_finder/web/sonnet_evaluator.py
    - job_finder/web/scoring_orchestrator.py

key-decisions:
  - "Inject provider via data dict merge ({**result, 'provider': result_obj.provider}) rather than adding field to ScoringResult NamedTuple — avoids positional unpacking breaks in callers"
  - "provider key in data dict is benign for consumers reading only 'score' or 'fit_analysis' — they ignore unknown keys"

patterns-established:
  - "Attribution threading pattern: add key to shared data dict (not type signature) when threading metadata through NamedTuple boundaries"

requirements-completed: [ATTR-03]

# Metrics
duration: 5min
completed: 2026-03-30
---

# Phase 31 Plan 03: Prompts Attribution Summary

**Provider attribution chain closed: evaluate_job_sonnet() injects ModelResult.provider into ScoringResult.data, scoring_orchestrator extracts and writes scoring_provider column on every Sonnet score**

## Performance

- **Duration:** 5 min
- **Started:** 2026-03-30T00:57:47Z
- **Completed:** 2026-03-30T01:02:00Z
- **Tasks:** 1
- **Files modified:** 2

## Accomplishments

- evaluate_job_sonnet() now returns ScoringResult with data dict containing "provider" key from ModelResult.provider
- score_and_persist_sonnet() extracts provider from result dict and passes provider=provider to persist_sonnet_score()
- scoring_provider column in the DB is now populated on every normal pipeline scoring run (ATTR-03 complete)
- Full test suite passes with no regressions: 1815 tests

## Task Commits

Each task was committed atomically:

1. **Task 1: Inject provider into ScoringResult.data and wire orchestrator (ATTR-03)** - `67af9ff` (feat)

**Plan metadata:** (docs commit follows)

## Files Created/Modified

- `job_finder/web/sonnet_evaluator.py` - Added provider injection into result dict after call_model() returns
- `job_finder/web/scoring_orchestrator.py` - Extract provider from result and pass provider=provider to persist_sonnet_score()

## Decisions Made

- Inject via data dict merge (`{**result, "provider": result_obj.provider}`) not by changing ScoringResult NamedTuple fields — avoids breaking positional unpacking in callers while still threading attribution through the existing contract.
- Unknown keys in data dict are benign: all consumers of ScoringResult.data use `.get("score")` and `.get("fit_analysis")` — they simply ignore the new "provider" key.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- Full ATTR chain is closed: Plan 01 added DB column, Plan 02 added fewshot variants and cascade prompt threading, Plan 03 wires provider from call_model() return value through to the DB column.
- Phase 31 (Prompts & Attribution) is complete — all 3 plans done.
- Next milestone: full test suite for cascade, rate limiting, backward compatibility, and provider attribution.

---
*Phase: 31-prompts-attribution*
*Completed: 2026-03-30*
