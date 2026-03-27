# Roadmap: Job Cannon

## Milestones

- ✅ **v1.0 Foundation + AI Scoring + Pipeline Automation** — Phases 1-5 (shipped 2026-03-23)
- ✅ **v1.1 Port job-finder Improvements** — Phases 6-12 (shipped 2026-03-24)
- ✅ **v1.2 Migration & Stabilization** — Phases 13-14 (shipped 2026-03-24)
- ✅ **v1.3 Fixes & Improvements** — Phases 15-18 (shipped 2026-03-26)
- ✅ **v1.4 Tech Debt Sweep** — Phases 19-23 (shipped 2026-03-27)
- 🚧 **v1.5 Multi-Provider Model Routing** — Phases 24-28 (in progress)

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

### 🚧 v1.5 Multi-Provider Model Routing (In Progress)

**Milestone Goal:** Make all AI model calls configurable to route through Anthropic, Gemini, or Ollama via config.yaml, with an evaluation framework to benchmark alternatives before switching.

- [x] **Phase 24: Provider Foundation** (2/2 plans) — completed 2026-03-27
- [x] **Phase 25: Provider Adapters** (3 plans) — Anthropic, Gemini, and Ollama adapter implementations (completed 2026-03-27)
- [ ] **Phase 26: Dispatcher & Cost Tracking** — call_model() dispatcher with retry, fallback, budget bypass, and cost UI
- [ ] **Phase 27: Caller Migration** — Migrate all call sites from call_claude() to call_model()
- [ ] **Phase 28: Evaluation Framework** — CLI benchmark tool for comparing provider quality vs. stored Sonnet results

## Phase Details

### Phase 25: Provider Adapters
**Goal**: Three provider adapters are implemented and independently testable — Anthropic wrapping existing internals, Gemini via google-genai, and Ollama via local REST
**Depends on**: Phase 24
**Requirements**: ADAPT-01, ADAPT-02, ADAPT-03
**Success Criteria** (what must be TRUE):
  1. Anthropic adapter calls call_claude() internally and returns a ModelResult with structured output
  2. Gemini adapter uses response_schema for structured output and retries automatically on HTTP 429 rate-limit errors
  3. Ollama adapter checks local service health on initialization and raises a clear error if unreachable
  4. All three adapters conform to the BaseProvider interface and are independently unit-testable with mocked transports
**Plans:** 3/3 plans complete
Plans:
- [x] 25-01-PLAN.md — Install dependencies + Anthropic adapter (TDD)
- [x] 25-02-PLAN.md — Gemini adapter with response_json_schema and 429 retry (TDD)
- [x] 25-03-PLAN.md — Ollama adapter with REST API and health check (TDD)

### Phase 26: Dispatcher & Cost Tracking
**Goal**: call_model() exists as the single dispatch point — it routes by tier, validates output schema, retries with error context, falls back to Anthropic, bypasses budget for free providers, and records provider in cost rows
**Depends on**: Phase 25
**Requirements**: INFRA-02, INFRA-03, INFRA-04, COST-02, COST-03
**Success Criteria** (what must be TRUE):
  1. call_model("sonnet", prompt, schema) routes to whichever provider is configured for that tier
  2. When a provider returns a response that fails schema validation, call_model() retries once with the schema errors appended to the prompt
  3. When retry also fails, call_model() re-dispatches to Anthropic as configured fallback
  4. Calls routed to Gemini free tier or Ollama skip budget gate checks entirely
  5. The Costs page shows a per-provider breakdown of API spend alongside the existing per-feature breakdown
**Plans:** 2 plans
Plans:
- [ ] 26-01-PLAN.md — call_model() dispatcher with schema retry, fallback, budget bypass, and record_cost provider param
- [ ] 26-02-PLAN.md — Per-provider cost breakdown query and Costs page UI

### Phase 27: Caller Migration
**Goal**: Every call site in the codebase uses call_model() with a logical tier name — no direct call_claude() calls or raw anthropic.Anthropic() usage remain in blueprint or orchestrator code
**Depends on**: Phase 26
**Requirements**: MIGR-01, MIGR-02, MIGR-03
**Success Criteria** (what must be TRUE):
  1. All 18+ call_claude() call sites are replaced with call_model() using logical tier names ("sonnet", "haiku", "opus")
  2. No blueprint or orchestrator module directly instantiates anthropic.Anthropic() — all such usage goes through the provider layer
  3. config.example.yaml contains a fully-documented `providers` section that demonstrates routing Sonnet to Gemini or Ollama
  4. The app starts and scores jobs end-to-end with the existing Anthropic config after migration
**Plans**: TBD

### Phase 28: Evaluation Framework
**Goal**: A CLI tool lets the developer run data-driven comparisons of alternative providers against stored Sonnet results, producing a JSON report with a clear SUITABLE/MARGINAL/NOT_RECOMMENDED verdict
**Depends on**: Phase 26
**Requirements**: EVAL-01, EVAL-02, EVAL-03, EVAL-04
**Success Criteria** (what must be TRUE):
  1. Running the CLI tool with a provider and sample size reconstructs Sonnet prompts from real stored job results and submits them to the target provider
  2. The report includes score correlation, schema adherence rate, and median latency for each sampled job
  3. The tool outputs a verdict (SUITABLE/MARGINAL/NOT_RECOMMENDED) computed from configurable thresholds against those metrics
  4. A JSON report file is saved to eval_results/ containing aggregate metrics and per-job details for offline review
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
| 22. Module Splits | v1.4 | 7/7 | Complete | 2026-03-27 |
| 23. N+1 Batching | v1.4 | 3/3 | Complete | 2026-03-27 |
| 24. Provider Foundation | v1.5 | 2/2 | Complete | 2026-03-27 |
| 25. Provider Adapters | v1.5 | 3/3 | Complete    | 2026-03-27 |
| 26. Dispatcher & Cost Tracking | v1.5 | 0/2 | Not started | - |
| 27. Caller Migration | v1.5 | 0/? | Not started | - |
| 28. Evaluation Framework | v1.5 | 0/? | Not started | - |
