# Changelog

All notable changes to this project are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project follows [Semantic Versioning](https://semver.org/).

## [5.1.0] — 2026-06-11

The first PyPI release. Release polish (demo mode, installers, screenshots),
autoheal default-on, primary-source resolution, and a posted-date
reliability wave.

### Added

- **Demo mode** — `job-cannon --demo` boots against a throwaway database
  seeded with ~30 fictional sample jobs on the next free port. Try the app
  without configuring Gmail or any provider. (#348)
- **Bootstrap one-liners** — pipe-safe `bootstrap.ps1` / `bootstrap.sh`
  end-user installers (PowerShell `irm | iex`, `bash -c "$(curl …)"`) that
  install pipx if missing, then `pipx install job-cannon`. (#355) The
  contributor `install.sh` gained Linux parity. (#356)
- **Windows installer** — PyInstaller-frozen app wrapped in an Inno Setup
  per-user installer (`JobCannon-Setup.exe`), built by a release workflow
  and attached to GitHub Releases with checksums. (#357)
- **Company tracking** — track-company action on any job plus a
  suggested-companies card sourced from your high-scoring history. (#354)
- **No-provider banner + budget-skip eventing** — the dashboard warns when
  no AI provider is reachable, and budget-gated skips surface as activity
  events instead of silence. (#349)
- **Screenshots + demo GIF** — captured via `scripts/capture_screenshots.py`
  against demo mode; embedded in the README. (#350)
- **2FA enrollment walkthrough** — the wizard now walks through enabling
  2-Step Verification before the App-Passwords step, matching Google's
  current UI. (#353)
- **Empty-board onboarding state** — an empty database now shows
  getting-started guidance, distinct from the "filters excluded everything"
  state. (#352)
- **Autoheal Phase D** — episodic attempt semantics with rollback, email
  dual-parse + careers generic-shadow guards, per-company careers re-keying
  with structural break detection, an upstream contribution channel
  (review-first, nothing auto-uploads), and `heal_enabled` flipped to
  default-on. Migrations m090–m091. (#337–#339, #341, #344, #345)
- **Primary-source resolution** — Apply buttons prefer the primary ATS
  posting over aggregator copies: precedence-ranked `direct_url`,
  strict-gated data merge, a scheduled company-batched resolver, a
  quick-tier LLM tie-breaker, and staleness decay that clears expired
  direct URLs. (#340, #342, #343, #346)
- **`posted_date` provenance** — `posted_date_precision` column with
  precedence-based upsert, Workday "Posted N Days Ago" parsing, and
  recency filters/sort based on `COALESCE(posted_date, first_seen)`.
  (#369–#371)
- **Zero-key free-portal path** — the wizard surfaces the no-API-key
  portal-search source, default on. (#335)
- **Real provider cost accounting** — Groq / Cerebras / OpenRouter /
  Gemini adapters compute actual `cost_usd` from token usage. (#334)
- **Migration safety** — downgrade guard, automatic pre-migration database
  backup, and a friendly failure page instead of a stack trace. (#333)
- **macOS validation checklist** — `docs/TESTING-MACOS.md`, a ~20-minute
  tester checklist (install, tray, Keychain, IMAP, local-ai). (#358)

### Changed

- **Python floor lowered to 3.12** (was 3.13) for broader distro
  compatibility. (#331)
- **README restructured** for a mixed audience — end-user quick start
  first, contributor detail later, FAQ added, stale counts softened. (#351)
- **Tray mode prints a startup banner and opens the browser** instead of
  starting silently. (#332)
- Renamed vestigial tier routing strings `haiku` / `sonnet` / `opus` to
  `low` / `mid` / `high` across config, dispatcher, DB `enrichment_tier`
  values (migration 50), and UI. Renamed `scoring.haiku_threshold` to
  `scoring.candidate_score_threshold`. Removed deprecated
  `/dashboard/batch-score/haiku/start` and `/dashboard/batch-score/sonnet/start`
  routes; consolidated `user_activity` batch-score actions (migration 51).
  Dashboard batch scoring is a single control. `config.yaml` legacy keys
  auto-migrate on load via ruamel.yaml round-trip.

### Fixed

- **Poisoned `posted_date` values** — naive-UTC enforced at the upsert
  boundary (m093), provably-wrong dates (`posted_date > first_seen`)
  cleared (m094), and Greenhouse / SmartRecruiters now read first-posted
  fields instead of last-updated. (#366–#368)
- **Windows user-data path docs** — five doc sites claimed
  `%APPDATA%\JobCannon`; the app actually uses `%LOCALAPPDATA%\JobCannon`
  (platformdirs `roaming=False`). (#358)
- **Tray ignored `--port`** and the `/__jc_health` endpoint was reachable
  in production mode; both fixed during installer work. (#357)

## [5.0.0] — 2026-06-10

The public-launch milestone. Packages on PyPI, onboarding wizard, OS keyring,
IMAP (no OAuth), system tray, autoheal, and this audit wave.

### Added

- **Onboarding wizard** (8-step) — auto-detects installed $0 AI providers
  (Ollama, Claude Code CLI, Gemini CLI), guides Gmail IMAP setup, and writes
  secrets to the OS keyring. No manual YAML editing required on first install.
- **OS keyring integration** — IMAP app password and provider API keys are
  stored in Windows Credential Manager / macOS Keychain / Linux Secret Service
  via `job_finder.secrets.get_secret()` (env → keyring → config.yaml fallback).
- **Gmail via IMAP** — replaces the OAuth 2.0 / Google Cloud Console flow.
  Uses an app password (`sources.imap.email` + `app_password`); no GCP project
  or OAuth consent screen required. UNSEEN search scoped to known senders;
  `BODY.PEEK[]` avoids marking messages as read.
- **System tray app** (`pystray`) — menu-driven start/stop/open with
  asymmetric fallback: if the tray cannot be created the app keeps serving
  headless. Linux requires the AppIndicator GNOME extension; macOS shows a
  brief Dock flash at startup.
- **PyPI packaging** — `pipx install job-cannon` is now the recommended
  end-user install path. `pyproject.toml` is the canonical surface; `uv.lock`
  is committed. Python 3.13+ required.
- **Autoheal pipeline** (Phases A–C) — declarative recipes detect and repair
  stale ATS slugs, inflated salaries, heal-state drift, and duplicate company
  records across m084–m088. Phase C ships the recipe-infra + email-override
  seam, ATS resolvers, VALIDATE gate, and ADOPT path.
- **Two-tier job board** — triage view (quick-scan) vs. deep-dive view;
  OKLCH color tokens for 3-band score chips.
- **Live SSE events stream** (`/events`) — per-job score events and
  orchestration log emitted to the dashboard in real time.
- **Cascade default updated** — shipped default is
  `ollama → gemini → claude_code_cli → anthropic`; `claude_code_cli` ($0 via
  Claude.ai subscription) replaces Groq/Cerebras in the default chain.
  `scoring.daily_budget_usd` (default $10/day) gates the paid fallback only.

### Changed

- **Default cascade chain** — `Ollama → Gemini → Claude Code CLI → Anthropic`
  (was `Ollama → Groq → Cerebras → Gemini → Anthropic` in v3.0.0). Groq and
  Cerebras remain supported via `providers.fallback_chain` config but are no
  longer in the out-of-the-box default.
- **Budget key** — `scoring.daily_budget_usd` (default $10/day) replaces the
  never-shipped `scoring.monthly_budget_usd` reference that appeared in some
  docs. Free providers (`ollama`, `gemini`, `claude_code_cli`, `gemini_cli`,
  `local_bundled`) are excluded from the gate.
- **`jd_full` write boundary enforced** — cleanliness invariant applied at
  persist time; HTML-polluted and stub JDs rejected upstream (m079, m015, m022).

### Fixed (since 4.0.0)

- Gemini provider ported to `google-genai` SDK v1+ (broke on v0.8.5 → v1
  API surface change).
- Playwright lazy-loaded to fix `pipx install` crash on machines without
  a browser (careers crawler is optional).
- Budget progress bar compared day-spend against daily cap (not monthly cap).
- Onboarding wizard wrote invalid config on fresh install (empty
  `target_titles` tripped validation before merge completed).
- IMAP UNSEEN search scoped to known senders; bulk-fetch via `BODY.PEEK[]`.

### Docs / audit wave (this PR)

- README cascade chain, migration count (88), template count (58), blueprint
  count (14), test count (5006), budget key, and Codecov badge corrected.
- `monthly_budget_usd` removed from all docs and tests; replaced with
  `daily_budget_usd` throughout.
- v5.1 keyring-shipping paradox resolved: keyring shipped in v5.0.0.
- CONTRIBUTING security email (`security@example.com`) replaced with GitHub
  Advisories link.
- Wizard step count corrected to 8 in INSTALL.md and SETUP.md.
- Python 3.13+ requirement called out up-front in install docs.

## [4.0.0] — 2026-05-06

### Removed (BREAKING)

- **Phase 4 (resume generation)** — Removed during the public-repo
  cleanup. Deleted modules: `resume_generator`, `drive_uploader`,
  `drive_status`, `docx_formatter`, `resume_feedback`,
  `resume_validator`, `resume_style_guide`, `resume_multi_version`,
  the `resume_review` blueprint, the `feedback` blueprint, and the
  `guidelines` blueprint. Migration 47 dropped the
  `resume_generations`, `resume_preferences_detected`, and
  `resume_upload_reviews` tables.
- **Phase 5 (intelligence)** — Removed during the same cleanup.
  Deleted modules: `interview_prep`, `rejection_analyzer`,
  `rejection_patterns`, `notifier`. Migration 48 dropped the
  `interview_preps`, `rejection_reports`, and `rejection_pattern_reports`
  tables and the `jobs.rejection_reviewed` column.

### Retained

- **AI career navigator** (`ai_career_navigator.py`) is retained as a
  Tier-4 crawler fallback. 16 cached navigation recipes cover ~10
  active companies whose career sites are custom-built (iCIMS, Phenom,
  UKG, bespoke).

### Added — Modern Python Surface

- **Console-script entry point.** `uv run job-cannon` is the canonical
  invocation; `python -m job_finder` and `python run.py` (now a shim)
  are equivalent legacy paths. Implemented via `[project.scripts]` +
  hatchling build-system in `pyproject.toml`.
- **Config-discovery lookup order.** `job_finder.config.resolve_config_path`
  resolves `config.yaml` from `$JOB_CANNON_CONFIG` →
  `./config.yaml` → user config dir (`%APPDATA%\job-cannon` on
  Windows, `~/.config/job-cannon` on Unix). An explicit-but-missing
  env var raises `ConfigNotFoundError` — silent fallback was wrong UX.
- **`pyproject.toml` is the canonical install surface.** Replaces
  `requirements.txt` and `pytest.ini`. `uv.lock` is committed.
  `[project.optional-dependencies]` exposes `dev` and `eval` extras
  (e.g. `uv sync --extra dev --extra eval`).
- **CI matrix on Ubuntu + Windows × Python 3.13** with `playwright
  install chromium` step and Codecov upload (codecov.io
  authorization pending).

### Added — Repo Hygiene

- `SECURITY.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`-equivalent
  documentation in `docs/`, `.editorconfig`, GitHub issue + PR
  templates, weekly Dependabot for `pip` + `github-actions` ecosystems.
- Pre-commit hook chain: `gitleaks`, `ruff`, `commitizen`, plus a local
  pygrep block on template-placeholder markers (the safety net for
  README/metadata templates).
- `main` is the default branch (renamed from `master`).
- `.planning/` and `.reviews/` working directories are gitignored.
  Tracked planning artifacts (59 `.planning/` files, 3 root-level session
  notes, 24 verbatim JD corpus files) removed from the index in #305;
  working copies remain on disk.

## [3.0.0] — 2026-04-21

The v3.0 ordinal-scoring milestone. Highlights:

- **Single-tier ordinal scoring** with a six-axis rubric (Plan 4
  Commit E collapsed the legacy haiku→sonnet two-tier into one
  `'scoring'` tier). Classification (`apply | consider | skip | reject`)
  is derived in Python from the numeric sub-scores — never emitted by
  the LLM — to prevent classification drift across model swaps.
- **Multi-provider cascade routing.** The `'scoring'` tier tries
  providers in order — Ollama (qwen2.5:14b, the Phase 33 shootout
  winner) → Groq → Cerebras → Gemini → Anthropic. Anthropic is the
  paid fallback; production typically resolves on the first or second
  hop at zero cost.
- **Eval harness** with paired MAE + BCa bootstrap 95% CIs for
  prompt-variant A/B testing across the full provider matrix.
- Migration 40 introduced the v3.0 ordinal scoring schema; Migration
  41 dropped the legacy `haiku_score` / `haiku_summary` / `sonnet_score`
  columns.

> **Naming note:** the strings `'haiku'`, `'sonnet'`, and `'opus'`
> persist in `_TIER_DEFAULTS`, in `providers.*` config keys, and in
> the `enrichment_tier` DB column as **vestigial routing labels** for
> non-scoring callers (enrichment, careers scrape, AI navigator,
> company research, description reformat). They no longer mean
> Anthropic models — `'haiku'` means cheap-fast, `'sonnet'` means
> balanced-deep, `'opus'` means heavy-reasoning. A future refactor
> will rename them to `'low' / 'mid' / 'high'`.

## Earlier history

The repo's earlier tags are `v1.1` (2026-03-23) through `v2.0`
(2026-03-? — the v2.0 ingestion + scoring milestones). They predate the
portfolio-cleanup cycle and the v3.0 ordinal scoring rewrite. See `git
log v3.0.0` for the per-commit history if you need the detail.
