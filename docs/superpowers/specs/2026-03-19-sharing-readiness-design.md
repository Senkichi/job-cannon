# Sharing Readiness — Design Spec

**Date:** 2026-03-19
**Goal:** Prepare the repo for colleagues (technical but non-dev) to clone and run their own local instance.

## Context

Job Finder is a personal job search command center. Each colleague runs their own local instance — no multi-tenancy. The current repo has personal data baked into tracked files and onboarding friction that assumes the original developer's context.

**Audience:** Technical but not developers (data scientists, PMs). Can follow step-by-step guides but won't debug pip errors or OAuth flows without help.

**Strategy:** Fresh repo from clean working tree (single squashed commit) rather than rewriting 712 commits of git history.

---

## v1 Scope — 17 Requirements Across 4 Phases

### Phase 1: Scrub + Scaffold

**SCRUB-01: Clean personal references from tracked files**
- `.planning/PROJECT.md` — genericize personal name references
- `SPEC.md` — remove personal name, email, salary/target details (use placeholders)
- Scan all `.planning/` files being retained (PROJECT.md, codebase/ docs) for personal references
- Files in directories being deleted (milestones/, phases/, etc.) don't need scrubbing

**SCRUB-02: Audit all tracked files for personal data**
- Grep all tracked files for: personal name, personal email, phone numbers, specific company names from career history, LinkedIn URLs
- Fix or remove any hits not in `.gitignore`d files
- Safety net for anything SCRUB-01 missed

**PROJ-01: Add MIT LICENSE**
- Add MIT license to root. Without one, colleagues technically can't fork or modify.

**PROJ-04: Fix cross-platform dependency**
- `win11toast~=0.35` in requirements.txt fails `pip install` on Mac/Linux
- Change to: `win11toast~=0.35; sys_platform == "win32"` (PEP 508 environment marker)
- Document in README that Windows toast notifications are Windows-only

**PROJ-05: Add .gitattributes**
- `*.sh text eol=lf` — shell scripts have CRLF from Windows development, will fail on Mac/Linux
- `* text=auto` — general cross-platform safety net

### Phase 2: Config + Code

**CONFIG-01: Verify and update .env.example**
- Verify it includes all environment variables the app reads
- Add `FLASK_SECRET_KEY=change-this-to-a-random-string` if missing
- Add any other expected env vars

**CONFIG-02: Verify config.example.yaml and experience_profile.example.json completeness**
- Ensure every config.yaml field the app reads has a corresponding entry in config.example.yaml
- Add inline comments explaining non-obvious fields
- Use clearly fake placeholder values
- Verify scoring section, exclusion filters, and profile targets all have documented defaults
- Verify `experience_profile.example.json` exists, has complete schema, and documents which fields are required vs optional

**CONFIG-03: Move hardcoded values to config**
- Company denylist (`config.py:39-47`) → `company_denylist` list in config.yaml. `config.py` loads from config with current frozenset as fallback.
- Localhost URLs (`notifier.py:104,134,168`) → derive from `server.host` and `server.port` in config.yaml.
- `run.py` host/port/debug → same `server` config block (`host: 127.0.0.1`, `port: 5000`, `debug: true`). Env var overrides: `PORT` (int) and `DEBUG` (truthy: "1", "true", "yes").
- Update `config.example.yaml` with the new fields (including new `server:` and `company_denylist:` sections).

**CODE-01: Improve error messages for setup failures**
- `config.py:63` references `"config.yaml.example"` but actual file is `config.example.yaml` — fix
- Gmail source: "Gmail not configured" → explain what "configured" means and how to fix
- Email parsers: log at WARNING (not DEBUG) when parsing fails, with context to diagnose
- Scheduler: log clearly when skipping Gmail poll due to missing credentials
- Anthropic client: validate API key exists before first call with clear error message

**CODE-03: Verify Flask secret key configurability**
- Already implemented: `web/__init__.py` reads `FLASK_SECRET_KEY` env var with `"dev-secret-key-change-in-production"` fallback
- Verify `.env.example` documents this variable (it currently doesn't — handled by CONFIG-01)

**CODE-04: Pin frontend CDN versions**
- Tailwind: `@tailwindcss/browser@4` → pin to specific version (e.g., `@tailwindcss/browser@4.0.6`)
- SortableJS: `sortablejs@latest` → pin to specific version (e.g., `sortablejs@1.15.6`)
- HTMX already pinned at `2.0.8` — no change needed
- Check current served versions and lock to those

### Phase 3: Clean + Docs

**CLEAN-01: Trim .planning/ directory**
- Keep: `codebase/` directory only (ARCHITECTURE.md, CONVENTIONS.md, CONCERNS.md, STACK.md, TESTING.md, STRUCTURE.md, INTEGRATIONS.md)
- Drop files: PROJECT.md, ROADMAP.md, STATE.md, REQUIREMENTS.md, MILESTONES.md, RETROSPECTIVE.md, CHANGELOG.md, milestone audits, config.json, SHARING-READINESS-SPEC.md
- Drop directories: debug/, milestones/, phases/, reviews/, scoring_evaluation/, research/, todos/
- Drop `.claude/` directory entirely from tracked files

**CLEAN-02: Clean up root directory**
- Remove `AUDIT_LOG.md` (untracked)
- Rename `SPEC.md` → `DESIGN.md` (personal data already removed by SCRUB-01)
- Update `CLAUDE.md`: remove stale "Current Status" section, run `pytest` and update test count to actual, remove references to deleted `.planning/` files and `.claude/` agents

**DOCS-01: Rewrite README.md**
- Structure:
  1. One-paragraph description + screenshot
  2. Quick-start checklist (8 numbered steps, uv commands with pip in parentheses)
  3. How to set up Gmail alerts (per-source: LinkedIn, Glassdoor, ZipRecruiter)
  4. Customizing your profile (config.yaml profile section + experience_profile.json)
  5. Cost expectations (brief — Haiku ~$0.001/job, Sonnet ~$0.01/job, budget_cap controls spend)
  6. Platform notes (Windows toast is Windows-only)
  7. Architecture pointer (link to DESIGN.md and .planning/codebase/)
- Store screenshots in `docs/screenshots/`

**DOCS-02: Create setup documentation**
- Quick path (top): "Ask [owner] to add you as a test user. Place the `credentials.json` you receive in project root."
- Independent path (appendix): Full GCP Console walkthrough — create project, enable Gmail + Drive APIs, OAuth consent screen, Desktop App credentials, download credentials.json
- First-run auth flow: `python -m job_finder.gmail_auth` with expected browser prompts
- config.yaml setup: explain every section, required vs optional
- experience_profile.json: document schema, which fields are required vs optional
- Troubleshooting: "no jobs appearing", "scoring not running", "pip install fails on Mac/Linux"

### Phase 4: Fresh Repo + Verify

**REPO-01: Create fresh repo from working tree**
- Widen `.gitignore`: add `.env*` with `!.env.example` to keep the example tracked
- Commit all scrubbing and cleanup changes first
- `git archive HEAD | tar -x -C ../job-cannon` (exports committed tree only — all changes must be committed before this step)
- `cd ../job-cannon && git init && git add -A && git commit -m "Initial commit"`
- Final grep scan for personal data on clean tree

**REPO-02: Verify clone-and-run experience**
- Simulate fresh clone: temp directory, follow only the docs
- Verify: can create config from examples, can run the app, dashboard loads
- Verify empty-state UX: dashboard, job board, Kanban show helpful messages not errors
- Verify `pytest` passes without external credentials (tests use mock fixtures)

**REPO-03: GitHub hosting**
- Create private GitHub repo
- Push initial commit
- Invite colleagues as collaborators

---

## Deferred to v2

These are documented for follow-up after colleagues have tried the app:

| Req | What | Why deferred |
|-----|------|-------------|
| PROJ-02 | pyproject.toml, .python-version | Not blocking — README says "Python 3.13" |
| PROJ-03 | Task runner scripts (scripts/setup.sh, etc.) | Commands documented in README suffice for v1 |
| CONFIG-04 | Config validation on startup + drift detection | Nice guardrails but not blocking first-run |
| CODE-02 | Fix silent failure patterns (DEBUG → WARNING) | Won't bite during initial setup |
| CODE-05 | Migration error handling with rollback | Edge case; fresh DB won't hit partial migrations |
| DOCS-03 | Detailed cost documentation (standalone doc) | Brief README section covers it for v1 |

---

## Out of Scope (Not Planned)

- Multi-tenancy / multi-user database
- Docker / containerization
- CI/CD pipeline
- CONTRIBUTING.md / SECURITY.md / CODE_OF_CONDUCT.md
- Pre-commit hooks
- GitHub issue/PR templates
- .editorconfig

---

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Package manager | uv primary, pip fallback | uv is faster; pip fallback ensures no one is blocked |
| OAuth onboarding | Both paths documented | Quick path (shared credentials) for fast start; independent path for autonomy |
| GSD artifacts | Drop .claude/ and most of .planning/ | Colleagues don't use GSD; codebase/ docs have onboarding value |
| Phase count | 4 (down from 7) | Natural dependency groupings; less overhead |
| Fresh repo | Single squashed commit | Avoids rewriting 712 commits; clean slate |
| Config hardcoded values | Move to config.yaml with code fallbacks | Colleagues can customize without touching code |
| CDN versions | Pin to currently-served versions | Prevent silent breakage from unpinned CDN |
