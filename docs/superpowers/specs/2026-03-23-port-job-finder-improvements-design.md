# Port job-finder Improvements to job-cannon

**Date:** 2026-03-23
**Status:** Approved
**Goal:** Replicate all improvements made in job-finder into job-cannon (the source of truth going forward), plus implement the outstanding multi-select status filter todo.

## Context

job-cannon was created as a v3.0 snapshot of job-finder. Development continued on job-finder in parallel, producing ~30 improvements (refactors, safety hardening, bugfixes, new features). job-cannon received only 9 small fix commits. This spec covers porting all divergent changes and retiring job-finder afterward.

**Source repo:** `<other-repo>`
**Target repo:** `<repo-root>`

## Strategy

Diff and apply surgically, organized in 5 waves following the dependency graph. Sub-sections within each wave are ordered by dependency (e.g., 2.1 must complete before 2.2). Tests run after each wave to catch breakage early.

## Wave 1: Foundation Types & Constants

New modules and small changes that everything else depends on.

### 1.1 Create `job_finder/json_utils.py`
- Copy from job-finder. Contains `safe_json_load()`.
- Update `web/db_helpers.py` import from `utils` to `json_utils`.

### 1.2 Create `job_finder/web/scoring_types.py`
- Copy from job-finder. Contains `JobRow` TypedDict, `ScoringResult` NamedTuple, `format_salary_range()`.

### 1.3 Update `job_finder/web/blueprints/__init__.py`
- `PIPELINE_STATUSES`: list → tuple.
- Add `VALID_PIPELINE_STATUSES = frozenset(PIPELINE_STATUSES)`.

### 1.4 Update `job_finder/config.py`
- Add `DEFAULT_BORDERLINE_HIGH = 54` constant.

### 1.5 Delete `job_finder/utils.py`
- Superseded by `json_utils.py`. Verify no other imports reference it.

### 1.6 Delete `job_finder/output/__init__.py`
- Empty legacy package, removed in job-finder.

**Conflict notes:** cannon's `utils.py` (created by commit `652a878`) has identical `safe_json_load` content — just a different path. Two imports to update: `db_helpers.py` and `db.py`. (The `db.py` import is handled implicitly by Wave 2.1's full rewrite. `main.py` also imports it but is deleted in Wave 5.5.)

## Wave 2: Core Module Refactors

### 2.1 `job_finder/db.py` (largest change, ~1450 diff lines)
- **JobDB class → module-level functions** taking `conn: sqlite3.Connection` as first arg.
- **Explicit column constants** (`_JOBS_ALL_COLUMNS`, `_UPSERT_MERGE_COLUMNS`) replacing `SELECT *`.
- **Smart description merging** on upsert: substring dedup, keep longer, eager `jd_full` promotion for descriptions >200 chars.
- **Pipeline status validation** against `VALID_PIPELINE_STATUSES` in `update_pipeline_status()`.
- **New query helpers**: `persist_haiku_score()`, `persist_sonnet_score()`, `load_job_context()`, `get_dashboard_stats()`, `get_recent_runs()`, `get_pipeline_summary()`.
- **Enhanced `get_jobs_by_status()`** with `days_in_stage` via pipeline_events subquery.
- **Auto-reopen archived jobs** that re-appear in ingestion.
- **Dead code removal**: `JobDB` class, unused CLI helpers.
- **Multi-select support in `get_filtered_jobs()`**: accept a list of statuses and build `WHERE pipeline_status IN (?, ?, ...)` clause. Single status → identical behavior to today. (Folded here since the file is being rewritten.)

### 2.2 Create `job_finder/web/scoring_orchestrator.py`
- Copy from job-finder (~200 lines). Public API:
  - `score_and_persist_haiku(conn, job_row, config)` — cost gate → client → profile → score → borderline re-eval → persist.
  - `score_and_persist_sonnet(conn, job_row, config)` — cost gate → client → score → persist.
  - `load_scoring_profile(config)` — canonical profile loading.
- Eliminates duplication across pipeline_runner, dashboard batch scoring, and jobs blueprint.

### 2.3 Create `job_finder/web/description_formatter.py`
- Extract `format_description` Jinja2 filter (~150 lines) out of `web/__init__.py`.

### 2.4 Update `job_finder/web/claude_client.py`
- Unknown model pricing → conservative fallback with warning log (no more `KeyError`).
- Add `DEFAULT_API_TIMEOUT_SECONDS = 120` with configurable `timeout` on `call_claude()`.
- `get_cost_stats()` signature: `get_cost_stats(conn, budget_cap: float = 25.0)` — caller can override, default preserves current behavior.

### 2.6 Update `job_finder/web/ats_scanner.py`
- Replace `from job_finder.db import JobDB` / `JobDB(db_path)` with module-level function pattern (`sqlite3.connect(db_path)` + module-level `db.*()` calls). Two callsites (around lines 1075 and 1258).

### 2.5 Update `job_finder/web/__init__.py`
- Remove inline `format_description` filter (import from `description_formatter`).
- Secret key → `secrets.token_hex(32)` instead of hardcoded string.
- Fix `os.sys.modules` → `sys.modules`.

**Dependency notes:** Sub-sections are ordered: 2.1 (db.py) must complete before 2.2 (scoring_orchestrator) since the orchestrator calls `persist_haiku_score()` and `persist_sonnet_score()` from db.py. 2.3 (description_formatter) must complete before 2.5 (app factory update).

**Conflict notes:** `db.py` is the riskiest change. Tests updated in Wave 5 to match new signatures. Cannon-only test files checked for `db.py` imports.

## Wave 3: Consumers (Scorers, Runner, Scheduler)

### 3.1 `job_finder/web/haiku_scorer.py`
- Return type: `dict | None` → `ScoringResult`.
- Profile param: `profile` → `experience_profile` (caller passes resolved section).
- Inline salary formatting → `format_salary_range()` from `scoring_types`.
- `dict` hints → `JobRow`.

### 3.2 `job_finder/web/sonnet_evaluator.py`
- Same pattern: `Optional[dict]` → `ScoringResult`, uses `format_salary_range()` and `JobRow`.

### 3.3 `job_finder/web/pipeline_runner.py`
- Replace inline `_load_profile()` → `scoring_orchestrator.load_scoring_profile()`.
- Replace inline haiku + borderline + persist → `score_and_persist_haiku()`.
- Replace inline sonnet + persist → `score_and_persist_sonnet()`.
- Company enrichment writes `company_size`/`industry` to `companies` table.
- Function arg order normalized.
- Remove import of `JobScorer` from `job_finder.scoring.scorer` (replaced by scoring_orchestrator).

### ~~3.5~~ (Not needed)
`borderline_high = 54` in `pipeline_runner.py` and `dashboard.py` is handled implicitly by the scoring_orchestrator integration in Waves 3.3 and 4.2.

### 3.4 `job_finder/web/scheduler.py`
- Replace ~300 lines of closure boilerplate with `_make_simple_job()` and `_make_tracked_job()` factories.
- Each job registration shrinks to ~10 lines. Same runtime behavior.

**Conflict notes:** Clean replacements — cannon has no unique changes in these files.

## Wave 4: Blueprints + Multi-Select Filter

### 4.1 `job_finder/web/blueprints/jobs.py`
- Scoring orchestrator integration.
- Add `_safe_float()` / `_safe_int()` query-string validators (HTTP 400 on malformed input).
- Re-add `HX-Request` header guards on fragment routes (dismiss, return, expand, collapse, save_jd). Previously added then reverted in cannon — job-finder has them working.
- **Multi-select status filter**: `_get_filter_kwargs()` uses `request.args.getlist("status")` instead of `.get("status")`.

### 4.2 `job_finder/web/blueprints/dashboard.py`
- Scoring orchestrator integration for batch scoring.
- Extract `_update_session_counter()`, `_finish_session()`, `_fail_session()` helpers.
- 30-minute timeout safety net for stuck batch sessions.
- Company enrichment writes to companies table.
- `get_cost_stats()` passes `budget_cap`.
- Fix: `cost_stats` called before template context (was after — line ordering bug).

### 4.3 `job_finder/web/blueprints/profile.py`
- Extract `_load_profile_page_extras()` helper.

### 4.4 `job_finder/web/blueprints/settings.py` (bugfixes)
- `guidelines_path`: `parent.parent.parent` → `parent.parent.parent.parent` in **both** occurrences (`index()` route ~line 92 and `apply_guidelines_merge()` ~line 350).
- Sonnet empty-field protection: preserve existing value when Sonnet returns empty.

### 4.5 `job_finder/web/templates/jobs/index.html` — Multi-select status filter UI
- Replace `<select>` dropdown with clickable status pill/chip toggles.
- Implementation: hidden checkboxes with styled `<label>` elements as pills.
  - Participates natively in `<form>` submission (no JS serialization needed).
  - `hx-include="[name='status']"` serializes multiple same-name checkboxes as `?status=X&status=Y` (standard HTMX 2.x behavior).
  - Update `hx-trigger` to `"change from:input"` (remove dead `change from:select` since the `<select>` is gone).
- Each status styled with existing status colors.
- "All" toggle: small JS function that checks/unchecks all boxes and fires a change event.
- Multiple statuses sent as `?status=X&status=Y` query params.
- Also inherits date filter fix (HTMX `change from:input` already handles clearing).

### ~~4.6~~ (Folded into Wave 2.1)
Multi-select `get_filtered_jobs()` change is now part of the `db.py` rewrite in Wave 2.1.

## Wave 5: Minor/Safety + Tests + Cleanup

### 5.1 `job_finder/sources/gmail_source.py`
- Add `max_messages=500` pagination cap to `_search_messages()`.

### 5.2 `job_finder/web/db_migrate.py`
- Add migration at index 15 (`user_version` 15): add `company_size` and `industry` columns to `companies` table.

### 5.3 `job_finder/models.py`
- Remove dead `to_dict()` method.

### 5.4 Test updates
- Update tests referencing `JobDB` class → module-level `db.py` functions.
- Update tests expecting `dict` returns from scorers → `ScoringResult`.
- Update tests using old `profile` param → `experience_profile`.
- **Preserve** 3 cannon-only test files (`test_docx_formatter.py`, `test_drive_status.py`, `test_drive_uploader.py`) — adjust `db.py` imports if needed.
- Add test for multi-select status filtering.
- Run full test suite — all 266+ tests must pass.

### 5.5 Cleanup
- Delete `job_finder/main.py` (legacy CLI entry point).
- Delete `job_finder/scoring/` package (`__init__.py`, `scorer.py`) — superseded by `scoring_orchestrator.py`. Verify no remaining imports reference it.
- Move cannon's 2 pending todos to `done/` (date filter fixed by port, multi-select implemented).

## Cannon-Only Assets to Preserve

These exist in job-cannon but not job-finder. They must not be lost during the port:

| File | Why |
|------|-----|
| `tests/test_docx_formatter.py` | Test coverage added by cannon review fix |
| `tests/test_drive_status.py` | Test coverage added by cannon review fix |
| `tests/test_drive_uploader.py` | Test coverage added by cannon review fix |

## Verification

After all 5 waves:
1. `uv run pytest tests/` — all existing tests pass, plus new multi-select test
2. Manual smoke test: start app, verify job board loads, status pills render, multi-select filtering works
3. Verify no regressions in scoring, pipeline detection, or resume generation flows

## Post-Port

- job-finder repo is retired (no further development)
- job-cannon becomes the single source of truth
- Update CLAUDE.md if any architectural decisions changed
