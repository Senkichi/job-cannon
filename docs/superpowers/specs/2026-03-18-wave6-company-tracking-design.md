# Wave 6: Company Tracking — Homepage Discovery & Careers Scraping

## Summary

The company tracking infrastructure exists (329 companies, ATS scanning, careers scraper) but has a critical data gap: only 1 company has a `homepage_url`. Without homepages, the careers page scraping can't function. This wave fills three gaps:

1. Auto-discover company homepages (URL extraction + free web search)
2. Navigate company websites to find and scrape careers pages with rich detail
3. Verify the auto-add pipeline works for new job ingestion

## Current State

| Component | Status |
|-----------|--------|
| `companies` table | Built. 329 companies, 99 ATS hits, 230 misses, 0 pending |
| ATS probing (`probe_ats_slugs`) | Built. Scheduled. Lever/Greenhouse/Ashby |
| ATS scanning (`run_ats_scan`) | Built. Scheduled. ~180 jobs discovered |
| Careers scraper (`careers_scraper.py`) | Built. `find_careers_url` + `scrape_careers_page` exist |
| HTML fallback in `run_ats_scan` | Built. Scans miss-status companies with homepage_url |
| Companies UI (`/companies`) | Built. Table with expand, scan history, manual retry |
| Homepage auto-discovery | **Missing.** Only 1/329 companies has homepage_url |
| Rich JD extraction from careers pages | **Partial.** Scraper extracts titles but not full descriptions |

## Gap A: Homepage Auto-Discovery

### Approach

Two-tier homepage lookup, run for companies where `homepage_url IS NULL`:

**Tier 1: URL extraction from existing data**
- For ATS-hit companies: derive homepage from ATS slug patterns
  - Ashby: slug is often the company domain prefix (e.g., `ramp` → `ramp.com`)
  - Greenhouse/Lever: slug alone doesn't reveal the homepage (the hosted URLs point back to the ATS domain). Fall through to Tier 2 for these.
- General heuristic: try `https://{slug}.com` with a HEAD request. If it resolves to a real company site (not a parked domain), use it.
- For non-ATS companies: extract domain from `source_urls` in the jobs table (LinkedIn/Glassdoor URLs don't help, but SerpAPI `apply_options` URLs often point to company domains)

**Tier 2: Free web search fallback**
- Use DuckDuckGo HTML search (NOT the Instant Answer API — the IA API's `AbstractURL` typically returns Wikipedia, not company sites)
- Query: `"{company_name} official website"`
- Parse the HTML search results page for the first organic result URL
- Alternatively, try DuckDuckGo's `?format=json` with the `Redirect` field or `Results[0].FirstURL` — test which returns actual company domains
- Validate: HEAD request on the discovered URL, follow redirects, confirm 200 and the response is an HTML page (not a login wall or error)
- Rate limit: 1s delay between companies, batch size cap of 50 per run (DDG rate-limits aggressively)

### New function

**File:** `job_finder/web/ats_scanner.py` (or new `job_finder/web/homepage_discoverer.py` if scope warrants)

```python
def discover_homepage(company_name: str, ats_platform: str, ats_slug: str, source_urls: list[str]) -> Optional[str]:
    """Auto-discover company homepage URL. Returns URL string or None."""
```

### Scheduler integration

Add homepage discovery to the existing `probe_ats_slugs` scheduler job — after probing, run homepage discovery for companies with `homepage_url IS NULL`. Or add as a separate lightweight scheduled job.

### Backfill

One-time: run homepage discovery for all 328 companies missing homepages. Can be triggered manually from `/companies` page or run as a management command.

## Gap B: Careers Page Navigation + Rich Extraction

### Current state of `careers_scraper.py`

The existing `find_careers_url(homepage_url)` fetches the homepage and looks for `/careers`, `/jobs`, etc. links. The existing `scrape_careers_page(careers_url, target_titles, exclusions)` scrapes the careers page for job titles matching keywords.

### What's missing

1. **Haiku fallback for careers URL discovery** — when heuristic link-finding fails (no obvious /careers link), call Haiku with the homepage HTML to identify the careers page URL
2. **Rich job extraction** — current scraper extracts titles but returns `description=""`. Need to follow individual job links and extract full JD text.
3. **Pagination/sub-page handling** — some careers pages list jobs across multiple pages or departments

### Changes to `careers_scraper.py`

**`find_careers_url` enhancement:**
- After heuristic link-finding fails, call Haiku with a truncated version of the homepage HTML (first 3000 chars)
- Prompt: "Given this company homepage HTML, identify the URL for their careers or jobs page. Return only the URL, or 'none' if not found."
- This is a cheap Haiku call (~500 input tokens) and only fires when heuristics fail

**`scrape_careers_page` enhancement:**
- For each matched job title, follow the job's URL and extract the full JD text (same `_fetch_direct_jd` pattern from `data_enricher.py`)
- Return `description` field populated with the full JD text instead of empty string
- Add rate limiting (1s delay between job page fetches) to be polite

**New: `_extract_jobs_with_haiku` fallback:**
- When HTML parsing finds no job listings, send the careers page HTML to Haiku
- Prompt: "Extract job listings from this careers page HTML. Return JSON array of objects with title, url, location fields."
- Only fires when heuristic parsing returns 0 results
- **Cost control:** Both Haiku calls (careers URL discovery + job extraction fallback) must go through `call_claude` with `purpose="careers_scrape"` for cost tracking. The `find_careers_url` Haiku call is ~500 input tokens. The `_extract_jobs_with_haiku` call processes truncated HTML (~3000 chars, ~1000 tokens). With 230 miss companies bi-weekly, worst case is ~460 Haiku calls/week (~$0.05). Budget gating via `cost_gate` should be checked before each Haiku call.

### Integration with `run_ats_scan`

The existing HTML fallback loop in `run_ats_scan` (lines 1162-1263) already calls `find_careers_url` + `scrape_careers_page` for miss companies with homepage_url. Once homepages are populated (Gap A) and the scraper is enhanced (Gap B), this loop will start producing results.

**Important:** The HTML fallback loop (line 1219) currently hardcodes `description=""` when creating Job objects from scraped results. This must be updated to pass through the description from `scrape_careers_page` (e.g., `description=scraped_job.get("description", "")`), or the rich JD extraction work will be silently discarded.

## Gap C: Pipeline Verification (checklist, not implementation)

### What to verify

1. **Auto-add on ingestion:** `_score_and_persist` in `pipeline_runner.py` calls `upsert_company` for every job. Confirm this is actually running by checking that new companies appear after a fresh ingestion run.
2. **Homepage discovery trigger:** After a new company is created, it should be queued for homepage discovery on the next scheduler cycle.
3. **End-to-end flow:** New job ingested → company created → homepage discovered → careers page found → additional jobs scraped → scored.

### How to verify

- Run a test ingestion and check the companies table for new entries
- Check scheduler logs for homepage discovery and careers scanning activity
- Query `company_scan_log` for recent successful scans with `jobs_found > 0`

## Schedule

Homepage discovery and careers scanning should run on the same schedule as ATS scanning (currently configured in scheduler). No new schedule needed — enhance the existing `run_ats_scan` job to include homepage discovery as a pre-step.

## Files Modified

| File | Change |
|------|--------|
| `ats_scanner.py` or new `homepage_discoverer.py` | `discover_homepage()` function |
| `careers_scraper.py` | Haiku fallback in `find_careers_url`, rich JD extraction in `scrape_careers_page`, `_extract_jobs_with_haiku` fallback |
| `scheduler.py` | Wire homepage discovery into scan schedule |
| `ats_scanner.py` (`run_ats_scan`) | Add homepage discovery pre-step before HTML fallback loop |

## Testing

- Run homepage discovery on a sample of companies — verify URLs are real company sites
- Test `find_careers_url` with a company whose careers link is non-obvious — verify Haiku fallback works
- Test `scrape_careers_page` rich extraction — verify returned jobs have full descriptions
- End-to-end: ingest a new job from Gmail → verify company auto-created → verify homepage discovered on next cycle
- `pytest tests/` for regression
