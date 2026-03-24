# Architectural Review

**Generated:** 2026-03-24T00:55:56Z
**Scope:** full (210 files)
**Agents:** complexity, language, api, robustness, flow, design

## Executive Summary

This review covers the full job-cannon codebase: a single-user Flask application that aggregates job listings from Gmail alerts and SerpAPI, scores them with a two-tier Claude AI pipeline (Haiku fast filter, Sonnet deep evaluation), and manages an application pipeline with status tracking and desktop notifications. The analysis spans 73 Python source files, 37 Jinja2/HTML templates, and supporting configuration and test infrastructure across 210 files total. Six independent analysis dimensions contributed findings: complexity, language idioms, API design, robustness, data flow, and architectural design.

The codebase is architecturally sound for its stated purpose. The layered design (ingestion, scoring, persistence, web) is well-separated, the blueprint-per-feature pattern aligns with Flask conventions, and the scoring pipeline demonstrates mature type design with the ScoringResult discriminated union. Thread safety for background jobs is consistently handled via dedicated SQLite connections, and the per-source error isolation in the ingestion pipeline is a genuine strength. However, the codebase has accumulated meaningful structural debt in one dominant area: **incomplete centralization of the scoring workflow**. The scoring_orchestrator module was created to consolidate Haiku/Sonnet scoring logic, but pipeline_runner -- the highest-volume scoring consumer -- was never migrated, leaving two parallel codepaths for the same operation. This single root cause generates cascading symptoms across profile loading, score persistence, borderline re-evaluation, and ScoringResult unwrapping.

The three highest-priority findings are: (1) **F005 -- Pipeline runner duplicates scoring orchestrator** (high): the most impactful structural debt, identified by all six agents, affecting 6+ files and creating maintenance risk across the entire scoring pipeline; (2) **F001 -- Empty config.yaml causes cryptic crash** (critical): a documented failure mode where config.yaml has been accidentally wiped 3 times, yet the loader lacks a None guard; (3) **F002 -- call_claude assumes non-empty API response content** (critical): an IndexError on empty response.content would lose cost data and produce confusing diagnostics. Across all dimensions, the review identified 4 critical, 6 high, 14 medium, and 7 low severity findings (31 total after deduplication from 64 raw agent findings).

## System Understanding

The system is invoked via `run.py`, which calls `load_config()` to parse `config.yaml` and then `create_app()` in `job_finder/web/__init__.py` (line 70) -- the Flask app factory that configures the database, runs schema migrations via `db_migrate.run_migrations()`, registers 10 blueprints (jobs, dashboard, pipeline, profile, settings, detections, and 4 others), sets up Jinja2 custom filters (`from_json`, `urlencode`, `format_description`, `relative_date`), and starts the APScheduler background scheduler via `init_scheduler()` in `scheduler.py`. The scheduler initializes 9 periodic jobs including ingestion (30min), stale detection (nightly), pipeline detection (30min), ATS scanning, drive feedback polling, rejection analysis, expiry checking, and preference consolidation. Two additional CLI entry points exist: `gmail_auth.py` for OAuth setup and `scoring_evaluator.py` for offline scoring evaluation with Opus-generated review reports.

The primary data flow is the Gmail ingestion pipeline: `GmailSource.fetch_jobs()` in `gmail_source.py` authenticates via OAuth and queries the Gmail API, routing emails to platform-specific parsers (linkedin, glassdoor, indeed, ziprecruiter) via the `SENDER_PARSERS` dispatch table. Each parser extracts title, company, location, salary, and URL into `Job` dataclass instances (defined in `models.py`), where the `dedup_key` property computes a normalized `company|title` key via `dedup_normalizer.normalized_dedup_key()`. The `JobScorer` in `scoring/scorer.py` computes a heuristic 0-100 score via weighted fuzzy matching, then `db.upsert_job()` merges sources, URLs, and descriptions before persisting to SQLite. The two-tier AI scoring follows: `haiku_scorer.score_job_haiku()` runs the Haiku fast-filter with budget gating via `claude_client.cost_gate()`, and jobs above the borderline threshold proceed to `sonnet_evaluator.evaluate_job_sonnet()` for deep evaluation with full JD analysis. A secondary scoring path exists for user-initiated rescoring: the paste-jd and rescore routes in the jobs blueprint flow through `scoring_orchestrator.score_and_persist_sonnet()`, while the dashboard batch scoring uses `scoring_orchestrator.score_and_persist_haiku()` -- but the primary ingestion path in `pipeline_runner.py` bypasses the orchestrator entirely with inline SQL persistence.

The persistence layer in `db.py` (747 lines) provides job CRUD, pipeline state management, filtered queries, dashboard aggregations, detection resolution, and activity queries. Pipeline status transitions flow through `update_pipeline_status()` which validates against `VALID_PIPELINE_STATUSES` (a frozenset) and records events to the `pipeline_events` table. The pipeline detection subsystem in `pipeline_detector.py` processes rejection, interview, and confirmation emails using multi-signal confidence scoring (company match, title match, timing, ATS domain) against active jobs, auto-updating pipeline status when confidence exceeds the threshold. The config dict loaded from `config.yaml` is threaded as a parameter through 5-7 function calls from scheduler closures down to `call_claude()`, with each intermediate function extracting specific config keys. Three module-level mutable state variables manage cross-run state: `_scheduler` (with proper locking), `_NOTIFY_SEEN` (with `_NOTIFY_LOCK`), and `_last_budget_pct_notified` (without thread safety).

Module cohesion is generally high across the codebase. The scoring pipeline (`claude_client.py`, `haiku_scorer.py`, `sonnet_evaluator.py`, `scoring_orchestrator.py`, `scoring_types.py`) is well-decomposed with proper type discrimination. The parser package provides clean per-source separation, though it lacks shared infrastructure for the common patterns (meta-email detection, salary extraction) that are duplicated across all four parsers. The `db.py` module has medium cohesion -- while internally well-organized, it serves five distinct consumer groups (jobs CRUD, pipeline state, dashboard stats, detections, activity) and has grown into a multi-concern catch-all. The `db_migrate.py` module mixes schema migration with one-time operational backfills that spawn background threads and call external APIs, conflating structural correctness with data quality enrichment.

## Findings

### Critical

#### F001: Empty config.yaml causes cryptic crash on startup

**Severity:** critical | **Blast Radius:** low | **Effort:** trivial
**Dimensions:** robustness
**Files:** `job_finder/config.py:114`

The `load_config()` function at config.py line 114 calls `yaml.safe_load(f)`, which returns `None` when `config.yaml` is empty or contains only whitespace. This `None` value flows directly to `validate_required_sections(cfg)` at line 116, which calls `cfg.get('profile', {})` -- but `cfg` is `None`, causing `AttributeError: 'NoneType' object has no attribute 'get'`. The error message is cryptic and unhelpful. This is a realistic failure mode: CLAUDE.md explicitly documents that config.yaml has been "accidentally wiped 3 times by full-file rewrites." A single-line guard after `yaml.safe_load` would catch this documented failure with a clear diagnostic.

**Suggestion:** Add a None check after yaml.safe_load: `if cfg is None: raise ValueError("Config file is empty or contains only comments: " + config_path)`. This catches the documented accidental-wipe failure mode with a clear error message at the earliest possible point.

---

#### F002: call_claude assumes response.content[0] exists without bounds check

**Severity:** critical | **Blast Radius:** medium | **Effort:** small
**Dimensions:** robustness
**Files:** `job_finder/web/claude_client.py:366`

In `call_claude()` at line 366, `response.content[0]` is accessed without checking that the content list is non-empty. The Anthropic API can return an empty content list in edge cases (rate limiting responses, overloaded_error responses that still return a Message object). An IndexError here would propagate up through the scoring layer. More critically, `record_cost()` is called at line 377, *after* content parsing at line 366, so an IndexError means the API cost is never recorded despite tokens being consumed -- the cost is lost silently.

**Suggestion:** Add a guard before accessing content: `if not response.content: raise RuntimeError(f"Claude API returned empty content for model={model}, purpose={purpose}")`. Move `record_cost()` to execute *before* content parsing (after token count extraction at lines 362-364) to ensure costs are always tracked even when parsing fails.

---

#### F003: resolve_detection does not validate resolution value against allowed set

**Severity:** critical | **Blast Radius:** low | **Effort:** trivial
**Dimensions:** robustness
**Files:** `job_finder/db.py:732`

The `resolve_detection()` function at db.py line 732 accepts a `resolution` parameter written directly to the `pipeline_detections.status` column with no validation. The docstring says it should be `'confirmed'` or `'dismissed'`, but any arbitrary string would be written. This is especially problematic because `get_pending_detections` queries `WHERE status = 'pending'` -- writing an unexpected status value silently removes the detection from the pending queue without proper resolution. This contrasts with `update_pipeline_status` (same file, line 483) which validates against `VALID_PIPELINE_STATUSES`.

**Suggestion:** Add validation at the top of `resolve_detection`: `if resolution not in ('confirmed', 'dismissed'): raise ValueError(f'Invalid resolution: {resolution!r}')`. This mirrors the validation pattern already established by `update_pipeline_status` in the same module.

---

#### F004: call_claude JSON fallback silently produces wrong-shaped result for structured output callers

**Severity:** critical | **Blast Radius:** medium | **Effort:** small
**Dimensions:** robustness
**Files:** `job_finder/web/claude_client.py:373`

In `call_claude()` at lines 371-374, when the API response text cannot be parsed as JSON, the fallback creates `result = {'text': str(text)}`. This result has a fundamentally different structure than the structured output schema the caller requested. Callers like `haiku_scorer.py` access `result.get('score')` which returns `None` from the fallback dict, producing a `ScoringResult` with `status='success'` but containing `{'text': '...'}` instead of the expected score data. The downstream `pipeline_runner.py` then does `result.data.get('score', 0)` which returns 0 -- a valid-looking but incorrect score that gets persisted to the database as a real Haiku score.

**Suggestion:** When `output_schema` is not None and the response is not structured output, raise an explicit error rather than silently falling back: `if output_schema is not None: raise ValueError(f"Expected structured output but got non-JSON response: {text[:100]}")`. The text fallback is only appropriate for unstructured calls where `output_schema` is None.

---

### High

#### F005: Pipeline runner duplicates scoring orchestrator instead of delegating to it

**Severity:** high | **Blast Radius:** high | **Effort:** large
**Dimensions:** complexity, language, api, flow, design
**Files:** `job_finder/web/pipeline_runner.py:509, job_finder/web/pipeline_runner.py:522, job_finder/web/pipeline_runner.py:548, job_finder/web/pipeline_runner.py:665, job_finder/web/scoring_orchestrator.py:1, job_finder/web/scoring_orchestrator.py:63, job_finder/web/scoring_orchestrator.py:149, job_finder/db.py:246, job_finder/db.py:270`

All six analysis agents independently identified this as the dominant structural debt. The `scoring_orchestrator` module was explicitly created to centralize the Haiku/Sonnet scoring workflow (per its docstring and STATE.md: "scoring_orchestrator replaces direct haiku_scorer/sonnet_evaluator calls"). Dashboard batch scoring and jobs blueprint routes correctly delegate to `score_and_persist_haiku` and `score_and_persist_sonnet`. However, `pipeline_runner` -- the highest-volume scoring consumer that runs every 30 minutes via APScheduler -- was never migrated. It contains its own inline implementation: `_run_haiku_scoring` calls `score_job_haiku` directly (line 509), writes haiku_score/haiku_summary with raw SQL UPDATE (lines 522-526), reimplements the borderline re-evaluation band (lines 532-556), and `_run_sonnet_evaluation` writes sonnet scores via raw SQL (lines 665-669). This means the borderline thresholds, persistence logic, and ScoringResult unwrapping exist in two independent codepaths that can drift silently. This is the root cause of several downstream findings including profile loading duplication and score persistence inconsistency.

**Root Cause:** This finding is the root cause of F006, F009, and F010.
**Suggestion:** Refactor `_run_haiku_scoring` to call `scoring_orchestrator.score_and_persist_haiku()` for each job, and `_run_sonnet_evaluation` to call `score_and_persist_sonnet()`. The pipeline_runner retains responsibility for enrichment, exclusion filtering, batch iteration, notification, and Sonnet queue management. This eliminates ~100 lines of duplicate logic and fulfills the orchestrator's stated architectural purpose.

---

#### F006: Profile loading duplicated across four modules with divergent path resolution

**Severity:** high | **Blast Radius:** medium | **Effort:** medium
**Dimensions:** complexity, api, flow, design
**Files:** `job_finder/web/pipeline_runner.py:395, job_finder/web/backfill_enrichment.py:461, job_finder/web/interview_prep.py:127, job_finder/web/scoring_orchestrator.py:39`

Four independent `_load_profile` implementations exist: (1) `pipeline_runner._load_profile` reads `config.scoring.profile_path`, (2) `backfill_enrichment._load_profile` reads `config.profile_path`, (3) `interview_prep._load_profile` hardcodes `'experience_profile.json'`, and (4) `scoring_orchestrator.load_scoring_profile` delegates to `profile_schema.load_profile` with proper validation and fallback to structured `EMPTY_PROFILE`. Each has different path resolution, error handling, and fallback behavior. The scoring_orchestrator version is canonical, but the pipeline_runner version (the highest-volume consumer) does raw `json.load()` without schema validation and returns bare `{}` on failure. This means jobs scored during ingestion may receive a different profile structure than jobs scored via dashboard or manual rescore.

**Symptom of:** See F005.
**Suggestion:** Replace all three private `_load_profile` implementations with calls to `scoring_orchestrator.load_scoring_profile(config)`. For `interview_prep._load_profile`, add a `config` parameter. This consolidates profile loading to a single path with consistent path resolution and schema validation.

---

#### F007: Background job entry points have inconsistent (config, db_path) vs (db_path, config) parameter ordering

**Severity:** high | **Blast Radius:** medium | **Effort:** medium
**Dimensions:** api
**Files:** `job_finder/web/scheduler.py:217, job_finder/web/pipeline_runner.py:61, job_finder/web/stale_detector.py:18, job_finder/web/ats_scanner.py:52`

The `run_*` functions called by the scheduler have contradictory parameter ordering: `run_ingestion(config, db_path)` and `run_pipeline_detection(config, db_path)` place config first, while `run_stale_detection(db_path)`, `run_ats_scan(db_path, config)`, `run_expiry_check(db_path, config)`, `run_rejection_analysis(db_path, config)`, `run_drive_feedback_poll(db_path, config)`, and `run_preference_consolidation(db_path, config)` place db_path first. This inconsistency is not theoretical -- it forced `scheduler.py` to introduce adapter lambdas like `lambda db_path, config: run_pipeline_detection(config, db_path)` to normalize signatures. Every new background job must navigate this minefield.

**Suggestion:** Standardize all background job entry points to `(db_path: str, config: dict)` ordering, which is the majority convention (6 of 8 functions). Update `run_ingestion` and `run_pipeline_detection` signatures, remove the adapter lambdas from `scheduler.py`, and update the direct call sites.

---

#### F008: ats_scanner imports private functions from pipeline_runner creating bidirectional dependency

**Severity:** high | **Blast Radius:** medium | **Effort:** medium
**Dimensions:** language
**Files:** `job_finder/web/ats_scanner.py:36, job_finder/web/pipeline_runner.py:417`

`ats_scanner.py` imports underscore-prefixed private functions `_run_haiku_scoring` and `_run_sonnet_evaluation` from `pipeline_runner.py` (lines 36-39), creating a bidirectional dependency: pipeline_runner imports from ats_scanner (for company population), and ats_scanner imports from pipeline_runner (for scoring). Cross-module import of private functions makes both modules harder to refactor independently and violates the underscore convention that marks these as internal helpers.

**Suggestion:** Extract the scoring entry points into `scoring_orchestrator` (which already exists for this purpose). Have `ats_scanner.py` import from `scoring_orchestrator` instead of `pipeline_runner`. This breaks the bidirectional dependency and respects module boundaries.

---

#### F009: Mixed ScoringResult and legacy dict handling in scoring orchestrator

**Severity:** high | **Blast Radius:** medium | **Effort:** medium
**Dimensions:** robustness, api, design
**Files:** `job_finder/web/scoring_orchestrator.py:103, job_finder/web/scoring_orchestrator.py:186`

`score_and_persist_haiku` and `score_and_persist_sonnet` use `hasattr(scoring_result, 'status')` at lines 103 and 186 to duck-type whether the scorer returned a `ScoringResult` NamedTuple or a plain dict. This duck-typing check means any dict that happens to contain a `'status'` key would be misinterpreted. The same unwrap pattern is duplicated between both functions. Meanwhile, `pipeline_runner.py` checks `result.status` directly (assuming ScoringResult), creating two co-existing calling conventions for the same scoring functions. The dual-type support exists for "legacy/mocked callers" per the docstring, but all production scoring functions now return ScoringResult.

**Symptom of:** See F005.
**Suggestion:** Remove the legacy dict path entirely. Add an `unwrap_scoring_result(result)` helper to `scoring_types.py` that encapsulates the ScoringResult dispatch, then use it in both orchestrator functions. Update any remaining test mocks to return ScoringResult objects. This makes the contract unambiguous and eliminates the duck-typing.

---

#### F010: Scoring evaluator and backfill scripts duplicate score persistence SQL

**Severity:** high | **Blast Radius:** medium | **Effort:** small
**Dimensions:** flow
**Files:** `scoring_evaluator.py:390, scoring_evaluator.py:411, job_finder/web/backfill_enrichment.py:378`

The standalone CLI script `scoring_evaluator.py` writes Haiku and Sonnet scores via inline SQL at lines 390 and 411, and `backfill_enrichment.py` writes Haiku scores at line 378 -- all bypassing `db.persist_haiku_score` and `db.persist_sonnet_score`. This places write-path logic in orchestration scripts rather than delegating to the persistence layer. Changes to the score persistence schema (e.g., adding a timestamp column) must be synchronized across these locations.

**Symptom of:** See F005.
**Suggestion:** Import and use `db.persist_haiku_score` and `db.persist_sonnet_score` from `job_finder.db` instead of inline SQL. These canonical functions already exist for this exact purpose.

---

### Medium

#### F011: Bare except Exception silently swallows errors in dashboard and db queries

**Severity:** medium | **Blast Radius:** medium | **Effort:** small
**Dimensions:** language, robustness, api
**Files:** `job_finder/db.py:369, job_finder/db.py:660, job_finder/db.py:696, job_finder/web/blueprints/dashboard.py:553`

Three functions in `db.py` -- `get_dashboard_stats` (line 369), `get_recent_activity` (line 660), and `get_recent_pipeline_events` (line 696) -- catch bare `except Exception` and return empty defaults (0, [], []). The comments explain this as "graceful pre-migration handling" for when tables don't exist, but the same pattern catches *any* error: SQL bugs, connection failures, type errors. A production SQL error would silently return zero pending detections or an empty activity list, and the dashboard would render normally with no indication of failure. Additionally, `dashboard.py _update_session_counter` at line 553 uses f-string SQL interpolation for a column name without allowlist validation.

**Suggestion:** Catch `sqlite3.OperationalError` specifically (the error type for "no such table") rather than bare `Exception`. For `_update_session_counter`, add an allowlist check: `assert counter in ('scored', 'skipped')`.

---

#### F012: Inconsistent naive vs UTC datetime.now() for database timestamps

**Severity:** medium | **Blast Radius:** high | **Effort:** medium
**Dimensions:** language, complexity
**Files:** `job_finder/db.py:86, job_finder/web/claude_client.py:98, job_finder/web/blueprints/dashboard.py:183`

The codebase mixes `datetime.now()` (naive, local time) and `datetime.now(timezone.utc)` for timestamps stored in the same SQLite database. `db.py` uses naive `datetime.now()` for all job timestamps, while `claude_client.py` uses `datetime.now(timezone.utc)` for cost recording. Since both go into the same database and are compared in SQL queries (e.g., cost_gate compares month_start with scoring_costs.timestamp), this creates a timezone mismatch. The verbose UTC pattern `datetime.now(timezone.utc).replace(tzinfo=None).isoformat()` appears in 15+ locations; `datetime.now().isoformat()` appears in 18+ locations using local time. There is no consistent convention.

**Suggestion:** Create a utility function `def utc_now_iso() -> str: return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()` and standardize all timestamp sites. Audit the `datetime.now().isoformat()` sites to determine whether they should also use UTC.

---

#### F013: Description merge logic duplicated between db.py and dedup_normalizer.py

**Severity:** medium | **Blast Radius:** low | **Effort:** small
**Dimensions:** complexity
**Files:** `job_finder/db.py:33, job_finder/web/dedup_normalizer.py:483`

Two implementations of the same description merge algorithm exist: `db._merge_description` (line 33) used during live `upsert_job` operations, and `dedup_normalizer._merge_descriptions` (line 483) used during retroactive dedup migration. Both implement the same semantic rules (empty check, substring check, append with separator) but with subtly different implementations. The list version starts with the longest description and iterates; the pair version checks substring containment directly. A bug fix in one will not apply to the other.

**Suggestion:** Refactor `_merge_descriptions` in dedup_normalizer to iteratively call `db._merge_description` for each pair, or extract the merge logic into a shared utility function.

---

#### F014: Meta-email detection duplicated identically across all four parsers

**Severity:** medium | **Blast Radius:** medium | **Effort:** medium
**Dimensions:** api, design
**Files:** `job_finder/parsers/linkedin_parser.py:33, job_finder/parsers/glassdoor_parser.py:31, job_finder/parsers/indeed_parser.py:51, job_finder/parsers/ziprecruiter_parser.py:39`

The `_is_meta_email` function and `_META_PATTERNS` list are copy-pasted nearly identically across all four parser modules. Three parsers share exactly the same 4-pattern list; LinkedIn has one additional pattern. The function body is identical in all four. If the meta-email detection logic needs updating (e.g., a new digest format), the change must be replicated in four places. The Indeed parser's divergent pattern list is documented and intentional.

**Suggestion:** Extract shared meta-email detection into `parsers/_common.py`: define `META_PATTERNS` as a base set and `is_meta_email(body, extra_patterns=None)`. LinkedIn passes its extra pattern via the parameter. Indeed keeps its custom version since its divergence is documented.

---

#### F015: Salary parsing logic duplicated across all four parsers

**Severity:** medium | **Blast Radius:** medium | **Effort:** medium
**Dimensions:** design
**Files:** `job_finder/parsers/linkedin_parser.py:153, job_finder/parsers/glassdoor_parser.py:180, job_finder/parsers/indeed_parser.py:497, job_finder/parsers/ziprecruiter_parser.py:330`

Salary extraction functions are implemented independently in each parser with near-identical regex patterns and K-notation conversion logic. All four use the same `$XXK-$XXK` regex and the same `if low < 1000: low *= 1000` conversion. The Indeed parser additionally handles hourly rate conversion, which is a legitimate specialization, but the base range extraction is identical.

**Suggestion:** Extract a shared `parse_salary_range(text)` function into `parsers/_common.py`. The Indeed parser composes this with its hourly rate fallback. This consolidates the regex and K-notation logic into a single tested function.

---

#### F016: db.py imports from web.blueprints creating upward dependency in the layer architecture

**Severity:** medium | **Blast Radius:** low | **Effort:** small
**Dimensions:** design
**Files:** `job_finder/db.py:481, job_finder/web/blueprints/__init__.py:5`

The persistence layer (`db.py`) contains a deferred import of `VALID_PIPELINE_STATUSES` from `job_finder.web.blueprints.__init__` inside `update_pipeline_status` (line 481). This creates a dependency from the foundation layer upward into the web layer, violating the layered architecture. The deferred import avoids circular import errors but means the persistence layer cannot be used independently of the web package.

**Suggestion:** Move `PIPELINE_STATUSES` and `VALID_PIPELINE_STATUSES` to `job_finder/constants.py` (or `config.py`) at the foundation layer. Both `db.py` and `web/blueprints/__init__.py` import from the foundation layer, eliminating the upward dependency.

---

#### F017: db.py serves too many disparate consumers from a single 747-line module

**Severity:** medium | **Blast Radius:** low | **Effort:** large
**Dimensions:** design
**Files:** `job_finder/db.py:1`

`db.py` contains functions spanning five distinct domain concerns: job CRUD and upsert, pipeline state management, dashboard aggregations, detection resolution, and activity/run queries. Each function group serves different blueprints with different query patterns. No single consumer uses more than half the module's exports. While internally well-organized, the module's public surface far exceeds what any caller needs.

**Suggestion:** As complexity grows, consider extracting query groups into sub-modules: `db_jobs.py` (CRUD), `db_pipeline.py` (status management), `db_dashboard.py` (aggregations). Low priority for a single-developer project but becomes important as the persistence layer grows.

---

#### F018: db_migrate.py mixes schema migration with one-time operational backfills

**Severity:** medium | **Blast Radius:** low | **Effort:** medium
**Dimensions:** design
**Files:** `job_finder/web/db_migrate.py:448`

`db_migrate.py` serves two fundamentally different purposes: schema migration (MIGRATIONS list, deterministic based on user_version) and one-time operational backfills (`_run_description_reformat_once`, `_run_data_backfills_once`, `_run_retroactive_dedup_once`) that spawn background threads, create separate SQLite connections, and call external APIs. Mixing them makes it harder to understand the startup sequence.

**Suggestion:** Extract the backfill functions into `web/startup_backfills.py` that `create_app` calls after `run_migrations`. This separates the schema-correctness concern from the data-enrichment concern.

---

#### F019: GmailSource._search_messages has no error handling for pagination failures

**Severity:** medium | **Blast Radius:** low | **Effort:** small
**Dimensions:** robustness
**Files:** `job_finder/sources/gmail_source.py:170`

The `while True` pagination loop calls `self.service.users().messages().list().execute()` without error handling. A transient Gmail API error (500, 503, rate limit) on the second or subsequent page loses all results from previous pages. The project's per-email isolation principle suggests partial results are better than no results.

**Suggestion:** Wrap the individual API call in a try/except that breaks the loop on error but returns the messages collected so far, plus log a warning.

---

#### F020: _mark_session_error opens connection without try/finally cleanup

**Severity:** medium | **Blast Radius:** low | **Effort:** trivial
**Dimensions:** complexity
**Files:** `job_finder/web/blueprints/dashboard.py:622`

The `_mark_session_error` helper opens a sqlite3 connection with sequential `conn.execute()`, `conn.commit()`, `conn.close()` calls. If execute or commit raises, close is never called. On Windows, unclosed SQLite connections hold file locks. Other connection sites in the same file correctly use try/finally.

**Suggestion:** Wrap in try/finally: `conn = sqlite3.connect(db_path); try: conn.execute(...); conn.commit() finally: conn.close()`.

---

#### F021: SELECT * usage contradicts project's own documented convention (30+ queries)

**Severity:** medium | **Blast Radius:** high | **Effort:** large
**Dimensions:** language
**Files:** `job_finder/web/pipeline_runner.py:458, job_finder/web/blueprints/dashboard.py:435, job_finder/web/blueprints/companies.py:1, job_finder/db.py:662, job_finder/db.py:715`

`db.py` line 13 documents: "Explicit column lists for high-traffic queries. Avoids SELECT * so that schema changes don't silently alter what callers receive." Despite this, SELECT * appears in 30+ queries across the codebase. `db.py` even provides a `_JOBS_ALL_COLUMNS` constant for this purpose that is underutilized.

**Suggestion:** Replace SELECT * with explicit column lists in high-traffic queries. Use the existing `_JOBS_ALL_COLUMNS` constant or define query-specific column lists when only a subset is needed.

---

#### F022: call_claude takes 11 parameters spanning four distinct concerns

**Severity:** medium | **Blast Radius:** medium | **Effort:** medium
**Dimensions:** api
**Files:** `job_finder/web/claude_client.py:292`

`call_claude()` takes 11 parameters spanning API setup, cost recording, budget gating, and structured output. Every caller must assemble the same (client, conn, config) triple. Callers like `resume_generator.py` call it 4 times, each with slightly different purpose strings but the same invariant triple.

**Suggestion:** Consider a `ClaudeContext` dataclass bundling the invariant triple (client, conn, config). This reduces `call_claude` to 8 parameters and eliminates repeated threading at every call site.

---

#### F023: Job dataclass allows empty title/company producing degenerate dedup_key

**Severity:** medium | **Blast Radius:** low | **Effort:** small
**Dimensions:** robustness
**Files:** `job_finder/models.py:9`

The `Job` dataclass has `title: str` and `company: str` as required fields but accepts empty strings. If both are empty, the `dedup_key` would be a degenerate value causing all such jobs to collide and overwrite each other. Parsers check for empty titles individually, but there is no enforcement at the dataclass boundary.

**Suggestion:** Add `__post_init__` validation: `if not title.strip() or not company.strip(): raise ValueError('Job requires non-empty title and company')`.

---

#### F024: Module-level _last_budget_pct_notified lacks thread safety

**Severity:** medium | **Blast Radius:** low | **Effort:** small
**Dimensions:** flow
**Files:** `job_finder/web/pipeline_runner.py:58`

The `_last_budget_pct_notified` float at pipeline_runner.py:58 is read and written in `_check_budget_alert` without thread safety. This function is called from both the APScheduler background thread and the Flask request thread (via trigger_sync). Unlike `_NOTIFY_SEEN` (which uses `_NOTIFY_LOCK`), this variable has no guard. A race could cause duplicate budget notifications.

**Suggestion:** Add a `threading.Lock` guard around the read-modify-write section, following the same pattern as `notifier.py`'s `_NOTIFY_LOCK`.

---

### Low

#### F025: run.py loads config twice -- once at module level and once in create_app

**Severity:** low | **Blast Radius:** low | **Effort:** trivial
**Dimensions:** robustness, design
**Files:** `run.py:6, job_finder/web/__init__.py:89`

`run.py` calls `load_config()` at module level (line 6) and then `create_app()` which loads config again internally. The server-level config (host, port, debug) and app-level config come from different parse results. `create_app` already accepts a `config=` parameter for test isolation.

**Suggestion:** Pass the config: `cfg = load_config(); app = create_app(config=cfg)`. Eliminates the double-load and ensures consistency.

---

#### F026: Mixed Optional[T] and T | None syntax across 20+ modules

**Severity:** low | **Blast Radius:** high | **Effort:** medium
**Dimensions:** language
**Files:** `job_finder/models.py:5, job_finder/db.py:1`

The project targets Python 3.13 which supports `X | None` natively. Some modules already use the modern syntax, but 20 modules still import and use `Optional[T]`. This creates inconsistent style.

**Suggestion:** Gradually migrate `Optional[T]` to `T | None` as files are touched.

---

#### F027: Redundant imports (sqlite3, datetime alias, logging inside functions)

**Severity:** low | **Blast Radius:** low | **Effort:** trivial
**Dimensions:** complexity, language
**Files:** `job_finder/web/pipeline_runner.py:106, job_finder/sources/gmail_source.py:11, job_finder/web/db_migrate.py:460, job_finder/web/haiku_scorer.py:81`

Several import hygiene issues: (1) pipeline_runner imports sqlite3 at module level and again inside `run_ingestion`, (2) gmail_source imports datetime twice with a redundant `_dt` alias, (3) db_migrate re-imports logging and creates loggers inside function bodies, (4) haiku_scorer imports `json as _json` inside a function body with an unnecessary alias.

**Suggestion:** Remove redundant in-function imports; use module-level imports consistently.

---

#### F028: normalize_location defined but never called anywhere

**Severity:** low | **Blast Radius:** low | **Effort:** trivial
**Dimensions:** complexity
**Files:** `job_finder/web/dedup_normalizer.py:219`

The `normalize_location` function and its 21-entry `_LOCATION_CANONICAL` dict are defined but have zero call sites. The docstring notes "NOT used in dedup_key."

**Suggestion:** Remove `normalize_location()` and `_LOCATION_CANONICAL`. ~30 lines of dead code.

---

#### F029: purge_jobs.py is a one-shot migration script hardcoded to a past event

**Severity:** low | **Blast Radius:** low | **Effort:** trivial
**Dimensions:** complexity
**Files:** `purge_jobs.py:23`

A 374-line standalone script hardcoded to a specific bulk-load timestamp from March 2026. Once executed, it always reports "No spike found." It has no future utility.

**Suggestion:** Archive or remove. Future purge scripts should be parameterized.

---

#### F030: Heuristic JobScorer persists in ingestion despite AI scoring superseding it

**Severity:** low | **Blast Radius:** low | **Effort:** medium
**Dimensions:** design
**Files:** `job_finder/scoring/scorer.py:15`

The heuristic `JobScorer` runs during every ingestion, but its scores are superseded by Haiku scoring that runs immediately after. The job board sorts by `COALESCE(sonnet_score, haiku_score, score)`, so the heuristic score only surfaces during the brief interval between `upsert_job` and Haiku completion.

**Suggestion:** Evaluate whether the heuristic scorer adds value proportional to its maintenance cost. If kept for offline/API-unavailable scenarios, document this explicitly as the rationale.

---

#### F031: Form elements missing associated labels in filter bar

**Severity:** low | **Blast Radius:** low | **Effort:** small
**Dimensions:** language
**Files:** `job_finder/web/templates/jobs/index.html:46`

Multiple input and select elements in the jobs filter bar lack associated `<label>` elements, reducing accessibility for screen readers. The select elements rely on placeholder text or first-option as visual labels.

**Suggestion:** Add visually-hidden labels using Tailwind's `sr-only` class to preserve the compact visual design while providing screen reader accessibility.

---

## Positive Observations

- **Well-designed ScoringResult discriminated union**: The `ScoringResult` NamedTuple in `scoring_types.py` with a `Literal` status field cleanly separates success from budget_exceeded/error/skipped outcomes, providing a pattern the rest of the codebase should emulate for pipeline status values.

- **Consistent per-source error isolation in ingestion**: The pipeline_runner catches failures per-source (Gmail, SerpAPI) and per-job independently, ensuring one malformed email or API error does not abort the entire ingestion run. This "graceful degradation" pattern is consistently applied and well-documented.

- **Thread-safe background job connection pattern**: Background jobs (stale_detector, pipeline_detector, batch scoring) consistently create their own SQLite connections rather than sharing Flask's `g.db`, correctly handling the cross-thread safety requirements of APScheduler.

- **Effective parser dispatch architecture**: The `SENDER_PARSERS` dispatch table in `gmail_source.py` elegantly routes emails to platform-specific parsers based on sender address, with a uniform `parse_*_alert(body, email_date) -> list[Job]` contract that makes adding new parsers straightforward.

- **Robust pipeline status validation**: `update_pipeline_status` validates against `VALID_PIPELINE_STATUSES` (a frozenset) before any write, and the status dropdown uses `PIPELINE_STATUSES` tuple for consistent ordering. Sort column names are validated against an explicit allowlist before SQL interpolation.

- **Clean HTMX integration patterns**: The blueprint routes consistently check `HX-Request` headers and return full-page fallbacks for direct browser access, the accordion expand/collapse pattern is well-implemented, and the use of `hx-target=this hx-swap=outerHTML` for the status dropdown is clean.

- **Comprehensive cost tracking and budget gating**: The `claude_client.py` module provides centralized cost computation, per-call recording to the `scoring_costs` table, and budget gating via `cost_gate()` -- ensuring AI scoring never silently exceeds the configured monthly budget.

- **Zero async correctness findings**: Despite having a background scheduler, thread-spawning batch scoring, and multiple SQLite connection patterns, no async/sync boundary issues were identified. The project correctly avoids APScheduler 4.x's async API as documented.

## Recommendations

1. **Complete the scoring orchestrator migration (F005, F006, F009, F010).** This is the single highest-impact change. Refactor `pipeline_runner._run_haiku_scoring` and `_run_sonnet_evaluation` to delegate to `scoring_orchestrator`, replace the three private `_load_profile` implementations with calls to `load_scoring_profile`, update `scoring_evaluator.py` and `backfill_enrichment.py` to use `db.persist_haiku_score/persist_sonnet_score`, and remove the hasattr duck-typing from the orchestrator. This addresses 4 findings, eliminates ~150 lines of duplicate code, and fulfills the documented architectural intent.

2. **Add defensive guards to critical boundary functions (F001, F002, F003, F004).** These are all trivial-to-small fixes with outsized impact: add a None check after yaml.safe_load, add a bounds check before `response.content[0]`, add validation in `resolve_detection`, and prevent the JSON fallback from producing wrong-shaped results. Total effort: ~20 lines of code across 3 files.

3. **Standardize background job parameter ordering (F007).** Adopt the majority convention `(db_path, config)` for all `run_*` functions. This removes the adapter lambdas in scheduler.py and prevents argument-swap bugs as new jobs are added.

4. **Extract shared parser infrastructure (F014, F015).** Create `parsers/_common.py` with `is_meta_email()` and `parse_salary_range()`. This consolidates 8 duplicate function implementations into 2 shared utilities.

5. **Narrow exception handling in db.py and jobs.py (F011).** Replace `except Exception` with `except sqlite3.OperationalError` in the three db.py functions, and add `logger.debug` in the 7 bare `except: pass` blocks in jobs.py. Minimal effort, significant debuggability improvement.

6. **Standardize timestamp handling (F012).** Create `utc_now_iso()` utility and migrate all 33+ timestamp sites to a consistent convention. This prevents timezone-related comparison bugs.

7. **Move pipeline status constants to foundation layer (F016).** Relocate `PIPELINE_STATUSES` and `VALID_PIPELINE_STATUSES` from `web/blueprints/__init__.py` to `constants.py`, eliminating the upward dependency from `db.py` to the web layer.
