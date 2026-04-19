---
gsd_state_version: 1.0
milestone: v3.0
milestone_name: Single-Tier Ordinal Scoring
status: executing
stopped_at: Wave 1 complete — awaiting user model pulls (qwen3.5:27b, phi4:14b) before Wave 2
last_updated: "2026-04-19T21:30:00.000Z"
last_activity: 2026-04-19 -- Phase 33 Wave 1 executed (Plan 01 preconditions)
progress:
  total_phases: 2
  completed_phases: 0
  total_plans: 2
  completed_plans: 1
  percent: 50
---

# State

## Current Position

Phase: 33
Plan: 01 complete (code); 02 pending (blocked on user model pulls)
Status: Wave 1 complete — awaiting user to pull qwen3.5:27b + phi4:14b before Wave 2
Last activity: 2026-04-19 -- Phase 33 Wave 1 executed (Plan 01 preconditions)

## Project Reference

See: .planning/PROJECT.md (updated 2026-04-18)

**Core value:** Surface the best-fit jobs fast and keep the application pipeline visible
**Current focus:** Milestone v3.0 — Phase 33 (Local-LLM Site-Fitness Survey) about to begin planning

## Milestone Structure (v3.0)

**Phases:** 2 (coarse granularity)
**Requirements:** 77 total, all mapped to phases (0 orphaned)

| Phase | Goal | Requirements | Plans |
|-------|------|--------------|-------|
| 33 — Local-LLM Site-Fitness Survey | Per-site winner matrix drives Phase 34 model selection | 15 (13 SURVEY + SCORER-11 + SCORER-12 preconditions) | TBD (1 est.) |
| 34 — Greenfield Scorer Rewrite | Unified job_scorer + Migration 40/41 + complete legacy deletion | 62 (11 SCORER + 5 MIGRATE + 11 COLLAPSE + 16 CONSUMERS + 20 TESTS − 1 overlap) | 5 (atomic, dependency-ordered) |

## Performance Metrics

**Velocity:** Carried forward from v2.0 (not reset).

| Phase | Plan | Duration | Tasks | Files |
|-------|------|----------|-------|-------|
| 29-cascade-config-rate-limiting | 01 | — | — | — |
| 29-cascade-config-rate-limiting | 02 | — | — | — |
| 30-cascade-execution | 01 | 6min | 2 | 2 |
| 31-prompts-attribution | 01 | 11min | 2 | 4 |
| 31-prompts-attribution | 02 | 12min | 2 | 5 |
| 31-prompts-attribution | 03 | 5min | 1 | 2 |
| 32-integration-config-wiring | 01 | 15min | 3 | 3 |

*Updated after each plan completion*

## Accumulated Context

### Key Design Decisions (v3.0, 2026-04-18)

**Design anchors (locked after deep architectural discussion + research synthesis):**

- **Ordinal 1-5, not continuous 0-100.** Direct response to diagnosed LLM failure mode: qwen2.5:14b produces raw scores of 62-68 for Anthropic baselines spanning 9-72. Calibration cannot repair bimodal raw-to-baseline relationships; isotonic regression is one-to-one by construction. Literature-settled per arXiv 2601.03444 (ICC 0.853 at 0-5 vs 0.840 at 0-100).
- **Six dimensions:** `title_fit`, `location_fit`, `comp_fit`, `domain_match`, `seniority_match`, `skills_match`. Residual collinearity between `title_fit` and `seniority_match` (~r=0.6) acceptable; prompt anchors separate them (role-function vs level-within-function).
- **Four-way classification** (apply/consider/skip/reject) with Python-derived rule: `reject ← legitimacy_note OR any sub-score=1`; `apply ← all >= 3`; `consider ← all >= 2`; else `skip`. Model emits ordinals only — classification is deterministic Python, not LLM free-form. Closes "all 4s but label=skip" self-contradiction bug class.
- **All-LLM assessment** of title/location/comp — no Python pre-parsing. Those fields are chaotic; LLM reads full JD and reconciles.
- **Preserve `fit_analysis` column** — rationale shape unchanged (strengths/gaps/talking_points/resume_priority_skills); out-of-scope modules (resume_generator, interview_prep) keep working with zero code changes.
- **Shared `call_model(tier="scoring", ...)`** dispatcher — no scorer-specific dispatch path. Inherits schema retry, cascade fallback, rate limiting, provider attribution.
- **Delete calibration entirely** — no continuous score means no calibration layer means no latent coupling.
- **`(provider, model)` persisted identity** everywhere — add `jobs.scoring_model` column alongside `scoring_provider`. `(provider, tier)` keying was the v2.0 calibration-invalidation bug root cause.
- **Grammar-constrained decoding** via Ollama `format=<schema dict>` (v0.5+). Deletes `_schema_to_field_instructions`, `_schema_to_example`, and most of `_sanitize_output`. Model physically cannot emit invalid output.
- **Zero new Python dependencies.** `jsonschema 4.26.0` and `pydantic 2.12.5` already installed. pydantic generates JSON schema via `model_json_schema()`; `@dataclass(frozen=True) JobAssessment` is the in-code value type.

**Phase structure (locked):**

- Phase 33 = Phase 1 (shootout). SCORER-11 and SCORER-12 are preconditions; land inside Phase 33.
- Phase 34 = Phase 2 (rewrite). FIVE atomic plans per ARCHITECTURE.md. Each independently revertable. Test suite green at every plan boundary.
- Phases NOT split into 6 separate phases — irreducible couplings (scorer-write + shim, read-swap + allowlist, column-drop + TypedDict) would cross phase boundaries.

**Phase 33 preconditions (must complete before any shootout run):**

1. Fix Ollama options in `ollama_provider.py:197-203` — add `temperature=0`, `seed=42`, `num_ctx=8192`.
2. Pull `qwen3.5:27b` (17 GB) and `phi4:14b` (9.1 GB).
3. Upgrade `OllamaProvider.call()` to pass schema dict to `format=`.
4. Freeze v3.0 scoring prompt (committed) before first shootout run.
5. Filter baseline pool to `scoring_provider='anthropic'` cross-checked with `scoring_costs.provider`.
6. Verify determinism per candidate (5× byte-identical).
7. Enforce per-site sample minima (n≥30/15/10/5 by site class).

**Model shortlist (7 candidates):**

- Primary new pulls: `qwen3.5:27b`, `phi4:14b`
- Baselines already pulled: `qwen2.5:14b` (incumbent/control), `qwen2.5:32b`, `qwen3:14b`, `gemma3:27b`
- Excluded with rationale: `qwen3.5:14b` (tag does not exist on Ollama), `gemma4:26b-moe` (open bug #15260), `deepseek-r1:14b` (reasoning overhead wasted on rubric task)

### Carried Forward from v2.0

- call_model() is the single dispatch point — all callers use logical tier names, never provider-specific model IDs
- Budget gate bypass: free providers (Gemini free tier, Ollama, Groq, Cerebras) skip budget checks entirely
- Schema validation retry: dispatcher retries once with schema errors appended to prompt before falling back
- OllamaProvider uses requests library — stream=False hardcoded
- AnthropicProvider stores job_id and purpose at init, forwards to call_claude for correct cost attribution
- scoring_costs table already has `provider` column; `scoring_costs.model` already stores exact model tag (pattern to replicate for `jobs.scoring_model`)
- Attribution chain in v2.0: call_model() → ModelResult.provider → evaluate_job_sonnet data dict → score_and_persist_sonnet → persist_sonnet_score(provider=) → DB scoring_provider column. v3.0 replaces terminal persist with persist_job_assessment.
- Full suite: 1815 tests passing after v2.0 Phase 31; ~2071 after DataForSEO additions (out-of-band 2026-04-03). v3.0 test count will churn during Plan 4 (deletions) and settle with new `test_job_scorer.py` + modernized fixtures.

### Pending Todos

12 todos in `.planning/todos/pending/` — run `/gsd:check-todos` to review.

### Blockers/Concerns

None. Ready to plan Phase 33.

### Implementation Reference

- Roadmap: `.planning/ROADMAP.md` (written 2026-04-18)
- Requirements: `.planning/REQUIREMENTS.md` (77 requirements, 100% mapped)
- Research synthesis: `.planning/research/SUMMARY.md`
- Architecture (five-plan build order): `.planning/research/ARCHITECTURE.md`
- Stack decisions (Ollama, pydantic, jsonschema): `.planning/research/STACK.md`
- Feature shape (rubric, classification, anti-features): `.planning/research/FEATURES.md`
- Pitfalls (21 items, 12 critical): `.planning/research/PITFALLS.md`
- Historical context: `.planning/CALIBRATION_REFIT_PLAN.md` (the calibration problem v3.0 retires)

## Session Continuity

Last session: 2026-04-18
Stopped at: Roadmap created — ready to discuss/plan Phase 33
Resume file: None
