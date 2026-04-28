# Scoring Pipeline Recalibration — Design

**Date:** 2026-04-27
**Status:** Approved by user, ready for implementation plan
**Scope:** AI scoring system end-to-end — enrichment cascade, prompt assembly, classification rule, and evaluation infrastructure. Builds on the v3.0 ordinal scorer (Phase 34).

---

## Problem

Now that scores are visible in the UI (`_score_cell.html`, shipped 2026-04-27), the user can read the table and feels scores are off. Concrete symptoms reported:
1. Wrong absolute classification (jobs labeled `apply` that don't deserve it)
2. Wrong relative ranking within bucket
3. Rationale text contradicts the numbers

Three concrete examples of false-positive `apply` classifications:
- **TMF Manager, Clinical QA, Vera Therapeutics** — pharma clinical-QA role; user is an analytics/data-science candidate
- **Machine Learning Engineer, Latent** — user is not an ML engineer
- **Research Engineer, Frontier Safety Mitigations, DeepMind** — frontier AI research, irrelevant to user

### Root-cause analysis (verified against DB and codebase)

Investigation surfaced **four independent root causes**:

**RC1 — The candidate profile never reaches the scorer.**
`job_finder/web/job_scorer.py::_build_user_message()` only sends title + company + location + salary + JD. `load_scoring_profile()` exists in `scoring_orchestrator.py` but `batch_scoring.py:320` calls it and **discards the return value**. The system prompt is the static rubric + few-shot examples; the few-shots feature an "ML engineer with 8–9 years PyTorch" persona, so the model scores against that implicit persona by default. This explains all three example false-positives — the Latent role gets `title_fit=5` because the JD title matches the few-shot persona, not because it matches the user.

**RC2 — Profile data is fragmented across two files, neither read.**
- `experience_profile.json`: positions (5), skills (30), education, resume_preferences. Contains the *résumé* side.
- `config.yaml [profile]`: `target_titles` (12 senior analytics/DS roles), `target_locations`, `min_salary` (150,000), `industries` (6), `exclusions`. Contains the *targeting* side.
The legacy `JobScorer` (`job_finder/scoring/scorer.py`) reads the config.yaml side; the v3 scorer reads neither.

**RC3 — "Default to 3 on missing info" + "all ≥ 3 = apply" makes uncertainty an apply.**
The rubric says: `3 — neutral or partial fit (missing info: infer neutrally)`. The classification rule says: `apply ← all sub-scores ≥ 3`. Combined, a model with no signal on every axis defaults to six 3s and gets classified `apply`. This is structural, not a model defect. Observable in production data:
- Apply rate across 5,010 scored jobs: **55.0%**
- Modal score on every axis: **3** (49–60% of all judgments)
- 60% of `comp_fit` scores are 3 (because salary rarely listed → "missing info → neutral → 3")

**RC4 — Enrichment cascade does LLM synthesis instead of fetching.**
Cascade is `free → ddg → haiku → serpapi → sonnet → exhausted`. `extract_with_haiku` and `extract_with_sonnet` are **synthesis** tiers — they ingest truncated fragments (Haiku: `search_text[:2000]`; Sonnet: 5000-char-capped fragments) and emit a re-synthesized "summary" of the JD. Haiku's prompt literally says `jd_full (string, job description summary)`. Result: 1,006 of 1,250 short-JD scored jobs got stuck at `enrichment_tier='haiku'` with synthesized stubs averaging 348–933 chars, blocking escalation to fetch tiers that could find a real JD.

---

## Goal

Recalibrate the scoring system so:
- Classifications are honest and defensible against the user's own judgment
- The system does not manufacture confidence on jobs where it lacks signal (genuine "low signal" is surfaced, not buried under fake `apply`)
- Future rubric changes can be evaluated on a labeled gold set with literature-informed metrics
- The enrichment pipeline fetches actual JDs and never synthesizes pseudo-JDs from fragments

The primary success metric is **apply false-positive rate** measured against a user-labeled gold set, with the **3 anchor cases** (Vera, Latent, DeepMind) functioning as a non-negotiable lock-in test.

---

## Solution Summary

Six-phase plan, executed in order:

| Phase | Deliverable | Compute / Time |
|---|---|---|
| 1. Literature survey | `.planning/research/SCORING-LITERATURE-SURVEY.md` covering LLM-as-judge biases, ordinal scoring metrics, rubric design, confidence/abstention, pointwise vs pairwise | ~1 day research, 2-3hr read |
| 2. Bug fixes | Profile injection + cascade rewrite + `low_signal` classification + backfill | ~3hr core code, ~9hr overnight backfill |
| 3. Gold set | 40 user-labeled jobs (per-axis 1-5 + classification + optional note), stored as columns on `jobs` | ~60–80 min labeling |
| 4. Rubric redesign | Variant files in `scoring_prompts/variants/`, A/B'd via the harness, winner committed | ~half a day per iteration cycle |
| 5. Eval harness | CLI tool computing per-axis MAE/bias/ICC/QW-κ, classification metrics, coherence violations, with bootstrap CIs and per-job diff tables | ~half a day to build |
| 6. Iterate + ship | Variant iteration loop, acceptance gates, wholesale re-score, optional weekly regression cron | open-ended |

The lit survey is **upfront** (informs phases 4 & 5); bug fixes are **before** the gold set (so labels are made against the intended scorer behavior, not the broken one).

---

## Phase 1 — Literature Survey

**Topics**, each with primary-source paper summaries + an "applicable to job-cannon?" verdict:

| # | Topic | Tied to which root cause / decision |
|---|---|---|
| 1 | LLM-as-judge core findings & biases (position, length, self, central-tendency, extremity) | RC3 — "central tendency" is exactly our default-to-3 |
| 2 | Ordinal scoring methodology (ICC, Krippendorff α, QW-κ; why Pearson r alone misleads) | Phase 5 metric selection; reinforces existing "eval bias blindspot" lesson |
| 3 | Rubric design and prompting (anchor density, CoT-before-score, reference-based vs reference-free, few-shot calibration) | Phase 4 dimensions C, D |
| 4 | Confidence, abstention, "no signal" handling (verbalized confidence, ECE, explicit abstain codes) | RC3, Phase 4 dimension B |
| 5 | Pointwise vs pairwise vs listwise scoring | Decision: stay pointwise; document why (parked alternative) |

**Method:** parallel research agents (one per topic, using WebSearch + Exa + WebFetch on primary sources), then synthesized into a single doc with a final "techniques to adopt" table.

**Out of scope:** Pre-2023 evaluation work (Likert fundamentals), non-LLM ranking literature, multi-judge ensembles, RAG-eval-specific work.

### Decisions

| # | Decision | Rationale |
|---|---|---|
| D-1.1 | 5 topic structure tied to known root causes | Every finding has clear "applicable here?" verdict; avoids abstract bibliography |
| D-1.2 | Parallel research agents, one per topic | Faster than serial; isolates context; matches established pattern from `/gsd-new-project` |
| D-1.3 | Survey runs **before** any other phase | Findings inform Phase 4 (rubric) and Phase 5 (metrics) |

---

## Phase 2 — Bug Fixes

Five sub-fixes, all of which are **bugs not design choices** — no measurement needed before applying.

### 2a. Profile injection (RC1 + RC2)

- New function in `scoring_orchestrator.py`: `build_candidate_context(config, profile) -> str` — merges `config.yaml [profile]` (target_titles, target_locations, min_salary, industries, exclusions) with `experience_profile.json` (positions summary, skills, education) into one prompt block.
- Modified `job_scorer.py::_build_system_prompt()` to take a `candidate_context` arg and splice it in **between** `FIELD_REINFORCEMENT` and `FEWSHOT_EXAMPLES`.
- `score_job()` accepts `candidate_context` arg (default None for back-compat with tests); orchestrator passes the merged string.
- Rendered once per batch, cached on the orchestrator (not per-job).
- Format: structured plain text (not nested JSON in prompt) — easier for qwen2.5:14b; ~400-500 token budget.
- Position summaries: 1-line per position (title + company + dates + 1-line achievement summary), top-30 skills list as flat strings, education as 1-line per degree.

### 2b. Enrichment cascade rewrite (RC4)

- **Delete `extract_with_haiku` and `extract_with_sonnet` from the cascade.** New cascade: `free → ddg → serpapi → agentic → exhausted`. No LLM synthesis tiers.
- Each fetch tier writes the **raw text** as `jd_full` (with existing `_AUTH_WALL_SIGNATURES` and `is_short_auth_page` rejection unchanged).
- `agentic_enricher` (Ollama query-gen + Playwright fetch + Ollama validation) stays as deepest tier; already runs nightly.
- Code reduction: ~50 lines from `data_enricher.py` cascade ordering, ~250 lines from the two synthesis functions in `enrichment_tiers.py`.

### 2c. Post-fetch structured-field extraction

- New module-level function `parse_structured_fields(jd_full, job_row, conn, config) -> dict` (in `enrichment_tiers.py` or new module).
- Runs **once after** `jd_full` is populated, on the actual full text (not cascade fragments).
- Uses Haiku for cost. Schema: `{salary_min: int, salary_max: int, location: str}` — **no `jd_full` field** (so the model cannot summarize the description).
- Replaces the salary-extraction side-effect of the deleted Haiku/Sonnet cascade tiers.

### 2d. `low_signal` classification

- New 5th classification value, distinct from `apply / consider / skip / reject`.
- Rule (Python-derived in `db.derive_classification`): `if enrichment_tier == 'exhausted' AND length(jd_full) < threshold: return 'low_signal'`.
- Threshold: `scoring.low_signal_jd_chars` config knob, default **1500**.
- Rule precedence: `legitimacy_note → reject; low_signal check → low_signal; any axis 1 → reject; all ≥ 3 → apply; …`. The `low_signal` check sits between the legitimacy-note check and the any-axis-1 check (i.e., it short-circuits the rubric outputs entirely — for genuinely-no-signal jobs, the rubric output is unreliable).
- UI affordance (deferred to a separate trivial follow-up): show `low_signal` rows with a "needs full description" badge + manual re-enrichment button. Color in `_score_cell.html`: muted gray, distinct from `skip`.

### 2e. One-shot backfill of stuck rows

- Standalone script (`scripts/backfill_stuck_at_haiku.py` or similar).
- Selects rows with `enrichment_tier IN ('haiku', 'free', 'ddg')` AND `length(jd_full) < 1500`.
- Resets `enrichment_tier = NULL` for these rows so the next enrichment cycle re-flows them through the new cascade.
- Run once, manually, after 2b lands and the new cascade is verified on a small sample.

### Decisions

| # | Decision | Rationale |
|---|---|---|
| D-2.1 | Inject profile into **system prompt** (between FIELD_REINFORCEMENT and FEWSHOT_EXAMPLES) | Stable across calls → prompt caching works; few-shots calibrate against the real profile |
| D-2.2 | Merge config.yaml + experience_profile.json at injection time, not at storage time | Both files keep existing roles (resume gen reads JSON; targeting lives in YAML); no migration burden |
| D-2.3 | Plain-text profile format, not nested JSON | qwen2.5:14b reads structured prose better than nested JSON in prompts |
| D-2.4 | **Delete** Haiku and Sonnet enrichment tiers (not gate them, not move them, not soften them) | They're synthesis tiers; the cascade should fetch, not invent. Synthesis from fragments necessarily loses information. |
| D-2.5 | `low_signal` is a 5th classification, not a flag/badge on existing classifications | Honest about uncertainty; doesn't pollute apply/consider/skip distribution; 5th column on confusion matrix in eval |
| D-2.6 | `low_signal` rule uses `enrichment_tier='exhausted'` (not just JD length) | Distinguishes "genuinely no enrichment available" from "enrichment hasn't run yet" — a 1000-char JD on a row with `enrichment_tier=NULL` should still be re-enriched, not labeled low_signal |
| D-2.7 | Wholesale backfill via standalone script, not migration | Migrations should be idempotent and DDL; this is data-flow re-triggering. Script is one-shot and manually invoked. |
| D-2.8 | Few-shot example **rewrite** is deferred to Phase 4, not Phase 2 | The new few-shots are part of rubric design; testing them belongs with the variant A/B |

### Failure modes guarded

- **Profile-loader failure**: if `experience_profile.json` is missing, `EMPTY_PROFILE` returned (existing behavior); profile injection produces a minimal block. Scorer still runs but with degraded signal — flag in logs.
- **`config.yaml [profile]` missing fields**: each field has a default ("Not specified" string per field); injection block adapts.
- **Token-budget overflow**: profile injection capped at 600 tokens; truncate position summaries first.
- **Re-flowed jobs hitting same fetch failures**: expected; they'll legitimately become `exhausted` and pick up `low_signal`.

---

## Phase 3 — Gold Set

40 user-labeled jobs, stratified across classification buckets, score spectrum, and sources.

### Strata

| Stratum | Count | Why |
|---|---|---|
| Anchor cases (Vera, Latent, DeepMind) | 3 | Lock-in test |
| Current `apply` (high composite ≥24) | 6 | Validate confident applies |
| Current `apply` (mid composite 18–23) | 6 | Danger zone; most false-positive volume |
| Current `consider` | 6 | Threshold validation |
| Current `reject` | 4 | Sanity check |
| Cross-source spread | 8 | LinkedIn / Glassdoor / DataForSEO / careers-scraped |
| Post-Phase-2 `low_signal` cases | 7 | Sampled after Phase 2 produces honest exhausted rows |

First 33 sampled pre-Phase-2; final 7 sampled post-Phase-2.

### Labeling format (per job)

- `gold_classification`: `apply | consider | skip | reject | low_signal`
- `gold_sub_scores_json`: 6 axes, integer 1–5 each
- `gold_notes`: optional ≤1 sentence justification
- `gold_labeled_at`: timestamp

### Storage — DB columns on `jobs` (new migration)

```sql
ALTER TABLE jobs ADD COLUMN gold_classification TEXT
  CHECK (gold_classification IS NULL
         OR gold_classification IN ('apply','consider','skip','reject','low_signal'));
ALTER TABLE jobs ADD COLUMN gold_sub_scores_json TEXT;
ALTER TABLE jobs ADD COLUMN gold_notes TEXT;
ALTER TABLE jobs ADD COLUMN gold_labeled_at TIMESTAMP;
```

### Workflow — CLI script

`uv run python -m job_finder.scripts.label_gold_set`
- Walks unlabeled gold-set rows one at a time
- Prints title, company, location, JD excerpt (~600 chars), source(s), current model output for context
- Prompts for classification, 6 sub-scores, optional note
- Writes to gold columns; tracks progress (Job 7/40); resumable

### Decisions

| # | Decision | Rationale |
|---|---|---|
| D-3.1 | N=40 (not 30 or 60) | 30 floor for credible per-axis CIs; 50+ diminishing returns; matches Phase 33 precedent (61) |
| D-3.2 | Per-axis labels mandatory (not classification-only) | Lets harness diagnose *which* axis is wrong, not just final classification; required for Phase 4 rubric A/B |
| D-3.3 | DB columns on `jobs` (not separate table or JSON file) | Queryable alongside scored data; no sync hazard |
| D-3.4 | CLI script (not web UI) | One-shot focused task; user is backend-focused; terminal velocity > HTMX round-trips |
| D-3.5 | Stratified sampling (not random) | Tests what we care about: classification accuracy across buckets, source spread, anchor cases |
| D-3.6 | 7 `low_signal` rows sampled post-Phase-2 | The classification doesn't exist yet pre-Phase-2; sampled honestly after the cascade rewrite produces real exhausted rows |
| D-3.7 | Labels are invariant to JD content changes | Gold labels capture candidate-fit truth, not JD-text truth; Phase 2 enrichment changes don't invalidate them |

### Failure modes guarded

- **Schema-drift (Phase 4 changes axes)**: if Phase 4 *adds* a new axis, gold-set re-labeling needed for that axis only; if it *removes* an axis, just stop measuring it. Lock the 6-axis schema for the duration of Phase 4 iteration.
- **Sampling bias**: anchor cases are forced; remaining strata are sampled by SQL filters. Document the sampling SQL with the gold set.

---

## Phase 4 — Rubric Redesign

Structured design-space exploration. Variants live as files; harness picks them by name.

### Four design dimensions

**Dimension A — Classification rule** (RC3 structural fix)
- A1: Stricter axis threshold (`apply ← all ≥ 4`)
- A2: Mean + floor (`apply ← mean ≥ 3.5 AND min ≥ 3`)
- A3: Weighted aggregate with axis weights from config
- A4: Two-gate rule (`apply ← (title_fit ≥ 4 AND skills_match ≥ 4) AND all ≥ 3`)

**Dimension B — Semantics of "3"**
- B1: Add explicit "no signal" code (0 or N/A)
- B2: Reanchor "3" as neutral evidence in JD; map missing info to 2
- B3: Force per-axis `evidence: <quote>`; no quote → score caps at 2

**Dimension C — Anchor density**
- C1: Anchor all 5 points verbally
- C2: Drop to 3-point scale (1=mismatch, 2=neutral, 3=match)
- C3: Add comparative anchors at 2 and 4

**Dimension D — Rationale-vs-numbers coherence**
- D1: Chain-of-thought before scoring (rationale-first)
- D2: Per-axis evidence field (`{evidence: <text>, score: <int>}`)
- D3: Mistake-driven calibration (correct/incorrect coherence examples)

### Selection criterion

Variants tested on the gold set, scored on the metrics defined in Phase 5. Winner is the variant that:
1. Strictly improves apply false-positive rate (mandatory)
2. Does not regress per-axis MAE on any axis by >0.2 (mandatory)
3. Stays within ~2× current latency budget (~5–10s/job at qwen2.5:14b)

### Practical scope

- Pick 2 variants per dimension (lit-survey-recommended + alternative)
- Screen one-dimension-at-a-time vs baseline first; A/B top candidates afterward
- Total variants tested: 6–8

### Implementation surface

- New directory `job_finder/web/scoring_prompts/variants/` — one Python module per variant
- Each variant exports `V3_SCORING_PROMPT`, `JOB_ASSESSMENT_SCHEMA`, `FEWSHOT_EXAMPLES`, `FIELD_REINFORCEMENT` (matches current contract)
- Selection knob: `scoring.prompt_variant` config key (default = production winner)
- Few-shot examples rewritten to match the actual candidate (analytics/data science, not ML engineer) — happens here, not Phase 2

### Decisions

| # | Decision | Rationale |
|---|---|---|
| D-4.1 | Variants live as separate files, selected via config knob | Reproducibility; old variants stay available; trivial rollback |
| D-4.2 | Screen one-dimension-at-a-time, then A/B finalists | Cuts 108 combinations to 6–8; isolates which dimension matters |
| D-4.3 | Few-shot rewrite happens in Phase 4, not Phase 2 | Few-shots are rubric calibration; testing them belongs with variant A/B |
| D-4.4 | Defer pairwise / listwise / multi-judge approaches | Higher cost, marginal benefit at this volume; revisit if pointwise hits a ceiling |

---

## Phase 5 — Eval Harness

Single CLI tool, three usage modes (diagnose / A/B / regression).

### Metric vector

**Per-axis metrics** (each of 6 axes):
- MAE
- Bias (mean signed error)
- ICC(2,1)
- Quadratic-weighted κ
- Distribution histogram (diagnostic)

**Classification metrics** (apply / consider / skip / reject / low_signal):
- Per-class precision/recall/F1
- Confusion matrix
- Apply false-positive rate (headline metric)
- Macro-F1

**Coherence metric**:
- For each job, flag if `gaps` text mentions a problem on axis X AND axis X scored ≥ 4
- Report % violations

**Run-level metrics**:
- p50 / p95 latency
- Schema adherence rate
- Cascade-exhaustion failure rate
- Cost per job (free vs paid breakdown)

**Calibration**:
- Brier score for classification head
- ECE deferred (insufficient labels at N=40 for reliable bin-based calibration)

### Comparison framework

Each metric reported as: baseline value, candidate value, Δ, 95% bootstrap CI on Δ (1000 resamples). Deltas whose CI crosses zero are not significant.

### Variance handling

- 3 runs per variant per gold set (qwen2.5:14b is non-deterministic per Phase 33 finding)
- Mean + within-variant variance reported
- Variance folded into cross-variant Δ CIs

### Output — versioned markdown reports

Path: `.planning/eval_results/YYYY-MM-DD-<variant>-vs-<baseline>.md`
Sections:
1. Headline verdict (better / worse / inconclusive)
2. Aggregated metric tables
3. Per-axis metric tables
4. Confusion matrix
5. Per-job diff table (jobs whose classification flipped or sub-scores moved by ≥2)
6. Cost / latency comparison
7. Coherence violation list

The per-job diff table is the most important output for human-in-the-loop review.

### Implementation surface

- New module `job_finder/eval/scoring_harness.py`
- Helper module `job_finder/eval/metrics.py` (`mae`, `bias`, `icc`, `qw_kappa`, `bootstrap_ci`)
- New table `eval_runs` in jobs.db (run_id, timestamp, variant_name, gold_set_version, raw scores per job)
- CLI: `uv run python -m job_finder.eval.scoring_harness --variant <name> [--baseline <name|run-id>] [--runs 3]`
- Verify `scipy` / `numpy` availability before depending on them (likely present via sentence-transformers; otherwise pin)

### Decisions

| # | Decision | Rationale |
|---|---|---|
| D-5.1 | Bootstrap CIs (not parametric tests) | Distribution-free; works at small N; standard for paired comparisons |
| D-5.2 | 3 runs per variant default | Captures qwen2.5:14b non-determinism; bumpable to 5 for finalists |
| D-5.3 | Per-job diff table is the headline output | Aggregate metrics tell you "did it improve"; per-job diffs tell you *why* — essential for human-in-the-loop iteration |
| D-5.4 | Eval runs stored in DB (not flat files) | JOIN-able with `jobs` for ad-hoc analysis; preserves history beyond most-recent-baseline |
| D-5.5 | Coherence metric uses simple keyword-overlap (not embedding-based) | Cheap, debuggable; refine only if false-positive rate too high |
| D-5.6 | ECE deferred to a future N=100+ gold set | Bin-based calibration unreliable below ~50 samples per bin |

### Failure modes guarded

- **Variant returns invalid JSON repeatedly**: harness logs schema-adherence rate; if <95% the variant is auto-disqualified
- **Cascade exhaustion mid-run**: caught by run-level failure metric; partial results still reported
- **Gold-set drift**: gold_set_version field on `eval_runs` table flags comparisons across schema changes

---

## Phase 6 — Iterate, Acceptance Gates, Rollout

### Iteration cycle

1. Edit a variant in `scoring_prompts/variants/`
2. Run harness: `--variant <name> --baseline production --runs 3`
3. Read per-job diff table first, then aggregates
4. Decide: keep / revert / iterate
5. Commit variant + eval report together (permanent record)

Budget: ~10–15 min per cycle. ~2-3 hours total iteration after harness exists.

### Acceptance gates (mandatory — all must pass to ship)

| Gate | Threshold | Why |
|---|---|---|
| All 3 anchor cases moved correctly | Vera/Latent/DeepMind not in `apply` | Lock-in test |
| Apply false-positive rate strictly improves | < baseline | Headline goal |
| No per-axis MAE worsens by >0.2 | per-axis | Don't break one axis to fix another |
| Schema adherence ≥ 95% | unchanged | Production reliability |
| Coherence violation rate strictly improves | < baseline | Addresses RC1's downstream symptom #5 |

### Soft targets (informed by lit survey; finalize after baseline run)

- Per-axis MAE < 0.7
- ICC(2,1) on title_fit ≥ 0.75; other axes ≥ 0.65
- Apply false-positive rate < 0.25
- Coherence violations < 10%

### Re-scoring strategy — wholesale, overnight

After winner ships:
- One-shot script: nullify `classification`, `sub_scores_json`, `fit_analysis` for all rows; kick off batch scorer
- ~5,010 × ~6s/job ≈ 8–9 hours at qwen2.5:14b
- Free (local Ollama)
- Atomic from user's perspective

Rejected: lazy re-score (inconsistent table state during transition); stratified by recency (complexity for no gain at this scale).

### Rollback

- Variants are committed files; revert = config knob change + re-run wholesale re-score
- `eval_runs` preserves historical metrics for "what did variant X look like" queries
- No `scoring_history` table on the jobs side — regenerating is just compute

### Ongoing regression check (lightweight, deferred)

- APScheduler cron, weekly: re-run harness in regression mode against gold set
- Alert on any acceptance gate flipping to fail
- Implementation: `scoring.regression_check_cron: "0 9 * * 0"` config knob
- Add **after** first ship, not now

### Decisions

| # | Decision | Rationale |
|---|---|---|
| D-6.1 | Wholesale re-score (not lazy / not stratified) | Atomic transition; Ollama is free; overnight job acceptable |
| D-6.2 | Acceptance gates locked before iteration starts (not adjusted to make a variant pass) | Prevents goal-seeking during iteration |
| D-6.3 | Anchor cases are mandatory gates, not soft targets | If Vera/Latent/DeepMind don't move, the rebuild has failed by definition |
| D-6.4 | Regression cron deferred to post-ship | Keeps initial milestone scope tight |

---

## Open Questions (to resolve during implementation)

1. **Profile injection format**: confirm plain text vs structured-text-in-JSON-block after lit survey — qwen2.5:14b may parse one better than the other
2. **`low_signal` JD-length threshold**: default 1500 chars but worth empirical confirmation by sampling 10–20 borderline cases (1000–2000 chars) and checking whether they're real JDs
3. **Gold-set sampling SQL**: write the exact SQL queries when sampling; commit alongside the gold-set CLI script
4. **Acceptance-gate thresholds**: revisit after baseline harness run; current thresholds are gut-feel anchors
5. **Soft-target ICC values**: lit survey may inform realistic bars per axis

## Parked (out of scope this milestone)

- **Pairwise / listwise scoring** — different paradigm, much higher cost
- **Multi-judge ensembles** — overkill for a single-user tool
- **Dynamic prompt generation per job** (omit comp_fit if no salary listed) — adds complexity, defer
- **Verbalized confidence per axis** — Phase 4 D3 lightly touches; deeper exploration parks here
- **Active-learning gold-set expansion (N=100+)** — only if pointwise hits a ceiling
- **Replacing qwen2.5:14b** — Phase 33 already chose this; revisit only if no rubric variant gets us where we need to be
- **`low_signal` UI affordance** (badge + manual re-enrichment button) — trivial follow-up, not gating
- **Consolidating profile storage** (merging config.yaml [profile] and experience_profile.json) — separate refactor

---

## Acceptance Criteria for This Design

- [ ] Six phases approved in order: literature survey → bug fixes → gold set → rubric redesign → eval harness → iterate
- [ ] All four root causes (RC1–RC4) explicitly addressed by at least one phase
- [ ] Decision tables (D-1.1 through D-6.4) capture the substantive choices
- [ ] Open questions surfaced separately from approved decisions
- [ ] Parked items explicitly listed (not silently dropped)

Ready for `/writing-plans` to break into implementation tasks.
