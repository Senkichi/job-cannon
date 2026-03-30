# Roadmap: Job Cannon

## Milestones

- ✅ **v1.0 Foundation + AI Scoring + Pipeline Automation** — Phases 1-5 (shipped 2026-03-23)
- ✅ **v1.1 Port job-finder Improvements** — Phases 6-12 (shipped 2026-03-24)
- ✅ **v1.2 Migration & Stabilization** — Phases 13-14 (shipped 2026-03-24)
- ✅ **v1.3 Fixes & Improvements** — Phases 15-18 (shipped 2026-03-26)
- ✅ **v1.4 Tech Debt Sweep** — Phases 19-23 (shipped 2026-03-27)
- ✅ **v1.5 Multi-Provider Model Routing** — Phases 24-28 (shipped 2026-03-27)
- 🔄 **v2.0 Cascading Free Provider Routing** — Phases 29-32 (in progress)

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

<details>
<summary>✅ v1.4 Tech Debt Sweep (Phases 19-23) — SHIPPED 2026-03-27</summary>

- [x] Phase 19: Housekeeping (1/1 plan) — completed 2026-03-27
- [x] Phase 20: Surgical Fixes (3/3 plans) — completed 2026-03-27
- [x] Phase 21: Test Coverage (1/1 plan) — completed 2026-03-27
- [x] Phase 22: Module Splits (7/7 plans) — completed 2026-03-27
- [x] Phase 23: N+1 Batching (3/3 plans) — completed 2026-03-27

</details>

<details>
<summary>✅ v1.5 Multi-Provider Model Routing (Phases 24-28) — SHIPPED 2026-03-27</summary>

- [x] Phase 24: Provider Foundation (2/2 plans) — completed 2026-03-27
- [x] Phase 25: Provider Adapters (3/3 plans) — completed 2026-03-27
- [x] Phase 26: Dispatcher & Cost Tracking (2/2 plans) — completed 2026-03-27
- [x] Phase 27: Caller Migration (4/4 plans) — completed 2026-03-27
- [x] Phase 28: Evaluation Framework (2/2 plans) — completed 2026-03-27

</details>

### v2.0 Cascading Free Provider Routing

- [x] **Phase 29: Cascade Config & Rate Limiting** - Parse fallback_chain config and track daily provider usage (completed 2026-03-29)
- [x] **Phase 30: Cascade Execution** - Iterate provider chain with 429 handling and exhaustion logic (completed 2026-03-29)
- [ ] **Phase 31: Prompts & Attribution** - Fewshot in production, per-model variants, provider stored on jobs
- [ ] **Phase 32: Integration & Config Wiring** - Wire production config.yaml, smoke test cascade end-to-end

## Phase Details

### Phase 29: Cascade Config & Rate Limiting
**Goal**: The app can parse a `fallback_chain` from config and accurately track daily provider usage in memory
**Depends on**: Phase 28 (Provider Foundation complete)
**Requirements**: CASC-01, CASC-02, CASC-06, CONF-01, TEST-01, TEST-03
**Success Criteria** (what must be TRUE):
  1. `resolve_provider_config()` returns a `fallback_chain` list (empty list when config has no chain, preserving old single-fallback behavior)
  2. Daily usage counters reset automatically at midnight and bootstrap from `scoring_costs` DB on the new day
  3. A provider at its daily limit is correctly identified as exhausted; a provider under its limit passes the check
  4. `config.example.yaml` shows a complete, commented cascade config block that new users can copy
  5. Config parse tests and daily limit tracker tests all pass
**Plans**: 2 plans
Plans:
- [x] 29-01-PLAN.md -- Cascade config parsing (resolve_provider_config + config.example.yaml + tests)
- [x] 29-02-PLAN.md -- Daily rate limit tracker (module-level state + helper functions + tests)

### Phase 30: Cascade Execution
**Goal**: `call_model()` walks the fallback chain, skipping exhausted or unavailable providers, and surfaces a clear error when all are exhausted
**Depends on**: Phase 29
**Requirements**: CASC-03, CASC-04, CASC-07, TEST-02
**Success Criteria** (what must be TRUE):
  1. When the primary provider is at its daily limit, scoring continues using the next provider in the chain without any user intervention
  2. A 429 response from any provider marks that provider as exhausted for the day and immediately cascades to the next provider
  3. A provider with no API key configured is silently skipped (not an error)
  4. When every provider in the chain is exhausted or unavailable, `call_model()` raises `RuntimeError` with a descriptive message
  5. Cascade execution tests all pass (skip exhausted, 429 mark-and-skip, all-exhausted error)
**Plans**: 1 plan
Plans:
- [x] 30-01-PLAN.md -- Cascade dispatch loop (tests + call_model cascade implementation)

### Phase 31: Prompts & Attribution
**Goal**: Production scoring uses fewshot examples by default, per-model prompt variants thread through the cascade, and every scored job records which provider produced its score
**Depends on**: Phase 30
**Requirements**: CASC-05, PRMT-01, PRMT-02, ATTR-01, ATTR-02, ATTR-03, TEST-04
**Success Criteria** (what must be TRUE):
  1. `sonnet_evaluator.py` includes fewshot examples in the system prompt without any extra config — existing jobs scored after this phase are evaluated with fewshot by default
  2. A cascade entry with `prompt_variant: fewshot-distribution` causes the evaluator to use the distribution-aware instructions for that provider
  3. After scoring, `SELECT scoring_provider FROM jobs WHERE dedup_key = ?` returns the name of the provider that produced the score (not NULL, not the default)
  4. Existing jobs without a provider value default to `'anthropic'` (migration is non-destructive)
  5. Provider attribution DB test passes: score a job, verify `scoring_provider` column written correctly
**Plans**: 3 plans
Plans:
- [ ] 31-01-PLAN.md -- DB Migration 20 + persist_sonnet_score provider param + attribution tests
- [ ] 31-02-PLAN.md -- Fewshot production prompts + PROMPT_VARIANTS + cascade prompt variant injection
- [ ] 31-03-PLAN.md -- Provider attribution threading (evaluate_job_sonnet -> orchestrator -> DB)

### Phase 32: Integration & Config Wiring
**Goal**: Production `config.yaml` runs the decided cascade order (Cerebras -> Groq -> Ollama -> Anthropic) and the full pipeline can be verified cascade-working end-to-end
**Depends on**: Phase 31
**Requirements**: CONF-02
**Success Criteria** (what must be TRUE):
  1. `config.yaml` has `fallback_chain` wired with Cerebras as primary, Groq second, Ollama third, Anthropic last, with correct model IDs and per-model prompt variants
  2. `daily_limits` for Cerebras (350) and Groq (170) are set in config
  3. Setting `daily_limits.cerebras: 2` and triggering scoring causes the second job to be scored by Groq (verifiable via `scoring_provider` column)
  4. All 1786+ tests continue to pass after config wiring
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
| 19. Housekeeping | v1.4 | 1/1 | Complete | 2026-03-27 |
| 20. Surgical Fixes | v1.4 | 3/3 | Complete | 2026-03-27 |
| 21. Test Coverage | v1.4 | 1/1 | Complete | 2026-03-27 |
| 22. Module Splits | v1.4 | 7/7 | Complete | 2026-03-27 |
| 23. N+1 Batching | v1.4 | 3/3 | Complete | 2026-03-27 |
| 24. Provider Foundation | v1.5 | 2/2 | Complete | 2026-03-27 |
| 25. Provider Adapters | v1.5 | 3/3 | Complete | 2026-03-27 |
| 26. Dispatcher & Cost Tracking | v1.5 | 2/2 | Complete | 2026-03-27 |
| 27. Caller Migration | v1.5 | 4/4 | Complete | 2026-03-27 |
| 28. Evaluation Framework | v1.5 | 2/2 | Complete | 2026-03-27 |
| 29. Cascade Config & Rate Limiting | v2.0 | 2/2 | Complete    | 2026-03-29 |
| 30. Cascade Execution | v2.0 | 1/1 | Complete    | 2026-03-29 |
| 31. Prompts & Attribution | v2.0 | 0/3 | In progress | - |
| 32. Integration & Config Wiring | v2.0 | 0/? | Not started | - |
