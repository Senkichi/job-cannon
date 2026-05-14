# Phase 36: Cascade Audit Eval Harness — Pattern Map

**Generated:** 2026-05-14
**Purpose:** Extract file lists from CONTEXT.md and RESEARCH.md, classify by role/data flow, find analogs in codebase, produce concrete code excerpts for planning.

---

## Files to Create/Modify

### New Files

| File | Role | Data Flow | Closest Analog | Code Excerpt |
|------|------|-----------|----------------|--------------|
| `job_finder/web/providers/openrouter_provider.py` | Provider adapter | HTTP client → ModelResult | `job_finder/web/providers/ollama_provider.py` | See OllamaProvider.call() implementation |
| `evals/cascade_audit/__init__.py` | Package init | Exports adapters, judge, verdict | `job_finder/eval/__init__.py` | Empty init with __all__ exports |
| `evals/cascade_audit/corpus_loader.py` | Corpus sampling | DB → dedup_keys → artifacts | `job_finder/eval/harness.py:_load_gold_rows()` | See _load_gold_rows() for DB query pattern |
| `evals/cascade_audit/adapters/__init__.py` | Protocol definition | Type hints for TaskAdapter | N/A (new pattern) | Protocol class with sample(), exercise(), score() |
| `evals/cascade_audit/adapters/parse_structured_fields_adapter.py` | Callsite adapter | DB row → provider call → metrics | `job_finder/web/enrichment_tiers.py:parse_structured_fields()` | See function signature and prompt structure |
| `evals/cascade_audit/adapters/find_careers_url_adapter.py` | Callsite adapter | DB row → HTML fetch → provider call → metrics | `job_finder/web/careers_scraper.py:_find_careers_url_with_haiku()` | See function signature and HTML handling |
| `evals/cascade_audit/adapters/extract_jobs_adapter.py` | Callsite adapter | Cached HTML → provider call → metrics | `job_finder/web/careers_scraper.py:_extract_jobs_with_haiku()` | See function signature and list extraction |
| `evals/cascade_audit/adapters/description_reformat_adapter.py` | Callsite adapter | DB row → provider call → judge verdict | `job_finder/web/description_reformatter.py:reformat_description()` | See function signature and text transformation |
| `evals/cascade_audit/adapters/company_research_adapter.py` | Callsite adapter | DB row → provider call → judge verdict | `job_finder/web/company_research.py:run_company_research_background()` | See function signature and summarization |
| `evals/cascade_audit/adapters/ai_nav_discovery_adapter.py` | Callsite adapter | Cached recipe → Playwright replay → metrics | `job_finder/web/ai_career_navigator.py:discover_navigation_recipe()` | See function signature and Playwright usage |
| `evals/cascade_audit/judge.py` | Judge protocol | A/B outputs → DeepSeek → Verdict | N/A (new pattern) | judge_pair() with OpenRouter HTTP call |
| `evals/cascade_audit/verdict.py` | Verdict ADT | Pydantic model for structured output | N/A (new pattern) | Verdict BaseModel with winner/loser/tie fields |
| `evals/cascade_audit/report.py` | Report generation | Artifacts → markdown | `job_finder/eval/report.py:write_report()` | See markdown generation pattern |
| `evals/cascade_audit/run_audit.py` | CLI orchestrator | Flags → round execution → artifacts | `job_finder/eval/harness.py:run()` | See CLI argument parsing and orchestration |

### Modified Files

| File | Role | Data Flow | Closest Analog | Code Excerpt |
|------|------|-----------|----------------|--------------|
| `job_finder/web/providers/__init__.py` | Provider registration | Imports → exports | Existing pattern | Add OpenRouterProvider to imports |
| `job_finder/web/model_provider.py` | Cost recording integration | ModelResult → _maybe_record_cost | Existing pattern | Ensure schema_valid propagation (already in place) |

---

## Pattern Analysis

### Provider Adapter Pattern

**Source:** `job_finder/web/providers/ollama_provider.py`

**Key characteristics:**
```python
class OllamaProvider(BaseProvider):
    def __init__(self, config: dict) -> None:
        # Load config, health check
    
    def call(
        self,
        model: str,
        system: str,
        messages: list[dict],
        output_schema: dict | None = None,
        max_tokens: int = 1024,
        timeout: float | None = None,
    ) -> ModelResult:
        # HTTP POST to provider API
        # Parse response, extract tokens/cost
        # Return ModelResult with provider field
```

**OpenRouter adaptation:**
- Same call() signature
- HTTP POST to `https://openrouter.ai/api/v1/chat/completions`
- Headers: `Authorization: Bearer $OPENROUTER_API_KEY`
- Body: model, messages, temperature=0 (for judge)
- Return ModelResult with provider="openrouter", cost_usd=0.0 (free tier)

### Corpus Loader Pattern

**Source:** `job_finder/eval/harness.py:_load_gold_rows()`

**Key characteristics:**
```python
def _load_gold_rows(db_path: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT dedup_key, title, company, location, jd_full
            FROM jobs
            WHERE gold_classification IS NOT NULL
            ORDER BY RANDOM()
            LIMIT ?
            """,
            (n,)
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]
```

**Cascade audit adaptation:**
- Query different tables per callsite (jobs, companies)
- Filter by callsite-specific criteria (jd_full length, homepage_url exists)
- Use `ORDER BY RANDOM() LIMIT ?` for sampling
- Persist dedup_keys to JSON file for reproducibility
- Cache HTML/inputs to artifacts/ for determinism

### Task Adapter Protocol Pattern

**New pattern** - not existing in codebase, but specified in CONTEXT.md D-03:

```python
from typing import Protocol

class TaskAdapter(Protocol):
    def sample(self, n: int, conn) -> list[dict]:
        """Load n production rows from DB."""
        ...
    
    def exercise(self, row: dict, provider: str, config: dict, conn) -> dict:
        """Run callsite against provider, return output."""
        ...
    
    def score(self, gold: dict, candidate: dict) -> dict:
        """Compute metrics vs Anthropic gold baseline."""
        ...
```

**Implementation approach:**
- Define Protocol in `evals/cascade_audit/adapters/__init__.py`
- Each adapter implements the three methods
- Type hints enable IDE auto-completion and mypy validation
- No inheritance needed - Protocol is structural typing

### Judge Protocol Pattern

**New pattern** - DeepSeek-V3.2 via OpenRouter:

```python
class Verdict(BaseModel):
    winner: Literal["A", "B", "tie"]
    rationale: str
    confidence: float

def judge_pair(
    output_a: dict,
    output_b: dict,
    callsite: str,
    provider: OpenRouterProvider,
) -> Verdict:
    prompt = f"""
Callsite: {callsite}
Output A: {json.dumps(output_a)}
Output B: {json.dumps(output_b)}
Which is better? Respond with JSON matching Verdict schema.
"""
    response = provider.call(
        model="deepseek/deepseek-chat:free",
        system=JUDGE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
        output_schema=Verdict.model_json_schema(),
        temperature=0,
    )
    return Verdict.model_validate_json(response.data)
```

**Position-swap validation:**
- Call judge_pair(output_a, output_b, ...) → verdict_ab
- Call judge_pair(output_b, output_a, ...) → verdict_ba
- Agreement: verdict_ab.winner == verdict_ba.winner or both tie
- Consensus: use verdict_ab if agreement, else tie

### Atomic Artifact Write Pattern

**Source:** `wiki/patterns/atomic-artifact-writes.md` (referenced in CONTEXT.md)

```python
from pathlib import Path

def write_artifact_atomic(data: dict, output_path: Path) -> None:
    """Write artifact atomically to prevent partial state."""
    temp_path = output_path.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(data, indent=2))
    temp_path.replace(output_path)  # Atomic on POSIX and Windows
```

**Usage in run_audit.py:**
- After each (callsite, provider) pair completes
- Build artifact dict with provenance block + results
- Write atomically to `artifacts/round_N/{callsite}_{provider}.json`

### Eval Harness Orchestration Pattern

**Source:** `job_finder/eval/harness.py:run()`

**Key characteristics:**
```python
def run(
    db_path: str,
    variant_name: str = "baseline",
    n_runs: int = 3,
    baseline_run_id: str | None = None,
    report_dir: str = "eval_results",
    config: dict | None = None,
) -> str:
    # 1. Load gold rows
    gold_rows = _load_gold_rows(db_path)
    
    # 2. Load variant (prompt module)
    _load_variant(variant_name)
    
    # 3. Configure scoring
    config["scoring"]["prompt_variant"] = variant_name
    
    # 4. Connect to DB, load profile/context
    conn = sqlite3.connect(db_path)
    profile = load_scoring_profile(config)
    candidate_context = build_candidate_context(config, profile)
    
    # 5. Execute runs (nested loops: rows × n_runs)
    for row in gold_rows:
        for run_idx in range(n_runs):
            result = _score_one(row, conn, config, candidate_context)
            per_job_runs[row["dedup_key"]].append(...)
    
    # 6. Compute metrics
    metrics_out = _compute_metrics(gold_rows, per_job_mean, per_job_runs)
    
    # 7. Persist to DB
    conn.execute("INSERT INTO eval_runs ...")
    
    # 8. Write report
    report_path = write_report(...)
    
    return report_path
```

**Cascade audit adaptation:**
- Similar structure: load corpus → execute adapters → compute metrics → persist artifacts
- Differences:
  - No DB persistence (artifacts are JSON files, not eval_runs table)
  - Per-callsite adapters instead of single scorer
  - Judge protocol for subjective tasks (description_reformat, company_research)
  - Round-based execution (Round 0, 1, 2) with resumability
  - Scheduler pause pre-flight

### Playwright Integration Pattern

**Source:** `job_finder/web/ai_career_navigator.py` and `job_finder/careers_crawler/_playwright_tier.py`

**Key characteristics:**
```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context()
    page = context.new_page()
    # ... use page ...
    browser.close()
```

**Cascade audit adaptation (context manager pattern from D-06):**
```python
from contextlib import contextmanager
from playwright.sync_api import sync_playwright

@contextmanager
def playwright_context():
    """Yields a Playwright context for ai_nav_discovery adapter."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        try:
            yield context
        finally:
            browser.close()

# In run_audit.py:
with playwright_context() as pw_context:
    for row in ai_nav_rows:
        adapter = AiNavDiscoveryAdapter(pw_context)
        result = adapter.exercise(row, provider, config, conn)
```

**Benefits:**
- Single browser context per round (not per call)
- Runner manages lifecycle (launch at round start, close at round end)
- Adapter just uses the context via `with` statement
- Clean separation of concerns

---

## Integration Points

### Provider Registration

**File:** `job_finder/web/providers/__init__.py`

**Current pattern:**
```python
from job_finder.web.providers.anthropic_provider import AnthropicProvider
from job_finder.web.providers.gemini_provider import GeminiProvider
from job_finder.web.providers.ollama_provider import OllamaProvider
```

**Add:**
```python
from job_finder.web.providers.openrouter_provider import OpenRouterProvider
```

### Cost Recording Integration

**File:** `job_finder/web/model_provider.py`

**Current pattern:** `_maybe_record_cost()` already handles ModelResult.schema_valid

**No changes needed** - OpenRouterProvider returns ModelResult with schema_valid field, existing path handles it.

### DB Query Helpers

**File:** `job_finder/db_helpers.py`

**Use existing helpers** for DB connections and query patterns. No new helpers needed.

---

## PATTERNS COMPLETE

All files classified, analogs identified, code excerpts extracted. Planner can reference this PATTERNS.md for concrete implementation guidance.
