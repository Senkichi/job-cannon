---
gsd_state_version: 1.0
milestone: v1.4
milestone_name: Tech Debt Sweep
status: executing
last_updated: "2026-03-27T02:10:52.473Z"
last_activity: 2026-03-27
progress:
  total_phases: 5
  completed_phases: 1
  total_plans: 4
  completed_plans: 2
  percent: 50
---

# State

## Current Position

Phase: 20 (Surgical Fixes) — EXECUTING
Plan: 2 of 3
Status: Executing Phase 20
Last activity: 2026-03-27 -- Phase 20 Plan 01 complete (FIX-01, FIX-02)

Progress: [█████░░░░░] 50%

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-26)

**Core value:** Surface the best-fit jobs fast and keep the application pipeline visible
**Current focus:** Phase 20 — Surgical Fixes

## Performance Metrics

**Velocity:**

- Total plans completed: 6 (including this session)
- Phase 14-01: 2min (2 tasks, 8 files)
- Phase 17-01: 8min (3 tasks, 6 files)
- Phase 18-01: 15min (1 task TDD, 5 files)
- Phase 20-01: 15min (2 tasks, 15 files)
- Average duration: ~10min
- Total execution time: ~40min

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

### Blockers/Concerns

None.

---
*Last session: 2026-03-27 — Phase 20 Plan 01 complete: Indeed dedup + 4 symbol renames. 1533 tests pass.*
