---
gsd_state_version: 1.0
milestone: v5.0
milestone_name: Public Release Foundation — Cascade Audit + Strangerify P1 + PyPI
status: in-progress
stopped_at: Phase 37 R0 + R1 complete; R2 + report generation + verification deferred to next session
last_updated: "2026-05-14T20:30:00.000Z"
last_activity: 2026-05-14 — Phase 37 plan 01 partial: R0 calibration + R1 contenders rounds executed; phase 36 harness defects patched
progress:
  total_phases: 11
  completed_phases: 1
  total_plans: 6
  completed_plans: 1
  percent: 17
---

# State

## Current Position

Milestone: v5.0 Public Release Foundation — Cascade Audit + Strangerify P1 + PyPI
Phase: 37 — Cascade Audit Execution & Decision — IN PROGRESS (R0 + R1 done; R2 + report + 10-spot-check + verify pending)
Plan: 1 of 1 — partial (tasks 01-01, 01-02, 01-03, 01-04, 01-08, 01-09 done; 01-05, 01-06, 01-07, 01-10 deferred to next session)
Status: Phase 37 R0/R1 complete on cranky-satoshi-410e36 worktree; multiple phase 36 harness defects patched in-flight, full follow-up audit pending
Last activity: 2026-05-14 — R0 calibration + R1 contenders rounds executed against main repo's jobs.db + config.yaml; artifacts in evals/cascade_audit/artifacts/round_0/ and round_1/

## Project Reference

See: .planning/PROJECT.md (updated 2026-05-13 — Current Milestone section added)

**Core value:** Surface the best-fit jobs fast and keep the application pipeline visible
**Current focus:** Phase 35 complete, ready for Phase 36

## Milestone Structure (v5.0)

**Phases:** 11 (standard granularity)
**Requirements:** 60 forward requirements, all mapped to phases (0 orphaned, 0 duplicated). Plus 5 retroactively-validated requirements excluded from phase mapping (already shipped).

| Phase | Goal | Requirements | Plans |
|-------|------|--------------|-------|
| 35 — Audit Telemetry & Callsite Attribution | Per-callsite cost telemetry with schema-validity attribution | 4 (AUDIT-01..04) | TBD |
| 36 — Cascade Audit Eval Harness | Offline shadow-replay + DeepSeek-V3.2 judge for the 6 non-scoring callsites | 4 (AUDIT-05..08) | TBD |
| 37 — Cascade Audit Execution & Decision | R0/R1/R2 audit produces CASCADE-AUDIT.md + Case A/B decision | 4 (AUDIT-09..12) | TBD |
| 38 — Strangerify Foundation | platformdirs + onboarding_state table + config no-longer-fail-fast + personal-data audit | 5 (STRANGE-FOUND-01..05) | TBD |
| 39 — Strangerify Provider Abstraction | _PROVIDER_DEFAULTS + auto-detection + claude_code_cli/gemini_cli/local_bundled | 5 (STRANGE-PROV-01..05) | TBD |
| 40 — Workload Tiers + Cascade Rewire + Canary | quick/score/triage + triage gate + single config rewrite (absorbs Case A/B) + 7-day canary | 13 (STRANGE-TIER-01..03, STRANGE-TRIAGE-01..06, AUDIT-13..16) | TBD |
| 41 — Strangerify Data Sources | IMAP source default + parser verification + PDF/DOCX resume parser | 4 (STRANGE-INGEST-01..03, STRANGE-RESUME-01) | TBD |
| 42 — Onboarding Wizard | 7-step Flask blueprint + system check + IMAP smoke test | 6 (STRANGE-WIZ-01..06) | TBD |
| 43 — Update Check, Legal, Strangerify Exit Gate | GitHub Releases banner + AGPL/PRIVACY/AUP/SECURITY + fresh-clone stranger validation | 6 (STRANGE-UPDATE-01, STRANGE-LEGAL-01..04, STRANGE-GATE-01) | TBD |
| 44 — PyPI Release Pipeline & Install Docs | PyPI name + trusted publishing + release.yml + INSTALL.md + README rewrite | 5 (PYPI-01..03, PYPI-07, PYPI-08) | TBD |
| 45 — Cross-Platform pipx Validation & Exit Gate | Win/macOS/Linux pipx install validated + ≥5-strangers gate | 4 (PYPI-04..06, PYPI-09) | TBD |

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
| 35-audit-telemetry-callsite-attribution | 01 | 25min | 10 | 7 |

*Updated after each plan completion*

## Accumulated Context

### Key Sequencing Decisions (v5.0, 2026-05-13)

**Canary collapsed into Phase 40 (not standalone after Phase 37).** The audit's 1-week production canary (AUDIT-15/16) and config rewire (AUDIT-13/14) run inside the Strangerify workload-tier phase rather than under the soon-to-be-deleted `low`/`mid`/`high` schema. Rationale: (a) `config.yaml` has been wiped three times by full-file writes — a single Edit-tool session is structurally safer than two sequenced rewrites; (b) canary observes the FINAL workload-class schema, not throwaway state; (c) Audit Chunks 1-4 (Phases 35-37) finalize the Case A/B decision in `CASCADE-AUDIT.md` as a paper artifact, so the decision is not blocked by the sequencing choice — only the wire-up + observation slide.

**Strangerify Foundation + Provider abstraction (Phases 38-39) can ship before Phase 40 cuts over the tier system.** Foundation does not touch tiers. Provider abstraction adds the three new providers (`claude_code_cli`, `gemini_cli`, `local_bundled`) but keeps `low`/`mid`/`high` working until Phase 40 deletes them.

**Strangerify Data Sources (Phase 41) parallels Phases 39-40 in principle.** It depends on Phase 38 (config + user-data dirs) but not on the cascade. If scheduling allows, it can land before Phase 40 ships canary.

**PyPI (Phases 44-45) waits for Strangerify exit gate (Phase 43).** Publishing a `pipx install job-cannon` that ships a broken stranger experience would burn the launch. PYPI-01..03 (registration + release.yml + trusted publishing) are technically independent but stay in Phase 44 to ensure the first published artifact carries post-Strangerify code.

### Key Design Decisions (v3.0, 2026-04-18) — carried forward

**Design anchors (locked after deep architectural discussion + research synthesis):**

- **Ordinal 1-5, not continuous 0-100.** Direct response to diagnosed LLM failure mode: qwen2.5:14b produces raw scores of 62-68 for Anthropic baselines spanning 9-72. Calibration cannot repair bimodal raw-to-baseline relationships; isotonic regression is one-to-one by construction. Literature-settled per arXiv 2601.03444 (ICC 0.853 at 0-5 vs 0.840 at 0-100).
- **Six dimensions:** `title_fit`, `location_fit`, `comp_fit`, `domain_match`, `seniority_match`, `skills_match`. Residual collinearity between `title_fit` and `seniority_match` (~r=0.6) acceptable; prompt anchors separate them (role-function vs level-within-function).
- **Four-way classification** (apply/consider/skip/reject) with Python-derived rule: `reject ← legitimacy_note OR any sub-score=1`; `apply ← all >= 3`; `consider ← all >= 2`; else `skip`. Model emits ordinals only — classification is deterministic Python, not LLM free-form.
- **All-LLM assessment** of title/location/comp — no Python pre-parsing. Those fields are chaotic; LLM reads full JD and reconciles.
- **Preserve `fit_analysis` column** — rationale shape unchanged.
- **Shared `call_model(tier=...)` dispatcher** — no scorer-specific dispatch path. Inherits schema retry, cascade fallback, rate limiting, provider attribution. (v5.0 renames `tier="scoring"` → `tier="score"` in Phase 40.)
- **Delete calibration entirely** — no continuous score means no calibration layer.
- **`(provider, model)` persisted identity** — `jobs.scoring_model` + `scoring_provider` columns. `(provider, tier)` keying was the v2.0 calibration-invalidation bug root cause.
- **Grammar-constrained decoding** via Ollama `format=<schema dict>` (v0.5+). Model physically cannot emit invalid output.

### Carried Forward from v2.0

- `call_model()` is the single dispatch point — all callers use logical tier names, never provider-specific model IDs
- Budget gate bypass: free providers (Gemini free tier, Ollama, Groq, Cerebras) skip budget checks entirely
- Schema validation retry: dispatcher retries once with schema errors appended to prompt before falling back
- OllamaProvider uses requests library — stream=False hardcoded
- AnthropicProvider stores job_id and purpose at init, forwards to call_claude for correct cost attribution
- scoring_costs table already has `provider` column; `scoring_costs.model` already stores exact model tag

### Pending Todos

12 todos in `.planning/todos/pending/` (unchanged at v3.0 close; not triaged during retroactive close ceremony) — run `/gsd-check-todos` to review.

### Blockers/Concerns

**`config.yaml` Edit-tool discipline (v5.0 critical).** The file has been wiped three times by full-file rewrites that intended to change a single value. Phase 40 rewrites the entire `providers:` block; `backup_userdata.sh` must run before the edit and the rewrite must use the Edit tool exclusively (surgical string replacements), never the Write tool.

**Two consecutive renames of the tier system.** `haiku`/`sonnet`/`opus` → `low`/`mid`/`high` shipped post-v4.0 (validated retroactively in v5.0). Phase 40 renames again to `quick`/`score`/`triage` (workload classes). The intermediate `low`/`mid`/`high` schema lives between v4.0 close and Phase 40 cutover — any new code added during Phases 35-39 must use the current (`low`/`mid`/`high`) names and will be migrated in Phase 40.

**Audit decision paper-trail discipline.** Phase 37's `CASCADE-AUDIT.md` is the authoritative input to Phase 40's config rewire. If the audit results are ambiguous, Phase 37 must not exit until the Case A/B decision is explicit. Phase 40 cannot derive the cascade ordering on its own.

### Implementation Reference

- Roadmap: `.planning/ROADMAP.md` (updated 2026-05-13 with v5.0 phases 35-45)
- Requirements: `.planning/REQUIREMENTS.md` (60 forward requirements, traceability table populated 2026-05-13)
- v4.0 milestone audit (retroactive placeholder): `.planning/milestones/v4.0-MILESTONE-AUDIT.md`
- v4.0 roadmap archive: `.planning/milestones/v4.0-ROADMAP.md`
- v3.0 milestone audit (retroactive placeholder): `.planning/milestones/v3.0-MILESTONE-AUDIT.md`
- Stage 1 gate blockers (3 user-action items, deferred): `.planning/portfolio-cleanup/STAGE-1-GATE-BLOCKERS.md`
- v5.0 source plans:
  - `.planning/plans/2026-05-13-local-cascade-audit-plan.md` (cascade audit; 5 chunks)
  - `.planning/public-release/PLAN-P1.md` (Strangerify; 6 chunks)
  - `.planning/public-release/DESIGN.md` (full P1-P4 design; P2 absorbed)

## Session Continuity

Last session: 2026-05-14 evening — Phase 37 R0 + R1 executed; phase 36 harness defects patched in-flight
Stopped at: Phase 37 plan 01 tasks 01-05 (R2), 01-06 (10 spot-checks), 01-07 (CASCADE-AUDIT.md), 01-10 (verify completeness) deferred
Resume file: n/a (continue by running `/gsd-execute-phase 37` against the same worktree; dedup_keys.json locks R0 sample so R2 measures comparably)

**Phase 36 audit followup REQUIRED before phase 37 completes:** During R0/R1 execution multiple phase 36 harness defects surfaced. Two classes already patched in-flight (commit 3930315):
1. corpus_loader schema mismatch (companies.dedup_key → CAST(id AS TEXT)) — 4 sampling methods + _load_by_keys
2. corpus_loader UTF-8 encoding (5 write_text/read_text/open call sites — Windows cp1252 default crashed on real production JD content)
3. ai_nav_discovery_adapter recipe filename mismatch (used raw dedup_key, corpus_loader uses _safe_cache_stem)
4. extract_jobs_adapter missing lazy HTML caching (spec required R1-start HTML fetch; harness had no fetcher)
5. test_audit_execution.py fixture used dedup_key TEXT companies (matched bug, not production)

Still un-patched (surface only as row-level failures inside artifacts — R2 cannot meaningfully test these callsites until fixed):
- description_reformat → OpenRouter API returns 404 (model endpoint stale or model name changed)
- company_research → production code path expects company_research table that does not exist in production DB
- ai_nav_discovery → discover_navigation_recipe() does not accept recipe= keyword (signature drift between adapter and production)
- parse_structured_fields → score function sets schema_valid=False even when candidate returns the expected keys (score-fn bug)
- R1 sample_size = 3 not 10 (corpus_loader R1 reuses R0's n_per_callsite=3 keys; plan expected R1 to sample n=10)

**Next step (recommended):**
1. `/gsd-validate-phase 36` — retroactive audit of phase 36 harness against production schemas + signatures (use this session's defect list as the seed set)
2. Apply phase 36 gap-closure plan to fix the remaining 5 defects above
3. Re-run R0 + R1 on the corrected harness for clean data
4. `/gsd-execute-phase 37 --tasks 01-05,01-06,01-07,01-10` (R2 + 10-spot-checks + CASCADE-AUDIT.md + verify completeness)

**Branch reconciliation note (this session):**
- main repo at cc28393 — untracked all .planning/ files per .gitignore:62 policy (was force-tracked at 9951b49 merge); planning artifacts remain on disk only
- this worktree (cranky-satoshi-410e36) at ba0944a — force-tracks phase 37 planning + ROADMAP + REQUIREMENTS + PROJECT (continuing the existing worktree pattern); main and this worktree both descend from merge 9951b49 but have diverged
- friendly-kilby-2d64a5 worktree at 2bf7fd7 — stale, predates wip(37) and phase 39 merge

**Planned Phase:** 37 (Cascade Audit Execution & Decision) — 1 plans — 2026-05-14T15:37:35.728Z (PARTIAL — see Session Continuity above)
