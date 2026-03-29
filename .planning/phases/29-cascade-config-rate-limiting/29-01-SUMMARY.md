---
phase: 29-cascade-config-rate-limiting
plan: "01"
subsystem: model_provider
tags: [config-parsing, cascade, rate-limiting, backward-compat, tdd]
dependency_graph:
  requires: []
  provides: [resolve_provider_config-fallback-chain, resolve_provider_config-daily-limits]
  affects: [job_finder/web/model_provider.py, tests/test_model_provider.py, config.example.yaml]
tech_stack:
  added: []
  patterns: [dict-extension-backward-compat, tdd-red-green-fix]
key_files:
  created: []
  modified:
    - job_finder/web/model_provider.py
    - tests/test_model_provider.py
    - config.example.yaml
decisions:
  - "fallback_chain parsed from tier_cfg (per-tier), daily_limits parsed from providers_cfg (shared sibling)"
  - "Return dict extended with two new keys — existing consumers using resolved['provider'] or resolved['fallback'] unaffected"
  - "5 existing exact-dict assertions updated to include fallback_chain=[] and daily_limits={} for backward compat verification"
metrics:
  duration_seconds: 377
  completed_date: "2026-03-29"
  tasks_completed: 2
  files_modified: 3
---

# Phase 29 Plan 01: Cascade Config Schema — Summary

**One-liner:** Extended `resolve_provider_config()` with `fallback_chain` list and `daily_limits` dict parsed from config, documented cascade schema in `config.example.yaml`, and updated all affected tests.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Extend resolve_provider_config and write config parsing tests | f906f03 | job_finder/web/model_provider.py, tests/test_model_provider.py |
| 2 | Document cascade config schema in config.example.yaml | 2d31927 | config.example.yaml |

## What Was Built

### `job_finder/web/model_provider.py`

`resolve_provider_config()` now returns two additional keys:

- `fallback_chain` (`list[dict]`): Ordered list of `{provider, model}` dicts parsed from `tier_cfg.get("fallback_chain", [])`. Empty list when not configured.
- `daily_limits` (`dict[str, int]`): Per-provider daily caps parsed from `providers_cfg.get("daily_limits", {})`. Note: `daily_limits` is a sibling of tier keys under `providers:`, not nested inside a tier. Empty dict when not configured.

Both keys default to empty collections, preserving all existing behavior for consumers that don't use them.

### `tests/test_model_provider.py`

Four new tests added under `# --- Cascade config parsing tests (TEST-01) ---`:

1. `test_resolve_with_fallback_chain` — chain present in config, asserts result contains it
2. `test_resolve_returns_daily_limits` — `daily_limits` at `providers.daily_limits`, asserts sibling parsing
3. `test_resolve_backward_compat_empty_chain` — no chain configured, asserts `[]` and `{}`
4. `test_resolve_chain_with_daily_limits_combined` — both keys present simultaneously

Five existing exact-dict assertions updated to include `"fallback_chain": [], "daily_limits": {}`.

### `config.example.yaml`

New commented example block added after the Mistral example, before `# --- Output settings ---`. Documents:
- `fallback_chain` list under a tier config
- `prompt_variant` as an optional per-entry override
- `daily_limits` as a sibling to tier configs (not nested inside a tier)
- Conservative cap values: `cerebras: 350`, `groq: 170`

## Verification

- `uv run pytest tests/test_model_provider.py -x -q`: 28 passed
- `uv run pytest tests/ -q`: 1790 passed (4 new tests, 0 regressions)
- `grep "fallback_chain" job_finder/web/model_provider.py`: present in resolve_provider_config
- `grep "fallback_chain" config.example.yaml`: 1 hit
- `grep "daily_limits" config.example.yaml`: 2 hits

## Deviations from Plan

None — plan executed exactly as written.

## Known Stubs

None. Both new keys are fully wired through `resolve_provider_config()` and consumed by tests. `call_model()` will use them in Phase 30 (cascade execution).

## Self-Check: PASSED

- `job_finder/web/model_provider.py` — FOUND (modified)
- `tests/test_model_provider.py` — FOUND (modified)
- `config.example.yaml` — FOUND (modified)
- Commit f906f03 — verified in git log
- Commit 2d31927 — verified in git log
