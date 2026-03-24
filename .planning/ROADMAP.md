# Roadmap: Job Cannon

## Milestones

- ✅ **v1.0 Foundation + AI Scoring + Pipeline Automation** — Phases 1-5 (shipped 2026-03-23)
- ✅ **v1.1 Port job-finder Improvements** — Phases 6-12 (shipped 2026-03-24)
- 🚧 **v1.2 Migration & Stabilization** — Phases 13-14 (in progress)

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

### v1.2 Migration & Stabilization (In Progress)

**Milestone Goal:** Make job-cannon fully operational with real data and accurate planning docs — complete the transition from job-finder.

- [ ] **Phase 13: Planning Doc Corrections** - Correct all planning docs to reflect Phase 4/5 as operational, fix stats, remove stale references
- [ ] **Phase 14: Data Migration & Validation** - Migrate all data files from job-finder, merge config, verify schema, validate app with real data

## Phase Details

### Phase 13: Planning Doc Corrections
**Goal**: Planning documentation accurately reflects the project's actual state — Phase 4/5 features operational, test counts correct, no stale "deferred" references
**Depends on**: Phase 12
**Requirements**: DOCS-01, DOCS-02, DOCS-03, DOCS-04
**Success Criteria** (what must be TRUE):
  1. ROADMAP.md shows Phase 4 and Phase 5 as operational (not deferred)
  2. CLAUDE.md project overview reads "Job Cannon" (not "Job Finder") and test count matches actual pytest output
  3. Grep for "deferred" across all planning docs returns zero matches on Phase 4/5
  4. Codebase docs (STACK.md, INTEGRATIONS.md) have no "Phase 4+" or "Phase 4)" annotations
**Plans:** 2 plans

Plans:
- [ ] 13-01-PLAN.md -- Correct PROJECT.md, CLAUDE.md, STATE.md (project name, test counts, phase status, stale references)
- [ ] 13-02-PLAN.md -- Remove phase annotations from STACK.md/INTEGRATIONS.md + verification sweep

Reference: `docs/superpowers/plans/2026-03-24-migration-and-stabilization.md` Chunk 1 (Tasks 1-6)

### Phase 14: Data Migration & Validation
**Goal**: job-cannon is fully operational with real data — all personal data files migrated from job-finder, config merged, schema verified, tests and app rendering confirmed
**Depends on**: Phase 13
**Requirements**: MIGR-01, MIGR-02, MIGR-03, MIGR-04, VALID-01, VALID-02, VALID-03
**Success Criteria** (what must be TRUE):
  1. All 8 data files (jobs.db, config.yaml, experience_profile.json, token.json, credentials.json, .env, resume_style_guide.json, experience_reference.md) exist in job-cannon root
  2. `git status` shows zero untracked data files — all are gitignored
  3. config.yaml contains cannon-specific sections (server, filters) and key values match job-finder's (haiku_threshold: 42, drive folder ID populated)
  4. Full test suite passes after migration (1359 tests, zero failures)
  5. App starts on localhost:5000 and renders real job listings from the migrated database
**Plans**: TBD

Reference: `docs/superpowers/plans/2026-03-24-migration-and-stabilization.md` Chunk 2 (Tasks 7-11)

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
| 13. Planning Doc Corrections | v1.2 | 0/2 | In progress | - |
| 14. Data Migration & Validation | v1.2 | 0/? | Not started | - |
