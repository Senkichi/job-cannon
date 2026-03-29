# Requirements: Job Cannon

**Defined:** 2026-03-29
**Core Value:** Surface the best-fit jobs fast and keep the application pipeline visible

## v2.0 Requirements

Requirements for Cascading Free Provider Routing milestone. Each maps to roadmap phases.

### Cascade Routing

- [x] **CASC-01**: `resolve_provider_config()` parses `fallback_chain` list from tier config and `daily_limits` from providers config
- [x] **CASC-02**: Daily rate limit tracker with in-memory counters that bootstrap from `scoring_costs` DB on date rollover
- [x] **CASC-03**: `call_model()` iterates fallback chain, skipping providers that are exhausted, unavailable (missing API key), or over budget
- [x] **CASC-04**: 429 HTTP responses mark the provider as exhausted for the day and cascade to next
- [ ] **CASC-05**: Per-model prompt variant override in fallback chain config, threaded to sonnet evaluator
- [x] **CASC-06**: Empty `fallback_chain` preserves existing single-fallback behavior (backward compatibility)
- [x] **CASC-07**: All providers exhausted raises `RuntimeError` with clear message

### Provider Attribution

- [ ] **ATTR-01**: DB migration adds `scoring_provider` column to `jobs` table with `'anthropic'` default
- [ ] **ATTR-02**: `persist_sonnet_score()` accepts and stores provider from scoring result
- [ ] **ATTR-03**: Provider attribution threaded from `call_model()` through `evaluate_job_sonnet()` to orchestrator to DB

### Production Prompts

- [ ] **PRMT-01**: Fewshot examples moved from `eval_provider.py` into `sonnet_evaluator.py` as production default
- [ ] **PRMT-02**: Distribution awareness instructions available for providers configured with `fewshot-distribution` variant

### Testing

- [x] **TEST-01**: Cascade config parsing tests (fallback chain + backward compat)
- [x] **TEST-02**: Cascade execution tests (skip exhausted, handle 429, all-exhausted error)
- [x] **TEST-03**: Daily limit tracker tests (check, increment, date rollover reset)
- [ ] **TEST-04**: Provider attribution DB test (`scoring_provider` written and read)

### Config

- [x] **CONF-01**: `config.example.yaml` documents cascade config schema with `fallback_chain` and `daily_limits`
- [ ] **CONF-02**: Production `config.yaml` wired with decided cascade order (Cerebras -> Groq -> Ollama -> Anthropic)

## Previous Milestones (Complete)

<details>
<summary>v1.5 Multi-Provider Model Routing (18 requirements)</summary>

- [x] INFRA-01 through INFRA-05: Provider infrastructure
- [x] ADAPT-01 through ADAPT-03: Provider adapters
- [x] MIGR-01 through MIGR-03: Caller migration
- [x] COST-01 through COST-03: Cost tracking
- [x] EVAL-01 through EVAL-04: Evaluation framework

</details>

<details>
<summary>v1.4 Tech Debt Sweep (18 requirements)</summary>

- [x] HOUSE-01, FIX-01 through FIX-05, TEST-01, SPLIT-01 through SPLIT-07, BATCH-01 through BATCH-05

</details>

## Out of Scope

| Feature | Reason |
|---------|--------|
| Score recalibration across providers | Deferred — provider attribution stored for future retroactive calibration |
| UI provider badge on job cards | Deferred — column populated but not displayed yet |
| SambaNova in cascade | Stuck at 20 RPD, not competitive until billing upgrade |
| Haiku tier cascade | Haiku is cheap ($0.001/job), not worth cascading |
| Async/parallel provider calls | Unnecessary for single-user localhost app |
| Settings page UI for cascade config | Config.yaml only for v2.0 |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| CASC-01 | Phase 29 | Complete |
| CASC-02 | Phase 29 | Complete |
| CASC-06 | Phase 29 | Complete |
| CONF-01 | Phase 29 | Complete |
| TEST-01 | Phase 29 | Complete |
| TEST-03 | Phase 29 | Complete |
| CASC-03 | Phase 30 | Complete |
| CASC-04 | Phase 30 | Complete |
| CASC-07 | Phase 30 | Complete |
| TEST-02 | Phase 30 | Complete |
| CASC-05 | Phase 31 | Pending |
| PRMT-01 | Phase 31 | Pending |
| PRMT-02 | Phase 31 | Pending |
| ATTR-01 | Phase 31 | Pending |
| ATTR-02 | Phase 31 | Pending |
| ATTR-03 | Phase 31 | Pending |
| TEST-04 | Phase 31 | Pending |
| CONF-02 | Phase 32 | Pending |

**Coverage:**
- v2.0 requirements: 16 total
- Mapped to phases: 16
- Unmapped: 0

---
*Requirements defined: 2026-03-29*
*Last updated: 2026-03-29 after roadmap creation (Phases 29-32)*
