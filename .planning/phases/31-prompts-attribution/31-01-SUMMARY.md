---
phase: 31-prompts-attribution
plan: "01"
subsystem: database
tags: [migration, attribution, provider-tracking, tdd]
dependency_graph:
  requires: []
  provides: [scoring_provider-column, persist_sonnet_score-provider-param]
  affects: [job_finder/db.py, job_finder/web/db_migrate.py]
tech_stack:
  added: []
  patterns: [COALESCE-for-optional-updates, DEFAULT-for-backward-compatibility]
key_files:
  created: []
  modified:
    - job_finder/web/db_migrate.py
    - job_finder/db.py
    - tests/test_db.py
    - tests/test_migration.py
decisions:
  - "COALESCE(?, scoring_provider) used in persist_sonnet_score — None preserves column DEFAULT 'anthropic', non-None writes the provider"
  - "Column DEFAULT 'anthropic' on Migration 20 covers all existing rows — no explicit UPDATE needed for backward compatibility"
  - "persist_sonnet_score provider param is positional-optional (keyword with default=None) — all 4-arg callers unchanged"
metrics:
  duration: "11min"
  completed_date: "2026-03-30"
  tasks_completed: 2
  files_modified: 4
---

# Phase 31 Plan 01: Provider Attribution DB Layer Summary

One-liner: Migration 20 adds `scoring_provider` TEXT column to jobs with DEFAULT 'anthropic', and `persist_sonnet_score()` gains an optional `provider` param that writes via COALESCE for backward-safe attribution.

## What Was Built

### Migration 20 (job_finder/web/db_migrate.py)
Added as index 19 in MIGRATIONS list:
```python
"ALTER TABLE jobs ADD COLUMN scoring_provider TEXT DEFAULT 'anthropic'"
```
All existing rows get DEFAULT 'anthropic' — non-destructive, no data loss.

### JOBS_ALL_COLUMNS update (job_finder/db.py)
Added `scoring_provider` to the end of the column list so `get_job()` and `get_filtered_jobs()` return the new column in their result dicts.

### persist_sonnet_score update (job_finder/db.py)
Added `provider: str | None = None` as a 5th parameter. SQL now includes:
```sql
scoring_provider = COALESCE(?, scoring_provider)
```
Callers that pass `provider=` write it to the DB. Callers that omit it (4-arg backward-compatible callers) pass `None`, which COALESCE resolves to the existing column value (DEFAULT 'anthropic' on fresh rows).

## Tests Written

`tests/test_db.py — TestProviderAttribution` class (3 tests):
- `test_persist_sonnet_score_writes_provider` — verifies provider="cerebras" is written
- `test_persist_sonnet_score_defaults_anthropic_when_no_provider` — verifies None preserves DEFAULT 'anthropic'
- `test_scoring_provider_in_get_job` — verifies JOBS_ALL_COLUMNS includes scoring_provider

TDD flow: RED commit `fb23985` (tests added, failing) → GREEN commit `7f6e5a3` (implementation, all 3 pass).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Updated hardcoded migration count assertions in test_migration.py**
- **Found during:** Task 2 verification (full suite run)
- **Issue:** `test_migration_count_is_thirteen` and `test_migrations_count_is_19` both asserted `len(MIGRATIONS) == 19`; adding Migration 20 broke them
- **Fix:** Updated both assertions to `== 20` and updated docstrings to reference Migration 20
- **Files modified:** tests/test_migration.py
- **Commit:** `0be3f00`

## Commits

| Hash | Type | Description |
|------|------|-------------|
| `fb23985` | test | Add failing attribution tests for scoring_provider (TDD RED) |
| `7f6e5a3` | feat | Migration 20 scoring_provider column + persist_sonnet_score provider param |
| `0be3f00` | fix | Update migration count assertions from 19 to 20 |

## Verification Results

- `uv run pytest tests/test_db.py` — 16/16 passed
- `uv run pytest tests/test_migration.py` — all passed (104 total with db tests)
- `uv run pytest tests/` — 1815/1815 passed (full suite green)

## Known Stubs

None — all implemented functionality is fully wired.

## Self-Check: PASSED
