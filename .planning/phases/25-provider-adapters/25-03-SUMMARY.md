---
phase: 25-provider-adapters
plan: "03"
subsystem: api

tags: [ollama, requests, rest-api, provider-adapter, local-llm, BaseProvider, ModelResult]

requires:
  - phase: 25-01
    provides: "AnthropicProvider adapter pattern and BaseProvider/ModelResult interfaces"
  - phase: 24-01
    provides: "BaseProvider ABC and ModelResult dataclass from model_provider.py"

provides:
  - "OllamaProvider class in job_finder/web/providers/ollama_provider.py"
  - "Health check on init via GET /api/tags with 5s timeout"
  - "POST /api/chat with stream=False (critical) and format=json"
  - "Schema embedded in system prompt (Ollama lacks native schema enforcement)"
  - "cost_usd=0.0 for all calls (local, no API cost)"
  - "Configurable base_url with trailing slash normalization"
  - "18 unit tests in tests/test_ollama_provider.py with mocked requests"

affects:
  - "26-dispatcher — OllamaProvider is one of three providers call_model() will instantiate"
  - "config.yaml — providers.ollama.base_url configures Ollama endpoint"

tech-stack:
  added: []
  patterns:
    - "Health-check-on-init pattern: provider raises RuntimeError at construction time if service unreachable"
    - "Schema-in-system-prompt: when output_schema provided, embed JSON schema instructions in system message"
    - "stream=False critical guard: Ollama SSE chunks break resp.json() without this flag"
    - "Mock helper pattern: _make_provider() fixture mocks health check for call() tests"

key-files:
  created:
    - job_finder/web/providers/ollama_provider.py
    - tests/test_ollama_provider.py
  modified: []

key-decisions:
  - "Use requests library (already installed) over ollama Python SDK — avoids extra dependency, REST API is sufficient"
  - "stream=False is hardcoded, not configurable — it's a correctness requirement, not a user option"
  - "format=json is hardcoded — guarantees valid JSON; schema adherence is best-effort via system prompt"
  - "Health check uses 5s timeout (_HEALTH_CHECK_TIMEOUT) to prevent Flask startup hang"
  - "Catch (ConnectionError, Timeout, HTTPError) from requests — raise RuntimeError with base_url in message for actionable error"
  - "Token counts from body.get('prompt_eval_count', 0) and body.get('eval_count', 0) — absent in some Ollama versions"

patterns-established:
  - "Provider init validates service availability eagerly — no lazy health checks"
  - "Schema embedding uses json.dumps(schema, indent=2) appended to system with instructional prefix"
  - "timeout=None handled with explicit conditional (not `or`) to allow 0.0 override if needed"

requirements-completed: [ADAPT-03]

duration: 11min
completed: 2026-03-27
---

# Phase 25 Plan 03: Ollama Provider Adapter Summary

**OllamaProvider using requests library with health-check-on-init, stream=False, format=json, and schema-in-system-prompt — 18 tests pass with mocked requests**

## Performance

- **Duration:** 11 min
- **Started:** 2026-03-27T19:35:58Z
- **Completed:** 2026-03-27T19:46:58Z
- **Tasks:** 1 (TDD: RED + GREEN)
- **Files modified:** 2

## Accomplishments

- OllamaProvider subclasses BaseProvider and implements the full call() interface
- Health check on `__init__` via GET /api/tags catches unreachable Ollama early with clear RuntimeError
- POST /api/chat with `stream: False` (critical — prevents SSE chunk streaming that breaks resp.json()) and `format: json`
- Schema embedded in system prompt because Ollama lacks native schema enforcement; `format=json` guarantees valid JSON
- 18 unit tests cover all behaviors including mocked health check, payload structure, error propagation, and edge cases

## Task Commits

Each task was committed atomically:

1. **Task 1: RED — Add failing tests** - `bf80658` (test)
2. **Task 1: GREEN — Implement OllamaProvider** - `8944ab9` (feat)

_TDD task had two commits: test (RED) then implementation (GREEN)_

## Files Created/Modified

- `/c/Users/senki/repos/job-cannon/.claude/worktrees/agent-afcf399d/job_finder/web/providers/ollama_provider.py` — OllamaProvider adapter with health check and REST calls
- `/c/Users/senki/repos/job-cannon/.claude/worktrees/agent-afcf399d/tests/test_ollama_provider.py` — 18 unit tests with mocked requests

## Decisions Made

- Used `requests` library (already installed at v2.32.5) rather than the `ollama` Python SDK — avoids an extra dependency and the REST API is sufficient for the current use case
- `stream=False` is hardcoded in the payload, not configurable — streaming breaks `resp.json()` and is never desired for structured scoring calls
- `format="json"` is also hardcoded — it guarantees a parseable response even when schema adherence is best-effort
- Health check timeout is `_HEALTH_CHECK_TIMEOUT = 5.0` (named constant, not magic number) to prevent Flask startup hang when Ollama is slow
- `timeout=None` handled with explicit `if timeout is not None else _DEFAULT_TIMEOUT` (not `or`) to correctly handle a hypothetical `timeout=0.0` argument

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

During full test suite run, 7 pre-existing test failures were found in `tests/test_backfill_enrichment.py` and `tests/test_data_enricher.py`. Root cause: the worktree has an older version of these test files that mock `evaluate_job_sonnet` to return plain dicts, but `backfill_enrichment.py` now calls `unwrap_scoring_result()` which expects `ScoringResult` NamedTuples. These failures pre-exist on the worktree branch before any changes in this plan. The fix exists on master (commit `5b980cbc`). These failures are out-of-scope for plan 25-03.

Logged to deferred items below.

## Known Stubs

None — OllamaProvider is fully wired to the Ollama REST API with no placeholder data.

## User Setup Required

None for the adapter itself. To use Ollama, install and run `ollama serve` locally. The provider raises a clear RuntimeError with instructions if Ollama is not running.

## Next Phase Readiness

- OllamaProvider is ready for Phase 26 (call_model() dispatcher) which will instantiate it based on config
- All three provider adapters now complete: Anthropic (25-01), Gemini (25-02), Ollama (25-03)
- No blockers

## Self-Check: PASSED

- FOUND: `job_finder/web/providers/ollama_provider.py`
- FOUND: `tests/test_ollama_provider.py`
- FOUND: `.planning/phases/25-provider-adapters/25-03-SUMMARY.md`
- FOUND: commit `bf80658` (TDD RED — failing tests)
- FOUND: commit `8944ab9` (TDD GREEN — implementation)
- FOUND: commit `89f073a` (docs — SUMMARY, STATE, ROADMAP)

---
*Phase: 25-provider-adapters*
*Completed: 2026-03-27*
