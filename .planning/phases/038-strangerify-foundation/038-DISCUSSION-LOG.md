# Phase 38: Strangerify Foundation - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-05-14
**Phase:** 038-strangerify-foundation
**Areas discussed:** Config path reconciliation, Existing DB transition, Personal-data audit scope, write_config placement

---

## Config Path Reconciliation

| Option | Description | Selected |
|--------|-------------|----------|
| Retire resolve_config_path() | Delete it. load_config() calls user_data_dirs.config_path() directly. Developer sets $JOB_CANNON_USER_DATA_DIR for local dev. | ✓ |
| Extend the lookup chain | Keep resolve_config_path() but add user_data_dirs as step 4. Backward compat but two path systems. | |
| Replace the fallback step only | Keep $JOB_CANNON_CONFIG + cwd lookup; replace only the %APPDATA%/job-cannon/ fallback with user_data_dirs. | |

**User's choice:** Retire resolve_config_path()
**Notes:** Cleaner — one canonical path function. Developer uses $JOB_CANNON_USER_DATA_DIR (same mechanism as test isolation, already in PLAN-P1).

---

## Existing DB Transition

| Option | Description | Selected |
|--------|-------------|----------|
| Env var override | Set $JOB_CANNON_USER_DATA_DIR to project root in local shell profile. No data move. | ✓ |
| Auto-detect and copy | Copy ./jobs.db to AppData on first boot if AppData target doesn't exist. | |
| Manual move + update path | Manually move jobs.db to AppData after phase ships. | |

**User's choice:** Env var override
**Notes:** Zero-risk, no extra code, consistent with test isolation pattern. Document in CLAUDE.md as dev-setup note.

---

## Personal-Data Audit Scope

| Option | Description | Selected |
|--------|-------------|----------|
| PLAN-P1 list + no regression test | Audit listed files only (example files, fixtures, prompt templates). | |
| PLAN-P1 list + regression test | Add test that greps for known personal identifiers. | |
| Broader scope + no regression test | Also sweep blueprint docstrings, README, migration comments, .planning/ .md files. | ✓ |

**User's choice:** Broader scope + no regression test
**Notes:** More thorough one-time sweep; no automated guard needed for a single-user pre-launch project.

---

## write_config Placement

| Option | Description | Selected |
|--------|-------------|----------|
| config.py | Collocated with load_config(). Callers import from one module. | ✓ |
| user_data_dirs.py | Collocated with path resolution. Creates circular concern (path + YAML writing). | |

**User's choice:** config.py
**Notes:** Matches PLAN-P1 spec. Moving it to user_data_dirs would muddy that module's responsibility.

---

## Claude's Discretion

- Exact shape of empty config returned by `load_config(allow_missing=True)` — plain `{}`
- `user_data_dirs.py` helper surface area beyond PLAN-P1 spec

## Deferred Ideas

None.
