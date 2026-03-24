---
gsd_state_version: 1.0
milestone: v1.1
milestone_name: Port job-finder Improvements
status: v1.1 milestone complete
last_updated: "2026-03-23T23:59:00.000Z"
progress:
  total_phases: 7
  completed_phases: 7
  total_plans: 8
  completed_plans: 8
---

# State

## Current Position

Phase: 12 (verification-backfill) — Complete
Plan: Complete

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-23)

**Core value:** Surface the best-fit jobs fast and keep the application pipeline visible
**Current focus:** Phase 12 — verification-backfill (final v1.1 phase)

## Accumulated Context

### Architectural Decisions (from v1.0)

- HTMX fragment routes MUST check HX-Request header and return full page for direct browser access
- Status dropdown: hx-target=this hx-swap=outerHTML on the select element itself
- Accordion: compact row + hidden `<tr data-expand-slot>` placeholder pairs
- Use hx-on:click not onclick for event.stopPropagation() in HTMX 2.x
- Dismiss/return responses: ('', 200) not 204 — HTMX requires 200 for outerHTML swap
- Detail-inline route registered BEFORE catch-all to avoid Flask route shadowing
- Migrations stored as list of discrete SQL strings (not semicolon-delimited)
- CREATE TABLE IF NOT EXISTS for idempotent migration
- Stale detector creates own sqlite3 connection (thread-safe for APScheduler)
- sort_by validated against Python allowlist before SQL interpolation
- cost_gate returns bool — callers decide whether to raise BudgetExceededError
- Sonnet skips if jd_full absent (no cost without full JD)
- Batch score skips already-scored jobs (haiku_score IS NOT NULL)

### Port-Specific Context

- Design spec: `docs/superpowers/specs/2026-03-23-port-job-finder-improvements-design.md`
- Implementation plan: `docs/superpowers/plans/2026-03-23-port-job-finder-improvements.md`
- Source repo (job-finder): `<other-repo>`
- 3 cannon-only test files to preserve: test_docx_formatter.py, test_drive_status.py, test_drive_uploader.py
- db.py rewrite is the riskiest change (~1450 diff lines) — Phase 7
- FILT-03 (multi-select IN clause) folded into db.py rewrite per design spec Wave 2.1

### Decisions Made in Phase 11

- Scheduler arg swap fixed at all 3 call sites + _import_detection adapter lambda added
- ScoringResult unwrap fixed via .status/.data attributes in all 3 pipeline_runner locations
- test_scoring.py mocks updated to return ScoringResult (Rule 1 auto-fix)
- Worktree rebased onto master before execution (required to get bugs that existed post-phase-8)

### Decisions Made in Phase 9

- HX-Request guards redirect to /jobs/ (full page) for direct browser access on all fragment routes
- status list passed as-is when multiple values, single string when one value, None when none — matching db.py interface
- _safe_float/_safe_int call abort(400) rather than returning None for invalid input
- guidelines_path requires 4 parent levels: blueprints -> web -> job_finder -> repo_root
- scoring_orchestrator replaces direct haiku_scorer/sonnet_evaluator calls in jobs.py and dashboard.py
- Status filter replaced: dropdown removed, checkbox pill row added with "All" toggle

### Completed Todos (from v1.0)

- [x] Replace status filter dropdown with multi-select checkboxes — DONE Phase 9
- [x] Fix job board not refreshing when date filter is cleared — DONE Phase 9 (change from:input trigger)

### Blockers/Concerns

None.

---
*Last session: 2026-03-23 — Completed Phase 12 (verification-backfill, milestone complete)*
