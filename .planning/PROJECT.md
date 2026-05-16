# Job Cannon

## What This Is

A personal job search command center. Flask web app (localhost:5000) that aggregates jobs from Gmail alerts (LinkedIn, Glassdoor, ZipRecruiter), SerpAPI, Thordata, DataForSEO, and live ATS scanners (Greenhouse / Lever / Ashby / Workday / SmartRecruiters), scores them through a single-tier ordinal rubric routed through a multi-provider cascade (Ollama qwen2.5:14b → Groq → Cerebras → Gemini → Anthropic paid fallback), and tracks application pipeline status. Single-user, local-only.

## Core Value

Surface the best-fit jobs fast and keep the application pipeline visible — every job gets scored, every status change gets tracked, nothing falls through the cracks.

## Current Milestone: v5.0 Public Release Foundation — Cascade Audit + Strangerify P1 + PyPI

**Goal:** Optimize the multi-provider cascade for the 6 non-scoring LLM callsites via a data-driven shadow-replay audit, then refactor the app so a stranger can install via `pipx install job-cannon` (or `git clone && uv sync`), complete an onboarding wizard with their own Gmail + provider, and score real jobs. Absorbs P1 (Strangerify) + P2 (PyPI / pipx) from the public-release thrust. P3 (cross-platform installers) and P4 (launch) deferred to v5.1+.

**Target features:**

- Local cascade audit: shadow-replay against an OpenRouter DeepSeek-V3.2 judge across the 6 non-scoring callsites (parse_structured_fields, find_careers_url, extract_jobs, description_reformat, company_research, ai_nav_discovery), producing a per-callsite routing recommendation (Case A single cascade or Case B `purpose_overrides`)
- `scoring_costs.schema_valid` telemetry (Migration 49) + per-callsite purpose attribution (split `careers_scrape` → `find_careers_url` + `extract_jobs`)
- 1-week production canary post-rewire; ≥80% Anthropic-tail spend reduction target
- Cross-platform user-data dirs via `platformdirs`; `onboarding_state` table (Migration 50) + before-request redirect
- Provider abstraction overhaul: `_TIER_DEFAULTS` (flat) → `_PROVIDER_DEFAULTS` (nested provider→workload→model); workload classes `quick`/`score`/`triage` replace capability tiers
- Three new providers: `claude_code_cli` (subscription-leveraged), `gemini_cli`, `local_bundled` (llama-cpp-python + GGUF); auto-detection module probes `claude`/`gemini`/`ollama` CLIs
- Triage gate: optional binary pre-scoring filter; `enabled='auto'` resolves per primary provider; dashboard filter reuses `pipeline_status_history.source='triage_filter'` (no migration)
- IMAP source (`imapclient`) as default ingest; `gmail_source.py` stays as opt-in
- Resume parser: PDF/DOCX → LLM-extracted profile (pdfplumber + python-docx)
- 7-step onboarding wizard (Flask blueprint): welcome → provider → credentials → resume → IMAP → schedule → done
- Update-check banner: cached GitHub Releases ping; no auto-update
- Personal-data audit (prompts/fixtures/configs genericized); License → AGPL-3.0; new docs: `PRIVACY.md`, `AUP.md`, `SECURITY.md`, `INSTALL.md`
- PyPI name `job-cannon` registered + trusted publishing; `.github/workflows/release.yml` builds + publishes sdist + wheel on tag push
- `pipx install job-cannon` validated on Windows / macOS / Linux; README restructured around three install paths
- P2 exit gate: ≥5 strangers install + run successfully

**Key context:**

- **Sequencing:** Cascade audit must precede Strangerify's workload-tier overhaul. Audit operates on the current `low`/`mid`/`high` tier system (post-v4.0 rename); Strangerify's `_PROVIDER_DEFAULTS` absorbs the audit's Case A/B decision. Strangerify → PyPI (need stable provider abstraction + onboarding before going public).
- **Two renames in one milestone:** `haiku`/`sonnet`/`opus` → `low`/`mid`/`high` already shipped post-v4.0 (validated retroactively this milestone); Strangerify renames again to workload-class semantics (`quick`/`score`/`triage`). Roadmapper to document dependencies.
- **`config.yaml` is gitignored + Edit-tool-only** (3 prior wipes from full-file writes). Audit + Strangerify both rewrite the providers block; sequenced execution prevents merge pain.
- **Canary timing:** the audit's 1-week canary (cascade Chunk 5) may be sequenced inside Strangerify's workload-tier phase to avoid two config rewrites — roadmapper decides.
- **Phase numbering** continues from v3.0's Phase 34 (v4.0 used tracks). v5.0 starts at Phase 35.
- **Out of scope this milestone:** P3 cross-platform installers (Briefcase/PyInstaller, bundled local model in installer, code-signing-bypass docs); P4 launch (HN/Reddit, hero GIF, demo video); macOS Gatekeeper / Windows SmartScreen UX; Linux-specific install paths beyond pipx; iOS/Android; SaaS/hosted version.

**Reference plans:**
- `.planning/plans/2026-05-13-local-cascade-audit-plan.md` (cascade audit; 5 chunks, ~27 tasks)
- `.planning/public-release/PLAN-P1.md` (Strangerify; 6 chunks)
- `.planning/public-release/DESIGN.md` (full P1–P4 design; P2 scope absorbed, P3/P4 deferred)

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

- ✓ Unified `job_scorer.py` emitting `JobAssessment` (six ordinal 1-5 sub-scores + Python-derived 4-way classification) — v3.0 Phase 34
- ✓ Phase 33 site-fitness shootout: 6-candidate × 9-site per-site winner matrix; qwen2.5:14b operational winner — v3.0 Phase 33
- ✓ Grammar-constrained Ollama decoding via `format=<schema dict>`; deletes `_schema_to_field_instructions` / `_schema_to_example` / most of `_sanitize_output` — v3.0 Phase 33 (SCORER-11)
- ✓ Deterministic Ollama inference params (`temperature=0`, `seed=42`, `num_ctx=8192`) — v3.0 Phase 33 (SCORER-12)
- ✓ Migration 40 (additive: `classification`, `sub_scores_json`, `scoring_model`) + Migration 41 (destructive: drop `haiku_score`/`haiku_summary`/`sonnet_score` + `idx_jobs_haiku_score`, backup-gated) — v3.0 Phase 34 (MIGRATE-01..05)
- ✓ Complete two-tier deletion: `haiku_scorer.py`, `sonnet_evaluator.py`, `score_calibration.py`, `calibration_ollama_*.json`, `scripts/calibration_refit.py`, `_apply_calibration`, `PROMPT_VARIANTS`, `_run_batch_haiku_bg`, `_run_batch_sonnet_bg`, `persist_haiku_score`, `persist_sonnet_score` — v3.0 Phase 34 (COLLAPSE-01..11)
- ✓ Config collapse `providers.haiku` + `providers.sonnet` → `providers.scoring` in `config.yaml` and `config.example.yaml` — v3.0 Phase 34 (COLLAPSE-08)
- ✓ All 16 downstream consumer call sites (queries, batch_scoring, dashboard, templates, resume/interview gating, pipeline_runner summary keys) migrated to unified scorer — v3.0 Phase 34 (CONSUMERS-01..16)
- ✓ Wholesale rescore of 3922 existing jobs under G1-G4 noise-floor gates; distribution 2212 apply / 1059 consider / 651 reject / 546 NULL — v3.0 Phase 34 (MIGRATE-05)
- ✓ TESTING-propagation fix in `create_app()` — closes race where TESTING-guarded background threads ran under pytest — v3.0 close (2026-04-24)

- ✓ Phase 4 (Resume Generation) modules + tables removed via Migration 47 — resume_generator, drive_uploader, drive_status, docx_formatter, resume_feedback, resume_validator, resume_style_guide, resume_multi_version + resume_review/feedback/guidelines blueprints all deleted — v4.0 Track A
- ✓ Phase 5 (Intelligence) modules + tables removed via Migration 48 — interview_prep, rejection_analyzer, rejection_patterns, notifier deleted; jobs.rejection_reviewed column dropped; ai_career_navigator.py retained as Tier-4 crawler fallback — v4.0 Track A
- ✓ Portfolio Track 1 (S0-S4): pre-cleanup baselines committed; tracked secrets purged; pyproject.toml canonical + uv.lock; `job-cannon` console script + run.py shim; README rewritten for Lead/Staff portfolio audience; CHANGELOG seeded — v4.0 Track B
- ✓ Portfolio Track 2 (S5-S7e): seven module splits — `migrations/`, `scheduler/`, `pipeline_detector/`, `ats_scanner/`, `db/`, `careers_crawler/` + shared `_http_constants.py` — v4.0 Track C
- ✓ Reconciliation R1-R8: docs/test-integrity/typecheck baselines reconciled post-splits; `.githooks/pre-commit` fails closed when binary missing; gitleaks bump; orphan interview_prep color mapping caught + removed — v4.0 Track D

- ✓ Tier label rename: `haiku` / `sonnet` / `opus` → `low` / `mid` / `high` across schema, migrations, call sites, UI, tests, docs (`53c19e9`, `fb948b6`, `88d165a`, `bffe056`) — v5.0 (retroactively absorbed; shipped 2026-05-13)
- ✓ Add-job-from-listing-URL modal with enrichment on dashboard (`03afade`) — v5.0 (retroactively absorbed; shipped 2026-05-13)
- ✓ Add-job-manually form: `GET/POST /jobs/add` (`c6a9d01`) — v5.0 (retroactively absorbed; shipped 2026-05-13)
- ✓ ATS identity reconciliation: URL evidence → live verify → hit (`43bb89e`) — v5.0 (retroactively absorbed; shipped 2026-05-13)
- ✓ Uncapped enrichment backfill + diagnostic tooling (`e982724`) — v5.0 (retroactively absorbed; shipped 2026-05-13)

### Active

- v5.0 cascade audit + Strangerify P1 + PyPI requirements (defined in REQUIREMENTS.md after roadmap creation)

## Shipped Milestone: v3.0 Single-Tier Ordinal Scoring

**Goal:** Collapse the vestigial Haiku/Sonnet two-tier architecture into a single-pass, Ollama-native job scorer emitting ordinal rubric output, eliminating the calibration infrastructure and all cost-era scaffolding.

**Target features:**
- Local LLM site-fitness survey across 9 active AI call sites (scoring, extraction, HTML reasoning, transformation) with candidate shortlist benchmarked against site-appropriate metrics; output is a per-site winner matrix that drives model selection for the rewrite
- Greenfield unified scorer emitting `JobAssessment` (ordinal 1-5 sub-scores per dimension + apply/consider/skip/reject classification + structured rationale), schema-validated, no calibration layer
- DB schema migration — drop/rename tier-specific score columns, add rubric columns, migrate all downstream consumers (~15+ call sites in `careers_crawler`, `batch_scoring`, `dashboard`, filters, and test suite)
- Deletion of Haiku tier (`haiku_scorer.py`, `score_and_persist_haiku`, borderline re-eval path), calibration infrastructure (`score_calibration.py`, `calibration_ollama_*.json`, `_apply_calibration`), cost-era scaffolding (`_CLIClientStub` duplication, `use_dispatcher` branches, tier-keyed provider configs)
- Test modernization — migrate ~15-20 Haiku-referencing test files to the unified scorer; update fixtures, mocks, and integration tests

**Key context:**

- **Design decision (ordinal rubric over continuous 0-100)** is a direct response to a diagnosed LLM failure mode: qwen2.5:14b produces raw scores of 62-68 for Anthropic baselines spanning 9-72 (63-point spread at one raw value). Calibration cannot repair bimodal raw-to-baseline relationships; isotonic regression is one-to-one by construction. The n=48 refit in commit d80f486 established MAE floor of 13.66 at zero bias — the limit is intrinsic to continuous numeric judgment on sub-100B local models, not a fit inadequacy. Ordinal 1-5 ratings + classification are tasks LLMs reliably handle.
- **All nine in-scope sites** use the same capability profile (read messy input, emit structured output, schema adherence, factual grounding). A single mid-tier generalist likely wins everything; cascade config may collapse to one model with CLI fallback.
- **Four sites explicitly out of scope** (vestigial, non-functional in weeks): resume generation, interview prep, rejection analysis, profile extraction. Separate backlog cleanup, not part of this milestone.
- **Baseline contamination mitigation**: 52% of `sonnet_score` rows and 27% of `haiku_score` rows are Ollama-origin post-cascade-flip. Scoring-site baselines must be filtered to `scoring_provider='anthropic'` during the survey; rescoring not required (Phase 2 schema migration resolves structurally).
- **Hardware ceiling**: RTX 4070 Ti SUPER, 16 GB VRAM, 64 GB RAM. Constrains candidate models to ~27B dense at Q4 or smaller; CPU offload available for 32B.
- **Planning discipline**: user directive — *"painstakingly methodical; any ambiguity or lazy specification is guaranteed to come back and bite us."* Each phase plan must specify acceptance criteria concretely, list all downstream call sites by file and line, and document rollback at each migration step.

**Phase numbering:** continues from v2.0 (Phase 32). v3.0 phases start at Phase 33.

### Out of Scope

- Deployment, Docker, CI/CD — local-only app
- ORM — raw SQL is intentional at this scale
- Build step or bundler — Tailwind CDN + HTMX CDN is intentional
- APScheduler 4.x — breaking async API, pinned <4.0

## Current State

v5.0 started 2026-05-13. Defining requirements + roadmap for cascade audit + Strangerify P1 + PyPI. Phase numbering continues from v3.0's Phase 34; v5.0 starts at Phase 35.

**Pre-v5.0 baseline (carried from v4.0):** Repo is in public-shareable state: Phase 4/5 modules removed via Migrations 47+48; `ai_career_navigator.py` retained as Tier-4 crawler fallback. Seven module splits delivered (`migrations/`, `scheduler/`, `pipeline_detector/`, `ats_scanner/`, `db/`, `careers_crawler/`) with split-sentinel invariant tests at every boundary. Type-check baselines (mypy + pyright) tracked through every split. `pyproject.toml` is canonical with `uv.lock` committed; `job-cannon` console script is the install entry point.

**Post-v4.0 work absorbed into v5.0 as validated requirements (shipped 2026-05-13):** tier label rename (`haiku`/`sonnet`/`opus` → `low`/`mid`/`high`), add-job-from-URL modal, add-job-manually form, ATS identity reconciliation, uncapped enrichment backfill + diagnostic tooling.

Cascade order at v5.0 start: Ollama qwen2.5:14b → Groq → Cerebras → Gemini → Anthropic (paid fallback). Single-tier ordinal `job_scorer.py` remains the sole scoring code path. Live DB at `user_version=51`. v5.0 will: (a) audit and rewire the cascade for non-scoring callsites, then (b) overhaul the tier system to workload classes (`quick`/`score`/`triage`) with three new providers (`claude_code_cli`/`gemini_cli`/`local_bundled`).

## Context

- job-cannon is the single source of truth (job-finder retired after v1.1)
- ~90 test files; 2412 tests green at v3.0 close (count has drifted post-v3.0 with Phase 4/5 module removal)
- All v3.0 surface features operational — multi-source ingestion (Gmail / SerpAPI / Thordata / DataForSEO / portal-search / live ATS scanners across Greenhouse / Lever / Ashby / Workday / SmartRecruiters), unified ordinal scoring with multi-provider cascade, pipeline tracking
- Live DB at `user_version = 41` (Migration 41 destructive column drop applied 2026-04-23 with backup gate; post-v3.0 migrations through 51 also applied per recent migrations/m001..m051 split)

## Constraints

- **Tech stack**: Python 3.13, Flask 3.1, HTMX 2.x, SQLite, Jinja2 — no changes
- **Single-user**: No auth, no multi-tenancy considerations
- **Source repo**: job-finder at `<other-repo>` (retired, read-only reference)

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Raw SQL, no ORM | Intentional for single-user scale, full control | ✓ Good |
| Two-tier AI scoring | Volume handling (Haiku cheap) + quality (Sonnet selective) | ⚠️ Retired in v3.0 — collapsed to single-tier ordinal scorer; calibration could not repair bimodal raw-to-baseline relationships on local models |
| Single-tier ordinal rubric (v3.0) | Six 1-5 sub-scores + Python-derived classification; literature-settled per arXiv 2601.03444 (ICC 0.853 at 0-5 vs 0.840 at 0-100); model emits ordinals only, classification is deterministic Python | ✓ Good |
| Grammar-constrained Ollama decoding (v3.0) | `format=<schema dict>` (Ollama v0.5+) makes invalid output physically impossible; deletes `_schema_to_field_instructions`, `_schema_to_example`, most of `_sanitize_output` | ✓ Good |
| `(provider, model)` persisted identity (v3.0) | `jobs.scoring_model` alongside `scoring_provider`; `(provider, tier)` keying was the v2.0 calibration-invalidation bug root cause | ✓ Good |
| Multi-provider cascade with free-tier primary (v2.0+) | Ollama qwen2.5:14b → Groq → Cerebras → Gemini → Anthropic (paid fallback); only Anthropic-fallback usage counts against `scoring.monthly_budget_usd` | ✓ Good |
| HTMX + Tailwind CDN | No build step, fast iteration, good enough for local app | ✓ Good |
| Surgical port from job-finder | Preserves cannon-only assets, dependency-ordered waves | ✓ Good |
| Module-level db functions over class | Simpler API, matches Flask per-request connection pattern | ✓ Good |
| Adapter lambdas for scheduler factories | Non-matching function signatures get lambda wrappers | ✓ Good |
| ScoringResult NamedTuple over raw dict | Type-safe scorer returns, attribute access | ✓ Good |
| Direct data file copy from job-finder | No intermediate format, config merged with Edit tool | ✓ Good |
| Infrastructure phases skip discuss | Doc/migration phases have no design decisions | ✓ Good |
| Phase 4/5 deletion over deprecation (v4.0) | Public-repo readiness; both feature sets vestigial since v1.0 inheritance and not actively used; Migrations 47+48 destructive over flag-disabled | ✓ Good |
| Retain `ai_career_navigator.py` post-Phase-5 removal (v4.0) | ~10 companies use ai_navigate/ai_replay Tier-4 crawler fallback; deletion would degrade crawler coverage; the module is the only Phase-5 code still load-bearing | ✓ Good |
| Module-splits-as-tracks instead of formal numbered phases (v4.0) | Work was already in flight as out-of-band cleanup when GSD ceremony resumed; retroactive numbering would conflict with portfolio/sN-* git tag history; tracks A/B/C/D map cleanly to the four parallel execution lines | ✓ Good |
| Split-sentinel invariant tests at every module split boundary (v4.0) | Catches helper-function drift across the split surface; gives a single-file regression target reviewers can run to verify package API stability | ✓ Good |
| `pyproject.toml` + `uv.lock` canonical (v4.0) | Removed `requirements.txt` + `pytest.ini`; CI on `uv sync`; matches user's `uv`-first global tooling preference | ✓ Good |

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
*Last updated: 2026-05-13 — milestone v5.0 started (Public Release Foundation: Cascade Audit + Strangerify P1 + PyPI)*
