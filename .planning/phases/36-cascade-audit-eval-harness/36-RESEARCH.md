# Phase 36: Cascade Audit Eval Harness — Research

**Researched:** 2026-05-14
**Status:** Complete

---

## Standard Stack

### Core Dependencies (existing)
- **anthropic~=0.84** — Anthropic SDK for gold baseline calls
- **requests~=2.31** — HTTP client for OpenRouter API and web fetching
- **jsonschema>=4.0.0** — Schema validation for verdicts and provider outputs
- **pydantic>=2.0** — Structured data models (Verdict ADT)
- **playwright** — Browser automation for ai_nav_discovery replay
- **sqlite3** (stdlib) — Production DB queries for corpus sampling

### New Dependencies (to add)
- **None** — OpenRouter integration uses requests directly; no new SDK needed

### Project-Internal Modules
- `job_finder.web.providers.base_provider` — BaseProvider abstract class
- `job_finder.web.model_provider` — ModelResult dataclass, call_model dispatcher
- `job_finder.web.claude_client` — call_claude for Anthropic gold baseline
- `job_finder.eval.harness` — Reference implementation for eval harness pattern
- `job_finder.db_helpers` — DB connection helpers for corpus sampling

---

## Architecture Patterns

### Provider Adapter Pattern
All providers implement `BaseProvider.call()` with identical signature:
```python
def call(
    self,
    model: str,
    system: str,
    messages: list[dict],
    output_schema: dict | None = None,
    max_tokens: int = 1024,
    timeout: float | None = None,
) -> ModelResult:
```

**Key characteristics:**
- Return `ModelResult` dataclass with fields: data, cost_usd, input_tokens, output_tokens, model, provider, schema_valid
- Cost recording via `_maybe_record_cost()` for non-Anthropic providers
- Anthropic providers delegate to `call_claude()` which handles cost recording internally
- Schema validation results propagated via `schema_valid` field

### Eval Harness Pattern
From `job_finder/eval/harness.py`:
- **Corpus loader** — Samples production DB rows via `_load_gold_rows()`
- **Metrics computation** — Aggregates results per provider
- **Artifact persistence** — Atomic writes to `eval_runs` table + markdown reports
- **CLI entrypoint** — `run_eval.py` orchestrates rounds

### Task Adapter Protocol
Defined in `evals/cascade_audit/adapters/__init__.py`:
```python
class TaskAdapter(Protocol):
    def sample(self, n: int) -> list[dict]:  # Load production rows
    def exercise(self, row: dict, provider: str) -> dict:  # Run callsite
    def score(self, gold: dict, candidate: dict) -> dict:  # Judge comparison
```

**Purpose:** Type-hinted contract ensuring IDE auto-completion and mypy validation across all 6 adapters.

### Shadow-Replay Methodology
1. **Corpus sampling** — Query production DB for rows with required fields
2. **Input caching** — Persist HTML, JD text, recipes to `artifacts/round_N/` for determinism
3. **Dual execution** — Exercise callsite against Provider A and Provider B with identical inputs
4. **Judging** — Feed A/B outputs to judge with blinded prompts
5. **Position-swap validation** — Judge each pair twice (A/B and B/A); agreement = confident verdict

### Atomic Artifact Writes
Pattern from `wiki/patterns/atomic-artifact-writes.md`:
- Write to temp file (e.g., `artifact.json.tmp`)
- Rename to final path (atomic on POSIX, Windows `MoveFileEx` with `MOVEFILE_REPLACE_EXISTING`)
- Prevents partial state on interruption/crash

### Environment Provenance Block
Per `wiki/patterns/environment-provenance-block.md`, each artifact includes:
```json
{
  "provenance": {
    "provider_config": { ... },
    "model_versions": { ... },
    "harness_commit_sha": "...",
    "sample_seed": 42,
    "scheduler_pause_status": true
  },
  "results": { ... }
}
```

---

## Don't Hand-Roll

### Use Existing Patterns
- **Provider adapters** — Implement `BaseProvider`, don't create new HTTP client wrappers
- **Schema validation** — Use `jsonschema.validate()` with existing schemas, don't write custom validators
- **Cost recording** — Use `_maybe_record_cost()` for non-Anthropic paths, don't write custom INSERT logic
- **DB connections** — Use `job_finder.db_helpers` helpers, don't open raw sqlite3 connections
- **Config loading** — Use `config.py` patterns, don't reimplement YAML parsing

### Don't Build Custom Frameworks
- **No multi-agent orchestration** — This is a structured testing harness, not a LangChain/CrewAI workflow
- **No RAG retrieval** — All context provided as explicit input; no vector database needed
- **No conversation history** — Stateless between rounds; all state persisted to JSON/DB
- **No async parallelization** — Synchronous execution is sufficient for offline batch process

### Don't Reimplement Eval Infrastructure
- **Corpus loading** — Follow `job_finder/eval/harness.py` pattern, don't invent new sampling logic
- **Metrics computation** — Use standard library (statistics module), don't import pandas/numpy
- **Report generation** — Markdown templates + Jinja2, don't use specialized report libraries

---

## Common Pitfalls

### Judge Temperature Non-Zero
**Problem:** Using temperature > 0 for judge introduces non-determinism; same A/B pair produces different verdicts.
**Solution:** Set `temperature=0` for all DeepSeek-V3.2 judge calls.

### Missing Schema Valid Propagation
**Problem:** Forgetting to propagate `schema_valid` from provider → ModelResult → _maybe_record_cost breaks Phase 35 telemetry.
**Solution:** All adapter INSERT paths must populate `scoring_costs.schema_valid` column.

### Position-Swap Agreement Not Computed
**Problem:** Running judge once (A vs B) misses position bias.
**Solution:** CLI must call `judge_pair()` twice (A/B and B/A) and compute consensus; disagreement = tie.

### Artifact Writes Not Atomic
**Problem:** Writing artifacts incrementally creates partial state on interruption.
**Solution:** Use atomic writes (write temp file, then rename) for all artifact JSONs.

### Playwright Context Lifecycle Mismanagement
**Problem:** Launching new browser context per ai_nav_discovery call is slow and resource-intensive.
**Solution:** Use single context per round, managed by `run_audit.py` context manager.

### Corpus Sampling Without Deduplication
**Problem:** Sampling same company/job multiple times across rounds skews statistics.
**Solution:** Persist `dedup_keys` from Round 0 to `artifacts/round_0/dedup_keys.json`; reuse in subsequent rounds.

### Missing Scheduler Pause
**Problem:** `agentic_backfill` runs nightly at 3:30 AM and competes with Ollama for GPU during Round 2 overnight batch.
**Solution:** Pause schedulers pre-flight (Round 1 start), emit clear "RESUME SCHEDULERS" prompt at Round 2 end.

### HTML Caching Timing
**Problem:** Caching HTML at Round 0 (n≤3) doesn't represent full corpus.
**Solution:** Cache HTML for extract_jobs at Round 1 start (full n=50 corpus), freeze across Round 1 → Round 2.

### OpenRouter Model ID Format
**Problem:** Using wrong model ID format for DeepSeek-V3.2 on OpenRouter.
**Solution:** Use `deepseek/deepseek-chat:free` or similar free-tier endpoint; verify via OpenRouter docs.

### Cost Projection Underestimation
**Problem:** Round 2 cost estimate assumes ~3k tokens per judge call, but verbose outputs blow up tokens.
**Solution:** Round 0 dry-run measures actual token counts; if projected Round 2 judge spend > $15, recheck before proceeding.

---

## Code Examples

### OpenRouter Provider Adapter
```python
# job_finder/web/providers/openrouter_provider.py
import os
import requests
from job_finder.web.model_provider import BaseProvider, ModelResult

class OpenRouterProvider(BaseProvider):
    """OpenRouter HTTP adapter for DeepSeek-V3.2 judge."""
    
    def __init__(self, config: dict) -> None:
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY not set")
        self._api_key = api_key
        self._base_url = "https://openrouter.ai/api/v1"
    
    def call(
        self,
        model: str,
        system: str,
        messages: list[dict],
        output_schema: dict | None = None,
        max_tokens: int = 1024,
        timeout: float | None = None,
    ) -> ModelResult:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system}] + messages,
            "temperature": 0,  # Deterministic for judge
            "max_tokens": max_tokens,
        }
        if output_schema:
            payload["response_format"] = {"type": "json_object"}
        
        resp = requests.post(
            f"{self._base_url}/chat/completions",
            json=payload,
            headers=headers,
            timeout=timeout or 60,
        )
        resp.raise_for_status()
        
        body = resp.json()
        data = json.loads(body["choices"][0]["message"]["content"])
        
        return ModelResult(
            data=data,
            cost_usd=0.0,  # Free tier
            input_tokens=body["usage"]["prompt_tokens"],
            output_tokens=body["usage"]["completion_tokens"],
            model=model,
            provider="openrouter",
            schema_valid=True,
        )
```

### Task Adapter Example (parse_structured_fields)
```python
# evals/cascade_audit/adapters/parse_structured_fields_adapter.py
from typing import Protocol
from job_finder.web.model_provider import call_model

class TaskAdapter(Protocol):
    def sample(self, n: int, conn) -> list[dict]: ...
    def exercise(self, row: dict, provider: str, config: dict, conn) -> dict: ...
    def score(self, gold: dict, candidate: dict) -> dict: ...

class ParseStructuredFieldsAdapter:
    def sample(self, n: int, conn) -> list[dict]:
        """Sample n jobs with jd_full from production DB."""
        query = """
            SELECT dedup_key, jd_full
            FROM jobs
            WHERE jd_full IS NOT NULL AND LENGTH(jd_full) > 400
            ORDER BY RANDOM()
            LIMIT ?
        """
        cursor = conn.execute(query, (n,))
        return [dict(row) for row in cursor.fetchall()]
    
    def exercise(self, row: dict, provider: str, config: dict, conn) -> dict:
        """Re-run parse_structured_fields with exact prompt."""
        # Import actual function from enrichment_tiers.py
        from job_finder.web.enrichment_tiers import parse_structured_fields
        
        result = parse_structured_fields(
            jd_full=row["jd_full"],
            provider=provider,
            config=config,
            conn=conn,
        )
        return result
    
    def score(self, gold: dict, candidate: dict) -> dict:
        """Compute metrics vs Anthropic gold baseline."""
        # Schema valid
        schema_valid = isinstance(candidate, dict) and all(k in candidate for k in gold)
        
        # Salary MAE (if both populated)
        salary_mae = None
        if gold.get("salary_min") and candidate.get("salary_min"):
            salary_mae = abs(gold["salary_min"] - candidate["salary_min"])
        
        # Location exact match
        location_match = gold.get("location") == candidate.get("location")
        
        # Hallucination count (fields in candidate not in gold)
        hallucinations = len(set(candidate.keys()) - set(gold.keys()))
        
        return {
            "schema_valid": schema_valid,
            "salary_mae": salary_mae,
            "location_match": location_match,
            "hallucination_count": hallucinations,
        }
```

### Judge Protocol with Position-Swap
```python
# evals/cascade_audit/judge.py
from pydantic import BaseModel, Field
from typing import Literal

class Verdict(BaseModel):
    """Judge verdict for pairwise comparison."""
    winner: Literal["A", "B", "tie"] = Field(description="Which output is better")
    rationale: str = Field(description="Brief explanation")
    confidence: float = Field(ge=0.0, le=1.0)

JUDGE_SYSTEM_PROMPT = """
You are an expert evaluator for job search automation systems.
Compare two LLM outputs (A and B) for the same input.

Evaluation criteria (in order of priority):
1. Functional correctness: Does the output contain all required fields?
2. Semantic accuracy: Is the extracted information factually correct?
3. Completeness: Does the output include all relevant information?

If both outputs are equally good, return "tie".
"""

def judge_pair(
    output_a: dict,
    output_b: dict,
    callsite: str,
    provider: OpenRouterProvider,
) -> Verdict:
    """Compare A/B pair and return verdict."""
    prompt = f"""
Callsite: {callsite}

Output A:
{json.dumps(output_a, indent=2)}

Output B:
{json.dumps(output_b, indent=2)}

Which output is better? Respond with JSON matching the Verdict schema.
"""
    
    response = provider.call(
        model="deepseek/deepseek-chat:free",
        system=JUDGE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
        output_schema=Verdict.model_json_schema(),
        temperature=0,
    )
    
    return Verdict.model_validate_json(response.data)

def judge_with_position_swap(
    output_a: dict,
    output_b: dict,
    callsite: str,
    provider: OpenRouterProvider,
) -> tuple[Verdict, bool]:
    """Judge A/B and B/A, return consensus verdict."""
    verdict_ab = judge_pair(output_a, output_b, callsite, provider)
    verdict_ba = judge_pair(output_b, output_a, callsite, provider)
    
    # Agreement: both verdicts agree (A wins both, B wins both, or both tie)
    agreement = verdict_ab.winner == verdict_ba.winner or verdict_ab.winner == "tie"
    
    # Consensus: use verdict_ab if agreement, else tie
    consensus = verdict_ab if agreement else Verdict(winner="tie", rationale="Position swap disagreement", confidence=0.5)
    
    return consensus, agreement
```

### Corpus Loader with Dedup Key Persistence
```python
# evals/cascade_audit/corpus_loader.py
import json
from pathlib import Path

class CorpusLoader:
    def __init__(self, artifact_dir: Path):
        self._artifact_dir = artifact_dir
        self._dedup_keys_file = artifact_dir / "dedup_keys.json"
    
    def load_round_0(self, n_per_callsite: int, conn) -> dict[str, list[dict]]:
        """Sample n rows per callsite, persist dedup_keys for reproducibility."""
        corpus = {}
        dedup_keys = {}
        
        for callsite in [
            "parse_structured_fields",
            "find_careers_url",
            "extract_jobs",
            "description_reformat",
            "company_research",
            "ai_nav_discovery",
        ]:
            rows = self._sample_callsite(callsite, n_per_callsite, conn)
            corpus[callsite] = rows
            dedup_keys[callsite] = [r["dedup_key"] for r in rows]
        
        # Persist dedup_keys for Round 1 → Round 2 reproducibility
        self._dedup_keys_file.write_text(json.dumps(dedup_keys, indent=2))
        
        return corpus
    
    def load_round_1(self, conn) -> dict[str, list[dict]]:
        """Load using persisted dedup_keys from Round 0."""
        dedup_keys = json.loads(self._dedup_keys_file.read_text())
        corpus = {}
        
        for callsite, keys in dedup_keys.items():
            rows = self._load_by_keys(callsite, keys, conn)
            corpus[callsite] = rows
        
        return corpus
```

### CLI Orchestrator with Atomic Artifact Writes
```python
# evals/cascade_audit/run_audit.py
import tempfile
from pathlib import Path

def write_artifact_atomic(data: dict, output_path: Path) -> None:
    """Write artifact atomically to prevent partial state on interruption."""
    temp_path = output_path.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(data, indent=2))
    temp_path.replace(output_path)  # Atomic on POSIX and Windows

def run_round(round_num: int, callsites: list[str], providers: list[str]) -> None:
    """Run audit round with atomic artifact writes."""
    artifact_dir = Path(f"artifacts/round_{round_num}")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    
    for callsite in callsites:
        for provider in providers:
            # Run adapter
            results = execute_adapter(callsite, provider)
            
            # Add provenance block
            artifact = {
                "provenance": {
                    "provider_config": get_provider_config(provider),
                    "model_versions": get_model_versions(),
                    "harness_commit_sha": get_git_sha(),
                    "sample_seed": 42,
                    "scheduler_pause_status": check_schedulers_paused(),
                },
                "results": results,
            }
            
            # Atomic write
            output_path = artifact_dir / f"{callsite}_{provider}.json"
            write_artifact_atomic(artifact, output_path)
```

---

## Validation Architecture

### Dimension 1: Judge Consistency
**Pass threshold:** Position-swap agreement ≥ 80%
**Measurement:** Compute agreement rate across all position-swapped pairs
**Priority:** Critical

### Dimension 2: Verdict Reproducibility
**Pass threshold:** Re-running judge on same A/B pair produces same verdict ≥ 90% of time
**Measurement:** Re-judge 10% of pairs, compute consistency
**Priority:** High

### Dimension 3: Functional Correctness
**Pass threshold:** Provider output passes schema validation on ≥ 95% of calls
**Measurement:** Query Phase 35 telemetry (scoring_costs.schema_valid column)
**Priority:** Critical

### Dimension 4: Semantic Accuracy
**Rubric:** 1-5 scale (human spot-check of 10 random verdicts)
**Measurement:** Project author reviews 10 verdicts, flags obvious errors
**Priority:** High

### Dimension 5: Artifact Completeness
**Pass threshold:** All artifacts include provenance block (config, versions, SHA, seed)
**Measurement:** Validate artifact JSON schema before persisting
**Priority:** Medium

### Dimension 6: Cost Reduction
**Pass threshold:** Winning provider reduces cost ≥ 50% vs Anthropic baseline
**Measurement:** Compare per-call costs from scoring_costs table
**Priority:** High

---

## RESEARCH COMPLETE

All domains investigated. Key findings:
- Provider adapter pattern is well-established (AnthropicProvider, OllamaProvider as examples)
- Eval harness pattern exists in `job_finder/eval/harness.py` — reuse structure
- OpenRouter integration is straightforward HTTP client work (no new SDK needed)
- Position-swap validation is critical for judge reliability
- Atomic artifact writes prevent partial state on interruption
- Scheduler pause is required to avoid GPU contention during overnight runs
- HTML caching at Round 1 start ensures deterministic inputs across rounds

Phase 36 is ready for planning.
