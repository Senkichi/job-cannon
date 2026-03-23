# Requirements: Job Cannon

**Defined:** 2026-03-23
**Core Value:** Surface the best-fit jobs fast and keep the application pipeline visible

## v1.1 Requirements

Requirements for milestone v1.1: Port job-finder improvements. Each maps to roadmap phases.

### Refactoring

- [ ] **REFAC-01**: db.py uses module-level functions taking `conn` as first arg instead of JobDB class
- [ ] **REFAC-02**: Explicit column constants replace `SELECT *` in db queries
- [ ] **REFAC-03**: Smart description merging on upsert (substring dedup, keep longer, eager jd_full promotion)
- [ ] **REFAC-04**: Scoring orchestrator centralizes haiku/sonnet scoring flow with persist helpers
- [ ] **REFAC-05**: Description formatter extracted from app factory into dedicated module
- [ ] **REFAC-06**: Scheduler uses closure factories (_make_simple_job, _make_tracked_job) instead of boilerplate

### Safety

- [ ] **SAFE-01**: Claude API calls have configurable timeout (default 120s)
- [ ] **SAFE-02**: Unknown model pricing falls back conservatively with warning log instead of KeyError
- [ ] **SAFE-03**: Gmail message fetch has 500-message pagination cap
- [ ] **SAFE-04**: Pipeline status changes validated against VALID_PIPELINE_STATUSES frozenset
- [ ] **SAFE-05**: Query string params use _safe_float/_safe_int validators (HTTP 400 on malformed)
- [ ] **SAFE-06**: Flask secret key generated via secrets.token_hex(32)

### Blueprints

- [ ] **BP-01**: Fragment routes have HX-Request header guards (dismiss, return, expand, collapse, save_jd)
- [ ] **BP-02**: Batch scoring has 30-minute timeout safety net for stuck sessions
- [ ] **BP-03**: Settings guidelines_path uses correct parent depth (parent.parent.parent.parent)
- [ ] **BP-04**: Sonnet empty-field protection preserves existing values
- [ ] **BP-05**: cost_stats budget_cap is configurable via caller

### Filter

- [ ] **FILT-01**: User can select multiple pipeline statuses simultaneously via checkbox pills
- [ ] **FILT-02**: "All" toggle button checks/unchecks all status filters
- [ ] **FILT-03**: Multi-status selection builds IN clause in db query
- [ ] **FILT-04**: Date filter clearing triggers table refresh (hx-trigger change from:input)

### Cleanup

- [ ] **CLN-01**: Dead modules removed: main.py, scoring/ package, utils.py, output/
- [ ] **CLN-02**: Scorers return ScoringResult instead of raw dict
- [ ] **CLN-03**: Scorer profile param renamed to experience_profile
- [ ] **CLN-04**: Dead to_dict() method removed from Job model
- [ ] **CLN-05**: New db migration adds company_size and industry columns to companies table
- [ ] **CLN-06**: Tests updated for new API (module-level db functions, ScoringResult, experience_profile)

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
| REFAC-01 | Phase 7 | Pending |
| REFAC-02 | Phase 7 | Pending |
| REFAC-03 | Phase 7 | Pending |
| REFAC-04 | Phase 7 | Pending |
| REFAC-05 | Phase 7 | Pending |
| REFAC-06 | Phase 8 | Pending |
| SAFE-01 | Phase 7 | Pending |
| SAFE-02 | Phase 7 | Pending |
| SAFE-03 | Phase 8 | Pending |
| SAFE-04 | Phase 10 | Pending |
| SAFE-05 | Phase 9 | Pending |
| SAFE-06 | Phase 6 | Pending |
| BP-01 | Phase 9 | Pending |
| BP-02 | Phase 9 | Pending |
| BP-03 | Phase 9 | Pending |
| BP-04 | Phase 9 | Pending |
| BP-05 | Phase 9 | Pending |
| FILT-01 | Phase 9 | Pending |
| FILT-02 | Phase 9 | Pending |
| FILT-03 | Phase 7 | Pending |
| FILT-04 | Phase 9 | Pending |
| CLN-01 | Phase 6 | Pending |
| CLN-02 | Phase 8 | Pending |
| CLN-03 | Phase 8 | Pending |
| CLN-04 | Phase 10 | Pending |
| CLN-05 | Phase 10 | Pending |
| CLN-06 | Phase 10 | Pending |

**Coverage:**
- v1.1 requirements: 27 total
- Mapped to phases: 27
- Unmapped: 0

---
*Requirements defined: 2026-03-23*
*Last updated: 2026-03-23 after roadmap creation*
