# Phase 6: Foundation Types & Constants - Context

**Gathered:** 2026-03-23
**Status:** Ready for planning
**Mode:** Auto-generated (infrastructure phase — discuss skipped)

<domain>
## Phase Boundary

The codebase has correct utility modules and constants as foundation for all subsequent changes. This phase creates json_utils.py, scoring_types.py, updates PIPELINE_STATUSES to tuple/frozenset, adds DEFAULT_BORDERLINE_HIGH constant, deletes dead utils.py and output/ package, and generates a non-hardcoded secret key.

</domain>

<decisions>
## Implementation Decisions

### Claude's Discretion
All implementation choices are at Claude's discretion — pure infrastructure phase. Use ROADMAP phase goal, success criteria, and codebase conventions to guide decisions.

</decisions>

<code_context>
## Existing Code Insights

### Reusable Assets
- `job_finder/utils.py` contains `safe_json_load()` — identical to job-finder's `json_utils.py`
- `job_finder/web/blueprints/__init__.py` has PIPELINE_STATUSES as list, needs tuple + frozenset
- `job_finder/config.py` has all scoring/model defaults, needs DEFAULT_BORDERLINE_HIGH = 54

### Established Patterns
- Modules use absolute imports from `job_finder` package root
- Type hints used in function signatures (PEP 8 style)
- Constants use UPPER_SNAKE_CASE

### Integration Points
- `safe_json_load` imported by: db.py, db_helpers.py, main.py (all from `job_finder.utils`)
- After rename: db.py and db_helpers.py need import updated to `job_finder.json_utils`
- main.py import handled by Phase 10 deletion

</code_context>

<specifics>
## Specific Ideas

No specific requirements — infrastructure phase. Files ported directly from job-finder source repo.

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>
