---
phase: 29-cascade-config-rate-limiting
plan: "02"
subsystem: model_provider
tags: [rate-limiting, daily-tracker, module-state, sqlite-bootstrap, tdd]
dependency_graph:
  requires: [29-01]
  provides: [_check_daily_limit, _increment_usage, _init_usage_from_db, _ensure_usage_current]
  affects: [job_finder/web/model_provider.py, Phase 30 cascade execution]
tech_stack:
  added: ["datetime.date (stdlib, as _date alias)"]
  patterns:
    - Module-level mutable dict with date-guard rollover (_daily_usage, _usage_date)
    - Pure helper functions with conn only in _init_usage_from_db and _ensure_usage_current
    - Pytest fixture for module-state isolation (_reset_daily_state)
key_files:
  modified:
    - path: job_finder/web/model_provider.py
      role: Added _daily_usage, _usage_date state + four helper functions
    - path: tests/test_model_provider.py
      role: Added 10 daily limit tracker tests with module-state isolation fixture
decisions:
  - "_ensure_usage_current handles date-rollover check, keeping _check_daily_limit and _increment_usage pure (no conn dependency)"
  - "date(timestamp) used in SQL bootstrap query — NOT date(created_at); STATE.md had incorrect column name"
  - "Module state exposed directly for test monkeypatching; no dedicated reset function needed"
metrics:
  duration: "8 minutes"
  completed_date: "2026-03-29"
  tasks_completed: 2
  files_modified: 2
  tests_added: 10
  full_suite_count: 1800
---

# Phase 29 Plan 02: Daily Rate Limit Tracker Summary

**One-liner:** In-memory daily usage counters (_daily_usage dict + _usage_date string) with DB bootstrap from scoring_costs using date(timestamp), four pure helper functions for Phase 30 cascade.

## What Was Built

Added a complete daily rate-limiting subsystem to `job_finder/web/model_provider.py`:

### Module-Level State

```python
_daily_usage: dict[str, int] = {}
_usage_date: str = ""
```

### Four Helper Functions

1. **`_check_daily_limit(provider, daily_limits)`** — Returns `True` if provider is under its daily cap or has no configured cap. Pure function (no conn).

2. **`_increment_usage(provider)`** — Increments `_daily_usage[provider]` by 1. Pure function (no conn).

3. **`_init_usage_from_db(conn)`** — Resets `_daily_usage` and bootstraps from `scoring_costs WHERE date(timestamp) = date('now') GROUP BY provider`. Sets `_usage_date` to today.

4. **`_ensure_usage_current(conn)`** — Date-rollover guard: calls `_init_usage_from_db(conn)` if `_usage_date != today`. Phase 30 will call this at the top of `call_model()`.

### 10 New Tests

All tests use `_reset_daily_state` fixture to prevent module-state bleeding between tests:

| Test | Behavior Verified |
|------|-------------------|
| test_daily_limit_under_limit | 100 usage < 350 limit → True |
| test_daily_limit_at_limit | 350 usage == 350 limit → False |
| test_daily_limit_over_limit | 351 usage > 350 limit → False |
| test_daily_limit_no_configured_limit | provider not in daily_limits → True |
| test_daily_limit_provider_not_in_usage | configured but zero usage → True |
| test_daily_increment | counter increments 0→1→2 |
| test_daily_increment_existing | counter increments 5→6 |
| test_daily_limit_resets_on_new_day | DB rows → _daily_usage populated correctly |
| test_ensure_usage_current_triggers_on_date_change | stale date → reset + reinit |
| test_ensure_usage_current_noop_same_day | today's date → no-op |

## Verification

- `uv run pytest tests/test_model_provider.py -x -q` — 38 passed
- `uv run pytest tests/ -q` — 1800 passed (full suite green)
- `date(timestamp)` confirmed in SQL query; `date(created_at)` absent

## Deviations from Plan

**1. [Rule 1 - Bug] Corrected incorrect column name documentation in STATE.md**

- **Found during:** Task 1 research cross-check
- **Issue:** STATE.md key design decisions stated `WHERE date(created_at) = date('now')` — this column does not exist in scoring_costs table (Migration 1 uses `timestamp TEXT NOT NULL`)
- **Fix:** Used `date(timestamp)` in the implementation as documented in the RESEARCH.md Pitfall 1 and plan CONTEXT. The STATE.md documentation error was not corrected (out of scope for this plan — a docs-only fix, no behavioral impact on this plan's code)
- **Impact:** Zero — implementation used the correct column; the STATE.md doc error is pre-existing

None affecting code — plan executed as written using correct `date(timestamp)` column.

## Known Stubs

None. All functions are fully implemented with correct SQL and live DB bootstrap logic.

## Commits

| Task | Commit | Description |
|------|--------|-------------|
| Task 1 | fbac1cf | feat(29-02): add daily rate limit tracker helpers to model_provider |
| Task 2 | cb23148 | test(29-02): add 10 daily rate limit tracker tests |

## Self-Check: PASSED
