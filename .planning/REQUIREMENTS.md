# Requirements: Job Cannon v3.0 Single-Tier Ordinal Scoring

**Defined:** 2026-04-18
**Core Value:** Surface the best-fit jobs fast and keep the application pipeline visible — every job gets scored, every status change gets tracked, nothing falls through the cracks.

**Milestone goal:** Collapse the vestigial Haiku/Sonnet two-tier architecture into a single-pass, Ollama-native job scorer emitting ordinal rubric output, eliminating the calibration infrastructure and all cost-era scaffolding.

**Design anchors (locked after deep architectural discussion + research synthesis):**
- **Ordinal, not continuous** — 6 sub-scores on 1-5 scale, not a 0-100 float (literature-settled: arXiv 2601.03444, ICC 0.853 at 0-5 vs 0.840 at 0-100)
- **Python-derived classification** — model emits ordinals only; classification (apply/consider/skip/reject) derived from sub-scores + legitimacy_note
- **All-LLM assessment** — no Python pre-parsing of title/location/salary (those fields are chaotic; LLM reads full JD and reconciles)
- **Preserve `fit_analysis` column** — rationale shape unchanged; out-of-scope modules (resume_generator, interview_prep) keep working
- **Shared dispatcher** — scorer uses `call_model(tier="scoring", ...)`; no scorer-specific code path
- **Delete calibration entirely** — no continuous score means no calibration layer means no latent coupling

## v1 Requirements (milestone v3.0)

### SCORER — Unified scorer behavior (Phase 34 — Greenfield Rewrite)

- [x] **SCORER-01
**: System produces a single `JobAssessment` per job with six ordinal sub-scores on 1-5 integer scale: `title_fit`, `location_fit`, `comp_fit`, `domain_match`, `seniority_match`, `skills_match`.
- [x] **SCORER-02
**: System derives `classification ∈ {apply, consider, skip, reject}` deterministically in Python from sub-scores + `legitimacy_note`, not from LLM free-form output. Rule: `reject ← legitimacy_note is truthy OR any sub-score = 1`; `apply ← all sub-scores >= 3`; `consider ← all sub-scores >= 2`; else `skip`.
- [x] **SCORER-03
**: System emits structured rationale preserving existing shape: `strengths: list[str]`, `gaps: list[str]`, `talking_points: list[str]`, `resume_priority_skills: list[str]`. Serialized to the retained `fit_analysis` column.
- [x] **SCORER-04
**: System enforces schema validation via `jsonschema` belt-and-suspenders after grammar-constrained decoding; invalid output triggers dispatcher retry-with-error-hint before falling back to next provider.
- [x] **SCORER-05
**: Scorer skips jobs where `jd_full` is empty, logs the skip, and returns `ScoringResult(status="skipped", data=None)` — does not crash or produce meaningless output.
- [x] **SCORER-06
**: Prompt includes behavioral anchors per dimension (concrete examples of score-1, score-3, score-5 for each sub-score) to suppress central-tendency bias on local models.
- [x] **SCORER-07
**: Prompt includes fewshot calibration examples spanning all levels (ported from `sonnet_evaluator._FEWSHOT_EXAMPLES` pattern).
- [x] **SCORER-08
**: Prompt includes a field-name reinforcement block equivalent to current `_FIELD_REINFORCEMENT` (local models invent `weaknesses` for `gaps`, `role_fit` for `title_fit`; reinforcement suppresses the rename).
- [x] **SCORER-09
**: Scorer preserves `liveness_check` pre-score gate (stays inside the unified scoring runner; no architectural relocation in v3.0).
- [x] **SCORER-10
**: Scorer uses shared `call_model(tier="scoring", ...)` dispatcher — no scorer-specific dispatch path. Inherits schema retry, cascade fallback, rate limiting, provider attribution.
- [x] **SCORER-11**: Ollama provider passes the JSON schema dict to `format=` (grammar-constrained decoding) instead of the legacy `format="json"` + prompt-instructions path. Upgrade applies to `OllamaProvider.call()` and cascades into deleting `_schema_to_field_instructions`, `_schema_to_example`, most of `_sanitize_output`.
- [x] **SCORER-12**: Ollama inference parameters explicitly set: `temperature=0`, `seed=42` (benchmark), `num_ctx=8192`, `top_p=0.9`, `num_predict=1024`, `repeat_penalty=1.05`. Default `num_ctx=2048` silently truncates long JDs.
- [x] **SCORER-13
**: No `PROMPT_VARIANTS` infrastructure in new scorer. If future A/B is needed, re-add then with real data.

### SURVEY — Local-LLM Site-Fitness Survey (Phase 33 — Phase 1)

- [x] **SURVEY-01**: `scripts/quality_cascade_validator.py` is expanded into a multi-model shootout tool that runs each candidate model against every active AI call site with site-appropriate sample sizes and produces a per-site verdict matrix.
- [x] **SURVEY-02**: Candidate model shortlist is benchmarked: `qwen3.5:27b` (new pull), `phi4:14b` (new pull), `qwen2.5:14b` (incumbent/control), `qwen2.5:32b`, `qwen3:14b`, `gemma3:27b`. Excluded with documented rationale: `qwen3.5:14b` (tag does not exist on Ollama), `gemma4:26b-moe` (open bug #15260), `deepseek-r1:14b` (reasoning overhead wasted on rubric task).
- [x] **SURVEY-03**: Scoring-site baselines are filtered to `scoring_provider = 'anthropic'` cross-checked against `scoring_costs.provider`, to eliminate the ~52%/27% post-flip contamination documented in PROJECT.md.
- [x] **SURVEY-04**: Sample-size minima enforced: n≥30 for scoring sites (`haiku_score`, `sonnet_eval`), n≥15 for extraction sites (`enrich_job`, `enrich_job_sonnet`, `homepage_backfill`), n≥10 for HTML reasoning sites (`careers_scrape_url`, `careers_scrape_jobs`, `ai_nav_discovery`), n≥5 for transformation (`description_reformat`).
- [x] **SURVEY-05**: `ollama_provider.py:197-203` is patched to set `temperature=0`, `seed=42`, `num_ctx=8192` BEFORE any benchmark run. Default `temperature=0.8` and `num_ctx=2048` make measurements meaningless.
- [x] **SURVEY-06**: `OllamaProvider.call()` grammar-constrained decoding upgrade lands BEFORE the shootout. Legacy `format="json"` path produces different outputs than the schema-dict path; mixing invalidates the winner matrix.
- [x] **SURVEY-07**: The v3.0 scoring prompt is frozen BEFORE Phase 33 benchmark begins. If iteration is needed mid-shootout, all previously-tested candidates re-run from scratch against the new prompt.
- [x] **SURVEY-08**: Per-candidate determinism is verified — 5× identical-input runs at `temperature=0`, `seed=42` must produce byte-identical output. Models that ignore seed are flagged and tested with multi-sample voting fallback.
- [x] **SURVEY-09**: Scoring-site verdicts use MAE + |bias| + bootstrap CI (not Pearson r alone). Pearson r is suppressed for n < 20 (mathematically 1.0 at n=2 — theater).
- [x] **SURVEY-10**: Per-candidate schema-retry rate is measured and models exceeding 20% retry rate on any site are flagged for disqualification-or-accept user review.
- [x] **SURVEY-11**: Phase 33 output is a winner matrix artifact committed to `.planning/research/v3.0-shootout-results.md` (or similar) containing per-site metric tables, per-candidate verdicts, and a recommendation (single model vs tiered mapping).
- [x] **SURVEY-12**: VRAM baseline is verified before each shootout run — `nvidia-smi` shows <1 GB non-Ollama VRAM use (browsers closed, no other GPU workload).
- [x] **SURVEY-13**: Shootout results feed Phase 34 Plan 1's `JOB_ASSESSMENT_SCHEMA` — no scorer code ships until the survey selects a model and validates its schema-adherence behavior.

### MIGRATE — Schema migration (Phase 34 — Greenfield Rewrite, Plans 1/5)

- [x] **MIGRATE-01
**: Migration 40 (additive) adds `classification TEXT DEFAULT NULL`, `sub_scores_json TEXT DEFAULT NULL`, and `CREATE INDEX idx_jobs_classification ON jobs(classification)`. Does not modify existing columns.
- [x] **MIGRATE-02
**: Migration 40 also adds `scoring_model TEXT DEFAULT NULL` alongside existing `scoring_provider` column, closing the `(provider, tier)` → `(provider, model)` keying gap.
- [ ] **MIGRATE-03**: Migration 41 (destructive) drops `haiku_score`, `haiku_summary`, `sonnet_score` and the `idx_jobs_haiku_score` index. Preserves `fit_analysis` (now holds rationale payload), `scoring_provider`, `scoring_model`, `eval_blocks` (vestigial but separate sweep), `opus_score` (separate concern), `job_archetype`, `legitimacy_note`, `score` (legacy tiebreaker).
- [ ] **MIGRATE-04**: Migration 41 is gated on explicit confirmation that `bash backup_userdata.sh` was run within 24 hours. The plan's runbook documents this gate. No inline rollback for Migration 41; recovery is DB restore from backup.
- [ ] **MIGRATE-05**: A one-off rescore of all existing jobs (`WHERE classification IS NULL AND jd_full IS NOT NULL` — ~3900 rows) runs through the new unified scorer as a plan deliverable. Can run overnight; acceptable wall-clock ~8-13 hours on Ollama.

### TIER-COLLAPSE — Deletion of two-tier infrastructure (Phase 34 — Plan 4)

- [ ] **COLLAPSE-01**: `job_finder/web/haiku_scorer.py` is deleted entirely. Reusable helpers (`build_description_snippet`, `_build_comp_context`) migrate to a shared module (e.g. `scoring_types.py`) BEFORE deletion if the new scorer uses them.
- [ ] **COLLAPSE-02**: `job_finder/web/sonnet_evaluator.py` is renamed to `job_finder/web/job_scorer.py`. All imports across the codebase are updated. `evaluate_job_sonnet()` renames to `score_job()`.
- [ ] **COLLAPSE-03**: `job_finder/web/score_calibration.py` is deleted entirely, along with all `calibration_ollama_*.json` files, `scripts/calibration_refit.py`, and `scoring_orchestrator._apply_calibration()`.
- [ ] **COLLAPSE-04**: `scoring_orchestrator.score_and_persist_haiku()` and `score_and_persist_sonnet()` are deleted. Replaced by single `score_and_persist_job()`. Orchestrator shrinks ~60%.
- [ ] **COLLAPSE-05**: Borderline re-eval path (`scoring_orchestrator.py:138-160`) is deleted. No replacement hook. If a future milestone surfaces a real ambiguity signal, it adds its own handler then.
- [ ] **COLLAPSE-06**: `scoring_runner.run_haiku_scoring()` and `run_sonnet_evaluation()` are deleted. Replaced by single `run_scoring()`.
- [ ] **COLLAPSE-07**: `db.persist_haiku_score()` and `persist_sonnet_score()` are deleted. Replaced by `persist_job_assessment(conn, dedup_key, assessment, provider, model)`.
- [ ] **COLLAPSE-08**: `providers.haiku` and `providers.sonnet` config keys are collapsed to `providers.scoring` in both `config.yaml` and `config.example.yaml`. `config.yaml` is modified via Edit tool only (per CLAUDE.md — the file has been wiped 3 times by full-file writes).
- [ ] **COLLAPSE-09**: `_TIER_DEFAULTS` dict at `model_provider.py:28-32` is updated or removed (currently maps `haiku`/`sonnet`/`opus` to Anthropic model IDs — vestigial once those aren't tier names). `resolve_provider_config()` inherit-from-peer-tier fallback at lines 151-161 is simplified to `providers_cfg.get(tier, {})`.
- [ ] **COLLAPSE-10**: `_CLIClientStub` duplication between `haiku_scorer.py` and `sonnet_evaluator.py` is resolved — the underlying `api_key` guard in `_make_adapter` is relaxed first; stubs are deleted as the modules are deleted.
- [ ] **COLLAPSE-11**: Plan 5 preflight runs the exhaustive grep checklist from ARCHITECTURE.md (`score_job_haiku`, `evaluate_job_sonnet`, `persist_haiku_score`, `persist_sonnet_score`, `_apply_calibration`, `calibrate_score`, `HAIKU_SCHEMA`, `SONNET_SCHEMA`, `"haiku_score"`, `"sonnet_score"`, `"haiku_summary"`, `tier="haiku"`, `tier="sonnet"`, `providers.haiku`, `providers.sonnet`, `haiku_threshold`, `borderline_high`, `DEFAULT_MODEL_HAIKU`, `DEFAULT_MODEL_SONNET`, `"haiku_scored"`, `"sonnet_evaluated"`, `"sonnet_queued"`). Every pattern must return 0 matches before Migration 41 runs.

### CONSUMERS — Downstream consumer updates (Phase 34 — Plan 3 read migration)

- [ ] **CONSUMERS-01**: `careers_crawler.py:625` gate `WHERE j.haiku_score >= ?` is replaced with `WHERE j.classification IN ('apply','consider')`. Summary counters `haiku_scored`/`sonnet_evaluated` collapse to `scored` + per-classification counts.
- [ ] **CONSUMERS-02**: `ats_scanner.py:675-1100` summary counters are updated to match new scheme.
- [ ] **CONSUMERS-03**: `agentic_enricher.py:500` `ORDER BY haiku_score DESC NULLS LAST` is replaced with classification-rank ordering + sub-score sum tiebreak.
- [ ] **CONSUMERS-04**: `blueprints/batch_scoring.py` — `batch_score_haiku_start()` and `batch_score_sonnet_start()` routes merge into single `batch_score_start()`. `_run_batch_haiku_bg` and `_run_batch_sonnet_bg` merge into `_run_batch_bg`. Route handlers and templates updated. `session_type` enum collapses from `{haiku, sonnet, sync}` to `{scoring, sync}`.
- [ ] **CONSUMERS-05**: `blueprints/dashboard.py:180-194` — separate Haiku/Sonnet eligible counts merge into single `scoring_eligible_count`. `_cached_tier_available("haiku")` / `("sonnet")` calls replaced with `("scoring")`.
- [ ] **CONSUMERS-06**: `blueprints/companies.py:141, 144, 292-293` — `COALESCE(sonnet_score, haiku_score, score) as effective_score` replaced with classification-rank ordering.
- [ ] **CONSUMERS-07**: `blueprints/resume.py:62, 210` — `if job.get("sonnet_score") is None` preconditions replaced with `if job.get("classification") is None`.
- [ ] **CONSUMERS-08**: `exclusion_filter.py:90` — `count_haiku_scorable()` renamed to `count_scorable()`; predicate `haiku_score IS NULL` replaced with `classification IS NULL`.
- [ ] **CONSUMERS-09**: `db.py::get_filtered_jobs()` — `COALESCE(sonnet_score, haiku_score, score)` sort and `min_score`/`max_score` filter predicates replaced with classification-rank ordering + optional per-sub-score range filters. `allowed_sort_cols` drops `haiku_score`/`sonnet_score`.
- [ ] **CONSUMERS-10**: `dedup_normalizer.py:287-394` merge logic — haiku_score/sonnet_score merge replaced with classification priority merge (apply > consider > skip > reject) and element-wise max on sub-scores.
- [ ] **CONSUMERS-11**: `rejection_analyzer.py:134-173` SELECT column list and prompt adapt to new columns.
- [ ] **CONSUMERS-12**: `rejection_patterns.py:24-200` — `RejectionPattern` dataclass fields `haiku_score`/`sonnet_score` replaced with `classification`/`sub_scores`.
- [ ] **CONSUMERS-13**: `resume_generator.py:419` — `sonnet_score >= multi_threshold` gate replaced with `classification == 'apply'`. (Resume generation itself is out-of-scope; only the gate changes.)
- [ ] **CONSUMERS-14**: `pipeline_runner.run_ingestion()` — summary keys `haiku_scored`, `sonnet_queued`, `sonnet_queue`, `sonnet_evaluated` collapse to `scored`, `classified_apply`, `classified_consider`, `classified_skip`, `classified_reject`.
- [ ] **CONSUMERS-15**: `backfill_enrichment.py` — `_OFFLINE_PROVIDERS` collapses to single tier. `run_borderline_rescore()` deleted. `run_sonnet_backfill()` renamed to `run_scoring_backfill()`; predicate changes from `sonnet_score IS NULL` to `classification IS NULL`. `estimate_and_confirm()` updated for single-tier cost math.
- [ ] **CONSUMERS-16**: Templates updated — `_row_detail.html` (lines 6, 67, 109-149), `_row_expanded.html` (lines 45, 104-213), `_score_cell.html` (line 9), `_resume_section.html` (lines 54, 80), `costs/index.html` (line 148). Score-badge logic changes from numeric to enum-colored. Classification badge + sub-score radar/bars in detail view.

### TESTS — Test modernization (Phase 34 — all plans)

- [ ] **TESTS-01**: `tests/test_scoring_evaluator.py` is deleted. `test_scoring_orchestrator_calibration.py` is deleted.
- [ ] **TESTS-02**: `tests/test_scoring.py` is rewritten as `test_job_scorer.py`, testing `score_job()` against the ordinal rubric schema.
- [ ] **TESTS-03**: `test_scoring_runner.py` is rewritten against single `run_scoring()` entry point.
- [ ] **TESTS-04**: `test_cascade_dispatch.py` fixtures updated: `tier="haiku"` / `tier="sonnet"` → `tier="scoring"`.
- [ ] **TESTS-05**: `test_eval_provider.py` updated: attribution flows via `persist_job_assessment(provider, model)`.
- [ ] **TESTS-06**: `test_backfill_enrichment.py` — borderline tests deleted, sonnet-backfill tests updated to scoring-backfill.
- [ ] **TESTS-07**: `test_batch_scoring.py` — haiku/sonnet route tests merge into single scoring route tests.
- [ ] **TESTS-08**: `test_careers_crawler.py` — gate assertion updated from `haiku_score >=` to `classification IN (…)`.
- [ ] **TESTS-09**: `test_ats_scanner.py` — summary counter key assertions updated.
- [ ] **TESTS-10**: `test_rejection_patterns.py` — dataclass field assertions updated.
- [ ] **TESTS-11**: `test_rejection_analyzer.py` — SELECT column expectations updated.
- [ ] **TESTS-12**: `test_opus_baseline.py` — baseline comparison updated to new columns.
- [ ] **TESTS-13**: `test_ingestion.py` — summary key assertions updated.
- [ ] **TESTS-14**: `test_views.py` — HTMX response body assertions updated.
- [ ] **TESTS-15**: `test_db.py` — `persist_haiku_score`/`persist_sonnet_score` tests replaced with `persist_job_assessment` tests.
- [x] **TESTS-16
**: `test_migration.py` — tests added for Migration 40 and 41 (additive shape, destructive shape, roundtrip via `user_version`).
- [ ] **TESTS-17**: `conftest.py` autouse `mock_run_oneshot` envelope updated to `JobAssessment` shape. `cascade_config_haiku`/`cascade_config_sonnet` replaced with `cascade_config_scoring`. `make_model_result` default `data` ordinal-shaped.
- [x] **TESTS-18
**: New `migrated_db_with_scored_jobs` fixture pre-inserts jobs with populated `classification` / `sub_scores_json` / `fit_analysis` for template and query tests.
- [ ] **TESTS-19**: Remaining test files (resume, dedup, costs, claude_client, agentic_enricher, data_enricher, resume_validator) receive targeted column renames only.
- [x] **TESTS-20
**: Test suite passes (`uv run --active pytest -q --tb=short`) at every plan boundary — shipping gate. No "temporarily broken" test states between plans.

## v2 Requirements (deferred to v3.1 or later)

### Observability (v3.1 candidate)

- **OBSERV-01**: Distribution monitoring — surface a dashboard if any rubric dimension shows >40% mass concentrated on one value (indicates collapsed resolution, prompt needs re-anchoring).
- **OBSERV-02**: Classification bucket usage tracking — if `user_activity` shows `skip` and `reject` are used identically, collapse to 3-way in v3.1.
- **OBSERV-03**: Per-candidate schema-retry rate in production — flag regression if any site exceeds 20% retry.

### Configurable weighting (v3.1+ candidate)

- **WEIGHTS-01**: User-configurable per-dimension weights in the hidden sort-order weighted-sum. Deferred until observed user behavior justifies the UX cost.

### Structural cleanup (separate milestone)

- **CLEANUP-01**: Delete vestigial Phase 4/5 generation code (resume_generator, interview_prep, rejection_analyzer, profile extraction) — non-functional in weeks per PROJECT.md. Independent of v3.0 scoring rewrite.
- **CLEANUP-02**: Update CLAUDE.md — currently says Phases 4/5 "Operational" which is stale; will mislead future work.
- **CLEANUP-03**: Investigate upstream sources producing 500-1000-job bulk batches — likely dedup gaps, crawler overreach, or too-wide search config. Filter-at-scorer would mask the bug.
- **CLEANUP-04**: `liveness_check` relocation to ingestion layer — deferred per v3.0 decision (stays pre-score).
- **CLEANUP-05**: `eval_blocks` column removal — vestigial, already decoupled, separate sweep.
- **CLEANUP-06**: `score` legacy heuristic column — final tiebreaker post-v3.0; separate cleanup.

## Out of Scope

| Feature | Reason |
|---------|--------|
| Generation modules (resume, interview prep, rejection analysis) | Vestigial per user confirmation; non-functional in weeks; separate cleanup milestone (CLEANUP-01) |
| Profile extraction rewrite (Opus tier) | Separate concern; one-time operation; `opus_score` column stays untouched |
| User-configurable rubric weights | YAGNI for single-user system; observe default behavior first (WEIGHTS-01 v3.1) |
| `culture_fit` dimension | Well-documented bias amplifier (Eightfold AI, LSE Business Review); explicit anti-feature |
| `growth_potential`, soft-skills, communication dimensions | Not assessable from JD alone; would introduce hallucination surface |
| `confidence` field on LLM output | LLM-as-judge systems are systematically overconfident (arXiv 2508.06225) |
| Per-dimension rationale blocks | Output bloat; degrades schema adherence on local models |
| Exposed numeric overall score | Reintroduces the exact failure mode v3.0 exists to eliminate |
| 1-7 or 1-10 ordinal scale | Strictly worse than 1-5 on LLM alignment (arXiv 2601.03444) |
| CoT / `meta_reasoning` output field | Traces are post-hoc rationalizations, not causal |
| Filter-at-scorer to mitigate bulk batches | Masks upstream dedup/crawler bug (CLEANUP-03) |
| DeepSeek-R1 distill models | Reasoning-token latency overhead wasted on rubric task |
| Qwen3-Next 80B | 0.5 tok/s unusable for batch scoring |
| New Python dependencies | Verified: `jsonschema 4.26.0` and `pydantic 2.12.5` already installed via transitive deps |
| Deployment, Docker, CI/CD | Local-only app per project constraints |
| ORM | Raw SQL intentional at this scale |

## Traceability

Each requirement maps to exactly one phase. Phase 34 entries include plan sub-assignment to preserve the build-order dependency ordering.

| Requirement | Phase | Status |
|-------------|-------|--------|
| SURVEY-01 | Phase 33 | Complete |
| SURVEY-02 | Phase 33 | Complete |
| SURVEY-03 | Phase 33 | Complete |
| SURVEY-04 | Phase 33 | Complete |
| SURVEY-05 | Phase 33 | Complete |
| SURVEY-06 | Phase 33 | Complete |
| SURVEY-07 | Phase 33 | Complete |
| SURVEY-08 | Phase 33 | Complete |
| SURVEY-09 | Phase 33 | Complete |
| SURVEY-10 | Phase 33 | Complete |
| SURVEY-11 | Phase 33 | Complete |
| SURVEY-12 | Phase 33 | Complete |
| SURVEY-13 | Phase 33 | Complete |
| SCORER-01 | Phase 34 (Plan 1) | Pending |
| SCORER-02 | Phase 34 (Plan 1) | Pending |
| SCORER-03 | Phase 34 (Plan 1) | Pending |
| SCORER-04 | Phase 34 (Plan 1) | Pending |
| SCORER-05 | Phase 34 (Plan 1) | Pending |
| SCORER-06 | Phase 34 (Plan 1) | Pending |
| SCORER-07 | Phase 34 (Plan 1) | Pending |
| SCORER-08 | Phase 34 (Plan 1) | Pending |
| SCORER-09 | Phase 34 (Plan 1) | Pending |
| SCORER-10 | Phase 34 (Plan 1) | Pending |
| SCORER-11 | Phase 33 (precondition) | Complete |
| SCORER-12 | Phase 33 (precondition) | Complete |
| SCORER-13 | Phase 34 (Plan 1) | Pending |
| MIGRATE-01 | Phase 34 (Plan 1) | Pending |
| MIGRATE-02 | Phase 34 (Plan 1) | Pending |
| MIGRATE-03 | Phase 34 (Plan 5) | Pending |
| MIGRATE-04 | Phase 34 (Plan 5) | Pending |
| MIGRATE-05 | Phase 34 (Plan 2) | Pending |
| COLLAPSE-01 | Phase 34 (Plan 4) | Pending |
| COLLAPSE-02 | Phase 34 (Plan 4) | Pending |
| COLLAPSE-03 | Phase 34 (Plan 4) | Pending |
| COLLAPSE-04 | Phase 34 (Plan 4) | Pending |
| COLLAPSE-05 | Phase 34 (Plan 4) | Pending |
| COLLAPSE-06 | Phase 34 (Plan 4) | Pending |
| COLLAPSE-07 | Phase 34 (Plan 4) | Pending |
| COLLAPSE-08 | Phase 34 (Plan 4) | Pending |
| COLLAPSE-09 | Phase 34 (Plan 4) | Pending |
| COLLAPSE-10 | Phase 34 (Plan 4) | Pending |
| COLLAPSE-11 | Phase 34 (Plan 5) | Pending |
| CONSUMERS-01 | Phase 34 (Plan 3) | Pending |
| CONSUMERS-02 | Phase 34 (Plan 3) | Pending |
| CONSUMERS-03 | Phase 34 (Plan 3) | Pending |
| CONSUMERS-04 | Phase 34 (Plan 3) | Pending |
| CONSUMERS-05 | Phase 34 (Plan 3) | Pending |
| CONSUMERS-06 | Phase 34 (Plan 3) | Pending |
| CONSUMERS-07 | Phase 34 (Plan 3) | Pending |
| CONSUMERS-08 | Phase 34 (Plan 3) | Pending |
| CONSUMERS-09 | Phase 34 (Plan 3) | Pending |
| CONSUMERS-10 | Phase 34 (Plan 3) | Pending |
| CONSUMERS-11 | Phase 34 (Plan 3) | Pending |
| CONSUMERS-12 | Phase 34 (Plan 3) | Pending |
| CONSUMERS-13 | Phase 34 (Plan 3) | Pending |
| CONSUMERS-14 | Phase 34 (Plan 3) | Pending |
| CONSUMERS-15 | Phase 34 (Plan 3) | Pending |
| CONSUMERS-16 | Phase 34 (Plan 3) | Pending |
| TESTS-01 | Phase 34 (Plan 4) | Pending |
| TESTS-02 | Phase 34 (Plan 4) | Pending |
| TESTS-03 | Phase 34 (Plan 4) | Pending |
| TESTS-04 | Phase 34 (Plan 2) | Pending |
| TESTS-05 | Phase 34 (Plan 2) | Pending |
| TESTS-06 | Phase 34 (Plan 4) | Pending |
| TESTS-07 | Phase 34 (Plan 4) | Pending |
| TESTS-08 | Phase 34 (Plan 3) | Pending |
| TESTS-09 | Phase 34 (Plan 3) | Pending |
| TESTS-10 | Phase 34 (Plan 3) | Pending |
| TESTS-11 | Phase 34 (Plan 3) | Pending |
| TESTS-12 | Phase 34 (Plan 3) | Pending |
| TESTS-13 | Phase 34 (Plan 3) | Pending |
| TESTS-14 | Phase 34 (Plan 3) | Pending |
| TESTS-15 | Phase 34 (Plan 4) | Pending |
| TESTS-16 | Phase 34 (Plans 1 & 5) | Pending |
| TESTS-17 | Phase 34 (Plans 2 & 4) | Pending |
| TESTS-18 | Phase 34 (Plan 1) | Pending |
| TESTS-19 | Phase 34 (Plan 3) | Pending |
| TESTS-20 | Phase 34 (all plans — shipping gate) | Pending |

**Coverage:**
- v1 requirements: 77 total (13 SURVEY + 13 SCORER + 5 MIGRATE + 11 COLLAPSE + 16 CONSUMERS + 20 TESTS — one SCORER item spans Phase 33 as precondition)
- Mapped to phases: 77 ✓
- Unmapped: 0 ✓
- Phase 33 requirement count: 15 (SURVEY-01..13 + SCORER-11 + SCORER-12)
- Phase 34 requirement count: 62 (11 remaining SCORER + 5 MIGRATE + 11 COLLAPSE + 16 CONSUMERS + 20 TESTS − 1 overlap offset accounted above)

**Phase 34 plan distribution:**
- Plan 1 (additive schema + scorer skeleton): 14 requirements
- Plan 2 (orchestrator dual-write): 5 requirements
- Plan 3 (read migration): 24 requirements
- Plan 4 (remove legacy writes + delete modules): 17 requirements
- Plan 5 (drop legacy columns): 4 requirements
- TESTS-16 and TESTS-17 span two plans each; TESTS-20 is a cross-plan shipping gate

---
*Requirements defined: 2026-04-18*
*Last updated: 2026-04-18 — traceability populated during roadmap creation*
