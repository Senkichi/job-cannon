# Multi-Provider Model Routing & Evaluation Framework

## Problem

All AI calls in Job Cannon route through Anthropic's API. Sonnet calls account for 18+ of 24 call sites and are the primary cost driver, budget-gated at $25/month and $10/day. Alternative providers (Gemini free tier, Ollama local models) could reduce or eliminate this cost, but switching blindly risks degrading scoring quality, resume output, and structured output reliability.

## Goal

Make all Sonnet-tier calls configurable to route through Anthropic, Gemini, or Ollama via config.yaml, without changing existing workflows. Build an evaluation framework that benchmarks alternative models against existing Anthropic Sonnet results so provider switches are data-driven.

## Non-Goals

- Actively migrating Haiku or Opus call sites to alternative providers (the config supports it, but this work focuses on Sonnet-tier calls)
- Per-purpose provider routing (all Sonnet calls route to the same provider)
- Shadow mode / always-on evaluation
- Settings page UI for provider management
- Deleting or modifying existing `call_claude()` behavior

## Architecture: Adapter Pattern

### Overview

A new `call_model()` dispatcher reads provider configuration and delegates to provider-specific adapters. Each adapter translates the common interface into provider-specific API calls. `call_claude()` stays untouched as the Anthropic adapter's backend.

```
caller ──► call_model(model="sonnet", ...) ──► Dispatcher ──► Provider Adapter ──► API
```

### New Module: `job_finder/web/model_provider.py`

The dispatcher and base adapter interface.

**`ModelResult` dataclass:**
- `data: dict` — parsed structured output
- `cost_usd: float` — 0.0 for free providers
- `input_tokens: int`
- `output_tokens: int`
- `model: str` — actual model used
- `provider: str` — "anthropic" | "gemini" | "ollama"
- `schema_valid: bool` — did output match expected schema

**`BaseProvider` ABC:**
- `call(model, system, messages, output_schema, max_tokens, timeout) -> ModelResult`
- `validate_schema(result, schema) -> bool`

**`call_model()` function:**
- Drop-in replacement for `call_claude()` — same signature, same `tuple[dict, float]` return type
- Resolves logical model tier ("sonnet") to provider + model ID via config
- Runs budget gate (existing logic; free providers bypass `cost_gate()` entirely)
- Dispatches to the appropriate adapter, which returns a `ModelResult`
- `call_model()` unpacks the `ModelResult` internally: logs `provider`, `schema_valid`, and token counts to the DB, then returns `(result.data, result.cost_usd)` to the caller
- Validates output against schema, retries once on failure
- Falls back to Anthropic if configured and retry fails
- Records cost via existing `record_cost()` with new `provider` column

**Return type layering:** Adapters return `ModelResult` (rich metadata). `call_model()` consumes the metadata for logging/validation and returns `tuple[dict, float]` to callers — preserving the existing `call_claude()` contract. Callers never see `ModelResult` directly.

### Provider Adapters (`job_finder/web/providers/`)

**Anthropic (`anthropic_provider.py`):**
- Wraps existing `call_claude()` API call logic
- Structured output via tool-choice mechanism (existing behavior)
- Cost computed from existing `MODEL_PRICING` table
- Auth: `ANTHROPIC_API_KEY` env var (existing)
- This is a refactor of existing code, not new behavior

**Gemini (`gemini_provider.py`):**
- SDK: `google-genai`
- Structured output via `response_mime_type: "application/json"` + `response_schema`
- Schema translation: JSON Schema to Gemini's response_schema format (mostly compatible subset)
- Cost: $0.00 on free tier (tokens still logged)
- Auth: `GEMINI_API_KEY` env var
- Rate limit handling: sleep and retry on 429 (15 RPM on free tier)

**Ollama (`ollama_provider.py`):**
- SDK: `requests` (already installed) against Ollama's native `/api/chat` endpoint
- Structured output: `"format": "json"` in request body + schema embedded in system prompt as instructions
- Cost: $0.00 always (local)
- Auth: none (local server)
- Connection: health-check `/api/tags` on init, fail fast if Ollama not running
- Higher retry rate expected due to less reliable schema adherence

### Schema Validation

Shared across all adapters using the `jsonschema` library.

**Flow:**
1. Provider returns raw result
2. Validate against expected JSON Schema
3. If valid: return `ModelResult(schema_valid=True)`
4. If invalid: retry once with augmented prompt including schema errors
5. If still invalid and fallback configured: re-dispatch via Anthropic adapter
6. If still invalid and no fallback: return `ModelResult(schema_valid=False)` with best-effort data

## Configuration Schema

New `providers` section in `config.yaml`, alongside existing `scoring` section.

```yaml
providers:
  # Per-tier provider routing
  sonnet:
    provider: gemini          # "anthropic" | "gemini" | "ollama"
    model: gemini-2.0-flash   # provider-specific model ID
    fallback: anthropic       # optional: fall back on failure
  haiku:
    provider: anthropic       # keep Haiku on Anthropic (already cheap)
    # model omitted → uses scoring.models.haiku
  opus:
    provider: anthropic
    # model omitted → uses scoring.models.opus

  # Provider connection settings
  gemini:
    api_key_env: GEMINI_API_KEY
  ollama:
    base_url: http://localhost:11434
```

**Resolution rules:**
1. `providers.<tier>` exists → use its `provider` and `model`
2. `providers.<tier>` missing → default to `provider: "anthropic"`, model from `scoring.models.<tier>`
3. `providers.<tier>.model` missing → use `scoring.models.<tier>` as model name
4. On failure with `fallback` configured → re-dispatch with fallback provider + `scoring.models.<tier>`

**Backwards compatibility:** No `providers` section means everything routes to Anthropic exactly as today. Existing `scoring.models` section still used as fallback model names.

**Budget gating:** Unchanged for Anthropic calls. The dispatcher bypasses `cost_gate()` entirely when the resolved provider is free (Gemini free tier, Ollama), regardless of cumulative Anthropic spend. This means switching to a free provider unblocks scoring even if the Anthropic budget is exhausted.

## Cost Tracking Adaptation

### Database Change

Add `provider` column to `scoring_costs` table:

```sql
ALTER TABLE scoring_costs ADD COLUMN provider TEXT DEFAULT 'anthropic';
```

All existing rows get `'anthropic'` default. New rows include the provider that handled the call. Tokens are always logged even for $0 providers (needed for evaluation comparison).

### Stats Functions

`get_cost_stats()` adds `by_provider` grouping alongside existing `by_feature`. Costs page shows provider breakdown automatically.

## Evaluation Framework

### Purpose

On-demand benchmarking that compares alternative model outputs against existing Anthropic Sonnet results. Produces a data-driven verdict before committing to a provider switch.

### CLI Interface

```bash
# Single candidate, single purpose
uv run python -m job_finder.eval.benchmark \
  --candidate gemini:gemini-2.0-flash \
  --purpose sonnet_eval \
  --sample 30

# Multiple candidates
uv run python -m job_finder.eval.benchmark \
  --candidate gemini:gemini-2.0-flash \
  --candidate ollama:qwen2.5:32b \
  --purpose sonnet_eval \
  --sample 20

# All Sonnet purposes
uv run python -m job_finder.eval.benchmark \
  --candidate gemini:gemini-2.0-flash \
  --purpose all \
  --sample 10
```

### Ground Truth Strategy

Uses existing Anthropic Sonnet results stored in the database as ground truth. The benchmark:
1. Samples N jobs that already have Sonnet results
2. Reconstructs the same prompt (system + messages) that was originally sent
3. Sends that prompt to the candidate model
4. Compares candidate output against the stored Sonnet result

No re-running Sonnet. Zero Anthropic cost for benchmarking.

**Prompt reconstruction:** The benchmark imports existing prompt-building functions from each module (e.g., `sonnet_evaluator.build_sonnet_prompt()`, `haiku_scorer.build_haiku_prompt()`) rather than trying to replay stored prompts. Where these functions don't exist as separable units today, extracting them is prerequisite refactoring work during implementation. Each purpose has its own prompt shape, so `sample.py` maintains a registry mapping purpose labels to their prompt-building functions.

### Metrics

**Score correlation:**
- `mean_delta` / `median_delta` — average score difference
- `std_delta` — score variance
- `correlation` — Pearson r
- `rank_agreement` — Spearman rho (do they rank jobs the same?)
- `threshold_agreement` — % same pass/fail at haiku_threshold

**Schema adherence:**
- `adherence_rate` — % of outputs matching expected schema
- `retry_rate` — % that needed a retry
- `fallback_rate` — % that fell back to Anthropic

**Qualitative output:**
- `avg_summary_length` — reference vs candidate
- `fit_analysis_rate` — did it produce strengths/gaps?
- `avg_strengths` / `avg_gaps` — depth of analysis

**Performance:**
- `avg_latency_ms` — wall-clock time
- `total_cost` — reference vs candidate

### Verdict

Auto-computed recommendation based on configurable thresholds:

| Verdict | Correlation | Schema Adherence | Rank Agreement | Fallback Rate |
|---------|-------------|------------------|----------------|---------------|
| SUITABLE | >= 0.85 | >= 90% | >= 80% | <= 10% |
| MARGINAL | 0.70-0.85 | 75-90% | 65-80% | 10-25% |
| NOT_RECOMMENDED | < 0.70 | < 75% | < 65% | > 25% |

Report saved to `eval_results/<date>_<model>_<purpose>.json` with aggregate metrics, verdict, and per-job details.

## Caller Migration

Mechanical one-line change per call site:

```python
# Before
from job_finder.web.claude_client import call_claude
result, cost = call_claude(model=model, ..., purpose="sonnet_eval")

# After
from job_finder.web.model_provider import call_model
result, cost = call_model(model="sonnet", ..., purpose="sonnet_eval")
```

The `model` parameter changes from a provider-specific model ID (e.g., `"claude-sonnet-4-6"`) to a logical tier name (`"sonnet"`). Everything else stays the same.

## New Dependencies

| Package | Purpose | Notes |
|---------|---------|-------|
| `google-genai` | Gemini SDK | New dependency |
| `jsonschema` | Schema validation for retry logic | New dependency |

| `scipy` | Pearson/Spearman correlation in eval framework | New dependency |

Already available: `anthropic`, `requests` (Ollama).

## File Layout

```
job_finder/
├── web/
│   ├── model_provider.py           NEW  — dispatcher, BaseProvider, call_model()
│   ├── providers/                  NEW
│   │   ├── __init__.py             NEW
│   │   ├── anthropic_provider.py   NEW  — wraps call_claude() internals
│   │   ├── gemini_provider.py      NEW  — google-genai SDK
│   │   └── ollama_provider.py      NEW  — REST API via requests
│   ├── claude_client.py            MOD  — add provider column to record_cost()
│   ├── db_migrate.py               MOD  — migration for provider column
│   ├── haiku_scorer.py             MOD  — call_claude → call_model
│   ├── sonnet_evaluator.py         MOD  — call_claude → call_model
│   ├── enrichment_tiers.py         MOD  — call_claude → call_model
│   ├── resume_generator.py         MOD  — call_claude → call_model
│   ├── resume_multi_version.py     MOD  — call_claude → call_model + refactor direct anthropic.Anthropic()
│   ├── resume_feedback.py          MOD  — call_claude → call_model
│   ├── interview_prep.py           MOD  — call_claude → call_model
│   ├── rejection_analyzer.py       MOD  — call_claude → call_model
│   ├── resume_validator.py         MOD  — call_claude → call_model
│   ├── resume_style_guide.py          MOD  — call_claude → call_model
│   ├── description_reformatter.py     MOD  — call_claude → call_model
│   ├── careers_scraper.py             MOD  — call_claude → call_model
│   ├── scoring_runner.py              MOD  — refactor direct anthropic.Anthropic() to use provider layer
│   ├── profile_schema.py              MOD  — refactor direct anthropic.Anthropic() to use provider layer
│   ├── ats_scanner.py                 MOD  — refactor direct anthropic.Anthropic() to use provider layer
│   ├── backfill_enrichment.py         MOD  — refactor direct anthropic.Anthropic() to use provider layer
│   └── blueprints/
│       ├── guidelines.py              MOD  — refactor direct anthropic.Anthropic() to use provider layer
│       ├── batch_scoring.py           MOD  — refactor direct anthropic.Anthropic() to use provider layer
│       ├── jobs.py                    MOD  — refactor direct anthropic.Anthropic() to use provider layer
│       ├── resume.py                  MOD  — refactor direct anthropic.Anthropic() to use provider layer
│       ├── resume_review.py           MOD  — call_claude → call_model
│       ├── profile_recommendations.py MOD  — call_claude → call_model
│       ├── profile.py                 —— no migration needed (API key verification only)
│       └── costs.py                   MOD  — add provider grouping
├── eval/                           NEW
│   ├── __init__.py                 NEW
│   ├── benchmark.py                NEW  — CLI entry point + orchestrator
│   ├── sample.py                   NEW  — job sampling + prompt reconstruction
│   ├── compare.py                  NEW  — metrics computation
│   └── report.py                   NEW  — verdict + JSON report generation
└── config.py                          MOD  — add DEFAULT_PROVIDER config

scoring_evaluator.py                   MOD  — call_claude → call_model (root-level CLI script)
config.example.yaml                    MOD  — add providers section with examples
eval_results/                          NEW  — benchmark output (gitignored)
```

## ClaudeContext Compatibility

`call_model()` accepts `ctx: ClaudeContext` for backwards compatibility. It uses `ctx.conn` and `ctx.config` for database and config access across all providers, but only passes `ctx.client` (the `anthropic.Anthropic()` instance) to the Anthropic adapter. Gemini and Ollama adapters create their own SDK clients internally from provider connection settings in config. This is safe because the Anthropic client is never used outside the Anthropic adapter.

## Testing Strategy

- **Adapter unit tests:** Each provider adapter tested with mocked SDK responses (mocked `anthropic.Anthropic`, mocked `google.genai`, mocked `requests.post` for Ollama). Verify structured output translation, error handling, and `ModelResult` construction.
- **Dispatcher tests:** Test config resolution (tier → provider + model), fallback chain on failure, budget gate bypass for free providers.
- **Schema validation tests:** Test validate-retry-fallback flow with intentionally invalid JSON, partial schemas, and type mismatches.
- **Existing test migration:** Tests that currently mock `call_claude` at injection points switch to mocking `call_model`. Since the return type is identical, test assertions don't change — only the mock target.
- **Eval framework tests:** Synthetic ground truth (fake Sonnet results + fake candidate results) to test metrics computation, verdict thresholds, and report generation. No real API calls.

## What Doesn't Change

- `call_claude()` — stays as-is, becomes Anthropic adapter's backend
- Budget gating logic — stays in `claude_client.py`, called by dispatcher
- `ClaudeContext` dataclass — still used for threading client/conn/config
- All prompt and schema definitions — unchanged in their respective modules
- Pipeline detector — no AI calls, untouched
- APScheduler jobs — untouched, they call scorers which call `call_model()`
- Template rendering and HTMX patterns — untouched
