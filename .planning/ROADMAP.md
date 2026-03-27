# Roadmap: Job Cannon

## Milestones

- ✅ **v1.0 Foundation + AI Scoring + Pipeline Automation** — Phases 1-5 (shipped 2026-03-23)
- ✅ **v1.1 Port job-finder Improvements** — Phases 6-12 (shipped 2026-03-24)
- ✅ **v1.2 Migration & Stabilization** — Phases 13-14 (shipped 2026-03-24)
- ✅ **v1.3 Fixes & Improvements** — Phases 15-18 (shipped 2026-03-26)
- 🚧 **v1.4 Tech Debt Sweep** — Phases 19-23 (in progress)

## Phases

<details>
<summary>✅ v1.0 Foundation + AI Scoring + Pipeline Automation (Phases 1-5) — SHIPPED 2026-03-23</summary>

- [x] Phase 1: Foundation (11/11 plans) — completed 2026-03-23
- [x] Phase 2: AI Scoring (5/5 plans) — completed 2026-03-23
- [x] Phase 3: Pipeline Automation (2/2 plans) — completed 2026-03-23
- [x] Phase 4: Resume Generation — inherited from job-finder, operational
- [x] Phase 5: Intelligence (interview prep + rejection analysis + notifications) — inherited from job-finder, operational. Semantic similarity/clustering/recommendations dropped.

</details>

<details>
<summary>✅ v1.1 Port job-finder Improvements (Phases 6-12) — SHIPPED 2026-03-24</summary>

- [x] Phase 6: Foundation Types & Constants (2/2 plans) — completed 2026-03-23
- [x] Phase 7: Core Module Refactors (3/3 plans) — completed 2026-03-23
- [x] Phase 8: Consumers (1/1 plan) — completed 2026-03-23
- [x] Phase 9: Blueprints + Multi-Select Filter (1/1 plan) — completed 2026-03-23
- [x] Phase 10: Safety, Tests & Cleanup (1/1 plan) — completed 2026-03-23
- [x] Phase 11: Fix Critical Runtime Bugs (1/1 plan) — completed 2026-03-23
- [x] Phase 12: Milestone Verification Backfill (2/2 plans) — completed 2026-03-24

</details>

<details>
<summary>✅ v1.2 Migration & Stabilization (Phases 13-14) — SHIPPED 2026-03-24</summary>

- [x] Phase 13: Planning Doc Corrections (2/2 plans) — completed 2026-03-24
- [x] Phase 14: Data Migration & Validation (2/2 plans) — completed 2026-03-24

</details>

<details>
<summary>✅ v1.3 Fixes & Improvements (Phases 15-18) — SHIPPED 2026-03-26</summary>

- [x] Phase 15: Parser Fixes (1/1 plan) — completed 2026-03-26
- [x] Phase 16: Homepage Discovery (2/2 plans) — completed 2026-03-26
- [x] Phase 17: Code Quality (1/1 plan) — completed 2026-03-26
- [x] Phase 18: Async Sync (1/1 plan) — completed 2026-03-26

</details>

### 🚧 v1.4 Tech Debt Sweep (In Progress)

**Milestone Goal:** Resolve all outstanding tech debt — move completed todos, apply surgical fixes, add test coverage, decompose god-object modules, and batch N+1 queries.

- [x] **Phase 19: Housekeeping** — Move 11 completed todos from pending/ to done/ (completed 2026-03-27)
- [x] **Phase 20: Surgical Fixes** — Mechanical fixes: Indeed dedup, renames, models.py layer violation, SQLite helper, config thread-safety (completed 2026-03-27)
- [x] **Phase 21: Test Coverage** — Companies blueprint test suite (completed 2026-03-27)
- [ ] **Phase 22: Module Splits** — Decompose 7 god-object modules along responsibility boundaries
- [ ] **Phase 23: N+1 Batching** — Batch 5 N+1 query patterns across scoring and detection pipelines

## Phase Details

### Phase 19: Housekeeping
**Goal**: Completed planning todos are filed in done/ so the pending/ directory reflects only unresolved work
**Depends on**: Phase 18
**Requirements**: HOUSE-01
**Success Criteria** (what must be TRUE):
  1. All 11 completed todos are present in .planning/todos/done/
  2. .planning/todos/pending/ contains no todos that describe already-completed work
  3. pending/ and done/ directories are browsable and their contents match expectations
**Plans**: 1 plan
Plans:
- [x] 19-01-PLAN.md -- Move 11 completed todos from pending/ to done/

### Phase 20: Surgical Fixes
**Goal**: Five targeted code quality issues are resolved — duplication removed, naming aligned, layer boundary enforced, boilerplate extracted, and background thread config made safe
**Depends on**: Phase 19
**Requirements**: FIX-01, FIX-02, FIX-03, FIX-04, FIX-05
**Success Criteria** (what must be TRUE):
  1. Indeed parser uses shared `_common.is_meta_email` with `extra_patterns`; the private `_is_meta_email` duplicate is gone
  2. All four renamed symbols (`run_homepage_discovery`, `run_sync_now`, `merge_guidelines_into_guide`, `FIELD_LABELS`) are used consistently across call sites — no stale names remain
  3. `Job.normalized_dedup_key()` is a static method on the `Job` dataclass; no web-layer callers hold the old function
  4. `standalone_connection()` context manager exists in `db_helpers.py` and replaces the 35+ boilerplate `sqlite3.connect` / `row_factory` / `WAL` call sites
  5. `get_config_snapshot()` exists and is used by background threads (scheduler, stale detector) instead of direct `config` dict access
**Plans**: 3 plans
Plans:
- [x] 20-01-PLAN.md -- Deduplicate Indeed meta-email detection + rename four symbols
- [x] 20-02-PLAN.md -- Move normalized_dedup_key to Job static method + add get_config_snapshot
- [x] 20-03-PLAN.md -- Extract standalone_connection and sweep 37 call sites

### Phase 21: Test Coverage
**Goal**: The companies blueprint is covered by an automated test suite with the same depth as other blueprint tests
**Depends on**: Phase 20
**Requirements**: TEST-01
**Success Criteria** (what must be TRUE):
  1. `tests/test_companies.py` exists and passes with `uv run pytest tests/test_companies.py`
  2. All six routes (index, expand, collapse, toggle, update_slug, retry) have at least one test
  3. Overall test suite still passes (no regressions introduced)
**Plans**: 1 plan
Plans:
- [x] 21-01-PLAN.md -- Companies blueprint test suite (all 8 routes)

### Phase 22: Module Splits
**Goal**: Seven god-object modules are decomposed into focused files; no file exceeds its target LOC ceiling and all imports resolve without circular dependencies
**Depends on**: Phase 21
**Requirements**: SPLIT-01, SPLIT-02, SPLIT-03, SPLIT-04, SPLIT-05, SPLIT-06, SPLIT-07
**Success Criteria** (what must be TRUE):
  1. `ats_scanner.py`, `ats_detection.py`, and `ats_prober.py` each exist with their designated responsibilities; combined LOC does not exceed the pre-split total
  2. `data_enricher.py`, `enrichment_tiers.py`, and `company_enricher.py` each exist with cohesive scopes
  3. `dashboard.py`, `batch_scoring.py`, and `sync.py` blueprints are registered and all dashboard/batch/sync routes respond correctly
  4. `resume_generator.py` and `resume_multi_version.py` exist; multi-version synthesis logic is fully in the new module
  5. `profile.py`, `resume_review.py`, and `profile_recommendations.py` blueprints are registered and all profile/resume-review/recommendations routes respond correctly
  6. `pipeline_runner.py` and `scoring_runner.py` exist with scoring orchestration in the new module
  7. `settings.py` and `guidelines.py` blueprints are registered and all settings/guidelines routes respond correctly
  8. Full test suite passes with no regressions after all splits
**Plans**: 7 plans
Plans:
- [ ] 22-01-PLAN.md -- Split ats_scanner.py into ats_detection.py + ats_prober.py + ats_scanner.py
- [x] 22-02-PLAN.md -- Split resume_generator.py into resume_generator.py + resume_multi_version.py
- [ ] 22-03-PLAN.md -- Split settings.py into settings.py + guidelines.py blueprint
- [ ] 22-04-PLAN.md -- Split data_enricher.py into enrichment_tiers.py + company_enricher.py + data_enricher.py
- [ ] 22-05-PLAN.md -- Split dashboard.py into batch_scoring.py + sync.py + dashboard.py blueprints
- [ ] 22-06-PLAN.md -- Split profile.py into resume_review.py + profile_recommendations.py + profile.py blueprints
- [ ] 22-07-PLAN.md -- Split pipeline_runner.py into scoring_runner.py + pipeline_runner.py

### Phase 23: N+1 Batching
**Goal**: Five N+1 query patterns are replaced with batch queries, reducing per-job DB round-trips during scoring and stale detection to O(1) per batch
**Depends on**: Phase 22
**Requirements**: BATCH-01, BATCH-02, BATCH-03, BATCH-04, BATCH-05
**Success Criteria** (what must be TRUE):
  1. Haiku scoring reads a single `SELECT ... WHERE dedup_key IN (?)` per batch instead of one query per job
  2. Sonnet evaluation reads use the same batched pattern as BATCH-01
  3. Stale detector issues a single `UPDATE ... WHERE dedup_key IN (?)` per batch run
  4. Dashboard cancellation check fires once per batch, not once per job
  5. Session counter increments once after a batch completes, not after each job
  6. Full test suite passes and no functional scoring or pipeline behavior changed
**Plans**: TBD

## Progress

| Phase | Milestone | Plans Complete | Status | Completed |
|-------|-----------|----------------|--------|-----------|
| 1. Foundation | v1.0 | 11/11 | Complete | 2026-03-23 |
| 2. AI Scoring | v1.0 | 5/5 | Complete | 2026-03-23 |
| 3. Pipeline Automation | v1.0 | 2/2 | Complete | 2026-03-23 |
| 4. Resume Generation | v1.0 | — | Inherited (operational) | — |
| 5. Intelligence | v1.0 | — | Inherited (operational) | — |
| 6. Foundation Types & Constants | v1.1 | 2/2 | Complete | 2026-03-23 |
| 7. Core Module Refactors | v1.1 | 3/3 | Complete | 2026-03-23 |
| 8. Consumers | v1.1 | 1/1 | Complete | 2026-03-23 |
| 9. Blueprints + Multi-Select Filter | v1.1 | 1/1 | Complete | 2026-03-23 |
| 10. Safety, Tests & Cleanup | v1.1 | 1/1 | Complete | 2026-03-23 |
| 11. Fix Critical Runtime Bugs | v1.1 | 1/1 | Complete | 2026-03-23 |
| 12. Milestone Verification Backfill | v1.1 | 2/2 | Complete | 2026-03-24 |
| 13. Planning Doc Corrections | v1.2 | 2/2 | Complete | 2026-03-24 |
| 14. Data Migration & Validation | v1.2 | 2/2 | Complete | 2026-03-24 |
| 15. Parser Fixes | v1.3 | 1/1 | Complete | 2026-03-26 |
| 16. Homepage Discovery | v1.3 | 2/2 | Complete | 2026-03-26 |
| 17. Code Quality | v1.3 | 1/1 | Complete | 2026-03-26 |
| 18. Async Sync | v1.3 | 1/1 | Complete | 2026-03-26 |
| 19. Housekeeping | v1.4 | 1/1 | Complete   | 2026-03-27 |
| 20. Surgical Fixes | v1.4 | 3/3 | Complete    | 2026-03-27 |
| 21. Test Coverage | v1.4 | 1/1 | Complete    | 2026-03-27 |
| 22. Module Splits | v1.4 | 1/7 | In Progress|  |
| 23. N+1 Batching | v1.4 | 0/TBD | Not started | - |
