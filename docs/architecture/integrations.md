# External Integrations

This document describes the external services Job Cannon integrates with for engineers reading the source. For setup and run instructions, see [docs/SETUP.md](../SETUP.md).

## APIs & External Services

**Job Aggregation:**
- Gmail API - Fetches job alert emails from LinkedIn, Glassdoor, Indeed, ZipRecruiter
  - SDK: `google-api-python-client`
  - Auth: OAuth 2.0 with stored token (`token.json`)
  - Scope: `https://www.googleapis.com/auth/gmail.readonly`
  - Implementation: `job_finder/sources/gmail_source.py`

- SerpAPI (Google Jobs) - Searches aggregated job listings
  - SDK: `google-search-results`
  - Auth: API key from config.yaml (`sources.serpapi.api_key`)
  - Endpoint: `https://serpapi.com/search.json` with `engine=google_jobs`
  - Configuration: `sources.serpapi.enabled`, `sources.serpapi.queries`
  - Implementation: `job_finder/sources/serpapi_source.py`

**Email Parsers:**
- LinkedIn job alerts - Parser: `job_finder/parsers/linkedin_parser.py`
- Glassdoor job alerts - Parser: `job_finder/parsers/glassdoor_parser.py`
- Indeed job alerts - Parser: `job_finder/parsers/indeed_parser.py` (stub)
- ZipRecruiter job alerts - Parser: `job_finder/parsers/ziprecruiter_parser.py`

**Sender-to-Parser Mapping:**
```python
SENDER_PARSERS = {
    "jobalerts-noreply@linkedin.com": parse_linkedin_alert,
    "jobs-noreply@linkedin.com": parse_linkedin_alert,
    "noreply@glassdoor.com": parse_glassdoor_alert,
    "alert@indeed.com": parse_indeed_alert,
    "no-reply@ziprecruiter.com": parse_ziprecruiter_alert,
}
```

## AI & Evaluation

**Anthropic Claude API:**
- Service: Anthropic - Haiku, Sonnet, Opus models
- Authentication: API key via `ANTHROPIC_API_KEY` environment variable
- Usage:
  - Haiku (claude-haiku-4-5): Fast job filtering (0-100 score)
  - Sonnet (claude-sonnet-4-6): Deep fit analysis and qualitative assessment
  - Opus (claude-opus-4-6): Profile extraction from experience documents
- Implementation: `job_finder/web/claude_client.py`
  - Cost calculation: per-million-token pricing table
  - Budget gating: Monthly cap enforced before Sonnet/Opus calls
  - Cost tracking: Recorded in `scoring_costs` table

## Data Storage

**Database:**
- SQLite 3.x local file (`jobs.db`)
  - Connection: Per-request via Flask `g.db` pattern
  - Mode: WAL (Write-Ahead Logging) for durability
  - Core tables: jobs, runs, pipeline_events, email_parse_log, scoring_costs, pipeline_detections
  - Implementation: `job_finder/db.py` (module-level functions), `job_finder/web/db_migrate.py` (migrations)

**File Storage:**
- Local filesystem only - `data/` directory for email parsing failures
  - Parse failure archival: `data/parse_failures/{sender_domain}_{ISO_timestamp}.html`
  - Purpose: Debugging email parsing issues

**Cache:**
- None (in-memory notification cooldown tracking via module-level dict)

## Authentication & Identity

**Google OAuth 2.0:**
- Provider: Google Cloud Console
- Flow: 3-legged OAuth (InstalledAppFlow) via `job_finder/gmail_auth.py`
- Scope: `https://www.googleapis.com/auth/gmail.readonly` - Read Gmail labels, messages
- Token storage: `token.json` (persistent, auto-refreshed)
- Credentials: `credentials.json` from Google Cloud Console (OAuth client secret)
- Implementation:
  - Auth flow: `job_finder/gmail_auth.py`
  - Gmail client: `job_finder/sources/gmail_source.py`

**Flask Session:**
- Secret key: `FLASK_SECRET_KEY` environment variable (or dev default)
- Purpose: Session signing for Flask-managed state (if any)

## Monitoring & Observability

**Error Tracking:**
- None - Errors logged to `logs/app.log` with RotatingFileHandler

**Logs:**
- File-based: `logs/app.log` (RotatingFileHandler, 5MB max, 3 backups)
- Format: `%(asctime)s %(levelname)s %(name)s: %(message)s`
- Logger: Python's standard `logging` module
- Activity tracking: `job_finder/web/activity_tracker.py` (custom activity log in database)

**Cost Tracking:**
- Database-backed: `scoring_costs` table with cost_usd, model, purpose, tokens, timestamp
- Queries: `get_cost_stats()`, `get_daily_cost_breakdown()`, `get_monthly_feature_breakdown()`
- Implementation: `job_finder/web/claude_client.py`

## Background Jobs & Scheduling

**APScheduler:**
- Scheduler type: BackgroundScheduler (daemon thread)
- Interval job: Ingestion pipeline every 30 minutes
  - Trigger: IntervalTrigger(minutes=30)
  - Task: `run_ingestion()` - Fetches Gmail + SerpAPI, dedupes, scores
- Cron job: Stale job detection (configurable)
  - Trigger: CronTrigger based on config.ats.scan_days and scan_hour
  - Task: Marks jobs as stale if no activity in configured days
- Guards:
  - TESTING=True disables scheduler (pytest isolation)
  - Werkzeug reloader child process skips double-start
  - Module-level singleton prevents re-initialization
- Implementation: `job_finder/web/scheduler.py`

## CI/CD & Deployment

**Hosting:**
- Localhost only (flask run on 127.0.0.1:5000)
- No external deployment infrastructure

**Development Server:**
- Flask built-in dev server (debug=True, use_reloader=False)
- Command: `python run.py` (entry point: `run.py`)

## Environment Configuration

**Required Environment Variables:**
- `ANTHROPIC_API_KEY` - Anthropic API key (sk-ant-...) — REQUIRED

**Optional Environment Variables:**
- `FLASK_SECRET_KEY` - Flask session signing key (default: "dev-secret-key-change-in-production")

**Config File Variables (config.yaml):**
- `sources.gmail.enabled` - Enable Gmail polling (default: False)
- `sources.gmail.lookback_days` - Email lookback window (default: 7)
- `sources.serpapi.enabled` - Enable SerpAPI searches (default: False)
- `sources.serpapi.api_key` - SerpAPI key (free tier: 100/month)
- `sources.serpapi.queries` - List of {query, location} dicts
- `sources.jsearch.enabled` - Enable JSearch (not yet implemented)
- `sources.jsearch.rapidapi_key` - RapidAPI key for JSearch
- `scoring.monthly_budget_usd` - Monthly Claude budget cap (default: $25.00)
- `scoring.haiku_threshold` - Score threshold for promoting jobs to Sonnet evaluation (default: 55)

**Secrets Location:**
- `.env` file (gitignored) - Contains ANTHROPIC_API_KEY
- `.env.example` - Template (safe to commit)
- `config.yaml` (gitignored) - Contains SerpAPI key and other source-specific keys
- `config.example.yaml` - Template
- `credentials.json` (gitignored) - OAuth credentials from Google Cloud
- `token.json` (gitignored) - Saved OAuth token (auto-generated by oauth flow)

## Webhooks & Callbacks

**Incoming:**
- None - Polling-based architecture (Gmail and SerpAPI)

**Outgoing:**
- Anthropic Streaming: Not used (all Claude calls are request-response)
