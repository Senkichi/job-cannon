# Local Cascade Audit — Design Spec

**Date:** 2026-05-13
**Status:** Approved interactively via `/brainstorming`; awaiting written-spec review
**Goal:** Extend the existing multi-provider cascade (currently used only for the `scoring` tier) to all remaining LLM call sites in job-cannon, with a definitive but efficient evaluation that proves each cascade is adequate for its task before the rewire ships.

---

## 1. Context

The `scoring` tier already routes through a cascade `Ollama qwen2.5:14b → Groq → Cerebras → Gemini → Anthropic (paid fallback)`. Phase 33 (April 2026) hand-annotated an n=61 Opus gold baseline and used MAE + bias + bucket-deltas to select `qwen2.5:14b` for production. That selection process and its lessons are documented in the llibrary at `wiki/evals/provider-leaderboard.md`, `wiki/anti-patterns/pearson-r-only-eval.md`, and `wiki/incidents/cerebras-false-positive-adoption.md`.

Five additional LLM call sites still default to Anthropic (currently Haiku-class) without cascade routing:

| Callsite | File | Task class | Anthropic spend driver |
|---|---|---|---|
| `parse_structured_fields` | `job_finder/web/enrichment_tiers.py:469` | Structured JSON extraction (salary, location from JD) | Every enriched job |
| `_find_careers_url_with_haiku` | `job_finder/web/careers_scraper.py:174` | URL identification from HTML | Per-company crawler fallback |
| `_extract_jobs_with_haiku` | `job_finder/web/careers_scraper.py:309` | List extraction (jobs from HTML) | Crawler fallback when link-parsing fails |
| `reformat_description` | `job_finder/web/description_reformatter.py:128` | Text transformation (add section headers) | Every enriched description that isn't already formatted |
| `run_company_research_background` | `job_finder/web/company_research.py:153` | Generative summarization (research brief) | User-initiated |
| `discover_navigation_recipe` | `job_finder/web/ai_career_navigator.py:414` | Agentic browser-automation recipe | One-time discovery per company, then cached |

**Additional finding surfaced during design:** `config.example.yaml` lines 184–196 document a 5-link cascade including Groq and Cerebras, but only three provider adapters exist on disk (`anthropic_provider.py`, `gemini_provider.py`, `ollama_provider.py`). The advertised Groq + Cerebras hops are not actually wired. The cascade audit deliberately produces evidence that can motivate building those adapters as a follow-on.

## 2. Locked decisions (from interactive brainstorm)

| Decision | Choice | Rationale |
|---|---|---|
| **Cascade tail** | Anthropic stays as paid fallback for every callsite (mirrors `scoring`) | Cheapest defense against unknown-unknown failure modes; allows config-only rollback |
| **Eval methodology** | Shadow-replay against fresh Anthropic baseline | Production DB already holds every input we need; only the gold reference must be regenerated; reuses scoring's n≥50 / MAE-style rigor adapted per task class |
| **Cascade architecture** | Decided **after** the audit, by the data — not pre-committed to shared / per-callsite / tiered | Avoids assuming `qwen2.5:14b` excels at every task class; aligned with [[fewshot-variant-by-model]] |
| **Eval flow** | Round 0 dry-run (n≤3) → Round 1 cheap screen (n=10) → Round 2 full battery (n=50) | Round 0 catches harness bugs before they invalidate expensive runs; mirrors scoring's screen-then-promote rhythm; specific defense against the [[cerebras-false-positive-adoption]] failure mode where the framework, not the model, was wrong |
| **Heavy adapters** | Both `extract_jobs` and `ai_nav_discovery` in scope | Audit covers all 6 callsite-equivalents; live Playwright replay is required for `ai_nav_discovery` |
| **Subjective-task verdict** | DeepSeek-V3.2 LLM-as-judge via new SambaNova/OpenRouter provider adapter | Non-Anthropic family avoids judge-family bias; reasoning capacity at medium effort; new adapter pays for itself when post-audit config rewire wires Groq/Cerebras/SambaNova into production |
| **Corpus** | Live read from production DB at audit time; sampled `dedup_keys` persisted for reproducibility | Always-fresh inputs; zero corpus-maintenance overhead |

## 3. Architecture

New eval-only directory mirroring `evals/scoring_eval/`:

```
evals/cascade_audit/
├── README.md                    # how to run; what each artifact means
├── run_audit.py                 # CLI entrypoint: --round {0,1,2} --callsite X --provider Y
├── corpus.py                    # samples DB rows: jd_full, descriptions, homepages, recipes
├── adapters/                    # one per callsite
│   ├── parse_structured_fields.py
│   ├── find_careers_url.py
│   ├── extract_jobs.py
│   ├── description_reformat.py
│   ├── company_research.py
│   └── ai_nav_discovery.py
├── judge.py                     # DeepSeek-V3.2 pairwise-blind + position-swap
├── verdict.py                   # SUITABLE/MARGINAL/UNSUITABLE gate computation
├── report.py                    # per-round MD reports
└── artifacts/                   # round-N output JSON + MD reports (gitignored)
```

**Reuses existing primitives:** `model_provider.call_model`, `_sanitize_output`, `_validate_schema`. The cascade machinery already works; the audit measures what it routes through.

**Task adapter protocol:**

```python
class TaskAdapter(Protocol):
    def sample(self, n: int) -> list[dict]: ...                # pull n rows from corpus
    def exercise(self, row: dict, provider: str) -> dict: ...  # run the actual call
    def score(self, gold: dict, candidate: dict) -> dict: ...  # task-specific metrics
```

## 4. Per-callsite adapters

| Adapter | Sample source | Exercise | Score (vs Anthropic gold) |
|---|---|---|---|
| `parse_structured_fields` | `jobs` rows with `jd_full IS NOT NULL` and `LENGTH(jd_full) > 400` | Re-run exact prompt | Schema-valid; salary MAE; location exact-match; hallucination count |
| `find_careers_url` | `companies` with `homepage_url IS NOT NULL` | Live-fetch homepage HTML, re-run prompt | URL HTTP 200; same-eTLD+1; career-keyword presence; diff vs gold |
| `extract_jobs` | 50 companies live-fetched at **Round 1 start** (full n=50 corpus, not the n≤3 Round 0 subset); HTML cached in `artifacts/round_1/html/` and frozen across Rounds 1–2 | Re-run prompt against cached HTML | Schema valid; extracted-URL HTTP-200 rate; title-set Jaccard |
| `description_reformat` | `jobs.description` rows | Re-run reformatter prompt | Tripwires + DeepSeek judge (pairwise blind, position-swap) |
| `company_research` | `companies` rows | Re-run research prompt | Tripwires + DeepSeek judge (pairwise blind, position-swap) |
| `ai_nav_discovery` | 16 cached recipes | Re-run discovery prompt → replay recipe via Playwright | Recipe replay yields ≥1 job; step-count delta; replay duration ratio |

## 5. Verdict gates

### 5.1 Objective tasks — deterministic gates

| Universal gate | SUITABLE | MARGINAL | UNSUITABLE |
|---|---|---|---|
| Schema validity rate | ≥ 95% | ≥ 85% | < 85% |
| Hard-error rate (timeout, crash, exception) | ≤ 2% | ≤ 10% | > 10% |
| Throughput (rows/min) | ≥ 50% of Anthropic | ≥ 25% of Anthropic | < 25% of Anthropic |

| Adapter | Primary gate | Secondary gate |
|---|---|---|
| `parse_structured_fields` | Salary MAE ≤ $5k / ≤ $15k when both populated; hallucination rate ≤ 2% / ≤ 8% | Location exact-match ≥ 70% / ≥ 50% |
| `find_careers_url` | Same-eTLD+1 ≥ 95% / ≥ 80%; URL HTTP-200 ≥ 90% / ≥ 70% | URL contains career-keyword ≥ 85% / ≥ 65% |
| `extract_jobs` | Title-set Jaccard ≥ 0.7 / ≥ 0.5; URL HTTP-200 ≥ 85% / ≥ 65% | Hallucinated-job rate ≤ 5% / ≤ 15% |
| `ai_nav_discovery` | Recipe replay yields ≥1 job ≥ 80% / ≥ 60%; step count ≤ gold+2 / ≤ gold+5 | Replay duration ratio ≤ 1.5× / ≤ 2.5× gold |

**Composite verdict:** worst tier across all gates wins. Two SUITABLEs and one UNSUITABLE = UNSUITABLE overall. Fail-closed, matching scoring's gate spine.

### 5.2 Subjective tasks — DeepSeek-V3.2 judge + tripwires

**Tripwires (any single trigger = UNSUITABLE regardless of judge verdict):**
- Schema break, empty output, hard error
- `description_reformat`: output length < 30% of input
- `company_research`: output length < 300 chars

**Judge protocol (DeepSeek-V3.2 via new SambaNova or OpenRouter adapter):**
- **Pairwise blind**: judge sees input + two anonymized outputs labeled `A` and `B`
- **Position-swap correction** (per `wiki/evals/position-bias-correction.md`): each pair judged **twice** with positions swapped; both verdicts must agree for a non-tie; disagreement = tie
- **Output schema**: `{winner: "A"|"B"|"tie", hallucination_flag: bool, reasoning: str}`
- **Rubrics**:
  - `description_reformat`: *"Which output better preserves all factual content from the input while improving formatting? Flag any output that adds content not in the input."*
  - `company_research`: *"Which output is more factually useful for someone applying to this company? Flag any output containing URLs, names, or claims that appear fabricated."*

| Judge gate | SUITABLE | MARGINAL | UNSUITABLE |
|---|---|---|---|
| Candidate position-corrected win-or-tie rate vs gold | ≥ 45% (≈tied) | ≥ 30% | < 30% |
| Hallucination flag rate | ≤ 5% | ≤ 15% | > 15% |

**Why win-or-tie ≥ 45%, not win-rate ≥ 50%:** position-swap kills position bias but not judge competence bias. Setting the bar at "effectively tied" absorbs the residual noise floor without inverting the verdict.

**Post-audit manual sanity layer:** user spot-checks 10 random judge verdicts after Round 2. If >2 are obviously wrong, audit is paused and judge configuration is retuned. Cheapest defense against a broken judge framework — same lesson as the Cerebras incident applied one layer up.

## 6. 3-round eval flow

### Round 0 — Dry-run (n=1–3 per pair)

- Verify harness produces sane output end-to-end on real data before committing to expensive runs.
- For each (callsite, provider) pair: 1 sample. Confirm schema parsing, gate computation, report-line emission, no crashes on edge cases (empty JD, very long JD, unicode).
- Judge dry-run: 3 pairwise comparisons. Confirm valid JSON, position-swap flips its mind on ≥1 case, hallucination flag fires when fed a deliberately fabricated output.
- Total: ~30 task calls + 6 judge calls. Anthropic spend < $0.20. DeepSeek spend < $0.05. Wallclock: 30 min.
- **Exit conditions** (all must hold before Round 1 begins):
  1. Zero harness errors across all (callsite, provider) dry-run pairs.
  2. **Playwright is operational** for `ai_nav_discovery`: harness imports Playwright, launches a Chromium context, replays one cached recipe end-to-end. (Playwright is already a project dep via `careers_crawler/_playwright_tier.py`; this is a one-line import check.)
  3. **Token-budget projection**: judge per-call input + output tokens measured on the 6 dry-run pairs. Project Round 2 spend = `2400 calls × avg_tokens_per_call × pricing`. **If projected Round 2 judge spend > $15, recheck the gate before proceeding** — outputs may have grown beyond the 3k-token assumption underlying the cost estimate.
  4. User manually inspects Round 0 report; thresholds may be recalibrated.

### Round 1 — Cheap screen (n=10 per pair)

- **Universal gates only** (schema validity, hard-error rate, throughput). Task-specific gates logged advisory-only.
- Pairs with schema validity < 80% or hard-error > 25% → DROPPED with verdict `UNSUITABLE-ROUND-1`.
- **Pre-flight**: pause `enrichment_backfill` + `agentic_backfill` schedulers per `wiki/workflows/pause-schedulers-before-bulk-rescore.md`. **Schedulers stay paused through the entire audit (Round 1 start → Round 2 end), not just Round 1**, because `agentic_backfill` runs nightly at 3:30 AM and would compete with Ollama for GPU during Round 2's overnight batch. Audit harness emits a clear "RESUME SCHEDULERS" prompt at end of Round 2 so the user doesn't forget; if forgotten, daily APScheduler boot logs will surface the still-paused state on next Flask restart.
- Per-provider throttle delay computed from documented RPM/TPM per `wiki/workflows/rate-limit-delay-formula.md`.
- Total: ~300 task calls. Anthropic spend ~$1. Wallclock: 3–6 h.

### Round 2 — Full battery (n=50 objective / n=100 subjective per surviving pair)

- All gates (universal + task-specific + judge for subjective tasks).
- **Objective tasks: n=50 per surviving pair.** Wilson 95% CI ~±13pp on proportions; adequate for the ≥85% / ≥95% gate margins.
- **Subjective tasks: n=100 per surviving pair.** The judge gate at win-or-tie ≥ 45% sits inside the noise band at n=50; n=100 tightens the CI to ~±9pp so a true 45% candidate reads as 36–54% rather than 32–58%. Cost scales linearly (~+$5).
- Subjective tasks: ~100 task calls × 2 providers × 2 (position-swap) = 400 judge calls per task per provider.
- **Borderline-verdict rule**: if any gate measurement falls within 1 Wilson-CI half-width of a gate boundary, the verdict becomes MARGINAL regardless of point estimate. Borderline pairs may be re-run with n=200 if the user wants a stronger read; otherwise MARGINAL stands.
- Final verdict per pair: SUITABLE / MARGINAL / UNSUITABLE.
- Total: ~1500 task calls + ~2400 judge calls. Anthropic spend ~$4. DeepSeek spend ~$10. Wallclock: 12–24 h (overnight batch).
- **Post-run**: user spot-checks 10 random judge verdicts.

### Resumability + atomicity

Each round writes per-pair artifacts atomically (per `wiki/patterns/atomic-artifact-writes.md`) to `evals/cascade_audit/artifacts/round_N/{callsite}_{provider}.json`. Re-running skips completed pairs. Sampled `dedup_keys` persisted so Round 1 → Round 2 uses the same corpus.

### Environment provenance

Each artifact includes a provenance block (per `wiki/patterns/environment-provenance-block.md`): provider config snapshot, model versions, harness commit SHA, sample seed, scheduler-pause status.

## 7. Decision artifact + rollout

### `CASCADE-AUDIT.md` (committed to `.planning/`)

```
# Cascade Audit — Results

## Grid (callsite × provider)
              | Ollama | Groq* | Cerebras* | Gemini | Anthropic (gold)
parse_fields  |   ?    |   ?   |     ?     |   ?    |       ✓
find_url      |   ?    |   ?   |     ?     |   ?    |       ✓
extract_jobs  |   ?    |   ?   |     ?     |   ?    |       ✓
desc_reformat |   ?    |   ?   |     ?     |   ?    |       ✓
company_res   |   ?    |   ?   |     ?     |   ?    |       ✓
ai_nav        |   ?    |   ?   |     ?     |   ?    |       ✓
* Groq/Cerebras audited only if adapters exist by Round 1 start.
✓ = SUITABLE, ⚠ = MARGINAL, ✗ = UNSUITABLE

## Recommended cascade per callsite
parse_fields:   Ollama → ... → Anthropic    (per data)
find_url:       per data
extract_jobs:   per data
desc_reformat:  per data
company_res:    per data
ai_nav:         per data

## Risk callouts
- DeepSeek family-bias bounded by win-or-tie ≥ 45% bar
- (any deltas surfaced by audit)
```

### Config rewire (separate PR, post-audit)

The rewire shape depends on what the audit grid shows.

**Case A — audit recommends one shared cascade across all six callsites** (i.e., the same ordered provider list is SUITABLE for every callsite):

- Single `config.yaml` edit (using **`Edit` tool only** per `CLAUDE.md` — never `Write`).
- Adds `providers.haiku.fallback_chain` (and `providers.sonnet.fallback_chain` if a heavier-tier callsite warrants it) based on audit recommendation.
- No code changes — callsites already route through `call_model(tier='haiku')`; peer-tier provider inheritance at `model_provider.py:162-175` handles routing.

**Case B — audit recommends per-callsite divergence** (e.g., `parse_fields` wants Ollama-first but `extract_jobs` wants Gemini-first):

- Existing `resolve_provider_config(tier, config)` cannot encode per-callsite routing — it routes by `tier` only. The rewire becomes:
  1. **Code change in `model_provider.py`**: extend `resolve_provider_config(tier, config, purpose=None)` to look up `providers.<tier>.purpose_overrides.<purpose>` before falling back to the tier-level chain. ~30 LOC.
  2. **Threading**: `call_model()` already receives `purpose`; just pass it through to `resolve_provider_config()`. ~5 LOC.
  3. **Config schema**: each callsite's `purpose` string (`parse_structured_fields`, `careers_scrape`, `description_reformat`, `company_research`, `ai_nav_discovery`) becomes a valid override key.
- Tests: add unit tests covering override-hits, override-misses (falls through to tier chain), and the existing tier-only path.
- Rollback: remove override section from `config.yaml`; tier-chain is unchanged.

**Either case:** Anthropic stays as cascade tail. Per-provider `daily_limits` carry over from scoring config. The decision between Case A and Case B is made when `CASCADE-AUDIT.md` lands, not before.

### Production telemetry prerequisites (pre-rewire, code change)

The canary tripwire below depends on per-callsite telemetry. `scoring_costs` already has a `purpose` column (every callsite passes a distinct purpose string today), so **per-callsite provider distribution is already queryable** via `GROUP BY purpose, provider`. What is missing:

- **`scoring_costs.schema_valid` column** (boolean): currently not logged. Add via Migration 49 (`m049_scoring_costs_schema_valid.py`) and instrument `_maybe_record_cost()` to populate it from the `ModelResult.schema_valid` field that already exists on the dataclass. ~20 LOC + migration.
- **Backfill**: existing rows get `schema_valid = NULL`; canary tripwire counts only post-migration rows.

This is **pre-audit-completion** work — it must land before Round 1 begins so the canary has a baseline reading of pre-rewire behavior to compare against.

### Production canary (1 week post-rewire)

Monitor via the existing + extended telemetry:

- Per-callsite provider distribution: `SELECT purpose, provider, COUNT(*) FROM scoring_costs WHERE timestamp > date('now', '-1 day') GROUP BY purpose, provider`.
- Per-callsite schema-failure rate: `WHERE schema_valid = 0` over the same query.
- Anthropic-tail invocation rate per callsite: `WHERE provider = 'anthropic'` over the same query.

**Tripwire:** if Anthropic-tail rate exceeds 10% of callsite volume (suggesting upstream is failing), revert cascade order. Rollback is config-only.

## 8. Out of scope

- **Tier-name rename** (`haiku`/`sonnet`/`opus` → `low`/`mid`/`high`): independent refactor.
- **Per-provider bias correction in production scoring**: noted in `wiki/anti-patterns/pearson-r-only-eval.md` as architectural follow-on; not in scope here.
- **Wiring Groq/Cerebras as production adapters**: the new SambaNova/OpenRouter adapter for the DeepSeek judge is in scope; full Groq/Cerebras production adapters are separate work that the audit may motivate.
- **Re-evaluating the `scoring` tier**: already audited (Phase 33).
- **Removing the Anthropic SDK dependency**: explicitly kept as cascade tail per Section 2.

## 9. Risks and adversarial-review caveats

| Risk | Mitigation | Residual |
|---|---|---|
| Round 2 judge cost / runtime estimate is wrong (verbose outputs blow up tokens) | Round 0 dry-run surfaces real token counts before Round 2 commits | Audit may take longer than 24h; not a correctness risk |
| qwen2.5:14b excels at scoring but is weak at JSON extraction / text transformation | The audit is the answer — surfacing this is the point | Cascade may need per-callsite ordering; acceptable |
| DeepSeek judge has its own invisible bias (different from Anthropic family bias, but still present) | Position-swap kills the largest invisible component; manual spot-check on 10 verdicts catches the rest | Cannot fully eliminate judge bias; bounded by win-or-tie ≥ 45% bar |
| New SambaNova/OpenRouter adapter has bugs the cascade depends on | Round 0 dry-run exercises the adapter end-to-end before Round 1 starts | If adapter is shipped before audit fully runs, real bugs may surface in production canary |
| Live careers-page replay (`extract_jobs`, `ai_nav_discovery`) is brittle across audit re-runs | Cache HTML for full n=50 corpus at **Round 1 start** (after dedup_keys are sampled); freeze across Round 1 → Round 2; refetching mid-audit not permitted | Eval reflects a frozen snapshot of the web; production sees live web (acceptable) |
| Production DB churns between Round 1 and Round 2 | Sampled `dedup_keys` persisted to `artifacts/` | None |
| Scheduler races eval and corrupts ground truth | Pause schedulers pre-flight; resume on audit end | Manual recovery if user forgets to resume |

## 10. Cost summary

| Item | Cost |
|---|---|
| Anthropic gold baseline (Round 0 + 1 + 2) | ~$5 |
| DeepSeek-V3.2 judge (Round 0 + 2 at n=100 subjective) | ~$10 |
| New SambaNova/OpenRouter adapter (LOC) | ~150 LOC, ~1 plan |
| Migration 49 + `_maybe_record_cost` instrumentation for `schema_valid` column | ~20 LOC + migration |
| Conditional per-callsite routing extension to `model_provider.py` (only if audit recommends Case B) | ~35 LOC + tests, conditional on audit outcome |
| Total Anthropic + judge spend | ~$15 |
| Estimated wallclock | Round 0: 30 min; Round 1: 3–6 h; Round 2: 12–24 h overnight; total: ~2 days |

## 11. Success criteria

The audit is **complete** when:
1. All 6 callsites × ≥3 candidate providers have a SUITABLE/MARGINAL/UNSUITABLE verdict at Round 2.
2. `CASCADE-AUDIT.md` is committed with the grid + recommended per-callsite cascade.
3. User has manually spot-checked 10 judge verdicts and approved (or audit re-tuned).

The cascade rewire is **complete** when:
4. Config edit lands; production canary runs 1 week; Anthropic-tail rate stays under 10% per callsite.

The work overall is **successful** when:
5. Marginal Anthropic spend on the 6 audited callsites drops by ≥80% relative to pre-audit baseline (telemetry: `scoring_costs.provider='anthropic'` rows per callsite per day).
