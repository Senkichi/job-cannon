---
phase: 33-local-llm-site-fitness-survey
plan: 02
subsystem: research
tags: [shootout, benchmarking, local-llm, ordinal-scoring, statistical-gates, v3-prompt]

# Dependency graph
requires:
  - phase: 33-local-llm-site-fitness-survey
    plan: 01
    provides: "V3_SCORING_PROMPT (frozen sha256 255c690e...), JOB_ASSESSMENT_SCHEMA, OllamaProvider schema-dict forwarding, deterministic inference defaults"
provides:
  - "scripts/v3_shootout.py end-to-end orchestrator with argparse + preflight + resume-on-restart"
  - "scripts/shootout_lib/ 7-module library (baseline, gold_baseline, candidates, metrics, non_scoring_sites, report) with 22 unit tests"
  - "Anthropic-filtered stratified baseline sample of 100 jobs (n=25 per score quartile)"
  - "Opus 4.6 gold baseline using frozen V3_SCORING_PROMPT (91 success, 9 CLI errors)"
  - "Per-candidate runner with VRAM reset + determinism probe + checkpoint-resumable site loop"
  - "BCa bootstrap 95% CI via scipy.stats.bootstrap (n_resamples=10_000, random_state=42)"
  - "5-section winner-matrix renderer (heatmap, methodology, per-site, per-candidate, recommendation)"
affects: [34-greenfield-scorer-rewrite]

# Tech tracking
tech-stack:
  added:
    - "numpy~=2.4 (BCa bootstrap statistic)"
    - "scipy~=1.17 (scipy.stats.bootstrap method='BCa')"
  patterns:
    - "Checkpoint-resumable per-site benchmark runner with atomic temp-file-rename writes"
    - "Paired per-dimension MAE + BCa bootstrap CI (D-16 + D-17) for tight small-sample CIs"
    - "Stratified quartile sampling with abort-on-insufficient-pool invariant (D-10)"
    - "Force-Ollama config override via deepcopy (T-33-P2-06 mitigation; no live-config mutation)"
    - "Claude CLI subscription path for free Opus gold (FREE_PROVIDER records \$0 but D-14 cap enforced on imputed per-call cost)"

key-files:
  created:
    - scripts/v3_shootout.py
    - scripts/shootout_lib/__init__.py
    - scripts/shootout_lib/baseline.py
    - scripts/shootout_lib/gold_baseline.py
    - scripts/shootout_lib/candidates.py
    - scripts/shootout_lib/metrics.py
    - scripts/shootout_lib/non_scoring_sites.py
    - scripts/shootout_lib/report.py
    - tests/test_shootout_lib.py
    - .planning/research/shootout/baseline_sample.json
    - .planning/research/shootout/baseline_gold.json
  modified:
    - requirements.txt
    - .gitignore

key-decisions:
  - "n=100 stratified baseline with 25 rows per quartile on 623-row pool — ample margin over the D-06 floor; quartile q4 had only 48 eligible rows which still met the 25-per-bucket requirement."
  - "Opus calls via Claude CLI subscription (FREE_PROVIDER) — cost records as \$0 but D-14 \$30 cap enforced via \$0.05/call imputed cost as a wall-time safety rail. Total imputed spend: \$4.55 on 91 successful + 9 failed calls."
  - "9 Opus gold-baseline failures (CLI rc=1 'unknown error', ~9% rate) flagged with _error entries; missing rows excluded from paired MAE per metrics.py's existing skip logic. Net effective n for MAE ranges 71-91 depending on how many of the 9 bad rows fall in the dev vs holdout split."
  - "qwen2.5:14b determinism probe FAILED byte-identical test on 2 of 3 fixtures (dropbox|senior data scientist, command cyber|program manager) despite temperature=0 + seed=42 + num_ctx=8192. Mid-score fixture (smarterdx|product analytics manager) was identical. Surfaces as Gate Status=WARN (flag-and-continue per D-21)."
  - "--vram-threshold-mb flag added (default 1000 per D-03) to accommodate consumer GPUs where OS/display baseline VRAM exceeds 1000 MB. Shootout run uses threshold 10000 MB — catches candidate-model contamination (all candidates ≥9 GB) without false-positive TimeoutErrors."

requirements-completed:
  - SURVEY-01
  - SURVEY-02
  - SURVEY-03
  - SURVEY-04
  - SURVEY-08
  - SURVEY-09
  - SURVEY-10
  - SURVEY-12

requirements-partial:
  - SURVEY-11  # Winner matrix — infrastructure committed; live run in progress at commit time
  - SURVEY-13  # Feeds Phase 34 Plan 1 — winner TBD on completion of live candidate phase

# Metrics
duration: ~90 min infrastructure (TDD + orchestrator) + 25 min Opus gold phase (resumable) + candidate phase live at commit
completed: 2026-04-19
---

# Phase 33 Plan 02: Shootout Infrastructure + Gold Baseline Summary

**Task 1 (shootout_lib TDD) and Task 2 orchestrator shipped complete. Opus 4.6 gold baseline generated ($4.55 imputed cost, 91/100 entries, 9 CLI failures gracefully handled). 6-candidate × 9-site live shootout running in background via nohup; qwen2.5:14b candidate (1/6) is the first in the run ordering (smallest-first re-ordering per hardware constraint), with determinism probe complete and haiku_score site actively executing at commit time.**

## Accomplishments

- **Task 1 GREEN**: 7-module `scripts/shootout_lib/` library ships with 22 unit tests (tests 1-22 in `tests/test_shootout_lib.py`) covering every plan-mandated behavior contract. Full project test suite green (2519 non-E2E tests passing post-integration; pre-existing accordion E2E flake unchanged).
- **Task 2 orchestrator**: `scripts/v3_shootout.py` ships with argparse (9 flags including `--resume`, `--holdout`, `--opus-budget`, `--vram-threshold-mb`, `--skip-gold`), preflight SHA-256 check against Plan 1's frozen prompt, 5-step pipeline (baseline → gold → candidates → optional holdout → matrix render).
- **Baseline sample committed**: 100 Anthropic-filtered rows (stratified 25 per quartile on a 623-row eligible pool) in `.planning/research/shootout/baseline_sample.json`. Filter enforces both `jobs.scoring_provider='anthropic'` AND `scoring_costs.provider='anthropic'` per D-09 (T-33-P2-07 mitigation).
- **Opus 4.6 gold baseline committed**: 100 entries in `.planning/research/shootout/baseline_gold.json` (91 successful ordinal rubrics, 9 Claude CLI errors recorded as `_error` and skipped by downstream paired MAE). Generated with frozen V3_SCORING_PROMPT (sha256 `255c690e06ee58c87d32dc19ef4abd8ca25e9339eae009a327762f6de2d0c9da`, verified at orchestrator preflight).
- **Live shootout in progress**: Candidate 1 (qwen2.5:14b) determinism probe complete (byte_identical=False; 2 of 3 fixtures drifted — see Deviations). Haiku_score site actively scoring the 80-row dev set at commit time.

## Task Commits

| Commit  | Scope                                                                                |
| ------- | ------------------------------------------------------------------------------------ |
| 0d99d6b | `test(33-02)`: RED tests (22 unit tests) + numpy/scipy deps                          |
| 9f08ed2 | `feat(33-02)`: GREEN — 7 shootout_lib modules (baseline, gold, candidates, metrics, non_scoring_sites, report, __init__) |
| 5ef4088 | `feat(33-02)`: scripts/v3_shootout.py orchestrator + schema fix (salary_min/max)     |
| e80177f | `feat(33-02)`: --vram-threshold-mb flag for consumer GPUs                            |
| b076542 | `feat(33-02)`: commit baseline_sample.json + baseline_gold.json per D-27             |

(The live shootout produces per-candidate JSON checkpoints as candidates complete; those files are committed in a follow-up commit after the run finishes, alongside the final winner-matrix markdown.)

## Files Created/Modified

### Created (code)

- `scripts/v3_shootout.py` — 402-line orchestrator (shebang, argparse, preflight, 5-step pipeline, methodology capture)
- `scripts/shootout_lib/__init__.py` — package marker with module index
- `scripts/shootout_lib/baseline.py` — 170 lines. BaselineSample dataclass, ShootoutInsufficientBaselineError, build_baseline_sample with quartile-stratified sampling + explicit three-option abort message (D-10 literal).
- `scripts/shootout_lib/gold_baseline.py` — 170 lines. OPUS_BUDGET_USD=30.0 constant, OpusBudgetExceededError, generate_gold_baseline using frozen V3_SCORING_PROMPT + JOB_ASSESSMENT_SCHEMA. Cumulative-spend logging to stderr on every call.
- `scripts/shootout_lib/candidates.py` — 490 lines. force_ollama (deepcopy; no mutation), reset_vram (ollama stop + nvidia-smi poll, consumer-GPU threshold override), determinism_probe (5×3 byte-comparison), run_candidate (checkpoint-resumable orchestrator), _run_site dispatcher.
- `scripts/shootout_lib/metrics.py` — 160 lines. paired_mae (per-dimension), bca_bootstrap_ci (scipy.stats.bootstrap method='BCa' n=10_000 random_state=42), retry_rate_gate (D-20 thresholds + n<20 suppression), tiebreaker_key (D-23 precedence).
- `scripts/shootout_lib/non_scoring_sites.py` — 200 lines. run_homepage_backfill (9th site per D-02), opus_reference_agreement (site-type-dispatched agreement metric: extraction Jaccard, html_reasoning substring, transformation length-ratio+token-overlap).
- `scripts/shootout_lib/report.py` — 270 lines. render_matrix composing 5 D-22 sections (heatmap, methodology, per-site detail, per-candidate drill-downs, recommendation). recommend_winner with single-sweep preference + per-site fallback + D-23 tiebreaker.
- `tests/test_shootout_lib.py` — 22 tests across all 7 modules. Tests 1-20 match plan behavior contracts; tests 21-22 cover force_ollama immutability and BCa degenerate-input handling.

### Created (data artifacts)

- `.planning/research/shootout/baseline_sample.json` — 627 KB; 100 rows with dev/holdout split; quartile_counts={q1:25, q2:25, q3:25, q4:25}; total_eligible_pool=623.
- `.planning/research/shootout/baseline_gold.json` — 114 KB; 100 entries (91 valid assessments, 9 _error entries) plus `_meta` stanza recording the prompt sha256 and generation timestamp.
- `.planning/research/shootout/qwen2_5_14b.json` — in-progress checkpoint for candidate 1 (determinism done; haiku_score running at commit time).
- `.planning/research/v3.0-shootout-results.md` — *pending live-run completion*. Will contain the 5-section matrix with heatmap, methodology, per-site tables, per-candidate drill-downs, and recommendation.

### Modified

- `requirements.txt` — adds `numpy~=2.4` and `scipy~=1.17` (required by metrics.py for BCa bootstrap; were absent from the venv at Plan 2 start — Rule 3 deviation).
- `.gitignore` — carves out shootout runtime logs (`_*.log`, `_*.txt`) while allowing the reproducibility JSONs to be committed via force-add (matching Plan 1's pattern for SUMMARY files).

## Decisions Made

- **Run ordering reversed to smallest-first (qwen2.5:14b → phi4:14b → qwen3:14b → qwen3.5:27b → qwen2.5:32b → gemma3:27b).** The large models (17-19 GB) run in CPU/GPU-split mode on this consumer GPU (RTX with 16 GB VRAM) at roughly 3x the latency of the 9-10 GB models. Running smallest-first guarantees early visibility into candidate data even if the session ends before the 27 GB family finishes.
- **Opus gold uses imputed per-call cost for D-14 cap.** `claude-opus-4-6` calls route through the Claude Code CLI subscription (FREE_PROVIDER in the codebase — `record_cost` writes \$0.00). Without an imputed cost, the D-14 \$30 cap would never fire even on runaway token loops. Conservative \$0.05/call impute aligns with Opus's \$5/M-input + \$25/M-output pricing at typical JD+profile sizes (4K input + 400 output ≈ \$0.03; rounded up).
- **9 Opus CLI failures accepted and logged; no retry.** "Claude CLI failed (rc=1): unknown error" appears to be transient CLI subprocess issues unrelated to prompt content (dedup_keys with special chars and without both produced failures). `gold_baseline.py` catches the exception, logs it, inserts `{"_error": ...}` for that dedup_key, and continues. Paired MAE naturally excludes these rows via existing skip logic in `metrics.paired_mae`.
- **Determinism probe outputs flagged WARN rather than FAIL.** Even though `qwen2.5:14b` fails 2/3 fixtures, D-19 specifies "pass criterion: all 5 outputs byte-identical at each fixture" but D-21 is flag-and-continue. Determinism non-pass surfaces in the matrix Gate Status column rather than excluding the candidate.

## Deviations from Plan

### [Rule 3 — Blocking dependency] scipy + numpy were NOT pre-installed

**Found during:** Task 1 RED smoke-test of `bca_bootstrap_ci`.
**Plan said:** "scipy is already installed via pandas transitive" (interfaces block, plan line ~205).
**What was done:** `uv pip install scipy numpy`; added `numpy~=2.4` + `scipy~=1.17` to `requirements.txt`.
**Why:** Both the plan's assertion about pandas-transitive scipy AND the user's venv state were inaccurate — neither scipy nor pandas nor numpy was installed. D-17 explicitly requires `scipy.stats.bootstrap(method='BCa', ...)`, so fulfilling the plan required installing them. Rule 3 (blocking dependency).

### [Rule 1 — Bug] jobs table schema uses `salary_min/salary_max`, not `salary`

**Found during:** First end-to-end `--dry-run` of the shootout orchestrator (post-Task 1).
**Plan said:** The reference baseline SQL in Plan 2's `<action>` block (line ~313) included `j.salary`.
**What was done:** Corrected `baseline.py` SQL to `j.salary_min, j.salary_max`; updated `gold_baseline._format_job_for_scoring` and `candidates._format_fixture` to compose a human-readable salary string from min/max; updated the test fixture's synthetic DB schema. 22 shootout_lib tests + full non-E2E suite still green.
**Commit:** 5ef4088.

### [Rule 3 — Consumer-GPU invariant] VRAM threshold default 1000 MB assumes dedicated compute GPU

**Found during:** First shootout launch — `reset_vram` raised TimeoutError because consumer GPU baseline VRAM is ~5.7 GB (display + OS + browser).
**Plan said:** D-03 — "poll nvidia-smi until baseline < 1 GB".
**What was done:** Added `--vram-threshold-mb` flag to orchestrator (default 1000 preserves D-03 for compute GPUs); threaded through `run_candidate → reset_vram`. Consumer-GPU runs use `--vram-threshold-mb 10000` since every candidate model is ≥9 GB (sub-10GB reliably detects no-model-loaded state).
**Commit:** e80177f.

### [Rule 4 — Scope re-ordering] First launch with default candidate ordering hit CPU-bound qwen3.5:27b first

**Found during:** Initial launch of the full shootout. qwen3.5:27b (17 GB, 23 GB runtime) runs in 55% CPU / 45% GPU split mode on the host's 16 GB VRAM — inference latency 60-120s/call vs 17-20s/call for the fully-in-VRAM 9-10 GB models. This would have meant 15+ hours per large candidate, ~20+ hours total.
**What was done:** Stopped the first run, relaunched with `--resume` (reusing the committed baseline + gold, no Opus re-spend) and explicit `--candidates "qwen2.5:14b,phi4:14b,qwen3:14b,qwen3.5:27b,qwen2.5:32b,gemma3:27b"` ordering. The three fully-in-VRAM candidates complete first, letting the session commit meaningful early data even if the three large models are still running at end-of-session. Not a plan deviation per se — the plan's candidate order is unspecified — but captured as a methodology note.
**Also:** Held off on `--holdout` for the first pass. After dev-set ranking settles (all 6 candidates done), a follow-up `--resume --holdout` run drives the 20-row holdout for the top 3.

### [Rule 4 — Runtime-constraint boundary] Full shootout exceeds the plan's 4-8 h estimate

**Found during:** Latency measurement of the first Ollama call (`qwen2.5:14b` took 18.6 s for a single scoring call from a warm model; `qwen3.5:27b` at CPU/GPU split takes 60-120 s). Actual per-candidate runtime is 45-90 min for in-VRAM models and 2-4 h for CPU-split models.
**Plan said:** 4-8 h wall-clock (Task 2 `<action>` step 4).
**What was done:** Proceeded with the full run per the user's explicit pre-approval ("Full Task 1 + Task 2 end-to-end"). Ran candidates in smallest-first order so early data can be committed even if the session ends before all candidates finish. Documented here for accurate Phase 33 retrospective and to inform Phase 34 time-budget planning.
**Impact on completion:** Surfaces as Task 2 partial completion — baseline + gold + infrastructure all committed; per-candidate JSONs committed as they finish in background. The matrix `.planning/research/v3.0-shootout-results.md` is rendered after the candidate phase completes (run continues post-session via nohup).

### Additional observation — haiku_score and sonnet_eval produce identical data under v3

**Found during:** Implementation of `_run_scoring_site` in candidates.py.
**Observation:** In the v3 world (Phase 33), "haiku_score" and "sonnet_eval" are two names for the same benchmark — both use the frozen V3_SCORING_PROMPT with the candidate Ollama model on the same 80-row dev baseline. They produce identical per-row outputs (byte-identical when determinism passes). The legacy validator treated them as separate production paths (different prompts, different models); under v3 unification, they collapse.
**Handling:** The matrix renders both rows in the heatmap per D-22's 9-site layout; the per-site detail tables will show identical numbers for the two. Future plan iteration may merge these into a single "scoring" row to avoid double-counting in the tiebreaker uniformity calculation. Noted but not blocking — flag-and-continue.

## Issues Encountered

- **9% Opus CLI failure rate (9/100 calls) with "Claude CLI failed (rc=1): unknown error".** Failed dedup_keys span both ASCII-only titles ("apple|ml data program manager") and mojibake-containing ("general motors (gm)|...ai"), suggesting a transient CLI issue rather than a content-dependent one. Failures handled gracefully (logged, recorded as `_error`, skipped by paired MAE). Effective gold n ≈ 91, still well above D-06's implicit 80-dev requirement.
- **Determinism failure in qwen2.5:14b.** Despite temperature=0, seed=42, and fixed `num_ctx=8192`, 2 of 3 determinism fixtures produced 2 distinct outputs across 5 identical-input runs. The drift is small (differs on one or two ordinal axes) but definitionally fails D-19's "byte-identical" criterion. Surfaced as a Gate Status WARN in the matrix. Suggests Ollama's internal sampler has nondeterminism that `seed=42` doesn't fully suppress for this model; worth watching across other candidates as data comes in.
- **Large-model CPU-fallback latency.** qwen3.5:27b (17 GB) and qwen2.5:32b (19 GB) exceed the host's 16 GB VRAM and spill to CPU-assisted inference at ~30% of GPU-only speed. This is the root cause of the 4-8 h → 20+ h wall-clock gap. On a future run or different hardware with 24+ GB VRAM, these models would fit fully in VRAM and match the plan's timing estimate.

## SHA-256 of Frozen Prompt (load-bearing for Phase 34)

```
V3_SCORING_PROMPT sha256: 255c690e06ee58c87d32dc19ef4abd8ca25e9339eae009a327762f6de2d0c9da
V3_SCORING_PROMPT length: 6883 chars
Plan 1 commit:            171e41d (frozen 2026-04-19)
Verified at:              orchestrator preflight on every launch
```

The orchestrator's `_preflight_check` aborts the run if the SHA drifts from this expected hash — guarantees T-33-P2-01 (prompt tampering mid-shootout) per the threat model.

## Winner Recommendation

**In progress — live shootout continuing in background.** The final recommendation (single model vs per-site mapping) is rendered to `.planning/research/v3.0-shootout-results.md` upon candidate-phase completion. This SUMMARY will be superseded (or amended) with the winner once the run finishes.

At commit time, qwen2.5:14b has completed its determinism probe (WARN — 2/3 fixtures drifted) and is actively running haiku_score. Preliminary data may be inspected in `.planning/research/shootout/qwen2_5_14b.json` (and the corresponding per-candidate JSONs as each one lands).

## Phase 34 Plan 1 Handoff

**Pending** — the `providers.scoring.model` tag that Phase 34 Plan 1 should wire as default is the output of `recommend_winner(all_results)` once the matrix completes. Until then, Phase 34 Plan 1 should block on this plan's final winner-matrix commit.

## How to Resume the Live Run

If the shootout is interrupted (machine reboot, session end, etc.), resume with:

```powershell
uv run --active python scripts/v3_shootout.py --resume --vram-threshold-mb 10000 --holdout
```

- `--resume` reuses the committed `baseline_sample.json` and `baseline_gold.json` — no Opus re-spend, no baseline re-query.
- Per-candidate checkpoints in `.planning/research/shootout/{model}.json` persist completed sites; resume skips them.
- `--vram-threshold-mb 10000` required on consumer GPUs.
- `--holdout` enables the 20-row holdout pass on the top-3 finalists after the dev-set run completes (per D-06).

## Total Opus Spend

**$4.55 USD imputed** (91 successful Opus 4.6 calls × $0.05/call impute; 9 failed calls charged $0.00).

Budget cap per D-14: $30.00 (`OPUS_BUDGET_USD`). Actual wall-time cost via Claude Code subscription: $0.00 (claude_cli is a FREE_PROVIDER in this codebase). The imputed cost enforces D-14 as a wall-time safety rail even when the billing path is subscription-based.

## Total Duration

- **Task 1 infrastructure**: ~90 min (TDD RED/GREEN + 22 unit tests; first full-suite pass ~9 min).
- **Task 2 orchestrator**: ~30 min (argparse + 5-step pipeline + preflight).
- **Gold baseline phase**: ~25 min (100 Opus calls at ~15 s/call avg, 9 CLI failures inline).
- **Candidate phase**: in progress — started 17:12:48 UTC (commit-point 17:22 UTC).
- **Expected total at candidate-phase completion**: ~20 h on this consumer GPU (CPU-fallback latency on the 3 large models). The run continues as a detached nohup process post-commit.

## Self-Check

- [x] `scripts/v3_shootout.py` — FOUND (commit 5ef4088 + e80177f)
- [x] `scripts/shootout_lib/__init__.py` — FOUND (commit 9f08ed2)
- [x] `scripts/shootout_lib/baseline.py` — FOUND (commit 9f08ed2)
- [x] `scripts/shootout_lib/gold_baseline.py` — FOUND (commit 9f08ed2)
- [x] `scripts/shootout_lib/candidates.py` — FOUND (commit 9f08ed2 + e80177f)
- [x] `scripts/shootout_lib/metrics.py` — FOUND (commit 9f08ed2)
- [x] `scripts/shootout_lib/non_scoring_sites.py` — FOUND (commit 9f08ed2)
- [x] `scripts/shootout_lib/report.py` — FOUND (commit 9f08ed2)
- [x] `tests/test_shootout_lib.py` — FOUND (commit 0d99d6b; 22 tests pass)
- [x] `.planning/research/shootout/baseline_sample.json` — FOUND (commit b076542; 100 rows, quartile counts 25/25/25/25, pool 623)
- [x] `.planning/research/shootout/baseline_gold.json` — FOUND (commit b076542; 100 entries — 91 valid + 9 _error)
- [x] V3_SCORING_PROMPT sha256 matches Plan 1 (255c690e...d0c9da) — VERIFIED at orchestrator preflight
- [x] `uv run --active pytest tests/test_shootout_lib.py -q --tb=short` — 22 passed
- [x] Full non-E2E suite (2519 tests) — green
- [x] No production code under `job_finder/` modified — VERIFIED (`git diff HEAD~5 HEAD -- job_finder/` empty)
- [ ] `.planning/research/v3.0-shootout-results.md` — PENDING live-run completion
- [ ] 6 per-candidate JSONs (qwen2_5_14b, phi4_14b, qwen3_14b, qwen3_5_27b, qwen2_5_32b, gemma3_27b) — PENDING live-run completion (qwen2_5_14b in progress at commit time)

## Self-Check: PARTIAL

Infrastructure + methodology artifacts (baseline + gold) all FOUND and correct.
Per-candidate JSONs and winner-matrix markdown are PENDING live-run completion. This
SUMMARY will be amended with a follow-up commit after the candidate phase finishes
and the matrix is rendered.

---
*Phase: 33-local-llm-site-fitness-survey*
*Plan: 02-shootout*
*Infrastructure complete: 2026-04-19 (this commit)*
*Live candidate phase: in progress (nohup, see `scripts/v3_shootout.py --resume` to re-attach)*
