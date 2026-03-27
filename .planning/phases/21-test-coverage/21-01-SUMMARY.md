---
phase: 21-test-coverage
plan: 01
subsystem: testing
tags: [pytest, flask-test-client, companies-blueprint, sqlite3, unittest.mock]

requires:
  - phase: 20-surgical-fixes
    provides: standalone_connection(), companies table schema complete with miss_reason column (Migration 12+)
provides:
  - tests/test_companies.py: 29 tests across 8 route test classes covering all companies blueprint routes
affects: [phase-22, future blueprint changes to companies.py, ats_scanner.py]

tech-stack:
  added: []
  patterns:
    - "companies_app fixture: migrated_db + TESTING=True in JF_CONFIG (activates probe_ats_slugs/run_ats_scan guards)"
    - "_insert_company() helper: direct sqlite3 INSERT into companies table (name, name_raw, ats_probe_status, scan_enabled, miss_reason)"
    - "patch at source module (job_finder.web.ats_scanner.upsert_company) not blueprint namespace when function imported locally inside route"

key-files:
  created:
    - tests/test_companies.py
  modified: []

key-decisions:
  - "Patch upsert_company at job_finder.web.ats_scanner (source module) not job_finder.web.blueprints.companies — it is imported locally inside the add() route body so it never appears in the blueprint module namespace"
  - "TESTING: True at top level of test_config dict activates both probe_ats_slugs/run_ats_scan guards (passed as JF_CONFIG) and sets Flask TESTING mode — one flag covers both"
  - "probe_single_company patched via unittest.mock.patch in retry tests — no TESTING guard in that function unlike probe_ats_slugs/run_ats_scan"

patterns-established:
  - "Blueprint test fixture trio: migrated_db -> companies_app -> companies_client (same chain as detections blueprint)"
  - "DB state assertions after mutating routes: query directly via conn.execute() after HTTP call to verify actual DB change"

requirements-completed: [TEST-01]

duration: 8min
completed: 2026-03-27
---

# Phase 21 Plan 01: Test Coverage Summary

**29-test companies blueprint test suite covering all 8 routes with DB state assertions for toggle and update_slug, TESTING guard for scan, and patch for probe_single_company in retry tests**

## Performance

- **Duration:** 8 min
- **Started:** 2026-03-27T04:00:49Z
- **Completed:** 2026-03-27T04:08:40Z
- **Tasks:** 2 (both delivered in single file write; Task 2 was already included)
- **Files modified:** 1

## Accomplishments

- Created tests/test_companies.py (405 lines, 29 tests) with full coverage of all 8 companies blueprint routes
- DB state assertions for toggle (scan_enabled flip verified) and update_slug (ats_probe_status reset verified)
- Retry route tests cover all 6 cases: error (200), unreachable miss (200), hit/pending/regular-miss (all 400), missing (404)
- Full test suite: 1562 tests passing (29 new, 0 regressions)

## Task Commits

Each task was committed atomically:

1. **Task 1: Create test fixtures and tests for index, expand, collapse, add routes** - `09c2dc1` (test)
2. **Task 2: Add tests for toggle, update_slug, scan, and retry routes** - included in Task 1 commit (full file written together)

## Files Created/Modified

- `tests/test_companies.py` - 29-test suite covering all 8 companies blueprint routes with fixtures, _insert_company helper, and 8 test classes

## Decisions Made

- Patched `job_finder.web.ats_scanner.upsert_company` (not the blueprint namespace) because `upsert_company` is imported locally inside the `add()` route body via `from job_finder.web.ats_scanner import upsert_company`. Patching the blueprint module namespace raised AttributeError because the name is never assigned there.
- TESTING: True at top level of test_config passes through as JF_CONFIG["TESTING"] = True, which activates both probe_ats_slugs and run_ats_scan TESTING guards. Also set Flask's application.config["TESTING"] = True separately per CLAUDE.md requirement.
- probe_single_company has no TESTING guard, so retry tests use `with patch("job_finder.web.blueprints.companies.probe_single_company")` — this works because it is imported at module level in companies.py.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Corrected patch target for upsert_company in TestAddRoute**
- **Found during:** Task 1 (TestAddRoute.test_add_redirects_to_index)
- **Issue:** Plan specified `patch("job_finder.web.blueprints.companies.upsert_company")` but the function is imported locally inside the route body, so it never appears in the blueprint module namespace. AttributeError: module does not have the attribute 'upsert_company'
- **Fix:** Changed patch target to `job_finder.web.ats_scanner.upsert_company` (source module)
- **Files modified:** tests/test_companies.py
- **Verification:** All 13 Task 1 tests pass after fix
- **Committed in:** 09c2dc1 (Task 1 commit)

---

**Total deviations:** 1 auto-fixed (Rule 1 - bug in plan's patch target)
**Impact on plan:** Fix required for test correctness. No scope creep.

## Issues Encountered

- Plan specified patch path `job_finder.web.blueprints.companies.upsert_company` but upsert_company is imported locally inside the route function body, not at module level. Fixed by patching at source module. See deviation above.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- Phase 21 complete. companies blueprint has full test coverage satisfying TEST-01.
- All 1562 tests pass, ready for v1.4 milestone completion.
- No concerns or blockers.

---
*Phase: 21-test-coverage*
*Completed: 2026-03-27*
