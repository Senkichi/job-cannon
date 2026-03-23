# Phase 8: Consumers - Context

**Gathered:** 2026-03-23
**Status:** Ready for planning
**Mode:** Auto-generated (infrastructure/refactoring phase — discuss skipped)

<domain>
## Phase Boundary

All modules that call db.py, scorers, or scheduler are updated to use the new APIs. Scorers return ScoringResult, profile param is experience_profile, scheduler uses factory functions, gmail has 500-message cap.

</domain>

<decisions>
## Implementation Decisions

### Claude's Discretion
All implementation choices are at Claude's discretion — pure refactoring phase.

</decisions>

<code_context>
## Existing Code Insights

### Reusable Assets
- Phase 6: ScoringResult NamedTuple in scoring_types.py
- Phase 7: scoring_orchestrator.py handles both ScoringResult and plain dict

### Integration Points
- haiku_scorer called by: scoring_orchestrator, pipeline_runner, dashboard
- sonnet_evaluator called by: scoring_orchestrator, pipeline_runner, dashboard
- scheduler called by: web/__init__.py (init_scheduler)
- gmail_source called by: pipeline_runner

</code_context>

<specifics>
## Specific Ideas

No specific requirements — refactoring phase.

</specifics>

<deferred>
## Deferred Ideas

None.

</deferred>
