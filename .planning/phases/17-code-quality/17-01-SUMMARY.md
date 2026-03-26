---
phase: 17-code-quality
plan: 01
subsystem: testing
tags: [logging, flask, htmx, jinja2, exception-handling]

# Dependency graph
requires: []
provides:
  - Test isolation: RotatingFileHandler not attached during pytest runs
  - Scan exception separation: template errors propagate as 500, scan errors show user-friendly message
  - Date filter HTMX: input event triggers on date inputs so clearing fires HTMX refresh
affects: [testing, companies, jobs]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "_is_testing guard for production-only side effects in create_app()"
    - "Two-layer exception handling: inner try for logic errors, render_template outside for template errors"
    - "Form-level hx-trigger with element-specific input event listeners for date inputs"

key-files:
  created:
    - tests/test_logging.py (new TestNoFileLoggingInTestMode class)
  modified:
    - job_finder/web/__init__.py
    - job_finder/web/blueprints/companies.py
    - job_finder/web/templates/jobs/index.html
    - tests/test_logging.py
    - tests/test_views.py
    - tests/test_ats_scanner.py

key-decisions:
  - "Gate _setup_file_logging() inside 'if not _is_testing:' block rather than moving it — minimal change, clear intent"
  - "Template error test uses pytest.raises because Flask TESTING=True propagates exceptions rather than returning 500"
  - "Add input event triggers at form level (not element level) to avoid element-owned HTMX requests conflicting with form submit"
  - "Fixed incomplete test mocks in test_ats_scanner.py that were masked by the old catch-all scan exception handler"

patterns-established:
  - "production-only side effects: gate behind 'if not _is_testing:' block that already exists in create_app()"
  - "exception separation: scan logic in try/except, render_template outside — clear attribution of 500s"

requirements-completed: [QUAL-01, QUAL-02, UI-01]

# Metrics
duration: 8min
completed: 2026-03-26
---

# Phase 17 Plan 01: Code Quality Fixes Summary

**Test log isolation via _is_testing guard, scan template error separation via two-layer exception handling, and date filter HTMX input event triggers for reliable clear behavior**

## Performance

- **Duration:** 8 min
- **Started:** 2026-03-26T03:25:10Z
- **Completed:** 2026-03-26T03:33:30Z
- **Tasks:** 3
- **Files modified:** 6

## Accomplishments

- QUAL-02: create_app() no longer writes to logs/app.log during pytest — RotatingFileHandler gated behind _is_testing check
- QUAL-01: scan() route now uses two-layer exception handling — TemplateErrors propagate as 500 with traceback, scan failures show "ATS scan failed" message
- UI-01: date filter inputs trigger HTMX refresh on both change and input events — clearing via native X button now works correctly

## Task Commits

TDD execution for Task 1 (RED then GREEN then REFACTOR):

1. **RED — Failing tests** - `7524c38` (test: add failing tests for QUAL-01, QUAL-02, UI-01)
2. **GREEN — Implementation** - `ba1699a` (feat: fix test log isolation, scan exception separation, date filter HTMX trigger)
3. **Bug fix — Mock completeness** - `230a00c` (fix: update scan route mocks to include html_scraped and errors keys)
4. **REFACTOR — Cleanup** - `7a02426` (refactor: merge file logging guard into existing _is_testing block)

## Files Created/Modified

- `job_finder/web/__init__.py` - moved _setup_file_logging() call inside `if not _is_testing:` block
- `job_finder/web/blueprints/companies.py` - split scan() try/except into inner scan logic + outer render_template
- `job_finder/web/templates/jobs/index.html` - added `input from:#filter-date-from, input from:#filter-date-to` to form hx-trigger
- `tests/test_logging.py` - rewrote TestFileLogging tests to be isolated (no shared state); added TestNoFileLoggingInTestMode
- `tests/test_views.py` - added TestScanExceptionSeparation and TestDateFilterHtmxTrigger test classes
- `tests/test_ats_scanner.py` - fixed incomplete mocks (missing html_scraped and errors keys)

## Decisions Made

- Gate `_setup_file_logging()` inside the existing `if not _is_testing:` block rather than a separate guard — keeps all production-only side effects together
- Template error test uses `pytest.raises(jinja2.TemplateSyntaxError)` because Flask TESTING=True propagates exceptions instead of returning 500 responses
- Form-level hx-trigger is the correct placement for date input events — element-level hx-trigger would make that element own its own HTMX request instead of submitting the form

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed incomplete scan route test mocks in test_ats_scanner.py**
- **Found during:** Task 3 (Full regression test)
- **Issue:** Two mocks in TestScanRouteProbeBeforeScan returned incomplete result dicts (missing `html_scraped` and `errors` keys). Previously masked by the old catch-all exception handler swallowing the TemplateUndefinedError. After the exception separation fix, the UndefinedError propagated and broke the test.
- **Fix:** Added `html_scraped: 0` and `errors: []` to both mock return values to match the actual `run_ats_scan` return signature
- **Files modified:** `tests/test_ats_scanner.py`
- **Verification:** `uv run pytest tests/ -x` — 1434 passed
- **Committed in:** `230a00c`

---

**Total deviations:** 1 auto-fixed (Rule 1 — pre-existing bug exposed by the fix)
**Impact on plan:** The mock fix was a direct consequence of QUAL-01's exception separation; the bug was hidden for the lifetime of the old catch-all handler.

## Issues Encountered

- The `test_create_app_attaches_file_handler` test in test_logging.py was testing behavior that no longer holds (create_app in test mode no longer attaches a handler). Updated tests to: (1) verify `_setup_file_logging()` works when called directly, and (2) add a new class verifying TESTING mode does NOT attach. Both test approaches are now independent (clean up handlers after each test to avoid process-level state pollution).

## Known Stubs

None — all three fixes are fully wired with real behavior.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- All three code quality issues resolved and verified by 1434 passing tests
- logs/app.log not modified during test run (verified before/after mtime match)
- Phase 17 has only one plan — phase complete

## Self-Check: PASSED

All required files exist:
- job_finder/web/__init__.py: FOUND
- job_finder/web/blueprints/companies.py: FOUND
- job_finder/web/templates/jobs/index.html: FOUND
- tests/test_logging.py: FOUND
- tests/test_views.py: FOUND
- .planning/phases/17-code-quality/17-01-SUMMARY.md: FOUND

All commits present:
- 7524c38 (RED tests): FOUND
- ba1699a (GREEN impl): FOUND
- 230a00c (bug fix): FOUND
- 7a02426 (refactor): FOUND
