# CLAUDE.md

## Project Overview

Job Cannon is a personal job search command center. Flask web app (localhost:5000) that aggregates jobs from Gmail alerts (LinkedIn, Glassdoor, ZipRecruiter), SerpAPI, Thordata, and DataForSEO, scores them with a two-tier Claude AI pipeline (Haiku fast filter → Sonnet deep evaluation), and tracks application pipeline status.

**Single-user, local-only app. No deployment, no Docker, no CI/CD.**

## Tech Stack

- **Backend**: Python 3.13, Flask 3.1, Jinja2 + jinja2-fragments
- **Frontend**: HTMX 2.x, Tailwind CSS (CDN), SortableJS, vanilla JS only
- **Database**: SQLite with WAL mode, raw SQL (no ORM), schema migrations via `pragma user_version`
- **Background**: APScheduler 3.11 (pinned <4.0 — 4.x has breaking async API)
- **AI**: Anthropic API — Haiku for fast scoring, Sonnet for deep evaluation
- **APIs**: Gmail API v1 (OAuth 2.0, read-only); SerpAPI / Thordata / DataForSEO / portal-search aggregator (all optional)

## Key Commands

```bash
# Run the app
uv run job-cannon                                 # Flask dev server on localhost:5000 (canonical)
uv run python -m job_finder                       # equivalent module entry
uv run python run.py                              # legacy entry, still works (now a shim)

# Tests
uv run --active pytest tests/                              # All tests (2135 passing / 9 skipped / 1 deselected as of 2026-05-06)
uv run --active pytest tests/test_pipeline_detector.py -v  # Specific file
uv run --active pytest -x                                  # Stop on first failure

# Dependencies
uv sync --extra dev --extra eval                  # Install pyproject deps + dev/eval extras
```

## Git Workflow

- Commit directly to main for all work (phase execution, hotfixes, config tweaks)
- Push to origin regularly

## Project Structure

```
job_finder/
├── web/
│   ├── __init__.py              # Flask app factory (create_app)
│   ├── blueprints/              # 11 blueprints: admin, batch_scoring, companies, costs, dashboard, detections, jobs, pipeline, profile, settings, sync
│   ├── templates/               # 36 Jinja2 templates (base.html + partials)
│   ├── claude_client.py         # Anthropic wrapper with cost tracking + budget gating
│   ├── haiku_scorer.py          # Fast-filter scoring
│   ├── sonnet_evaluator.py      # Deep evaluation with fit analysis
│   ├── scheduler/               # APScheduler background jobs (split S7a 2026-05-06: __init__ lifecycle + _pidfile + _ollama + _factories + _jobs + _runners + _sync)
│   ├── pipeline_detector/       # Multi-signal email classification (split S7b 2026-05-06: __init__ + _constants + _gmail + _signals + _db + _processing)
│   ├── ats_scanner/             # ATS platform scanner (split S7c 2026-05-06: __init__ + _upsert + _probe + _promote + _run + _run_html)
│   ├── careers_crawler/         # Multi-tier careers-page crawler (split S7e 2026-05-06: __init__ + _title_filters + _api_cache + _static_tier + _playwright_tier + _ai_nav_tier + _tier_cache + _persistence + _scoring)
│   ├── migrations/              # Per-version migration modules (split S6 2026-05-06; m001..m048 + _gate, _runner, _post_hooks, types)
│   ├── _http_constants.py       # Shared HTTP _HEADERS / _TIMEOUT (extracted S7e onset; consumed by careers_crawler + enrichment_tiers)
│   ├── pipeline_runner.py       # Orchestrates ingestion + scoring + detection
│   ├── db_helpers.py            # Per-request g.db pattern
│   ├── db_migrate.py            # Migrations driver (slim post-S6: discovers + applies modules from migrations/)
│   └── stale_detector.py        # Nightly stale job detection (own DB connection)
├── parsers/                     # Email parsers: linkedin, glassdoor, indeed (stub), ziprecruiter
├── sources/                     # gmail_source.py, serpapi_source.py, thordata_source.py, dataforseo_source.py, portal_search_source.py
├── models.py                    # Job dataclass with dedup_key
├── db.py                        # Original CLI-era DB module (module-level functions take Connection); split into db/ package by Reconciliation R3 (concurrent session in progress 2026-05-06)
└── config.py                    # YAML config loader (fail-fast, no defaults)
tests/
├── conftest.py                  # Fixtures: app factory, test DB, mocked Claude client
└── test_*.py                    # ~90 test files (canary + invariant suites added in S6/S7a/S7b/S7e)
```

## Architecture Decisions That Matter

These decisions are documented in `.planning/STATE.md` and recur constantly:

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
- `CREATE TABLE IF NOT EXISTS` for idempotent migration on both empty and populated DBs
- Stale detector creates own sqlite3 connection (thread-safe for APScheduler, not Flask g.db)
- sort_by validated against Python allowlist before SQL interpolation (no parameterized column names in SQLite)

**Testing**:
- `create_app()` accepts `config=` dict for test isolation
- Temp DB per test; mocked Claude client at injection point
- `conftest.py` has fixtures for app factory, test DB, mock Claude

**Scoring**:
- cost_gate returns bool — callers decide whether to raise BudgetExceededError
- Sonnet skips if jd_full absent (no cost without full JD)
- Batch score skips already-scored jobs (haiku_score IS NOT NULL)

## Planning Documentation

This project uses the GSD framework. Key docs:
- `.planning/ROADMAP.md` — All phases complete, milestone history
- `.planning/STATE.md` — Current state, 100+ architectural decisions
- `.planning/codebase/` — ARCHITECTURE.md, CONVENTIONS.md, CONCERNS.md, STACK.md, TESTING.md

## Current Status

- **Phase 1 (Foundation)**: Complete — 11/11 plans, 36/36 must-haves verified
- **Phase 2 (AI Scoring)**: Complete — 5/5 plans
- **Phase 3 (Pipeline Automation)**: Complete — 2/2 plans
- **Phase 4 (Resume Generation)**: Removed (public-repo cleanup, 2026-05) — resume_generator, drive_uploader, drive_status, docx_formatter, resume_feedback, resume_validator, resume_style_guide, resume_multi_version, resume_review blueprint, feedback blueprint, guidelines blueprint all deleted; Migration 47 dropped resume_generations / resume_preferences_detected / resume_upload_reviews tables.
- **Phase 5 (Intelligence)**: Removed (public-repo cleanup, 2026-05) — interview_prep, rejection_analyzer, rejection_patterns, notifier all deleted; Migration 48 dropped interview_preps / rejection_reports / rejection_pattern_reports tables and the jobs.rejection_reviewed column. AI career navigator (`ai_career_navigator.py`) was retained as a Tier-4 crawler fallback (16 cached recipes, ~10 active companies use ai_navigate/ai_replay tier).
- **Portfolio Cleanup (Track 1, Stage 1 link-shareable, 2026-05)**: Sessions 0-4 complete (`portfolio/s0-baseline` through `portfolio/s4-readme-skeleton`). Stage 1 gate held open by 3 user-action items (codecov authorization, hero GIF, "Why I Built It" narrative) — see `.planning/portfolio-cleanup/STAGE-1-GATE-BLOCKERS.md`.
- **Portfolio Cleanup (Track 2, Lead/Staff depth, 2026-05)**: Sessions 5, 6, 7a, 7b, 7c, 7e complete (tags `portfolio/s5-typecheck-baseline` through `portfolio/s7e-careers-crawler-split`). Session 7d (db.py split) was SKIPPED in the original execution and is being recovered by Reconciliation Plan v1 Session R3 (concurrent at time of writing). Reconciliation track R0–R8 is in flight (`.planning/PORTFOLIO_RECONCILIATION_PLAN.md`). Sessions 8–11 unstarted.

## Verification Standards

When verifying phase completion or running `/gsd:verify-work`, self-check everything automatable before flagging items as human-needed.

**Self-check (do NOT flag as human-needed):**
- Route returns correct HTTP status — use Flask test client or curl
- HTML response contains expected elements (IDs, classes, buttons, forms)
- HTMX attributes are correctly wired (hx-get, hx-target, hx-swap)
- Form submissions return expected responses
- Fragment routes check HX-Request header
- Collapsed/expanded sections exist in markup
- Polling endpoints return correct fragments
- Template variables match route context

**Only flag as human-needed:**
- Visual/aesthetic judgments (spacing, colors, sizing, "looks broken")
- Real browser JS execution (HTMX swap animations, SortableJS drag behavior)
- Cross-element visual layout rendering (does two-column actually render correctly?)
- Subjective UX quality ("is this intuitive?")

**Use these agents/skills proactively at the right stages:**
- `/systematic-debugging` — when encountering any bug or test failure, BEFORE proposing fixes

## Custom Agents, Skills, and Hooks

- `.claude/agents/htmx-reviewer.md` — Proactive HTMX+Jinja2+Flask review agent. Use when modifying templates or hx-* attributes.
- `.claude/agents/flask-template-auditor.md` — Audits Jinja2 template variable usage against Flask route context. Catches silent failures where routes pass variables templates never render, or templates reference variables routes don't provide. Use after editing any .html template or blueprint route.
- `.claude/agents/arch-reviewer.md` — Reviews code changes for architectural consistency with .planning/ docs. Catches anti-patterns and component boundary violations.
- `.claude/skills/uat-check/SKILL.md` — Post-phase UAT gap analysis against ROADMAP success criteria.
- `/brainstorming` — Explore intent, requirements, and design before implementing features.
- `/systematic-debugging` — Structured debugging before proposing fixes for bugs or test failures.

## Conventions

- Always use Context7 MCP when working with library APIs, especially for: APScheduler, HTMX, Anthropic SDK, Flask, jinja2-fragments
- Snake_case everywhere (files, functions, variables). PascalCase for classes only.
- No formatter or linter configured. PEP 8 followed implicitly.
- Absolute imports from `job_finder` package root.
- No barrel files; `__init__.py` files are mostly empty.
- Blueprint routes use `strict_slashes=False`.
- Jinja2 custom filters: `from_json`, `urlencode`, `format_description`, `relative_date`.

## User Data Files (Not Tracked in Git)

These files contain personal data and API keys. They are `.gitignore`d and must be backed up manually (`bash backup_userdata.sh`). Example templates are tracked for schema reference.

| File | Template | Purpose |
|------|----------|---------|
| `config.yaml` | `config.example.yaml` | App config, API keys, profile targets |
| `experience_profile.json` | `experience_profile.example.json` | Career history (positions / skills / education) for Sonnet fit-scoring personalization |

**config.yaml must ONLY be modified with the Edit tool (surgical string replacement), NEVER with the Write tool (full-file overwrite).** This file has been accidentally wiped 3 times by full-file rewrites that intended to change a single value. The settings save route (`_write_config`) is safe because it reads→merges→writes. The risk is Claude/GSD execution doing full-file writes.

## Environment Variables

- `OLLAMA_EXE` (optional) — absolute path to `ollama.exe`. Only needed if Ollama is installed somewhere other than the Windows default (`%LOCALAPPDATA%\Programs\Ollama\ollama.exe`) and not on PATH. The scheduler auto-starts Ollama at app boot so the nightly agentic backfill (3:30 AM) has a live service.

## Don't

- Don't add an ORM — raw SQL is intentional for this project's scale
- Don't add a build step or bundler — Tailwind CDN + HTMX CDN is intentional
- Don't use `--no-verify` or skip hooks
- Don't use APScheduler 4.x (breaking async API)
- Don't use `204` for HTMX fragment responses (use `200`)
- Don't create separate detail pages — inline expansion via HTMX is the pattern
- Don't use `hx-include` with CSS selectors for form fields — use proper `<form>` wrappers
