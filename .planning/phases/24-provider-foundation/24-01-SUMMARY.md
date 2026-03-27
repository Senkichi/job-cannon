---
phase: 24-provider-foundation
plan: "01"
subsystem: model-routing
tags: [tdd, types, abstract-base, config-resolution, providers]
dependency_graph:
  requires: [job_finder/config.py]
  provides: [job_finder/web/model_provider.py, job_finder/web/providers/__init__.py]
  affects: []
tech_stack:
  added: []
  patterns: [frozen-dataclass-slots, abc-abstractmethod, config-chaining-get]
key_files:
  created:
    - job_finder/web/model_provider.py
    - job_finder/web/providers/__init__.py
    - tests/test_model_provider.py
  modified: []
decisions:
  - _TIER_DEFAULTS dict maps tier names to DEFAULT_MODEL_* constants from config.py — single lookup table avoids repeated conditionals
  - Empty providers/ package has only a docstring — no re-exports, Phase 25 populates it
  - resolve_provider_config chains .get() calls: providers->tier->model, then scoring->models->tier, then _TIER_DEFAULTS — priority ordering correct
metrics:
  duration: 2min
  completed: "2026-03-27"
  tasks_completed: 1
  files_changed: 3
---

# Phase 24 Plan 01: Provider Foundation — Types and Config Resolution Summary

**One-liner:** Frozen ModelResult dataclass, BaseProvider ABC, and resolve_provider_config() with 5-path config chaining — foundational types for Phase 25-28 adapter work.

## What Was Built

Phase 24 Plan 01 creates the shared type foundation for the v1.5 multi-provider routing system:

- **ModelResult** (`job_finder/web/model_provider.py`): Frozen dataclass with `slots=True` matching the ClaudeContext pattern. Seven fields: `data`, `cost_usd`, `input_tokens`, `output_tokens`, `model`, `provider`, `schema_valid`. Immutable — adapters return new instances.

- **BaseProvider** (`job_finder/web/model_provider.py`): Abstract base class with single abstract method `call()`. Cannot be instantiated directly. Subclasses without `call()` also raise TypeError on instantiation. Phase 25 Anthropic/Gemini/Ollama adapters extend this.

- **resolve_provider_config()** (`job_finder/web/model_provider.py`): Resolves logical tier name (`"sonnet"`, `"haiku"`, `"opus"`) to `{provider, model, fallback}` dict. Five resolution paths:
  1. `config.providers.{tier}` present with model → use it
  2. `config.providers.{tier}` present without model → use `scoring.models.{tier}` or tier default
  3. `config.providers.{tier}` has fallback key → include in return dict
  4. No providers section → default to `anthropic` + `scoring.models.{tier}` or tier default
  5. Empty config → default to `anthropic` + `_TIER_DEFAULTS[tier]`

- **providers/** (`job_finder/web/providers/__init__.py`): Empty package marker with docstring. Phase 25 will create `anthropic_provider.py`, `gemini_provider.py`, `ollama_provider.py` inside it.

## TDD Execution

- **RED commit** (`761c595`): 11 failing tests written first. All fail with ImportError.
- **GREEN commit** (`20382e8`): model_provider.py + providers/__init__.py created. All 11 pass.
- No REFACTOR needed — implementation already clean.

## Decisions Made

| Decision | Rationale |
|----------|-----------|
| `_TIER_DEFAULTS` dict maps tier → model constant | Single lookup table, no if/elif chain; easily extended if new tiers added |
| `providers/__init__.py` empty (docstring only) | No re-exports per plan anti-pattern constraint; Phase 25 populates it |
| `resolve_provider_config` returns plain dict | Simple, JSON-serializable; Phase 26 dispatcher will consume it |
| `from __future__ import annotations` | Forward references in type hints (future-proof for self-referential types) |

## Verification

All acceptance criteria met:

- `model_provider.py` contains `@dataclass(frozen=True, slots=True)` and `class ModelResult`
- `model_provider.py` contains `class BaseProvider(ABC):`
- `model_provider.py` contains `def resolve_provider_config(tier: str, config: dict) -> dict:`
- `model_provider.py` imports `DEFAULT_MODEL_HAIKU, DEFAULT_MODEL_OPUS, DEFAULT_MODEL_SONNET`
- `model_provider.py` does NOT contain `call_claude` or `call_model`
- `providers/__init__.py` exists and does NOT re-export ModelResult or BaseProvider
- `tests/test_model_provider.py` contains 11 test functions
- `uv run pytest tests/test_model_provider.py -v` → 11 passed in 0.11s

## Deviations from Plan

None — plan executed exactly as written.

## Known Stubs

None.

## Self-Check: PASSED

- `job_finder/web/model_provider.py` — FOUND
- `job_finder/web/providers/__init__.py` — FOUND
- `tests/test_model_provider.py` — FOUND
- Commit `761c595` — FOUND (RED tests)
- Commit `20382e8` — FOUND (GREEN implementation)
