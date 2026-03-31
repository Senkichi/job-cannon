# Job Cannon v2.0 Implementation Plan — Cascading Free Provider Routing

> Paste this file's contents into a new Claude Code session to execute the implementation.

---

## Objective

Replace paid Anthropic Sonnet deep evaluation ($0.011/job) with a cascading chain of free API providers. Target: $0.00/job for daily volumes under 400, with automatic fallback to Anthropic when free quotas are exhausted.

## What's Already Done (This Session — 2026-03-29)

### Infrastructure Built
- **Groq and Cerebras provider adapters** created and registered:
  - `job_finder/web/providers/groq_provider.py` — OpenAI-compatible, GROQ_API_KEY
  - `job_finder/web/providers/cerebras_provider.py` — OpenAI-compatible, CEREBRAS_API_KEY
  - Both registered in `model_provider.py` (`_make_adapter()`, `_FREE_PROVIDERS`)
  - Both added to `eval_provider.py` CLI choices
  - All 1786 existing tests pass

### 6 New Prompt Variants Added to `eval_provider.py`
Added to `PROMPT_VARIANTS` dict and `--prompt-variant` CLI choices:
- `fewshot-anchored` — anti-inflation instructions
- `fewshot-cot` — chain-of-thought reasoning before scoring
- `fewshot-distribution` — score distribution awareness
- `fewshot-comparative` — anchor against explicit ideal role
- `fewshot-rubric-strict` — hard scoring gates (cap rules)
- `fewshot-negative` — counter-examples of common mistakes

### Comprehensive Eval Results (72+ eval runs today)

**Confirmed Results (n>=30, trustworthy):**

| Provider | Model | Variant | r | Schema | n | Latency | Capacity |
|---|---|---|---|---|---|---|---|
| Cerebras | qwen-3-235b | fewshot | **0.839** | **100%** | 61/61 | 6.2s | 363/day |
| Ollama | qwen2.5:14b | fewshot-comparative | **0.856** | 93% | 28/30 | 21.2s | unlimited |
| Cerebras | qwen-3-235b | fewshot-distribution | 0.808 | 100% | 30/30 | 1.3s | 363/day |
| Cerebras | qwen-3-235b | fewshot-anchored | 0.766 | 97% | 29/30 | 4.4s | 363/day |

**SambaNova Historical (n=18-19, small sample, very high r but 20 RPD limit):**

| Model | r | Schema | Latency |
|---|---|---|---|
| Meta-Llama-3.3-70B | 0.935 | 100% | 1.9s |
| DeepSeek-V3.2 | 0.934 | 95% | 5.0s |
| Qwen3-235B | 0.905 | 100% | 4.5s |

**Key Experimental Findings:**
1. Plain `fewshot` is the most robust variant at scale — new variants overfitted at n=10 screening
2. n=10 screening inflates correlation by +0.05-0.13 vs n=30 confirmation
3. Different models respond differently to prompting techniques (fewshot-comparative best for Ollama, fewshot best for Cerebras)
4. Groq llama-3.3-70b is unusable on free tier (12K TPM too tight, constant 429s)
5. Groq llama-4-scout works well (r=0.833, 100% schema, 1.2s, 181/day capacity)
6. OpenRouter: high latency (70-100s), poor schema adherence (70-80%), not competitive
7. Gemini: moderate (r=0.80), 20 RPD quota exhausted quickly, not competitive
8. Cerebras gpt-oss-120b: rotated out of Cerebras catalog (404 errors)

### Decided Cascade Order
```
Cerebras qwen-3-235b (primary, fewshot, 363/day, r=0.839)
  -> Groq llama-4-scout (fewshot-distribution, 181/day, r=0.833)
  -> Ollama qwen2.5:14b (fewshot, unlimited, r=0.856)
  -> Anthropic Sonnet (paid, last resort)
```

---

## Architecture Context

### Current Scoring Flow
1. `pipeline_runner.run_ingestion()` fetches jobs from Gmail/SerpAPI
2. `scoring_runner.run_haiku_scoring()` fast-filters all new jobs via Haiku
3. Jobs above `haiku_threshold` (42) enter `sonnet_queue`
4. `scoring_runner.run_sonnet_evaluation()` deep-evaluates via Sonnet
5. Both scorers call `call_model()` in `model_provider.py` — the central dispatcher

### `call_model()` Current Behavior (model_provider.py:219-306)
1. `resolve_provider_config(tier, config)` → returns `{provider, model, fallback}`
2. Budget gate for non-free providers (skip for `_FREE_PROVIDERS`)
3. Instantiate adapter via `_make_adapter()`
4. Call adapter → schema validate → retry on schema fail → single fallback to Anthropic
5. Record cost via `_maybe_record_cost()`

### Key Integration Points
- `call_model()` is the ONLY place that needs cascade logic — both haiku_scorer and sonnet_evaluator route through it
- `resolve_provider_config()` needs to parse `fallback_chain` from config
- `_FREE_PROVIDERS` already includes `groq` and `cerebras`
- `record_cost()` already accepts `provider=` parameter
- Cost tracking table (`scoring_costs`) already has `provider` column

### Config Structure (config.yaml)
Currently: `providers.sonnet.provider`, `providers.sonnet.model`, `providers.sonnet.fallback` (single string)
Target: add `providers.sonnet.fallback_chain` (list) and `providers.daily_limits` (dict)

---

## Implementation Steps

### Step 1: Cascade Config Schema + Parsing (~30 min, ~50 lines)

**File: `job_finder/web/model_provider.py`**

Modify `resolve_provider_config()` (line 58-81) to also return:
- `fallback_chain`: parsed from `tier_cfg.get("fallback_chain", [])` — list of `{provider, model}` dicts
- `daily_limits`: parsed from `providers_cfg.get("daily_limits", {})` — flat `{provider_name: max_per_day}` dict

Current return: `{"provider": str, "model": str, "fallback": str | None}`
New return: `{"provider": str, "model": str, "fallback": str | None, "fallback_chain": list[dict], "daily_limits": dict[str, int]}`

Backward compatible: when `fallback_chain` is empty, existing single-fallback path is used.

**File: `config.example.yaml`**

Add documented example in providers section:
```yaml
providers:
  sonnet:
    provider: cerebras
    model: qwen-3-235b-a22b-instruct-2507
    fallback_chain:
      - provider: groq
        model: meta-llama/llama-4-scout-17b-16e-instruct
      - provider: ollama
        model: qwen2.5:14b
      - provider: anthropic
        model: claude-sonnet-4-6
  daily_limits:
    cerebras: 350
    groq: 170
```

### Step 2: Daily Rate Limit Tracker (~40 min, ~60 lines)

**File: `job_finder/web/model_provider.py`**

Add module-level state and helper functions:

```python
_daily_usage: dict[str, int] = {}
_usage_date: str = ""

def _check_daily_limit(provider: str, daily_limits: dict[str, int]) -> bool:
    """Return True if provider is under its daily limit (or has no limit)."""

def _increment_usage(provider: str) -> None:
    """Increment the daily usage counter for a provider."""

def _init_usage_from_db(conn: sqlite3.Connection, daily_limits: dict[str, int]) -> None:
    """On date rollover, reconstruct today's usage from scoring_costs table."""
```

Logic:
- `_check_daily_limit` returns True if provider not in daily_limits (no limit) or count < limit
- `_init_usage_from_db` queries: `SELECT provider, COUNT(*) FROM scoring_costs WHERE date(created_at) = date('now') GROUP BY provider`
- Date rollover: if `_usage_date != today`, reinitialize from DB

### Step 3: Cascade Logic in `call_model()` (~40 min, ~80 lines)

**File: `job_finder/web/model_provider.py`**

Refactor `call_model()` (line 219-306):

1. After `resolve_provider_config()`, build the full chain:
   ```python
   chain = [(provider_name, model)]
   for entry in resolved.get("fallback_chain", []):
       chain.append((entry["provider"], entry["model"]))
   ```

2. Iterate through chain:
   ```python
   for provider_name, model in chain:
       # Skip if daily limit exhausted
       if not _check_daily_limit(provider_name, daily_limits):
           logger.info("Cascade: %s exhausted, skipping", provider_name)
           continue

       # Skip if API key not set (catch ValueError from adapter __init__)
       try:
           adapter = _make_adapter(provider_name, client, conn, config, job_id, purpose)
       except ValueError as e:
           logger.warning("Cascade: %s unavailable: %s", provider_name, e)
           continue

       # Budget gate for non-free providers
       if provider_name not in _FREE_PROVIDERS:
           if not cost_gate(conn, config, tier):
               continue

       try:
           result = adapter.call(model, system, messages, output_schema, max_tokens, timeout)
           # Schema validation + retry (existing logic)
           errors = _validate_schema(result.data, output_schema)
           if errors:
               augmented = _augment_with_errors(messages, errors)
               result = adapter.call(model, system, augmented, output_schema, max_tokens, timeout)
               errors = _validate_schema(result.data, output_schema)
           if not errors:
               _increment_usage(provider_name)
               _maybe_record_cost(result, conn, job_id, purpose)
               return result
       except requests.HTTPError as e:
           if e.response and e.response.status_code == 429:
               logger.warning("Cascade: %s rate limited (429), skipping", provider_name)
               _daily_usage[provider_name] = daily_limits.get(provider_name, 999999)  # mark exhausted
               continue
           logger.warning("Cascade: %s HTTP error: %s", provider_name, e)
           continue
       except Exception as e:
           logger.warning("Cascade: %s error: %s", provider_name, e)
           continue
   ```

3. If `fallback_chain` is empty, preserve existing single-fallback path (backward compat)
4. If all providers exhausted: `raise RuntimeError("All providers exhausted")`

**CRITICAL**: Import `requests` at top of file (for `requests.HTTPError` catch).

### Step 4: Per-Model Prompt Variant Support (~20 min, ~30 lines)

Different models perform best with different prompt variants. The cascade config should allow per-provider prompt variant overrides.

**File: `job_finder/web/model_provider.py`**

Extend `fallback_chain` entries to optionally include `prompt_variant`:
```yaml
fallback_chain:
  - provider: groq
    model: meta-llama/llama-4-scout-17b-16e-instruct
    prompt_variant: fewshot-distribution   # optional override
  - provider: ollama
    model: qwen2.5:14b
    # no prompt_variant = use default (fewshot)
```

Thread the variant through to `call_model()` callers. This may require `sonnet_evaluator.py` to read the variant from config and pass it when building the system prompt.

**File: `job_finder/web/sonnet_evaluator.py`**

Move `_FEWSHOT_EXAMPLES` from `eval_provider.py` into `sonnet_evaluator.py`. Update `_SYSTEM_PROMPT` to include fewshot examples (append `_FEWSHOT_EXAMPLES` to the base prompt). This makes fewshot the production default.

Also add `_FEWSHOT_DISTRIBUTION_INSTRUCTIONS` (the score distribution awareness text) for use by providers that need it.

### Step 5: DB Migration + Provider Attribution (~20 min, ~25 lines)

**File: `job_finder/web/db_migrate.py`**

Add Migration 20:
```python
"ALTER TABLE jobs ADD COLUMN scoring_provider TEXT DEFAULT 'anthropic'",
```

**File: `job_finder/db.py`**

Modify `persist_sonnet_score()` (line 258):
- Add `provider: str | None = None` parameter
- Update SQL: `UPDATE jobs SET sonnet_score = ?, fit_analysis = ?, scoring_provider = COALESCE(?, scoring_provider) WHERE dedup_key = ?`
- Update params tuple to include provider

**File: `job_finder/web/scoring_orchestrator.py`**

In `score_and_persist_sonnet()`: extract `result.provider` from ModelResult and pass to `persist_sonnet_score()`.

Note: `call_model()` returns `ModelResult` which has `.provider` field. The `evaluate_job_sonnet()` function in `sonnet_evaluator.py` needs to thread this through. Currently it returns a `ScoringResult` dataclass — add the provider to the data dict.

### Step 6: Tests (~45 min, ~150 lines)

**File: `tests/test_model_provider.py`**

Add tests:
1. `test_resolve_with_fallback_chain` — chain parsed from config
2. `test_resolve_backward_compat` — no chain = empty list, old behavior
3. `test_cascade_skips_exhausted_provider` — mock primary at limit, verify second called
4. `test_cascade_429_marks_exhausted` — mock 429 response, verify skip
5. `test_cascade_all_exhausted_raises` — RuntimeError when nothing available
6. `test_cascade_empty_chain_uses_old_fallback` — backward compat with single fallback
7. `test_daily_limit_check_and_increment` — counter logic
8. `test_daily_limit_resets_on_new_day` — date rollover

**File: `tests/test_db.py`**
- `test_persist_sonnet_score_with_provider` — new column written

### Step 7: Wire Config + Integration Test (~25 min)

**File: `config.yaml` (Edit tool ONLY, never Write)**

Set the sonnet tier to use cascade:
```yaml
providers:
  sonnet:
    provider: cerebras
    model: qwen-3-235b-a22b-instruct-2507
    fallback_chain:
      - provider: groq
        model: meta-llama/llama-4-scout-17b-16e-instruct
      - provider: ollama
        model: qwen2.5:14b
      - provider: anthropic
        model: claude-sonnet-4-6
  daily_limits:
    cerebras: 350
    groq: 170
```

### Step 8: Full Test Suite + Smoke Test (~15 min)

1. `uv run pytest tests/` — all existing + new tests pass
2. Manual: set `daily_limits.cerebras: 2`, trigger scoring, verify cascade falls to Groq
3. Query: `SELECT scoring_provider, COUNT(*) FROM jobs WHERE scoring_provider IS NOT NULL GROUP BY 1`

---

## Key Files Reference

| File | Purpose |
|---|---|
| `job_finder/web/model_provider.py` | Central dispatcher — cascade logic lives here |
| `job_finder/web/sonnet_evaluator.py` | Deep evaluator — fewshot prompt wiring |
| `job_finder/web/scoring_orchestrator.py` | Orchestrates scoring + persistence |
| `job_finder/web/scoring_runner.py` | Pipeline entry — calls orchestrator |
| `job_finder/web/db_migrate.py` | Schema migrations |
| `job_finder/db.py` | `persist_sonnet_score()` |
| `job_finder/web/providers/cerebras_provider.py` | Cerebras adapter (already built) |
| `job_finder/web/providers/groq_provider.py` | Groq adapter (already built) |
| `eval_provider.py` | Prompt variants defined here (move fewshot to sonnet_evaluator) |
| `config.example.yaml` | Config documentation |
| `tests/test_model_provider.py` | Primary test file for cascade |

## Scope Explicitly Cut

- **Score recalibration** — deferred. Provider attribution stored for future retroactive calibration.
- **UI provider badge** — deferred. Column populated but not displayed yet.
- **SambaNova in cascade** — deferred until billing upgrade (stuck at 20 RPD).
- **Haiku tier cascade** — stays Anthropic. Haiku is cheap ($0.001/job).
- **Async/parallel provider calls** — unnecessary for single-user localhost app.

## Rate Limit Reference

| Model | Provider | Delay | Max/day |
|---|---|---|---|
| qwen-3-235b | Cerebras | 3s | 363 |
| llama-4-scout-17b-16e | Groq | 3s | 181 |
| qwen2.5:14b | Ollama | 0s | unlimited |
| claude-sonnet-4-6 | Anthropic | 0s | budget-gated |

## Important Constraints

- `config.yaml` must ONLY be modified with Edit tool, NEVER Write tool
- Run tests with `uv run pytest tests/` (not bare `pytest`)
- All providers already registered in `_FREE_PROVIDERS` and `_make_adapter()`
- Schema validation uses `SONNET_SCHEMA` from `sonnet_evaluator.py`
- `scoring_costs` table already has `provider` column — no migration needed for cost tracking
