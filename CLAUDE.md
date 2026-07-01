# CLAUDE.md

## Project Overview

Job Cannon is a personal job search command center. Flask web app (localhost:5000) that aggregates jobs from Gmail alerts (LinkedIn, Glassdoor, ZipRecruiter), SerpAPI, Thordata, DataForSEO, and live ATS scanners (Greenhouse / Lever / Ashby / Workday / SmartRecruiters), scores them through a single-tier ordinal rubric routed through a multi-provider cascade (Ollama qwen2.5:14b → Groq → Cerebras → Gemini → Anthropic CLI fallback ($0 via Claude.ai subscription)), and tracks application pipeline status.

**Single-user, local-only app. No deployment, no Docker, no CI/CD.**

## Tech Stack

- **Backend**: Python 3.13, Flask 3.1, Jinja2 + jinja2-fragments
- **Frontend**: HTMX 2.x, Tailwind CSS (CDN), SortableJS, vanilla JS only
- **Database**: SQLite with WAL mode, raw SQL (no ORM), schema migrations via a `schema_migrations` applied-set ledger (`pragma user_version` kept only as a best-effort cache)
- **Background**: APScheduler 3.11 (pinned <4.0 — 4.x has breaking async API)
- **AI**: Multi-provider cascade via `job_finder.web.model_provider.call_model()`. Production primary is Ollama (qwen2.5:14b); cascade falls through Groq, Cerebras, Gemini before reaching the Anthropic CLI fallback ($0 via Claude.ai subscription). The Anthropic SDK client is constructed as an availability gate (no-key clients are skipped), but inference itself dispatches through `claude -p` CLI subprocesses — see `claude_client.py` module docstring.
- **APIs**: Gmail API v1 (OAuth 2.0, read-only); SerpAPI / Thordata / DataForSEO / portal-search aggregator (all optional)

## Key Commands

```bash
# Run the app
uv run job-cannon                                 # Flask dev server on localhost:5000 (canonical)
uv run python -m job_finder                       # equivalent module entry

# Lifecycle / single-instance (see "Process lifecycle" below)
uv run job-cannon stop                            # stop the running instance + disable the supervisor
uv run job-cannon doctor                          # read-only diagnostics: claim markers, liveness, supervisor
uv run job-cannon serve                           # headless launch under the OS supervisor (health-gated)
uv run job-cannon supervisor-install [--uninstall] # opt-in OS keepalive (Scheduled Task / launchd / systemd)
uv run job-cannon healthcheck                     # machine-readable health verdict (exit 0/1/2)

# Tests — parallel by default (addopts carries `-n auto --dist loadscope`)
uv run --active pytest tests/                     # full suite
uv run --active pytest -x                         # stop on first failure
uv run --active pytest -n0                        # force serial (bisecting flakes, readable output)

# Dependencies
uv sync --extra dev --extra eval                  # install pyproject deps + dev/eval extras
```

## Git Workflow

`main` is branch-protected. Contributions go through a pull request; the CI aggregate gate (`tests-passed`) must pass before merge. Use conventional commits (`feat:`, `fix:`, `docs:`, etc.).

## Project Structure

```
job_finder/
├── web/
│   ├── __init__.py              # Flask app factory (create_app)
│   ├── blueprints/              # 14 blueprints: admin, batch_scoring, companies, costs, dashboard, detections, events, jobs, onboarding, pipeline, profile, settings, sync, updates
│   ├── templates/               # Jinja2 templates (base.html + partials)
│   ├── claude_client.py         # Anthropic SDK wrapper + cost tracking + budget gating (paid-fallback path)
│   ├── model_provider.py        # Multi-provider cascade dispatcher (call_model + tier resolution)
│   ├── providers/               # Per-provider implementations (anthropic, gemini, ollama)
│   ├── job_scorer.py            # Single-tier v3.0 ordinal scoring
│   ├── scoring_orchestrator.py  # Per-run orchestration, retry, attribution
│   ├── scheduler/               # APScheduler background jobs
│   ├── pipeline_detector/       # Multi-signal email classification
│   ├── ats_platforms/           # ATS platform scanners + registry
│   ├── ats_scanner/             # ATS platform scanner
│   ├── careers_crawler/         # Multi-tier careers-page crawler
│   ├── migrations/              # Per-version migration modules
│   ├── pipeline_runner.py       # Orchestrates ingestion + scoring + detection
│   ├── db_helpers.py            # Per-request g.db pattern
│   ├── db_migrate.py            # Migrations driver
│   └── stale_detector.py        # Nightly stale job detection (own DB connection)
├── parsers/                     # Email parsers: linkedin, glassdoor, indeed, ziprecruiter, monster, trueup, greenhouse
├── sources/                     # gmail_source.py, serpapi_source.py, thordata_source.py, dataforseo_source.py, portal_search_source.py
├── models.py                    # Job dataclass with dedup_key
├── db/                          # DB package: __init__ + _queries + _jobs + _persistence + _classification
└── config.py                    # YAML config loader (fail-fast, no defaults)
tests/
├── conftest.py                  # Fixtures: app factory, test DB, mocked Claude client
└── test_*.py                    # ~90 test files
```

## Architecture Decisions That Matter

**HTMX patterns** (most common source of bugs):
- Fragment routes MUST check `HX-Request` header and return full page for direct browser access
- Status dropdown: `hx-target=this hx-swap=outerHTML` on the select element itself
- Accordion: compact row + hidden `<tr data-expand-slot>` placeholder pairs
- Collapse returns hidden placeholder (NOT duplicate compact row)
- Use `hx-on:click` not `onclick` for event.stopPropagation() in HTMX 2.x
- Dismiss/return responses: `('', 200)` not `204` — HTMX requires 200 for outerHTML swap
- Detail-inline route registered BEFORE catch-all to avoid Flask route shadowing

**Database**:
- Migrations stored as list of discrete SQL strings (not semicolon-delimited)
- "Applied" is set membership in the `schema_migrations` ledger — NOT `user_version > N`. A migration merged in below the current max still runs (no silent skip). Create new migrations with `python scripts/new_migration.py "<slug>"` (mints a collision-free version — never hand-pick a number). Legacy DBs backfill the ledger from `user_version` once on first run.
- `CREATE TABLE IF NOT EXISTS` for idempotent migration on both empty and populated DBs
- Stale detector creates own sqlite3 connection (thread-safe for APScheduler, not Flask g.db)
- sort_by validated against Python allowlist before SQL interpolation (no parameterized column names in SQLite)

**Process lifecycle / single-instance** (one live Job Cannon tree per `(host, port)`):
- **One pre-bind authority**: every launch (bare or `serve`) calls `job_finder.web._takeover.claim_or_takeover` before binding. Health-gated: defer to a healthy / pre-`/__jc_health` instance, reap only a **wedged** orphan (socket held, no HTTP within a short timeout), refuse a foreign listener. `serve` runs the same path but headless (never opens a browser) — it does NOT kill a healthy instance.
- **One lock**, keyed on `(host, port)`: `_pidfile.claim_paths()` is the sole authority for the on-disk names (`logs/server-<slug>.lock` + `.json`). The in-process scheduler consults `_pidfile.holds_claim()` (does THIS process hold the lock?) instead of a second, separately-keyed lock. There is no `scheduler.pid` — collapsed in favor of the single claim.
- **Child reap**: Windows Job Object uses `KILL_ON_JOB_CLOSE` only (NOT `SILENT_BREAKAWAY_OK`), so spawned children (Ollama) inherit the job and are reaped on any death of the owner. Belt-and-suspenders: `register_owned_process` records owned child `{pid, name, create_time}` in the sidecar; `free_jc_port` sweeps those (create_time-validated) for the reparented-orphan-across-restart case.
- **Commands**: `stop` (terminate instance + disable supervisor), `doctor` (read-only diagnostics), `serve` (headless, supervised), `supervisor-install [--uninstall]` (opt-in OS keepalive — never auto-installed in dev). Prefer `job-cannon stop` over manually killing the process tree.

**Testing**:
- `create_app()` accepts `config=` dict for test isolation
- Temp DB per test; mocked Claude client at injection point
- `conftest.py` has fixtures for app factory, test DB, mock Claude
- Scheduler start in tests is gated by the autouse `mock_scheduler_claim` fixture (patches `holds_claim`); the prior `mock_scheduler_pidfile` seam is gone

**Scoring**:
- v3.0 single tier: `'scoring'` tier. Output is a six-axis ordinal rubric; classification is **Python-derived** from the sub-scores in `job_finder.db._classification.derive_classification` — never emitted by the LLM.
- Cascade order (configurable in `config.yaml > providers.fallback_chain`): Ollama → Gemini → Claude Code CLI → Anthropic. Each provider can be disabled or rate-limited independently.
- cost_gate returns bool — callers decide whether to raise BudgetExceededError. The gate excludes all members of `claude_client.FREE_PROVIDERS` (gemini, ollama, claude_cli, claude_code_cli, gemini_cli, local_bundled, google_cse) from the spend sum.
- Scoring requires `jd_full` (no cost without full JD); jobs lacking jd_full route to enrichment first.
- Rescoring skips already-scored jobs unless `force=True` (manual rescore).
- `description` vs `jd_full`: `description` is parser-supplied short text; `jd_full` is the canonical full body (often a separate fetch, promoted via `set_jd_full`).

**Workload-class tiers:**
- `quick`: every non-scoring LLM call (extraction, parsing, navigation, research, reformatting, agentic enricher)
- `score`: full ordinal-rubric job scoring
- `triage`: optional pre-scoring gate (uses quick model with triage-specific prompt)

Per-provider model defaults live in `_PROVIDER_DEFAULTS` (nested provider→workload→model dict).

## Conventions

- Always use Context7 MCP when working with library APIs, especially for: APScheduler, HTMX, Anthropic SDK, Flask, jinja2-fragments
- Snake_case everywhere (files, functions, variables). PascalCase for classes only.
- Ruff enforced in CI (`uvx ruff check .` + `ruff format --check` via the Lint & Format job and pre-commit). Run `uvx ruff format <changed files>` before pushing.
- Absolute imports from `job_finder` package root.
- No barrel files; `__init__.py` files are mostly empty.
- Blueprint routes use `strict_slashes=False`.
- Jinja2 custom filters: `from_json`, `urlencode`, `format_description`, `relative_date`.

## User Data Files (Not Tracked in Git)

These files contain personal data and API keys. They are `.gitignore`d.

| File | Template | Purpose |
|------|----------|---------|
| `config.yaml` | `config.example.yaml` | App config, API keys, profile targets |
| `experience_profile.json` | `experience_profile.example.json` | Career history for fit-scoring personalization in the `'scoring'` tier prompt |

**config.yaml must ONLY be modified with surgical string replacement (Edit tool), NEVER with a full-file overwrite (Write tool).** The settings save route (`_write_config`) is safe because it reads→merges→writes. Full-file rewrites silently drop existing config sections.

## Environment Variables

- `JOB_CANNON_USER_DATA_DIR` (optional) — absolute path to user data directory. For local development, set this to the project root if you want config.yaml and jobs.db to stay in the repository directory instead of the OS user-data directory.
- `OLLAMA_EXE` (optional) — absolute path to `ollama.exe`. Only needed if Ollama is installed somewhere other than the Windows default (`%LOCALAPPDATA%\Programs\Ollama\ollama.exe`) and not on PATH.

## Don't

- Don't add an ORM — raw SQL is intentional for this project's scale
- Don't add a build step or bundler — Tailwind CDN + HTMX CDN is intentional
- Don't use `--no-verify` or skip hooks
- Don't use APScheduler 4.x (breaking async API)
- Don't use `204` for HTMX fragment responses (use `200`)
- Don't create separate detail pages — inline expansion via HTMX is the pattern
- Don't use `hx-include` with CSS selectors for form fields — use proper `<form>` wrappers
- Don't manually kill the process tree to restart — use `job-cannon stop` (it reaps owned children + disables the supervisor); a bare relaunch already reclaims a wedged orphan
- Don't add a second single-instance lock or `scheduler.pid` — the one `(host, port)` claim (`_pidfile`) is the single source of truth; the scheduler consults `holds_claim()`
- Don't re-add `SILENT_BREAKAWAY_OK` to the Win32 Job Object — it orphans the spawned Ollama on hard-kill
