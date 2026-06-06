# Parser Auto-Heal — Phase C Design Plan (NOT yet decomposed into issues)

> **Status:** DESIGN-LEVEL. This is the locked approach for Phase C, captured now to record the roadmap. It is **not** a headless-ready, bite-sized implementation plan and **must not** be converted into `automated-ready` issues until Phase B lands — Phase C builds directly on Phase B's `Extractor`/`Strategy` abstraction, whose exact shape is finalized during B's implementation. Several decisions below are flagged **NEEDS-DESIGN**: they require a human design pass before headless dispatch is safe.

**Goal:** When a source is confirmed broken (Phase A detection) and deterministic resilience can't extract it (Phase B exhausted), generate a candidate extractor, prove it against the corpus, and adopt it locally — **behind a flag, off by default**.

**Depends on:** Phase A (engine + detection) and Phase B (`Extractor`/`Strategy`, corpus with raw artifacts). Spec: `.planning/specs/2026-06-06-parser-auto-heal-design.md` §6.

## Architecture — the heal pipeline (stages 1–4; 5–6 are Phase D)

A new `job_finder/web/autoheal/heal_pipeline.py` orchestrates, fired from the post-ingestion detection seam **only** when `autoheal.heal_enabled` (config, default `false`) and a source is `DEGRADED` with K-failed-attempts not exhausted.

1. **ASSEMBLE** (no LLM): pull the ≥3 failing samples + the full source corpus + the current strategy/parser source code + the drift signal from `source_health`.
2. **GENERATE** (LLM, the only model call): one `call_model('quick')` (Ollama-first, $0 local) with a constrained prompt → a candidate **strategy module** (email/ATS) or **nav recipe** (careers). Output is code/recipe text, surface-specialized, one pipeline.
3. **VALIDATE** (no LLM — the gate): run the candidate in an isolated subprocess against the entire corpus. Must pass ALL: (a) every prior-working corpus sample still yields ≥1 valid `Job` with key fields present (count is advisory); (b) the failing samples now yield ≥1 `Job`; (c) `pytest tests/ -k <source>` (skipped with 3a/3b/3d still required if the test tree is absent); (d) AST safety scan — imports from an allowlist only, no network/filesystem/subprocess.
4. **ADOPT** (no LLM): on all-green, write the override artifact and hot-swap via `OverrideLoader`. On any failure: discard, increment the attempt counter, back off (1/source/24h; permanent `DEGRADED` after K=3).

## Components (new)

- `heal_pipeline.py` — stage orchestrator + backoff/attempt accounting (extends `source_health` with `heal_attempts`, `last_heal_at`).
- `codegen.py` — builds the generation prompt from ASSEMBLE inputs; defines the constrained output contract; calls `call_model('quick')`.
- `sandbox.py` — **NEEDS-DESIGN.** Runs candidate code against the corpus in an isolated subprocess with a timeout and an import allowlist. Windows-compatible isolation (no `fork`/`resource` limits) is the open question — likely `subprocess` running a worker script that imports the candidate from a temp file, with an AST pre-scan (3d) as the primary guard since OS-level sandboxing is weak on Windows.
- `override_loader.py` — **NEEDS-DESIGN.** `importlib`-loads an adopted override from `<userdata>/heal_overrides/<source>.py` and makes the relevant registry prefer it. Requires Phase B's email `Extractor`/ATS-registry/careers-recipe layers to expose an override-lookup indirection (a small change folded into B or done first in C). Atomic single-reference swap; in-flight reads see whole-old or whole-new.
- `m085_heal_state.py` — migration adding `heal_attempts`, `last_heal_at` to `source_health` (+ any heal-audit table).
- Config: `autoheal.heal_enabled` (default false), `autoheal.heal_provider` (workload tier), allowlist + timeout settings.

## Testing strategy (design-level)

- **Break-simulation harness:** mutate a known-good corpus sample (rename CSS classes / drop a JSON key / restructure DOM); assert detection confirms the break, the pipeline produces a candidate, and VALIDATE accepts a good candidate and rejects a regressing one.
- **Adversarial validation test:** feed a deliberately-bad generated module; assert stage 3 rejects + no adoption.
- **Safety tests:** generated code with a disallowed import / network call is rejected by 3d.
- All gated behind `heal_enabled=true` set in test config; default-off means production is untouched.

## NEEDS-DESIGN items (resolve with a human before issue creation)

1. **Sandbox isolation on Windows** — depth of isolation vs. relying on AST allowlist + subprocess timeout. Determines safety posture of auto-adoption.
2. **OverrideLoader indirection** — exactly where each registry consults overrides (email dispatch, ATS `SCANNERS_BY_NAME`, careers recipe cache); whether this indirection is added in Phase B or as C's first task.
3. **Codegen output contract** — strict schema for the generated strategy (function signature, allowed imports) so 3d is enforceable and generation is constrained.
4. **LLM-absent behavior** — confirmed: no provider → source stays `DEGRADED`, samples queued, no heal attempt (Phase A floor holds). Re-confirm the cost-gate path for any non-free fallback.

## Out of scope (Phase C)

- Shadow mode, live rollback, upstream contribution (Phase D).
- Auto-enabling heal by default (stays flag-off until Phase D rollout).

## Decomposition note

When B has landed, decompose C into roughly: (C1) override-loader indirection + `m085`; (C2) ASSEMBLE+GENERATE+codegen behind flag; (C3) sandbox+VALIDATE gate; (C4) ADOPT + break-simulation/adversarial tests. C1 is mergeable alone; C2–C4 are flag-off and independently mergeable on C1. Re-ground each against the real Phase-B code before writing issue bodies.
