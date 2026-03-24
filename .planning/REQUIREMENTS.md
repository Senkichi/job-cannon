# Requirements: Job Cannon

**Defined:** 2026-03-23
**Core Value:** Surface the best-fit jobs fast and keep the application pipeline visible

## v1.1 Requirements

Requirements for milestone v1.1: Port job-finder improvements. Each maps to roadmap phases.

### Refactoring

- [x] **REFAC-01**: db.py uses module-level functions taking `conn` as first arg instead of JobDB class
- [x] **REFAC-02**: Explicit column constants replace `SELECT *` in db queries
- [x] **REFAC-03**: Smart description merging on upsert (substring dedup, keep longer, eager jd_full promotion)
- [x] **REFAC-04**: Scoring orchestrator centralizes haiku/sonnet scoring flow with persist helpers
- [x] **REFAC-05**: Description formatter extracted from app factory into dedicated module
- [x] **REFAC-06**: Scheduler uses closure factories (_make_simple_job, _make_tracked_job) instead of boilerplate

### Safety

- [x] **SAFE-01**: Claude API calls have configurable timeout (default 120s)
- [x] **SAFE-02**: Unknown model pricing falls back conservatively with warning log instead of KeyError
- [x] **SAFE-03**: Gmail message fetch has 500-message pagination cap
- [x] **SAFE-04**: Pipeline status changes validated against VALID_PIPELINE_STATUSES frozenset
- [x] **SAFE-05**: Query string params use _safe_float/_safe_int validators (HTTP 400 on malformed)
- [x] **SAFE-06**: Flask secret key generated via secrets.token_hex(32)

### Blueprints

- [x] **BP-01**: Fragment routes have HX-Request header guards (dismiss, return, expand, collapse, save_jd)
- [x] **BP-02**: Batch scoring has 30-minute timeout safety net for stuck sessions
- [x] **BP-03**: Settings guidelines_path uses correct parent depth (parent.parent.parent.parent)
- [x] **BP-04**: Sonnet empty-field protection preserves existing values
- [x] **BP-05**: cost_stats budget_cap is configurable via caller

### Filter

- [x] **FILT-01**: User can select multiple pipeline statuses simultaneously via checkbox pills
- [x] **FILT-02**: "All" toggle button checks/unchecks all status filters
- [x] **FILT-03**: Multi-status selection builds IN clause in db query
- [x] **FILT-04**: Date filter clearing triggers table refresh (hx-trigger change from:input)

### Cleanup

- [x] **CLN-01**: Dead modules removed: main.py, ~~scoring/ package,~~ utils.py, output/ (scoring/ retained — actively used by pipeline_runner.py)
- [x] **CLN-02**: Scorers return ScoringResult instead of raw dict
- [x] **CLN-03**: Scorer profile param renamed to experience_profile
- [x] **CLN-04**: Dead to_dict() method removed from Job model
- [x] **CLN-05**: New db migration adds company_size and industry columns to companies table
- [x] **CLN-06**: Tests updated for new API (module-level db functions, ScoringResult, experience_profile)

## Future Requirements

### Resume Generation (deferred from v1.0)

- **RESUME-01**: User can generate tailored resume from job description via Google Docs API
- **RESUME-02**: Resume content adapts to job requirements using experience profile

### Intelligence (deferred from v1.0)

- **INTEL-01**: Semantic job similarity and clustering
- **INTEL-02**: Smart job recommendations based on application history

## Out of Scope

| Feature | Reason |
|---------|--------|
| Deployment/Docker/CI | Single-user local app, not needed |
| ORM | Raw SQL intentional at this scale |
| Build step/bundler | Tailwind CDN + HTMX CDN intentional |
| APScheduler 4.x | Breaking async API, pinned <4.0 |
| New job sources | Port only, no new integrations this milestone |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| REFAC-01 | Phase 7 | Satisfied |
| REFAC-02 | Phase 7 | Satisfied |
| REFAC-03 | Phase 7 | Satisfied |
| REFAC-04 | Phase 7 | Satisfied |
| REFAC-05 | Phase 7 | Satisfied |
| REFAC-06 | Phase 11 | Complete |
| SAFE-01 | Phase 7 | Satisfied |
| SAFE-02 | Phase 7 | Satisfied |
| SAFE-03 | Phase 11 | Complete |
| SAFE-04 | Phase 10 | Satisfied |
| SAFE-05 | Phase 9 | Satisfied |
| SAFE-06 | Phase 6 | Satisfied |
| BP-01 | Phase 9 | Satisfied |
| BP-02 | Phase 9 | Satisfied |
| BP-03 | Phase 9 | Satisfied |
| BP-04 | Phase 9 | Satisfied |
| BP-05 | Phase 9 | Satisfied |
| FILT-01 | Phase 9 | Satisfied |
| FILT-02 | Phase 9 | Satisfied |
| FILT-03 | Phase 7 | Satisfied |
| FILT-04 | Phase 9 | Satisfied |
| CLN-01 | Phase 10 | Satisfied |
| CLN-02 | Phase 11 | Satisfied |
| CLN-03 | Phase 8 | Satisfied |
| CLN-04 | Phase 10 | Satisfied |
| CLN-05 | Phase 10 | Satisfied |
| CLN-06 | Phase 11 | Satisfied |

**Coverage:**
- v1.1 requirements: 27 total
- Satisfied: 27 (all v1.1 requirements)
- Pending: 0
- Unmapped: 0

---
*Requirements defined: 2026-03-23*
*Last updated: 2026-03-23 after Phase 12 verification backfill*
