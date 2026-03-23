# Roadmap: Job Cannon

## Milestones

- ✅ **v1.0 Foundation + AI Scoring + Pipeline Automation** - Phases 1-5 (shipped 2026-03-23)
- 🚧 **v1.1 Port job-finder Improvements** - Phases 6-10 (in progress)

## Phases

<details>
<summary>✅ v1.0 Foundation + AI Scoring + Pipeline Automation (Phases 1-5) - SHIPPED 2026-03-23</summary>

### Phase 1: Foundation
**Goal**: Users can ingest jobs and view them in a filterable, interactive job board
**Plans**: 11 plans

Plans:
- [x] All 11 foundation plans complete

### Phase 2: AI Scoring
**Goal**: Jobs are automatically scored by two-tier AI pipeline with cost tracking
**Plans**: 5 plans

Plans:
- [x] All 5 AI scoring plans complete

### Phase 3: Pipeline Automation
**Goal**: Application status tracks automatically from email signals
**Plans**: 2 plans

Plans:
- [x] All 2 pipeline automation plans complete

### Phase 4: Resume Generation
**Goal**: (Deferred — context gathered, not started)
**Plans**: TBD

### Phase 5: Intelligence
**Goal**: (Deferred — not started)
**Plans**: TBD

</details>

---

### 🚧 v1.1 Port job-finder Improvements (In Progress)

**Milestone Goal:** Port ~30 improvements from job-finder, implement multi-select status filter, retire job-finder repo.

**Phase Numbering:** Continues from v1.0. Phases 6-10.

- [x] **Phase 6: Foundation Types & Constants** - New utility modules, constants, delete dead files (completed 2026-03-23)
- [x] **Phase 7: Core Module Refactors** - db.py rewrite, scoring orchestrator, description formatter, claude_client hardening (completed 2026-03-23)
- [x] **Phase 8: Consumers** - Scorers, pipeline runner, scheduler updated to new APIs (completed 2026-03-23)
- [x] **Phase 9: Blueprints + Multi-Select Filter** - Fragment guards, safe params, batch timeout, filter UI (completed 2026-03-23)
- [x] **Phase 10: Safety, Tests & Cleanup** - Status validation, test updates, dead code removal, db migration (completed 2026-03-23)
- [ ] **Phase 11: Fix Critical Runtime Bugs** - Scheduler arg swap, ScoringResult unwrap, regression tests (gap closure)
- [ ] **Phase 12: Milestone Verification Backfill** - Missing artifacts, requirements checkboxes, state updates (gap closure)

## Phase Details

### Phase 6: Foundation Types & Constants
**Goal**: The codebase has correct utility modules and constants as foundation for all subsequent changes
**Depends on**: Phase 5 (v1.0 complete)
**Requirements**: SAFE-06, CLN-01
**Success Criteria** (what must be TRUE):
  1. App starts and all existing tests pass after deleting utils.py and output/ (main.py and scoring/ deferred to Phase 10)
  2. Flask app generates a non-hardcoded secret key on each startup (secrets.token_hex(32))
  3. json_utils.py and scoring_types.py exist and are importable from their new paths
  4. PIPELINE_STATUSES is a tuple and VALID_PIPELINE_STATUSES frozenset exists in blueprints/__init__.py
**Plans**: 2 plans

Plans:
- [ ] 06-01-PLAN.md — Create json_utils.py, migrate imports, delete utils.py and output/
- [ ] 06-02-PLAN.md — Create scoring_types.py, harden constants, fix Flask secret key

**UI hint**: no

### Phase 7: Core Module Refactors
**Goal**: db.py, scoring orchestrator, description formatter, and claude_client are fully refactored to the job-finder versions
**Depends on**: Phase 6
**Requirements**: REFAC-01, REFAC-02, REFAC-03, REFAC-04, REFAC-05, SAFE-01, SAFE-02, FILT-03
**Success Criteria** (what must be TRUE):
  1. db.py exposes module-level functions (no JobDB class) and all callers pass conn as first arg
  2. All db queries use explicit column constants instead of SELECT *
  3. Job upsert deduplicates descriptions, keeps the longer, and eagerly promotes jd_full for descriptions >200 chars
  4. A scoring_orchestrator module exists and centralizes haiku/sonnet scoring with persist helpers
  5. Claude API calls accept a configurable timeout (default 120s) and unknown model pricing falls back with a warning instead of raising KeyError
**Plans**: 3 plans

Plans:
- [ ] 07-01-PLAN.md — Rewrite db.py: remove JobDB class, add column constants, smart merging, persist helpers, multi-select
- [ ] 07-02-PLAN.md — Harden claude_client (timeout, pricing fallback), create description_formatter, update app factory
- [ ] 07-03-PLAN.md — Create scoring_orchestrator, update ats_scanner to remove JobDB usage

### Phase 8: Consumers
**Goal**: All modules that call db.py, scorers, or scheduler are updated to use the new APIs
**Depends on**: Phase 7
**Requirements**: REFAC-06, SAFE-03, CLN-02, CLN-03
**Success Criteria** (what must be TRUE):
  1. haiku_scorer and sonnet_evaluator return ScoringResult instead of raw dict
  2. Scorer profile parameter is named experience_profile in all callers
  3. Scheduler uses _make_simple_job/_make_tracked_job factory functions (no boilerplate closures)
  4. Gmail message fetch stops at 500 messages and does not paginate beyond that cap
**Plans**: TBD

### Phase 9: Blueprints + Multi-Select Filter
**Goal**: Users can filter jobs by multiple pipeline statuses simultaneously, and all blueprint safety improvements are in place
**Depends on**: Phase 8
**Requirements**: BP-01, BP-02, BP-03, BP-04, BP-05, FILT-01, FILT-02, FILT-04, SAFE-05
**Success Criteria** (what must be TRUE):
  1. User can click multiple status pills on the job board and see only jobs matching any selected status
  2. An "All" toggle button on the filter bar checks or unchecks all status pills and refreshes the table
  3. Accessing a fragment route directly in the browser returns a full page (not a bare fragment)
  4. Malformed query string params (e.g., non-numeric score filter) return HTTP 400 instead of a 500 error
  5. Clearing the date filter input triggers a job table refresh without requiring a separate submit action
**Plans**: TBD
**UI hint**: yes

### Phase 10: Safety, Tests & Cleanup
**Goal**: All tests pass against the new APIs, pipeline status transitions are validated, and dead code is fully removed
**Depends on**: Phase 9
**Requirements**: SAFE-04, SAFE-05, CLN-04, CLN-05, CLN-06
**Success Criteria** (what must be TRUE):
  1. Setting an invalid pipeline status is rejected (validated against VALID_PIPELINE_STATUSES frozenset)
  2. All 266+ tests pass including new multi-select filter test and cannon-only test files
  3. Job model has no to_dict() method and no dead scoring/ package or main.py exists in the repo
  4. companies table has company_size and industry columns (migration at user_version 15)
**Plans**: TBD

### Phase 11: Fix Critical Runtime Bugs
**Goal**: Two critical runtime bugs (scheduler arg swap, ScoringResult unwrap) are fixed with regression tests, restoring all E2E flows
**Depends on**: Phase 10
**Requirements**: REFAC-06, SAFE-03, CLN-02, CLN-06
**Gap Closure:** Closes critical bugs from v1.1 milestone audit
**Success Criteria** (what must be TRUE):
  1. `run_ingestion(config, db_path)` is called with correct argument order in both scheduler closures (lines 146, 393)
  2. `pipeline_runner._run_haiku_scoring` and `_run_sonnet_evaluation` access ScoringResult fields via attribute access (not .get())
  3. Regression tests exist that would catch both bugs (scheduler args, ScoringResult unwrap)
  4. Scheduled ingestion and "Sync Now" complete without AttributeError
**Plans**: TBD

### Phase 12: Milestone Verification Backfill
**Goal**: All v1.1 phases have complete artifacts (VERIFICATION.md, SUMMARY.md), requirements checkboxes are accurate, and planning state is current
**Depends on**: Phase 11
**Requirements**: CLN-03, SAFE-04, CLN-04, CLN-05, CLN-01, BP-01, BP-02, BP-03, BP-04, BP-05, FILT-01, FILT-02, FILT-04, SAFE-05
**Gap Closure:** Closes artifact gaps from v1.1 milestone audit
**Success Criteria** (what must be TRUE):
  1. Phases 8, 9, 10 each have VERIFICATION.md confirming their requirements are met
  2. All 27 REQUIREMENTS.md checkboxes reflect actual satisfaction status
  3. STATE.md completed_phases and ROADMAP.md progress table are current
  4. Stale docstrings in test_db_helpers.py and pipeline_runner.py are fixed
**Plans**: TBD

---

## Progress

| Phase | Milestone | Plans Complete | Status | Completed |
|-------|-----------|----------------|--------|-----------|
| 1. Foundation | v1.0 | 11/11 | Complete | 2026-03-23 |
| 2. AI Scoring | v1.0 | 5/5 | Complete | 2026-03-23 |
| 3. Pipeline Automation | v1.0 | 2/2 | Complete | 2026-03-23 |
| 4. Resume Generation | v1.0 | 0/? | Deferred | - |
| 5. Intelligence | v1.0 | 0/? | Deferred | - |
| 6. Foundation Types & Constants | v1.1 | 2/2 | Complete | 2026-03-23 |
| 7. Core Module Refactors | v1.1 | 3/3 | Complete | 2026-03-23 |
| 8. Consumers | v1.1 | —/— | Complete (bugs found) | 2026-03-23 |
| 9. Blueprints + Multi-Select Filter | v1.1 | —/— | Complete | 2026-03-23 |
| 10. Safety, Tests & Cleanup | v1.1 | —/— | Complete | 2026-03-23 |
| 11. Fix Critical Runtime Bugs | v1.1 | 0/? | Not started | - |
| 12. Milestone Verification Backfill | v1.1 | 0/? | Not started | - |
