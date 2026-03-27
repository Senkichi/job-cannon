---
gsd_state_version: 1.0
milestone: v1.5
milestone_name: Multi-Provider Model Routing
status: executing
stopped_at: Completed 27-caller-migration 27-03-PLAN.md
last_updated: "2026-03-27T21:16:20.634Z"
last_activity: 2026-03-27
progress:
  total_phases: 4
  completed_phases: 1
  total_plans: 3
  completed_plans: 3
  percent: 100
---

# State

## Current Position

Phase: 26 (Dispatcher & Cost Tracking) — EXECUTING
Plan: 3 of 3 COMPLETE
Status: Ready to execute
Last activity: 2026-03-27

Progress: [██████████] 100%

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-27)

**Core value:** Surface the best-fit jobs fast and keep the application pipeline visible
**Current focus:** Phase 26 — Dispatcher & Cost Tracking

## Performance Metrics

**Velocity:**

- Total plans completed: 3 (this milestone)

*Updated after each plan completion*

## Accumulated Context

### Key Design Decisions (v1.5)

- call_model() is the single dispatch point — all callers use logical tier names ("sonnet", "haiku", "opus"), never provider-specific model IDs
- call_claude() internals stay untouched; Anthropic adapter wraps them (no behavior change for existing paths)
- Budget gate bypass: free providers (Gemini free tier, Ollama) skip budget checks entirely — cost_gate not called
- Schema validation retry: dispatcher retries once with schema errors appended to prompt before falling back to Anthropic
- Configurable fallback: if retry fails, re-dispatch to Anthropic (not hardcoded — config specifies fallback provider)
- COST-01 DB migration adds `provider` column with 'anthropic' default — existing rows unaffected
- eval_results/ directory for evaluation JSON reports (CLI tool, not web UI)
- Phase 28 (Evaluation Framework) depends on Phase 26 (Dispatcher) but is independent of Phase 27 (Caller Migration)
- OllamaProvider uses requests library (not ollama SDK) — stream=False is hardcoded correctness requirement, format=json guarantees parseable output
- OllamaProvider health check on init with 5s timeout — prevents silent failures during Flask startup
- Schema embedded in system prompt for Ollama (lacks native schema enforcement); format=json guarantees valid JSON

### Decisions Made in Phase 26 Plan 01

- Lazy imports inside _make_adapter() to break circular import: model_provider imports providers which import model_provider (Plan 01)
- AnthropicProvider patched at job_finder.web.providers.anthropic_provider (lazy import site), not at model_provider module level (Plan 01)
- record_cost() provider parameter with 'anthropic' default — backwards compatible, all 30+ existing callers unaffected (Plan 01)
- _maybe_record_cost() guards on result.provider == 'anthropic' to prevent double-recording — call_claude() handles Anthropic cost internally (Plan 01)
- Fallback model resolved with resolve_provider_config(tier, {}) (empty config) to get Anthropic default, not the Gemini/Ollama model string (Plan 01)

### Blockers/Concerns

None.

### Implementation Reference

- Design spec: `docs/superpowers/specs/2026-03-27-multi-provider-model-routing-design.md`
- Implementation plan: `docs/superpowers/plans/2026-03-27-multi-provider-model-routing.md`

## Session Continuity

Last session: 2026-03-27T21:16:20.631Z
Stopped at: Completed 27-caller-migration 27-03-PLAN.md
Resume file: None
