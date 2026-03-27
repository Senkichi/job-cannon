---
phase: 25-provider-adapters
plan: "02"
subsystem: ai
tags: [google-genai, gemini, provider-adapter, rate-limit, retry, structured-output, response-json-schema, tdd]

# Dependency graph
requires:
  - phase: 25-01
    provides: AnthropicProvider and BaseProvider/ModelResult interface established

provides:
  - GeminiProvider adapter (job_finder/web/providers/gemini_provider.py)
  - 13 unit tests for GeminiProvider (tests/test_gemini_provider.py)

affects:
  - 25-03 (OllamaProvider — same adapter pattern)
  - 26 (Dispatcher — consumes GeminiProvider)

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Client injection pattern: GeminiProvider(config={}, client=mock) for testability"
    - "response_json_schema dict path (not Pydantic) for Gemini structured output"
    - "Two-attempt retry loop: range(2), sleep on 429 attempt 0, raise on attempt 1"
    - "usage_metadata guard: field or 0 for None token counts"

key-files:
  created:
    - job_finder/web/providers/gemini_provider.py
    - tests/test_gemini_provider.py
  modified: []

key-decisions:
  - "Used json.loads(response.text) not response.parsed — dict schema path avoids Pydantic dependency"
  - "Default retry_sleep_seconds=15.0 for Gemini 5 RPM free tier (cut from 15 RPM in Dec 2025)"
  - "genai_errors.ClientError(code, response_json={}, response=None) is instantiable for test fixtures"
  - "system_instruction in GenerateContentConfig not prepended to messages — SDK-native approach"
  - "cost_usd=0.0 always — Gemini free tier, dispatcher records costs separately if needed"

patterns-established:
  - "Provider init: client injection bypasses env var check; env var path uses provider_cfg.get('api_key_env', 'GEMINI_API_KEY')"
  - "Retry pattern: for attempt in range(2): try/except; sleep+continue on 429 attempt 0; raise on others"

requirements-completed: [ADAPT-02]

# Metrics
duration: 5min
completed: 2026-03-27
---

# Phase 25 Plan 02: Gemini Provider Adapter Summary

**GeminiProvider adapter using google-genai SDK with response_json_schema structured output and configurable 429 retry (default 15.0s sleep for 5 RPM free tier), 13 tests all green**

## Performance

- **Duration:** 5 min
- **Started:** 2026-03-27T19:35:11Z
- **Completed:** 2026-03-27T19:40:15Z
- **Tasks:** 1
- **Files modified:** 2

## Accomplishments

- GeminiProvider(BaseProvider) subclass using google-genai 1.68.0 SDK
- Structured output via response_json_schema dict (not Pydantic path) with response_mime_type="application/json"
- system_instruction forwarded via GenerateContentConfig (not prepended to messages)
- Single retry on 429 with configurable sleep (default 15.0s; config path: providers.gemini.retry_sleep_seconds)
- Raises ClientError immediately after double 429, immediately on non-429 errors
- Optional client= injection for testability — bypasses GEMINI_API_KEY env var check
- Guards usage_metadata token fields with `or 0` for None values
- 13 unit tests with fully mocked SDK — no live API calls, 1616 total tests pass

## Task Commits

1. **Task 1: Implement GeminiProvider adapter with TDD** - `223448b` (feat)

**Plan metadata:** (docs commit to follow)

## Files Created/Modified

- `job_finder/web/providers/gemini_provider.py` - GeminiProvider adapter class (112 lines)
- `tests/test_gemini_provider.py` - 13 unit tests with mocked google-genai SDK (215 lines)

## Decisions Made

- Used `json.loads(response.text)` not `response.parsed` — the dict schema path avoids adding a Pydantic dependency and is confirmed by RESEARCH.md
- Default retry_sleep_seconds=15.0 because Gemini free tier was cut from 15 RPM to 5 RPM in Dec 2025
- `genai_errors.ClientError(429, {}, None)` is directly instantiable (constructor takes code, response_json, optional response) — no MagicMock needed for creating 429 test fixtures
- cost_usd=0.0 always — Gemini free tier; the Phase 26 dispatcher will handle cost recording for paid tiers if applicable

## Deviations from Plan

None — plan executed exactly as written.

## Issues Encountered

None — google-genai 1.68.0 was already installed from Phase 25-01. The ClientError constructor (`code, response_json, response=None`) was confirmed by inspecting the SDK source, enabling real exception objects in tests instead of MagicMock fakes.

## User Setup Required

None — no external service configuration required for this implementation task. Users must set GEMINI_API_KEY in their environment when using the GeminiProvider in production.

## Known Stubs

None — GeminiProvider is fully implemented. cost_usd=0.0 is intentional (free tier design decision, not a stub).

## Next Phase Readiness

- GeminiProvider complete, ready for Phase 25-03 (OllamaProvider — same adapter pattern)
- Phase 26 (Dispatcher) can now reference all three provider adapters

---
*Phase: 25-provider-adapters*
*Completed: 2026-03-27*

## Self-Check: PASSED

- FOUND: job_finder/web/providers/gemini_provider.py
- FOUND: tests/test_gemini_provider.py
- FOUND: .planning/phases/25-provider-adapters/25-02-SUMMARY.md
- FOUND commit: 223448b (feat(25-02): implement GeminiProvider adapter with TDD)
- All 1616 tests pass
