---
phase: 23-n-plus-one-batching
plan: 02
subsystem: database
tags: [sqlite, batch, executemany, stale-detection, pipeline-events]

# Dependency graph
requires:
  - phase: 22-module-splits
    provides: "scoring_runner.py and stale_detector.py as independent modules"
provides:
  - "Batch archive in stale_detector.run_stale_detection() using 2 SQL statements per batch"
  - "Pipeline events audit trail preserved with per-job from_status"
  - "6 tests in tests/test_stale_detector.py covering batch archive behavior"
affects: [23-n-plus-one-batching, stale-detection, pipeline-events]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Batch UPDATE with WHERE dedup_key IN (?,?,...) for bulk status changes"
    - "executemany INSERT into pipeline_events for O(1) audit trail per batch"

key-files:
  created:
    - tests/test_stale_detector.py
  modified:
    - job_finder/web/stale_detector.py

key-decisions:
  - "Batch archive replaces N per-row update_pipeline_status() calls with 1 UPDATE + 1 executemany INSERT"
  - "SELECT now includes pipeline_status column (was only dedup_key) — needed for from_status in pipeline_events"
  - "utc_now_iso() called once before executemany — all events in a batch share same timestamp"
  - "update_pipeline_status import removed — was only used for archive loop, now unused"

patterns-established:
  - "Batch pattern: SELECT ids + statuses → bulk UPDATE → executemany INSERT audit log"

requirements-completed: [BATCH-03]

# Metrics
duration: 8min
completed: 2026-03-27
---

# Phase 23 Plan 02: Batch Archive in Stale Detector Summary

**Replaced N+1 per-row update_pipeline_status() calls in stale_detector.py archive loop with 1 bulk UPDATE + 1 executemany INSERT for pipeline_events audit trail**

## Performance

- **Duration:** 8 min
- **Started:** 2026-03-27T07:01:22Z
- **Completed:** 2026-03-27T07:09:22Z
- **Tasks:** 1 (TDD: RED + GREEN)
- **Files modified:** 2

## Accomplishments
- Archive operation reduced from N*3 queries (SELECT + UPDATE + INSERT per row) to 2 queries total per batch
- Pipeline events audit trail preserved: each archived job gets a pipeline_events row with correct from_status (discovered or reviewing)
- 6 tests cover all batch archive scenarios including mixed statuses, active-stage exclusion, empty DB, and stale marking unchanged
- update_pipeline_status import removed from stale_detector.py (now unused)

## Task Commits

Each task was committed atomically:

1. **Task 1 RED: Batch archive tests** - `d9b8b91` (test)
2. **Task 1 GREEN: Batch archive implementation** - `c392d23` (feat)

_Note: TDD task has two commits (test → feat)_

## Files Created/Modified
- `tests/test_stale_detector.py` - 6 tests for batch archive behavior (new file)
- `job_finder/web/stale_detector.py` - Archive loop replaced with batch UPDATE + executemany INSERT

## Decisions Made
- Batch archive replaces N per-row update_pipeline_status() calls with 1 UPDATE + 1 executemany INSERT — reduces N*3 queries to 2 per batch
- SELECT now includes `pipeline_status` column (previously only `dedup_key`) — needed to preserve from_status accuracy in pipeline_events without an extra per-row lookup
- utc_now_iso() called once before executemany loop — all events in a batch share one timestamp (acceptable for audit log, avoids N function calls)
- update_pipeline_status import removed since it became unused after refactor

## Deviations from Plan

None - plan executed exactly as written.

Note: TDD RED phase tests passed immediately with the existing implementation (tests verify observable behavior which both old and new code satisfy). This is expected for a pure refactoring task — the tests define correct behavior, not implementation details. GREEN phase changed the internals while preserving behavior.

## Issues Encountered
- Parallel agent (Plan 23-03) had committed RED phase tests for `test_batch_scoring.py` with 1 intentionally failing test (`test_cancellation_check_once_sonnet`). Full suite was run excluding that file to confirm zero regressions from this plan's changes. The failing test is 23-03's RED test, not caused by this plan.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- BATCH-03 complete. Stale detector now archives N jobs with 2 SQL statements
- Plan 23-01 (scoring_runner batch reads) and Plan 23-03 (batch_scoring cancellation + counters) are parallel plans in the same phase

---
*Phase: 23-n-plus-one-batching*
*Completed: 2026-03-27*
