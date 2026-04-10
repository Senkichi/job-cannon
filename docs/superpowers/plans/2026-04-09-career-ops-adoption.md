# Career-Ops Adoption Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the architecture-safe subset of the career-ops adoption work: Sonnet liveness preflight, structured eval blocks with temporary calibration bypass, low-score warnings, deterministic job archetypes, interview-prep story reuse, and on-demand company research.

**Architecture:** Keep scoring decisions centralized in `job_finder/web/scoring_orchestrator.py`, persist all new state via explicit DB helpers in `job_finder/db.py`, reuse the existing `interview_preps` subsystem instead of creating a new star-bank product, and model company research after the existing resume-generation async lifecycle. The plan intentionally defers PDF export and intentionally uses `job_archetype` naming to avoid collision with existing resume `role_archetype` concepts.

**Tech Stack:** Flask 3.1, Jinja2 + HTMX, SQLite with `pragma user_version` migrations, pytest via `uv run --active pytest -q --tb=short`, existing provider cascade through `call_model()`.

---

## Implementation Decisions Locked By This Plan

1. **Calibration bypass mechanism:** when a Sonnet result includes `eval_blocks`, skip provider calibration in `score_and_persist_sonnet()` and persist the raw score as the displayed score. No new config key is added in this phase. **Note:** no `calibration_*.json` files are currently deployed, so `has_calibration()` always returns `False` today — this bypass is forward-compatible infrastructure, not a current behavioral change. Test the bypass path for correctness but do not treat it as a regression risk against existing scoring.
2. **Archetype field name:** use `job_archetype` on the `jobs` table, not `archetype`, to avoid semantic collision with resume-generation `role_archetype` language.
3. **Interview story storage:** add `reusable_stories_json TEXT DEFAULT NULL` to `interview_preps`; story reuse remains prep-row scoped rather than becoming a separate table or UI.
4. **Company research route ownership:** add research routes directly to `job_finder/web/blueprints/companies.py`. Do not create a second `/companies` blueprint.
5. **Expiry status enum:** persist only `expired`, `live`, or `inconclusive`. Do not add `expiry_reason` in this phase; reason stays in logs only.
6. **Expiry writes are atomic:** `persist_job_expiry_state()` performs one `UPDATE` that writes both `expiry_status` and `expiry_checked_at`. The nightly batch runner in `expiry_checker.py` currently writes `expiry_checked_at` via direct SQL — it must be migrated to use this helper so there is exactly one write path for expiry state.
7. **Helper signatures stay minimal:** new DB helpers are intentionally narrow and do not add speculative optional parameters in this phase.
8. **Reusable story extraction algorithm:** derive `reusable_stories_json` directly from `predicted_questions` by storing up to the first 5 distinct `{question, star_story, key_points}` objects whose `star_story` is non-empty after whitespace normalization. This is pure JSON filtering — no LLM call required.

---

## File Map

| File | Responsibility |
|------|----------------|
| `job_finder/web/expiry_checker.py` | Lightweight per-job URL liveness helpers reused by scoring preflight |
| `job_finder/web/scoring_orchestrator.py` | Single Sonnet preflight boundary, eval-block persistence, calibration bypass, archetype injection |
| `job_finder/web/sonnet_evaluator.py` | Dynamic prompt builder, optional `eval_blocks`, optional `job_archetype` prompt context |
| `job_finder/web/archetype_classifier.py` | Deterministic archetype classification + weight lookup |
| `job_finder/web/interview_prep.py` | Extract/store reusable stories per prep row, feed prior stories into prompt context |
| `job_finder/web/company_research.py` | Background company research service and polling-state helpers |
| `job_finder/web/blueprints/companies.py` | Start/status routes for company research alongside existing company routes |
| `job_finder/db.py` | `JOBS_ALL_COLUMNS`, new persistence helpers, expanded Sonnet persistence contract |
| `job_finder/web/db_migrate.py` | New migrations for `expiry_status`, `eval_blocks`, `job_archetype`, `reusable_stories_json`, `company_research` |
| `job_finder/web/__init__.py` | *(No change needed — company research routes live on the existing `companies_bp`)* |
| `job_finder/web/templates/jobs/_row.html` | Compact-row archetype badge |
| `job_finder/web/templates/jobs/_row_expanded.html` | Expiry badge, low-score banner, eval-block rendering, archetype badge, research button |
| `job_finder/web/templates/companies/_row_expanded.html` | Render research section / generating state |
| `job_finder/web/templates/companies/_research_section.html` | Final research display |
| `job_finder/web/templates/companies/_research_generating.html` | Polling fragment |
| `tests/test_expiry_checker.py` | Liveness helper and expiry-state behavior |
| `tests/test_scoring.py` | Orchestrator Sonnet preflight, eval-block persistence, calibration bypass, prompt context |
| `tests/test_sonnet_evaluator.py` | Schema/prompt construction coverage |
| `tests/test_db.py` | Persistence helper coverage |
| `tests/test_migration.py` | New schema/index coverage |
| `tests/test_interview_prep.py` | Reusable-story extraction, storage, and prompt reuse |
| `tests/test_companies.py` | Company-research route lifecycle coverage |
| `tests/test_company_research.py` | Company-research service-layer coverage |

### Config Shape For Task 5

Add this exact example structure to `config.example.yaml`:

```yaml
profile:
  job_archetypes:
    platform_engineering:
      keywords: ["platform", "infrastructure", "kubernetes", "devops"]
      weight_overrides: {}
    ml_engineering:
      keywords: ["machine learning", "ml", "model serving", "feature store"]
      weight_overrides: {}
    analytics_lead:
      keywords: ["analytics", "experimentation", "stakeholder", "roadmap"]
      weight_overrides: {}
```

Consumers must read `config.get("profile", {}).get("job_archetypes", {})` and fail soft when absent.

---

## Chunk 1: Scoring Boundary Hardening

### Task 1: Add schema and DB persistence surfaces for Phase 1

**Files:**
- Modify: `job_finder/web/db_migrate.py`
- Modify: `job_finder/db.py`
- Test: `tests/test_migration.py`
- Test: `tests/test_db.py`

- [ ] **Step 1: Add failing migration tests**

Add tests for:
- `jobs.expiry_status`
- `jobs.eval_blocks`
- `jobs.job_archetype`

Use the existing style in `tests/test_migration.py` near the Migration 14 tests.

- [ ] **Step 2: Add failing DB persistence tests**

Extend `tests/test_db.py` with tests that assert:
- `persist_sonnet_score(..., eval_blocks=...)` writes `eval_blocks`
- `persist_job_expiry_state(...)` writes `expiry_status` and `expiry_checked_at`
- `persist_job_archetype(...)` writes `job_archetype`

- [ ] **Step 3: Add migrations in `db_migrate.py`**

Add one new migration entry that:
- adds `expiry_status TEXT DEFAULT NULL`
- adds `eval_blocks TEXT DEFAULT NULL`
- adds `job_archetype TEXT DEFAULT NULL`

Keep to the project’s discrete-string migration pattern.

- [ ] **Step 4: Expand DB helpers in `db.py`**

Update:
- `JOBS_ALL_COLUMNS` to include `expiry_status`, `eval_blocks`, `job_archetype`, and `opus_score` (the last is a pre-existing omission — Migration 19 added the column but `JOBS_ALL_COLUMNS` was never updated)
- `persist_sonnet_score()` signature to accept `eval_blocks: str | None = None`
- add `persist_job_expiry_state(conn, dedup_key, expiry_status, checked_at)`
- add `persist_job_archetype(conn, dedup_key, job_archetype)`

Do not overload `persist_haiku_score()` for archetype persistence.

- [ ] **Step 5: Run targeted tests**

Run:
```powershell
uv run --active pytest -q --tb=short tests/test_migration.py tests/test_db.py
```

Expected: new schema/persistence tests pass.

- [ ] **Step 6: Commit**

```bash
git add job_finder/web/db_migrate.py job_finder/db.py tests/test_migration.py tests/test_db.py
git commit -m "feat: add persistence surfaces for career-ops scoring metadata"
```

### Task 2: Implement Sonnet liveness preflight in the orchestration layer

**Files:**
- Modify: `job_finder/web/expiry_checker.py`
- Modify: `job_finder/web/scoring_orchestrator.py`
- Test: `tests/test_expiry_checker.py`
- Test: `tests/test_scoring.py`

- [ ] **Step 1: Add failing tests for lightweight liveness helpers**

Extend `tests/test_expiry_checker.py` with focused tests for:
- 404 => `expired`
- 200 => `live`
- timeout/network error => `inconclusive`
- body markers such as `position filled` => `expired`

- [ ] **Step 2: Add failing orchestrator tests**

Extend `tests/test_scoring.py` so `score_and_persist_sonnet()`:
- skips evaluator call when preflight returns `expired`
- persists `expiry_status='expired'`
- still runs evaluator on `live` and `inconclusive`

- [ ] **Step 3: Implement per-job helper(s) in `expiry_checker.py`**

Add a small helper set, for example:
- `quick_liveness_check(url: str, timeout: int = 8) -> str`
- `check_job_liveness(job_row: dict) -> str`

These helpers must remain independent from the nightly ATS-specific expiry cascade.

**Throughput note:** The 8-second timeout applies per-job inside `score_and_persist_sonnet()`. In batch scoring, this is sequential. To bound worst-case batch impact:
- Cache liveness results by URL for the duration of a batch run (avoids re-checking the same employer domain)
- Treat `inconclusive` (timeout/network error) the same as `live` — proceed with Sonnet, don't block on flaky networks
- Log but do not retry failed checks; the nightly cascade handles thorough re-verification

- [ ] **Step 4: Implement orchestration preflight**

In `job_finder/web/scoring_orchestrator.py`:
- add `_preflight_sonnet_job(...)`
- call it inside `score_and_persist_sonnet()` before invoking the evaluator
- persist expiry state through `persist_job_expiry_state()`
- return `None` if expired so existing callers inherit the skip automatically
- accept an optional `liveness_cache: dict | None` parameter on `score_and_persist_sonnet()` — when provided, skip the HTTP check for URLs already resolved in this batch run

- [ ] **Step 5: Migrate nightly runner to use `persist_job_expiry_state()`**

In `job_finder/web/expiry_checker.py`:
- replace the direct `UPDATE jobs SET expiry_checked_at = ?` batch write (line ~496) with calls to `persist_job_expiry_state()`
- import `persist_job_expiry_state` from `job_finder.db`
- the nightly runner should pass the cascade result (`expired`/`live`/`inconclusive`) as `expiry_status` alongside the timestamp
- this eliminates the dual write path and makes `persist_job_expiry_state()` the sole writer of expiry columns

- [ ] **Step 6: Run targeted tests**

Run:
```powershell
uv run --active pytest -q --tb=short tests/test_expiry_checker.py tests/test_scoring.py -k "expiry or sonnet"
```

Expected: no caller-specific liveness edits are required. Nightly runner tests should still pass with the refactored write path.

- [ ] **Step 7: Commit**

```bash
git add job_finder/web/expiry_checker.py job_finder/web/scoring_orchestrator.py tests/test_expiry_checker.py tests/test_scoring.py
git commit -m "feat: gate Sonnet evaluation with centralized liveness preflight"
```

### Task 3: Implement eval blocks with temporary calibration bypass

**Files:**
- Modify: `job_finder/web/sonnet_evaluator.py`
- Modify: `job_finder/web/scoring_orchestrator.py`
- Test: `tests/test_sonnet_evaluator.py`
- Test: `tests/test_scoring.py`
- Test: `tests/test_db.py`

- [ ] **Step 1: Add failing schema and prompt tests**

In `tests/test_sonnet_evaluator.py`, add tests that verify:
- `SONNET_SCHEMA` accepts `eval_blocks`
- responses without `eval_blocks` remain valid
- prompt builder includes eval-block instructions

- [ ] **Step 2: Add failing orchestrator tests for calibration bypass**

In `tests/test_scoring.py`, add tests that verify:
- if result has `eval_blocks`, `calibrate_score()` is not applied
- if result lacks `eval_blocks`, current calibration behavior still applies

- [ ] **Step 3: Refactor prompt assembly in `sonnet_evaluator.py`**

Add a helper such as:
- `_build_sonnet_system_prompt(job_archetype: str | None = None) -> str`

Use it in `evaluate_job_sonnet()` instead of hardcoding `_SYSTEM_PROMPT` directly.

- [ ] **Step 4: Extend evaluator result contract**

Update `SONNET_SCHEMA` and result handling for:
- `eval_blocks`

Keep it optional.

- [ ] **Step 5: Implement data-driven calibration bypass**

In `score_and_persist_sonnet()`:
- extract `eval_blocks`
- if `eval_blocks` is present, skip `has_calibration()` / `calibrate_score()`
- persist `eval_blocks` JSON via `persist_sonnet_score()`

Do not introduce a config flag in this phase.

This bypass is scoped to the Sonnet persistence boundary only. Do not change the generic behavior of `job_finder/web/score_calibration.py` in this phase unless a tiny helper extraction is required for testing.

- [ ] **Step 6: Run targeted tests**

Run:
```powershell
uv run --active pytest -q --tb=short tests/test_sonnet_evaluator.py tests/test_scoring.py tests/test_db.py
```

Expected: mixed old/new Sonnet output remains supported.

- [ ] **Step 7: Commit**

```bash
git add job_finder/web/sonnet_evaluator.py job_finder/web/scoring_orchestrator.py tests/test_sonnet_evaluator.py tests/test_scoring.py tests/test_db.py
git commit -m "feat: add Sonnet eval blocks with temporary calibration bypass"
```

### Task 4: Add Phase 1 UI updates

**Files:**
- Modify: `job_finder/web/templates/jobs/_row_expanded.html`
- Test: `tests/test_views.py` or template-oriented route tests in `tests/test_scoring.py`

- [ ] **Step 1: Add failing UI assertions**

Add coverage for expanded job rows showing:
- expired badge when `expiry_status == 'expired'`
- low-score warning when `COALESCE(sonnet_score, haiku_score, score) < 40`
- eval blocks only when present

- [ ] **Step 2: Update `_row_expanded.html`**

Implement:
- expiry badge near stale metadata
- `effective_score` local variable using Sonnet > Haiku > heuristic precedence
- warning banner below the error banner
- eval-block display that gracefully skips when `job.eval_blocks` is empty/null

- [ ] **Step 3: Run targeted tests**

Run:
```powershell
uv run --active pytest -q --tb=short tests/test_views.py tests/test_scoring.py -k "expand or score"
```

Expected: expanded rows render for old and new Sonnet rows.

- [ ] **Step 4: Commit**

```bash
git add job_finder/web/templates/jobs/_row_expanded.html tests/test_views.py tests/test_scoring.py
git commit -m "feat: surface expiry, eval blocks, and low-score warnings in job detail rows"
```

---

## Chunk 2: Archetypes and Interview Story Reuse

### Task 5: Add deterministic job archetype classification

**Files:**
- Create: `job_finder/web/archetype_classifier.py`
- Modify: `config.example.yaml`
- Modify: `job_finder/web/scoring_orchestrator.py`
- Modify: `job_finder/web/sonnet_evaluator.py`
- Modify: `job_finder/web/templates/jobs/_row.html`
- Modify: `job_finder/web/templates/jobs/_row_expanded.html`
- Test: `tests/test_archetype_classifier.py`
- Test: `tests/test_scoring.py`

- [ ] **Step 1: Write failing classifier tests**

Create `tests/test_archetype_classifier.py` covering:
- keyword match => expected archetype
- no match => `None`
- missing config section => `None`
- missing weight overrides => `{}`

- [ ] **Step 2: Implement `archetype_classifier.py`**

Add small pure helpers:
- `classify_job_archetype(title: str, description: str, config: dict) -> str | None`
- `get_job_archetype_weights(job_archetype: str | None, config: dict) -> dict`

Return soft defaults, never raise on malformed/missing config.

- [ ] **Step 3: Add orchestrator tests for archetype persistence and prompt threading**

Extend `tests/test_scoring.py` so a classified archetype:
- is persisted via `persist_job_archetype()`
- is included in the Sonnet prompt context

- [ ] **Step 4: Integrate classifier into scoring flow**

**Timing:** Classification runs inside `score_and_persist_sonnet()`, after the liveness preflight (Task 2) but before invoking the evaluator. This means:
- Classification only runs for jobs that pass the liveness check (no wasted work on expired postings)
- The archetype is available to influence Sonnet prompt construction
- Classification uses `job_row["title"]` and `job_row["jd_full"]` (or `job_row["description"]` as fallback) — both are already loaded by callers before entering the orchestrator

In `scoring_orchestrator.py`:
- classify from job title + JD/description context
- persist `job_archetype` via `persist_job_archetype()`
- pass `job_archetype` into `evaluate_job_sonnet()`

In `sonnet_evaluator.py`:
- accept `job_archetype: str | None = None`
- include it in prompt construction

- [ ] **Step 5: Add UI badges**

Update:
- `job_finder/web/templates/jobs/_row.html`
- `job_finder/web/templates/jobs/_row_expanded.html`

Render a compact badge only when `job.job_archetype` is present.

- [ ] **Step 6: Update `config.example.yaml`**

Add example config entries for `profile.job_archetypes` with keywords and optional weight overrides. Keep edits additive only.

- [ ] **Step 7: Run targeted tests**

Run:
```powershell
uv run --active pytest -q --tb=short tests/test_archetype_classifier.py tests/test_scoring.py
```

Expected: archetype classification is deterministic and non-breaking.

- [ ] **Step 8: Commit**

```bash
git add job_finder/web/archetype_classifier.py job_finder/web/scoring_orchestrator.py job_finder/web/sonnet_evaluator.py job_finder/web/templates/jobs/_row.html job_finder/web/templates/jobs/_row_expanded.html config.example.yaml tests/test_archetype_classifier.py tests/test_scoring.py
git commit -m "feat: add deterministic job archetype classification"
```

### Task 6: Reuse interview stories inside `interview_preps`

**Files:**
- Modify: `job_finder/web/db_migrate.py`
- Modify: `job_finder/web/interview_prep.py`
- Test: `tests/test_interview_prep.py`
- Test: `tests/test_migration.py`

- [ ] **Step 1: Add failing schema tests**

Extend `tests/test_migration.py` with coverage for:
- `interview_preps.reusable_stories_json`

- [ ] **Step 2: Add failing behavior tests in `tests/test_interview_prep.py`**

Add tests that verify:
- completed prep generation stores reusable stories JSON
- later prep prompt includes prior reusable stories
- malformed JSON in older rows fails soft

- [ ] **Step 3: Add migration**

Add `ALTER TABLE interview_preps ADD COLUMN reusable_stories_json TEXT DEFAULT NULL`.

- [ ] **Step 4: Implement story extraction and reuse**

In `job_finder/web/interview_prep.py`:
- add a pure-Python helper (no LLM call) to extract reusable stories from completed prep output by reading `predicted_questions` — per Decision #8, this is deterministic JSON filtering
- keep up to 5 distinct entries with non-empty `star_story`
- store only `question`, `star_story`, and `key_points` on the current prep row after successful completion
- load recent `reusable_stories_json` rows and inject them into the prompt context for future prep generation

- [ ] **Step 5: Run targeted tests**

Run:
```powershell
uv run --active pytest -q --tb=short tests/test_interview_prep.py tests/test_migration.py
```

Expected: interview-prep reuse remains row-scoped and no new subsystem is introduced.

- [ ] **Step 6: Commit**

```bash
git add job_finder/web/db_migrate.py job_finder/web/interview_prep.py tests/test_interview_prep.py tests/test_migration.py
git commit -m "feat: reuse interview stories within interview prep lifecycle"
```

---

## Chunk 3: Company Research Lifecycle

### Task 7: Add company-research schema and service layer

**Files:**
- Modify: `job_finder/web/db_migrate.py`
- Create: `job_finder/web/company_research.py`
- Test: `tests/test_migration.py`
- Test: `tests/test_company_research.py`

- [ ] **Step 1: Add failing schema tests**

Add tests for a new `company_research` table with at least:
- `id`
- `company_id`
- `status`
- `research_json`
- `error_msg`
- `requested_at` — when the research request was initiated (used for timeout detection)
- `completed_at` — when research finished or errored (NULL while pending/generating)
- `cost_usd`

- [ ] **Step 2: Add failing service tests**

Add tests for:
- cache hit within TTL
- missing cache => background path used
- failed research => status becomes `error`

- [ ] **Step 3: Add migration**

Create the table and index on `company_id`.

- [ ] **Step 4: Implement `company_research.py`**

Add helpers such as:
- `get_cached_company_research(...)`
- `start_company_research(...)`
- `run_company_research_background(...)`

Use `call_model('haiku', ...)` for synthesis. Keep all DB access on explicit connections.

- [ ] **Step 5: Run targeted tests**

Run:
```powershell
uv run --active pytest -q --tb=short tests/test_migration.py tests/test_company_research.py
```

Expected: schema and service behavior are stable before route work starts.

- [ ] **Step 6: Commit**

```bash
git add job_finder/web/db_migrate.py job_finder/web/company_research.py tests/test_migration.py tests/test_company_research.py
git commit -m "feat: add company research storage and service layer"
```

### Task 8: Add company research routes, polling, and UI

**Files:**
- Modify: `job_finder/web/blueprints/companies.py`
- Modify: `job_finder/web/templates/jobs/_row_expanded.html`
- Modify: `job_finder/web/templates/companies/_row_expanded.html`
- Create: `job_finder/web/templates/companies/_research_section.html`
- Create: `job_finder/web/templates/companies/_research_generating.html`
- Test: `tests/test_companies.py`

- [ ] **Step 1: Add failing route tests**

Extend `tests/test_companies.py` with coverage for:
- `POST /companies/<id>/research` creates a pending/generating row
- `GET /companies/<id>/research/status/<id>` returns generating fragment
- stale generating rows where `requested_at` is older than 10 minutes are marked `error`
- done rows return final section and stop polling

- [ ] **Step 2: Implement company research routes in `companies.py`**

Add routes to the existing `companies_bp` with:
- `POST /companies/<int:company_id>/research`
- `GET /companies/<int:company_id>/research/status/<int:research_id>`

- [ ] **Step 3: Implement timeout safety net**

Copy the resume-status pattern:
- if `status in ('pending', 'generating')` and `requested_at` is older than 10 minutes
- mark row `error`
- return terminal error fragment

- [ ] **Step 4: Wire service helpers and templates**

Import the new service helpers into `companies.py` and keep all research routes alongside the existing company route set.

Update templates:
- add `Research Company` button in `jobs/_row_expanded.html` when `company_id` exists
- render cached/final section in `companies/_row_expanded.html`
- create `_research_generating.html` with polling that stops on terminal states

- [ ] **Step 5: Run targeted tests**

Run:
```powershell
uv run --active pytest -q --tb=short tests/test_companies.py
```

Expected: route ownership and timeout behavior are covered.

- [ ] **Step 6: Commit**

```bash
git add job_finder/web/blueprints/companies.py job_finder/web/templates/jobs/_row_expanded.html job_finder/web/templates/companies/_row_expanded.html job_finder/web/templates/companies/_research_section.html job_finder/web/templates/companies/_research_generating.html tests/test_companies.py
git commit -m "feat: add on-demand company research workflow"
```

---

## Final Verification

### Task 9: Full-suite verification and documentation sync

**Files:**
- Modify as needed: any failing tests surfaced by full-suite verification
- Verify: `.planning/career-ops-adoption-plan.md`

- [ ] **Step 1: Run the full suite**

Run:
```powershell
uv run --active pytest -q --tb=short
```

Expected: full suite passes.

- [ ] **Step 2: Run focused regression commands for changed areas**

Run:
```powershell
uv run --active pytest -q --tb=short tests/test_scoring.py tests/test_sonnet_evaluator.py tests/test_db.py tests/test_migration.py tests/test_expiry_checker.py tests/test_interview_prep.py tests/test_companies.py
```

Expected: all changed subsystems remain stable in isolation.

- [ ] **Step 3: Verify documentation still matches implementation**

Confirm that `.planning/career-ops-adoption-plan.md` still matches the shipped implementation decisions:
- `job_archetype` naming
- row-scoped interview story reuse
- eval-block calibration bypass
- company-research async lifecycle
- PDF export still deferred

- [ ] **Step 4: Final commit**

```bash
git add .
git commit -m "feat: implement architecture-safe career-ops adoption work"
```

---

## Out of Scope

Do not implement in this plan:

1. standalone STAR-bank pages or CRUD routes
2. ATS-friendly PDF export
3. retroactive recalibration of historical eval-block scores
4. `expiry_reason` persistence
5. any direct Anthropic-only side paths that bypass `call_model()` for new Haiku-class work

---

Plan complete and saved to `docs/superpowers/plans/2026-04-09-career-ops-adoption.md`. Ready to execute?