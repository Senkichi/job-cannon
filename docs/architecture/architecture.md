# Architecture

This document describes the layered architecture, data flow, and key abstractions of Job Cannon for engineers reading the source. For setup and run instructions, see [docs/SETUP.md](../SETUP.md).

## Pattern Overview

**Overall:** Layered request-response web app with background job pipeline

Job Finder follows a classic 3-tier architecture:
1. **Ingestion Layer** — Data fetching and parsing (Gmail, SerpAPI)
2. **Scoring Layer** — Single-tier ordinal scoring (six-axis rubric) routed through a multi-provider cascade
3. **Web Layer** — Flask blueprints with HTMX-driven interactivity and per-request database access

**Key Characteristics:**
- Single-process Flask development server on localhost:5000 (no ASGI, no WSGI app server)
- Background APScheduler ingestion every 30 minutes (separate SQLite connection)
- Per-request SQLite connections via Flask g.db context variable
- Raw SQL (no ORM) with parameterized queries and composite indexes
- Cascade scoring: every job runs through one `'scoring'` tier with a six-axis ordinal rubric. The cascade tries Ollama (qwen2.5:14b) → Groq → Cerebras → Gemini → Anthropic in order; production typically resolves on the first or second hop at $0 cost
- Budget gating: only the Anthropic paid-fallback path counts against the monthly cap; free-provider calls are never gated
- Deduplication by company+title (location intentionally excluded)

## Layers

**Ingestion Layer:**
- Purpose: Fetch job postings from external sources, parse into normalized Job objects
- Location: `job_finder/sources/` (Gmail, SerpAPI), `job_finder/parsers/` (email format parsing)
- Contains: Source adapters, email format parsers
- Depends on: Job model, Gmail API v1, SerpAPI REST API
- Used by: `pipeline_runner.py` (orchestrator)

**Persistence Layer:**
- Purpose: SQLite database access with deduplication, migrations, cost tracking
- Location: `job_finder/db.py` (JobDB class, CLI-era module-level functions), `job_finder/web/db_migrate.py` (schema + migrations)
- Contains: JobDB class (insert/update/query), migration definitions, per-request connection helpers
- Depends on: sqlite3 standard library, Job model
- Used by: All blueprints, pipeline_runner, background jobs

**Scoring Layer:**
- Purpose: Single-tier ordinal scoring with cascade-routed model dispatch, cost tracking, and budget gating on the Anthropic fallback path
- Location: `job_finder/web/job_scorer.py` (single-tier `score_job()`), `job_finder/web/scoring_orchestrator.py` (per-run orchestration), `job_finder/web/model_provider.py` (cascade dispatcher and tier resolution), `job_finder/web/providers/` (per-provider clients: anthropic, gemini, ollama), `job_finder/web/claude_client.py` (Anthropic-specific cost tracking, budget gating, fallback-of-last-resort path)
- Contains: Six-axis rubric prompt, structured output schema (`JOB_ASSESSMENT_SCHEMA`), provider-cascade dispatch, daily rate-limit tracking
- Depends on: Anthropic SDK + google-genai + httpx (Ollama/Groq/Cerebras over plain HTTP)
- Used by: `pipeline_runner.py` (auto-scoring), Dashboard UI (manual rescore)

**Web/Request Layer:**
- Purpose: HTTP endpoints, form processing, template rendering, user interactions
- Location: `job_finder/web/blueprints/` (jobs, dashboard, pipeline, profile, settings, etc.), `job_finder/web/templates/`
- Contains: Route handlers, HTMX fragment responses, Jinja2 templates
- Depends on: Flask, Jinja2, database connection from context
- Used by: Browser (user)

**Background Tasks:**
- Purpose: Long-running operations outside the request cycle
- Location: `job_finder/web/scheduler.py` (APScheduler init), pipeline_runner, stale_detector
- Contains: Scheduled jobs (3x/day ingestion + enrichment cadence), one-time backfills (description reformat, data enrichment)
- Depends on: APScheduler (background thread), separate DB connections
- Used by: APScheduler event loop

## Data Flow

**Ingestion → Persistence → Scoring (Main Pipeline):**

1. **APScheduler timer fires** (every 30 minutes)
   - Calls `run_ingestion(config, db_path)` in background thread

2. **run_ingestion orchestrates:**
   - Gmail fetch via `GmailSource.fetch_recent()` → emails with sender address
   - Email parsing via `SENDER_PARSERS[sender]()` → list of Job objects per email
   - SerpAPI fetch via `SerpAPISource.search()` → list of Job objects
   - Per-source error isolation (Gmail failure doesn't stop SerpAPI)

3. **Deduplication & Persistence:**
   - For each Job, check exclusion filters (`exclusion_filter.py`)
   - Call `JobDB.upsert_job(job)` → returns True if new, False if updated
   - Merge sources, locations (Remote/Hybrid first), descriptions (keep longer or append)

4. **Cascade Scoring (single tier):**
   - For NEW jobs that have `jd_full` populated (skip if score already present unless force-rescore)
   - Call `score_job(conn, job_row, config, profile)` (single entry point, dispatches via `model_provider.call_model(tier="scoring", ...)`)
   - The cascade resolves in order: Ollama qwen2.5:14b → Groq → Cerebras → Gemini → Anthropic. First provider that returns valid schema-conformant output wins
   - Returns: six-axis ordinal sub-scores + Python-derived classification (`apply | consider | skip | reject`) + structured fit analysis
   - Skipped if `jd_full` is missing (jobs without full JD route to enrichment first)
   - Cost: $0 in the typical case (free-provider hop). Anthropic fallback path costs ~$0.05–$0.15 per job and is gated by `scoring.monthly_budget_usd`
   - Cost recorded to `scoring_costs` table with provider attribution (`scoring_costs.provider`, `scoring_costs.model`)

5. **Summary returned to scheduler:**
   - gmail_fetched, gmail_errors, serpapi_fetched, serpapi_errors
   - jobs_new, jobs_updated, jobs_scored, job_errors
   - scoring_attempted, scoring_succeeded, scoring_skipped (jd_full missing or budget exhausted)

**User Interaction (Request/Response):**

1. User browses `/jobs/` — renders full page with filter bar
2. User applies filter (status, score, salary, source, date range) → GET /jobs/?status=...&min_score=...
3. Fragment routes (HTMX): GET /jobs/_table?filters=... → returns rows only
4. User clicks expand → GET /jobs/{dedup_key}/detail → shows full description + AI analysis
5. User changes status dropdown → POST /jobs/{dedup_key}/status?value=applied → updates DB + logs pipeline event
6. User pastes full JD → POST /jobs/{dedup_key}/paste-jd → stores jd_full + triggers cascade rescoring
7. User rescores manually → POST /jobs/{dedup_key}/rescore → re-runs the `'scoring'` cascade (paid-fallback path budget-gated)

**State Management:**

Job state transitions are tracked in the database:
- `pipeline_status` on jobs table (discovered → reviewing → applied → phone_screen → ... → archived/rejected)
- `pipeline_events` table logs all transitions with source (manual/detected/automated) and evidence
- `pipeline_detections` table holds AI-detected transitions (awaiting manual confirmation)
- Transitions logged to activity_tracker for audit trail

## Key Abstractions

**Job Model (`job_finder/models.py`):**
- Purpose: Normalized representation across all job sources
- Examples: `job_finder/models.py` line 9-55
- Pattern: Python dataclass with immutable dedup_key property (company+title only)
- Used by: All parsers, DB layer, scoring layer

**JobDB Class (`job_finder/db.py`):**
- Purpose: SQLite query interface with deduplication logic
- Examples: `upsert_job()`, `get_filtered_jobs()`, `load_job_context()`
- Pattern: Module-level functions take Connection object (per-request in web layer, custom in batch jobs)
- Instance method initialization deprecated in favor of function-based API

**Scoring Abstractions:**
- `score_job(conn, job_row, config, profile)` (`job_finder/web/job_scorer.py`) — single-tier scoring entry point; dispatches through the cascade
- `call_model(tier, ...)` (`job_finder/web/model_provider.py`) — provider-cascade dispatcher; resolves per-tier provider from config and tries fallback chain on schema-validation or rate-limit failures
- `derive_classification(...)` (`job_finder/db/_classification.py`) — Python-side derivation of `apply | consider | skip | reject` from numeric sub-scores
- `call_claude()` (`job_finder/web/claude_client.py`) — Anthropic-only adapter, used as the bottom of the cascade and for legacy non-scoring tiers
- Pattern: Returns `ScoringResult` dataclass (or `JobAssessment`) — never None on success; raises on hard failure

**Workload-class routing:** every LLM call — scoring and non-scoring alike — dispatches against a workload class, not a vendor-model tier. The three classes are:
- `quick` — extraction, parsing, navigation, research, reformatting, and the agentic enricher.
- `score` — full ordinal-rubric job scoring.
- `triage` — optional pre-scoring gate; reuses the `quick` model with a triage-specific prompt.

Per-provider model defaults live in `model_provider._PROVIDER_DEFAULTS` (a nested `provider → workload → model_id` dict). Users override these in `config.yaml` under `providers.overrides.<provider>.<workload>`. The legacy `'haiku' | 'sonnet' | 'opus'` and `'low' | 'mid' | 'high'` tier names are gone — both renames have shipped.

**Flask Blueprints:**
- Purpose: Modular route groups
- Examples: `jobs_bp`, `dashboard_bp`, `pipeline_bp`, `profile_bp`, `companies_bp`, etc.
- Pattern: Registered in order (detail routes BEFORE catch-alls to prevent shadowing)
- HTMX partials check `request.headers.get('HX-Request')` to return fragments vs. full pages

**Pipeline Detector (`job_finder/web/pipeline_detector.py`):**
- Purpose: Multi-signal email classification to auto-detect pipeline state changes
- Examples: Rejection email patterns, confirmation patterns, status-change cues
- Pattern: Analyzes inbound application-related emails to emit pipeline events
- Used by: Background pipeline watcher, manual confirmation UI

## Entry Points

**Main Web App:**
- Location: `job_finder/__main__.py` (canonical CLI entry, `main()`) → `job_finder/web/__init__.py:create_app()` (factory). `run.py` is a backward-compat shim that imports the same `main`.
- Triggers: `uv run job-cannon` (console script registered via `[project.scripts]`), `python -m job_finder`, or `python run.py` — all start the Flask development server on localhost:5000
- Config path resolution: `job_finder.config.resolve_config_path` looks at `$JOB_CANNON_CONFIG` → `./config.yaml` → user-config dir, in order
- Responsibilities:
  - Load config.yaml
  - Run database migrations
  - Register all blueprints
  - Initialize APScheduler background tasks
  - Set up Jinja2 filters and globals

**Background Ingestion:**
- Location: `job_finder/web/scheduler.py:init_scheduler()` → `pipeline_runner.run_ingestion()`
- Triggers: APScheduler fires every 30 minutes
- Responsibilities:
  - Fetch from Gmail and SerpAPI
  - Parse emails
  - Deduplicate and persist
  - Score through the cascade (`'scoring'` tier)
  - Track costs

**Manual Ingestion (Batch):**
- Location: `job_finder/main.py` (CLI interface)
- Triggers: `python -m job_finder.main` (used for backfills, one-time operations)
- Responsibilities: Triggered backfill operations (description reformat, company enrichment)

**API Routes (from Blueprints):**
- `/jobs/` — Job board index (GET full page)
- `/jobs/_table` — Fragment route (GET rows only)
- `/jobs/{dedup_key}/detail` — Expand row detail (GET fragment)
- `/jobs/{dedup_key}/status` — Change status (POST)
- `/jobs/{dedup_key}/paste-jd` — Store full JD and trigger scoring (POST)
- `/dashboard/` — Summary dashboard
- `/pipeline/` — Pipeline event viewer
- `/profile/` — Candidate profile editor
- `/companies/` — Company list and ATS scan controls
- `/settings/` — App config editor
- `/costs/` — Cost tracking dashboard

## Error Handling

**Strategy:** Per-layer isolation with graceful degradation

**Ingestion Layer:**
- Per-source error isolation: Gmail failure does not stop SerpAPI
- Per-email error isolation: One bad email doesn't stop the batch
- Errors logged to email_parse_log table with sender, message_id, error text
- Archive failures: Bad email bodies saved to `data/parse_failures/` for later inspection

**Scoring Layer:**
- `BudgetExceededError` raised by `call_claude()` when monthly Anthropic budget cap reached
- Callers (pipeline_runner, manual rescore) handle this and skip remaining Anthropic-path calls
- Free providers (Ollama, Groq, Cerebras, Gemini) are never budget-gated; only Anthropic-fallback usage counts against the cap
- `'scoring'` returns None / skips when `jd_full` is absent (jobs route to enrichment first)
- Cascade resilience: schema-validation failure on a provider falls through to the next provider in the chain rather than failing the whole call

**Web Layer (Request Handling):**
- HTMX fragment routes check `HX-Request` header, return full page if missing (for direct browser access)
- Form endpoints return empty response `('', 200)` for dismiss/success (not 204; HTMX needs 200)
- Job not found → 404
- Database connection errors → 500 (Flask default)

**Database:**
- Migration errors caught per-statement (avoid aborting entire migration on duplicate column)
- Query errors bubble up (no retry logic; explicit transaction control)

## Cross-Cutting Concerns

**Logging:** Python stdlib logging module
- Root logger configured with RotatingFileHandler (logs/app.log, 5MB max, 3 backups)
- Per-module loggers for structured debugging
- APScheduler logs background job lifecycle

**Validation:**
- Config: `load_config()` raises FileNotFoundError if config.yaml missing (fail-fast)
- Sort parameters: Allowlist check before SQL interpolation (no parameterized column names in SQLite)
- Job objects: Dataclass validators (minimal; rely on parser quality)

**Authentication:** None (single-user local app)
- Gmail OAuth 2.0 for API access only (not user auth)

**Cost Tracking:**
- Every model call recorded to `scoring_costs` table (job_id, purpose, provider, model, tokens, cost_usd, timestamp). Free-provider calls record `cost_usd=0`.
- Aggregated by provider/model/purpose for dashboard display
- Monthly budget gating applies to the Anthropic paid-fallback path only; all free-provider hops in the cascade are exempt

**Activity Tracking:**
- User actions logged to `activity_log` table (action, job_id, metadata, timestamp)
- Actions: expand_job, status_change, paste_jd, rescore, scheduled_sync, etc.
- Used for audit trail and UX insights
