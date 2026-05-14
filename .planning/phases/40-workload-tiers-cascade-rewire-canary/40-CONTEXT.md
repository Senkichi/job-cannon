# Phase 40: Workload Tiers + Cascade Rewire + Canary - Context

**Gathered:** 2026-05-14
**Status:** Ready for planning

<domain>
## Phase Boundary

The milestone's inflection point. Three interlocked sub-deliverables in one coordinated phase:

1. **Workload tier rename** — `_TIER_DEFAULTS` (flat `low`/`mid`/`high`/`scoring`) replaced by `_PROVIDER_DEFAULTS` (nested `provider→workload→model`) in `model_provider.py`. Three workload classes: `quick` (all 7 non-scoring callsites), `score` (job scoring), `triage` (pre-scoring gate resolving to the `quick` model). All callsite literals updated: 7 `tier="low"` → `tier="quick"`, `tier="scoring"` → `tier="score"`, legacy constants retired.

2. **Triage gate** — New `job_finder/web/triage.py` with `should_score_job()` (fail-open), wired into `scoring_orchestrator.py` before the `score` call. Settings page exposes a single toggle. Dashboard adds a "Dismissed by triage" filter chip. No DB migration needed — reuses `pipeline_status='dismissed'` + `pipeline_status_history.source='triage_filter'`.

3. **Config rewire + 1-week production canary** — Single `config.yaml` Edit-tool session adopts the new flat `providers:` schema (absorbs Phase 37's Case A/B decision). 7-day observation window validates ≥80% Anthropic-tail spend reduction on the 6 audited callsites.

**Depends on:** Phase 37 (CASCADE-AUDIT.md decision) AND Phase 39 (provider abstraction). Plans for this phase should be written to execute after both dependencies complete.

</domain>

<decisions>
## Implementation Decisions

### _PROVIDER_DEFAULTS model assignments
- **D-01:** Use the following verified model IDs (research confirmed against current provider docs; some PLAN-P1 draft IDs were stale):

| Provider | quick model | score model | Notes |
|----------|-------------|-------------|-------|
| `claude_code_cli` | `claude-haiku-4-5` | `claude-sonnet-4-6` | As prescribed in PLAN-P1.md |
| `anthropic` | `claude-haiku-4-5` | `claude-sonnet-4-6` | Same surface as claude_code_cli; different dispatch path |
| `gemini_cli` | `gemini-2.5-flash` | `gemini-2.5-pro` | `gemini-2.0-pro` was never stable GA; 2.5 variants are the correct current IDs |
| `gemini` (API) | `gemini-2.5-flash` | `gemini-2.5-pro` | Same models as gemini_cli; different dispatch path |
| `ollama` | `qwen2.5:14b` | `qwen2.5:14b` | Phase 33 winner; single-model avoids double-download for strangers. Override via `providers.overrides.ollama.score` for 32b on capable machines |
| `local_bundled` | `Qwen2.5-3B-Instruct-Q4_K_M` | *(absent — no score entry)* | 3B GGUF cannot reliably produce valid 6-axis ordinal rubric output; cascade must route `score` workloads past `local_bundled` |
| `groq` | `llama-3.1-8b-instant` | `llama-3.3-70b-versatile` | `llama3-8b-8192`/`llama3-70b-8192` deprecated; use current IDs |
| `cerebras` | `llama3.1-8b` | `llama-3.3-70b` | Note: Cerebras slug format differs from Groq (`llama3.1-8b` not `llama-3.1-8b`). `llama-3.3-70b` deprecation Feb 2026 — adequate for Phase 40 window |

- **D-02:** `triage` workload resolves to the same model as `quick` for any given provider (by design — triage is a prompt+schema choice, not a capability tier). No `triage` key in `_PROVIDER_DEFAULTS`; `resolve_workload_routing()` maps `triage` → `quick` at lookup time.

### Triage auto-resolution architecture
- **D-03:** `resolve_triage_enabled(config) -> bool` is a pure function in `config.py`, called by both `scoring_orchestrator.py` and `settings.py`. The `'auto'` string is preserved in the live config dict for UI round-trip fidelity (settings page must distinguish "user explicitly set True" from "auto resolved to True for this primary"). No restart required — `settings.save` already performs live dict replacement (`current_app.config["JF_CONFIG"] = config`).
- **D-04:** Auto-resolution rule: `True` when primary is `claude_code_cli`, `gemini`, `gemini_cli`, or `anthropic`; `False` when primary is `ollama` or `local_bundled`. This constant set (`_LOCAL_PRIMARIES`) lives in `config.py` only — orchestrator has zero resolution logic.

### Case A/B plan strategy
- **D-05:** Phase 40 plans implement Case A (single flat cascade) unconditionally. No conditional branch tasks for Case B.
- **D-06:** If Phase 37's CASCADE-AUDIT.md shows Case B is needed (a callsite has no suitable providers under Case A), a separate Phase 40b plan will be authored post-audit using the actual callsite names from the audit output. ~35 LOC per affected callsite (`purpose_overrides` extension in `model_provider.py`).
- **D-07:** The planner should include a final task in the canary plan: "Read CASCADE-AUDIT.md. If Case B is required for any callsite, create Phase 40b plan before canary observation begins."

### Canary monitoring
- **D-08:** Canary SQL query is ephemeral — run locally for 7 days, not committed to git. The audit plan Task 26 prescribes the query shape (`SELECT purpose, provider, COUNT(*) FROM scoring_costs WHERE created_at > NOW()-7d GROUP BY purpose, provider`). No new dashboard view; no scripts/ directory entry.
- **D-09:** Tripwire threshold: if `provider='anthropic'` rows exceed 10% of any callsite's daily total, revert cascade rewire by restoring config.yaml backup (Edit tool only).

### Claude's Discretion
- Exact triage prompt text (JD excerpt length `_JD_EXCERPT_CHARS`, which profile fields to include, prompt wording)
- `_PROVIDER_DEFAULTS` dict structure for providers not yet implemented in Phase 39 (handled as stub entries returning `None` until Phase 39 ships the actual provider classes)
- Test fixture design for `test_workload_routing.py` and `test_triage.py`
- Exact wording of the scheduler resume prompt at end of canary

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Requirements
- `.planning/REQUIREMENTS.md` — STRANGE-TIER-01..03, STRANGE-TRIAGE-01..06, AUDIT-13..16 (13 requirements for this phase)

### Phase specification
- `.planning/ROADMAP.md` §Phase 40 — Phase boundary, 7 success criteria, dependency information, and sequencing rationale

### Source plans
- `.planning/public-release/PLAN-P1.md` §Chunk 3 (Tasks 3.1–3.5) — Workload tiers + triage gate: detailed step-by-step tasks with test code, commit messages, and exact model ID test stubs. Authoritative for task structure.
- `.planning/plans/2026-05-13-local-cascade-audit-plan.md` §Chunk 5 (Tasks 22–26) — Config rewire + canary: Case A/B decision branch, `config.yaml` edit procedure, canary SQL, rollback path.

### Design contracts
- `.planning/public-release/DESIGN.md` §Bucket B — Workload-class tier system design: single flat cascade, per-provider defaults in code, triage auto-default logic, `purpose_overrides` for Case B.

### Phase dependencies (must read before planning)
- `.planning/phases/37-cascade-audit-execution-decision/37-CONTEXT.md` — Produces CASCADE-AUDIT.md; Case A/B routing decision read from this output
- `.planning/phases/39-strangerify-provider-abstraction/39-CONTEXT.md` — Produces new provider classes (claude_code_cli, gemini_cli, local_bundled); Phase 40 registers their `_PROVIDER_DEFAULTS` entries

### Code — primary modification targets
- `job_finder/web/model_provider.py` — Replace `_TIER_DEFAULTS` with `_PROVIDER_DEFAULTS`; add `resolve_workload_routing()`; all tier literal callsites
- `job_finder/web/scoring_orchestrator.py` — Wire triage gate before `call_model(tier="score", ...)`
- `job_finder/web/triage.py` — New module: `TRIAGE_SCHEMA`, `build_triage_prompt()`, `should_score_job()` (fail-open)
- `job_finder/config.py` — New `resolve_triage_enabled(config) -> bool`; validate new flat `providers:` schema; reject old `providers.scoring` shape with migration error
- `config.yaml` + `config.example.yaml` — Single coordinated Edit-tool session rewiring `providers:` block to new schema

### Code — callsites requiring `tier` rename
- `job_finder/web/enrichment_tiers.py` — `tier="low"` → `tier="quick"`
- `job_finder/web/careers_scraper.py` — both callsites: `tier="low"` → `tier="quick"`
- `job_finder/web/ai_career_navigator.py` — `tier="low"` → `tier="quick"`
- `job_finder/web/company_research.py` — `tier="low"` → `tier="quick"`
- `job_finder/web/description_reformatter.py` — `tier="low"` → `tier="quick"`
- `job_finder/web/agentic_enricher.py` — drop direct `OllamaProvider` instantiation → `call_model(tier="quick", ...)`
- `job_finder/web/job_scorer.py` — `tier="scoring"` → `tier="score"`
- `job_finder/web/careers_crawler/_scoring.py` — `tier_has_configured_provider("scoring", ...)` → `"score"`

### Code — UI targets
- `job_finder/web/blueprints/settings.py` — triage toggle (reads/writes `providers.triage.enabled`)
- `job_finder/web/blueprints/dashboard.py` — "Dismissed by triage" filter chip
- `job_finder/web/templates/settings/index.html` — triage toggle UI
- `job_finder/web/templates/jobs/index.html` — new filter chip; triage-reason display

### Critical constraint
- CLAUDE.md: `config.yaml` MUST be modified with Edit tool only (never Write tool) — file has been wiped 3 times by full-file rewrites. Run `bash backup_userdata.sh` before editing.

</canonical_refs>

<code_context>
## Existing Code Insights

### Current state (pre-Phase 40)
- `model_provider.py` has `_TIER_DEFAULTS: dict[str, str]` with flat `low`/`mid`/`high`/`scoring` keys (lines 33-47)
- `resolve_provider_config(tier, config)` at line 138 uses the flat dict
- All 7 non-scoring callsites use `tier="low"`; job_scorer uses `tier="scoring"`
- `agentic_enricher.py` bypasses the tier system entirely — directly instantiates `OllamaProvider`
- No `triage.py` exists; no triage call in `scoring_orchestrator.py`
- `pipeline_status_history` table has `source` and `evidence` columns (precedent: `source='run_scoring_exclusion'`)

### Reusable Assets
- **`pipeline_status_history` pattern** — Triage dismissals use identical schema: `pipeline_status='dismissed'`, `source='triage_filter'`, `evidence=<triage_reason>`. No migration needed.
- **`ProviderCascadeExhaustedError`** — Raised when all cascade providers fail; `triage.py` catches this for fail-open behavior
- **`_maybe_record_cost`** — Cost recording for non-Anthropic paths; triage calls through `call_model` will use this automatically
- **`candidate_score_threshold`** — Existing heuristic gate in `scoring_orchestrator`; triage gate sits between this and the `score` call

### Integration Points
- **`scoring_orchestrator.py`**: triage gate inserts before `call_model(tier="score", ...)`; reads `config['providers']['triage']['enabled']` + calls `resolve_triage_enabled(config)` + dispatches `should_score_job()`
- **`providers/__init__.py`** — May need factory registration for new workload-class lookup (depends on Phase 39 output)
- **CLAUDE.md "Tier-name vestigial labels" section** — Must be updated as part of the `_PROVIDER_DEFAULTS` commit (per PLAN-P1.md Task 3.1 Step 5)

</code_context>

<specifics>
## Specific Ideas

- PLAN-P1.md Task 3.1 provides exact test code for `test_workload_routing.py` including assertion that `_VALID_WORKLOADS == {"quick", "score", "triage"}` and that legacy keys (`low`/`mid`/`high`/`haiku`/`sonnet`/`opus`) are disjoint from `_PROVIDER_DEFAULTS` keys — use these tests verbatim.
- PLAN-P1.md Task 3.5 provides near-complete `triage.py` implementation including the fail-open pattern and test stubs — use as the implementation blueprint.
- Config schema: if old `providers.scoring` key is present, `config.py` raises `ConfigError` pointing to this plan for the migration instructions.

</specifics>

<deferred>
## Deferred Ideas

- **Phase 40b (conditional)** — If Phase 37 produces a Case B decision (callsites needing `purpose_overrides`), a separate Phase 40b plan will be authored post-audit using actual callsite names from CASCADE-AUDIT.md. ~35 LOC per affected callsite.
- **Ollama score differentiation** — `qwen2.5:32b` for score-class on capable machines is available via `providers.overrides.ollama.score` power-user override; not the default to avoid double-download requirement for strangers.
- **Cerebras model update** — `llama-3.3-70b` is scheduled for deprecation Feb 2026; replacement model ID should be updated in a maintenance pass post-Phase 40.
- **Permanent Anthropic-tail rate monitoring** — If per-callsite provider breakdown in the admin panel is useful long-term, it belongs in a dedicated future phase, not in Phase 40's canary window.

</deferred>

---

*Phase: 40-workload-tiers-cascade-rewire-canary*
*Context gathered: 2026-05-14*
