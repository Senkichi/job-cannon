---
phase: 34-greenfield-scorer-rewrite
plan: 03
subsystem: scoring
tags: [read-migration, query-layer, batch-scoring, dashboard, templates, pipeline-summary, v3, ordinal, classification]
requires:
  - Phase 34 Plan 2 (score_and_persist_job + legacy shim, use_unified_scorer=true live)
provides:
  - Query layer (db.py, exclusion_filter, careers_crawler, ats_scanner,
    agentic_enricher, companies blueprint, dedup_normalizer,
    rejection_analyzer, rejection_patterns) reads classification +
    sub_scores_json instead of haiku_score / sonnet_score
  - Unified /dashboard/batch-score/start route + _run_batch_bg worker
    replacing the Haiku/Sonnet route split; legacy URLs retained as
    delegating wrappers for UI back-compat
  - Unified scoring_eligible_count + _cached_tier_available("scoring")
    in dashboard stats
  - Classification-enum badges (apply=green, consider=amber, skip=slate,
    reject=red) + 6-dimension sub-score horizontal bars in all four jobs/
    templates and the costs/index legend
  - Resume generation gate flipped from sonnet_score to classification
    (generate + quick_apply); multi-version dispatch triggered by
    classification=='apply' instead of sonnet_score >= threshold
  - pipeline_runner.run_ingestion summary keys collapsed to scored +
    classified_{apply,consider,skip,reject}; legacy Haiku/Sonnet
    else-branch deleted; use_unified_scorer default flipped False -> True
affects:
  - job_finder/db.py (get_filtered_jobs, allowed_sort_cols, score-filter shim)
  - job_finder/web/exclusion_filter.py (count_haiku_scorable -> count_scorable)
  - job_finder/web/careers_crawler.py (query + _score_new_jobs via score_and_persist_job)
  - job_finder/web/ats_scanner.py (query + scoring block)
  - job_finder/web/agentic_enricher.py (ORDER BY classification_rank + sub_score_sum)
  - job_finder/web/blueprints/companies.py (effective_score subquery x2)
  - job_finder/web/dedup_normalizer.py (merge logic + new _merge_v3_scoring helpers)
  - job_finder/web/rejection_analyzer.py (SELECT + payload shape)
  - job_finder/web/rejection_patterns.py (dataclass + bucket computation)
  - job_finder/web/blueprints/batch_scoring.py (route + worker merge)
  - job_finder/web/blueprints/dashboard.py (stats + tier availability)
  - job_finder/web/blueprints/resume.py (generate + quick_apply gates)
  - job_finder/web/resume_generator.py (multi-version dispatch)
  - job_finder/web/pipeline_runner.py (run_ingestion shape)
  - job_finder/web/templates/jobs/_row_detail.html
  - job_finder/web/templates/jobs/_row_expanded.html
  - job_finder/web/templates/jobs/_score_cell.html
  - job_finder/web/templates/jobs/_resume_section.html
  - job_finder/web/templates/costs/index.html
  - 10 test files (listed in key-files.modified)
tech-stack:
  added: []
  patterns:
    - "SQL CASE expression for classification_rank (apply=4, consider=3, skip=2, reject=1)"
    - "json_extract composite ORDER BY with COALESCE guards so NULL sub_scores_json never breaks sort"
    - "delegating back-compat route wrappers (haiku/sonnet -> scoring) preserve HTMX hx-target IDs during the Commit B -> Commit D transition window"
    - "element-wise max sub_scores merge + priority-max classification merge in dedup_normalizer"
    - "legacy URL sort alias dict (haiku_score/sonnet_score -> classification_rank composite) for bookmark back-compat"
key-files:
  created:
    - .planning/phases/34-greenfield-scorer-rewrite/34-03-SUMMARY.md
  modified:
    - job_finder/db.py
    - job_finder/web/exclusion_filter.py
    - job_finder/web/careers_crawler.py
    - job_finder/web/ats_scanner.py
    - job_finder/web/agentic_enricher.py
    - job_finder/web/blueprints/companies.py
    - job_finder/web/dedup_normalizer.py
    - job_finder/web/rejection_analyzer.py
    - job_finder/web/rejection_patterns.py
    - job_finder/web/blueprints/batch_scoring.py
    - job_finder/web/blueprints/dashboard.py
    - job_finder/web/blueprints/resume.py
    - job_finder/web/resume_generator.py
    - job_finder/web/pipeline_runner.py
    - job_finder/web/templates/jobs/_row_detail.html
    - job_finder/web/templates/jobs/_row_expanded.html
    - job_finder/web/templates/jobs/_score_cell.html
    - job_finder/web/templates/jobs/_resume_section.html
    - job_finder/web/templates/costs/index.html
    - tests/test_agentic_enricher.py
    - tests/test_ats_scanner.py
    - tests/test_batch_scoring.py
    - tests/test_careers_crawler.py
    - tests/test_dedup_normalizer.py
    - tests/test_ingestion.py
    - tests/test_log_levels.py
    - tests/test_rejection_analyzer.py
    - tests/test_rejection_patterns.py
    - tests/test_resume.py
    - tests/test_resume_validator.py
    - tests/test_scoring.py
    - tests/test_views.py
key-decisions:
  - "D-17 (ratified in commits): Plan 3 ships as 5 atomic, independently-revertable commits (A/B/C/D/E) in query -> routes -> dashboard -> templates -> pipeline order. Writes stay dual via the Plan 2 shim throughout — every commit can be reverted individually without leaving stale data."
  - "Classification-rank ORDER BY uses an inline SQL CASE (apply=4, consider=3, skip=2, reject=1) with a sub_score_sum tiebreak derived from json_extract on the 6 sub-score keys. The composite expression is shared across db.get_filtered_jobs, blueprints.companies (x2), and agentic_enricher.py line 500."
  - "Sort-by URL back-compat: allowed_sort_cols in db.get_filtered_jobs keeps haiku_score/sonnet_score aliases that translate to the classification + sub_score_sum composite, so pre-v3 bookmarks don't 500. _LEGACY_SORT_ALIAS dict isolates the names so Plan 4 can drop them atomically."
  - "min_score/max_score filter back-compat: legacy numeric thresholds map to classification IN (...) via _classifications_for_min_score / _classifications_for_max_score, OR-joined with a NULL-classification heuristic-score fallback so pre-v3 rows (no classification populated) still match numeric filters."
  - "Batch-scoring route wrappers (haiku/start + sonnet/start) kept as delegating pass-throughs to avoid a half-wired UI between Commits B and D. The unified session_type value is 'scoring'; templates' hx-target selectors (batch-score-<label>-status) still resolve via the label= passed from each wrapper. Plan 4 removes both wrappers."
  - "Dashboard _get_quick_actions_context returns both the v3 keys (active_scoring, scoring_eligible_count, scoring_available) and back-compat aliases (active_haiku, active_sonnet, haiku_scorable_count, sonnet_eligible_count, haiku_available, sonnet_available) so the Commit-D-pending Jinja templates that still read the old keys never NameError. All aliases flagged PLAN-4-REMOVE."
  - "Resume multi-version dispatch switched from sonnet_score >= multi_threshold to classification == 'apply'. 'consider'/'skip'/'reject' all take the single-pass path. multi_version_threshold config is retained-but-unused for a one-line revert path."
  - "pipeline_runner.run_ingestion's use_unified_scorer default flipped False -> True. A False value now skips AI scoring entirely (no legacy fallback) — Wave 2's Commit B had already flipped config.yaml's flag, so default=True matches production behavior."
requirements-completed:
  - CONSUMERS-01
  - CONSUMERS-02
  - CONSUMERS-03
  - CONSUMERS-04
  - CONSUMERS-05
  - CONSUMERS-06
  - CONSUMERS-07
  - CONSUMERS-08
  - CONSUMERS-09
  - CONSUMERS-10
  - CONSUMERS-11
  - CONSUMERS-12
  - CONSUMERS-13
  - CONSUMERS-14
  - CONSUMERS-15
  - CONSUMERS-16
  - TESTS-08
  - TESTS-09
  - TESTS-10
  - TESTS-11
  - TESTS-12
  - TESTS-13
  - TESTS-14
  - TESTS-19
  - TESTS-20
duration: 4h 45min
completed: 2026-04-22
---

# Phase 34 Plan 3: Read Migration Summary

Ship the read-side of the v3.0 scoring rewrite in five independently-
revertable commits. Every downstream consumer of `haiku_score` /
`sonnet_score` / `haiku_summary` migrates to the v3 columns
`classification` + `sub_scores_json` + `fit_analysis` (rationale
payload) that Plan 2 already populates via its atomic dual-write shim.

Legacy write paths stay live throughout Plan 3 — the Plan 2 shim
continues to populate `haiku_score = sonnet_score = mean(sub_scores)*20`
on every freshly scored row, so every one of A/B/C/D/E is individually
revertable without leaving data stale. Plan 4 removes the shim (and
deletes haiku_scorer.py / sonnet_evaluator.py / score_calibration.py).

- Duration: 4h 45min (wall-clock, single session)
- Commits: 5 atomic, dependency-ordered sub-commits
- Test delta: 2622 baseline -> 2616 after Plan 3 + 7 legacy-path tests
  marked skip (TestBatchHaikuBorderlineReeval, TestHaikuPipelineIntegration,
  TestExclusionFilterIntegration, TestPipelineRunnerLogLevels.test_claude_cli_not_found_logs_at_debug).
  Every skipped test is a Plan 4 deletion target — their premise
  (Haiku-then-Sonnet two-phase pipeline) no longer exists in code.

## Commits

| # | Hash    | Scope | What |
|---|---------|-------|------|
| A | c9bf746 | Query layer (9 prod + 6 test files) | db.py get_filtered_jobs + classification-rank ORDER BY; count_haiku_scorable -> count_scorable; careers_crawler + ats_scanner + agentic_enricher predicates; companies blueprint effective_score; dedup_normalizer merge logic; rejection_analyzer / rejection_patterns dataclass fields |
| B | 6223ce9 | batch_scoring.py route merge | batch_score_start + _run_batch_bg replace the haiku/sonnet pair; legacy URLs kept as delegating wrappers (Plan 4 removes); session_type enum collapses to {scoring, sync} |
| C | 6c950a1 | dashboard.py stats | single scoring_eligible_count + _cached_tier_available("scoring"); active-session detection folds legacy 'haiku'/'sonnet' rows into the unified 'scoring' bucket; back-compat aliases preserve template interop |
| D | 511e590 | Templates | _score_cell + _row_detail + _row_expanded + _resume_section + costs/index render classification-enum badges + 6-dim horizontal-bar sub-score breakdown; Quick Apply + Generate Resume gated on classification is not none |
| E | 30002ef | Resume + pipeline summary | resume_generator multi-version dispatch on classification=='apply'; resume.generate + quick_apply gates check classification; pipeline_runner.run_ingestion summary keys collapse to scored + classified_{apply,consider,skip,reject}; legacy else-branch deleted; use_unified_scorer default True |

## Verification

Per plan `<verification>` and `<success_criteria>`:

| Check | Result |
|-------|--------|
| `uv run --active pytest -q --tb=short` exits 0 after Commit A | PASS — 2622 passed, 1 skipped |
| `uv run --active pytest -q --tb=short` exits 0 after Commit B | PASS — 2623 passed, 4 skipped (+1 TestUnifiedRouteShape, -3 of 6 duplicate haiku/sonnet tests merged; 3 TestBatchHaikuBorderlineReeval skipped) |
| `uv run --active pytest -q --tb=short` exits 0 after Commit C | PASS — 2623 passed, 4 skipped |
| `uv run --active pytest -q --tb=short` exits 0 after Commit D | PASS — 2623 passed, 4 skipped |
| `uv run --active pytest -q --tb=short` exits 0 after Commit E | PASS — 2616 passed, 11 skipped (net -7 to skipped: TestHaikuPipelineIntegration class + TestExclusionFilterIntegration class + TestPipelineRunnerLogLevels.test_claude_cli_not_found_logs_at_debug) |
| `grep -n "j\\.classification IN" job_finder/web/careers_crawler.py` returns a match around line 625 | PASS (line 634 post-edit) |
| `grep -n "def count_scorable" job_finder/web/exclusion_filter.py` returns a match | PASS |
| `grep -n "classification_rank\\|CASE classification" job_finder/db.py` matches inside get_filtered_jobs | PASS — _CLASSIFICATION_RANK_CASE at module level + _classification_score_order() |
| `grep -n "classification_rank\\|CASE classification" job_finder/web/agentic_enricher.py` shows the new ORDER BY | PASS (lines 500-518) |
| `grep -n "_merge_classification\\|_CLASSIFICATION_RANK" job_finder/web/dedup_normalizer.py` matches | PASS (helper + rank dict) |
| `grep -n "classification\\|sub_scores" job_finder/web/rejection_patterns.py` shows renamed fields | PASS |
| `rtk grep -rn '"haiku_score"' job_finder/web/ --include='*.py' --exclude='scoring_orchestrator.py' --exclude='scoring_runner.py' --exclude='haiku_scorer.py' --exclude='sonnet_evaluator.py' --exclude='db_migrate.py'` zero results | PASS — confirmed zero production READ sites |
| `grep -rn "haiku_score\\|sonnet_score\\|haiku_summary" job_finder/web/templates/` zero (excluding alias + comment in costs/index.html) | PASS — 2 hits remain: the intentional legacy-alias entry for pre-Plan-2-flip cost rows |
| `grep -n "def batch_score_start\\|def _run_batch_bg" job_finder/web/blueprints/batch_scoring.py` returns exactly two matches | PASS |
| `grep -n "def _run_batch_haiku_bg\\|def _run_batch_sonnet_bg" job_finder/web/blueprints/batch_scoring.py` returns zero matches | PASS |
| `grep -n "scoring_eligible_count" job_finder/web/blueprints/dashboard.py` returns a match | PASS |
| `grep -n "_cached_tier_available(\"scoring\")" job_finder/web/blueprints/dashboard.py` returns a match | PASS |
| `grep -n "sonnet_score.*multi_threshold\\|sonnet_score\\s*>=" job_finder/web/resume_generator.py` zero matches | PASS |
| `grep -n "classification\\s*==\\s*['\"]apply['\"]" job_finder/web/resume_generator.py` returns a match | PASS |
| `grep -n "haiku_scored\\|sonnet_queued\\|sonnet_evaluated" job_finder/web/pipeline_runner.py` zero matches | PASS |
| `grep -n "scored\\|classified_apply" job_finder/web/pipeline_runner.py` returns matches | PASS |
| Git log shows five `feat(34-03-[A-E]): ...` commits in order | PASS (see Commits table above) |
| Plan 2 legacy-shim write still in place (scoring_orchestrator.py) | PASS — untouched; Plan 4 removes |

## Acceptance Criteria — per task (from PLAN.md)

### Task 1 / Commit A: query-layer migration

All 12 behavior assertions + 8 acceptance-criteria bullets PASS.
Dedup merge test (test_dedup_normalizer) exercises the new
_merge_classification + _merge_sub_scores helpers via the integration
path. Rejection-pattern score distribution derives from mean(sub_scores)*20
for continuity with the old 0-100 bucket edges.

### Task 2 / Commit B: batch_scoring merge

All 5 behavior assertions + 7 acceptance-criteria bullets PASS.
TestUnifiedRouteShape adds 6 new invariants: batch_score_start exists,
_run_batch_bg exists, legacy _run_batch_haiku_bg / _run_batch_sonnet_bg
absent, session_type inserted is 'scoring', SQL predicate uses
`classification IS NULL`, legacy /batch-score/haiku/start URL still
responds 200 via delegating wrapper.

### Task 3 / Commit C: dashboard stats

All 3 behavior assertions + 5 acceptance-criteria bullets PASS.
Legacy session_type values ('haiku', 'sonnet') written before Wave 2's
flag flip fold correctly into the unified 'scoring' active-session
bucket so the UI reflects the live pipeline regardless of session
origin.

### Task 4 / Commit D: templates

All 6 behavior assertions + 6 acceptance-criteria bullets PASS.
Score cell renders a color-coded classification badge (apply=green,
consider=amber, skip=slate, reject=red, unscored=muted) with the
sort-score data attribute set to the classification_rank value.
_row_detail + _row_expanded each gain a 6-dimension Fit Breakdown
card rendering sub_scores_json as horizontal bars (bg color
thresholded on ordinal value: >=4 green, ==3 amber, ==2 orange,
<=1 red) and an AI Fit Analysis section pulling strengths /
gaps / talking_points / resume_priority_skills from fit_analysis.

### Task 5 / Commit E: resume gating + pipeline summary

All 3 behavior assertions + 6 acceptance-criteria bullets PASS.
Resume generate + quick_apply routes both 400 when classification is
None. Multi-version dispatch calls generate_resume_multi iff
classification == 'apply'. pipeline_runner.run_ingestion summary dict
has zero haiku_scored / sonnet_queued / sonnet_evaluated keys;
contains scored + 4 classified_* keys.

## Key Design Decisions (summarized)

All 8 D-17 bullets in the frontmatter `key-decisions` block, plus:

- **Classification-aware URL sort aliases instead of a break-at-the-URL-layer
  change.** The `haiku_score`/`sonnet_score` entries in allowed_sort_cols
  are not deleted in Plan 3 — they're moved into _LEGACY_SORT_ALIAS and
  mapped to the classification + sub_score_sum composite. Bookmarks
  continue to work. Plan 4 removes the alias dict once the rescore pass
  re-populates every row.

- **Numeric-score filter back-compat via OR fallback.** min_score/max_score
  predicates compile to `(classification IN (...) OR (classification IS
  NULL AND score <= X))`. This keeps the pre-Plan-2 heuristic score filter
  (where score is the 0-10 relevance score, not an AI score) working for
  rows that never got a classification, while new rows sort/filter by
  classification.

- **Back-compat aliases in dashboard context dict.** The Commit C stats
  block returns the v3 keys (scoring_eligible_count, etc.) AND the
  legacy aliases (haiku_scorable_count, sonnet_eligible_count, etc.)
  until Commit D lands. This prevents half-wired failures where Commit C
  ships the new stats but Jinja templates still reference the old keys.

- **Delegating batch-scoring wrappers.** Commit B's new
  batch_score_start route is the canonical entry, but the legacy haiku/
  sonnet URLs are preserved as thin wrappers that pass the old "Haiku"
  or "Sonnet" label to the render helper. This makes the existing
  dashboard HTMX selectors (which depend on
  `batch-score-<label.lower()>-status` div IDs) still work between
  Commits B and D without touching the templates.

## Deviations from Plan

Three deviations documented for transparency; none affect user-observable
contracts or D-17 invariants:

1. **Commit B kept session_type='scoring' even for the legacy wrapper
   routes.** The PLAN.md task text was ambiguous about whether the
   delegating wrappers should preserve session_type='haiku'/'sonnet'
   or write session_type='scoring' like the canonical route. I chose
   'scoring' because (a) the unified worker only processes
   classification IS NULL rows, so the distinction is cosmetic, and
   (b) keeping two value-identity for session_type would have blocked
   Commit C's stats collapse. The test test_haiku_start_creates_session_in_db
   in test_views.py was updated to assert session_type='scoring' to
   match.

2. **Added a classification= kwarg to get_filtered_jobs.** The plan
   scoped db.py changes to the internal SQL composition. Exposing
   classification= as an optional filter kwarg was additive and
   necessary to avoid forcing every caller through the
   min_score/max_score translation shim. New behavior tests assert the
   kwarg's IN-predicate path directly.

3. **Skipped TestHaikuPipelineIntegration + TestExclusionFilterIntegration
   + test_claude_cli_not_found_logs_at_debug instead of rewriting
   them.** All three tests guard legacy-path behavior whose code no
   longer exists post-Commit-E. Rewriting them as unified-path tests
   would have duplicated coverage already present in
   TestUnifiedScorerFlagGate + tests/test_scoring_runner.py
   (TestRunScoring). The skip markers include Plan 4 context so the
   deletion is obvious.

## Issues Encountered

One test-matrix defect caught during Commit E's fixture rollup:

- **test_resume.py fixture column-order mismatch (self-caught in CI).**
  After my initial replace_all of the INSERT column list to include
  classification + sub_scores_json, the value tuple order didn't match
  the column order (values had classification BEFORE jd_full, but
  columns had jd_full BEFORE classification). SQLite's "Incorrect
  number of bindings" error triggered the 15-vs-17 count mismatch. Fix
  was to re-order the value tuples so they matched the column order
  (sonnet_score, jd_full, classification, sub_scores_json). No
  production code was affected.

## Authentication Gates

None — Plan 3 never hits a live Claude or Ollama API. Every modified
route is covered by fixture-backed tests using the unified
mock_run_oneshot envelope (superset since Wave 2).

## Next Phase Readiness

Plan 3 is complete. All 16 CONSUMERS-* requirements + 8 TESTS-*
requirements shipped. Read paths fully migrated to classification +
sub_scores_json; writes still dual via the Plan 2 shim.

**Ready for Plan 34-04.** Plan 4 performs the re-score of ~3900
existing rows through the unified scorer (so classification is
populated on every row), then deletes:

- job_finder/web/haiku_scorer.py (entire file)
- job_finder/web/sonnet_evaluator.py (entire file)
- job_finder/web/score_calibration.py (entire file)
- scripts/calibration_refit.py (entire file)
- db.persist_haiku_score + db.persist_sonnet_score
- scoring_orchestrator.score_and_persist_haiku +
  score_and_persist_sonnet (+ the dual-write shim inside
  score_and_persist_job)
- scoring_runner.run_haiku_scoring + run_sonnet_evaluation
- model_provider._TIER_DEFAULTS haiku/sonnet entries
- config.yaml providers.haiku + providers.sonnet (collapse to
  providers.scoring only)
- Back-compat aliases introduced in Plan 3 (count_haiku_scorable alias,
  _LEGACY_SORT_ALIAS dict, batch_score_haiku_start / batch_score_sonnet_start
  wrappers, dashboard aliases)
- Legacy pytest.mark.skip test classes
  (TestBatchHaikuBorderlineReeval, TestHaikuPipelineIntegration,
  TestExclusionFilterIntegration, test_claude_cli_not_found_logs_at_debug)

Plan 5 performs Migration 41 (the final DROP COLUMN pass for
haiku_score, haiku_summary, sonnet_score, opus_score if decided).

No intermediate state is fragile — reverting any Plan 3 commit
restores a fully working system (the shim keeps legacy columns fresh).
