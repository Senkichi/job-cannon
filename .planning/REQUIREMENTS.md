# Requirements: Job Cannon

**Defined:** 2026-03-27
**Core Value:** Surface the best-fit jobs fast and keep the application pipeline visible

## v1.5 Requirements

Requirements for Multi-Provider Model Routing milestone. Each maps to roadmap phases.

### Provider Infrastructure

- [ ] **INFRA-01**: Dispatcher resolves logical tier names ("sonnet", "haiku", "opus") to provider + model via config.yaml providers section
- [ ] **INFRA-02**: Schema validation retries once on failure with augmented prompt including schema errors
- [ ] **INFRA-03**: Configurable fallback re-dispatches to Anthropic when retry fails
- [ ] **INFRA-04**: Budget gate bypassed for free providers (Gemini free tier, Ollama)
- [ ] **INFRA-05**: Missing providers section defaults to Anthropic routing (backwards compatible)

### Provider Adapters

- [ ] **ADAPT-01**: Anthropic adapter wraps existing call_claude() internals with tool-choice structured output
- [ ] **ADAPT-02**: Gemini adapter uses google-genai SDK with response_schema structured output and 429 rate-limit retry
- [ ] **ADAPT-03**: Ollama adapter uses REST API with JSON format + schema-in-prompt, health-check on init

### Caller Migration

- [ ] **MIGR-01**: All call_claude() call sites migrated to call_model() with logical tier names
- [ ] **MIGR-02**: All direct anthropic.Anthropic() usage in blueprints/orchestrators refactored to use provider layer
- [ ] **MIGR-03**: config.example.yaml updated with providers section examples

### Cost Tracking

- [ ] **COST-01**: Provider column added to scoring_costs table with 'anthropic' default
- [ ] **COST-02**: Cost stats include per-provider grouping alongside existing per-feature breakdown
- [ ] **COST-03**: Costs page shows provider breakdown

### Evaluation Framework

- [ ] **EVAL-01**: CLI benchmark tool samples jobs with existing Sonnet results and reconstructs prompts
- [ ] **EVAL-02**: Metrics computed: score correlation, schema adherence, qualitative output, latency
- [ ] **EVAL-03**: Auto-computed verdict (SUITABLE/MARGINAL/NOT_RECOMMENDED) with configurable thresholds
- [ ] **EVAL-04**: JSON report saved to eval_results/ with aggregate metrics and per-job details

## v1.4 Requirements (Complete)

### Housekeeping

- [x] **HOUSE-01**: Move 11 completed todos from pending/ to done/

### Surgical Fixes

- [x] **FIX-01**: Deduplicate Indeed `_is_meta_email` via `_common.is_meta_email` with `extra_patterns`
- [x] **FIX-02**: Rename `discover_homepages_batch` → `run_homepage_discovery`, `trigger_sync` → `run_sync_now`, `_merge_guidelines_into_guide` → `merge_guidelines_into_guide`, `_FIELD_LABELS` → `FIELD_LABELS`
- [x] **FIX-03**: Move `normalized_dedup_key()` from web layer into `models.py` as `Job.normalized_dedup_key()` static method
- [x] **FIX-04**: Extract `standalone_connection()` context manager into `db_helpers.py`, replace 35+ boilerplate occurrences
- [x] **FIX-05**: Add `get_config_snapshot()` helper for background thread config access

### Test Coverage

- [x] **TEST-01**: Create `tests/test_companies.py` covering companies blueprint routes (index, expand, collapse, toggle, update_slug, retry)

### Module Splits

- [x] **SPLIT-01**: Split `ats_scanner.py` (1500 LOC) → `ats_scanner.py` + `ats_detection.py` + `ats_prober.py`
- [x] **SPLIT-02**: Split `data_enricher.py` (~1100 LOC) → `data_enricher.py` + `enrichment_tiers.py` + `company_enricher.py`
- [x] **SPLIT-03**: Split `dashboard.py` (956 LOC) → `dashboard.py` + `batch_scoring.py` + `sync.py` blueprints
- [x] **SPLIT-04**: Split `resume_generator.py` (1019 LOC) → `resume_generator.py` + `resume_multi_version.py`
- [x] **SPLIT-05**: Split `profile.py` (901 LOC) → `profile.py` + `resume_review.py` + `profile_recommendations.py` blueprints
- [x] **SPLIT-06**: Split `pipeline_runner.py` (692 LOC) → `pipeline_runner.py` + `scoring_runner.py`
- [x] **SPLIT-07**: Split `settings.py` (602 LOC) → `settings.py` + `guidelines.py` blueprints

### N+1 Query Batching

- [x] **BATCH-01**: Batch Haiku scoring reads — single `SELECT ... WHERE dedup_key IN (?)` per batch
- [x] **BATCH-02**: Batch Sonnet evaluation reads — same pattern as BATCH-01
- [x] **BATCH-03**: Batch stale detector updates — single `UPDATE ... WHERE dedup_key IN (?)`
- [x] **BATCH-04**: Batch dashboard cancellation check — check once per batch, not per job
- [x] **BATCH-05**: Defer session counter update — increment once after batch completes

## Future Requirements

None identified for this milestone.

## Out of Scope

| Feature | Reason |
|---------|--------|
| Haiku/Opus call site migration to alternative providers | Config supports it, but v1.5 focuses on Sonnet-tier calls |
| Per-purpose provider routing | All Sonnet calls route to same provider in v1.5 |
| Shadow mode / always-on evaluation | On-demand benchmarking sufficient |
| Settings page UI for provider management | Config.yaml only for v1.5 |
| Modifying existing call_claude() behavior | Stays untouched as Anthropic adapter backend |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| INFRA-01 | Phase 24 | Pending |
| INFRA-02 | Phase 26 | Pending |
| INFRA-03 | Phase 26 | Pending |
| INFRA-04 | Phase 26 | Pending |
| INFRA-05 | Phase 24 | Pending |
| ADAPT-01 | Phase 25 | Pending |
| ADAPT-02 | Phase 25 | Pending |
| ADAPT-03 | Phase 25 | Pending |
| MIGR-01 | Phase 27 | Pending |
| MIGR-02 | Phase 27 | Pending |
| MIGR-03 | Phase 27 | Pending |
| COST-01 | Phase 24 | Pending |
| COST-02 | Phase 26 | Pending |
| COST-03 | Phase 26 | Pending |
| EVAL-01 | Phase 28 | Pending |
| EVAL-02 | Phase 28 | Pending |
| EVAL-03 | Phase 28 | Pending |
| EVAL-04 | Phase 28 | Pending |

**Coverage:**
- v1.5 requirements: 18 total
- Mapped to phases: 18
- Unmapped: 0

---
*Requirements defined: 2026-03-27*
*Last updated: 2026-03-27 after roadmap creation (v1.5)*
