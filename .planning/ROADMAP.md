# Roadmap: Job Cannon

## Milestones

- ✅ **v1.0 Foundation + AI Scoring + Pipeline Automation** — Phases 1-5 (shipped 2026-03-23)
- ✅ **v1.1 Port job-finder Improvements** — Phases 6-12 (shipped 2026-03-24)
- ✅ **v1.2 Migration & Stabilization** — Phases 13-14 (shipped 2026-03-24)
- **v1.3 Fixes & Improvements** — Phases 15-18 (in progress)

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

### v1.3 Fixes & Improvements (Phases 15-18)

- [x] **Phase 15: Parser Fixes** — Glassdoor positional extraction + Indeed rc/clk/dl URL support (1/1 plan, completed 2026-03-26)
- [x] **Phase 16: Homepage Discovery** — Three-tier discovery + daily APScheduler cron job at 06:30 (2/2 plans, completed 2026-03-26)
- [x] **Phase 17: Code Quality** — Test log isolation, exception handler separation, date filter fix (1/1 plan, completed 2026-03-26)
- [x] **Phase 18: Async Sync** — Background thread with HTMX progress polling replaces blocking sync (completed 2026-03-26)

## Phase Details

### Phase 15: Parser Fixes
**Goal**: Jobs from Glassdoor and Indeed email alerts flow into the pipeline without data loss
**Depends on**: Nothing (first phase of v1.3, fully self-contained)
**Requirements**: PARSE-01, PARSE-02
**Success Criteria** (what must be TRUE):
  1. Glassdoor alert emails using positional span/p format (no CSS classes) produce parsed jobs with correct company names, titles, and URLs — no trailing rating digits
  2. Indeed alert emails using the `rc/clk/dl` URL format produce parsed jobs with resolved job detail URLs
  3. Parser tests pass against real archived emails from `data/parse_failures/`, not just synthetic fixtures
  4. Ingestion run after the fix produces nonzero jobs from Glassdoor and Indeed email batches
**Plans**: 1 plan

Plans:
- [x] 15-01-PLAN.md -- Fix Glassdoor positional extraction and Indeed rc/clk/dl URL support

### Phase 16: Homepage Discovery
**Goal**: Company homepages are resolved reliably via a three-tier strategy that eliminates dependence on the broken DDG fallback
**Depends on**: Nothing (independent of Phase 15, can start in parallel)
**Requirements**: DISC-01, DISC-02, DISC-03, DISC-04
**Success Criteria** (what must be TRUE):
  1. Companies with normalized single-word names (e.g., "Stripe") get homepage URLs resolved via domain-guessing heuristic at zero API cost
  2. Companies with compound multi-word names (e.g., "Hinge Health") get correct homepage URLs via slug normalization — no broken slugs, no parked-domain false positives
  3. Companies that fail the heuristic get homepage URLs discovered via SerpAPI `engine=google` web search — results exclude third-party directory URLs (Glassdoor, Crunchbase, LinkedIn, Bloomberg, ZoomInfo)
  4. A daily APScheduler job at 6:30 AM runs homepage discovery against companies with no `homepage_url`, using its own sqlite3 connection (not Flask `g.db`), skipping companies already attempted via `homepage_probe_attempted_at` tracking
  5. SerpAPI quota errors (JSON `error` key) short-circuit the batch immediately with a logged error rather than silently continuing
**Plans**: 2 plans
**UI hint**: no

Plans:
- [x] 16-01-PLAN.md -- Three-tier discovery refactor + Migration 17 + test rewrite
- [x] 16-02-PLAN.md -- Register homepage_discovery_job in scheduler (daily 06:30 cron)

### Phase 17: Code Quality
**Goal**: Test runs are clean, scan errors are attributed correctly, and the date filter works reliably across all browsers
**Depends on**: Nothing (fully independent of Phases 15 and 16)
**Requirements**: QUAL-01, QUAL-02, UI-01
**Success Criteria** (what must be TRUE):
  1. Running `uv run pytest tests/` produces no entries in `logs/app.log` — test execution is fully isolated from the production log file
  2. A template rendering error in the companies ATS scan route surfaces as a 500 with a traceback referencing the template, not as a scan failure message — the two exception handlers are distinct
  3. Clearing the date filter input on the job board immediately refreshes the job list — no stale state requiring a page reload
**Plans**: 1 plan
**UI hint**: yes

Plans:
- [x] 17-01-PLAN.md — Test log isolation, exception handler separation, date filter fix

### Phase 18: Async Sync
**Goal**: Users trigger a sync and see live progress feedback instead of a frozen browser waiting 30+ seconds
**Depends on**: Phase 15 (parser fixes ensure sync produces correct jobs; sync is meaningless if parsers are dark)
**Requirements**: UI-02
**Success Criteria** (what must be TRUE):
  1. Clicking "Sync Now" returns a progress fragment immediately (within ~1 second) — the browser is not blocked during the sync operation
  2. The progress fragment polls every 2 seconds and shows the current sync phase label (gmail, serpapi, scoring, done)
  3. When sync completes, the done fragment displays jobs found, new jobs added, and error count for at least 10 seconds before dismissal
  4. If a background sync thread dies without writing a terminal status, the status route returns an error state after 30 minutes — the browser never polls forever
  5. A second "Sync Now" click while a sync is running is rejected (existing `running` session detected, no duplicate thread spawned)
**Plans**: 1 plan

Plans:
- [x] 18-01-PLAN.md -- Async sync with HTMX progress polling

**UI hint**: yes

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
| 18. Async Sync | v1.3 | 1/1 | Complete   | 2026-03-26 |
| 22. Module Splits | v1.4 | 5/7 | In Progress|  |
