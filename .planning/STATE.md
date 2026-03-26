---
gsd_state_version: 1.0
milestone: v1.3
milestone_name: Fixes & Improvements
status: Phase complete — ready for verification
last_updated: "2026-03-26T00:40:00Z"
progress:
  total_phases: 4
  completed_phases: 1
  total_plans: 1
  completed_plans: 1
---

# State

## Current Position

Phase: 15 (Parser Fixes) — EXECUTING
Plan: 1 of 1

## Progress Bar

```
Phase 15 [ ] Parser Fixes
Phase 16 [ ] Homepage Discovery
Phase 17 [ ] Code Quality
Phase 18 [ ] Async Sync
```

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-25)

**Core value:** Surface the best-fit jobs fast and keep the application pipeline visible
**Current focus:** Phase 15 — Parser Fixes

## Performance Metrics

**Velocity:**

- Total plans completed: 3 (this milestone)
- Phase 14-01: 2min (2 tasks, 8 files)
- Average duration: ~2min
- Total execution time: ~6min

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

### v1.3 Roadmap Decisions

- **Phase 15 first:** Two parsers (Glassdoor, Indeed) are completely dark — 29 emails/run yield 0 jobs. Correctness regression ships before all other work.
- **Phase 16 independent:** Homepage discovery has no dependency on parser fixes; can run in parallel but sequenced after 15 for clarity.
- **Phase 17 independent:** Code quality + date filter fix have zero cross-dependencies — slot any time.
- **Phase 18 depends on Phase 15:** Async sync is meaningless if the parsers feeding the sync are dark.
- **SerpAPI replaces DDG:** Google CSE deprecated for new integrations Jan 2026; DDG endpoint confirmed broken. SerpAPI `engine=google` is the only viable option — API key already in config.yaml.
- **`homepage_probe_attempted_at` column required:** Needed for DISC-04 retry-avoidance. Must add via migration before scheduler job is useful.
- **Glassdoor positional extraction:** Strip `\d+\.\d+ ★` suffix from outer span text — do not navigate nested children.
- **SerpAPI `_SKIP_DOMAINS`:** Must include glassdoor.com, crunchbase.com, bloomberg.com, zoominfo.com, pitchbook.com, linkedin.com before shipping DISC-03.
- **Async sync 30-min timeout:** Replicates batch-score safety net — not optional, must ship from day one (not retrofitted).
- **Async sync double-click guard:** Check for existing `running` session before spawning new thread.
- **Background thread app context:** Pass `current_app._get_current_object()` before thread start.

### Research Flags (from SUMMARY.md)

- SerpAPI quota: 250/month vs 100/month discrepancy — verify against active plan before setting `batch_cap`
- All v1.3 patterns already exist in codebase — no architectural unknowns, no new dependencies

### Blockers/Concerns

None.

### Decisions Made in Phase 13

- STACK.md and INTEGRATIONS.md cleaned of all "(Phase N)" annotations -- features are operational, not future
- Verification sweep confirmed zero stale phase references in codebase docs

### Decisions Made in Phase 14

- All 8 data files gitignored -- no per-task commits for data migration (Plan 01)
- Config merge via copy + Edit append -- preserves job-finder values while adding cannon sections (Plan 01)

### Decisions Made in Phase 15

- Glassdoor positional fallback: CSS-class extraction first, fall back to span.string + p tags positionally when CSS-class title extraction yields None
- Company name extracted from first span.string not matching rating pattern (r'^\s*\d+\.\d+\s*★?\s*$')
- Indeed rc/clk/dl: dual URL pattern matching in _parse_plaintext; _extract_job_id (jk= param) passed as id_fn for new format
- Added _SUMMARY_COUNT_RE and _SEE_MATCHING_RE noise filters for rc/clk preamble lines ("Jobs 1-2 of 2 new jobs", "See matching results on Indeed: URL")
- Pre-existing tests encoding "wrong CSS = 0 jobs" assumption updated to reflect correct new behavior (positional fallback succeeds)
- Phase 15-01: 15min (3 tasks, 3 files, 18 new tests, +28 assertions)

---
*Last session: 2026-03-26 — Completed Phase 15 Plan 01 (Parser Fixes: Glassdoor positional + Indeed rc/clk)*
