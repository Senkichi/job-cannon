# Enrichment Pipeline Fix Plan

## Problem Statement

The enrichment pipeline has a structural deficiency: it burns expensive SerpAPI credits on jobs that could be enriched for free, and the agentic recovery enricher only achieves 18% success rate (9/50). The DDG tier uses the Instant Answer API (returns Wikipedia summaries, not search results), making it effectively useless. Meanwhile, 716 jobs sit at NULL tier with 651 having no jd_full.

SerpAPI credits exhaust before month-end, making it unreliable as a fallback.

## Root Causes

1. **DDG tier is a no-op**: `search_duckduckgo()` calls the DuckDuckGo Instant Answer API (`api.duckduckgo.com`), which returns encyclopedia snippets — NOT web search results. It will never find a job description. The `ddgs` library (already installed for the agentic enricher) provides real web search via `ddgs.text()`.

2. **No URL-based fetch from DDG results**: Even if DDG returned URLs, the tier doesn't try to fetch them. It just passes text fragments to Haiku for extraction.

3. **SerpAPI used as first real search**: Because DDG is useless, SerpAPI is the first tier that actually searches the web for job descriptions. This burns paid credits on every job that the free tier can't resolve via direct URL fetch.

4. **Agentic enricher wastes attempts on LinkedIn URLs**: LinkedIn is not in BLOCKED_DOMAINS (correct for the free tier which has `fetch_linkedin_jd`), but the agentic enricher's `_rank_urls()` doesn't filter them either. Playwright fetches hit the auth wall every time, wasting one of 6 `_MAX_FETCH_ATTEMPTS` slots.

5. **Agentic enricher doesn't use `fetch_linkedin_jd`**: The specialized LinkedIn extractor exists but isn't called from the agentic path.

6. **`agentic_exhausted` is permanent death**: No TTL, no reset, no recovery path.

7. **No failure categorization**: All failures log at DEBUG level. Operators see "NOT FOUND" but never why.

## Architecture

### Changes to Existing Files

#### 1. `job_finder/web/enrichment_tiers.py` — Upgrade DDG search

**What**: Replace the DuckDuckGo Instant Answer API call with real web search using the `ddgs` library, and add URL fetching from search results.

**Why**: The Instant Answer API returns encyclopedia content. The `ddgs.text()` method returns actual web search results with URLs, like the agentic enricher already uses.

**Implementation**:

a) Replace `search_duckduckgo()` with `search_ddg_web()` that:
   - Takes `title`, `company` as args (not a pre-composed query string)
   - Generates 2 search queries: `"{company}" "{title}" job description` and `{company} careers {title}`
   - Calls `ddgs.text()` with `max_results=5` per query
   - Filters results through `is_blocked_domain()` (reuses domain_policy)
   - Sorts results by `domain_priority()` (ATS platforms first)
   - Returns a dict with keys:
     - `"ddg_urls"`: list[str] of discovered URLs (up to 8)
     - `"ddg_snippet"`: str concatenation of result body text (for Haiku extraction fallback)

b) Add `fetch_ddg_jds()` function that:
   - Takes a list of URLs from DDG search results
   - For each URL (up to 4 attempts):
     - If `"linkedin.com/jobs/"` in URL: call `fetch_linkedin_jd(url)`
     - Elif `is_blocked_domain(url)`: skip
     - Else: call `fetch_direct_jd(url)`
   - Returns the first successful JD text (>= 200 chars, passes `is_chrome_or_login_page` check), or None
   - Also returns the successful URL for source_urls persistence

c) Keep old `search_duckduckgo()` function renamed to `_search_ddg_instant()` with a deprecation log, so existing callers don't break during transition.

**Key design decisions**:
- Use `ddgs` library (already in requirements for agentic enricher) not raw HTTP
- 1.0s delay between the 2 DDG queries to avoid rate limits (less aggressive than agentic enricher's 1.5s because fewer queries)
- Re-use `fetch_direct_jd()` and `fetch_linkedin_jd()` — no new fetch code
- Re-use `is_blocked_domain()` and `domain_priority()` from domain_policy

#### 2. `job_finder/web/data_enricher.py` — Wire upgraded DDG into tier pipeline

**What**: Update DDG tier (Tier 1) to use the new `search_ddg_web()` and `fetch_ddg_jds()`, and add SerpAPI conservation logic.

**Why**: DDG tier should actually find job descriptions. SerpAPI should only fire for high-value jobs when DDG fails.

**Implementation**:

a) Replace DDG tier block (lines ~237-249) with:
   ```python
   if start_idx <= TIER_ORDER.index("ddg"):
       try:
           ddg_result = search_ddg_web(title, company)
           
           # Sub-tier A: Try fetching JDs from DDG URLs
           if ddg_result.get("ddg_urls"):
               jd_text, source_url = fetch_ddg_jds(ddg_result["ddg_urls"])
               if jd_text:
                   fragments["url_jd"] = jd_text
                   # Persist discovered URL to source_urls
                   if conn is not None and job_row.get("dedup_key") and source_url:
                       _merge_apply_urls(conn, job_row["dedup_key"], [source_url])
           
           # Sub-tier B: Save snippets for Haiku extraction
           if ddg_result.get("ddg_snippet"):
               fragments["ddg"] = ddg_result["ddg_snippet"]
           
           # If DDG URL fetch found a real JD, resolve and return
           enriched = _resolve_from_fragments(fragments, missing, job_row)
           if enriched and not is_stub_jd(enriched.get("jd_full"), title, company):
               _persist(conn, job_row, enriched, "ddg")
               return enriched
       except Exception as e:
           logger.debug("DDG tier failed for '%s': %s", title, e)
   ```

b) Add SerpAPI conservation gate before the SerpAPI tier:
   ```python
   # SerpAPI conservation: only use paid credits for jobs with haiku_score >= 40
   # or jobs that have never been scored (haiku_score IS NULL).
   haiku_score = job_row.get("haiku_score")
   serpapi_worth_it = haiku_score is None or haiku_score >= 40
   
   if start_idx <= TIER_ORDER.index("serpapi") and serpapi_key and jd_still_missing and serpapi_worth_it:
       ...existing serpapi code...
   ```

c) When SerpAPI is skipped due to conservation gate, persist at "ddg" tier (not advance to exhausted) so the job can still reach Sonnet via Haiku extraction of whatever fragments exist.

#### 3. `job_finder/web/agentic_enricher.py` — Fix LinkedIn handling and observability

**What**: Route LinkedIn URLs through `fetch_linkedin_jd()` instead of generic Playwright, add failure reason tracking, and filter LinkedIn from DDG search results.

**Implementation**:

a) In `_rank_urls()`, add LinkedIn filtering for the agentic path:
   - Add a new parameter `filter_linkedin: bool = False` (default preserves backward compat)
   - When True, skip URLs containing `linkedin.com/jobs/` (agentic enricher doesn't have the specialized extractor wired up)
   
   Actually, better approach: **wire up `fetch_linkedin_jd` in `_fetch_page_text`**. Before trying Playwright for a LinkedIn URL, try the lightweight BeautifulSoup extractor first:

   ```python
   def _fetch_page_text(page, url: str, timeout_ms: int = 15000) -> Optional[str]:
       # LinkedIn shortcut: try lightweight extractor first (no Playwright needed)
       if "linkedin.com/jobs/" in url:
           from job_finder.web.enrichment_tiers import fetch_linkedin_jd
           li_text = fetch_linkedin_jd(url)
           if li_text and len(li_text) >= 300:
               return li_text[:_MAX_JD_CHARS * 2]
           # Fall through to Playwright if LinkedIn extractor fails
       
       # ...existing Playwright code...
   ```

b) Add failure reason tracking to `enrich_single_job()`:
   - Track failure categories in a simple counter dict
   - At the end of the URL loop, log at INFO level:
   ```python
   logger.info(
       "Agentic: '%s' @ '%s' — urls=%d, fetched=%d, company_mismatch=%d, "
       "low_confidence=%d, auth_wall=%d",
       title[:40], company[:20], len(all_urls), fetch_ok, company_miss, low_conf, auth_walls
   )
   ```

c) Lower the company-name heuristic threshold: require ANY 1 of the meaningful tokens to appear (already the case), but add a bypass when company has <= 2 meaningful tokens AND the page has > 2000 chars (long pages are more likely to be real JDs, worth the Ollama validation cost):
   ```python
   if not any(tok in text_lower for tok in company_tokens):
       # Bypass for long pages with short company names — worth validating
       if len(company_tokens) <= 2 and len(text) > 2000:
           logger.debug("Agentic: bypassing company check for long page %s", url[:60])
       else:
           logger.debug("Agentic: skipping %s (company name not found)", url[:60])
           continue
   ```

#### 4. `job_finder/web/data_enricher.py` — Add `agentic_exhausted` TTL reset

**What**: In `run_enrichment_backfill()`, reset `agentic_exhausted` jobs older than 7 days back to `exhausted` for re-processing by the agentic enricher.

**Implementation**:

In the reset section of `run_enrichment_backfill()`, add:
```python
# Reset agentic_exhausted jobs older than 7 days (TTL recovery).
# Companies repost jobs, careers pages update — worth retrying.
aged_reset = conn.execute(
    """UPDATE jobs SET enrichment_tier = 'exhausted'
       WHERE enrichment_tier = 'agentic_exhausted'
         AND jd_full IS NULL
         AND last_seen < datetime('now', '-7 days')""",
).rowcount
conn.commit()
if aged_reset:
    logger.info("Reset %d agentic_exhausted jobs (>7 days old) for retry", aged_reset)
    result["reset"] += aged_reset
```

#### 5. `job_finder/web/enrichment_tiers.py` — Add enrichment stats helper

**What**: Add a lightweight `EnrichmentStats` counter class for tracking failure reasons across the pipeline.

**Implementation**:

```python
class EnrichmentStats:
    """Lightweight counter for enrichment failure categorization.
    
    Used by the agentic enricher and potentially the standard pipeline
    to track why jobs fail enrichment at INFO level.
    """
    __slots__ = ("_counts",)
    
    def __init__(self):
        self._counts: dict[str, int] = {}
    
    def record(self, reason: str) -> None:
        self._counts[reason] = self._counts.get(reason, 0) + 1
    
    def summary(self) -> dict[str, int]:
        return dict(self._counts)
    
    def __str__(self) -> str:
        return ", ".join(f"{k}={v}" for k, v in sorted(self._counts.items()))
```

Add batch-level stats logging at the end of `run_agentic_backfill()`:
```python
logger.info("Agentic enrichment stats: %s", stats)
```

### Changes to Test Files

#### 6. `tests/test_enrichment_tiers.py` — Tests for upgraded DDG

- Test `search_ddg_web()` with mocked `ddgs.text()` returning realistic results
- Test URL filtering (blocked domains removed, sorted by priority)
- Test `fetch_ddg_jds()` with mocked `fetch_direct_jd` / `fetch_linkedin_jd`
- Test that LinkedIn URLs route to `fetch_linkedin_jd`, not `fetch_direct_jd`
- Test empty results, all-blocked results, auth-wall-only results

#### 7. `tests/test_data_enricher.py` — Tests for DDG tier integration and SerpAPI conservation

- Test that DDG tier now calls `search_ddg_web` (not old `search_duckduckgo`)
- Test DDG URL fetch success → job returned at "ddg" tier (never reaches SerpAPI)
- Test SerpAPI conservation: job with haiku_score=30 skips SerpAPI
- Test SerpAPI conservation: job with haiku_score=50 uses SerpAPI
- Test SerpAPI conservation: job with haiku_score=None uses SerpAPI
- Test that DDG failure falls through to Haiku correctly

#### 8. `tests/test_agentic_enricher.py` — Tests for LinkedIn routing and observability

- Test that LinkedIn URLs try `fetch_linkedin_jd` before Playwright
- Test that `fetch_linkedin_jd` success skips Playwright entirely
- Test company-name bypass for long pages with short company names
- Test failure stats are populated and logged

#### 9. `tests/test_data_enricher.py` — Test agentic_exhausted TTL reset

- Test that `run_enrichment_backfill` resets `agentic_exhausted` jobs older than 7 days
- Test that recent `agentic_exhausted` jobs are NOT reset

## Implementation Order

1. `enrichment_tiers.py` — `search_ddg_web()` + `fetch_ddg_jds()` + `EnrichmentStats` (foundation)
2. `data_enricher.py` — Wire DDG upgrade + SerpAPI conservation + TTL reset (integration)
3. `agentic_enricher.py` — LinkedIn routing + observability (improvement)
4. Tests for all three files
5. Run full test suite to verify no regressions

## Expected Impact

| Metric | Before | Expected After |
|--------|--------|----------------|
| DDG tier effectiveness | ~0% (Instant Answer API) | ~30-40% (real web search + URL fetch) |
| SerpAPI calls per backfill | All jobs reaching SerpAPI tier | Only haiku_score >= 40 jobs |
| Agentic enricher success rate | 18% (9/50) | ~30-40% (LinkedIn routing + relaxed heuristic) |
| `agentic_exhausted` recovery | Never | 30-day TTL reset |
| Failure observability | DEBUG only | INFO with categorized stats |

## Files Modified

- `job_finder/web/enrichment_tiers.py` — New `search_ddg_web()`, `fetch_ddg_jds()`, `EnrichmentStats`
- `job_finder/web/data_enricher.py` — DDG tier rewrite, SerpAPI conservation, TTL reset
- `job_finder/web/agentic_enricher.py` — LinkedIn routing, observability, relaxed heuristic
- `tests/test_enrichment_tiers.py` — DDG search/fetch tests
- `tests/test_data_enricher.py` — DDG integration, SerpAPI conservation, TTL tests
- `tests/test_agentic_enricher.py` — LinkedIn routing, observability tests

## What This Does NOT Change

- No new dependencies (ddgs already installed)
- No schema migrations
- No changes to parsers, sources, or ingestion
- No changes to scoring (Haiku/Sonnet/Opus)
- No changes to the scheduler
- No changes to domain_policy.py (already correct)
