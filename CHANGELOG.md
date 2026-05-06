# Changelog

All notable changes to this project are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project follows [Semantic Versioning](https://semver.org/).

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
- `.planning/` and `.reviews/` working directories are gitignored;
  100+ tracked planning artifacts removed in S1.

## [3.0.0] — 2026-04-21

The v3.0 ordinal-scoring milestone. Highlights:

- **Two-tier AI scoring** with a six-axis ordinal rubric. Classification
  (`apply | consider | skip | reject`) is derived in Python from the
  numeric sub-scores — never emitted by the LLM — to prevent
  classification drift across model upgrades.
- **Eval harness** with paired MAE + BCa bootstrap 95% CIs for
  prompt-variant A/B testing across model providers.
- **Phase 33 model shootout** finalized `qwen2.5:14b` as the local
  fallback; `claude-haiku-4-5` and `claude-sonnet-4-6` are the production
  cloud models.
- Migration 40 introduced the v3.0 ordinal scoring schema.

## Earlier history

The repo's earlier tags are `v1.1` (2026-03-23) through `v2.0`
(2026-03-? — the v2.0 ingestion + scoring milestones). They predate the
portfolio-cleanup cycle and the v3.0 ordinal scoring rewrite. See `git
log v3.0.0` for the per-commit history if you need the detail.
