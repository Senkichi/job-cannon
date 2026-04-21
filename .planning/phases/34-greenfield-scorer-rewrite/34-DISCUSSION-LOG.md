# Phase 34: Greenfield Scorer Rewrite - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-04-21
**Phase:** 34-greenfield-scorer-rewrite
**Areas discussed:** use_unified_scorer flag cadence, Plan 5 backup gate, one-off rescore scope, rescore validation methodology, rescue/post-fail protocol
**Mode:** Interactive (autonomous `--only 34 --interactive`)

---

## Gray area identification (pre-discussion)

9 gray areas initially surfaced. User elected the "drive with recommended defaults" path, discussing 3 operationally load-bearing ones (`#2 flag cadence`, `#4 backup gate`, `#5 rescore scope`) and accepting my recommendations on the other 6. User subsequently surfaced 2 additional concerns via follow-up: (a) explicit validation of rescored outputs, (b) post-fail action protocol.

### Recommended defaults accepted without discussion

| # | Area | Default (accepted) |
|---|------|--------------------|
| 1 | Shim math fidelity (Plan 2) | `mean × 20` — accept distribution drift; ordering preserved, calibration deleted |
| 3 | `legitimacy_note` sourcing | Use existing column; scorer does not emit it |
| 6 | Cascade fallback for scoring tier | Full cascade (Ollama → Groq → Cerebras → Gemini → Anthropic) |
| 7 | `liveness_check` placement | Stays pre-score in `run_scoring()` |
| 8 | PROMPT_VARIANTS fate | Delete (single-model decision from Phase 33) |
| 9 | D-19 determinism redefinition | Doc-only update in Plan 1's test harness |

---

## Area 1: `use_unified_scorer` flag cadence (Plan 2)

| Option | Description | Selected |
|--------|-------------|----------|
| 1 | Ship `default: False`, flip to `True` in follow-up commit (Recommended) | ✓ |
| 2 | Ship `default: True` directly | |
| 3 | Ship `default: False`, require manual user edit | |

**User's choice:** Option 1 — two-step rollout
**Rationale:** Smoke-test window where dual-write code is live but reads remain legacy. Revert one commit if a bug surfaces; don't have to undo config changes.

---

## Area 2: Plan 5 backup gate mechanism

| Option | Description | Selected |
|--------|-------------|----------|
| 1 | File-mtime check + `GSD_BACKUP_CONFIRMED=1` override, fail-closed (Recommended) | ✓ |
| 2 | Env-var only | |
| 3 | Interactive confirmation at runtime | |
| 4 | Manual pre-flight only, no code gate | |

**User's choice:** Option 1 — file-mtime check + env-var override
**Rationale:** Structural safety that's friendly to autonomous execution. Env-var bypass lets user take the backup via a different path (manual rsync, snapshot, etc.) and still proceed. Fail-closed default is safer than fail-open given destructive migration.

---

## Area 3: One-off rescore of ~3900 existing jobs — scope

| Option | Description | Selected |
|--------|-------------|----------|
| 1 | Standalone script between Plan 4 and Plan 5 (Recommended) | |
| 2 | Bundled into Plan 5 (single atomic plan) | |
| 3 | Part of Plan 4 (final task before legacy-write removal) | ✓ |
| 4 | New decimal phase 34.5 | |

**User's choice:** Option 3 — part of Plan 4 (final task)
**Rationale:** Dual-write is still active during rescore (Plan 2's shim continues to populate legacy columns), so no data is at risk if rescore crashes mid-flight. Keeps Plan 5 a tight DROP COLUMN commit. Claude's original recommendation (Option 1) was reconsidered after the user flagged that validation needs dual-write active to be meaningful.

---

## Follow-up: Explicit validation of rescored outputs

**User question:** "Is there explicit validation of the rescored outputs against legacy data built into the process?"

**Gap identified:** ARCHITECTURE.md's stated rescore criterion was row-count convergence only (completeness, not correctness).

**Claude proposed 4-gate validation scheme:**
- G1 — Completeness (row count)
- G2 — Distribution monotonicity (legacy-score bucket → new classification distribution is monotonic)
- G3 — Numeric-ordinal correlation (Pearson r ≥ 0.5)
- G4 — Production-path refit (Phase 33's 100-row baseline through `job_scorer.score_job()`)

**User extended:** proposed batched rescore with per-batch validation to fast-fail on code-path bugs and catch sample bias.

### Batch structure (user-proposed, Claude-refined)

| Batch | Rows | Wall-clock | Purpose |
|-------|------|------------|---------|
| B1 | 150 | ~22 min | Fast-fail: code-path correctness + basic distribution shape |
| B2 | 1000 | ~2.5 h | Sample-bias protection on real volume |
| B3 | ~2750 | ~7 h | Finish |

### Refinements Claude added (user accepted via "Looks good!")

- Stratify row selection by legacy `sonnet_score` quartile so each batch tests monotonicity meaningfully
- G4 runs once before B1 (code-path sanity check; not per-batch)
- Gate threshold strictness ratchets with sample size (G2/G3 loose on B1, strict on B2/B3)
- Plan 4 commit structure revised to accommodate batched rescore + fix commits interleaved

---

## Follow-up: Post-fail action protocol

**User's direction:** "The post fail action should always be 'investigate root cause and iteratively troubleshoot, then continue once fixed', not 'halt all forward progress until user manually specifies to that the issue should be fixed'"

**Protocol locked:**

1. On gate failure: invoke `/systematic-debugging` skill (or equivalent logic)
2. Hypothesis → minimal repro → fix → commit → re-validate
3. Iterate up to 3 cycles / 2h wall-clock per gate
4. **Escalation only when fix instinct is clearly wrong:**
   - Metric oscillates across attempts (not converging)
   - Fixes regress other gates (tension in gate definitions)
   - 3-cycle ceiling reached without metric improvement (structural issue, not a bug)
   - Root cause is non-code (hardware, GPU OOM, data corruption)
   - Fix would require reverting a Phase 33 locked decision (prompt, model)

Escalation output is a structured handoff with full diagnostic history, not a cry for help.

---

## Claude's Discretion (areas where Claude has latitude per CONTEXT.md)

- Exact CLI arg names for `scripts/v3_rescore.py` / `scripts/v3_rescore_validate.py`
- `rescore-batch-N-report.json` schema (as long as it includes G1-G4 metrics with thresholds and pass/fail)
- Stratified-sampling SQL implementation detail (window functions vs subquery vs Python-side)
- Test parametrization approach for classification rule edge cases
- Commit message bodies (follow project conventional-commit format)

---

## Deferred Ideas

Captured in CONTEXT.md `<deferred>` section:
- Explicit ordinal-stability probe (v3.1 or later)
- `fit_analysis` column rename
- `opus_score` rebaseline
- `eval_blocks` / `score` cleanup sweeps
- D-23 tiebreaker bias-weighting patch (Phase 33 carry-forward)
- Cosmetic Opus-spend display bug
- Per-site model routing exploration

No scope creep during discussion — all user concerns stayed within Phase 34 boundary.
