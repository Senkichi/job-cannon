# Phase 37: Cascade Audit Execution & Decision - Context

**Gathered:** 2026-05-14
**Status:** Ready for planning

<domain>
## Phase Boundary

Execute the 3-round shadow-replay audit (R0 calibration / R1 contenders / R2 head-to-head) using the harness built in Phase 36, produce CASCADE-AUDIT.md with per-callsite verdicts, and record the Case A/B routing decision for Phase 40's config rewire. The audit covers all 6 non-scoring LLM callsites: parse_structured_fields, find_careers_url, extract_jobs, description_reformat, company_research, ai_nav_discovery.

</domain>

<decisions>
## Implementation Decisions

### Audit failure handling
- **D-01:** Abort immediately on any catastrophic failure (fail-fast)
- Harness bugs, provider outages, or judge calibration errors (>2 spot-check failures) halt the audit without retry attempts
- User must diagnose and fix the root cause before re-running

### Marginal verdict handling
- **D-02:** MARGINAL providers enter the cascade with a warning in CASCADE-AUDIT.md
- Only UNSUITABLE providers are excluded from the cascade
- Warnings document which gates caused the MARGINAL verdict for transparency

### Case A vs Case B decision criteria
- **D-03:** Default to Case A (single shared cascade) unless a callsite has NO SUITABLE providers
- Case B (purpose_overrides) is triggered only when a callsite's best verdict is MARGINAL or UNSUITABLE across all providers
- This minimizes code complexity (Case B requires ~35 LOC + tests) while preserving an escape hatch for edge cases
- If multiple callsites need Case B, each gets its own purpose_overrides entry

### CASCADE-AUDIT.md presentation
- **D-04:** Comprehensive report with full detail
- Includes: verdict grid, sample sizes, gate measurements, confidence intervals, recommended cascade ordering, raw data tables, per-round summaries, risk callouts, calibration log (10 spot-check results), and explicit Case A/B decision
- Serves as authoritative input to Phase 40's config rewire

### Borderline re-run policy
- **D-05:** Always re-run borderline cases for stronger confidence
- Any measurement within 1 Wilson CI half-width of a gate boundary triggers an n=200 re-run
- This eliminates ambiguity from statistical noise before committing to cascade decisions
- Additional cost is acceptable given the audit's one-time nature

### Scheduler pause/resume discipline
- **D-06:** Audit harness emits explicit "RESUME SCHEDULERS" prompt at end of Round 2
- No automated checkpoint or verification — relies on user discipline
- Spec already requires this prompt; implementation must ensure it's visible and unmissable

### Claude's Discretion
- Exact formatting of the CASCADE-AUDIT.md tables and sections
- Wording of risk callouts and calibration log entries
- Prompt message text for scheduler resume reminder

</decisions>

<specifics>
## Specific Ideas

- The audit design spec (`.planning/specs/2026-05-13-local-cascade-audit-design.md`) is prescriptive about methodology — this phase is execution, not design
- Follow the spec's 3-round flow exactly: R0 (dry-run n=1-3), R1 (cheap screen n=10), R2 (full battery n=50 objective / n=100 subjective)
- CASCADE-AUDIT.md should be committed to repo root, not in .planning/, per spec section 7
- User spot-checks 10 judge verdicts per spec section 5.2; calibration log records which ones were checked and whether they passed

</specifics>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Audit design spec
- `.planning/specs/2026-05-13-local-cascade-audit-design.md` — Complete methodology: 3-round flow, verdict gates, judge protocol, success criteria, cost estimates

### Implementation plan
- `.planning/plans/2026-05-13-local-cascade-audit-plan.md` — Chunk 3 tasks (audit execution), CLI invocation, artifact structure

### Phase dependencies
- `.planning/phases/36-cascade-audit-eval-harness/36-CONTEXT.md` — Harness output: evals/cascade_audit/ package, OpenRouter adapter, per-callsite adapters

### Phase consumer
- `.planning/ROADMAP.md` (Phase 40 entry) — Workload Tiers + Cascade Rewire + Canary phase that consumes CASCADE-AUDIT.md

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `evals/cascade_audit/` package (built in Phase 36): corpus loader, verdict gates, judge protocol, per-callsite adapters
- OpenRouter provider adapter (`providers/openrouter_provider.py`) for DeepSeek-V3.2 judge
- Existing telemetry: `scoring_costs` table with `purpose` column (Phase 35 added `schema_valid` column)

### Established Patterns
- Shadow-replay methodology: production DB rows as inputs, fresh Anthropic calls as gold reference
- Atomic artifact writes to `evals/cascade_audit/artifacts/round_N/` for resumability
- Environment provenance blocks in artifacts (config snapshot, model versions, commit SHA)

### Integration Points
- Audit CLI entrypoint: `evals/cascade_audit/run_audit.py` (to be created in this phase)
- CASCADE-AUDIT.md output location: repo root (committed, not gitignored)
- Phase 40 reads CASCADE-AUDIT.md to determine Case A/B decision and cascade ordering

</code_context>

<deferred>
## Deferred Ideas

- Building Groq/Cerebras production adapters (audit may motivate this as follow-on, but out of scope for this phase)
- Per-provider bias correction in production scoring (noted in spec as architectural follow-on)
- Re-evaluating the scoring tier (already audited in Phase 33)

</deferred>

---

*Phase: 37-cascade-audit-execution-decision*
*Context gathered: 2026-05-14*
