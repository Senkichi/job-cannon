# Phase 38: Strangerify Foundation - Context

**Gathered:** 2026-05-14
**Status:** Ready for planning

<domain>
## Phase Boundary

This phase makes the app bootable for a stranger — pure scaffolding, no user-facing changes.
Four deliverables:

1. `user_data_dirs.py` — platformdirs wrapper; single source of truth for config/DB/logs/cache paths per OS
2. Config bootstrap — `load_config()` no longer fail-fast; `write_config()` for first-run atomic write
3. Migration N — `onboarding_state` table with `onboarding_complete` column (default false)
4. Personal-data audit — genericize example files, fixtures, prompt templates

**Out of scope (other phases):**
- Onboarding redirect before_request logic → Phase 42
- `_PROVIDER_DEFAULTS` / provider abstraction → Phase 39
- `low`/`mid`/`high` → `quick`/`score`/`triage` rename → Phase 40
- Any user-facing wizard UI → Phase 42

</domain>

<decisions>
## Implementation Decisions

### Config Path Resolution

- **D-01:** `resolve_config_path()` is retired entirely. `load_config()` calls `user_data_dirs.config_path()` directly for its default path. The `$JOB_CANNON_CONFIG` env var (explicit override) is preserved in `load_config()` for power users; the `./config.yaml` cwd lookup is dropped (unnecessary now that platformdirs is canonical).
- **D-02:** Developer local setup uses `$JOB_CANNON_USER_DATA_DIR` pointing at the project root. This is the same env var the test suite uses for test isolation — no new mechanism needed.
- **D-03:** `load_config(allow_missing=True)` returns `{}` when config doesn't exist. Default behavior (`allow_missing=False`) still raises `ConfigNotFoundError`. The app factory calls with `allow_missing=True`; redirects to onboarding are Phase 42's job. Phase 38 only guarantees no crash.

### DB Path Transition

- **D-04:** App factory resolves DB path via `user_data_dirs.db_path()` — not from config.yaml. `db_helpers.get_db(db_path)` keeps its `db_path` argument (no signature change).
- **D-05:** No auto-copy / migration logic for the existing `./jobs.db`. Developer sets `$JOB_CANNON_USER_DATA_DIR` to the project root so the existing DB is picked up without moving any files. Add a dev-setup note to CLAUDE.md.

### write_config Placement

- **D-06:** `write_config(data: dict)` lives in `config.py` alongside `load_config()`. Uses `tempfile + os.replace` for atomic write to `user_data_dirs.config_path()`. The function is ONLY for first-run wizard (file doesn't exist yet) and the settings save route (which already does read→merge→write). Must NOT be used for surgical single-key edits — use the Edit tool per CLAUDE.md rule.

### Personal-Data Audit Scope

- **D-07:** Broader scope than PLAN-P1 minimum. Files to sweep and genericize:
  - `experience_profile.example.json` — full placeholder replacement
  - `config.example.yaml` — remove user-specific preferences; add `imap:` section placeholder
  - `job_finder/web/job_scorer.py` prompt templates — strip any user-specific phrasing
  - `job_finder/web/data_enricher.py` — same
  - `job_finder/web/ai_career_navigator.py` — same
  - `tests/fixtures/*.json` — replace all author-tied data
  - **Additional scope:** blueprint docstrings, README examples, migration comments, and `.md` files in `.planning/` that reference personal data (names, emails, locations, company names tied to the author)
  - No automated regression test — one-time audit, single-user project, no external contributors yet

### Migration Numbering

- **D-08:** Before assigning the migration version number, the planner must verify the current `PRAGMA user_version` from the live DB. Two m049_* files both claim version=49 (`m049_ats_identity_evidence.py` and `m049_schema_valid.py`); `m050_rename_tier_strings.py` is already at version=50. The success criteria requires user_version=53 after Phase 38's migration — work backward from that to determine the correct version number. Do NOT create a new migration file without first auditing the existing version sequence.

### Claude's Discretion

- Exact shape of the empty config returned by `load_config(allow_missing=True)` — use plain `{}` for simplicity; callers check for missing keys
- Whether to add any helper to `user_data_dirs.py` beyond what PLAN-P1 specifies (config_path, db_path, logs_path, cache_path, user_data_root, ensure_user_data_dir)

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Primary Design Sources
- `.planning/public-release/PLAN-P1.md` §"Chunk 1: Foundation" — Step-by-step task breakdown for Tasks 1.1 and 1.2; exact test code specified; commit messages specified. Design wins over plan on conflicts.
- `.planning/public-release/DESIGN.md` §"Bucket D — Personal-data extraction" — Defines the audit scope
- `.planning/REQUIREMENTS.md` — STRANGE-FOUND-01 through STRANGE-FOUND-05 (exact acceptance criteria)

### Architecture Constraints
- `CLAUDE.md` — `config.yaml` must only be modified with the Edit tool, never Write tool (wipe incident ×3). `write_config()` is the only legitimate exception: first-run creation (file doesn't exist yet) and the settings save route.
- `.planning/ROADMAP.md` §Phase 38 — Success criteria 1–5 (the authoritative test oracle)

### Existing Code to Read Before Touching
- `job_finder/config.py` — Current `resolve_config_path()` and `load_config()` implementations (to be refactored; retire `resolve_config_path()`)
- `job_finder/web/db_helpers.py` — Current `get_db(db_path)` signature (stays unchanged; app factory wiring changes)
- `job_finder/web/__init__.py` (create_app) — Where DB path is currently wired; where `user_data_dirs` integration lands
- `job_finder/web/migrations/m049_ats_identity_evidence.py` + `m049_schema_valid.py` — Both claim version=49; audit before numbering the new migration
- `job_finder/web/migrations/m050_rename_tier_strings.py` — Already at version=50; new onboarding_state migration must be ≥51

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `job_finder/web/migrations/types.py` (Migration dataclass) — Same pattern for the new `onboarding_state` migration
- `job_finder/config.py:ConfigNotFoundError` — Keep this exception class; `load_config(allow_missing=False)` still raises it
- `$JOB_CANNON_USER_DATA_DIR` env var — Already referenced in PLAN-P1 test design; consistent with the test isolation pattern in `conftest.py`

### Established Patterns
- Migrations: one file per version, `MIGRATION = Migration(version=N, description=..., sql=[...])`. Auto-discovered by `db_migrate.py` — no manual registration needed.
- DB path: passed as argument from app factory → `get_db(db_path)`. No global state; keep it that way.
- Config load: currently `load_config(config_path: str = "config.yaml")` — the `path` kwarg is kept as a test override escape hatch.

### Integration Points
- `job_finder/web/__init__.py` (create_app): needs to call `user_data_dirs.ensure_user_data_dir()` early, resolve DB path via `user_data_dirs.db_path()`, and call `load_config(allow_missing=True)` instead of the current direct-path call
- `job_finder/web/db_migrate.py`: auto-discovers migrations by filename pattern — no change needed as long as the new migration file follows the existing `mNNN_*.py` naming convention

</code_context>

<specifics>
## Specific Ideas

- Add a dev-setup note to CLAUDE.md: "Set `$JOB_CANNON_USER_DATA_DIR` to the project root in your local PowerShell profile to keep `config.yaml` and `jobs.db` in the project dir during development (avoids moving existing data to AppData)"
- `_APP_AUTHOR = False` in `user_data_dirs.py` (platformdirs convention: skips the author segment on Windows so path is `%APPDATA%\JobCannon\` not `%APPDATA%\JobCannon\JobCannon\`)
- Generic resume fixtures (`tests/fixtures/sample_resume.pdf` + `.docx`) can be minimal stubs now — actual resume parsing is Phase 41; just need non-personal placeholder content

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 038-strangerify-foundation*
*Context gathered: 2026-05-14*
