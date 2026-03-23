---
gsd_state_version: 1.0
milestone: v1.1
milestone_name: Port job-finder Improvements
status: Ready to plan
last_updated: "2026-03-23T20:22:42.073Z"
progress:
  total_phases: 5
  completed_phases: 0
  total_plans: 5
  completed_plans: 0
---

# State

## Current Position

Phase: 8
Plan: Not started

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-23)

**Core value:** Surface the best-fit jobs fast and keep the application pipeline visible
**Current focus:** Phase 07 — Core Module Refactors

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

### Pending Todos (from v1.0)

- Replace status filter dropdown with multi-select checkboxes (addressed in v1.1 Phase 9)
- Fix job board not refreshing when date filter is cleared (addressed in v1.1 Phase 9)

### Blockers/Concerns

None yet.

---
*Last updated: 2026-03-23*
