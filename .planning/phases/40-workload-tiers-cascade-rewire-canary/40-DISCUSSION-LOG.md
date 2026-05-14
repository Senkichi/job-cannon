# Phase 40: Workload Tiers + Cascade Rewire + Canary - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-05-14
**Phase:** 40-workload-tiers-cascade-rewire-canary
**Areas discussed:** _PROVIDER_DEFAULTS model assignments, Triage auto-resolution location, Case A/B plan strategy, Canary monitoring artifact

---

## _PROVIDER_DEFAULTS model assignments

| Option | Description | Selected |
|--------|-------------|----------|
| Use verified assignments | claude_code_cli/anthropic: haiku-4-5/sonnet-4-6; gemini/gemini_cli: gemini-2.5-flash/gemini-2.5-pro; ollama: qwen2.5:14b for both; local_bundled: quick only (no score entry); groq: llama-3.1-8b-instant/llama-3.3-70b-versatile; cerebras: llama3.1-8b/llama-3.3-70b | ✓ |
| Differentiate ollama quick/score (14b/32b) | ollama quick→qwen2.5:14b, score→qwen2.5:32b | |
| local_bundled attempts score with fallback | Give local_bundled a score entry (same 3B model), cascade falls through | |

**User's choice:** Use verified assignments
**Notes:** Research confirmed PLAN-P1 draft used `gemini-2.0-pro` (never stable GA). Correct IDs are `gemini-2.5-flash`/`gemini-2.5-pro`. Groq deprecated `llama3-8b-8192`/`llama3-70b-8192`; current IDs are `llama-3.1-8b-instant`/`llama-3.3-70b-versatile`. local_bundled 3B GGUF cannot handle the 6-axis ordinal rubric reliably; no score entry is the correct call.

---

## Triage auto-resolution location

| Option | Description | Selected |
|--------|-------------|----------|
| resolve_triage_enabled() in config.py, called by callers | Pure function in config.py; config dict preserves 'auto' for UI round-trip fidelity; orchestrator and settings call it | ✓ |
| Resolve at config load time | load_config() converts 'auto' to bool before returning; loses UI fidelity | |
| Orchestrator resolves inline | Resolution logic in scoring_orchestrator.py; splits _LOCAL_PRIMARIES knowledge | |

**User's choice:** resolve_triage_enabled() in config.py, called by callers
**Notes:** The settings page needs to distinguish "user set auto" from "user explicitly set True" — preserving 'auto' in the config dict is necessary for correct UI rendering. settings.save already does live dict replacement so no restart is needed.

---

## Case A/B plan strategy

| Option | Description | Selected |
|--------|-------------|----------|
| Phase 40 = Case A only; Phase 40b if needed | Linear plans for Case A. Post-audit Phase 40b (if Case B) written with actual callsite names. | ✓ |
| Phase 40 + conditional Case B stubs | Case B stubs in Phase 40 plan; executor gates on audit output mid-execution | |
| Pre-plan both branches fully | Two task groups in Phase 40 plan; references callsites not known until Phase 37 | |

**User's choice:** Phase 40 = Case A only; Phase 40b if needed
**Notes:** Phase 37 D-03 defaults to Case A unless a callsite has no suitable providers, making Case B unlikely. Authoring Phase 40b post-audit (when actual callsite names are known from CASCADE-AUDIT.md) is cheaper than navigating conditional branches mid-execution of an already-complex 13-requirement phase.

---

## Canary monitoring artifact

| Option | Description | Selected |
|--------|-------------|----------|
| Ephemeral — run locally, don't commit | Write query locally, run daily 7 days, discard after canary passes | ✓ |
| Track in git as scripts/canary_query.sql | Commit ~10-line SQL file; methodology record | |
| Admin dashboard canary view | Permanent /admin page addition showing Anthropic-tail rate | |

**User's choice:** Ephemeral — run locally, don't commit
**Notes:** The canary is a one-time 7-day observation window. A committed SQL file provides no live feedback. The admin dashboard option was ruled out as scope creep on an already-complex phase.

---

## Claude's Discretion

- Exact triage prompt text (JD excerpt length, which profile fields to include)
- _PROVIDER_DEFAULTS dict structure for Phase-39 providers not yet implemented
- Test fixture design
- Exact wording of scheduler resume prompt

## Deferred Ideas

- Phase 40b (conditional Case B extension) — authored post-Phase 37 audit if needed
- Ollama score differentiation (qwen2.5:32b via power-user override)
- Cerebras model ID update (llama-3.3-70b deprecation Feb 2026)
- Permanent Anthropic-tail rate admin view (future phase)
