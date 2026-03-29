---
gsd_state_version: 1.0
milestone: v2.0
milestone_name: Cascading Free Provider Routing
status: verifying
stopped_at: Completed 29-cascade-config-rate-limiting/29-02-PLAN.md
last_updated: "2026-03-29T23:11:03.014Z"
last_activity: 2026-03-29
progress:
  total_phases: 4
  completed_phases: 1
  total_plans: 2
  completed_plans: 2
---

# State

## Current Position

Phase: 29 (Cascade Config & Rate Limiting) — COMPLETE (2/2 plans)
Plan: 2 of 2
Status: Phase complete — ready for verification
Last activity: 2026-03-29

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-29)

**Core value:** Surface the best-fit jobs fast and keep the application pipeline visible
**Current focus:** Phase 29 — Cascade Config & Rate Limiting

## Performance Metrics

**Velocity:**

- Total plans completed: 0 (this milestone)

*Updated after each plan completion*

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

Last session: 2026-03-29T23:11:03.012Z
Stopped at: Completed 29-cascade-config-rate-limiting/29-02-PLAN.md
Resume file: None
