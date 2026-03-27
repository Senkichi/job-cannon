---
phase: 25-provider-adapters
plan: "01"
subsystem: api

tags: [anthropic, google-genai, jsonschema, provider-adapter, model-routing]

# Dependency graph
requires:
  - phase: 24-model-provider-types
    provides: BaseProvider, ModelResult, resolve_provider_config()

provides:
  - AnthropicProvider(BaseProvider) wrapping call_claude() as a thin facade
  - google-genai and jsonschema packages installed and importable
  - TDD test coverage for Anthropic adapter (7 tests)

affects: [26-dispatcher, 27-caller-migration, 28-evaluation-framework, 29-gemini-provider, 30-ollama-provider]

# Tech tracking
tech-stack:
  added: [google-genai>=1.0.0, jsonschema>=4.0.0]
  patterns: [provider-adapter pattern: delegate to existing implementation, return ModelResult with provider label]

key-files:
  created:
    - job_finder/web/providers/anthropic_provider.py
    - tests/test_anthropic_provider.py
  modified:
    - requirements.txt

key-decisions:
  - "AnthropicProvider delegates entirely to call_claude() — no cost_gate() or record_cost() calls in the adapter (call_claude handles both)"
  - "input_tokens and output_tokens are 0 in ModelResult — call_claude records them to scoring_costs internally, not re-exposed"
  - "schema_valid=True unconditionally — call_claude enforces schema via tool-choice, so output is always valid when no exception raised"

patterns-established:
  - "Provider adapter pattern: __init__ stores (client, conn, config); call() delegates to underlying implementation; returns ModelResult with provider label"
  - "Error propagation: adapters never catch BudgetExceededError or RuntimeError — callers decide how to handle"

requirements-completed: [ADAPT-01]

# Metrics
duration: 8min
completed: 2026-03-27
---

# Phase 25 Plan 01: Provider Adapters - Anthropic Adapter Summary

**AnthropicProvider(BaseProvider) wrapping call_claude() as a thin facade with google-genai and jsonschema dependency baseline installed**

## Performance

- **Duration:** 8 min
- **Started:** 2026-03-27T19:24:20Z
- **Completed:** 2026-03-27T19:32:24Z
- **Tasks:** 2
- **Files modified:** 3

## Accomplishments

- Installed google-genai>=1.0.0 and jsonschema>=4.0.0 into project venv (shared at <user-home>\repos\.venv)
- Implemented AnthropicProvider as a BaseProvider subclass delegating to call_claude()
- 7 unit tests covering subclass relationship, ModelResult fields, parameter forwarding, error propagation — all passing
- Full test suite (1603 tests) remains green with no regressions

## Task Commits

Each task was committed atomically:

1. **Task 1: Install dependencies** - `022c443` (chore)
2. **Task 2 RED: Failing tests** - `54d30cc` (test)
3. **Task 2 GREEN: AnthropicProvider implementation** - `70da9b9` (feat)

**Plan metadata:** (docs commit — see below)

_Note: TDD task has multiple commits (test RED → feat GREEN)_

## Files Created/Modified

- `job_finder/web/providers/anthropic_provider.py` - AnthropicProvider(BaseProvider) delegating to call_claude()
- `tests/test_anthropic_provider.py` - 7 unit tests for the Anthropic adapter
- `requirements.txt` - Added google-genai>=1.0.0 and jsonschema>=4.0.0

## Decisions Made

- AnthropicProvider deliberately does not call cost_gate() or record_cost() — call_claude() handles both internally. The adapter is a pass-through.
- input_tokens=0 and output_tokens=0 in ModelResult because call_claude records them to scoring_costs internally. The adapter does not re-expose them.
- schema_valid=True unconditionally because call_claude enforces schema via tool-choice mechanism — if an exception is not raised, the output is schema-valid.

## Deviations from Plan

None - plan executed exactly as written.

Note: The install step required specifying the shared venv path explicitly (`uv pip install --python .venv/Scripts/python.exe`) because the project uses a shared venv at <user-home>\repos\.venv rather than a project-local .venv. The `uv run` command works correctly despite the VIRTUAL_ENV warning.

## Issues Encountered

- `uv pip install` without a Python specifier installed into a local .venv that `uv run` ignores (it uses the shared workspace venv at <user-home>\repos\.venv). Fixed by using `uv pip install --python <path-to-shared-venv-python>`.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- AnthropicProvider is complete and test-covered. Ready for Plan 25-02 (Gemini provider) and Plan 25-03 (Ollama provider).
- All three adapters follow the same constructor pattern: `__init__(client, conn, config)`.
- Phase 26 dispatcher can instantiate AnthropicProvider with the existing Anthropic client.

---
*Phase: 25-provider-adapters*
*Completed: 2026-03-27*

## Self-Check: PASSED

- FOUND: job_finder/web/providers/anthropic_provider.py
- FOUND: tests/test_anthropic_provider.py
- FOUND: .planning/phases/25-provider-adapters/25-01-SUMMARY.md
- FOUND commit: 022c443 (chore - Task 1 dependencies)
- FOUND commit: 54d30cc (test - Task 2 RED)
- FOUND commit: 70da9b9 (feat - Task 2 GREEN)
