# CODE CRITIQUE — Glassdoor Enrichment Recovery (Iteration 3 / Final)

**Reviewer persona:** Hostile NASA flight-software reviewer. Every defect is a potential mission loss event.  
**Review date:** 2026-04-01  
**Test baseline:** 169/169 task-specific tests pass (post Iteration 2 fixes).  
**Scope:** All files touched by the Glassdoor enrichment recovery implementation.

---

## DEFECT 015 — `enrich_single_job` company-token heuristic silently passes all URLs when `company_tokens` resolves to empty list

**File:** [`job_finder/web/agentic_enricher.py`](job_finder/web/agentic_enricher.py:383)  
**Severity:** MEDIUM — silent permissiveness allows garbage pages through to Ollama  
**Status:** OPEN

```python
company_tokens = [
    tok for tok in re.split(r"[\s\-&,\.]+", company.lower())
    if tok and tok not in _COMPANY_STOP_WORDS and len(tok) > 1
]
if company_tokens and not any(tok in text_lower for tok in company_tokens):
    logger.debug("Agentic: skipping %s (company name not found)", url[:60])
    continue
```

The outer `if company_tokens and …` guard means: when `company_tokens` is empty (every token is a stop-word or single character), **the heuristic is completely bypassed and the page goes straight to Ollama**. A company name like `"AI"` → `company_tokens = []` (single-character filtered out by `len(tok) > 1`). An empty company string → same bypass. The NASA rule: a safety guard must fail **closed** (reject), not **open** (permit). When the heuristic cannot operate, the correct response is to skip the URL (or at minimum log a WARNING that the guard was bypassed), not silently pass it.

**Fix:** log a DEBUG when `company_tokens` is empty and `continue` (skip the heuristic URL, let Ollama see the next candidate that we CAN validate) — or, remove the outer `if company_tokens` guard and always apply the check, falling back to skipping only when the token list is empty.

---

## DEFECT 016 — `apply_urls` DB commit executes even when `TransientEnrichmentError` is raised mid-call

**File:** [`job_finder/web/data_enricher.py`](job_finder/web/data_enricher.py:317)  
**Severity:** MEDIUM — source_urls written even when serpapi call is partially failed  
**Status:** OPEN

The `apply_urls` merge and `conn.commit()` now run **before** the `if serpapi_result is not None` check (as fixed by DEFECT 010). However, `apply_urls` is initialized to `[]` before the `search_serpapi()` call. The `TransientEnrichmentError` is raised **inside** `search_serpapi()`, so it propagates out before `apply_urls` is ever populated from the tuple return — meaning `apply_urls` stays `[]` in that error path and the commit block gate `if apply_urls` correctly blocks it.

**However**: the structural concern is that `conn.commit()` inside the `apply_urls` merge block at line 354 commits **before** the main `serpapi_result` enrichment path can do its own `_persist()` + commit. If `_persist()` then raises, the `source_urls` write is permanently committed to the DB while the main enrichment fields are not — leaving an inconsistent record (source_urls updated, enrichment_tier not advanced). The two writes should be part of the same transaction or the `source_urls` commit must happen after `_persist()` succeeds.

---

## DEFECT 017 — `_fetch_page_text` timeout in `agentic_enricher.py` swallows `TimeoutError` but does not log the URL

**File:** [`job_finder/web/agentic_enricher.py`](job_finder/web/agentic_enricher.py:162)  
**Severity:** LOW — silent failure, no operator visibility  
**Status:** OPEN

```python
except Exception:
    return None
```

`_fetch_page_text` catches all exceptions and returns `None` with no log at all. When Playwright times out or raises a network error, the operator has no way to know _which_ URL timed out or _how often_ this is happening across the batch. The NASA rule: silent exception swallowing is mission-critical avoidance. Every caught exception at the edge of an external call must be logged at DEBUG or INFO level with enough context (URL, exception type) to diagnose field failures.

**Fix:** `logger.debug("_fetch_page_text failed for %s: %s", url[:80], type(exc).__name__)` before returning `None`.

---

## DEFECT 018 — `run_agentic_backfill` summary log divides with `total or 1` guard but no corresponding summary is emitted on early exit (zero-rows path)

**File:** [`job_finder/web/agentic_enricher.py`](job_finder/web/agentic_enricher.py:462)  
**Severity:** LOW — observability gap  
**Status:** OPEN

The DEFECT 008 fix correctly added `total or 1` to the final summary log. However, the early-exit path at line 462:

```python
if not rows:
    logger.info("Agentic backfill: no exhausted jobs to enrich.")
    return 0
```

…emits a different message format than the final summary. Operators monitoring log patterns with a regex for `"Agentic enrichment complete"` will see **no output** for the zero-rows run, creating a false gap in the operational log. Both exit paths should emit the same structured summary line so monitoring rules have a single pattern to match.

---

## DEFECT 019 — `_generate_queries` fallback path uses `_fallback_queries()` but `_fallback_queries()` returns hardcoded strings with no company name for exotic characters

**File:** [`job_finder/web/agentic_enricher.py`](job_finder/web/agentic_enricher.py:258)  
**Severity:** LOW — edge-case degraded quality, no crash  
**Status:** OPEN

```python
def _fallback_queries(title: str, company: str) -> list[str]:
    ...
    return [
        f"{title} {company} job description",
        f"{company} careers {title}",
    ]
```

This function is not a defect in isolation. The defect is that `_generate_queries()` uses `_fallback_queries()` on **any** exception from OllamaProvider — including cases where Ollama is temporarily overloaded (HTTP 503), not just cases where the model returned malformed JSON. A 503 from Ollama is a transient error that should be retried or escalated, not silently degraded to fallback queries. The operator has no visibility that the Ollama call failed vs. the response was parseable — both paths look identical in the enrichment output.

**Fix:** log at WARNING when `_fallback_queries()` is used due to a non-parse exception (provider error vs. JSON error should be distinguished).

---

## DEFECT 020 — `domain_policy.py` `is_blocked_domain()` uses `in url.lower()` substring match — a URL like `https://acme.com/jobs/glassdoor-review` is incorrectly blocked

**File:** [`job_finder/web/domain_policy.py`](job_finder/web/domain_policy.py:65)  
**Severity:** MEDIUM — valid ATS URLs silently filtered out, silent data loss  
**Status:** OPEN

```python
def is_blocked_domain(url: str) -> bool:
    url_lower = url.lower()
    return any(domain in url_lower for domain in BLOCKED_DOMAINS)
```

`"glassdoor" in "https://acme.com/jobs/glassdoor-review-embed"` → `True`. A careers page that happens to mention "glassdoor" or "indeed" in its path or query string would be incorrectly blocked. The check must be against the **hostname only**, not the full URL string.

**Fix:**
```python
from urllib.parse import urlparse
def is_blocked_domain(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return False
    return any(domain in host.lower() for domain in BLOCKED_DOMAINS)
```

This is a standard URL parsing pattern with no additional dependencies (stdlib only, preserving the zero-import constraint).

---

## Summary

| # | File | Severity | Status |
|---|------|----------|--------|
| DEFECT 015 | `agentic_enricher.py:383` — company token heuristic bypassed (open-fail) when all tokens are stop-words | MEDIUM | OPEN |
| DEFECT 016 | `data_enricher.py:354` — `source_urls` commit before `_persist()` risks partial transaction on exception | MEDIUM | OPEN |
| DEFECT 017 | `agentic_enricher.py:162` — `_fetch_page_text` swallows all exceptions silently, no URL logged | LOW | OPEN |
| DEFECT 018 | `agentic_enricher.py:462` — zero-rows early exit emits different log format than normal exit (monitoring gap) | LOW | OPEN |
| DEFECT 019 | `agentic_enricher.py:258` — `_fallback_queries()` used on transient Ollama errors without WARNING | LOW | OPEN |
| DEFECT 020 | `domain_policy.py:65` — `is_blocked_domain()` uses substring match on full URL, not hostname only | MEDIUM | OPEN |
