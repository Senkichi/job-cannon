---
phase: 33-local-llm-site-fitness-survey
plan: 01
subsystem: infra
tags: [ollama, jsonschema, structured-outputs, grammar-constrained-decoding, ordinal-rubric, prompt-engineering, tdd]

# Dependency graph
requires:
  - phase: 32-integration-config-wiring
    provides: OllamaProvider v2.0 (format='json' string, num_predict only)
  - phase: 31-prompts-attribution
    provides: Sonnet fewshot + _FIELD_REINFORCEMENT patterns referenced for prompt authoring
provides:
  - OllamaProvider schema-dict forwarding via payload.format (Ollama v0.5+ GBNF grammar)
  - Deterministic default inference options (temperature=0, seed=42, num_ctx=8192, top_p=0.9, num_predict from max_tokens, repeat_penalty=1.05)
  - Per-call options= kwarg with override-merge-into-defaults (no cross-call state leak)
  - Frozen v3.0 scoring prompt module (V3_SCORING_PROMPT, JOB_ASSESSMENT_SCHEMA, FEWSHOT_EXAMPLES, FIELD_REINFORCEMENT)
  - Single source of truth prompt file shared by Phase 33 Plan 2 (shootout) and Phase 34 Plan 1 (production scorer)
affects: [33-local-llm-site-fitness-survey/02-shootout, 34-greenfield-scorer-rewrite]

# Tech tracking
tech-stack:
  added: []   # zero new Python deps — jsonschema 4.26.0 already installed
  patterns:
    - "Provider adapter v3.0: forward caller-supplied JSON schema dict unchanged via payload.format"
    - "Deterministic benchmark defaults with per-call override merge (no instance state)"
    - "Frozen prompt-as-Python-module — single source of truth, zero copy-drift risk"

key-files:
  created:
    - job_finder/web/scoring_prompts/__init__.py
    - job_finder/web/scoring_prompts/v3_scoring_prompt.py
    - tests/test_v3_scoring_prompt.py
  modified:
    - job_finder/web/providers/ollama_provider.py
    - tests/test_ollama_provider.py

key-decisions:
  - "v3.0 schema-dict path does NOT inject 'EXACTLY these fields' prompt verbiage — grammar-constrained decoding enforces field names at the token level, making the injection redundant and marginally harmful to schema adherence"
  - "Legacy _schema_to_field_instructions and _schema_to_example helpers retained with LEGACY markers (deletion deferred to Phase 34 Plan 4)"
  - "options= kwarg is keyword-only-at-end position to preserve positional-arg backward compat for the 6-arg BaseProvider.call signature"
  - "Default options dict built fresh every call via {**defaults, **overrides} — no mutable instance state, no leak across calls"
  - "Frozen prompt is pure Python module (not YAML/JSON/text file) so schema constant and prompt string share import boundary and same git commit"

patterns-established:
  - "Schema-dict forwarding: isinstance(output_schema, dict) → payload.format = output_schema; else payload.format = 'json'"
  - "Benchmark preconditions land as production code with NO production caller (D-25) — Phase 34 Plan 1 owns the wiring"

requirements-completed:
  - SCORER-11
  - SCORER-12
  - SURVEY-05
  - SURVEY-06
  - SURVEY-07

# Metrics
duration: ~24 min code-change time + ~18 min full-suite validation
completed: 2026-04-19
---

# Phase 33 Plan 01: Preconditions Summary

**OllamaProvider v3.0 now forwards JSON schema dicts through `payload.format` for llama.cpp GBNF grammar-enforced structured output, applies deterministic benchmark inference defaults (temperature=0, seed=42, num_ctx=8192, top_p=0.9, num_predict=max_tokens, repeat_penalty=1.05) with per-call override merge, and ships a frozen v3.0 ordinal-rubric scoring prompt module (`V3_SCORING_PROMPT` + `JOB_ASSESSMENT_SCHEMA`) as the single source of truth for Phase 33 Plan 2 shootout and Phase 34 Plan 1 scorer.**

## Performance

- **Duration:** ~42 min total (code changes ~24 min; full pytest run ~9 min; plus targeted re-runs)
- **Started:** 2026-04-19
- **Completed:** 2026-04-19
- **Tasks:** 3 of 4 completed in code (Task 2 deferred — see below)
- **Files modified/created:** 5 (2 modified, 3 created)

## Accomplishments

- `OllamaProvider.call()` now accepts a JSON schema dict via `output_schema` and forwards it unchanged via `payload["format"]`. Legacy `output_schema=None` → `format="json"` string path preserved for backward compatibility with every existing caller (haiku_scorer, sonnet_evaluator, enrich_job, etc.).
- Deterministic inference options pinned on every call: `temperature=0, seed=42, num_ctx=8192, top_p=0.9, num_predict=max_tokens, repeat_penalty=1.05`.
- New `options=` kwarg merges per-call overrides INTO the defaults; caller-specified keys win, unspecified keys retain defaults. Fresh dict per call — no instance state, no cross-call leak. Test 5 (`test_call_options_override_does_not_leak_across_calls`) proves it directly.
- `job_finder/web/scoring_prompts/` package shipped with `v3_scoring_prompt.py` exporting four module-level constants: `V3_SCORING_PROMPT` (6883 chars), `JOB_ASSESSMENT_SCHEMA`, `FEWSHOT_EXAMPLES` (5 examples spanning ordinal levels 1..5), `FIELD_REINFORCEMENT`.
- Prompt module committed to git before any shootout run — D-26 freeze discipline satisfied.

## Task Commits

Each task was committed atomically using TDD RED/GREEN discipline:

1. **Task 1 RED — OllamaProvider schema-dict + options tests** — `c336d6a` (test)
2. **Task 1 GREEN — OllamaProvider schema-dict forwarding + deterministic inference defaults** — `be3b4e3` (feat)
3. **Task 2 — Pull qwen3.5:27b + phi4:14b** — DEFERRED per wave-1 scope (see Deviations)
4. **Task 3 RED — v3.0 scoring prompt tests** — `7878e3b` (test)
5. **Task 3 GREEN — frozen v3.0 ordinal scoring prompt module** — `171e41d` (feat)

_Per GSD TDD discipline, each TDD task produced 2 commits (test + feat). No refactor commits needed — GREEN implementations were minimal by design._

## Files Created/Modified

- `job_finder/web/providers/ollama_provider.py` (modified, 192 → 253 lines) — schema-dict path, default_options dict with {**defaults, **overrides} merge, new `options=` kwarg, updated module + method docstrings, LEGACY markers on `_schema_to_field_instructions` and `_schema_to_example`
- `job_finder/web/scoring_prompts/__init__.py` (created, 1 line) — package marker
- `job_finder/web/scoring_prompts/v3_scoring_prompt.py` (created, 223 lines) — frozen v3.0 prompt + schema + fewshot + reinforcement constants
- `tests/test_ollama_provider.py` (modified, 363 → 511 lines) — 5 new tests covering schema-dict forwarding, default options, per-call override merge, no-leak-across-calls; 1 existing test updated for the v3.0 behavior change (schema dict → clean system prompt, no field-instruction injection)
- `tests/test_v3_scoring_prompt.py` (created, 195 lines) — 10 tests: importability, six-dimension coverage, behavioral anchors, JSON Schema meta-validation, top-level shape, valid instance acceptance, out-of-range rejection, missing-field rejection, ordinal-level coverage, anti-rename field reinforcement

## Decisions Made

- **Test `test_call_embeds_schema_in_system` was repurposed, not added-then-replaced.** The test previously asserted the legacy schema-dict→system-prompt-injection pattern. Under v3.0, when `output_schema` is a dict, the payload.format IS the dict and the system prompt stays clean (no "EXACTLY these fields" block). Re-repurposing the existing test to pin the new contract avoided creating two tests that overlap. The docstring of the updated test documents the behavior shift explicitly.
- **Legacy helpers retained, not deleted.** `_schema_to_field_instructions` and `_schema_to_example` are marked `# LEGACY:` with a comment pointing to the Phase 34 Plan 4 deletion sweep. Deleting them now would either (a) break the legacy `format='json'` string path if any caller still relied on the injection via their own prompt text, or (b) force a caller migration that belongs in Phase 34 Plan 4's deletion PR. Kept separation of concerns.
- **Zero production caller imports the new prompt module.** Verified by `grep -r "from job_finder.web.scoring_prompts" job_finder/ --include='*.py'` returning empty. Phase 34 Plan 1 owns that wiring.

## SHA256 of Frozen Prompt (load-bearing for Plan 2 reproducibility)

```
V3_SCORING_PROMPT sha256: 255c690e06ee58c87d32dc19ef4abd8ca25e9339eae009a327762f6de2d0c9da
V3_SCORING_PROMPT length: 6883 chars
```

Plan 2's shootout should recompute this hash on import and abort if it drifts — any drift means someone mutated the prompt after Plan 1 committed it (D-26 violation).

## Deviations from Plan

### [Wave-scope] Task 2 (model pulls) deferred to user

**Found during:** Wave-1 scope review per orchestrator prompt.
**Plan said:** Pull `qwen3.5:27b` (~17 GB) and `phi4:14b` (~9.1 GB) via `ollama pull`, smoke-test with `ollama run ... --format json`, then `ollama stop` both models for a clean VRAM baseline.
**What was done:** Skipped. Per the orchestrator's `<wave_scope_guardrails>`, multi-GB Ollama downloads are the user's manual prerequisite for Plan 02 (the shootout), not a Wave-1 code-path gate. Current `ollama list` shows five pulled models (qwen2.5:14b, qwen2.5:32b, gemma3:27b, deepseek-r1:14b, qwen3:14b) — the two Plan-02 candidates are NOT present.
**Why:** Wave-1 is pure code (provider upgrade + prompt module + tests). All Task 1 and Task 3 tests run against mocked HTTP; no test references `qwen3.5:27b` or `phi4:14b`. Deferring the pulls does not block Plan 01's deliverables.
**Recorded as deviation, not escalation:** Plan 01 ships all artifacts it must. The user runs the pulls before launching Plan 02.

### Task 4 (monolithic final commit) inapplicable — GSD per-task commits already done

**Found during:** Task 4 protocol review.
**Plan said:** Stage all five files together and produce a single `feat(33): Phase 33 Plan 1 preconditions ...` commit.
**What was done:** All five files were committed incrementally across four atomic per-task commits (RED/GREEN for Tasks 1 and 3). This is the GSD canonical pattern; a monolithic final commit would undo that discipline.
**Why:** The executor's `<task_commit_protocol>` mandates atomic per-task commits. Task 4's language was written for a non-TDD single-commit variant; the TDD-marked Tasks 1 and 3 override that with RED/GREEN commits. The orchestrator directive to "NOT update STATE.md or ROADMAP.md" means the `docs:` metadata commit that normally bundles a SUMMARY + STATE is also owned by the orchestrator after Wave 1 completes.
**Full test suite verification (Task 4 step 1):** `uv run --active pytest -q --tb=short` run at 2026-04-19; result: **2503 passed, 1 skipped, 1 failed** where the 1 failure is `tests/e2e/test_jobs_page.py::TestAccordionExpandCollapse::test_expand_shows_detail_inline[chromium]`. Confirmed pre-existing Playwright-timing flake unrelated to this plan: re-running the single test in isolation (both with my changes applied AND with my changes stashed) passes. The non-E2E suite (which is where backward-compat regressions would show up) ran 2487 passed, 1 skipped, 0 failed.
**Push to origin:** NOT done — per orchestrator directive Wave 1 completes on commit-only; orchestrator or user handles remote push after Wave 1 metadata commit.

### [Plan drift] test_ollama_provider.py baseline 18 tests not 7

**Found during:** Task 1 RED setup.
**Plan said:** `grep -c "def test_" tests/test_ollama_provider.py` returns >= 7.
**What was done:** File already had 18 passing tests from prior phases; 5 new v3.0 tests added. Final count is 23.
**Why:** The plan's "create tests/test_ollama_provider.py with 7 tests" language assumed a from-scratch file. File already existed (Phase 25 artifact). I preserved every existing test, edited one (`test_call_embeds_schema_in_system`) to reflect the v3.0 behavior change, and added 5 new v3.0-specific tests. Final: 23 tests, all passing.

---

**Total deviations:** 3 documented, 0 auto-fixed code-path deviations. All three are process-level (wave scope, commit protocol, pre-existing test count) — zero code deviations.
**Impact on plan:** All success-criteria deliverables shipped; non-E2E test suite 100% green. Only Task 2 (model pulls) is outstanding, and it is explicitly scoped out of Wave 1.

## Issues Encountered

- **Pre-existing E2E test flake.** `tests/e2e/test_jobs_page.py::TestAccordionExpandCollapse::test_expand_shows_detail_inline[chromium]` fails under full-suite load (server startup + Playwright timing). Confirmed pre-existing: passes in isolation with my changes applied AND with my changes stashed. Not caused by this plan and not within its scope. Logged for future attention — E2E accordion test may need a longer `BASE_TIMEOUT` or an HTMX settle wait.

## User Setup Required

**Before Plan 02 (shootout) runs, the user must pull two Ollama models manually** (Wave-1 guardrail deferred these):

```powershell
ollama pull qwen3.5:27b    # ~17 GB Q4_K_M
ollama pull phi4:14b       # ~9.1 GB Q4_K_M
ollama list | Select-String "qwen3.5:27b", "phi4:14b"
```

Expected: both models appear in `ollama list`. Total download ~26.1 GB. Time depends on network.

No other manual configuration required — Plan 01's code changes do not touch `config.yaml` or any user-data file.

## Next Phase Readiness

- **Phase 33 Plan 02 (shootout) code-gates all cleared.** OllamaProvider forwards schema dicts; deterministic defaults pinned; v3.0 prompt importable at `from job_finder.web.scoring_prompts.v3_scoring_prompt import V3_SCORING_PROMPT, JOB_ASSESSMENT_SCHEMA, FEWSHOT_EXAMPLES, FIELD_REINFORCEMENT`; schema validates against `jsonschema.Draft202012Validator.check_schema`.
- **Phase 34 Plan 01 (scorer skeleton) shares the same prompt module** — zero copy-drift risk.
- **Blocker for Plan 02:** user must `ollama pull qwen3.5:27b && ollama pull phi4:14b` before the shootout script launches. Once those are pulled, Plan 02 has no remaining preconditions.
- **Prompt freeze discipline:** any future change to `job_finder/web/scoring_prompts/v3_scoring_prompt.py` after Plan 02 begins invalidates every candidate already measured. Plan 02 is expected to recompute the SHA256 of `V3_SCORING_PROMPT` on import and abort if it drifts from the hash recorded above.

## Self-Check

- [x] `job_finder/web/providers/ollama_provider.py` — FOUND (modified)
- [x] `job_finder/web/scoring_prompts/__init__.py` — FOUND (created)
- [x] `job_finder/web/scoring_prompts/v3_scoring_prompt.py` — FOUND (created)
- [x] `tests/test_ollama_provider.py` — FOUND (modified)
- [x] `tests/test_v3_scoring_prompt.py` — FOUND (created)
- [x] Commit `c336d6a` (test RED, Task 1) — FOUND in git log
- [x] Commit `be3b4e3` (feat GREEN, Task 1) — FOUND in git log
- [x] Commit `7878e3b` (test RED, Task 3) — FOUND in git log
- [x] Commit `171e41d` (feat GREEN, Task 3) — FOUND in git log
- [x] Schema validates via `jsonschema.Draft202012Validator.check_schema` — VERIFIED
- [x] `V3_SCORING_PROMPT` length > 2000 chars (actual: 6883) — VERIFIED
- [x] `tests/test_ollama_provider.py` 23 tests pass — VERIFIED
- [x] `tests/test_v3_scoring_prompt.py` 10 tests pass — VERIFIED
- [x] Non-E2E full suite (2487 tests) green — VERIFIED

## Self-Check: PASSED

---
*Phase: 33-local-llm-site-fitness-survey*
*Plan: 01-preconditions*
*Completed: 2026-04-19*
