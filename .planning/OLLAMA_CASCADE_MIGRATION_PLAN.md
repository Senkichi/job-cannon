# Ollama Cascade Migration Plan

> **Purpose:** Migrate all AI call sites to the provider cascade (`call_model()`) so
> Ollama routing is universally available, then validate end-to-end with tests and
> log auditing.
>
> **Context:** An adversarial audit found that 6 call sites (83% of API spend)
> bypass the provider cascade by calling `call_claude()` directly. The cascade
> infrastructure in `model_provider.py` is well-built but underutilized. This plan
> migrates each bypass site, fixes a config-plumbing gap in the backfill path, adds
> comprehensive test coverage, and installs a permanent audit trap so the problem
> cannot recur.
>
> **Starting state:** 2447 tests passing, 0 failures. 7 uncommitted modified files
> on master (see Git State section). All changes in this plan build on top of
> that state.
>
> **Adversarial review status:** This plan has been adversarially reviewed. Four
> blockers were found and addressed in this revision. See section 10 for the full
> review log and resolutions.

---

## Table of Contents

1. [Background & Findings](#1-background--findings)
2. [Git State at Plan Creation](#2-git-state-at-plan-creation)
3. [Architecture Reference](#3-architecture-reference)
4. [Phase 0: Schema Definitions for Freeform Call Sites](#4-phase-0-schema-definitions-for-freeform-call-sites)
5. [Phase 1: Migrate Bypass Call Sites](#5-phase-1-migrate-bypass-call-sites)
6. [Phase 2: Fix Backfill Config Plumbing](#6-phase-2-fix-backfill-config-plumbing)
7. [Phase 3: Test Coverage](#7-phase-3-test-coverage)
8. [Phase 4: E2E Audit Infrastructure](#8-phase-4-e2e-audit-infrastructure)
9. [Phase 5: Validation & Sign-off](#9-phase-5-validation--sign-off)
10. [Adversarial Review Log](#10-adversarial-review-log)
11. [Anti-Regression Checklist](#11-anti-regression-checklist)

---

## 1. Background & Findings

### The Problem

The app has two AI dispatch paths that don't compose:

```
Path A: call_model() → provider cascade → OllamaProvider / AnthropicProvider
Path B: call_claude() → Anthropic API direct (bypasses cascade entirely)
```

Six call sites use Path B. They account for 83% of all observed API calls (1,922
total in the current app.log). These sites can **never** route to Ollama regardless
of config.

### Critical Design Constraint: output_schema=None Incompatibility

`call_claude()` and `OllamaProvider` have **different return contracts** when
`output_schema=None`:

| Provider | output_schema=None behavior |
|----------|---------------------------|
| `call_claude()` | Attempts `json.loads(raw)`. If valid JSON, returns parsed dict. If not, wraps in `{"text": str(raw)}`. |
| `OllamaProvider` | Always forces `"format": "json"` (line 200). Model MUST produce JSON. Returns `json.loads(content)` — arbitrary dict keys. |

This means freeform text prompts (e.g., "Return ONLY the URL") produce different
result shapes per provider:
- Anthropic: `{"text": "https://example.com/careers"}` (JSON parse fails, text wrapper)
- Ollama: `{"url": "https://example.com/careers"}` (forced JSON, model invents keys)

Downstream code doing `result.get("text", "")` silently gets `""` from Ollama.
**This is a silent data loss bug** that would affect 3 of 6 bypass sites if
migrated naively.

**Resolution:** Phase 0 adds explicit `output_schema` definitions for all
freeform text call sites. Both providers then return the same dict shape. See
section 4.

### Call Volume by Purpose (from app.log, April 17 2026)

| Purpose | Calls | % | Dispatch Path | Bypass? |
|---------|-------|---|---------------|---------|
| `careers_scrape` | 1,449 | 75.4% | `call_claude()` direct | **YES** |
| `haiku_score` | 245 | 12.7% | `call_model()` (config-gated) | No |
| `enrich_job` | 116 | 6.0% | `call_claude()` direct | **YES** |
| `sonnet_eval` | 44 | 2.3% | `call_model()` (config-gated) | No |
| `haiku_reeval` | 32 | 1.7% | same as haiku_score | No |
| `ai_nav_discovery` | 32 | 1.7% | `call_claude()` direct | **YES** |
| `enrich_job_sonnet` | 4 | 0.2% | `call_claude()` direct | **YES** |
| `description_reformat` | ? | ? | `call_claude()` direct | **YES** |

### Bypass Call Sites (6 total, 8 invocations)

| # | File | Line | Function | Purpose | Tier | Schema | max_tokens | Needs Phase 0? |
|---|------|------|----------|---------|------|--------|------------|----------------|
| 1 | `careers_scraper.py` | 117 | `_find_careers_url_with_haiku` | `careers_scrape` | haiku | None | 256 | **YES** — text prompt |
| 2 | `careers_scraper.py` | 216 | `_extract_jobs_with_haiku` | `careers_scrape` | haiku | None | 1024 | **YES** — text/JSON hybrid |
| 3 | `enrichment_tiers.py` | 510 | `extract_with_haiku` | `enrich_job` | haiku | None | 512 | No — prompt says "return JSON", both paths return parsed dict |
| 4 | `enrichment_tiers.py` | 323 | `extract_with_sonnet` | `enrich_job_sonnet` | sonnet | None | 1024 | No — same as above |
| 5 | `ai_career_navigator.py` | 376 | `discover_navigation_recipe` | `ai_nav_discovery` | haiku | dict | 1024 | No — already has schema |
| 6 | `description_reformatter.py` | 94 | `reformat_description` | `description_reformat` | haiku | None | 2048 | **YES** — text prompt |

### Additional Finding: Backfill Config Gap

`backfill_enrichment.py` defines `_offline_config()` to inject Ollama routing, but
`run_enrichment_pass()` passes raw `config` to `enrich_job()`. Even after migrating
enrichment_tiers.py, the backfill enrichment pass won't use Ollama unless this is
fixed.

### Additional Finding: Inconsistent fallback key

`_OFFLINE_PROVIDERS` in `backfill_enrichment.py` uses `"fallback"` (singular) for
the haiku entry but `"fallback_chain"` (list) for sonnet. The cascade path in
`call_model()` only activates when `fallback_chain` is non-empty (line 524). With
just `"fallback"`, the backward-compat path fires instead, which raises generic
`RuntimeError` rather than `ProviderCascadeExhaustedError`. Phase 2 fixes this.

### Log Evidence

- **app.log (current rotation, April 17):** 1,922 calls, ALL to claude-haiku-4-5
  or claude-sonnet-4-6. Zero Ollama calls.
- **app.log.1 (April 8):** Shows "Calibrated ollama score" entries — Ollama was
  active for scoring. Also shows cascade errors: "Unterminated string starting at"
  (schema adherence failure).
- **api_spend_trap.log:** 17 entries, all through `anthropic_provider.py`. No
  Ollama entries. The trap is **not wired in code** — it was created by an
  ephemeral hook/script since removed. This plan installs a permanent replacement.

---

## 2. Git State at Plan Creation

**Branch:** master  
**Date:** 2026-04-18  
**Uncommitted changes (staged with `git diff`):**

| File | Change Summary |
|------|----------------|
| `job_finder/web/backfill_enrichment.py` | Added `_OFFLINE_PROVIDERS`, `_offline_config()`, wired into `run_sonnet_backfill()` and `run_borderline_rescore()` |
| `job_finder/web/claude_client.py` | Added CLI env vars for cold-start tuning (MCP_CONNECTION_NONBLOCKING, MAX_STRUCTURED_OUTPUT_RETRIES, --strict-mcp-config, --disable-slash-commands) |
| `job_finder/web/haiku_scorer.py` | Added `_CLIClientStub`, `use_dispatcher` pattern with `call_model()`, `ProviderCascadeExhaustedError` handling |
| `job_finder/web/sonnet_evaluator.py` | Same as haiku_scorer — `use_dispatcher`, `call_model()`, cascade fallback |
| `job_finder/web/scoring_runner.py` | Liveness gate moved from pre-Haiku to pre-Sonnet |
| `tests/conftest.py` | Added autouse fixtures: `mock_liveness_check`, `mock_scheduler_pidfile` |
| `tests/test_scoring_runner.py` | Liveness gate tests updated for Sonnet path |

**Test suite:** 2447 passed, 1 skipped, 0 failed.

**Action:** Commit these changes first before starting Phase 0. They are
prerequisites — the `_CLIClientStub` and `use_dispatcher` patterns in
haiku_scorer.py and sonnet_evaluator.py are the reference implementation that all
Phase 1 migrations will follow.

---

## 3. Architecture Reference

### The Dispatch Pattern (copy from haiku_scorer.py)

Every migrated call site MUST follow this exact pattern:

```python
# --- Imports needed ---
from job_finder.web.model_provider import ProviderCascadeExhaustedError, call_model
from job_finder.web.claude_client import call_claude  # keep for fallback

# --- Stub for CLI-based Anthropic routing (no SDK import needed) ---
class _CLIClientStub:
    api_key = "cli-managed"

_CLI_CLIENT_STUB = _CLIClientStub()

# --- Dispatch logic ---
use_dispatcher = bool(config.get("providers", {}).get("TIER"))  # "haiku" or "sonnet"

try:
    if use_dispatcher:
        model_result = call_model(
            tier="TIER",                    # "haiku" or "sonnet"
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
            conn=conn,
            config=config,
            output_schema=SCHEMA_OR_NONE,
            job_id=job_id_or_none,
            purpose="PURPOSE_STRING",
            max_tokens=MAX_TOKENS,
            client=_CLI_CLIENT_STUB,
        )
        result = model_result.data
        cost_usd = model_result.cost_usd
    else:
        result, cost_usd = call_claude(
            model=model,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
            output_schema=SCHEMA_OR_NONE,
            conn=conn,
            job_id=job_id_or_none,
            purpose="PURPOSE_STRING",
            config=config,
            max_tokens=MAX_TOKENS,
        )
except ProviderCascadeExhaustedError as exc:
    logger.warning("... cascade exhausted ... retrying via CLI", ...)
    try:
        result, cost_usd = call_claude(
            model=model,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
            output_schema=SCHEMA_OR_NONE,
            conn=conn,
            job_id=job_id_or_none,
            purpose="PURPOSE_STRING",
            config=config,
            max_tokens=MAX_TOKENS,
        )
    except Exception:
        logger.warning("... CLI retry also failed ...")
        # return original/empty/None — site-specific fallback
```

### call_model() Signature (model_provider.py)

```python
def call_model(
    tier: str,                          # "haiku" | "sonnet" | "opus"
    system: str,                        # System prompt
    messages: list[dict],               # [{"role": "user", "content": "..."}]
    conn: sqlite3.Connection,           # DB connection for cost recording (REQUIRED, cannot be None)
    config: dict,                       # App config (must have providers.TIER for cascade)
    output_schema: dict | None = None,  # JSON schema or None
    job_id: str | None = None,          # For cost attribution
    purpose: str = "",                  # Cost tracking label
    max_tokens: int = 1024,             # Max output tokens
    timeout: float | None = None,       # Provider timeout override
    client: Any | None = None,          # _CLI_CLIENT_STUB for Anthropic fallback
) -> ModelResult:
```

**CRITICAL:** `conn` is NOT optional. `call_model()` calls `_ensure_usage_current(conn)`
and `_maybe_record_cost(result, conn, ...)` which will `AttributeError` on None.
Any call site where `conn` can be None MUST guard `use_dispatcher = False` when
`conn is None`.

### ModelResult (model_provider.py, frozen dataclass)

```python
@dataclass(frozen=True, slots=True)
class ModelResult:
    data: dict
    cost_usd: float
    input_tokens: int
    output_tokens: int
    model: str
    provider: str
    schema_valid: bool
```

### How call_claude() Returns Results (claude_client.py lines 581-598)

When `output_schema` IS provided:
- Returns `(structured_output_dict, cost_usd)` — parsed from Claude's native
  structured output. Always a dict matching the schema.

When `output_schema` is None:
- Attempts `json.loads(raw)` on the model's text response.
- If valid JSON: returns `(parsed_dict, cost_usd)` — the parsed dict directly.
- If not valid JSON: returns `({"text": str(raw)}, cost_usd)` — wrapped in text key.

**Implication:** Call sites using `output_schema=None` with freeform text prompts
(careers_scraper, description_reformatter) get `{"text": "raw text"}`. Call sites
using `output_schema=None` with JSON-instructed prompts (enrichment_tiers) get
the parsed JSON dict directly.

### How OllamaProvider Returns Results (ollama_provider.py lines 197-224)

- ALWAYS sends `"format": "json"` (line 200) — model MUST produce JSON regardless
  of `output_schema`.
- Returns `ModelResult(data=json.loads(content), ...)` — arbitrary dict keys.
- When `output_schema` is provided, schema instructions are embedded in system
  prompt, guiding the model to use correct keys.
- When `output_schema` is None, the model invents its own JSON structure.

**Implication:** With `output_schema=None`, Ollama returns dicts with arbitrary
keys (e.g., `{"url": "..."}`, `{"content": "..."}`) while `call_claude()` returns
`{"text": "..."}`. Downstream code using `result.get("text", "")` silently gets
empty strings from Ollama. **Phase 0 fixes this by adding schemas.**

### _CLIClientStub (haiku_scorer.py lines 29-36)

```python
class _CLIClientStub:
    api_key = "cli-managed"

_CLI_CLIENT_STUB = _CLIClientStub()
```

`_make_adapter("anthropic", ...)` in model_provider.py checks for an `api_key`
attribute. The stub satisfies this without importing the Anthropic SDK, routing
Anthropic calls through the CLI path (preserving OAuth/subscription billing).

### Key Constraints

- `call_model()` requires `config["providers"][tier]` to exist. Without it, it
  raises KeyError. The `use_dispatcher` guard prevents this.
- `ProviderCascadeExhaustedError` is a `RuntimeError` subclass. Catch it
  specifically BEFORE generic `Exception`.
- The `_sanitize_output()` function (model_provider.py line 307) handles Ollama
  quirks: strips extra keys, coerces string→int for integer fields, coerces
  verbose strings to enum values, backfills missing required arrays. It short-
  circuits when `schema is None` (line 320: `if schema is None: return data`).
  **This means schema-less call sites get NO sanitization** — another reason to
  add schemas in Phase 0.

---

## 4. Phase 0: Schema Definitions for Freeform Call Sites

### Why This Phase Exists

Three bypass call sites use `output_schema=None` with freeform text prompts.
Without explicit schemas, these sites would silently produce empty results when
routed through Ollama (see section 1, "Critical Design Constraint").

Adding schemas also enables `_sanitize_output()` for Ollama responses (type
coercion, extra key stripping) and `_schema_to_field_instructions()` for clearer
Ollama prompts.

### 4.1 Schema for `careers_scraper.py` — URL Discovery (site 1)

**File:** `job_finder/web/careers_scraper.py`  
**Add near top of file, after imports:**

```python
_CAREERS_URL_SCHEMA = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "description": "The absolute URL to the careers/jobs page, or the word 'none' if not found",
        },
    },
    "required": ["url"],
    "additionalProperties": False,
}
```

**Update `_find_careers_url_with_haiku()`:**

Change the `output_schema=None` argument to `output_schema=_CAREERS_URL_SCHEMA`
in BOTH the `call_claude()` and `call_model()` invocations.

Update the result extraction (currently lines 129-131):
```python
# Before:
url_text = result.get("text", "").strip()

# After:
url_text = result.get("url", "").strip()
```

The rest of the function (relative URL resolution, http validation) stays the same.

### 4.2 Schema for `careers_scraper.py` — Job Extraction (site 2)

```python
_CAREERS_JOBS_SCHEMA = {
    "type": "object",
    "properties": {
        "jobs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "location": {"type": "string"},
                },
                "required": ["title"],
            },
        },
    },
    "required": ["jobs"],
    "additionalProperties": False,
}
```

**Update `_extract_jobs_with_haiku()`:**

Change `output_schema=None` to `output_schema=_CAREERS_JOBS_SCHEMA`.

Update the result extraction (currently lines 228-235):
```python
# Before:
text = result.get("text", "").strip()
if text.startswith("```"):
    text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
jobs = _json.loads(text)
if not isinstance(jobs, list):
    return []

# After:
jobs = result.get("jobs", [])
if not isinstance(jobs, list):
    return []
```

This eliminates the fragile text→JSON parsing and markdown code block stripping.
Both providers now return `{"jobs": [...]}` directly.

### 4.3 Schema for `description_reformatter.py`

```python
_REFORMAT_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": "The reformatted job description with clear section headers",
        },
    },
    "required": ["text"],
    "additionalProperties": False,
}
```

**Update `reformat_description()`:**

Change `output_schema=None` to `output_schema=_REFORMAT_SCHEMA`.

The result extraction (lines 107-110) already uses `result.get("text", "")` which
matches the schema key. No change needed there.

### 4.4 No Schema Needed: enrichment_tiers.py

Both `extract_with_haiku()` and `extract_with_sonnet()` use prompts that
explicitly instruct "Return ONLY a JSON object with these fields: jd_full,
salary_min, salary_max..." The model returns valid JSON, which both `call_claude()`
and OllamaProvider parse into the same dict shape. The downstream code iterates
`result.items()` checking for known keys — compatible with both providers.

**However**, adding schemas here would enable `_sanitize_output()` (type coercion
for salary fields, extra key stripping). This is a nice-to-have but NOT blocking.
Defer to a follow-up if desired. The current `output_schema=None` is safe for
migration.

### 4.5 No Schema Needed: ai_career_navigator.py

Already has `output_schema=recipe_schema` defined inline (lines 347-374). No
change needed.

### Validation After Phase 0

```bash
uv run --active pytest tests/test_careers_scraper.py tests/test_description_reformatter.py -q --tb=short
```

Existing tests mock at the helper function level, so they should be unaffected by
schema additions. If any tests assert on `result.get("text", ...)`, update them
to match the new schema keys.

**Commit:** `refactor: add output schemas for freeform Haiku call sites`

---

## 5. Phase 1: Migrate Bypass Call Sites

### Commit strategy: One commit per file migrated. Atomic, revertible.

### 5.1 Migrate `careers_scraper.py` (2 call sites)

**File:** `job_finder/web/careers_scraper.py`  
**Priority:** HIGHEST — 75% of all API calls.

**Step 1: Move local imports to module level and add cascade imports.**

Both `_find_careers_url_with_haiku` (line 103-104) and `_extract_jobs_with_haiku`
(line 202-203) have local imports inside their function bodies:
```python
from job_finder.config import DEFAULT_MODEL_HAIKU
from job_finder.web.claude_client import call_claude
```

Move these to module level. Then ADD the cascade imports:
```python
from job_finder.config import DEFAULT_MODEL_HAIKU
from job_finder.web.claude_client import call_claude
from job_finder.web.model_provider import ProviderCascadeExhaustedError, call_model
```

Remove the duplicate local imports from both function bodies.

**Step 2: Add `_CLIClientStub` near top of file (after imports):**
```python
class _CLIClientStub:
    api_key = "cli-managed"

_CLI_CLIENT_STUB = _CLIClientStub()
```

**Step 3: Site 1 — `_find_careers_url_with_haiku()` (~line 117):**

This function receives `conn` and `config` as parameters. Replace the
`call_claude()` invocation with the dispatch pattern.

**IMPORTANT:** The existing code has a `try/except Exception` block that returns
`None` (line 144). The cascade exceptions must be caught INSIDE this block, before
the generic handler.

```python
    try:
        use_dispatcher = bool(config.get("providers", {}).get("haiku"))

        if use_dispatcher:
            model_result = call_model(
                tier="haiku",
                system=system,
                messages=messages,
                conn=conn,
                config=config,
                output_schema=_CAREERS_URL_SCHEMA,
                job_id=None,
                purpose="careers_scrape",
                max_tokens=256,
                client=_CLI_CLIENT_STUB,
            )
            result = model_result.data
        else:
            result, cost = call_claude(
                model=DEFAULT_MODEL_HAIKU,
                system=system,
                messages=messages,
                output_schema=_CAREERS_URL_SCHEMA,
                conn=conn,
                job_id=None,
                purpose="careers_scrape",
                config=config,
                max_tokens=256,
            )

        url_text = result.get("url", "").strip()
        if not url_text or url_text.lower() == "none":
            return None

        # Resolve relative URL
        if url_text.startswith("/"):
            url_text = urljoin(homepage_url, url_text)

        if url_text.startswith("http"):
            logger.debug("Haiku found careers URL for '%s': %s", homepage_url, url_text)
            return url_text

        return None

    except ProviderCascadeExhaustedError:
        logger.warning("careers_scrape: cascade exhausted for URL discovery, retrying via CLI")
        try:
            result, cost = call_claude(
                model=DEFAULT_MODEL_HAIKU,
                system=system,
                messages=messages,
                output_schema=_CAREERS_URL_SCHEMA,
                conn=conn,
                job_id=None,
                purpose="careers_scrape",
                config=config,
                max_tokens=256,
            )
            url_text = result.get("url", "").strip()
            if not url_text or url_text.lower() == "none":
                return None
            if url_text.startswith("/"):
                url_text = urljoin(homepage_url, url_text)
            return url_text if url_text.startswith("http") else None
        except Exception:
            logger.warning("careers_scrape: CLI retry also failed for URL discovery")
            return None
    except Exception as e:
        logger.debug("careers_scrape: URL discovery failed for '%s': %s", homepage_url, e)
        return None
```

**Step 4: Site 2 — `_extract_jobs_with_haiku()` (~line 216):**

Same pattern, same tier, same purpose. `max_tokens=1024`. Uses
`_CAREERS_JOBS_SCHEMA`. Result extraction changes to `result.get("jobs", [])`.

Follow the identical structure as site 1, but:
- Use `output_schema=_CAREERS_JOBS_SCHEMA`
- Use `max_tokens=1024`
- Cascade-exhausted fallback returns `[]` (not `None`)
- Generic exception returns `[]`
- After getting result: `jobs = result.get("jobs", [])` then filter+resolve as before

**Validation:**
```bash
uv run --active pytest tests/test_careers_scraper.py -q --tb=short
```
All existing tests must pass. They mock at the helper function level, so internal
dispatch changes should be transparent. If any tests assert on `result.get("text")`
patterns, update them to use the new schema keys.

**Commit:** `feat: migrate careers_scraper to provider cascade`

---

### 5.2 Migrate `enrichment_tiers.py` (2 call sites)

**File:** `job_finder/web/enrichment_tiers.py`  
**Priority:** HIGH — 6.2% of calls, critical for backfill path.

**Step 1: Add imports (line ~20 area):**
```python
from job_finder.web.model_provider import ProviderCascadeExhaustedError, call_model
```
Note: `call_claude` is already imported at line 20. Keep it for fallback path.

**Step 2: Add `_CLIClientStub` near top (after imports).**

**Step 3: Site 1 — `extract_with_haiku()` (~line 510):**

This function receives `search_text`, `job_row`, `conn`, and `config` as params.
`job_id = job_row.get("dedup_key")` is extracted at line 508.

The existing `try/except Exception` block (lines 535-537) returns `{}` on error.
Insert the dispatch pattern inside it:

```python
    try:
        use_dispatcher = bool(config.get("providers", {}).get("haiku"))

        if use_dispatcher:
            model_result = call_model(
                tier="haiku",
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
                conn=conn,
                config=config,
                output_schema=None,
                job_id=job_id,
                purpose="enrich_job",
                max_tokens=512,
                client=_CLI_CLIENT_STUB,
            )
            result = model_result.data
        else:
            result, _cost = call_claude(
                model=model,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
                output_schema=None,
                conn=conn,
                job_id=job_id,
                purpose="enrich_job",
                config=config,
                max_tokens=512,
            )

        # (existing result extraction at lines 522-531 stays unchanged)
        if isinstance(result, dict):
            enriched = {}
            for key, value in result.items():
                if value is not None and key in ("jd_full", "salary_min", "salary_max", "location"):
                    if key in ("salary_min", "salary_max") and isinstance(value, (int, float)):
                        enriched[key] = int(value)
                    elif isinstance(value, str) and value.strip():
                        enriched[key] = value
            return enriched

        return {}

    except ProviderCascadeExhaustedError:
        logger.warning("enrich_job: Haiku cascade exhausted for '%s', retrying via CLI", job_id)
        try:
            result, _cost = call_claude(
                model=model,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
                output_schema=None,
                conn=conn,
                job_id=job_id,
                purpose="enrich_job",
                config=config,
                max_tokens=512,
            )
            if isinstance(result, dict):
                enriched = {}
                for key, value in result.items():
                    if value is not None and key in ("jd_full", "salary_min", "salary_max", "location"):
                        if key in ("salary_min", "salary_max") and isinstance(value, (int, float)):
                            enriched[key] = int(value)
                        elif isinstance(value, str) and value.strip():
                            enriched[key] = value
                return enriched
            return {}
        except Exception:
            logger.warning("enrich_job: CLI retry also failed for '%s'", job_id)
            return {}
    except Exception as e:
        logger.debug("Haiku extraction failed: %s", e)
        return {}
```

**NOTE:** The result extraction logic is duplicated in the cascade fallback path.
To avoid this, extract it into a local helper function:

```python
def _parse_enrich_result(result):
    if isinstance(result, dict):
        enriched = {}
        for key, value in result.items():
            if value is not None and key in ("jd_full", "salary_min", "salary_max", "location"):
                if key in ("salary_min", "salary_max") and isinstance(value, (int, float)):
                    enriched[key] = int(value)
                elif isinstance(value, str) and value.strip():
                    enriched[key] = value
        return enriched
    return {}
```

Then both the primary and fallback paths call `return _parse_enrich_result(result)`.

**Step 4: Site 2 — `extract_with_sonnet()` (~line 323):**

Same pattern. tier=`"sonnet"`, purpose=`"enrich_job_sonnet"`, max_tokens=1024.
The `use_dispatcher` checks `config.get("providers", {}).get("sonnet")`.

The result extraction (lines 335-343) filters for `jd_full`, `salary_min`,
`salary_max` — same as haiku but without `location`. Use the same helper approach.

**Validation:**
```bash
uv run --active pytest tests/test_enrichment_tiers.py tests/test_data_enricher.py -q --tb=short
```

**Commit:** `feat: migrate enrichment_tiers to provider cascade`

---

### 5.3 Migrate `ai_career_navigator.py` (1 call site)

**File:** `job_finder/web/ai_career_navigator.py`  
**Priority:** MEDIUM — 32 calls, but one-time per company.

**Special considerations:**
- `call_claude` and `standalone_connection` are **lazy imports** inside the
  function body (lines 342-343). Move them to module level.
- `conn` is created locally via `standalone_connection(db_path)` (line 346).
- `output_schema` is already a dict (`recipe_schema`). No Phase 0 change needed.
- `job_id` is NOT passed (use `None`).

**Step 1: Move lazy imports to module level and add cascade imports:**
```python
from job_finder.web.claude_client import call_claude
from job_finder.web.db_helpers import standalone_connection
from job_finder.web.model_provider import ProviderCascadeExhaustedError, call_model
```

Check if any of these are already imported at module level to avoid duplicates.

**Step 2: Add `_CLIClientStub` at module level.**

**Step 3: Site — `discover_navigation_recipe()` (~line 376):**

The `with standalone_connection(db_path) as conn:` block provides the DB connection.
Replace the `call_claude()` invocation inside it:

```python
            use_dispatcher = bool(config.get("providers", {}).get("haiku"))

            try:
                if use_dispatcher:
                    model_result = call_model(
                        tier="haiku",
                        system=_DISCOVERY_SYSTEM,
                        messages=[{"role": "user", "content": user_message}],
                        conn=conn,
                        config=config,
                        output_schema=recipe_schema,
                        job_id=None,
                        purpose="ai_nav_discovery",
                        max_tokens=1024,
                        client=_CLI_CLIENT_STUB,
                    )
                    result = model_result.data
                else:
                    result, cost = call_claude(
                        model="claude-haiku-4-5",
                        system=_DISCOVERY_SYSTEM,
                        messages=[{"role": "user", "content": user_message}],
                        output_schema=recipe_schema,
                        conn=conn,
                        purpose="ai_nav_discovery",
                        config=config,
                        max_tokens=1024,
                    )
            except ProviderCascadeExhaustedError:
                logger.warning("ai_nav: cascade exhausted for %s, retrying via CLI", careers_url)
                try:
                    result, cost = call_claude(
                        model="claude-haiku-4-5",
                        system=_DISCOVERY_SYSTEM,
                        messages=[{"role": "user", "content": user_message}],
                        output_schema=recipe_schema,
                        conn=conn,
                        purpose="ai_nav_discovery",
                        config=config,
                        max_tokens=1024,
                    )
                except Exception:
                    logger.warning("ai_nav: CLI retry also failed for %s", careers_url)
                    return None
```

Replace the existing generic `except Exception` at line 387 — it now becomes the
outer handler after the cascade-specific one.

**Validation:**
```bash
uv run --active pytest tests/test_ai_career_navigator.py -q --tb=short
```

**Commit:** `feat: migrate ai_career_navigator to provider cascade`

---

### 5.4 Migrate `description_reformatter.py` (1 call site)

**File:** `job_finder/web/description_reformatter.py`  
**Priority:** LOWER — low volume, easy win.

**Special considerations:**
- `config` defaults to `None` (set to `{}` on line 80 if not provided).
- `conn` defaults to `None` (optional parameter).
- `call_model()` requires non-None `conn`. **MUST guard with conn-is-None check.**
- Returns original description on any failure (graceful degradation).
- Uses `_REFORMAT_SCHEMA` from Phase 0.

**Step 1: Add imports:**
```python
from job_finder.web.model_provider import ProviderCascadeExhaustedError, call_model
```

**Step 2: Add `_CLIClientStub` at module level.**

**Step 3: Site — `reformat_description()` (~line 94):**

**The conn-is-None guard MUST come first:**

```python
    # call_model() requires a non-None conn for cost recording.
    # When conn is None, skip cascade routing entirely.
    if conn is not None:
        use_dispatcher = bool(config.get("providers", {}).get("haiku"))
    else:
        use_dispatcher = False

    try:
        if use_dispatcher:
            model_result = call_model(
                tier="haiku",
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": description[:4000]}],
                conn=conn,
                config=config,
                output_schema=_REFORMAT_SCHEMA,
                job_id=None,
                purpose="description_reformat",
                max_tokens=2048,
                client=_CLI_CLIENT_STUB,
            )
            result = model_result.data
        else:
            result, _cost = call_claude(
                model=model,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": description[:4000]}],
                output_schema=_REFORMAT_SCHEMA,
                conn=conn,
                job_id=None,
                purpose="description_reformat",
                config=config,
                max_tokens=2048,
            )

        # Extract reformatted text (matches _REFORMAT_SCHEMA key)
        if isinstance(result, dict):
            reformatted = result.get("text", "")
        else:
            reformatted = str(result)

        if reformatted and reformatted.strip():
            return reformatted.strip()

        return description

    except ProviderCascadeExhaustedError:
        logger.warning("description_reformat: cascade exhausted, retrying via CLI")
        try:
            result, _cost = call_claude(
                model=model,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": description[:4000]}],
                output_schema=_REFORMAT_SCHEMA,
                conn=conn,
                job_id=None,
                purpose="description_reformat",
                config=config,
                max_tokens=2048,
            )
            if isinstance(result, dict):
                reformatted = result.get("text", "")
            else:
                reformatted = str(result)
            return reformatted.strip() if reformatted and reformatted.strip() else description
        except Exception:
            logger.warning("description_reformat: CLI retry also failed")
            return description
    except Exception as e:
        logger.warning("reformat_description failed (returning original): %s", e)
        return description
```

**Validation:**
```bash
uv run --active pytest tests/test_description_reformatter.py -q --tb=short
```

**Commit:** `feat: migrate description_reformatter to provider cascade`

---

## 6. Phase 2: Fix Backfill Config Plumbing

**File:** `job_finder/web/backfill_enrichment.py`

### 6.1 Fix `run_enrichment_pass()` config plumbing

**Problem:** `run_enrichment_pass()` calls `enrich_job()` with raw `config`
(line ~216). After Phase 1 migrates `enrichment_tiers.py` to respect
`config.providers.haiku/sonnet`, the backfill enrichment pass STILL won't use
Ollama unless we inject the offline providers.

**Fix — one-line change in `run_enrichment_pass()`:**

```python
# Before (line ~216):
result = enrich_job(
    job_row,
    serpapi_key=serpapi_key,
    conn=conn,
    config=config,
)

# After:
result = enrich_job(
    job_row,
    serpapi_key=serpapi_key,
    conn=conn,
    config=_offline_config(config),
)
```

### 6.2 Fix `_OFFLINE_PROVIDERS` haiku fallback key

**Problem:** The haiku entry uses `"fallback"` (singular) which triggers the
backward-compat path in `call_model()`, not the cascade path. This means
`ProviderCascadeExhaustedError` never fires for haiku tier, and the
`ProviderCascadeExhaustedError` catch blocks in the migrated code are dead code
for backfill runs.

**Fix — change `_OFFLINE_PROVIDERS` (line ~68):**

```python
# Before:
_OFFLINE_PROVIDERS: dict = {
    "haiku": {
        "provider": "ollama",
        "model": "qwen2.5:14b",
        "fallback": "anthropic",
    },
    "sonnet": {
        "provider": "ollama",
        "model": "qwen2.5:14b",
        "fallback_chain": [
            {"provider": "anthropic", "model": DEFAULT_MODEL_SONNET},
        ],
    },
}

# After:
_OFFLINE_PROVIDERS: dict = {
    "haiku": {
        "provider": "ollama",
        "model": "qwen2.5:14b",
        "fallback_chain": [
            {"provider": "anthropic", "model": DEFAULT_MODEL_HAIKU},
        ],
    },
    "sonnet": {
        "provider": "ollama",
        "model": "qwen2.5:14b",
        "fallback_chain": [
            {"provider": "anthropic", "model": DEFAULT_MODEL_SONNET},
        ],
    },
}
```

### 6.3 Update cost estimation (nice-to-have)

`estimate_and_confirm()` (lines 145-168) computes costs using Anthropic
`MODEL_PRICING`. After this fix, most backfill calls route through Ollama ($0).
The estimate will overstate costs. Consider adding a note:

```python
print("Note: Actual cost depends on how many jobs need AI tiers.")
print("      With Ollama configured, most calls are $0 (local inference).")
```

This is cosmetic — it doesn't affect correctness.

### Validation

```bash
uv run --active pytest tests/test_backfill_enrichment.py -q --tb=short
```

**Commit:** `fix: plumb _offline_config through enrichment pass + normalize fallback_chain`

---

## 7. Phase 3: Test Coverage

### Test philosophy

Every migrated call site needs tests for THREE paths:
1. **No-dispatcher path** (no `providers.haiku/sonnet` in config) — calls
   `call_claude()` directly.
2. **Dispatcher path** (providers configured) — calls `call_model()`.
3. **Cascade-exhausted fallback** — all cascade providers fail, falls back to
   `call_claude()` via CLI.

### 7.1 Shared Test Fixtures (add to `tests/conftest.py`)

```python
@pytest.fixture
def cascade_config_haiku():
    """Config with Ollama primary + Anthropic fallback for haiku tier."""
    return {
        "providers": {
            "haiku": {
                "provider": "ollama",
                "model": "qwen2.5:14b",
                "fallback_chain": [
                    {"provider": "anthropic", "model": "claude-haiku-4-5"},
                ],
            },
        },
    }


@pytest.fixture
def cascade_config_sonnet():
    """Config with Ollama primary + Anthropic fallback for sonnet tier."""
    return {
        "providers": {
            "sonnet": {
                "provider": "ollama",
                "model": "qwen2.5:14b",
                "fallback_chain": [
                    {"provider": "anthropic", "model": "claude-sonnet-4-6"},
                ],
            },
        },
    }
```

### 7.2 Mock Targets Per File

| File | Mock `call_model` at | Mock `call_claude` at |
|------|---------------------|-----------------------|
| `careers_scraper.py` | `job_finder.web.careers_scraper.call_model` | `job_finder.web.careers_scraper.call_claude` |
| `enrichment_tiers.py` | `job_finder.web.enrichment_tiers.call_model` | `job_finder.web.enrichment_tiers.call_claude` |
| `ai_career_navigator.py` | `job_finder.web.ai_career_navigator.call_model` | `job_finder.web.ai_career_navigator.call_claude` |
| `description_reformatter.py` | `job_finder.web.description_reformatter.call_model` | `job_finder.web.description_reformatter.call_claude` |

### 7.3 ModelResult Helper for Tests

Add to each test file (or to `conftest.py`):

```python
from job_finder.web.model_provider import ProviderCascadeExhaustedError, ModelResult

def _make_model_result(data, provider="ollama", cost_usd=0.0):
    return ModelResult(
        data=data, cost_usd=cost_usd, input_tokens=100, output_tokens=50,
        model="qwen2.5:14b", provider=provider, schema_valid=True,
    )
```

### 7.4 Required Tests Per Migrated File

#### tests/test_careers_scraper.py — 8 new tests

```
TestCareersScraperCascade:
  # Site 1: _find_careers_url_with_haiku
  test_find_url_uses_call_model_when_providers_configured
  test_find_url_uses_call_claude_when_no_providers
  test_find_url_cascade_exhausted_falls_back_to_cli
  test_find_url_cascade_and_cli_both_fail_returns_none

  # Site 2: _extract_jobs_with_haiku
  test_extract_jobs_uses_call_model_when_providers_configured
  test_extract_jobs_uses_call_claude_when_no_providers
  test_extract_jobs_cascade_exhausted_falls_back_to_cli
  test_extract_jobs_cascade_and_cli_both_fail_returns_empty_list
```

**Test 1 example (dispatcher path):**
```python
def test_find_url_uses_call_model_when_providers_configured(self, migrated_db, cascade_config_haiku):
    path, conn = migrated_db
    config = {**cascade_config_haiku, "db": {"path": path}}
    with patch("job_finder.web.careers_scraper.call_model") as mock_cm:
        mock_cm.return_value = _make_model_result({"url": "https://example.com/careers"})
        from job_finder.web.careers_scraper import _find_careers_url_with_haiku
        result = _find_careers_url_with_haiku(
            "https://example.com", "<html>careers link</html>", conn, config,
        )
        mock_cm.assert_called_once()
        assert mock_cm.call_args.kwargs["tier"] == "haiku"
        assert mock_cm.call_args.kwargs["purpose"] == "careers_scrape"
        assert result == "https://example.com/careers"
```

**Test 3 example (cascade exhausted):**
```python
def test_find_url_cascade_exhausted_falls_back_to_cli(self, migrated_db, cascade_config_haiku):
    path, conn = migrated_db
    config = {**cascade_config_haiku, "db": {"path": path}}
    with patch("job_finder.web.careers_scraper.call_model") as mock_cm, \
         patch("job_finder.web.careers_scraper.call_claude") as mock_cc:
        mock_cm.side_effect = ProviderCascadeExhaustedError("all exhausted")
        mock_cc.return_value = ({"url": "https://example.com/careers"}, 0.001)
        from job_finder.web.careers_scraper import _find_careers_url_with_haiku
        result = _find_careers_url_with_haiku(
            "https://example.com", "<html>careers</html>", conn, config,
        )
        mock_cm.assert_called_once()
        mock_cc.assert_called_once()
        assert result == "https://example.com/careers"
```

#### tests/test_enrichment_tiers.py — 8 new tests

Same 4-test pattern for each of `extract_with_haiku` and `extract_with_sonnet`.

Key difference: these use `output_schema=None` and the result is a dict with
domain-specific keys. Test data:
```python
# For extract_with_haiku:
_make_model_result({"jd_full": "Full job description", "salary_min": 120000})

# For extract_with_sonnet:
_make_model_result({"jd_full": "Full job description", "salary_min": 120000, "salary_max": 180000})
```

#### tests/test_ai_career_navigator.py — 4 new tests

Same 4-test pattern for `discover_navigation_recipe`.

**Special handling:** The function creates its own DB connection via
`standalone_connection(db_path)`. Tests must either:
- Mock `standalone_connection` to return a test connection, OR
- Provide a valid `db_path` in config pointing to the test DB

Test data:
```python
_make_model_result({
    "steps": [{"action": "navigate", "url": "https://example.com/careers"}],
    "extraction": {"method": "list"},
})
```

#### tests/test_description_reformatter.py — 5 new tests

4 cascade tests + 1 for the conn=None guard:

```
TestDescriptionReformatterCascade:
  test_reformat_uses_call_model_when_providers_configured
  test_reformat_uses_call_claude_when_no_providers
  test_reformat_cascade_exhausted_falls_back_to_cli
  test_reformat_cascade_and_cli_both_fail_returns_original
  test_reformat_conn_none_skips_dispatcher  # Verifies use_dispatcher=False when conn=None
```

**Test 5 example (conn=None guard):**
```python
def test_reformat_conn_none_skips_dispatcher(self, cascade_config_haiku):
    config = cascade_config_haiku
    with patch("job_finder.web.description_reformatter.call_model") as mock_cm, \
         patch("job_finder.web.description_reformatter.call_claude") as mock_cc:
        mock_cc.return_value = ({"text": "reformatted"}, 0.001)
        from job_finder.web.description_reformatter import reformat_description
        result = reformat_description("ugly text here | more text", conn=None, config=config)
        mock_cm.assert_not_called()  # Dispatcher was NOT used despite config
        mock_cc.assert_called_once()
```

#### tests/test_backfill_enrichment.py — 2 new tests

```
TestBackfillOfflineConfig:
  test_enrichment_pass_uses_offline_config
  test_offline_providers_use_fallback_chain_not_fallback
```

**Test 1 verifies `_offline_config` flows through:**
```python
def test_enrichment_pass_uses_offline_config(self, migrated_db):
    path, conn = migrated_db
    with patch.object(be_module, "enrich_job") as mock_enrich:
        mock_enrich.return_value = {}
        conn.execute(
            "INSERT INTO jobs (dedup_key, ...) VALUES (?...)",
            ("test|job|remote", ...),
        )
        conn.commit()
        be_module.run_enrichment_pass(conn, serpapi_key=None, config={})
        # Verify the config passed to enrich_job has providers.haiku set
        call_config = mock_enrich.call_args.kwargs["config"]
        assert "haiku" in call_config.get("providers", {})
        assert call_config["providers"]["haiku"]["provider"] == "ollama"
```

**Test 2 verifies consistent fallback_chain usage:**
```python
def test_offline_providers_use_fallback_chain_not_fallback(self):
    from job_finder.web.backfill_enrichment import _OFFLINE_PROVIDERS
    for tier, cfg in _OFFLINE_PROVIDERS.items():
        assert "fallback_chain" in cfg, f"{tier} should use fallback_chain, not fallback"
        assert "fallback" not in cfg, f"{tier} should not use singular fallback key"
```

### 7.5 Existing Cascade Test Gaps (haiku_scorer, sonnet_evaluator)

The haiku_scorer.py and sonnet_evaluator.py ALREADY have the dispatch pattern but
have NO cascade-specific tests. Add these:

#### tests/test_haiku_cascade.py (or append to test_scoring.py) — 4 tests

```
TestHaikuCascadeDispatch:
  test_haiku_uses_call_model_when_providers_configured
  test_haiku_uses_call_claude_when_no_providers
  test_haiku_cascade_exhausted_falls_back_to_cli
  test_haiku_cascade_and_cli_both_fail_returns_error_status
```

#### tests/test_sonnet_cascade.py (or append to test_scoring.py) — 4 tests

Same pattern for sonnet tier.

### 7.6 Total New Tests

| File | New Tests |
|------|-----------|
| `tests/test_careers_scraper.py` | 8 |
| `tests/test_enrichment_tiers.py` | 8 |
| `tests/test_ai_career_navigator.py` | 4 |
| `tests/test_description_reformatter.py` | 5 |
| `tests/test_backfill_enrichment.py` | 2 |
| `tests/test_haiku_cascade.py` | 4 |
| `tests/test_sonnet_cascade.py` | 4 |
| **Total** | **35** |

### 7.7 Full Test Suite Validation

After ALL test additions:

```bash
uv run --active pytest tests/ -q --tb=short
```

**REQUIREMENT: ALL tests must pass. Fix any failures, whether preexisting or
introduced by this migration. Zero tolerance for test failures at completion.**

If you encounter preexisting failures in unrelated test files, fix them. Document
what you fixed and why.

**Commit:** `test: cascade dispatch coverage for all migrated call sites`

---

## 8. Phase 4: E2E Audit Infrastructure

### 8.1 Add Provider Routing Log to model_provider.py

**File:** `job_finder/web/model_provider.py`

Add a structured log entry when a provider is selected and called successfully.
This must be placed immediately before the `return result` statement in the
cascade path (find the line where `result` is returned after schema validation
succeeds — approximately line 623).

```python
logger.info(
    "call_model ROUTED: tier=%s provider=%s model=%s purpose=%s job_id=%s",
    tier, result.provider, result.model, purpose, job_id,
)
```

Also add an INFO log at cascade entry (before the provider loop starts):

```python
logger.info(
    "call_model CASCADE: tier=%s chain=[%s] purpose=%s",
    tier,
    ", ".join(e.get("provider", "?") for e in fallback_chain) if fallback_chain else "primary-only",
    purpose,
)
```

**NOTE:** The existing `call_claude START: purpose=... model=... job_id=... tier=...`
log line in `claude_client.py` only captures calls going through Path B (direct
`call_claude()`). The new `call_model ROUTED` log captures Path A. Together they
provide complete coverage. Calls that go through both (cascade→Anthropic fallback
→call_claude) will appear in both logs — this is expected and provides full
traceability.

### 8.2 Cascade Bypass Audit Script

**File:** `scripts/audit_cascade_bypass.sh`

```bash
#!/usr/bin/env bash
# Audit: find call_claude() invocations that bypass the provider cascade.
# Expected: only call_claude inside cascade fallback blocks and infrastructure.
set -euo pipefail

echo "=== Direct call_claude() invocations (excluding infrastructure) ==="
grep -rn 'call_claude(' job_finder/ \
    --include='*.py' \
    | grep -v 'claude_client.py' \
    | grep -v 'anthropic_provider.py' \
    | grep -v 'model_provider.py' \
    | grep -v 'import.*call_claude' \
    | grep -v '# .*call_claude' \
    || echo "  (none found)"

echo ""
echo "=== Files importing call_claude but NOT call_model ==="
bypass_found=0
for f in $(grep -rl 'from.*claude_client.*import.*call_claude' job_finder/ --include='*.py' \
    | grep -v claude_client.py \
    | grep -v anthropic_provider.py \
    | grep -v model_provider.py); do
    if ! grep -q 'from.*model_provider.*import.*call_model' "$f"; then
        echo "  BYPASS: $f"
        bypass_found=1
    fi
done
if [ "$bypass_found" -eq 0 ]; then
    echo "  (none found — all files import call_model)"
fi

echo ""
echo "Done. 'BYPASS' files need migration or explicit exemption."
```

**Allowed exemptions** (files that legitimately call `call_claude` without cascade):
- `claude_client.py` — defines the function
- `model_provider.py` — cascade infrastructure
- `providers/anthropic_provider.py` — adapter layer

Any other file appearing in the BYPASS list is a regression.

### 8.3 Post-Migration Log Audit Commands

After deployment with Ollama configured, run:

```bash
# Count by provider in app.log
grep 'call_model ROUTED' logs/app.log | grep -o 'provider=[^ ]*' | sort | uniq -c | sort -rn

# Verify cascaded purposes appear in call_model ROUTED (not just call_claude START)
grep 'call_model ROUTED' logs/app.log | grep -o 'purpose=[^ ]*' | sort | uniq -c | sort -rn

# Check for any direct call_claude leaks for migrated purposes
grep 'call_claude START' logs/app.log | grep -E 'purpose=(careers_scrape|enrich_job|enrich_job_sonnet|ai_nav_discovery|description_reformat)' | head -5
# ^ Non-zero results mean the cascade was bypassed (missing providers config,
#   or cascade-exhausted fallback fired). Investigate if unexpected.
```

**Commit:** `feat: cascade routing audit log + bypass detection script`

---

## 9. Phase 5: Validation & Sign-off

### 9.1 Automated Validation Checklist

Run these commands sequentially. ALL must pass:

```bash
# 1. Full test suite — zero failures
uv run --active pytest tests/ -q --tb=short

# 2. Grep audit — no unexpected bypass sites
bash scripts/audit_cascade_bypass.sh

# 3. Import verification — all migrated files import call_model
for f in \
    job_finder/web/careers_scraper.py \
    job_finder/web/enrichment_tiers.py \
    job_finder/web/ai_career_navigator.py \
    job_finder/web/description_reformatter.py; do
    echo -n "$f: "
    grep -c 'from job_finder.web.model_provider import' "$f"
done
# Expected: each prints 1

# 4. Schema verification — freeform sites now have schemas
grep -n 'output_schema=None' \
    job_finder/web/careers_scraper.py \
    job_finder/web/description_reformatter.py
# Expected: zero results (all replaced with named schemas)

# 5. Fallback key verification — no singular "fallback" in _OFFLINE_PROVIDERS
grep -n '"fallback"' job_finder/web/backfill_enrichment.py | grep -v 'fallback_chain'
# Expected: zero results
```

### 9.2 Manual Smoke Test

Start the app with Ollama running and providers configured:

```yaml
# config.yaml — add this section temporarily for testing
providers:
  haiku:
    provider: ollama
    model: qwen2.5:14b
    fallback_chain:
      - provider: anthropic
        model: claude-haiku-4-5
  sonnet:
    provider: ollama
    model: qwen2.5:14b
    fallback_chain:
      - provider: anthropic
        model: claude-sonnet-4-6
```

Then:

1. Start app: `uv run python run.py`
2. Trigger a careers scan or scoring run via the web UI
3. Check `logs/app.log` for `call_model ROUTED: ... provider=ollama`
4. Verify cost is $0.00 for Ollama calls in the `scoring_costs` table:
   ```sql
   SELECT provider, purpose, cost_usd FROM scoring_costs ORDER BY id DESC LIMIT 20;
   ```
5. Kill Ollama (`taskkill /im ollama.exe /f`) and trigger another run.
   Verify it falls back to Anthropic (check for `provider=anthropic` in logs).

### 9.3 Sign-off Criteria

- [ ] Phase 0: Schemas defined for 3 freeform text call sites
- [ ] Phase 1: All 6 bypass call sites migrated to `use_dispatcher` pattern
- [ ] Phase 2a: `backfill_enrichment.run_enrichment_pass()` passes `_offline_config(config)`
- [ ] Phase 2b: `_OFFLINE_PROVIDERS` uses `fallback_chain` (not `fallback`) for both tiers
- [ ] Phase 3: 35+ new tests added
- [ ] Phase 3: Full test suite passes with zero failures (including any preexisting fixes)
- [ ] Phase 4: `call_model ROUTED` log line present in `model_provider.py`
- [ ] Phase 4: `audit_cascade_bypass.sh` reports zero unexpected BYPASS files
- [ ] Phase 5: Smoke test shows Ollama routing when providers configured
- [ ] All changes committed with atomic, descriptive commits

---

## 10. Adversarial Review Log

This plan was adversarially reviewed after initial drafting. Four blockers were
found and resolved:

### BLOCKER 1: output_schema=None Silent Data Loss (RESOLVED)

**Problem:** `call_claude()` wraps freeform text in `{"text": raw}` when JSON
parsing fails. `OllamaProvider` always forces `"format": "json"`, so the model
invents arbitrary JSON keys. Downstream code doing `result.get("text", "")` gets
empty strings from Ollama — silent data loss for 3 of 6 call sites.

**Resolution:** Added Phase 0 (section 4) which defines explicit `output_schema`
dicts for all freeform text call sites. Both providers now return the same dict
shape via native structured output enforcement.

**Affected sites:** `careers_scraper.py` (both), `description_reformatter.py`.
Not affected: `enrichment_tiers.py` (prompt already instructs JSON with known
keys), `ai_career_navigator.py` (already has schema).

### BLOCKER 2: conn=None in description_reformatter.py (RESOLVED)

**Problem:** `call_model()` requires non-None `conn` for cost recording. The
`reformat_description()` function accepts `conn=None`.

**Resolution:** Added explicit conn-is-None guard in Phase 1.4 (section 5.4):
```python
if conn is not None:
    use_dispatcher = bool(config.get("providers", {}).get("haiku"))
else:
    use_dispatcher = False
```

### BLOCKER 3: _OFFLINE_PROVIDERS haiku uses "fallback" not "fallback_chain" (RESOLVED)

**Problem:** The singular `"fallback"` key triggers the backward-compat path in
`call_model()`, not the cascade path. `ProviderCascadeExhaustedError` never fires.

**Resolution:** Added Phase 2.2 (section 6.2) to change haiku entry from
`"fallback": "anthropic"` to `"fallback_chain": [{"provider": "anthropic", "model": DEFAULT_MODEL_HAIKU}]`.

### BLOCKER 4: Test template missing imports and function access (RESOLVED)

**Problem:** Test code template called private functions (`_find_careers_url_with_haiku`)
without import statements, and didn't account for lazy imports inside function
bodies.

**Resolution:** Phase 1.1 (section 5.1) now explicitly says to move local imports
to module level and remove duplicates. Test examples include import statements.

### WARNINGS ACKNOWLEDGED

- **WARNING 6 (cost estimation):** `estimate_and_confirm()` will overstate costs
  when Ollama handles most calls. Added note in Phase 2.3. Cosmetic only.
- **WARNING 7 (test coverage is greenfield):** Existing tests mock at the helper
  function level, not at `call_claude()`. New tests are truly greenfield. Noted.

---

## 11. Anti-Regression Checklist

### For future developers adding new AI call sites:

1. **NEVER call `call_claude()` directly** from feature code. Always use
   `call_model()` with the `use_dispatcher` pattern.

2. **ALWAYS define an `output_schema`** for any call site that will go through the
   cascade. Using `output_schema=None` with text-returning prompts causes silent
   data loss when routed through Ollama (`"format": "json"` forces arbitrary JSON).

3. **The only files allowed to call `call_claude()` directly:**
   - `claude_client.py` (it defines the function)
   - `model_provider.py` / `providers/anthropic_provider.py` (cascade infrastructure)
   - Inside `ProviderCascadeExhaustedError` fallback blocks (last-resort CLI path)

4. **Run `bash scripts/audit_cascade_bypass.sh`** before merging any PR that
   adds AI call sites. If it reports a BYPASS, migrate or document the exemption.

5. **Test all three paths** for any new call site:
   - No-dispatcher (config has no providers.TIER)
   - Dispatcher (config has providers.TIER with cascade)
   - Cascade-exhausted (ProviderCascadeExhaustedError → CLI fallback)

6. **If `conn` can be None**, guard with `use_dispatcher = False` when `conn is None`.

---

## Execution Order Summary

| Step | Phase | Files Changed | Commit Message |
|------|-------|---------------|----------------|
| 0 | Prerequisite | (commit existing uncommitted changes) | `feat: provider cascade for haiku/sonnet scorers + liveness gate reposition` |
| 1 | Phase 0 | `careers_scraper.py`, `description_reformatter.py` | `refactor: add output schemas for freeform Haiku call sites` |
| 2 | Phase 1.1 | `careers_scraper.py` | `feat: migrate careers_scraper to provider cascade` |
| 3 | Phase 1.2 | `enrichment_tiers.py` | `feat: migrate enrichment_tiers to provider cascade` |
| 4 | Phase 1.3 | `ai_career_navigator.py` | `feat: migrate ai_career_navigator to provider cascade` |
| 5 | Phase 1.4 | `description_reformatter.py` | `feat: migrate description_reformatter to provider cascade` |
| 6 | Phase 2 | `backfill_enrichment.py` | `fix: plumb _offline_config through enrichment pass + normalize fallback_chain` |
| 7 | Phase 3 | `tests/conftest.py`, `tests/test_*.py` (7 files) | `test: cascade dispatch coverage for all migrated call sites` |
| 8 | Phase 4 | `model_provider.py`, `scripts/audit_cascade_bypass.sh` | `feat: cascade routing audit log + bypass detection script` |
| 9 | Phase 5 | (none — validation only) | — |

**Run tests after EVERY commit. Fix failures immediately, whether preexisting or new.**
