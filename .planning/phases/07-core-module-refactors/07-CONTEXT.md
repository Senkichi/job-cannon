# Phase 7: Core Module Refactors - Context

**Gathered:** 2026-03-23
**Status:** Ready for planning
**Mode:** Auto-generated (infrastructure/refactoring phase — discuss skipped)

<domain>
## Phase Boundary

db.py, scoring orchestrator, description formatter, and claude_client are fully refactored to the job-finder versions. This is the largest change wave — db.py rewrite is ~1450 diff lines. Creates scoring_orchestrator.py and description_formatter.py as new modules.

</domain>

<decisions>
## Implementation Decisions

### Claude's Discretion
All implementation choices are at Claude's discretion — pure refactoring phase. Use ROADMAP phase goal, success criteria, design spec (docs/superpowers/specs/2026-03-23-port-job-finder-improvements-design.md), and codebase conventions to guide decisions.

Key references:
- Source implementations: <other-repo>\job_finder\db.py, web/scoring_orchestrator.py, web/description_formatter.py, web/claude_client.py
- Design spec Wave 2 for ordering and conflict notes

</decisions>

<code_context>
## Existing Code Insights

### Reusable Assets
- Phase 6 created json_utils.py (safe_json_load), scoring_types.py (JobRow, ScoringResult, format_salary_range)
- PIPELINE_STATUSES tuple and VALID_PIPELINE_STATUSES frozenset in blueprints/__init__.py
- DEFAULT_BORDERLINE_HIGH = 54 in config.py

### Established Patterns
- Module-level functions with conn as first arg (target pattern for db.py)
- Absolute imports from job_finder package root
- Type hints on function signatures

### Integration Points
- db.py is imported by: pipeline_runner, haiku_scorer, sonnet_evaluator, scheduler, all blueprints, tests
- claude_client is imported by: haiku_scorer, sonnet_evaluator, pipeline_runner, dashboard blueprint
- web/__init__.py app factory registers format_description filter

</code_context>

<specifics>
## Specific Ideas

No specific requirements — refactoring phase. Files ported directly from job-finder source repo.

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>
