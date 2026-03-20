# Codebase Structure

**Analysis Date:** 2026-03-17

## Directory Layout

```
job-finder/
├── job_finder/                 # Main package
│   ├── __init__.py             # Package init (empty)
│   ├── config.py               # Configuration loader + defaults
│   ├── db.py                   # SQLite persistence layer (JobDB class + helpers)
│   ├── models.py               # Job dataclass
│   ├── gmail_auth.py           # OAuth token setup for Gmail API
│   ├── main.py                 # CLI entry point for batch operations
│   │
│   ├── parsers/                # Email format parsers
│   │   ├── __init__.py
│   │   ├── linkedin_parser.py  # LinkedIn job alert emails
│   │   ├── glassdoor_parser.py # Glassdoor job alert emails
│   │   ├── indeed_parser.py    # Indeed job alert emails (stub)
│   │   └── ziprecruiter_parser.py  # ZipRecruiter job alert emails
│   │
│   ├── sources/                # Job source adapters
│   │   ├── __init__.py
│   │   ├── gmail_source.py     # Fetch emails via Gmail API
│   │   └── serpapi_source.py   # Fetch jobs via SerpAPI REST
│   │
│   ├── scoring/                # Legacy scoring module
│   │   ├── __init__.py
│   │   └── scorer.py           # Basic job scorer (deprecated in favor of Haiku/Sonnet)
│   │
│   ├── output/                 # Legacy output module (unused)
│   │   └── __init__.py
│   │
│   └── web/                    # Flask web application
│       ├── __init__.py         # App factory (create_app)
│       ├── db_helpers.py       # Per-request DB connection context (g.db)
│       ├── db_migrate.py       # Schema migrations + one-time backfills
│       ├── scheduler.py        # APScheduler background job init
│       ├── claude_client.py    # Anthropic API wrapper + cost tracking
│       ├── haiku_scorer.py     # First-tier fast-filter scoring
│       ├── sonnet_evaluator.py # Second-tier deep evaluation
│       ├── pipeline_runner.py  # Orchestrator: fetch → parse → score → persist
│       │
│       ├── data_enricher.py    # Optional: enrich jobs with company/role data
│       ├── dedup_normalizer.py # Normalize company names for dedup_key
│       ├── exclusion_filter.py # Skip jobs by company/role patterns
│       ├── ats_scanner.py      # Detect ATS job postings (score penalty)
│       ├── description_reformatter.py  # Reformat descriptions on startup
│       ├── expiry_checker.py   # Mark jobs as stale (not active recently)
│       ├── stale_detector.py   # Background job to mark old jobs stale
│       ├── activity_tracker.py # Log user actions for audit trail
│       ├── rejection_analyzer.py  # Analyze rejection reasons
│       ├── pipeline_detector.py   # Multi-signal email classification for state detection
│       │
│       ├── resume_generator.py # Generate tailored resumes via Google Docs API
│       ├── resume_validator.py # Validate resume content before generation
│       ├── resume_style_guide.py  # Extract style preferences from existing resume
│       ├── resume_feedback.py  # Collect feedback on generated resumes
│       ├── docx_formatter.py   # Format DOCX resume templates
│       ├── interview_prep.py   # Generate interview prep guides
│       ├── drive_status.py     # Check Google Drive auth status
│       ├── drive_uploader.py   # Upload files to Google Drive
│       │
│       ├── careers_scraper.py  # Optional: scrape company careers pages
│       ├── notifier.py         # Optional: send desktop notifications
│       ├── backfill_enrichment.py   # Backfill company data for existing jobs
│       ├── backfill_companies.py    # Batch create company records
│       ├── profile_schema.py   # Validate profile.json structure
│       │
│       ├── templates/          # Jinja2 HTML templates
│       │   ├── base.html       # Base layout (nav, sidebar, content area)
│       │   ├── components/     # Shared components
│       │   │   └── _sidebar.html  # Left navigation
│       │   ├── jobs/           # Job board routes
│       │   │   ├── index.html  # Full page with filter bar
│       │   │   ├── detail.html # Single job detail (modal-like)
│       │   │   ├── _row.html   # Compact row (collapsed)
│       │   │   ├── _row_detail.html  # Expanded row detail
│       │   │   ├── _row_collapse_response.html  # Response when collapsing
│       │   │   ├── _interview_prep.html  # Interview prep display
│       │   │   ├── _interview_prep_generating.html  # Generating state
│       │   │   ├── _quick_apply_response.html  # Quick apply feedback
│       │   │   ├── _resume_section.html  # Resume generation UI
│       │   │   ├── _resume_generating.html  # Generating state
│       │   │   ├── _resume_done.html  # Success state
│       │   │   └── _resume_error.html  # Error state
│       │   ├── dashboard/      # Dashboard routes
│       │   │   ├── index.html  # Main dashboard
│       │   │   ├── _batch_score_progress.html  # Batch scoring progress
│       │   │   ├── _batch_score_done.html  # Batch done summary
│       │   │   ├── _detection_card.html  # Pipeline detection display
│       │   │   ├── _detection_confirmed.html  # Confirmed detection
│       │   │   ├── _cost_detail.html  # Cost breakdown
│       │   │   ├── _review_queue.html  # Jobs awaiting review
│       │   │   └── _rejection_insights.html  # Rejection analysis
│       │   ├── pipeline/       # Pipeline tracking routes
│       │   │   └── index.html  # Pipeline event log
│       │   ├── profile/        # Profile editor routes
│       │   │   └── index.html  # Edit candidate profile
│       │   ├── resume/         # Resume generation routes
│       │   │   └── index.html  # Resume config/history
│       │   ├── settings/       # Settings routes
│       │   │   └── index.html  # App configuration editor
│       │   ├── companies/      # Company tracking routes
│       │   │   ├── index.html  # Company list
│       │   │   ├── _row.html   # Compact company row
│       │   │   ├── _row_expanded.html  # Expanded company detail
│       │   │   ├── _table.html  # Full company table
│       │   │   └── _scan_result.html  # ATS scan result
│       │   ├── costs/          # Cost tracking routes
│       │   │   └── index.html  # Cost dashboard
│       │   └── feedback/       # Feedback routes
│       │       ├── index.html  # Preference feedback form
│       │       └── _preference_row.html  # Single preference row
│       │
│       └── blueprints/         # Flask blueprint modules
│           ├── __init__.py     # Shared constants (PIPELINE_STATUSES)
│           ├── jobs.py         # Job board routes + HTMX fragments
│           ├── dashboard.py    # Dashboard routes
│           ├── pipeline.py     # Pipeline event routes
│           ├── profile.py      # Profile editor routes
│           ├── resume.py       # Resume generation routes
│           ├── companies.py    # Company tracking routes
│           ├── costs.py        # Cost dashboard routes
│           ├── settings.py     # Settings routes
│           ├── feedback.py     # User feedback routes
│           └── detections.py   # Pipeline detection review routes
│
├── tests/                      # Test suite
│   ├── conftest.py             # Pytest fixtures (app factory, temp DB, mocks)
│   ├── test_linkedin_parser.py # LinkedIn parser tests
│   ├── test_glassdoor_parser.py # Glassdoor parser tests
│   ├── test_ziprecruiter_parser.py  # ZipRecruiter parser tests
│   ├── test_dedup_normalizer.py # Dedup key generation tests
│   ├── test_db.py              # JobDB persistence tests
│   ├── test_haiku_scorer.py    # Haiku scoring tests
│   ├── test_pipeline_detector.py  # Email state detection tests
│   ├── test_pipeline_runner.py # Ingestion orchestration tests
│   ├── test_activity_tracker.py # Activity logging tests
│   └── test_routes.py          # Web route tests (blueprints)
│
├── run.py                      # CLI entry point — starts Flask dev server
├── config.example.yaml         # Example configuration (with secrets template)
├── config.yaml                 # User configuration (NOT tracked in git)
├── experience_profile.example.json  # Example candidate profile
├── experience_profile.json     # User profile (NOT tracked in git)
├── resume_style_guide.json     # Resume formatting preferences (NOT tracked)
├── experience_reference.md     # Full experience narrative (NOT tracked)
│
├── requirements.txt            # Python dependencies
├── .gitignore                  # Git ignore rules (excludes config, profiles, logs)
├── .env.example                # Example .env (with API key templates)
├── .env                        # Local environment variables (NOT tracked)
├── CLAUDE.md                   # Project instructions + architecture decisions
│
├── .planning/                  # GSD planning documentation
│   ├── ROADMAP.md              # 5-phase milestone plan
│   ├── STATE.md                # Current state + 100+ architectural decisions
│   ├── codebase/               # Codebase analysis documents
│   │   ├── ARCHITECTURE.md     # Architecture (this directory)
│   │   ├── STRUCTURE.md        # Structure (this directory)
│   │   ├── CONVENTIONS.md      # Coding conventions
│   │   ├── TESTING.md          # Testing patterns
│   │   ├── STACK.md            # Technology stack
│   │   ├── INTEGRATIONS.md     # External integrations
│   │   └── CONCERNS.md         # Technical debt + issues
│   ├── debug/                  # Debug documentation + resolved issues
│   └── milestones/             # Phase-based planning documents
│
└── logs/                       # Application logs (NOT tracked, created at runtime)
```

## Directory Purposes

**`job_finder/`** — Main package root
- Contains core logic (config, models, database, parsers, sources)
- Entrypoint: `web/__init__.py:create_app()` for web server, `main.py` for CLI

**`job_finder/parsers/`** — Email format parsers
- Purpose: Extract job postings from raw email bodies
- Pattern: Each parser implements `parse_*_alert(body, email_date) → list[Job]`
- Format: Handles LinkedIn, Glassdoor, Indeed, ZipRecruiter email formats
- Input: Raw email body string from Gmail API
- Output: List of normalized Job objects or empty list if parse fails

**`job_finder/sources/`** — Job source adapters
- Purpose: Fetch job postings from external APIs
- Modules:
  - `gmail_source.py` — OAuth-authenticated Gmail API client; routes emails to appropriate parser
  - `serpapi_source.py` — REST client for SerpAPI job search
- Pattern: Each source implements `fetch_recent(config, days_back) → list[Job]`

**`job_finder/web/`** — Flask application code
- Substructure:
  - `blueprints/` — Route handlers (10+ Flask blueprints)
  - `templates/` — Jinja2 HTML + HTMX fragments
  - Scoring modules: `haiku_scorer.py`, `sonnet_evaluator.py`, `claude_client.py`
  - Orchestration: `pipeline_runner.py`, `scheduler.py`
  - Utilities: data enrichment, dedup, filtering, activity tracking

**`job_finder/web/blueprints/`** — Flask blueprints (modular route groups)
- One blueprint per feature area: jobs, dashboard, pipeline, profile, resume, companies, costs, settings, feedback, detections
- Each blueprint:
  - Registers routes with `@blueprint.route()` decorator
  - Uses Flask `g.db` for per-request database connections
  - Returns full page on direct browser access, fragments on HTMX requests
  - No circular imports (blueprints registered AFTER scheduler init to avoid import loops)

**`job_finder/web/templates/`** — Jinja2 templates organized by blueprint
- `base.html` — Master layout with nav, sidebar, content area
- `components/` — Reusable HTML fragments (sidebar, etc.)
- `jobs/` — Job board routes (19+ templates)
- `dashboard/` — Dashboard views (7+ templates)
- Other blueprint directories (pipeline, profile, resume, companies, costs, settings, feedback)
- Pattern:
  - Fragment routes return `_partial.html` files (prefixed with underscore)
  - HTMX partials check `HX-Request` header; return full page if missing
  - Status dropdowns use `hx-target=this hx-swap=outerHTML` on select element itself
  - Accordion/collapsible rows use compact + expanded pair pattern

**`tests/`** — Pytest test suite (266 tests)
- Structure: One test file per module under test
- Pattern: `test_*.py` files with `test_*()` functions
- Fixtures: `conftest.py` provides app factory, temp DB, mocked Claude client
- Coverage: Parsers, dedup, DB, scoring, pipeline orchestration, activity tracking, routes

**`logs/`** — Application logs (created at runtime)
- File: `logs/app.log`
- Rotation: 5MB per file, 3 backup files kept
- Encoding: UTF-8
- Format: `%(asctime)s %(levelname)s %(name)s: %(message)s`

## Key File Locations

**Entry Points:**
- `run.py` — CLI: `python run.py` starts Flask dev server on localhost:5000
- `job_finder/main.py` — CLI: `python -m job_finder.main` for batch operations
- `job_finder/web/__init__.py:create_app()` — Flask app factory

**Configuration:**
- `config.yaml` — User configuration (NOT tracked; copy from `config.example.yaml`)
- `experience_profile.json` — Candidate profile (NOT tracked; copy from `experience_profile.example.json`)
- `job_finder/config.py` — Centralized defaults (model names, thresholds, budget)

**Core Logic:**
- `job_finder/db.py` — SQLite persistence (JobDB class, helper functions)
- `job_finder/models.py` — Job dataclass definition
- `job_finder/web/pipeline_runner.py` — Main ingestion orchestrator

**Scoring:**
- `job_finder/web/haiku_scorer.py` — First-tier fast-filter (0.5-2 cents per job)
- `job_finder/web/sonnet_evaluator.py` — Second-tier deep eval (4-8 cents per job)
- `job_finder/web/claude_client.py` — Anthropic API wrapper + cost tracking

**Background Jobs:**
- `job_finder/web/scheduler.py` — APScheduler initialization (30-min interval)
- `job_finder/web/stale_detector.py` — Mark old jobs stale (background task)
- `job_finder/web/resume_generator.py` — Resume generation (background thread on apply)
- `job_finder/web/interview_prep.py` — Interview prep generation (background thread)

**Web Routes:**
- `job_finder/web/blueprints/jobs.py` — `/jobs/` (job board)
- `job_finder/web/blueprints/dashboard.py` — `/dashboard/` (summary)
- `job_finder/web/blueprints/pipeline.py` — `/pipeline/` (event log)
- `job_finder/web/blueprints/profile.py` — `/profile/` (edit profile)
- `job_finder/web/blueprints/resume.py` — `/resume/` (resume generation)
- `job_finder/web/blueprints/companies.py` — `/companies/` (company tracking)
- `job_finder/web/blueprints/costs.py` — `/costs/` (cost dashboard)
- `job_finder/web/blueprints/settings.py` — `/settings/` (app config)

**Testing:**
- `tests/conftest.py` — Shared pytest fixtures
- `tests/test_linkedin_parser.py` — Email parser tests
- `tests/test_db.py` — Database persistence tests
- `tests/test_pipeline_detector.py` — Email state detection tests
- `tests/test_routes.py` — Web route tests

## Naming Conventions

**Files:**
- Python modules: `snake_case.py` (e.g., `pipeline_runner.py`, `haiku_scorer.py`)
- HTML templates: `snake_case.html` (e.g., `base.html`, `_resume_section.html`)
- Partials (HTMX fragments): `_snake_case.html` (leading underscore)
- Test files: `test_*.py` (e.g., `test_linkedin_parser.py`)

**Directories:**
- Python packages: `snake_case/` (e.g., `parsers/`, `sources/`, `blueprints/`)
- Template groups: `snake_case/` matching blueprint name (e.g., `jobs/`, `dashboard/`, `pipeline/`)

**Classes:**
- PascalCase (e.g., `Job`, `JobDB`, `BudgetExceededError`)

**Functions/Methods:**
- snake_case (e.g., `score_job_haiku()`, `evaluate_job_sonnet()`, `parse_linkedin_alert()`)

**Constants:**
- UPPER_SNAKE_CASE (e.g., `PIPELINE_STATUSES`, `HAIKU_SCHEMA`, `SENDER_PARSERS`)
- Config defaults: `DEFAULT_*` (e.g., `DEFAULT_HAIKU_THRESHOLD`, `DEFAULT_MONTHLY_BUDGET_USD`)

**Variables:**
- snake_case (e.g., `job_row`, `haiku_score`, `dedup_key`)

**Jinja2 Filters:**
- snake_case (e.g., `from_json`, `urlencode`, `format_description`, `relative_date`)

## Where to Add New Code

**New Feature (e.g., Company Tracking):**
1. Model: Add columns to `jobs` table in `db_migrate.py` (new migration)
2. Web layer: Create `job_finder/web/blueprints/companies.py` blueprint
3. Templates: Create `job_finder/web/templates/companies/` directory with `index.html` + partials
4. Tests: Create `tests/test_companies.py` with route + logic tests
5. Register blueprint in `job_finder/web/__init__.py` (add to `create_app()`)

**New Blueprint:**
- Pattern: `job_finder/web/blueprints/{feature}.py`
- Structure:
  ```python
  from flask import Blueprint

  {feature}_bp = Blueprint("{feature}", __name__, url_prefix="/{feature}")

  @{feature}_bp.route("/", strict_slashes=False)
  def index():
      # Route logic
  ```
- Register in `create_app()` — order matters if routes overlap
- Create templates in `job_finder/web/templates/{feature}/`

**New Jinja2 Filter:**
- Register in `create_app()` via `@app.template_filter("name")`
- Example: `format_description_filter()` at line 153 of `job_finder/web/__init__.py`
- Centralizes filter logic in one place

**New Email Parser:**
- Pattern: Create `job_finder/parsers/{source}_parser.py`
- Implement: `parse_{source}_alert(body, email_date) → list[Job]`
- Register in `SENDER_PARSERS` dict in `job_finder/sources/gmail_source.py`

**New Scoring Feature:**
- Pattern: Create `job_finder/web/{feature}.py` module
- Use `call_claude()` for API calls (auto cost tracking)
- Check `cost_gate()` before Sonnet/Opus calls (not Haiku)
- Record calls via `record_cost()` for dashboard visibility
- Integrate into `pipeline_runner.run_ingestion()` or manual rescore route

**New Background Task:**
- Pattern: Create function in `job_finder/web/{task}.py` or add to `scheduler.py`
- APScheduler job: Register in `init_scheduler(app)` with CronTrigger or IntervalTrigger
- Create own DB connection (don't use Flask g.db)
- Handle errors gracefully (log, don't crash scheduler)

**Utilities/Helpers:**
- Shared helpers: `job_finder/web/{feature}.py` (e.g., `dedup_normalizer.py`, `exclusion_filter.py`)
- No barrel files (`__init__.py` are mostly empty)
- Import directly: `from job_finder.web.dedup_normalizer import normalized_dedup_key`

## Special Directories

**`.planning/`**
- Purpose: GSD (Get Stuff Done) planning and architecture documentation
- Contents:
  - `ROADMAP.md` — 5-phase milestone plan with success criteria
  - `STATE.md` — Current state + 100+ architectural decisions + pending todos
  - `codebase/` — This directory (ARCHITECTURE.md, STRUCTURE.md, etc.)
  - `debug/` — Resolved bugs and debug notes
  - `milestones/` — Phase-based planning documents
- Generated: No (manually maintained)
- Committed: Yes (part of git history for context)

**`logs/`**
- Purpose: Application runtime logs
- Generated: Yes (created by `create_app()` on first run)
- Committed: No (in `.gitignore`)

**`data/`** (if it exists)
- Purpose: Runtime data (e.g., parse failures archived for inspection)
- Generated: Yes (created by ingestion pipeline on demand)
- Committed: No

**`.env`**
- Purpose: Local environment variables (API keys, secrets)
- Generated: Manually created from `.env.example`
- Committed: No (in `.gitignore`)

---

*Structure analysis: 2026-03-17*
