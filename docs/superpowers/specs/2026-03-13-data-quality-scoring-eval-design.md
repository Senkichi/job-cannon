# Data Quality & Scoring Evaluation — Design Spec

**Date:** 2026-03-13
**Scope:** Clear outstanding TODOs, backfill missing data, evaluate scoring system effectiveness

## Overview

Four-phase milestone to clean up technical debt, backfill job and company data, then use the clean dataset to evaluate whether the Haiku/Sonnet scoring pipeline is effective or needs fundamental changes.

## Phase 1: Bug Fixes

### Test Fixtures Optimization
- Analyze current `conftest.py` per-test migration overhead
- Introduce session-scoped fixture: create a migrated DB once, each test gets a copy or uses transaction rollback
- Constraint: tests must remain fully isolated — no test sees another test's data

### Duplicate Notifications Fix
- **Investigation 1 — dedup failure:** Determine why notifications repeat (e.g., Stripe rejection 8x). Check `email_parse_log` for duplicate `message_id`s to confirm whether `_already_processed()` is failing or whether Gmail API query overlap produces different IDs across runs. Fix: move `_mark_processed()` before notification dispatch, and address any Gmail query overlap if found
- **Investigation 2 — "Title Body" placeholder:** Trace the origin across the full pipeline: parsers, `gmail_source.py`, `pipeline_detector.py` `_extract_body_from_payload()`, and `notifier.py`. Do not assume the source until confirmed. Fix: address wherever the placeholder originates
- **Safety net:** Add dedup guard in `notifier.py` — don't fire same notification for same job within 24h

### Verification
- Run pipeline detector 3 times in succession; confirm `email_parse_log` has no duplicate `message_id`s and notification count equals detection count
- Verify no "Title Body" placeholder text appears in any notification path

## Phase 2: Data Cleanup (Purge)

### Identify Bulk Load
- Query `SELECT date(first_seen) as day, count(*) FROM jobs GROUP BY day ORDER BY count(*) DESC` to find the ingestion spike
- Confirm with user before purging — show date range, count, sample titles

### Purge Process
- Export matched jobs to `data/purged_jobs_YYYY-MM-DD.json` (full rows, all columns)
- Hard delete from `jobs` table
- Clean up orphaned rows in `scoring_costs` and `pipeline_detections` referencing purged `dedup_key`s (note: `email_parse_log` has no `dedup_key` column — purge by date range if needed)
- Report: count purged, count remaining, company records with zero jobs after purge

### Verification
- Post-purge job count is within expected range (confirm with user)
- Zero orphaned rows in `scoring_costs` or `pipeline_detections` referencing deleted jobs

## Phase 3: Backfill & Enrichment

### Job Enrichment
- Run `run_enrichment_backfill()` on all jobs not at `exhausted` tier
- Multiple passes needed (100 jobs per call)
- Track before/after: count of jobs at each enrichment tier

### Re-Score After Enrichment
- Jobs with newly populated `jd_full` but no `sonnet_score` → queue for Sonnet evaluation
- Re-score Haiku on borderline jobs (score 40-70) whose `enrichment_tier` advanced during this phase (proxy for key fields changing, since no per-field changelog exists)
- Budget-aware: estimate cost before running

### Companies Backfill
- Extract distinct company names from `jobs WHERE company_id IS NULL`
- Normalize names (strip "Inc.", "LLC", match against existing companies to avoid duplicates)
- Create new company records, update `jobs.company_id` linkage
- Run ATS probing on new companies (Lever/Greenhouse/Ashby slug discovery)
- Run DuckDuckGo company info enrichment (size, industry, funding stage)

### Budget Consideration
- Company name normalization and ATS probing are free (HTTP-only); only AI-tier job enrichment (Haiku/Sonnet) needs budget gating
- Count jobs at each enrichment tier, estimate expected spend against $25/month budget
- Present cost estimate to user before running AI-tier enrichment

### Verification
- All jobs at `exhausted` or have `jd_full` populated
- All jobs have `company_id` linked to a valid company record
- Enrichment tier distribution reported before and after

## Phase 4: Scoring Evaluation

### Part A — Opus Code Review
- **Input:** Full text of `haiku_scorer.py`, `sonnet_evaluator.py`, profile schema, `config.yaml` scoring section
- **Questions:** Are scoring dimensions right? Is calibration effective? Blind spots in prompts? Is static weighting fundamentally limiting?
- **Output:** `.planning/scoring_evaluation/CODE_REVIEW.md`

### Part B — Opus Data Review
- **Input:** ~50-100 scored jobs, stratified: ~10 per `user_interest` bucket (unreviewed, interested, not_interested, applied), balanced across score quintiles. Cap total input to manage Opus costs
- **Questions:** Where do scores disagree with user decisions? Systematic biases? Missed patterns?
- **Output:** `.planning/scoring_evaluation/DATA_REVIEW.md`

### Part C — Recommendations
- **Synthesize** both reviews into `.planning/scoring_evaluation/RECOMMENDATIONS.md`
- **Concrete outputs:** Prompt tuning recommendations, calibration adjustments, new scoring dimensions, or recommendation to move toward iterative learning
- **If prompt changes recommended:** Draft updated prompts for user review before applying

### Verification
- All three reports produced with concrete, actionable findings
- User reviews recommendations and decides next steps

### Explicitly Out of Scope
- No app features or dashboards (evaluate first, decide later)
- No automatic prompt changes — recommendations only, user decides what to apply

## Dependency Chain

```
Phase 1 (Bug Fixes) → independent, can run first
Phase 2 (Purge) → must complete before Phase 3
Phase 3 (Backfill) → must complete before Phase 4
Phase 4 (Scoring Eval) → needs clean, enriched dataset
```

## Key Files

| File | Role |
|------|------|
| `job_finder/web/haiku_scorer.py` | Fast-filter scoring (Haiku) |
| `job_finder/web/sonnet_evaluator.py` | Deep evaluation (Sonnet) |
| `job_finder/web/data_enricher.py` | Cost-ordered enrichment pipeline |
| `job_finder/web/pipeline_detector.py` | Email classification + dedup |
| `job_finder/web/notifier.py` | Windows toast notifications |
| `job_finder/web/blueprints/companies.py` | Company management routes |
| `job_finder/web/claude_client.py` | API wrapper + cost tracking |
| `tests/conftest.py` | Test fixtures |
