---
phase: 24-provider-foundation
plan: 02
subsystem: database
tags: [sqlite, migration, scoring-costs, multi-provider]

# Dependency graph
requires:
  - phase: 23-n-plus-one-batching
    provides: stable DB schema baseline for this migration to build on
provides:
  - scoring_costs.provider column with DEFAULT 'anthropic' — Phase 26 dispatcher can record provider per API call
affects: [25-provider-adapters, 26-dispatcher, 27-caller-migration]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Migration 18 follows the established ALTER TABLE ADD COLUMN DEFAULT pattern (idempotent via _apply_migration OperationalError catch)"

key-files:
  created: []
  modified:
    - job_finder/web/db_migrate.py
    - tests/test_migration.py

key-decisions:
  - "DEFAULT 'anthropic' on provider column ensures all pre-existing rows get the correct value without a data backfill step"
  - "record_cost() INSERT deliberately left unchanged — provider column populated via DEFAULT, Phase 25 adds explicit provider parameter"

patterns-established:
  - "New provider column uses DEFAULT 'anthropic' so existing callers need zero changes"

requirements-completed: [COST-01]

# Metrics
duration: 8min
completed: 2026-03-27
---

# Phase 24 Plan 02: Provider Column Migration Summary

**SQLite migration 18 adds `provider TEXT DEFAULT 'anthropic'` to scoring_costs, enabling per-provider cost attribution for Phase 26's multi-provider dispatcher**

## Performance

- **Duration:** ~8 min
- **Started:** 2026-03-27T18:35:00Z
- **Completed:** 2026-03-27T18:43:00Z
- **Tasks:** 1
- **Files modified:** 2

## Accomplishments
- Migration 18 added to MIGRATIONS list in db_migrate.py — `ALTER TABLE scoring_costs ADD COLUMN provider TEXT DEFAULT 'anthropic'`
- All existing scoring_costs rows automatically get 'anthropic' as their provider value via DEFAULT
- record_cost() INSERT in claude_client.py left completely unchanged — DEFAULT fills the new column
- TestMigration18 class with 4 tests: column existence, default value, backwards-compatible INSERT, and MIGRATIONS count

## Task Commits

Each task was committed atomically:

1. **Task 1: Add migration 18 (provider column) and test** - `577b3a7` (feat)

## Files Created/Modified
- `job_finder/web/db_migrate.py` - Migration 18 appended to MIGRATIONS list (ALTER TABLE scoring_costs ADD COLUMN provider TEXT DEFAULT 'anthropic')
- `tests/test_migration.py` - TestMigration18 class added at end; test_migration_count_is_thirteen updated from 17 to 18

## Decisions Made
- DEFAULT 'anthropic' chosen so all rows from before Phase 24 are correctly attributed without a data backfill step
- record_cost() deliberately not modified — Phase 25 plan adds the explicit `provider` parameter to that function

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Updated stale MIGRATIONS count assertion in test_migration_count_is_thirteen**
- **Found during:** Task 1 (full suite run after GREEN)
- **Issue:** `test_migration_count_is_thirteen` asserted `len(MIGRATIONS) == 17`, which broke immediately after migration 18 was added. The test already had a docstring noting it was updated historically for migrations 14, 16, 17.
- **Fix:** Updated assertion to `== 18` and extended the docstring note to include migration 18
- **Files modified:** tests/test_migration.py
- **Verification:** Full suite passes (1584 passed)
- **Committed in:** 577b3a7 (part of task commit)

---

**Total deviations:** 1 auto-fixed (Rule 1 - stale count assertion)
**Impact on plan:** Required fix — the stale assertion directly blocked the full suite from passing. No scope creep.

## Issues Encountered
None — migration and tests followed the plan exactly.

## Next Phase Readiness
- scoring_costs.provider column exists with correct DEFAULT — Phase 25 (provider adapters) and Phase 26 (dispatcher) can safely record provider values
- record_cost() signature unchanged — Phase 25 plan adds the explicit provider parameter
- Full suite at 1584 passing (up from 1359 mentioned in CLAUDE.md — additional tests added by prior phases)

---
*Phase: 24-provider-foundation*
*Completed: 2026-03-27*
