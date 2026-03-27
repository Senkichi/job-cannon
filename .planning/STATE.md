---
gsd_state_version: 1.0
milestone: v1.5
milestone_name: Multi-Provider Model Routing
status: Ready to plan Phase 24
last_updated: "2026-03-27"
progress:
  total_phases: 5
  completed_phases: 0
  total_plans: 0
  completed_plans: 0
---

# State

## Current Position

Phase: 24 of 28 (Provider Foundation)
Plan: —
Status: Ready to plan
Last activity: 2026-03-27 — Roadmap created for v1.5

Progress: [░░░░░░░░░░] 0%

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-27)

**Core value:** Surface the best-fit jobs fast and keep the application pipeline visible
**Current focus:** v1.5 Multi-Provider Model Routing — Phase 24 Provider Foundation

## Performance Metrics

**Velocity:**

- Total plans completed: 0 (this milestone)

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

### Blockers/Concerns

None.

### Implementation Reference

- Design spec: `docs/superpowers/specs/2026-03-27-multi-provider-model-routing-design.md`
- Implementation plan: `docs/superpowers/plans/2026-03-27-multi-provider-model-routing.md`

## Session Continuity

Last session: 2026-03-27
Stopped at: Roadmap written — ready to plan Phase 24
Resume file: None
