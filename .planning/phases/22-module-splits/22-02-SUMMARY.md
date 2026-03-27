---
phase: 22-module-splits
plan: 02
subsystem: resume
tags: [module-split, refactor, resume-generator]
dependency_graph:
  requires: []
  provides: [resume_multi_version.py]
  affects: [resume_generator.py, tests/test_resume.py]
tech_stack:
  added: []
  patterns: [deferred-import-circular-avoidance]
key_files:
  created:
    - job_finder/web/resume_multi_version.py
  modified:
    - job_finder/web/resume_generator.py
    - tests/test_resume.py
decisions:
  - Deferred import of generate_resume_multi inside _generate_resume_background avoids circular import at module load time
  - sqlite3 imported in resume_multi_version.py to support patch("resume_multi_version.sqlite3.connect") in TestThreadSafety
  - TestScoreThresholdDispatch patches generate_resume_multi at resume_multi_version (the deferred import resolves there)
metrics:
  duration: 17min
  completed_date: "2026-03-27"
  tasks_completed: 2
  files_modified: 3
  files_created: 1
---

# Phase 22 Plan 02: Resume Generator Module Split Summary

**One-liner:** Split resume_generator.py (995 LOC) into resume_generator.py (single-pass, constants, helpers) and resume_multi_version.py (multi-version strategy selection and variant synthesis).

## What Was Done

Extracted the multi-version resume synthesis logic from `resume_generator.py` into a new focused module `resume_multi_version.py`, following the SPLIT-04 plan.

### Task 1: Extract resume_multi_version.py

Created `job_finder/web/resume_multi_version.py` (~409 LOC) with:
- `_haiku_select_strategies()` — Haiku-based strategy selection from STRATEGY_POOL
- `_generate_single_variant()` — thread-safe single strategy-focused variant generator
- `generate_resume_multi()` — orchestrates parallel variant generation + synthesis
- `_synthesize_variants()` — Sonnet synthesis pass merging best sections

Updated `resume_generator.py` (~601 LOC):
- Removed 4 moved functions
- Added deferred import inside `_generate_resume_background()` to avoid circular import at module load time: `from job_finder.web.resume_multi_version import generate_resume_multi`
- Removed unused `DEFAULT_MODEL_HAIKU` import
- Removed unused `ThreadPoolExecutor`, `as_completed`, `Callable` imports

### Task 2: Update test imports

Updated `tests/test_resume.py`:
- 8 import statements changed from `resume_generator` to `resume_multi_version` for moved symbols
- 16 patch targets updated: `call_claude`, `cost_gate`, `anthropic`, and moved function names now patched at `resume_multi_version`
- `TestScoreThresholdDispatch` patches `generate_resume_multi` at `resume_multi_version` (where the deferred import resolves during execution)

## Verification

- `uv run python -c "from job_finder.web.resume_multi_version import generate_resume_multi; ..."` → All imports OK
- `uv run pytest tests/test_resume.py tests/test_resume_validator.py` → 121 passed
- `uv run pytest tests/` → 1562 passed (no regressions)
- Combined LOC: 1010 (601 + 409), up slightly from 995 due to new module docstring and deferred import comment

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - Missing critical detail] Added `import sqlite3` to resume_multi_version.py**
- **Found during:** Task 2 test update
- **Issue:** `TestThreadSafety.test_each_variant_thread_opens_own_sqlite_connection` patches `resume_generator.sqlite3.connect`. After split, patch must target `resume_multi_version.sqlite3.connect`, but that requires `sqlite3` to be importable as a module attribute in `resume_multi_version`. The module used `standalone_connection` (which internally calls `sqlite3.connect` from `db_helpers`) but did not directly import `sqlite3`.
- **Fix:** Added `import sqlite3` to `resume_multi_version.py` to preserve the patching surface for the thread-safety test.
- **Files modified:** `job_finder/web/resume_multi_version.py`
- **Commit:** 513ce7e (included in Task 1 commit after realization)

## Key Decisions

1. **Deferred import pattern**: `generate_resume_multi` is imported inside `_generate_resume_background()` body, not at module top. This avoids circular import since `resume_multi_version` imports from `resume_generator` at module load time.

2. **sqlite3 import in resume_multi_version**: Added even though the module uses `standalone_connection` (not `sqlite3.connect` directly), to maintain the same patch surface for `TestThreadSafety`.

3. **TestScoreThresholdDispatch patch target**: Patches `resume_multi_version.generate_resume_multi` (not `resume_generator.generate_resume_multi`), because the deferred import binds the name in `resume_multi_version`'s namespace when the function is called.

## Self-Check: PASSED

- `job_finder/web/resume_multi_version.py` exists: FOUND
- Task 1 commit 513ce7e: FOUND
- Task 2 commit 95c3e39: FOUND
- Full test suite: 1562 passed, 0 failed
