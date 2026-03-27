---
gsd_state_version: 1.0
milestone: v1.4
milestone_name: Tech Debt Sweep
status: executing
last_updated: "2026-03-27T05:03:36.414Z"
last_activity: 2026-03-27
progress:
  total_phases: 5
  completed_phases: 3
  total_plans: 12
  completed_plans: 6
  percent: 100
---

# State

## Current Position

Phase: 22 (Module Splits) — EXECUTING
Plan: 2 of 7 complete
Status: Executing Phase 22
Last activity: 2026-03-27 -- Phase 22 Plan 02 complete

Progress: [█████░░░░░] 50%

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-26)

**Core value:** Surface the best-fit jobs fast and keep the application pipeline visible
**Current focus:** Phase 22 — Module Splits

## Performance Metrics

**Velocity:**

- Total plans completed: 9 (including this session)
- Phase 14-01: 2min (2 tasks, 8 files)
- Phase 17-01: 8min (3 tasks, 6 files)
- Phase 18-01: 15min (1 task TDD, 5 files)
- Phase 20-01: 15min (2 tasks, 15 files)
- Phase 20-02: 24min (2 tasks, 7 files)
- Phase 20-03: 49min (1 task, 20 files)
- Phase 21-01: 8min (2 tasks, 1 file)
- Phase 22-02: 17min (2 tasks, 4 files)
- Average duration: ~18min
- Total execution time: ~138min

*Updated after each plan completion*

## Accumulated Context

### Architectural Decisions

- HTMX fragment routes MUST check HX-Request header and return full page for direct browser access
- Status dropdown: hx-target=this hx-swap=outerHTML on the select element itself
- Accordion: compact row + hidden `<tr data-expand-slot>` placeholder pairs
- Use hx-on:click not onclick for event.stopPropagation() in HTMX 2.x
- Dismiss/return responses: ('', 200) not 204 — HTMX requires 200 for outerHTML swap
- Migrations stored as list of discrete SQL strings (not semicolon-delimited)
- CREATE TABLE IF NOT EXISTS for idempotent migration
- sort_by validated against Python allowlist before SQL interpolation
- config.yaml MUST be edited with Edit tool only (never Write — wiped 3 times previously)

### Decisions Made in Phase 18

- Reuse batch_score_sessions table with session_type='sync' for async sync — no new table needed
- Store sync results in existing columns: scored=jobs_new, total=total_fetched, skipped=error_count
- Simplified phase labels (running->gmail->done) since run_sync_now is synchronous and opaque
- Flask's native app.config["TESTING"] must be set explicitly in test fixtures

### Decisions Made in Phase 20 Plan 01

- Use is_meta_email with extra_patterns to extend base meta-email detection without duplication — parsers pass source-specific patterns as extra_patterns instead of maintaining private copies
- Indeed's _INDEED_META_PATTERNS retains only 2 source-specific patterns; "job alert digest" dropped as duplicate of BASE_META_PATTERNS
- Public functions/constants use no leading underscore (run_homepage_discovery, run_sync_now, merge_guidelines_into_guide, FIELD_LABELS) — private helpers keep _prefix

### Decisions Made in Phase 20 Plan 02

- Job.normalized_dedup_key uses lazy imports inside static method to break circular import between models.py (foundation layer) and dedup_normalizer.py (web layer)
- run_retroactive_dedup calls normalize_company/normalize_title directly instead of the backward-compat wrapper to avoid a potential circular import chain within dedup_normalizer.py
- get_config_snapshot lives in db_helpers.py (not a new module) since it pairs naturally with the per-request connection helper pattern
- Background threads take config snapshot at job-start via get_config_snapshot(app) rather than reading individual keys from shared app.config dict

### Decisions Made in Phase 20 Plan 03

- standalone_connection() extracted to db_helpers.py (after close_db) — single context manager for all background/CLI sqlite3 connections; sets Row factory + WAL mode, guarantees close via contextlib
- Files with sqlite3.Connection type hints or sqlite3.IntegrityError/OperationalError in except clauses retain `import sqlite3`; files that only used it for sqlite3.connect() have the import removed
- dashboard.py _run_batch_*_bg outer exception handlers changed from _fail_session(conn) to _mark_session_error(db_path) — conn is not in scope in the outer except block
- resume_generator.py mid-function close/reopen pattern eliminated — generate_resume_multi() uses standalone_connection internally so no connection conflict; single with standalone_connection covers entire function body

### Decisions Made in Phase 21 Plan 01

- Patch upsert_company at source module (job_finder.web.ats_scanner) not blueprint namespace — locally imported functions inside a route body never appear in the blueprint's module namespace, so patch must target the definition site
- TESTING: True in top-level JF_CONFIG dict activates probe_ats_slugs/run_ats_scan guards; probe_single_company has no TESTING guard and requires explicit unittest.mock.patch
- Blueprint test fixture chain: migrated_db -> companies_app (with TESTING=True) -> companies_client; same pattern as detections blueprint

### Decisions Made in Phase 22 Plan 02

- Deferred import of generate_resume_multi inside _generate_resume_background avoids circular import at module load time (resume_multi_version imports from resume_generator at top level)
- resume_multi_version.py adds explicit `import sqlite3` to preserve patch surface for TestThreadSafety.test_each_variant_thread_opens_own_sqlite_connection
- TestScoreThresholdDispatch patches at resume_multi_version.generate_resume_multi since deferred import binds name there during execution

### Blockers/Concerns

None.

---
*Last session: 2026-03-27 — Phase 22 Plan 02 complete: resume_generator.py split into resume_multi_version.py. 1562 tests pass.*
