# Career-Ops Adoption Plan

## Purpose

This document replaces the earlier draft with an execution-safe plan that fits job-cannon's current architecture.

The original comparison against career-ops surfaced useful ideas, but several of them crossed existing subsystem boundaries in unsafe ways. This revision keeps the ideas that fit the current system, re-scopes the ones that should live inside existing features, and explicitly defers work that needs new lifecycle design before implementation.

## Current Constraints

- Scoring behavior must stay centralized in `job_finder/web/scoring_orchestrator.py`.
- DB write logic must stay centralized in `job_finder/db.py` helpers.
- Background work must follow the existing `standalone_connection()` thread pattern.
- Long-running UI flows must follow the existing status + polling lifecycle already used by resume generation.
- This is a single-user, local-only Flask app. New subsystems need a high bar.
- Migration numbers in this doc are placeholders. Confirm the next `PRAGMA user_version` at implementation time instead of trusting stale plan text.

## Scope Decisions

### Adopt In This Plan

1. Pre-Sonnet liveness gate
2. Structured Sonnet eval blocks
3. Low-score warning banner
4. Archetype classification
5. Company deep research on demand

### Re-Scope

1. STAR bank becomes interview-prep story reuse inside the existing `interview_preps` lifecycle. No standalone `/interview-prep/star-bank` subsystem in this plan.

### Defer

1. ATS-optimized PDF export is deferred until storage lifecycle, Windows dependency strategy, and resume-history integration are designed explicitly.

## Architecture Guardrails

These are non-negotiable implementation rules for every phase.

1. Do not add new per-caller Sonnet gating logic in `scoring_runner.py`, `batch_scoring.py`, and job routes. All Sonnet preflight rules belong in the orchestration layer.
2. Do not overload `persist_sonnet_score()` with unrelated state transitions. Add dedicated DB helpers when the state being written is not part of Sonnet scoring output.
3. Do not ship prompt changes that alter Sonnet score distribution without handling the existing calibration layer. In this plan, eval-block mode ships with calibration bypass enabled until refreshed calibration data is generated and validated.
4. Do not create new top-level feature pages when the behavior belongs to an existing subsystem.
5. Do not add async polling endpoints without timeout, stale-row recovery, and terminal states.

## Phase 1: Scoring Boundary Hardening

This phase delivers liveness gating, eval blocks, and the low-score banner while preserving the current scoring architecture.

### Feature A: Sonnet Preflight Liveness Gate

**Goal:** stop wasting Sonnet budget on clearly expired postings without duplicating logic across callers.

**Design:**

- Keep lightweight URL-level liveness signal helpers in `expiry_checker.py`.
- Add a new orchestration-level preflight helper in `scoring_orchestrator.py` that decides whether Sonnet should run for a given job.
- Persist expiry state through a dedicated DB helper instead of ad hoc `UPDATE` statements in callers.
- All Sonnet callers continue to call `score_and_persist_sonnet()`; they do not add their own liveness logic.

**Files to modify:**

| File | Change |
|------|--------|
| `job_finder/web/expiry_checker.py` | Add small reusable helpers for per-job URL checks only. Reuse constants and request behavior from the existing module, but do not couple the new helper to the nightly batch runner's ATS-specific cascade. |
| `job_finder/db.py` | Add `expiry_status` to `JOBS_ALL_COLUMNS`. Add `persist_job_expiry_state(conn, dedup_key, expiry_status, checked_at, reason=None)` as the single point of truth for expiry writes. |
| `job_finder/web/db_migrate.py` | Add migration for `jobs.expiry_status TEXT DEFAULT NULL`. Reuse existing `expiry_checked_at`; do not create duplicate timestamps. |
| `job_finder/web/scoring_orchestrator.py` | Add `preflight_sonnet_job(...) -> tuple[bool, dict]` or equivalent internal helper. If expired, persist state and return without calling the evaluator. |
| `job_finder/web/templates/jobs/_row_expanded.html` | Show an `Expired` badge next to stale metadata when `job.expiry_status == 'expired'`. |

**Files not to modify for this feature:**

- `job_finder/web/scoring_runner.py`
- `job_finder/web/blueprints/batch_scoring.py`

Those callers should benefit automatically by continuing to use `score_and_persist_sonnet()`.

**Tests:**

- Add unit tests for the new lightweight liveness helpers.
- Add orchestrator tests verifying Sonnet is skipped and expiry state is persisted.
- Add regression tests proving batch/manual callers do not need separate liveness logic.

### Feature B: Structured Sonnet Eval Blocks

**Goal:** add explainable dimension scores without breaking the current scoring pipeline.

**Design:**

- Extend `SONNET_SCHEMA` with optional `eval_blocks`.
- Persist `eval_blocks` separately from `fit_analysis`.
- Treat this as a scoring-contract change, not a cosmetic prompt edit.
- Before shipping prompt changes broadly, handle calibration explicitly.
- Chosen path for this revision: when eval-block mode is enabled, bypass provider calibration for those Sonnet results until refreshed calibration data is generated and validated.
- Recalibration work still needs to happen, but it is no longer a hidden follow-up item.

**Files to modify:**

| File | Change |
|------|--------|
| `job_finder/web/sonnet_evaluator.py` | Add optional `eval_blocks` schema and move prompt assembly behind a helper so the evaluator is no longer locked to a single module-level prompt string. |
| `job_finder/db.py` | Add `eval_blocks` to `JOBS_ALL_COLUMNS`. Extend `persist_sonnet_score()` to accept `eval_blocks`. |
| `job_finder/web/scoring_orchestrator.py` | Extract and persist `eval_blocks`. Bypass provider calibration while eval-block mode is enabled. |
| `job_finder/web/score_calibration.py` and related evaluation tooling | Update calibration assumptions or add a guarded bypass path for the new prompt contract. |
| `job_finder/web/templates/jobs/_row_expanded.html` | Render eval blocks only when present. The UI must degrade cleanly for old rows. |

**Required implementation note:**

The expanded UI must continue to work for the mixed population of jobs:

- old Sonnet rows with no `eval_blocks`
- new Sonnet rows with `eval_blocks`
- Haiku-only rows

**Tests:**

- Schema accepts responses with and without `eval_blocks`.
- Persistence writes `eval_blocks` correctly.
- Calibration path is explicitly covered by tests for the chosen strategy.
- Template rendering works when `eval_blocks` is missing.

### Feature C: Low-Score Warning Banner

**Goal:** show a clear warning for poor-fit jobs using the app's existing score precedence.

**Design:**

- No new DB field.
- Define `effective_score` as `COALESCE(sonnet_score, haiku_score, score)`.
- Use the same precedence already used in existing company/job listing queries.

**Files to modify:**

| File | Change |
|------|--------|
| `job_finder/web/templates/jobs/_row_expanded.html` | Add an amber warning banner when the effective score is below 40. Compute the value using the existing precedence rule, not a new column. |

**Tests:**

- Template-level coverage for Sonnet-scored, Haiku-only, and unscored rows.

## Phase 2: Prompt Context and Interview Reuse

This phase adds archetype-aware context and reuses interview story material without creating a new standalone subsystem.

### Feature D: Archetype Classification

**Goal:** classify jobs into a small number of role archetypes and use that context consistently across scoring and UI.

**Design:**

- Implement archetype classification as a pure Python helper, not as an additional Haiku output field.
- Persist the derived archetype on the job row for display and later reuse.
- Pass archetype into Sonnet prompt construction explicitly instead of relying on callers to re-fetch mutated rows.

This avoids two current risks:

1. expanding the Haiku response contract for something that is deterministic and config-driven
2. depending on stale prefetched job rows between Haiku persistence and Sonnet evaluation

**Files to create:**

| File | Contents |
|------|----------|
| `job_finder/web/archetype_classifier.py` | Pure helper(s) for classification and config-driven weight lookup. No model calls. |
| `tests/test_archetype_classifier.py` | Keyword classification, no-match fallback, config validation behavior. |

**Files to modify:**

| File | Change |
|------|--------|
| `config.example.yaml` | Add example archetype definitions under `profile`. |
| `job_finder/web/db_migrate.py` | Add migration for `jobs.archetype TEXT DEFAULT NULL`. |
| `job_finder/db.py` | Add `archetype` to `JOBS_ALL_COLUMNS`. Add `persist_job_archetype(conn, dedup_key, archetype)` helper rather than overloading Haiku persistence with another unrelated responsibility. |
| `job_finder/web/scoring_orchestrator.py` | Classify archetype before persistence, store it, and pass it explicitly into Sonnet evaluation. |
| `job_finder/web/sonnet_evaluator.py` | Accept optional archetype context in prompt assembly. |
| `job_finder/web/templates/jobs/_row.html` | Render archetype badge. |
| `job_finder/web/templates/jobs/_row_expanded.html` | Render archetype badge. |

**Implementation note:**

If config entries are removed later, classification helpers must fail soft and return no overrides instead of throwing.

### Feature E: Reusable Interview Stories Inside Existing Interview Prep

**Goal:** reuse strong STAR-style material across interview prep runs without introducing a separate star-bank product.

**Design:**

- Extend the existing `interview_preps` lifecycle.
- After successful interview prep generation, optionally persist a normalized `reusable_stories_json` payload on that prep row or in a tightly scoped companion table keyed to `job_id`.
- Future interview-prep runs can load recent reusable stories from prior completed interview preps.
- Do not add a standalone `/interview-prep/star-bank` blueprint in this phase.
- Any added extraction call must use `call_model('haiku', ...)` so it inherits the existing cascade/provider-attribution behavior instead of making a direct Anthropic call.

**Why this scope is safer:**

- the app already has `interview_preps`
- the data stays job-linked
- the feature remains optional context for the existing Opus prompt
- there is no new permanent CRUD UI to maintain

**Files to modify:**

| File | Change |
|------|--------|
| `job_finder/web/db_migrate.py` | Add the minimum schema needed for reusable story context inside the interview-prep subsystem. Prefer extending `interview_preps` over adding a new top-level knowledge base. |
| `job_finder/web/interview_prep.py` | Extract and persist reusable story context after successful prep generation, then load recent story context for later prompt construction. |
| `tests/test_interview_prep.py` or dedicated interview story tests | Cover extraction, storage, and prompt reuse. |

**Explicit non-goals for this phase:**

- no `star_stories` table as a new app-wide subsystem
- no `/interview-prep/star-bank` page
- no delete/edit UI for story records

## Phase 3: Async Company Intelligence

This phase adds company research, but only if it follows the existing async lifecycle discipline already established by resume generation.

### Feature F: Company Deep Research On Demand

**Goal:** provide cached, on-demand company intelligence for jobs already linked to companies.

**Design:**

- Keep this feature company-linked. Do not create research rows for free-floating company names that are not represented in `companies`.
- Use a dedicated `company_research` table with `status`, `research_json`, `error_msg`, `generated_at`, and cost metadata.
- Any summarization/classification model call in this feature must use `call_model('haiku', ...)` so it inherits the existing cascade/provider-attribution behavior.
- Mirror the resume-generation lifecycle:
	- insert pending/generating row
	- run background thread with `standalone_connection()`
	- poll a status endpoint
	- enforce timeout/stale-row recovery using the same 10-minute safety-net pattern already used by resume status polling
	- stop polling on `done` or `error`

**Files to create:**

| File | Contents |
|------|----------|
| `job_finder/web/company_research.py` | Research service and background runner. |
| `job_finder/web/blueprints/company_research.py` | Start/status endpoints under `/companies`. |
| `job_finder/web/templates/companies/_research_section.html` | Research display partial. |
| `job_finder/web/templates/companies/_research_generating.html` | Polling partial that stops on terminal states. |
| `tests/test_company_research.py` | Service, route, and timeout behavior. |

**Files to modify:**

| File | Change |
|------|--------|
| `job_finder/web/db_migrate.py` | Add `company_research` table with status/error fields and index on `company_id`. |
| `job_finder/web/templates/jobs/_row_expanded.html` | Add `Research Company` action only when `company_id` is present. |
| `job_finder/web/templates/companies/_row_expanded.html` | Include current/cached research section. |
| `job_finder/web/__init__.py` | Register the blueprint before routes that could shadow it. |

**Required lifecycle rules:**

1. status endpoint must mark stale `generating` rows as `error` after a timeout
2. polling fragment must stop on `done` and `error`
3. cache reads must be explicit about TTL
4. route tests must cover timeout and cache-hit behavior

The timeout threshold for the first implementation should match the existing resume-generation safety net: 10 minutes from `generated_at`, after which the status route marks the row as `error` and returns a terminal fragment.

## Deferred Work: ATS-Friendly PDF Export

Feature 7 is intentionally deferred.

It is useful, but it is not execution-safe yet because the unresolved problems are not implementation details; they are lifecycle design problems.

### Reasons For Deferral

1. local file storage policy is undefined
2. cleanup/retention policy is undefined
3. interaction with existing `resume_generations` history is undefined
4. Windows rendering dependency strategy is still speculative

### Entry Criteria To Reopen This Work

Do not re-open PDF export until a short design note answers all of these:

1. Where does the PDF live?
2. How is it cleaned up?
3. Is it cached or generated on demand?
4. How does it appear in resume history?
5. What is the Windows-first rendering stack?

## Migration Summary

Migration numbering must be assigned from the live database state at implementation time.

Planned schema changes in this revision:

| Area | Change |
|------|--------|
| Jobs | Add `expiry_status` |
| Jobs | Add `eval_blocks` |
| Jobs | Add `archetype` |
| Interview prep | Add minimal reusable-story storage inside the existing subsystem |
| Company research | Add `company_research` status/cache table |

## Verification Plan

### Automated

Run the full suite after each phase:

```bash
uv run --active pytest -q --tb=short
```

Phase-specific coverage must also include:

1. orchestrator-level Sonnet skip coverage for expiry
2. calibration-safety coverage for eval-block mode
3. mixed-row template rendering coverage for old/new Sonnet rows
4. archetype classification soft-failure coverage when config is incomplete
5. async company-research timeout coverage

### Manual

1. Expired job: Sonnet does not run, expiry badge appears in expanded view.
2. Mixed Sonnet data: old scored jobs still expand cleanly without eval blocks; new scored jobs show eval blocks.
3. Low-score job: warning banner uses the same score precedence as the rest of the app.
4. Archetype job: badge appears in compact and expanded rows; Sonnet prompt receives archetype context.
5. Interview prep reuse: later prep runs can reference previous reusable story context without requiring a new top-level page.
6. Company research: polling stops on completion or error and stale generations do not spin forever.

## Exit Criteria

This adoption work is complete only when all of the following are true:

1. Sonnet gating is enforced in one place.
2. New DB writes use explicit persistence helpers.
3. Eval blocks ship with a calibration-safe strategy.
4. Interview story reuse stays inside the existing interview-prep subsystem.
5. Company research follows the same async discipline as resume generation.
6. PDF export remains deferred unless its lifecycle is designed explicitly.
# Career-Ops Feature Adoption Plan

## Context

Comparative analysis of [santifer/career-ops](https://github.com/santifer/career-ops) identified 7 improvements worth adopting into job-cannon. This plan covers their implementation in dependency order across 3 phases. Each feature preserves existing patterns (scoring_orchestrator dispatch, persist_*() single-point-of-truth, standalone_connection() for background threads, HTMX fragment routes).

Current migration count: **25** (next = 26). Current `JOBS_ALL_COLUMNS` in `db.py:21-28`.

---

## Phase 1: Quick Wins (Features 1, 3, 4)

Zero inter-dependencies. Template + prompt changes. No new blueprints.

### Feature 1: Pre-Sonnet Liveness Check

**Problem:** Sonnet budget wasted evaluating expired job postings. The existing `expiry_checker.py` runs nightly as a batch; there's no per-job gate before Sonnet eval.

**Approach:** Add a lightweight `quick_liveness_check()` to the existing `expiry_checker.py` module (reuse its constants `EXPIRED`/`LIVE`/`INCONCLUSIVE`, `_TIMEOUT`, and `requests` import). Gate Sonnet eval in both scoring paths.

**Files to modify:**

| File | Change |
|------|--------|
| `job_finder/web/expiry_checker.py` | Add `quick_liveness_check(url, timeout=8) -> str` — HTTP HEAD with GET fallback, body pattern scan for "no longer accepting", "position filled", "expired", 404/410 detection. Returns `EXPIRED`/`LIVE`/`INCONCLUSIVE`. Add `check_job_liveness(job_row) -> tuple[str, str]` — iterates `source_urls` JSON, returns `(status, reason)`. ~60 lines. |
| `job_finder/web/db_migrate.py` | **Migration 26:** `ALTER TABLE jobs ADD COLUMN expiry_status TEXT DEFAULT NULL` |
| `job_finder/db.py` | Add `expiry_status` to `JOBS_ALL_COLUMNS` (line 21-28) |
| `job_finder/web/scoring_runner.py` | In `run_sonnet_evaluation()` at line ~248 (the per-job loop, before `is_stub_jd` check): call `check_job_liveness()`, if expired → UPDATE `expiry_checked_at` + `expiry_status`, skip job |
| `job_finder/web/blueprints/batch_scoring.py` | In `_run_batch_sonnet_bg()` at line ~540 (before `score_and_persist_sonnet`): same liveness gate |
| `job_finder/web/templates/jobs/_row_expanded.html` | After stale badge: render `Expired` amber badge when `job.expiry_status == 'expired'` |

**New test file:** `tests/test_liveness_check.py` (~120 lines)
- `TestQuickLivenessCheck`: mock requests.head/get for 404, 200, "position filled" body, timeout
- `TestCheckJobLiveness`: multiple URLs, empty source_urls, all live, one expired
- `TestLivenessGateIntegration`: mock liveness → expired, verify Sonnet skipped + expiry_status set

---

### Feature 3: Structured 6-Block Evaluation in Sonnet

**Problem:** Flat score + fit_analysis lacks dimensional transparency. Career-ops evaluates across 6 explicit blocks with separate scores.

**Approach:** Extend `SONNET_SCHEMA` with an **optional** `eval_blocks` field (not in `required` list initially — old providers and cached evals still work). Update system prompt to instruct block-level evaluation.

**Files to modify:**

| File | Change |
|------|--------|
| `job_finder/web/sonnet_evaluator.py` | Add `eval_blocks` to `SONNET_SCHEMA["properties"]` as optional object with 6 sub-objects: `role_match`, `skills_experience`, `compensation`, `culture_growth`, `red_flags`, `recommendation` — each `{score: int, rationale: str}`. Update `_BASE_SYSTEM_PROMPT` to add block evaluation instructions (~25 lines of prompt text). Update `_FEWSHOT_EXAMPLES` to include block scores in examples. |
| `job_finder/web/db_migrate.py` | **Migration 27:** `ALTER TABLE jobs ADD COLUMN eval_blocks TEXT DEFAULT NULL` |
| `job_finder/db.py` | Add `eval_blocks` to `JOBS_ALL_COLUMNS`. Update `persist_sonnet_score()` signature: add `eval_blocks: str \| None = None` param, COALESCE in SQL. |
| `job_finder/web/scoring_orchestrator.py` | In `score_and_persist_sonnet()` (~line 180): extract `eval_blocks` from result, `json.dumps()`, pass to `persist_sonnet_score()` |
| `job_finder/web/templates/jobs/_row_expanded.html` | After existing fit_analysis section: render eval_blocks as horizontal CSS bar chart (6 bars, color-coded by score tier). Graceful: only renders when `job.eval_blocks` is truthy. ~35 lines template. |

**Test additions:** In `tests/test_sonnet_evaluator.py` — verify schema accepts response with eval_blocks, verify response without eval_blocks still validates. In `tests/test_db.py` — verify `persist_sonnet_score()` writes eval_blocks. ~40 lines.

---

### Feature 4: Low-Score Warning Banner

**Problem:** No visual signal discouraging application to low-match jobs.

**Approach:** Template-only change. Zero backend modifications.

**File to modify:**

| File | Change |
|------|--------|
| `job_finder/web/templates/jobs/_row_expanded.html` | After the error banner block (~line 30): add conditional amber banner when effective_score < 40. Text: "Low match — AI recommends focusing your time on stronger-fit roles". ~8 lines. |

---

## Phase 2: Intelligence Layer (Features 6, 2)

Feature 2 (STAR Bank) benefits from Feature 3's richer eval_blocks data. Feature 6 (Archetypes) feeds into scoring prompts.

### Feature 6: Archetype Classification

**Problem:** Flat profile treats all jobs identically. Jobs in different role archetypes (Data Platform vs ML Engineering vs Analytics Lead) should be evaluated with different criteria emphasis.

**Approach:** Keyword-based classification in Haiku (zero extra AI cost — add `archetype` to existing Haiku output schema). Config-driven archetype definitions.

**Files to create:**

| File | Contents |
|------|----------|
| `job_finder/web/archetype_classifier.py` | `classify_archetype(title: str, description: str, config: dict) -> str \| None` — keyword matching against `config.profile.archetypes[].keywords`. `get_archetype_weights(archetype: str, config: dict) -> dict` — return weight overrides. ~70 lines. |
| `tests/test_archetype_classifier.py` | Keyword matching, fallback to None, config-driven definitions. ~80 lines. |

**Files to modify:**

| File | Change |
|------|--------|
| `config.example.yaml` | Add `archetypes` list under `profile` with 3-5 example archetypes (name, keywords[], weight_overrides{}) |
| `job_finder/web/db_migrate.py` | **Migration 28:** `ALTER TABLE jobs ADD COLUMN archetype TEXT DEFAULT NULL` |
| `job_finder/db.py` | Add `archetype` to `JOBS_ALL_COLUMNS`. Update `persist_haiku_score()`: add `archetype: str \| None = None` param. |
| `job_finder/web/haiku_scorer.py` | Add `archetype` string field to `HAIKU_SCHEMA`. Add archetype list to system prompt from config. ~15 lines. |
| `job_finder/web/scoring_orchestrator.py` | In `score_and_persist_haiku()`: extract archetype from result, pass to `persist_haiku_score()`. In `score_and_persist_sonnet()`: read job's archetype, inject into Sonnet user message. |
| `job_finder/web/sonnet_evaluator.py` | Accept optional `archetype` in user message construction. Include weight overrides in system prompt when archetype has overrides. |
| `job_finder/web/templates/jobs/_row.html` | Archetype badge (purple pill) after company name. |
| `job_finder/web/templates/jobs/_row_expanded.html` | Archetype badge in header area. |

---

### Feature 2: STAR+R Cumulative Story Bank

**Problem:** Interview prep generates STAR stories fresh for each job. Career-ops accumulates stories across evaluations, building a reusable master bank.

**Approach:** New `star_stories` table. Extract stories after each Sonnet eval using Haiku (~$0.002/extraction). New blueprint for viewing/managing the bank. Integrate into interview prep prompt.

**Files to create:**

| File | Contents |
|------|----------|
| `job_finder/web/star_bank.py` | `StarStory` frozen dataclass. `extract_stories_from_eval(job_row, fit_analysis, eval_blocks, profile, conn, config) -> list[dict]` — Haiku call to structure STAR-R from fit_analysis strengths + talking_points + profile achievements. `merge_into_bank(conn, new_stories) -> int` — dedup by title keyword overlap, merge source_job_ids. `get_star_bank(conn) -> list[dict]`. ~180 lines. |
| `job_finder/web/blueprints/star_bank.py` | Blueprint `star_bank_bp` at `/interview-prep`. Routes: `GET /star-bank` (index), `POST /star-bank/<id>/delete`. ~60 lines. |
| `job_finder/web/templates/star_bank/index.html` | Card grid: story title, STAR-R sections, question archetypes tags, usage count, source job links. ~90 lines. |
| `tests/test_star_bank.py` | Extraction with mock Haiku, merge dedup logic, route tests. ~130 lines. |

**Files to modify:**

| File | Change |
|------|--------|
| `job_finder/web/db_migrate.py` | **Migration 29:** CREATE TABLE `star_stories` (id, story_title, situation, task, action, result, reflection, source_job_ids JSON, question_archetypes JSON, times_used INT, created_at, updated_at). Index on `times_used DESC`. |
| `job_finder/web/scoring_orchestrator.py` | In `score_and_persist_sonnet()` after persist: call `extract_stories_from_eval()` in try/except (best-effort, never blocks scoring). |
| `job_finder/web/interview_prep.py` | In prompt construction: load top 10 star_bank stories, include as "Reusable STAR Stories" context. Increment `times_used` for stories referenced in output. |
| `job_finder/web/__init__.py` | Register `star_bank_bp`. |

---

## Phase 3: Deep Intelligence (Features 5, 7)

Higher effort. New external dependencies for Feature 7.

### Feature 5: Company Deep Research On-Demand

**Problem:** Company features are ATS-probe focused. No deep research on culture, funding, tech stack, Glassdoor signals for high-scoring jobs before applying.

**Approach:** On-demand research triggered by button click (high-score jobs) or manual trigger. SerpAPI for web search (3-5 queries per company), Haiku for synthesis. Cache per company with 30-day TTL.

**Files to create:**

| File | Contents |
|------|----------|
| `job_finder/web/company_research.py` | `CompanyBrief` frozen dataclass (funding, size, culture, tech_stack, glassdoor_rating, recent_news, generated_at, cost_usd). `RESEARCH_SCHEMA` JSON schema. `research_company(company_name, conn, config) -> CompanyBrief \| None` — 3-5 SerpAPI queries + Haiku synthesis. `get_cached_research(conn, company_id, max_age_days=30) -> dict \| None`. `research_company_background(company_id, db_path, config)` — background thread wrapper. ~170 lines. |
| `job_finder/web/blueprints/company_research.py` | Blueprint at `/companies`. `POST /<id>/research` (start), `GET /<id>/research/status` (poll). ~60 lines. |
| `job_finder/web/templates/companies/_research_section.html` | Collapsible section: funding, culture, tech stack list, Glassdoor rating, recent news. ~70 lines. |
| `job_finder/web/templates/companies/_research_generating.html` | Spinner with `hx-trigger="every 2s"` polling. ~12 lines. |
| `tests/test_company_research.py` | Mock SerpAPI + Haiku, verify brief structure, cache hit/miss, routes. ~100 lines. |

**Files to modify:**

| File | Change |
|------|--------|
| `job_finder/web/db_migrate.py` | **Migration 30:** CREATE TABLE `company_research` (id, company_id FK, status TEXT DEFAULT 'generating', research_json TEXT, generated_at, cost_usd). Index on `company_id`. |
| `job_finder/web/templates/jobs/_row_expanded.html` | "Research Company" button near action buttons (when company_id present). |
| `job_finder/web/templates/companies/_row_expanded.html` | Include research section partial when data exists. |
| `job_finder/web/__init__.py` | Register `company_research_bp`. |

---

### Feature 7: ATS-Optimized PDF Export

**Problem:** Resume generation only produces DOCX via Google Docs API. Many ATS systems prefer single-column PDF with keyword injection.

**Approach:** HTML template rendered to PDF via `weasyprint`. Haiku reformulates experience bullets with `resume_priority_skills` from Sonnet eval (keyword injection, never fabrication).

**Risk:** WeasyPrint has native dependencies (Pango, Cairo) that can be complex on Windows. **Fallback:** `fpdf2` (pure Python) if WeasyPrint install fails — simpler but requires manual layout.

**Files to create:**

| File | Contents |
|------|----------|
| `job_finder/web/pdf_generator.py` | `generate_ats_pdf(job_row, experience_profile, fit_analysis, conn, config) -> bytes` — load priority skills, reformulate bullets via Haiku, render HTML template, convert to PDF. `_reformulate_bullets(bullets, priority_skills, conn, config) -> list[str]` — Haiku call for keyword injection. `generate_pdf_background(dedup_key, db_path, config)` — background thread. ~160 lines. |
| `job_finder/web/templates/resume/ats_template.html` | Single-column, system-font (Arial/Helvetica), 11pt, 0.5in margins. Sections: header, summary, experience (reformulated bullets), skills, education. No tables, no graphics. ~100 lines. |
| `tests/test_pdf_generator.py` | Mock Haiku reformulation, mock WeasyPrint, verify HTML rendering, route integration. ~110 lines. |

**Files to modify:**

| File | Change |
|------|--------|
| `job_finder/web/db_migrate.py` | **Migration 31:** `ALTER TABLE resume_generations ADD COLUMN format TEXT DEFAULT 'docx'`, `ALTER TABLE resume_generations ADD COLUMN file_path TEXT DEFAULT NULL` |
| `job_finder/web/blueprints/resume.py` | Add `POST /<key>/resume/generate-pdf` route (spawn background thread), `GET /<key>/resume/pdf-status/<id>` polling endpoint, `GET /<key>/resume/download-pdf/<id>` serve file. |
| `job_finder/web/templates/jobs/_row_expanded.html` | "Generate PDF" button alongside existing resume generation. |
| `requirements.txt` | Add `weasyprint~=62.0` (or `fpdf2~=2.8` as fallback) |

---

## Migration Summary

| # | Feature | SQL |
|---|---------|-----|
| 26 | Liveness | `ALTER TABLE jobs ADD COLUMN expiry_status TEXT DEFAULT NULL` |
| 27 | Eval Blocks | `ALTER TABLE jobs ADD COLUMN eval_blocks TEXT DEFAULT NULL` |
| 28 | Archetypes | `ALTER TABLE jobs ADD COLUMN archetype TEXT DEFAULT NULL` |
| 29 | STAR Bank | `CREATE TABLE star_stories (...)` |
| 30 | Company Research | `CREATE TABLE company_research (...)` |
| 31 | PDF Export | `ALTER TABLE resume_generations ADD COLUMN format/file_path` |

## Cross-Cutting Updates

Every feature adding a jobs column must update:
1. `job_finder/db.py` `JOBS_ALL_COLUMNS` (line 21-28)
2. Relevant `persist_*()` functions in `job_finder/db.py`

## Key Risks

1. **Feature 3 schema change**: Making `eval_blocks` required would break older/alternative providers. Mitigation: keep it optional (not in `required` list) until all providers confirmed.
2. **Feature 7 WeasyPrint on Windows**: Native dependencies (Pango, Cairo) can be complex. Mitigation: fall back to `fpdf2` (pure Python) if install fails.
3. **Feature 2 STAR extraction cost**: Haiku call per Sonnet eval adds ~$0.002/job. Mitigation: budget-gated same as scoring, best-effort extraction (never blocks scoring).

## Verification

After each phase:
```bash
uv run --active pytest -q --tb=short                    # All tests pass
uv run --active pytest tests/test_liveness_check.py -v  # Phase 1
uv run --active pytest tests/test_archetype_classifier.py tests/test_star_bank.py -v  # Phase 2
uv run --active pytest tests/test_company_research.py tests/test_pdf_generator.py -v  # Phase 3
```

Manual verification:
- Phase 1: Expand a job with expired URL → see "Expired" badge, Sonnet skipped in batch
- Phase 1: Expand a Sonnet-scored job → see 6-block bar chart below fit analysis
- Phase 1: Expand a low-score job → see amber warning banner
- Phase 2: View `/interview-prep/star-bank` → see accumulated stories
- Phase 2: Job cards show archetype badges
- Phase 3: Click "Research Company" → see polling → company brief renders
- Phase 3: Click "Generate PDF" → download ATS-friendly single-column PDF
