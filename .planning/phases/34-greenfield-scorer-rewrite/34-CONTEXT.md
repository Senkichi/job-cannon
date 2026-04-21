# Phase 34: Greenfield Scorer Rewrite - Context

**Gathered:** 2026-04-21
**Status:** Ready for planning

<domain>
## Phase Boundary

Ship unified `job_scorer.py` emitting `JobAssessment` (six ordinal 1-5 sub-scores + Python-derived 4-way classification + 4-list rationale), migrate the DB schema via Migration 40 (additive) and Migration 41 (destructive), delete all two-tier / calibration / cost-era scaffolding, and migrate every downstream consumer. Test suite (`uv run --active pytest -q --tb=short`) must pass at every plan boundary and the system must function between plans.

5 atomic, dependency-ordered plans per ARCHITECTURE.md "Irreducible couplings summary" — plans co-land specific commits that must not split; splitting into 5 separate phases would cross irreducible coupling boundaries.

</domain>

<decisions>
## Implementation Decisions

### Locked from Phase 33 (carry-forward — do not revisit)

- **D-01** — Scoring model: `providers.scoring.model = "qwen2.5:14b"` as the default for all AI call-sites. Single-model sweep supersedes the renderer's per-site split (rationale: 3.5% MAE cost vs 2.4× throughput + tightest bias CI + single deployment config axis). Recorded in `.planning/phases/33-local-llm-site-fitness-survey/02-shootout-SUMMARY.md` Completion Addendum.
- **D-02** — v3 scoring prompt frozen: `job_finder/web/scoring_prompts/v3_scoring_prompt.py` at sha256 `255c690e...d0c9da`. No prompt modifications allowed in Phase 34 unless Plan 4 rescore gate fails with root cause in prompt (see D-24).
- **D-03** — Ollama inference parameters locked: `temperature=0, seed=42, num_ctx=8192, top_p=0.9, num_predict=1024, repeat_penalty=1.05`. Already shipped in SCORER-12.
- **D-04** — Grammar-constrained decoding via Ollama `format=<schema dict>`. Already shipped in SCORER-11.

### Data model

- **D-05** — `JobAssessment` dataclass shape (frozen):
  ```python
  @dataclass(frozen=True)
  class JobAssessment:
      sub_scores: dict[str, int]   # 6 ordinal 1-5 keys: title_fit, location_fit, comp_fit,
                                   #                     domain_match, seniority_match, skills_match
      classification: str          # derived: apply|consider|skip|reject
      rationale: dict              # strengths, gaps, talking_points, resume_priority_skills (list[str] each)
      provider: str | None         # attribution from call_model return
  ```
- **D-06** — Classification rule is Python-derived, not LLM-emitted (derive at `persist_job_assessment` time):
  ```python
  def derive_classification(sub_scores: dict, legitimacy_note: str | None) -> str:
      if legitimacy_note: return "reject"
      if any(v == 1 for v in sub_scores.values()): return "reject"
      if all(v >= 3 for v in sub_scores.values()): return "apply"
      if all(v >= 2 for v in sub_scores.values()): return "consider"
      return "skip"
  ```
- **D-07** — `legitimacy_note` sourcing: use existing `jobs.legitimacy_note` column (populated by ingestion-time scam/exclusion detection). Scorer does NOT emit this field. The classification rule reads it from the row at persist time, not from LLM output.
- **D-08** — `fit_analysis` column reused to hold rationale payload (strengths/gaps/talking_points/resume_priority_skills). Zero-change compatibility with `resume_generator.py`, `resume_multi_version.py`, `interview_prep.py` (all out-of-scope for v3.0).

### Dispatch & fallback

- **D-09** — Scorer routes through shared `call_model(tier="scoring", ...)`. No scorer-specific dispatch path. Inherits schema retry, cascade fallback, rate limiting, provider attribution. The `JOB_ASSESSMENT_SCHEMA` is passed as `output_schema` argument.
- **D-10** — Cascade fallback: scoring tier inherits the full cascade chain (Ollama → Groq → Cerebras → Gemini → Anthropic). When Ollama fails mid-batch, remote fallbacks produce the same schema via grammar-constrained equivalents (tool_use for Anthropic). `scoring_provider` column captures which provider scored each row.
- **D-11** — `liveness_check` placement: stays pre-score inside `run_scoring()` (same position as the old `run_sonnet_evaluation` used it). Moving it to ingestion is out of scope — preserve current behavior.

### Schema migration

- **D-12** — Migration 40 (Plan 1, additive): adds `classification TEXT DEFAULT NULL`, `sub_scores_json TEXT DEFAULT NULL`, `CREATE INDEX idx_jobs_classification`. Reuses `fit_analysis` column (no rename).
- **D-13** — Migration 41 (Plan 5, destructive): `DROP COLUMN haiku_score`, `haiku_summary`, `sonnet_score`; `DROP INDEX idx_jobs_haiku_score`. Retains `fit_analysis`, `scoring_provider`, `eval_blocks`, `opus_score`, `score` (legacy tiebreaker heuristic), `job_archetype`, `legitimacy_note`.
- **D-14** — Migration 41 backup gate: file-mtime check on most recent `backup_userdata_*.tar.gz` — if modification time > 24h old, abort with error. Environment variable `GSD_BACKUP_CONFIRMED=1` bypasses the mtime check (for user-controlled backup paths). Fail-closed default: if neither gate is satisfied, Migration 41 does not execute.

### Plan 2 (dual-write)

- **D-15** — `use_unified_scorer: bool` config flag ships with `default: False` in the Plan 2 commit. Flipped to `True` in a separate follow-up commit within Plan 2's commit sequence. Two-step rollout gives a smoke-test window where dual-write code is live but reads are still legacy-only — revert one commit if a bug surfaces.
- **D-16** — Legacy shim math (Plan 2): `haiku_score ← mean(sub_scores.values()) × 20` (produces range 20-100 vs empirical 9-72 — accepted drift; shim exists only for Plan 3's read-swap window, ordering is preserved, calibration layer deleted means distribution match is not load-bearing). `sonnet_score ← same`. `haiku_summary ← rationale.strengths[0] or rationale.gaps[0] or ""`. Dual-write commit must include BOTH the new-column write AND the shim atomically (splitting creates inconsistent-data window).

### Plan 3 (reads)

- **D-17** — Plan 3 ships as 5 commits in dependency order, each independently revertable (writes stay dual via Plan 2 shim throughout):
  - A: Query-layer (`db.py`, `exclusion_filter.py`, `careers_crawler.py`, `agentic_enricher.py`, `blueprints/companies.py`, `dedup_normalizer.py`, `rejection_analyzer.py`, `rejection_patterns.py`)
  - B: `batch_scoring.py` haiku+sonnet routes merged into single scoring route
  - C: `dashboard.py` quick-actions + stats merge
  - D: Template updates (`_row_detail.html`, `_row_expanded.html`, `_score_cell.html`, `_resume_section.html`, `costs/index.html`)
  - E: Resume/interview gating flip (`classification == 'apply'`) + `pipeline_runner.run_ingestion` summary keys collapse

### Plan 4 (legacy write removal + rescore)

- **D-18** — Rescore scope: part of Plan 4 as its final task before legacy-write removal. Dual-write active during rescore = no data at risk if rescore crashes mid-flight. Completion ordering within Plan 4: setup → rescore (batched) → validation → legacy-write removal + deletions.
- **D-19** — Rescore batching: **3 stratified batches** with per-batch validation.
  - B1: **150 rows** (~22 min wall-clock) — fast-fail code-path correctness check
  - B2: **1000 rows** (~2.5h) — sample-bias protection on real volume
  - B3: **~2750 remaining rows** (~7h) — finish
  - Row selection stratified by legacy `sonnet_score` quartile (each batch has rows across the full distribution — makes G2 monotonicity test meaningful per batch)
  - Deterministic seeding: `ORDER BY (dedup_key, :batch_seed)` with distinct seeds per batch for reproducibility on re-run
- **D-20** — Validation gates (run after each batch; G4 runs once up-front before B1):
  - **G1 — Completeness (batch-local):** all rows in the batch have `classification IS NOT NULL`
  - **G2 — Distribution monotonicity:** group new classifications by legacy `sonnet_score` buckets (0-25, 26-50, 51-75, 76-100); each higher bucket has strictly more `apply+consider` counts than the lower. Loose enforcement on B1 (n=150, flag only extreme violations); strict on B2 and B3.
  - **G3 — Numeric-ordinal correlation:** Pearson r between `legacy_sonnet_score` and `mean(new_sub_scores)` across rescored rows. Threshold `r ≥ 0.3` on B1 (low-n allowance), `r ≥ 0.5` on B2 and B3. Suppress check if n < 20 (per Phase 33 convention).
  - **G4 — Production-path refit (once, before B1):** run Phase 33's 100-row frozen baseline (`.planning/research/shootout/baseline_sample.json`) through `job_scorer.score_job()` (the new production module), compare outputs against `.planning/research/shootout/baseline_gold.json`. MAE ≤ 1.0 (Phase 33's shootout_lib measured 0.799; allows headroom for any production-path variance). G4 failure blocks B1 from starting.
  - After B3 completes: **global G1** — `SELECT COUNT(*) FROM jobs WHERE classification IS NULL AND jd_full IS NOT NULL` must equal 0.

### Post-fail protocol (applies to G1-G4 on any batch)

- **D-21** — On gate failure: invoke the project's `/systematic-debugging` skill (or equivalent investigation logic). DO NOT halt-and-wait for user — follow the investigate-and-fix loop:
  1. Classify failure (G1/G2/G3/G4) and gather row-level evidence (outliers, NULLs, code-path diff)
  2. Form specific testable hypothesis
  3. Minimal-repro test (5-row subset, not full batch)
  4. Apply fix as atomic `fix(34-4): {root cause}` commit
  5. Re-run validation on the same batch rows
  6. If gate now passes: continue to next batch
  7. If still fails: iterate up to 3 cycles / 2h wall-clock total per gate
- **D-22** — Escalation criteria (when agent stops and surfaces to user with full history — NOT a cry for help, a structured handoff):
  - Metric oscillates across fix attempts (not converging)
  - Fix attempts regress other gates (gate definitions in tension)
  - 3 cycles spent, metric not improving → classify as structural, not a bug
  - Root cause is non-code (hardware crash, GPU OOM, data corruption)
  - Fix would require reverting a Phase 33 locked decision (prompt, model) — out of Phase 34 scope

### Plan 4 (legacy removal)

- **D-23** — Plan 4 commit sequence:
  - A: Rescore infrastructure (`scripts/v3_rescore.py`, `scripts/v3_rescore_validate.py`, G4 baseline refit test). G4 green before commit lands.
  - A.1, A.2, … (optional): `fix(34-4): …` commits if G4 reveals code-path divergence
  - B: B1 rescore complete + `rescore-batch-1-report.json` committed
  - B.1, B.2, … (optional): `fix(34-4): …` commits from B1 gate investigation
  - C: B2 rescore complete + report committed (with any B2-phase fix commits interleaved)
  - D: B3 rescore complete + final global G1 check + all reports
  - E: Legacy-write removal + module deletions (all of: `haiku_scorer.py`, `sonnet_evaluator.py` rename→`job_scorer.py`, `score_calibration.py`, `_apply_calibration`, `calibration_ollama_*.json`, `scripts/calibration_refit.py`, borderline re-eval path, `run_haiku_scoring`/`run_sonnet_evaluation`, `_run_batch_haiku_bg`/`_run_batch_sonnet_bg`, `persist_haiku_score`/`persist_sonnet_score`, `PROMPT_VARIANTS` block). Collapse `providers.haiku`/`providers.sonnet` → `providers.scoring` in `config.yaml` (Edit tool ONLY — never Write; file has been wiped 3 times by full-file writes) and `config.example.yaml`. Update `_TIER_DEFAULTS`, `resolve_provider_config()`. Relax `_make_adapter` api_key guard. Migrate `_build_comp_context` and `build_description_snippet` helpers BEFORE deleting `haiku_scorer.py`. Test suite green.
- **D-24** — `PROMPT_VARIANTS` fate: delete. Phase 33 selected a single model; per-model prompt variant infrastructure never materialized and dead code rots. Included in Plan 4 commit E.

### Plan 5 (column drop)

- **D-25** — Plan 5 scope: Migration 41 + TypedDict/fixture/COLUMNS updates only. Rescore and validation are complete before Plan 5 starts (they lived in Plan 4 per D-18). Plan 5 is a tight destructive commit gated on D-14's backup check.

### Testing strategy

- **D-26** — Test-file churn happens in the same commit as production code changes per plan (per ARCHITECTURE.md test strategy). No "temporarily broken" test states between plans. Suite must pass at every plan boundary.
- **D-27** — Test migrations per plan:
  - Plan 1: Add `test_job_scorer.py` (against `score_job()`), update `test_migration.py` for Migration 40
  - Plan 2: Add dual-write tests to `test_scoring_orchestrator.py` / `test_scoring_runner.py`. Add `mock_run_oneshot_legacy` fixture in `conftest.py` for multi-plan migration window
  - Plan 3: Update `test_careers_crawler.py`, `test_ats_scanner.py`, `test_rejection_patterns.py`, `test_rejection_analyzer.py`, `test_batch_scoring.py`, `test_ingestion.py`, `test_views.py`, `test_resume.py`, `test_dedup_normalizer.py`, `test_data_enricher.py`, `test_costs.py`, `test_claude_client.py`, `test_agentic_enricher.py` per per-file strategy in ARCHITECTURE.md
  - Plan 4: DELETE `test_scoring.py` (rewrite as `test_job_scorer.py` in Plan 1 already), `test_scoring_evaluator.py`, `test_scoring_orchestrator_calibration.py`. Rewrite `test_scoring_runner.py`, `test_backfill_enrichment.py`, `test_cascade_dispatch.py` (tier rename). Update `test_db.py` (persist_*_score → persist_job_assessment).
  - Plan 5: Add Migration 41 tests. Update fixtures that insert legacy columns directly.

### D-19 determinism criterion (carry-forward from Phase 33)

- **D-28** — Byte-identical determinism is not achievable on this hardware (CUDA non-deterministic reductions below Ollama). Phase 33 flagged D-19 for redefinition. Phase 34 lands a doc-only update in Plan 1's test harness notes (inside `tests/test_job_scorer.py` docstring or adjacent README) redefining the success criterion as **ordinal stability** — axis rankings preserved across repeated invocations. No new enforcement test in Phase 34 (rescore's per-batch gates capture the same intent via G3 correlation). A future milestone may add an explicit ordinal-stability probe.

### Rename blast radius

- **D-29** — Exhaustive grep checklist from ARCHITECTURE.md lines 457-498 must return 0 matches for ALL legacy symbols before Migration 41 runs. This is Plan 5's precondition — encoded as a preflight check in the migration runbook: `score_job_haiku`, `evaluate_job_sonnet`, `persist_haiku_score`, `persist_sonnet_score`, `_apply_calibration`, `HAIKU_SCHEMA`, `SONNET_SCHEMA`, `"haiku_score"`, `"sonnet_score"`, `tier="haiku"`, `tier="sonnet"`, `providers.haiku`, `providers.sonnet`, `DEFAULT_MODEL_HAIKU`, `DEFAULT_MODEL_SONNET`, `haiku_threshold`, `borderline_high`, `"haiku_scored"`, `"sonnet_evaluated"`, `"sonnet_queued"`, `from job_finder.web.haiku_scorer`, `from job_finder.web.sonnet_evaluator`, `from job_finder.web.score_calibration`, `PROMPT_VARIANTS`, `calibration_*.json`, `calibration_refit.py`.

### Claude's Discretion

- Exact field names in `scripts/v3_rescore.py` CLI args (`--batch-size`, `--seed`, etc.) — planner decides
- Exact format of `rescore-batch-N-report.json` — JSON schema is Claude's call as long as it includes G1-G4 metrics with thresholds and pass/fail status
- Stratified-sampling SQL implementation detail (window functions vs subquery vs Python-side) — Claude picks based on SQLite capability and simplicity
- Test parametrization approach for classification rule edge cases — pytest.mark.parametrize vs explicit test functions is planner's call
- Commit message bodies — follow project convention (feat/fix/refactor/docs) with a body paragraph; exact phrasing is Claude's

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Phase 34 authoritative specs

- `.planning/ROADMAP.md` §Phase 34 (lines 123-171) — Plan 1-5 scope, success criteria, requirement IDs, rollback strategy
- `.planning/REQUIREMENTS.md` — SCORER-01..10,13, MIGRATE-01..05, COLLAPSE-01..11, CONSUMERS-01..16, TESTS-01..20 (62 requirement IDs mapped to Phase 34)
- `.planning/research/ARCHITECTURE.md` — **Authoritative for file-level migration targets.** Every file:line target in the "Data Flow — complete junction inventory" section (lines 86-159) is load-bearing. Plans MUST NOT invent new targets not listed here.

### Phase 33 carry-forward (model selection + precondition evidence)

- `.planning/research/v3.0-shootout-results.md` — 7-candidate × 9-site winner matrix
- `.planning/phases/33-local-llm-site-fitness-survey/02-shootout-SUMMARY.md` — Completion Addendum documents qwen2.5:14b single-model-sweep decision; supersedes renderer's per-site split
- `.planning/phases/33-local-llm-site-fitness-survey/33-VERIFICATION.md` — recorded overrides on D-19 determinism, D-03 VRAM, D-21 retry-rate (carry-forward context for planner)
- `HANDOFF_phase33_shootout_recap.md` (repo root) — state recap with key commit SHAs
- `job_finder/web/scoring_prompts/v3_scoring_prompt.py` — frozen v3 scoring prompt, sha256 `255c690e...d0c9da`. Any modification invalidates Phase 33's winner matrix.
- `.planning/research/shootout/baseline_sample.json` — 100-row stratified baseline for G4 production-path refit
- `.planning/research/shootout/baseline_gold.json` — Opus 4.6 gold for G4 comparison

### Project context

- `.planning/STATE.md` — v3.0 locked design decisions section (ordinal 1-5, 6 dimensions, 4-way classification, shared dispatcher, no calibration, zero new deps)
- `.planning/PROJECT.md` — v3.0 milestone boundaries, non-negotiables
- `CLAUDE.md` — project conventions. **Load-bearing:** `config.yaml` MUST be modified with Edit tool only (wiped 3 times by full-file writes); HTMX patterns; migration idempotency; snake_case; test command (`uv run --active pytest -q --tb=short`)
- `.planning/research/PITFALLS.md` — 21 documented pitfalls; PITFALLS #1 (Ollama options) and #3 (self-contradicting classification) are already locked via D-03 and D-06

### Scoring infrastructure (unchanged — planner must respect)

- `job_finder/web/model_provider.py` — `call_model()` dispatcher (schema retry, cascade fallback, provider attribution). v3.0 scorer routes through this, does not duplicate.
- `job_finder/web/providers/ollama_provider.py` — Already shipped SCORER-11 (schema dict → format=) and SCORER-12 (inference options). Do not modify.

### Scripts and test files (planner must know these exist)

- `scripts/shootout_lib/` — Phase 33 shootout infra. Production path (`job_scorer.py`) must match this behavior per G4.
- `tests/test_shootout_lib.py` — 22 tests covering shootout scoring semantics; a regression risk if `job_scorer.py` re-implements anything differently
- `scripts/backup_userdata.sh` — backup script referenced by D-14's mtime check; planner may need to inspect its output file naming

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets

- **`call_model(tier=…, output_schema=…)` dispatcher** — shared across 9 AI call-sites. Scorer routes through it identically. Inherits ~250 lines of battle-tested retry/cascade/attribution. Do NOT duplicate.
- **`OllamaProvider.call()`** (already upgraded in SCORER-11) — passes schema dict to `format=` for grammar-constrained decoding. Ready for v3 use.
- **`_sanitize_output()`** in `model_provider.py:307-356` — coerces string→int, strips extra keys, retries with error hints. Already handles 5-10% schema-correction rate on Ollama.
- **`scorer_fn` injection pattern** in `scoring_orchestrator.py:109-119` — test-override path. Carries cleanly from old `score_job_haiku` to new `score_job`.
- **`fit_analysis` JSON column** — preserve as-is; hold v3 rationale payload. Zero-change downstream (`resume_generator.py`, `interview_prep.py`, `resume_multi_version.py`).
- **`legitimacy_note` column** — already populated by ingestion-time detection. Classification rule reads it; no new write path needed.
- **`scoring_provider` column** — already exists; `persist_job_assessment()` writes it identically to today's `persist_sonnet_score`. Cascade attribution carries through.

### Established Patterns

- **SQLite migrations** stored as list of discrete SQL strings in `db_migrate.py`, `CREATE TABLE IF NOT EXISTS` for idempotency. Additive migrations (40) and destructive migrations (41) follow existing precedent (Migrations 13, 21, 22, 25, 39 were destructive).
- **Per-request `g.db` pattern** via `db_helpers.py` — continues to work. No Flask app factory changes.
- **Batch scoring background jobs** in `blueprints/batch_scoring.py` — haiku + sonnet routes merge into one (Plan 3 Commit B). APScheduler integration unchanged.
- **Dataclass immutability:** project convention is `@dataclass(frozen=True)`. `JobAssessment` MUST be frozen.
- **Python type hints on function signatures** (per global CLAUDE.md). f-strings, PEP 8.

### Integration Points

- `pipeline_runner.run_ingestion()` — Plan 3 Commit E collapses summary keys (`haiku_scored`, `sonnet_queued`, `sonnet_evaluated` → `scored` + `classified_{apply,consider,skip,reject}`)
- `scheduler.py` — APScheduler jobs call `run_scoring()` via `scoring_runner`. `run_haiku_scoring` + `run_sonnet_evaluation` collapse to single `run_scoring` call.
- `backfill_enrichment.py` — `run_sonnet_backfill` renames to `run_scoring_backfill`; predicate `sonnet_score IS NULL` → `classification IS NULL`. `run_borderline_rescore` DELETED (Plan 4).
- `dashboard.py:180-194` — single `scoring_eligible_count` replaces separate Haiku/Sonnet eligible counts.
- `exclusion_filter.py:90` — `count_haiku_scorable()` renames to `count_scorable()`.
- `dedup_normalizer.py:287-394` — merge logic over `haiku_score`/`sonnet_score`/`fit_analysis` replaced with classification-merge (highest apply>consider>skip>reject) and sub_scores (element-wise max or newer-wins — planner decides; recommend element-wise max to preserve strongest signal per axis).
- Templates `_row_detail.html`, `_row_expanded.html`, `_score_cell.html`, `_resume_section.html`, `costs/index.html` — swap numeric score display to classification-enum badges + sub-score breakdown. UI hint in ROADMAP: yes.

### Architectural Boundaries (do not cross without re-discussion)

- **No new Python deps** — `jsonschema 4.26.0`, `pydantic 2.12.5` already installed. `pydantic.BaseModel.model_json_schema()` generates schema; `@dataclass(frozen=True) JobAssessment` is the in-code type.
- **No ORM** — raw SQL only per CLAUDE.md.
- **Single-user, local-only** — no deployment, no Docker, no CI/CD; SQLite WAL; APScheduler 3.11 (not 4.x — breaking async API).
- **`config.yaml` is user data** — `.gitignore`d; Edit tool only; `config.example.yaml` tracked for schema reference.

</code_context>

<specifics>
## Specific Ideas

- **Rescore command:** `scripts/v3_rescore.py --batch-size 150 --seed 20260421001 --report-path .planning/phases/34-greenfield-scorer-rewrite/rescore-batch-1-report.json` (CLI surface is planner's call, but these semantics are required)
- **Validation command:** `scripts/v3_rescore_validate.py --batch-report .planning/phases/.../rescore-batch-1-report.json` — exits non-zero on any gate failure; stdout names the failed gate and dumps outliers/evidence
- **G4 once-up-front** runs as part of Plan 4 Commit A's test suite: `tests/test_v3_production_path_refit.py` or similar — invoked on every Plan 4 CI run until batches begin
- **Plan 5 backup check implementation hint:**
  ```python
  def _check_backup_recent() -> None:
      if os.environ.get("GSD_BACKUP_CONFIRMED") == "1":
          return
      backups = sorted(glob.glob("backup_userdata_*.tar.gz"), reverse=True)
      if not backups: raise MigrationBlockedError("No backup found. Run bash backup_userdata.sh first.")
      age_h = (time.time() - os.path.getmtime(backups[0])) / 3600
      if age_h > 24:
          raise MigrationBlockedError(f"Most recent backup is {age_h:.1f}h old (>24h). Run bash backup_userdata.sh or set GSD_BACKUP_CONFIRMED=1.")
  ```
  Invoked at the top of Migration 41's `up()` function before any DROP COLUMN executes.

</specifics>

<deferred>
## Deferred Ideas

- **Ordinal-stability determinism probe (explicit test):** Phase 34 lands a doc-only redefinition (D-28). An actual automated ordinal-stability probe for the production scorer can land in a future milestone (v3.1 tech-debt sweep or similar).
- **`fit_analysis` column rename:** Semantically stretched now (holds full rationale, not just fit). Renaming cascades into 3 out-of-scope modules. Deferred to a future cleanup milestone.
- **`opus_score` rebaseline under new schema:** Column stays in v3.0 (baseline comparison column, separate from live scoring). Rebaseline under ordinal schema is a future concern.
- **`eval_blocks` cleanup:** Already vestigial; leave for separate sweep.
- **`score` (legacy heuristic column) cleanup:** Stays as tiebreaker. Separate future cleanup.
- **D-23 tiebreaker bias-weighting patch (Phase 33 flagged):** Matrix renderer's tiebreaker doesn't weight bias. Cosmetic now — operational decision already overrode the tiebreaker with qwen2.5:14b sweep. Deferred.
- **Cosmetic Opus-spend display bug (Phase 33 flagged):** Accounting correct, display-only. Deferred.
- **Per-site model routing exploration:** Phase 33's renderer recommended `mistral-small:24b` for scoring sites and `qwen2.5:14b` for others. Single-sweep decision supersedes for Phase 34. If future throughput or quality data says per-site routing wins, it's a v3.1+ concern.

</deferred>

---

*Phase: 34-greenfield-scorer-rewrite*
*Context gathered: 2026-04-21*
