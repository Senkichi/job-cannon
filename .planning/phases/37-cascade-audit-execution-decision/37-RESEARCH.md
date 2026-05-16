# Phase 37: Cascade Audit Execution & Decision - Research

**Researched:** 2026-05-14
**Status:** Complete

## Standard Stack

### Execution Environment
- Python 3.13 with existing project dependencies (no new packages required)
- SQLite database with Phase 36's `scoring_costs.schema_valid` column for telemetry
- OpenRouter API for DeepSeek-V3.2 judge (uses `OPENROUTER_API_KEY` env var)
- Eval harness from Phase 36: `evals/cascade_audit/` package

### Key Dependencies
- `evals/cascade_audit/` package (built in Phase 36): corpus loader, verdict gates, judge protocol, per-callsite adapters
- `providers/openrouter_provider.py` (built in Phase 36): OpenRouter adapter for judge calls
- Production DB rows in `scoring_costs` table: shadow-replay corpus source
- Audit design spec: `.planning/specs/2026-05-13-local-cascade-audit-design.md` (prescriptive methodology)

## Architecture Patterns

### Three-Round Audit Flow
The audit follows a prescriptive 3-round structure defined in the design spec:

1. **R0 Calibration (n=1-3)**: Dry-run validation of harness and judge protocol
2. **R1 Contenders (n=10)**: Cheap screen to identify promising providers per callsite
3. **R2 Head-to-Head (n=50 objective / n=100 subjective)**: Full battery with statistical confidence

Each round writes atomic artifacts to `evals/cascade_audit/artifacts/round_N/` for resumability.

### Shadow-Replay Methodology
- Corpus: Production DB rows from `scoring_costs` table (non-Anthropic calls only)
- Gold reference: Fresh Anthropic calls during audit execution
- Comparison: Judge evaluates candidate provider output vs Anthropic baseline
- Attribution: `purpose` column (from Phase 35) groups results per callsite

### Verdict Classification
Per design spec section 4:
- **SUITABLE**: Passes all gates (accuracy, latency, cost, schema_valid)
- **MARGINAL**: Fails soft gates but acceptable with warnings
- **UNSUITABLE**: Fails hard gates (accuracy < threshold, schema_valid < 90%)

### Cascade Decision Logic
Per CONTEXT.md decision D-03:
- Default to Case A (single shared cascade)
- Case B (`purpose_overrides`) only when a callsite has NO SUITABLE providers
- Minimizes code complexity (Case B requires ~35 LOC + tests)

## Don't Hand-Roll

### Judge Protocol
- Use Phase 36's `evals/cascade_audit/judge_protocol.py` module
- Implements pairwise-blind comparison + position-swap (DeepSeek-V3.2 via OpenRouter)
- Do not implement custom judge logic

### Verdict Gates
- Use Phase 36's `evals/cascade_audit/verdict_gates.py` ADTs
- Gate thresholds are defined in design spec section 3
- Do not modify gate thresholds without spec amendment

### Statistical Confidence
- Use Wilson score interval for binomial proportion confidence (design spec section 6)
- Do not implement custom confidence interval calculations
- Borderline re-run: n=200 if measurement within 1 Wilson CI half-width of gate boundary

### Artifact Management
- Use Phase 36's artifact writer pattern (atomic writes, environment provenance blocks)
- Do not invent custom serialization formats
- Artifacts are gitignored (`evals/cascade_audit/artifacts/` in .gitignore)

## Common Pitfalls

### Harness Dependency
- Phase 36 must be complete before audit execution
- Verify `evals/cascade_audit/` package exists and tests pass
- Missing harness = audit cannot run (fail-fast per D-01)

### Judge Calibration Errors
- User spot-checks 10 judge verdicts per spec section 5.2
- >2 obvious errors = calibration failure, halt audit (D-01)
- Must diagnose root cause before re-running

### Scheduler Resume Discipline
- Audit harness emits "RESUME SCHEDULERS" prompt at end of Round 2
- No automated checkpoint or verification (D-06)
- User must manually re-enable APScheduler jobs post-audit
- Failure to resume = background jobs stay paused indefinitely

### CASCADE-AUDIT.md Placement
- Must be at repo root, not in `.planning/` (design spec section 7)
- Committed to git (not gitignored)
- Serves as authoritative input to Phase 40's config rewire
- Wrong location = Phase 40 cannot read decision

### Borderline Ambiguity
- Statistical noise can create ambiguous verdicts near gate boundaries
- Always re-run borderline cases (n=200) per D-05
- Skipping re-runs = unreliable cascade decisions

### Case A/B Ambiguity
- Decision must be EXPLICIT in CASCADE-AUDIT.md (success criterion 4)
- Verdict grid alone is insufficient
- Ambiguous decision = Phase 40 blocked on config rewire

## Code Examples

### Audit CLI Entrypoint Pattern
```python
# evals/cascade_audit/run_audit.py (to be created)
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--round', choices=['r0', 'r1', 'r2'], required=True)
    parser.add_argument('--callsite', help='Specific callsite or all')
    args = parser.parse_args()

    harness = AuditHarness(db_path='jobs.db')
    if args.round == 'r0':
        harness.run_calibration()
    elif args.round == 'r1':
        harness.run_contenders()
    elif args.round == 'r2':
        harness.run_head_to_head()
```

### Artifact Write Pattern (from Phase 36)
```python
# Atomic write with environment provenance
artifact = {
    'round': 'r2',
    'callsite': 'parse_structured_fields',
    'timestamp': datetime.now().isoformat(),
    'config_snapshot': load_config_snapshot(),
    'model_versions': get_model_versions(),
    'commit_sha': get_git_sha(),
    'results': {...}
}
write_artifact(f'artifacts/round_2/{callsite}_results.json', artifact)
```

### CASCADE-AUDIT.md Structure (design spec section 7)
```markdown
# Cascade Audit Report

## Executive Summary
- 6 callsites audited
- Case A/B decision: [explicit choice]
- Recommended cascade ordering: [verbatim list]

## Verdict Grid
| Callsite | Provider | Verdict | Sample Size | Confidence | Gates Failed |
|----------|----------|---------|-------------|------------|--------------|
| parse_structured_fields | ollama | SUITABLE | 50 | 95% | None |

## Per-Callsite Recommendations
### parse_structured_fields
- Recommended cascade: [ordering]
- Rationale: [from R2 results]

## Calibration Log
10 spot-checks performed:
- Check 1: [verdict] - [pass/fail]
...
```

## Implementation Notes

### Corpus Selection
- Query `scoring_costs` table for non-Anthropic calls with `purpose` in the 6 audited callsites
- Group by `purpose` for per-callsite analysis
- Exclude Anthropic calls (these are gold reference, not corpus)

### Cost Attribution
- Judge calls via OpenRouter are NOT tracked in `scoring_costs` (they're audit infrastructure)
- Only candidate provider calls are shadow-replayed (no actual production impact)
- Audit is read-only on production DB (no schema changes)

### Resumability
- Each round writes atomic artifacts before proceeding
- If audit fails mid-round, can restart from last successful artifact
- Environment provenance blocks enable reproducibility

### User Workflow
1. Run R0 calibration (validate harness + judge)
2. Run R1 contenders (screen providers per callsite)
3. Run R2 head-to-head (full battery with confidence)
4. Spot-check 10 judge verdicts (calibration verification)
5. Generate CASCADE-AUDIT.md (comprehensive report)
6. Verify Case A/B decision is explicit
7. Resume APScheduler jobs (manual step post-Round 2)

## Validation Architecture

This phase produces a validation artifact (CASCADE-AUDIT.md) that serves as the authoritative input to Phase 40. The validation strategy is:

### Dimension 1: Artifact Completeness
- CASCADE-AUDIT.md exists at repo root
- Contains all required sections (executive summary, verdict grid, per-callsite recommendations, calibration log)
- Case A/B decision is explicit with cascade ordering written verbatim

### Dimension 2: Calibration Verification
- User spot-checks 10 judge verdicts
- ≤2 obvious errors per spec section 11
- Calibration log records which verdicts were checked and results

### Dimension 3: Statistical Confidence
- All measurements include Wilson confidence intervals
- Borderline cases re-run with n=200
- Sample sizes meet design spec minimums (n=50 objective, n=100 subjective for R2)

### Dimension 4: Artifact Integrity
- Raw artifacts present in `evals/cascade_audit/artifacts/` (gitignored)
- Each artifact has environment provenance block (config snapshot, model versions, commit SHA)
- CASCADE-AUDIT.md summaries are derivable from raw artifacts

---

## RESEARCH COMPLETE

The audit methodology is fully specified in the design spec and implementation plan. This phase is execution-focused, not research-heavy. The harness from Phase 36 provides all necessary infrastructure. Key risks are harness dependency, judge calibration errors, and scheduler resume discipline.

*Phase: 37-cascade-audit-execution-decision*
*Research completed: 2026-05-14*
