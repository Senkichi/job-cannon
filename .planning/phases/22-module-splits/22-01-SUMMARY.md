---
phase: 22-module-splits
plan: 01
subsystem: ats
tags: [module-split, ats, refactor, import-graph]

# Dependency graph
requires: []
provides:
  - ats_detection.py: ATS URL pattern extraction and slug candidate derivation
  - ats_prober.py: Single-company ATS probing with retry/backoff and error handling
  - ats_scanner.py: ATS scanning orchestration, company upsert, re-exports for backward compat
affects: [ats_scanner, pipeline_runner, blueprints/companies, tests/test_ats_scanner]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Module split with re-export pattern for backward compatibility (ats_scanner re-exports from ats_detection and ats_prober)"
    - "Python module singleton ensures requests.get patch works across split module boundaries"

key-files:
  created:
    - job_finder/web/ats_detection.py
    - job_finder/web/ats_prober.py
  modified:
    - job_finder/web/ats_scanner.py
    - job_finder/web/blueprints/companies.py
    - job_finder/web/pipeline_runner.py
    - tests/test_ats_scanner.py
    - tests/test_log_levels.py

key-decisions:
  - "Re-export all moved symbols from ats_scanner.py for backward compatibility — avoids updating 8+ callers"
  - "probe_ats_slugs patch tests work without changes because Python modules are singletons — ats_scanner.requests and ats_prober.requests reference the same module object"
  - "caplog logger target in test_log_levels.py updated to ats_prober — _handle_scan_error's logger moved with the function"

requirements-completed: [SPLIT-01]

# Metrics
duration: 12min
completed: 2026-03-26
---

# Phase 22 Plan 01: ATS Scanner Module Split Summary

**ats_scanner.py (1500 LOC) split into ats_detection.py (119 LOC) + ats_prober.py (396 LOC) + ats_scanner.py (1020 LOC) with re-exports for backward compatibility**

## Performance

- **Duration:** ~12 min
- **Completed:** 2026-03-26
- **Tasks:** 2
- **Files modified:** 5 (+ 2 new files created)

## Accomplishments

- SPLIT-01: ats_detection.py created — holds ATS URL regex constants + `extract_ats_from_urls` + `derive_slug_candidates`
- SPLIT-01: ats_prober.py created — holds retry state machine constants/helpers, `probe_single_company`, `_probe_lever/greenhouse/ashby`
- ats_scanner.py reduced from 1500 → 1020 LOC; re-exports moved symbols for 100% backward compatibility
- All 3 callers updated to import from new module locations
- All 1533 tests pass (no regressions)

## Task Commits

1. **Task 1 — Extract modules** - `31b655e` (feat(22-01): extract ats_detection.py and ats_prober.py from ats_scanner.py)
2. **Task 2 — Update callers** - `8e89013` (feat(22-01): update callers and test imports for SPLIT-01)

## Files Created/Modified

- `job_finder/web/ats_detection.py` (NEW, 119 LOC) — ATS URL regex patterns, `extract_ats_from_urls`, `derive_slug_candidates`
- `job_finder/web/ats_prober.py` (NEW, 396 LOC) — retry state machine, `probe_single_company`, `_probe_lever`, `_probe_greenhouse`, `_probe_ashby`
- `job_finder/web/ats_scanner.py` (modified, 1020 LOC) — removed moved functions, added re-export block, cleaned imports
- `job_finder/web/blueprints/companies.py` — updated `probe_single_company` import to `ats_prober`
- `job_finder/web/pipeline_runner.py` — updated `extract_ats_from_urls` import to `ats_detection`
- `tests/test_ats_scanner.py` — updated `extract_ats_from_urls` imports to `ats_detection`
- `tests/test_log_levels.py` — updated caplog logger target to `job_finder.web.ats_prober`

## Decisions Made

- Re-export all moved symbols from ats_scanner.py to maintain 100% backward compatibility with the ~8 callers that import directly (data_enricher.py, careers_scraper.py, expiry_checker.py, backfill_companies.py, scheduler.py). This avoids cascading updates across the codebase that are not part of SPLIT-01's scope.
- Python's module singleton property means `patch("job_finder.web.ats_scanner.requests.get")` continues to work for probe_ats_slugs tests even though probe functions now live in ats_prober.py — both modules reference the same `requests` module object.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] caplog logger target needed updating in test_log_levels.py**
- **Found during:** Task 2 (full test suite run)
- **Issue:** `test_promoted_to_unreachable_caplog_integration` sets `caplog.at_level(logging.INFO, logger="job_finder.web.ats_scanner")` to capture `_handle_scan_error` logs. After the split, `_handle_scan_error` lives in `ats_prober.py` and logs via `logger = logging.getLogger("job_finder.web.ats_prober")`. The old logger target no longer captured the output.
- **Fix:** Changed caplog logger from `job_finder.web.ats_scanner` to `job_finder.web.ats_prober` in `test_promoted_to_unreachable_caplog_integration`
- **Files modified:** `tests/test_log_levels.py`
- **Commit:** `8e89013`

## Known Stubs

None — all three module files are fully implemented with no placeholder code.

## User Setup Required

None.

## Next Phase Readiness

- SPLIT-01 complete with all tests passing (1533 total)
- ats_scanner.py reduced to 1020 LOC (target was ~1025)
- ats_detection.py at 119 LOC (plan target: ~120 LOC)
- ats_prober.py at 396 LOC (plan target: ~330 LOC — slightly higher due to complete docstrings)
- Ready for SPLIT-02 (data_enricher.py)

## Self-Check: PASSED

All required files exist:
- job_finder/web/ats_detection.py: FOUND
- job_finder/web/ats_prober.py: FOUND
- job_finder/web/ats_scanner.py: FOUND
- job_finder/web/blueprints/companies.py: FOUND
- job_finder/web/pipeline_runner.py: FOUND
- tests/test_ats_scanner.py: FOUND
- tests/test_log_levels.py: FOUND
- .planning/phases/22-module-splits/22-01-SUMMARY.md: FOUND

All commits present:
- 31b655e (Task 1 - extract modules): FOUND
- 8e89013 (Task 2 - update callers): FOUND
