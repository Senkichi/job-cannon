---
gsd_state_version: 1.0
milestone: v2.0
milestone_name: Cascading Free Provider Routing
status: verifying
stopped_at: Completed 32-integration-config-wiring/32-01-PLAN.md
last_updated: "2026-03-30T01:20:27.525Z"
last_activity: 2026-03-30
progress:
  total_phases: 4
  completed_phases: 4
  total_plans: 7
  completed_plans: 7
---

# State

## Current Position

Phase: 32 (Integration & Config Wiring) — EXECUTING
Plan: 1 of 1
Status: Phase complete — ready for verification
Last activity: 2026-03-30

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-29)

**Core value:** Surface the best-fit jobs fast and keep the application pipeline visible
**Current focus:** Phase 32 — Integration & Config Wiring

## Performance Metrics

**Velocity:**

- Total plans completed: 3 (this milestone)

| Phase | Plan | Duration | Tasks | Files |
|-------|------|----------|-------|-------|
| 29-cascade-config-rate-limiting | 01 | — | — | — |
| 29-cascade-config-rate-limiting | 02 | — | — | — |
| 30-cascade-execution | 01 | 6min | 2 | 2 |

*Updated after each plan completion*
| Phase 31-prompts-attribution P01 | 11min | 2 tasks | 4 files |
| Phase 31-prompts-attribution P02 | 12min | 2 tasks | 5 files |
| Phase 31-prompts-attribution P03 | 5min | 1 tasks | 2 files |
| Phase 32-integration-config-wiring P01 | 15min | 3 tasks | 3 files |

## Accumulated Context

### Key Design Decisions (v2.0)

- Decided cascade order: Cerebras qwen-3-235b (primary, fewshot, 363/day) -> Groq llama-4-scout (fewshot-distribution, 181/day) -> Ollama qwen2.5:14b (fewshot, unlimited) -> Anthropic Sonnet (paid, last resort)
- daily_limits values: cerebras: 350, groq: 170 (conservative below measured 363/181 hard caps)
- Empty fallback_chain in config preserves existing single-fallback behavior — no breaking change
- `_daily_usage` is module-level in model_provider.py, resets on date rollover via `_usage_date` comparison
- DB bootstrap on rollover: `SELECT provider, COUNT(*) FROM scoring_costs WHERE date(timestamp) = date('now') GROUP BY provider` (column is `timestamp`, NOT `created_at`)
- scoring_provider column default is 'anthropic' — migration is non-destructive for existing rows
- Per-model prompt variant is optional in fallback_chain entries; absent = use default fewshot
- `_ensure_usage_current(conn)` is the date-rollover guard (Plan 02); Phase 30 calls it at top of call_model()
- `_check_daily_limit` and `_increment_usage` are pure (no conn dependency); conn only needed for DB bootstrap
- Module state (_daily_usage, _usage_date) directly mutated in tests via `_reset_daily_state` fixture — no dedicated reset function needed
- Full suite: 1800 tests passing after Plan 02 (10 new daily tracker tests)
- Full suite: 1805 tests passing after Phase 30 Plan 01 (5 new cascade execution tests)
- Cascade path guarded by `if fallback_chain:` — empty list preserves existing single-fallback behavior exactly
- Schema failure after retry inside cascade loop does `continue` (not raise) — try next provider before giving up
- Both `ValueError` and `RuntimeError` caught from `_make_adapter` — ValueError for missing API key, RuntimeError for Ollama health check
- `_ensure_usage_current(conn)` is first statement in cascade path — before any `_check_daily_limit` call (Pitfall 3 guard)
- `_SYSTEM_PROMPT` in sonnet_evaluator now includes fewshot examples by default (PRMT-01); `_BASE_SYSTEM_PROMPT` is the plain version
- eval_provider "default" variant maps to `_BASE_SYSTEM_PROMPT` to preserve legacy eval baseline (no fewshot in eval baseline)
- Cascade chain rebuilt as `list[dict]` to carry `prompt_variant` per entry; lazy import of PROMPT_VARIANTS inside loop avoids circular dependency
- Full suite: 1815 tests passing after Phase 31 Plan 02 (10 new tests: 5 prompt + 2 cascade variant + 3 test_eval_provider fixes)
- Inject provider via data dict merge ({**result, "provider": result_obj.provider}) rather than changing ScoringResult NamedTuple — avoids positional unpacking breaks in callers (ATTR-03)
- Attribution chain closed: call_model() -> ModelResult.provider -> evaluate_job_sonnet data dict -> score_and_persist_sonnet -> persist_sonnet_score(provider=) -> DB scoring_provider column

### Carried Forward from v1.5

- call_model() is the single dispatch point — all callers use logical tier names ("sonnet", "haiku", "opus"), never provider-specific model IDs
- Budget gate bypass: free providers (Gemini free tier, Ollama, Groq, Cerebras) skip budget checks entirely
- Schema validation retry: dispatcher retries once with schema errors appended to prompt before falling back
- OllamaProvider uses requests library — stream=False hardcoded, format=json guarantees parseable output
- AnthropicProvider stores job_id and purpose at init, forwards to call_claude for correct cost attribution
- scoring_costs table already has `provider` column

### Blockers/Concerns

None.

### Implementation Reference

- Implementation plan: `.planning/IMPLEMENTATION_PLAN_V2.md`
- Eval results: `eval_results/` (72+ runs from benchmarking session)
- Provider eval session notes: `.claude/projects/C--Users-senki-repos-job-cannon/memory/project_provider_eval_session.md`

## Session Continuity

Last session: 2026-03-30T01:20:27.522Z
Stopped at: Completed 32-integration-config-wiring/32-01-PLAN.md
Resume file: None
