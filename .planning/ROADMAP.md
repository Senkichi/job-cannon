# Roadmap: Job Cannon

## Milestones

- ✅ **v1.0 Foundation + AI Scoring + Pipeline Automation** — Phases 1-5 (shipped 2026-03-23)
- ✅ **v1.1 Port job-finder Improvements** — Phases 6-12 (shipped 2026-03-24)
- ✅ **v1.2 Migration & Stabilization** — Phases 13-14 (shipped 2026-03-24)
- ✅ **v1.3 Fixes & Improvements** — Phases 15-18 (shipped 2026-03-26)
- ✅ **v1.4 Tech Debt Sweep** — Phases 19-23 (shipped 2026-03-27)
- ✅ **v1.5 Multi-Provider Model Routing** — Phases 24-28 (shipped 2026-03-27)
- ✅ **v2.0 Cascading Free Provider Routing** — Phases 29-32 (shipped 2026-03-30)
- 🚧 **v3.0 Single-Tier Ordinal Scoring** — Phases 33-34 (started 2026-04-18)

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

<details>
<summary>✅ v2.0 Cascading Free Provider Routing (Phases 29-32) — SHIPPED 2026-03-30</summary>

- [x] Phase 29: Cascade Config & Rate Limiting (2/2 plans) — completed 2026-03-29
- [x] Phase 30: Cascade Execution (1/1 plan) — completed 2026-03-29
- [x] Phase 31: Prompts & Attribution (3/3 plans) — completed 2026-03-30
- [x] Phase 32: Integration & Config Wiring (1/1 plan) — completed 2026-03-30

</details>

### 🚧 v3.0 Single-Tier Ordinal Scoring (Phases 33-34) — STARTED 2026-04-18

- [x] **Phase 33: Local-LLM Site-Fitness Survey** — Evidence-driven multi-model shootout across 9 active AI call sites, producing a per-site winner matrix that drives Phase 34 model selection (completed 2026-04-21)
- [ ] **Phase 34: Greenfield Scorer Rewrite** — Five atomic, dependency-ordered plans delivering `job_scorer.py`, Migration 40+41, complete deletion of Haiku/calibration/cost-era scaffolding, and downstream consumer migration

## Phase Details

### Phase 33: Local-LLM Site-Fitness Survey

**Goal**: Select Phase 34's scoring model through rigorous, reproducible benchmarking. Produce a per-site winner matrix (4-7 candidate models × 9 active AI call sites) with site-appropriate metrics (MAE + |bias| + bootstrap CI for scoring sites; structural validation verdicts for non-scoring sites). No scorer code ships until the survey picks a model and validates its schema-adherence behavior.

**Depends on**: Nothing (first v3.0 phase; continues from v2.0 Phase 32)

**Requirements**: SURVEY-01, SURVEY-02, SURVEY-03, SURVEY-04, SURVEY-05, SURVEY-06, SURVEY-07, SURVEY-08, SURVEY-09, SURVEY-10, SURVEY-11, SURVEY-12, SURVEY-13, SCORER-11, SCORER-12

**Preconditions (must land before any benchmark run)**:
- SCORER-11 — `OllamaProvider.call()` passes JSON schema dict to `format=` (grammar-constrained decoding). Legacy `format="json"` path produces different outputs; mixing invalidates the winner matrix.
- SCORER-12 — Ollama inference parameters explicitly set: `temperature=0`, `seed=42`, `num_ctx=8192`, `top_p=0.9`, `num_predict=1024`, `repeat_penalty=1.05`. Default `num_ctx=2048` silently truncates long JDs.
- Two new model pulls complete: `ollama pull qwen3.5:27b` (17 GB) and `ollama pull phi4:14b` (9.1 GB).
- v3.0 scoring prompt frozen (committed) before the first shootout run (SURVEY-07).

**Success Criteria** (what must be TRUE):
  1. Per-site winner matrix committed to `.planning/research/v3.0-shootout-results.md` (or equivalent) containing, for each candidate × site combination: MAE + |bias| + bootstrap CI (scoring sites) or structural validation verdict + retry rate (non-scoring sites), plus a documented single-model-vs-tiered-mapping recommendation.
  2. Every candidate model verified deterministic at (`temperature=0, seed=42`) via 5× identical-input byte-comparison, OR explicitly flagged for multi-sample voting fallback. Determinism result recorded per candidate in the matrix artifact.
  3. Baseline pool explicitly filtered to `scoring_provider='anthropic'` cross-checked against `scoring_costs.provider`; filter query and resulting row count recorded in the survey methodology section. Candidates agreeing with contaminated Ollama majority cannot vacuously "win."
  4. Sample-size minima enforced and reported: n≥30 for scoring sites, n≥15 for extraction sites, n≥10 for HTML-reasoning sites, n≥5 for transformation. Pearson r suppressed below n=20.
  5. Per-candidate schema-retry rate measured and models exceeding 20% retry rate on any site flagged for disqualify-or-accept user review. VRAM baseline verified (<1 GB non-Ollama use) before each run via `nvidia-smi`.

**Plans**: 2 plans

- [x] 01-preconditions-PLAN.md — SCORER-11 (OllamaProvider format=<schema dict>) + SCORER-12 (deterministic inference options) + two Ollama model pulls (qwen3.5:27b, phi4:14b) + frozen v3.0 scoring prompt committed at job_finder/web/scoring_prompts/v3_scoring_prompt.py *(code complete 2026-04-19; model pulls deferred to user before Plan 02)*
- [x] 02-shootout-PLAN.md — scripts/v3_shootout.py + scripts/shootout_lib/ drive 6-candidate × 9-site shootout with Opus 4.6 gold baseline; commits per-site winner matrix to .planning/research/v3.0-shootout-results.md

### Phase 34: Greenfield Scorer Rewrite

**Goal**: Ship unified `job_scorer.py` emitting `JobAssessment` (six ordinal 1-5 sub-scores + Python-derived 4-way classification + 4-list rationale), migrate the DB schema, delete all two-tier / calibration / cost-era scaffolding, and migrate every downstream consumer. Every plan boundary is a shipping gate — the test suite (`uv run --active pytest -q --tb=short`) must pass at each boundary and the system must function between plans.

**Depends on**: Phase 33 (winner matrix drives model selection; SCORER-11 and SCORER-12 preconditions already shipped as part of Phase 33)

**Requirements**: SCORER-01, SCORER-02, SCORER-03, SCORER-04, SCORER-05, SCORER-06, SCORER-07, SCORER-08, SCORER-09, SCORER-10, SCORER-13, MIGRATE-01, MIGRATE-02, MIGRATE-03, MIGRATE-04, MIGRATE-05, COLLAPSE-01 through COLLAPSE-11, CONSUMERS-01 through CONSUMERS-16, TESTS-01 through TESTS-20

**Structure**: Single phase containing 5 dependency-ordered atomic plans (per ARCHITECTURE.md "Irreducible couplings summary"). Plans co-land specific commits that must not split; splitting into 5 separate phases would cross irreducible coupling boundaries (scorer-write + legacy-shim, template-read-swap + query-allowlist-swap, column-drop + JOBS_ALL_COLUMNS + JobRow TypedDict).

#### Plan 1 — Additive schema + scorer skeleton (no callers yet)

- **Scope**: Migration 40 (additive: `classification`, `sub_scores_json`, `scoring_model`, index); `JobAssessment` dataclass; `JOB_ASSESSMENT_SCHEMA`; `score_job()` pure function in new `job_scorer.py`; `persist_job_assessment()` in `db.py`. No callers yet — pure-function addition.
- **Requirements**: SCORER-01, SCORER-02, SCORER-03, SCORER-04, SCORER-05, SCORER-06, SCORER-07, SCORER-08, SCORER-09, SCORER-10, SCORER-13, MIGRATE-01, MIGRATE-02, TESTS-16, TESTS-18, TESTS-20
- **Rollback**: `git revert` + drop new columns. Byte-equivalent to pre-Plan 1.

#### Plan 2 — Orchestrator dual-write (reads unchanged, flagged)

- **Scope**: New `score_and_persist_job()`; `scoring_runner.run_scoring()`; writes to BOTH new columns AND legacy shim (`haiku_score ← mean sub-score × 20`, `sonnet_score ← same`, `haiku_summary ← first rationale item`); `use_unified_scorer: bool` config flag (default False); flip flag True; verify nightly + batch routes still work. Commit must include unified write AND shim atomically.
- **Requirements**: MIGRATE-05, TESTS-04, TESTS-05, TESTS-17, TESTS-20
- **Rollback**: Flip config flag False; `git revert` if bug is code not data.

#### Plan 3 — Read migration (5 sub-commits, dependency-ordered)

- **Scope**: 5 commits in dependency order, each independently revertable. Writes stay dual (Plan 2 shim still populates legacy columns). Commit A: query-layer (`db.py`, `exclusion_filter.py`, `careers_crawler.py`, `agentic_enricher.py`, `blueprints/companies.py`, `dedup_normalizer.py`, `rejection_analyzer.py`, `rejection_patterns.py`). Commit B: `batch_scoring.py` merge. Commit C: `dashboard.py` quick-actions + stats merge. Commit D: template updates (5 template files). Commit E: resume/interview gating flip + `pipeline_runner.run_ingestion` summary keys.
- **Requirements**: CONSUMERS-01 through CONSUMERS-16, TESTS-08, TESTS-09, TESTS-10, TESTS-11, TESTS-12, TESTS-13, TESTS-14, TESTS-19, TESTS-20
- **Rollback**: `git revert` per commit. Legacy columns still fresh from Plan 2 shim.

#### Plan 4 — Remove legacy writes + delete legacy modules

- **Scope**: Delete `haiku_scorer.py`, rename `sonnet_evaluator.py` → `job_scorer.py` (with `evaluate_job_sonnet` → `score_job`), delete `score_calibration.py`, `calibration_ollama_*.json`, `scripts/calibration_refit.py`, `_apply_calibration`, borderline re-eval path, `run_haiku_scoring`/`run_sonnet_evaluation`, `_run_batch_haiku_bg`/`_run_batch_sonnet_bg`, `persist_haiku_score`/`persist_sonnet_score`, `PROMPT_VARIANTS` block. Collapse `providers.haiku`/`providers.sonnet` → `providers.scoring` in `config.yaml` (Edit tool ONLY — never Write) and `config.example.yaml`. Update `_TIER_DEFAULTS`, `resolve_provider_config()`. Relax `_make_adapter` api_key guard and delete `_CLIClientStub` duplication. Migrate `_build_comp_context` and `build_description_snippet` helpers BEFORE deleting `haiku_scorer.py`.
- **Requirements**: COLLAPSE-01, COLLAPSE-02, COLLAPSE-03, COLLAPSE-04, COLLAPSE-05, COLLAPSE-06, COLLAPSE-07, COLLAPSE-08, COLLAPSE-09, COLLAPSE-10, TESTS-01, TESTS-02, TESTS-03, TESTS-06, TESTS-07, TESTS-15, TESTS-17, TESTS-20
- **Rollback**: `git revert`. Legacy column data is stale (last shim write at Plan 4 deploy); columns still exist.

#### Plan 5 — Drop legacy columns (destructive, backup-gated)

- **Scope**: Migration 41 drops `haiku_score`, `haiku_summary`, `sonnet_score`, and `idx_jobs_haiku_score`. Updates `JOBS_ALL_COLUMNS`, `RejectionPattern`, `JobRow` TypedDict. Updates test fixtures that insert these columns directly. Preflight runs the exhaustive grep checklist from ARCHITECTURE.md — every pattern must return 0 matches before migration executes.
- **Requirements**: MIGRATE-03, MIGRATE-04, COLLAPSE-11, TESTS-16, TESTS-20
- **Rollback**: Restore `jobs.db` from `backup_userdata.sh` snapshot. **No inline rollback** — gated on explicit confirmation that `bash backup_userdata.sh` was run within 24 hours.

**Success Criteria** (what must be TRUE):
  1. Test suite passes at every plan boundary. `uv run --active pytest -q --tb=short` green after each of Plans 1-5. No "temporarily broken" test states between plans.
  2. Pipeline run end-to-end produces a populated `JobAssessment` for a fresh job, visible in the dashboard with a classification badge (apply/consider/skip/reject) and per-dimension ordinal sub-scores in the expanded row detail. Verified against a live job post-Plan 3.
  3. Exhaustive grep checklist from ARCHITECTURE.md returns 0 matches for all legacy symbols (`score_job_haiku`, `evaluate_job_sonnet`, `persist_haiku_score`, `persist_sonnet_score`, `_apply_calibration`, `HAIKU_SCHEMA`, `SONNET_SCHEMA`, `"haiku_score"`, `"sonnet_score"`, `tier="haiku"`, `tier="sonnet"`, `providers.haiku`, `providers.sonnet`, `DEFAULT_MODEL_HAIKU`, `DEFAULT_MODEL_SONNET`, `haiku_threshold`, `borderline_high`, `"haiku_scored"`, `"sonnet_evaluated"`, `"sonnet_queued"`) before Migration 41 runs.
  4. Migration 41 gated on confirmed backup within 24 hours. Plan 5 runbook documents the gate; `bash backup_userdata.sh` timestamp verified before DROP COLUMN executes. No inline rollback path — DB restore from backup is the recovery strategy.
  5. One-off rescore of existing jobs completes. All existing jobs (`WHERE classification IS NULL AND jd_full IS NOT NULL`, ~3900 rows) scored through the unified scorer; post-rescore query `SELECT COUNT(*) FROM jobs WHERE classification IS NOT NULL AND jd_full IS NOT NULL` matches expected count. Acceptable wall-clock ~8-13 hours on Ollama, run overnight.

**Plans**: 5 plans

- [ ] 34-01-PLAN.md — Additive schema (Migration 40) + JobAssessment/derive_classification/persist_job_assessment in db.py + job_scorer.py skeleton (no callers yet)
- [ ] 34-02-PLAN.md — Orchestrator dual-write: score_and_persist_job + run_scoring + use_unified_scorer flag (2-step rollout A→B)
- [ ] 34-03-PLAN.md — Read migration in 5 revertable sub-commits A→E (queries, batch_scoring merge, dashboard, templates, resume+summary)
- [ ] 34-04-PLAN.md — Batched rescore (B1=150, B2=1000, B3=remaining ~2750) with G1-G4 gates + systematic-debugging loop, then legacy-write removal + module deletion sweep (A→E)
- [ ] 34-05-PLAN.md — Migration 41 (destructive column drop) backup-gated + preflight-grep-gated; JOBS_ALL_COLUMNS/JobRow TypedDict/fixture cleanup
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
| 29. Cascade Config & Rate Limiting | v2.0 | 2/2 | Complete | 2026-03-29 |
| 30. Cascade Execution | v2.0 | 1/1 | Complete | 2026-03-29 |
| 31. Prompts & Attribution | v2.0 | 3/3 | Complete | 2026-03-30 |
| 32. Integration & Config Wiring | v2.0 | 1/1 | Complete | 2026-03-30 |
| 33. Local-LLM Site-Fitness Survey | v3.0 | 2/2 | Complete    | 2026-04-21 |
| 34. Greenfield Scorer Rewrite | v3.0 | 0/5 | Not started | — |
