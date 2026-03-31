# Job Cannon

## What This Is

A personal job search command center. Flask web app (localhost:5000) that aggregates jobs from Gmail alerts (LinkedIn, Glassdoor, ZipRecruiter) and SerpAPI, scores them with a two-tier Claude AI pipeline (Haiku fast filter, Sonnet deep evaluation), tracks application pipeline status, and generates tailored resumes via Google Docs. Single-user, local-only.

## Core Value

Surface the best-fit jobs fast and keep the application pipeline visible — every job gets scored, every status change gets tracked, nothing falls through the cracks.

## Requirements

### Validated

- Job ingestion from Gmail alerts and SerpAPI with deduplication (v1.0 Phase 1)
- HTMX-driven job board with filters, accordion expand, inline detail (v1.0 Phase 1)
- Two-tier AI scoring: Haiku fast filter + Sonnet deep evaluation with budget gating (v1.0 Phase 2)
- Cost tracking dashboard with per-model breakdown (v1.0 Phase 2)
- Pipeline status tracking with manual transitions (v1.0 Phase 3)
- Email-based pipeline detection (rejection, interview, offer patterns) (v1.0 Phase 3)
- Interview prep auto-generation on status change to "applied" (v1.0 Phase 3)
- ✓ db.py rewrite: module-level functions with explicit columns, smart merging — v1.1
- ✓ Scoring orchestrator centralizes haiku/sonnet flow — v1.1
- ✓ Safety hardening: API timeout, pricing fallback, Gmail cap, pipeline validation — v1.1
- ✓ Multi-select status filter with checkbox pill UI — v1.1
- ✓ Blueprint improvements: HX-Request guards, batch timeout, safe params — v1.1
- ✓ Dead code removal: main.py, utils.py, output/ (scoring/ retained — active) — v1.1
- Tailored resume generation with Google Drive upload, multi-variant synthesis, feedback + style guide (inherited from job-finder)
- Interview prep auto-generation with Opus, rejection pattern analysis, Windows toast notifications (inherited from job-finder)
- ✓ Planning docs corrected to reflect all features operational — v1.2
- ✓ Data migration from job-finder complete (8 files, config merged, schema verified) — v1.2
- ✓ Full validation: 1359 tests pass, app renders 592 real jobs — v1.2

- ✓ Cascade config schema with `fallback_chain` list parsed by `resolve_provider_config()` — v2.0
- ✓ Daily rate limit tracker with in-memory counters bootstrapped from scoring_costs DB — v2.0
- ✓ Cascade logic in `call_model()` — iterate providers, skip exhausted/unavailable, handle 429s — v2.0
- ✓ Per-model prompt variant support threaded through cascade config — v2.0
- ✓ DB migration for `scoring_provider` column on jobs table with provider attribution threading — v2.0
- ✓ Fewshot examples moved from eval CLI into production sonnet evaluator — v2.0
- ✓ Full test suite for cascade, rate limiting, backward compatibility, and provider attribution — v2.0

### Active

(No active requirements — planning next milestone)

### Out of Scope

- Deployment, Docker, CI/CD — local-only app
- ORM — raw SQL is intentional at this scale
- Build step or bundler — Tailwind CDN + HTMX CDN is intentional
- APScheduler 4.x — breaking async API, pinned <4.0

## Current State

v2.0 shipped (2026-03-30). Cascading free provider routing replaces paid Sonnet evaluation ($0.011/job → $0.00/job) for daily volumes under 400. Cascade order: Cerebras qwen-3-235b (primary) → Groq llama-4-scout → Ollama qwen2.5:14b → Anthropic Sonnet (paid fallback). 1815 tests pass. 592+ real jobs in database. Every scored job records which provider produced its score.

**Shipped in v2.0:** 4 phases, 7 plans. Cascade config parsing, daily rate limiting, dispatch loop with 429 handling, fewshot production prompts, per-model prompt variants, provider attribution to DB, production config wiring.

## Context

- job-cannon is the single source of truth (job-finder retired after v1.1)
- 52,381 LOC Python across job_finder/ and tests/
- 1786+ tests, all passing
- All features operational — resume generation, interview prep, rejection analysis, scoring, pipeline tracking

## Constraints

- **Tech stack**: Python 3.13, Flask 3.1, HTMX 2.x, SQLite, Jinja2 — no changes
- **Single-user**: No auth, no multi-tenancy considerations
- **Source repo**: job-finder at `<other-repo>` (retired, read-only reference)

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Raw SQL, no ORM | Intentional for single-user scale, full control | ✓ Good |
| Two-tier AI scoring | Volume handling (Haiku cheap) + quality (Sonnet selective) | ✓ Good |
| HTMX + Tailwind CDN | No build step, fast iteration, good enough for local app | ✓ Good |
| Surgical port from job-finder | Preserves cannon-only assets, dependency-ordered waves | ✓ Good |
| Module-level db functions over class | Simpler API, matches Flask per-request connection pattern | ✓ Good |
| Adapter lambdas for scheduler factories | Non-matching function signatures get lambda wrappers | ✓ Good |
| ScoringResult NamedTuple over raw dict | Type-safe scorer returns, attribute access | ✓ Good |
| Direct data file copy from job-finder | No intermediate format, config merged with Edit tool | ✓ Good |
| Infrastructure phases skip discuss | Doc/migration phases have no design decisions | ✓ Good |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd:transition`):
1. Requirements invalidated? Move to Out of Scope with reason
2. Requirements validated? Move to Validated with phase reference
3. New requirements emerged? Add to Active
4. Decisions to log? Add to Key Decisions
5. "What This Is" still accurate? Update if drifted

**After each milestone** (via `/gsd:complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-03-30 after v2.0 milestone shipped (Cascading Free Provider Routing)*
