---
phase: 22-module-splits
plan: 03
subsystem: ui
tags: [flask, blueprint, settings, guidelines, style-guide]

# Dependency graph
requires: []
provides:
  - guidelines_bp blueprint with /settings/migrate-style-guide, /settings/preview-guidelines-merge, /settings/apply-guidelines-merge routes
  - settings.py trimmed to config page + save routes only (~384 LOC)
  - guidelines.py containing all AI-powered style guide merge routes (~249 LOC)
affects: [22-module-splits]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Blueprint split: extract AI routes into dedicated module, keep config-management routes in original"
    - "Monkeypatch targets follow module of definition, not URL prefix"

key-files:
  created:
    - job_finder/web/blueprints/guidelines.py
  modified:
    - job_finder/web/blueprints/settings.py
    - job_finder/web/__init__.py
    - tests/test_settings.py

key-decisions:
  - "guidelines_bp shares url_prefix='/settings' with settings_bp — routes stay at same URLs, no template changes needed"
  - "Removed render_template, load_config, _CONFIG_PATH from guidelines.py — these are only used by settings.index()"

patterns-established:
  - "When splitting blueprints that share a URL prefix, test client routes need no changes but monkeypatch targets must follow the new module location"

requirements-completed: [SPLIT-07]

# Metrics
duration: 26min
completed: 2026-03-26
---

# Phase 22 Plan 03: Settings/Guidelines Split Summary

**Split settings.py (621 LOC) into settings.py (384 LOC, config management) + guidelines.py (249 LOC, AI-powered style guide merge routes) — both registered under `/settings` URL prefix**

## Performance

- **Duration:** 26 min
- **Started:** 2026-03-26T04:24:03Z
- **Completed:** 2026-03-26T04:50:00Z
- **Tasks:** 2
- **Files modified:** 4

## Accomplishments
- Created `guidelines_bp` with 3 routes: migrate-style-guide, preview-guidelines-merge, apply-guidelines-merge
- Trimmed `settings.py` to index/save/helpers only by removing the 3 AI-powered routes
- Registered `guidelines_bp` in `create_app()` after `settings_bp`
- Updated 4 monkeypatch targets in test_settings.py from `settings` to `guidelines` module
- All 29 settings+log_levels tests pass

## Task Commits

Each task was committed atomically:

1. **Task 1: Extract guidelines.py blueprint from settings.py** - `7f30ffe` (feat)
2. **Task 2: Update test imports and verify routes for SPLIT-07** - `ae9bd90` (fix)
3. **Cleanup: Remove unused imports from guidelines.py** - `07cdc81` (refactor)

**Plan metadata:** (docs commit follows)

## Files Created/Modified
- `job_finder/web/blueprints/guidelines.py` - New blueprint: 3 style guide AI routes (249 LOC)
- `job_finder/web/blueprints/settings.py` - Trimmed: removed AI routes, unused imports (384 LOC)
- `job_finder/web/__init__.py` - Added guidelines_bp import + register_blueprint call
- `tests/test_settings.py` - Updated 4 monkeypatch targets to reference guidelines module

## Decisions Made
- `guidelines_bp` uses `url_prefix="/settings"` — all routes stay at the same URL paths, no template or link changes needed
- Removed `render_template`, `load_config`, and `_CONFIG_PATH` from guidelines.py after discovering they are only used in `settings.index()` — kept guidelines.py clean
- URL prefix sharing between two blueprints is Flask-supported and clean for this split boundary

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - Missing Critical] Removed unused imports from guidelines.py**
- **Found during:** Task 2 (post-split cleanup)
- **Issue:** `render_template`, `load_config`, and `_CONFIG_PATH` were included per plan's import list but are not used by any moved function
- **Fix:** Removed 3 unused names from imports and deleted `_CONFIG_PATH = "config.yaml"` module constant
- **Files modified:** `job_finder/web/blueprints/guidelines.py`
- **Verification:** All 29 tests still pass after removal
- **Committed in:** `07cdc81` (refactor commit)

---

**Total deviations:** 1 auto-fixed (1 missing critical — unused import cleanup)
**Impact on plan:** Minor cleanup. All plan acceptance criteria met.

## Issues Encountered
- `test_activity_tracker.py` has a pre-existing `NameError: name 'Callable' is not defined` in `resume_generator.py` — this is an unrelated parallel-agent change, not caused by this plan's work. Confirmed by git stash test that isolated the failure to resume_generator.py modifications.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- SPLIT-07 complete: settings.py decomposed into settings.py + guidelines.py
- Remaining splits in Phase 22: SPLIT-01 through SPLIT-06 in separate plans

## Self-Check: PASSED

All files and commits verified:
- `job_finder/web/blueprints/guidelines.py` — FOUND
- `.planning/phases/22-module-splits/22-03-SUMMARY.md` — FOUND
- Commit `7f30ffe` — FOUND
- Commit `ae9bd90` — FOUND
- Commit `07cdc81` — FOUND

---
*Phase: 22-module-splits*
*Completed: 2026-03-26*
