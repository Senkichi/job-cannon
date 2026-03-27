---
gsd_state_version: 1.0
milestone: v1.5
milestone_name: Multi-Provider Model Routing
status: Defining requirements
last_updated: "2026-03-27"
progress:
  total_phases: 0
  completed_phases: 0
  total_plans: 0
  completed_plans: 0
---

# State

## Current Position

Phase: Not started (defining requirements)
Plan: —
Status: Defining requirements
Last activity: 2026-03-27 — Milestone v1.5 started

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-27)

**Core value:** Surface the best-fit jobs fast and keep the application pipeline visible
**Current focus:** v1.5 Multi-Provider Model Routing

## Performance Metrics

**Velocity:**

- Total plans completed: 0 (this milestone)

*Updated after each plan completion*

## Accumulated Context

### Architectural Decisions (from v1.0)

- HTMX fragment routes MUST check HX-Request header and return full page for direct browser access
- Status dropdown: hx-target=this hx-swap=outerHTML on the select element itself
- Accordion: compact row + hidden `<tr data-expand-slot>` placeholder pairs
- Use hx-on:click not onclick for event.stopPropagation() in HTMX 2.x
- Dismiss/return responses: ('', 200) not 204 — HTMX requires 200 for outerHTML swap
- Migrations stored as list of discrete SQL strings (not semicolon-delimited)
- CREATE TABLE IF NOT EXISTS for idempotent migration
- sort_by validated against Python allowlist before SQL interpolation

### Migration Context

- Implementation plan: `docs/superpowers/plans/2026-03-24-migration-and-stabilization.md`
- Source repo (job-finder): `<other-repo>` (retired, read-only reference)
- Phase 13 = Chunk 1 (Tasks 1-6): Planning doc updates — 6 files, surgical edits
- Phase 14 = Chunk 2 (Tasks 7-11): Data migration — 8 files, config merge, schema check, validation
- config.yaml MUST be edited with Edit tool only (never Write — wiped 3 times previously)

### Blockers/Concerns

None.

### Decisions Made in Phase 13

- STACK.md and INTEGRATIONS.md cleaned of all "(Phase N)" annotations -- features are operational, not future
- Verification sweep confirmed zero stale phase references in codebase docs

### Decisions Made in Phase 14

- All 8 data files gitignored -- no per-task commits for data migration (Plan 01)
- Config merge via copy + Edit append -- preserves job-finder values while adding cannon sections (Plan 01)

### Decisions Made in Phase 17

- Gate _setup_file_logging() inside existing if not _is_testing block — keeps all production-only side effects together (Plan 01)
- scan() route uses two-layer exception handling — inner try for scan logic, render_template outside so TemplateErrors propagate as 500 (Plan 01)
- Date filter uses form-level hx-trigger with input event for #filter-date-from and #filter-date-to — element-level triggers would own their own HTMX request (Plan 01)

### Decisions Made in Phase 18

- Reuse batch_score_sessions table with session_type='sync' for async sync — no new table needed, consistent pattern (Plan 01)
- Store sync results in existing columns: scored=jobs_new, total=total_fetched, skipped=error_count — avoids JSON in error_msg (Plan 01)
- Simplified phase labels (running->gmail->done) since trigger_sync is synchronous and opaque — no sub-phase callbacks available (Plan 01)
- Flask's native app.config["TESTING"] must be set explicitly in test fixtures, separate from JF_CONFIG["TESTING"] (Plan 01)

### Decisions Made in Phase 22

- Re-export all moved symbols from ats_scanner.py for backward compatibility — avoids updating 8+ callers that import from ats_scanner (Plan 01)
- Python module singleton ensures patch("ats_scanner.requests.get") still works for probe_ats_slugs tests after probe functions moved to ats_prober.py (Plan 01)
- caplog logger target updated to ats_prober in test_log_levels.py — _handle_scan_error logs via its own module's logger, not ats_scanner (Plan 01)
- batch_scoring_bp and sync_bp use url_prefix='/dashboard' — multiple blueprints can share url_prefix, no URL changes needed (Plan 05)
- Background thread functions (_run_batch_haiku_bg, _run_batch_sonnet_bg, _run_sync_bg) colocated with their blueprint, not extracted to shared utilities (Plan 05)
- test_async_sync.py needed no import changes — uses Flask test client URLs only, no direct function imports (Plan 05)
- _PROFILE_PATH duplicated in all three profile modules for self-containment; no shared constant needed (Plan 06)
- rec_app fixture patches both profile_mod and profile_recs_mod _PROFILE_PATH — index route reads from profile module, recommendation routes read from profile_recommendations module (Plan 06)
- Multiple blueprints sharing url_prefix='/profile' — Flask supports this, all routes remain at same URLs (Plan 06)

### Decisions Made in Phase 23

- Batch prefetch before scoring loop using WHERE dedup_key IN — O(1) DB round-trip per batch instead of O(N) (Plan 01)
- Missing key handling: warning logged and key skipped, consistent with previous per-job behavior (Plan 01)
- Enrichment path unchanged: enrich_job updates in-memory job_row dict after prefetch, which is correct since enrichment writes to DB directly via conn (Plan 01)

---
*Last session: 2026-03-26 — Completed Phase 23 Plan 01 (N+1 Batch Prefetch in Scoring Runner)*
