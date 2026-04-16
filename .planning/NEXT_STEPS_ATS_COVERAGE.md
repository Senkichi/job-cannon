# Next Steps: Closing the ATS Coverage Gap

**Date:** 2026-04-15
**Author:** Claude session (Workday + SmartRecruiters + AI navigator session)
**Status:** Ready for next session

---

## Current State

### What Was Built This Session

1. **Workday ATS Scanner** — `scan_workday()` in `ats_platforms.py`
   - POST-based CXS API with pagination (20/page, max 200)
   - Slug format: `"{subdomain}/{board}"` (e.g. `walmart.wd5/WalmartExternal`)
   - Tenant derived from subdomain prefix before `.wd`
   - 27 companies promoted, 15 matched postings
   - Commit: `23090ca`

2. **SmartRecruiters ATS Scanner** — `scan_smartrecruiters()` in `ats_platforms.py`
   - GET-based public REST API, no auth needed
   - `GET https://api.smartrecruiters.com/v1/companies/{slug}/postings?offset=N&limit=100`
   - **Must send `Accept: application/json` header** — without it, API returns YAML-like schema docs instead of data
   - 15 companies promoted, 12 matched jobs (Experian 6, WNS 3, Intuitive 2, Sandisk 1)
   - Commit: `feat: SmartRecruiters ATS scanner`

3. **AI-Navigated Careers Crawler** — `ai_career_navigator.py` (new file)
   - Two-phase: discover (Haiku, ~$0.01) → replay (mechanical, $0)
   - Supports `goto`, `click`, `type`, `press`, `wait` recipe actions
   - Pre-check: skips Haiku when page already has static job links
   - Snapshot includes page links with hrefs for `goto` discovery
   - `_derive_search_term()` extracts broad single-word term from target titles ("data" from the current profile)
   - 7 companies navigated: Voleon, Cigna, Moniepoint, CMU, Glean, Yahoo, Rippling
   - 7 recipes cached in `careers_nav_recipe` column (Migration 37)
   - Commit: `23090ca`

4. **Escalation Chain Fix** — `careers_crawler.py`
   - Flattened tier nesting: static → url_param → playwright → AI navigate
   - Previously, Playwright was trapped in the `else` branch of `if static_result is not None`
   - Now each tier runs independently when the previous tier returns 0 jobs

### Coverage Numbers

| Platform | Companies | Source |
|----------|-----------|--------|
| Greenhouse | 234 | Pre-existing |
| Ashby | 97 | Pre-existing |
| Lever | 35 | Pre-existing |
| **Workday** | **27** | This session |
| **SmartRecruiters** | **15** | This session |
| **AI Navigate** | **7** | This session (recipe-cached) |
| **TOTAL** | **415** | **78% of 532 high-scoring** |

### Remaining Gap: 117 companies (22%)

| Category | Count | Description |
|----------|-------|-------------|
| Pending + careers_url | 43 | Never probed — Google, Uber, Netflix, Apple, Walmart |
| Miss + static crawl (no jobs) | 187 | Career pages exist, static extraction yields 0 |
| Miss + no crawl tier | 42 | Career URL found but never crawled |
| Miss + no careers_url | 25 | No discoverable career page |
| Pending + no careers_url | 16 | No homepage or career page |
| Error (transient) | 7 | NVIDIA, Disney, Palo Alto Networks — in backoff |

---

## Quick Wins (Do First)

### QW-1: Probe the 43 PENDING companies

These are major companies (Google, Uber, Netflix, Apple) that were added to the registry after the last `probe_ats_slugs` run. Many will have Greenhouse/Lever/Ashby/Workday APIs.

```python
# Run from Flask shell or script:
from job_finder.web.ats_scanner import probe_ats_slugs
from job_finder.config import load_config
config = load_config()
result = probe_ats_slugs(config['db_path'], config)
# Expected: 15-25 new hits
```

**Estimated gain:** 15-25 companies promoted to ATS-hit.

### QW-2: Trigger crawl for 42 uncrawled MISS companies

These have `careers_url` but `careers_crawl_tier IS NULL` — the careers crawler never attempted them.

```python
from job_finder.web.careers_crawler import crawl_careers_batch
result = crawl_careers_batch(config['db_path'], config)
```

The batch query in `crawl_careers_batch()` (line ~610) already selects these companies. Just run it.

### QW-3: Increase probe frequency

Current: `probe_ats_slugs` runs Mon/Wed at 7:30 AM (file: `scheduler.py`).
Fix: Change to daily or every 8 hours. New companies are added daily via email alerts.

---

## Medium-Term: iCIMS Scanner (Playwright-Based)

### Why iCIMS Is Different

iCIMS career pages are **100% JavaScript-rendered**. A `requests.get()` returns an empty HTML shell with `<script>` tags. There is no public JSON API. The official iCIMS API (`api.icims.com`) requires Basic Auth with customer credentials.

**Evidence from this session:**
```
curl https://careers-herbalife.icims.com/jobs/search → HTML shell, no job data
curl https://careers-arh.icims.com/jobs/search → Same — JS-only rendering
```

### Companies Using iCIMS (3 confirmed in DB)

| Company | careers_url |
|---------|-------------|
| Appalachian Regional Healthcare | `https://careers-arh.icims.com/` |
| Herbalife | `https://careers-herbalife.icims.com/jobs/intro` |
| Law School Admission Council | `https://careers-lsac.icims.com/` |

More may be discoverable by scanning career pages with Playwright (see Long-Term section).

### Implementation Approach

**Unlike all other scanners**, iCIMS requires Playwright to render the page. This is a different architecture pattern — closer to the careers_crawler than to the ats_platforms scanners.

**Option A (Recommended): Extend the careers crawler's Playwright active tier**
- When `careers_crawl_tier == "playwright"` and the URL contains `icims.com`, use iCIMS-specific extraction
- iCIMS pages use a known DOM structure: `.iCIMS_JobsTable`, `.iCIMS_JobTitle`, `.iCIMS_JobLocation`
- After Playwright renders, parse with BeautifulSoup using iCIMS-specific selectors
- No new file needed — add to `careers_page_interactions.py` or `careers_crawler.py`

**Option B: Standalone iCIMS scanner with Playwright**
- New file `job_finder/web/ats_icims.py`
- Probe: `_probe_icims(slug)` via `requests.get` checking for HTTP 200
- Scan: `scan_icims(slug, ...)` launches Playwright, renders page, extracts with CSS selectors
- Downside: Playwright dependency in a scan function (all other scanners are requests-only)

**iCIMS DOM selectors to research (browser dev tools on live page):**
```
.iCIMS_JobsTable          — container for all job listings
.iCIMS_JobTitle a          — job title link
.iCIMS_JobLocation         — location text
.iCIMS_JobDate             — posted date
```

**Important:** These selectors may vary by iCIMS version and customer configuration. Research by opening browser dev tools on `careers-herbalife.icims.com/jobs/search` with JavaScript enabled.

### URL Patterns for Detection

```python
_ICIMS_URL = re.compile(
    r"https?://(?:careers-)?([a-z0-9-]+?)(?:-careers)?\.icims\.com",
    re.IGNORECASE,
)
```

Handles: `careers-herbalife.icims.com`, `arh.icims.com`, `lsac-careers.icims.com`

---

## Medium-Term: Career Page ATS Discovery

### The Problem

Many of the 187 "miss + static crawl" companies are actually on known ATS platforms — but the ATS URL isn't in their `source_urls` (they were discovered via email alerts, not ATS APIs). The ATS is embedded in their career page as:
- An iframe (`<iframe src="https://company.wd1.myworkdayjobs.com/..."`)
- An outbound link ("Apply" button linking to Workday/iCIMS/etc.)
- A JavaScript redirect
- An API endpoint called by frontend JS

### Implementation: `discover_ats_from_career_pages()`

New function in `ats_scanner.py` (or new file `ats_discoverer.py`):

1. Query companies with `ats_probe_status='miss'` and `careers_url IS NOT NULL`
2. For each company, open the career page with Playwright
3. Extract all outbound links and iframe sources
4. Match against known ATS URL patterns (reuse `extract_ats_from_urls()`)
5. If ATS found: verify with probe, update company record

**Key insight from this session:** Dick's Sporting Goods' career page at `dickssportinggoods.jobs/search-jobs` has a link to `dickssportinggoods.wd1.myworkdayjobs.com/DSG` — a Workday URL. The existing Workday scanner could handle it, but the URL was never discovered because the company was classified as "miss" (slug speculation failed).

**Expected gain:** 20-40 companies promoted from miss → hit across Workday, iCIMS, and other platforms.

### Infrastructure Already Available

- `careers_page_interactions.py:setup_api_capture()` — captures XHR/fetch requests made by the career page JS. These often include ATS API calls.
- `ats_detection.py:extract_ats_from_urls()` — already detects Lever, Greenhouse, Ashby, Workday, SmartRecruiters from URLs.
- The Playwright active tier in `careers_crawler.py` already visits these pages — just need to extract ATS URLs from the page's links/iframes.

---

## Long-Term: Additional Platform Scanners

### Platforms Detected in the Wild (from audit)

| Platform | Companies in DB | API Type | Effort |
|----------|----------------|----------|--------|
| iCIMS | 3 (careers_url) | JS-only, Playwright needed | High |
| Rippling | 2 (ats.rippling.com) | Unknown API | Medium |
| Workable | 1 (apply.workable.com) | Public REST API | Low |
| Avature | 1 (maximus.avature.net) | Unknown | Medium |
| Phenom | 0 in DB (24 from audit) | OAuth API, hidden frontend API | High |
| Taleo | 0 (from pipeline_detector) | Legacy Oracle, SOAP | Very High |
| SuccessFactors | 0 (from pipeline_detector) | SAP, complex auth | Very High |

### Priority Order

1. **Workable** — Low effort, has public API (`https://apply.workable.com/api/v1/widget/{slug}/jobs`), but only 1 company
2. **Rippling** — 2 companies, unknown API, need research
3. **iCIMS** — 3 confirmed, likely 20+ discoverable via career page scanning
4. **Phenom** — 0 currently in DB, need career page scanning to discover them first

### Phenom Research Notes

Phenom powers career sites but uses custom domains (not `phenompeople.com`). Detection requires:
- Checking for Phenom JavaScript libraries in career page source (`phenom-*`, `pcs-*` CSS classes)
- Migration 25 already has a data cleanup for "Eightfold/Phenom PCS ATS SPA shell garbage" — these pages have `"themeOptions"` JSON in the HTML
- The existing `_WRONG_PAGE_SIGNATURES` in enrichment code already detects Phenom shells

To find Phenom companies: scan the 187 static-crawl-miss companies' career pages for Phenom indicators.

---

## Architecture Reference

### Adding a New ATS Platform (Checklist)

Every platform touches exactly 4 files + 6 dispatch points:

**Files:**
1. `job_finder/web/ats_detection.py` — URL regex + `extract_ats_from_urls()` elif
2. `job_finder/web/ats_prober.py` — `_probe_{platform}()` + `probe_single_company()` elif
3. `job_finder/web/ats_platforms.py` — `scan_{platform}()` function
4. `job_finder/web/ats_scanner.py` — 3 dispatch points + re-export

**Dispatch Points in `ats_scanner.py`:**
1. Line ~730: `run_ats_scan()` scan dispatch (`elif platform == "xxx": ...`)
2. Line ~370: `promote_ats_from_source_urls()` verification (`elif platform == "xxx": verified = _probe_xxx(slug)`)
3. Line ~65: Re-exports (`from job_finder.web.ats_prober import _probe_xxx`)

**Dispatch Points in `ats_prober.py`:**
4. Line ~210: `probe_single_company()` known-platform HTTP probe (set URL for GET, or delegate for POST)
5. Line ~290: `probe_single_company()` speculative probing loop (try each platform for derived slugs)

**Return Contract (all `scan_*` functions):**
```python
list[dict] with keys:
  title: str          # Job title
  company_source: str # Platform name (e.g. "SmartRecruiters")
  location: str       # Job location
  description: str    # Full JD text (or "" if not available from list API)
  source_url: str     # Link to job posting
  salary_min: int|None
  salary_max: int|None
  comp_json: str|None # JSON string with compensation details
```

### Key Files Reference

| File | Lines | Purpose |
|------|-------|---------|
| `ats_detection.py` | ~150 | URL regex patterns + `extract_ats_from_urls()` |
| `ats_prober.py` | ~430 | Probe functions + `probe_single_company()` + retry state machine |
| `ats_platforms.py` | ~480 | Scan functions for all platforms |
| `ats_scanner.py` | ~1000 | Orchestration: `probe_ats_slugs()`, `promote_ats_from_source_urls()`, `run_ats_scan()` |
| `careers_crawler.py` | ~1150 | Multi-tier careers page crawler with AI navigation |
| `ai_career_navigator.py` | ~550 | Haiku discovery + mechanical replay |
| `careers_page_interactions.py` | ~520 | Playwright interactions (load-more, scroll, search, API capture) |
| `db_migrate.py` | ~810 | 37 migrations |

### Database Columns (companies table)

```
ats_platform TEXT          — 'lever'|'greenhouse'|'ashby'|'workday'|'smartrecruiters'|NULL
ats_slug TEXT              — platform-specific identifier
ats_probe_status TEXT      — 'pending'|'hit'|'miss'|'error'
careers_url TEXT           — discovered career page URL
careers_crawl_tier TEXT    — 'static'|'url_param'|'playwright'|'ai_navigate'|'ai_replay'|NULL
careers_nav_recipe TEXT    — JSON recipe for AI navigator (NULL if not discovered)
careers_api_endpoint TEXT  — cached API endpoint discovered by Playwright active tier
careers_crawl_last_at TEXT — last crawl timestamp
retry_count INTEGER        — exponential backoff counter
retry_after TEXT           — next eligible retry timestamp
miss_reason TEXT           — 'unreachable' for permanent failures
```

### Test Patterns

All ATS scanner tests follow the same structure (see `tests/test_workday_scanner.py` or `tests/test_smartrecruiters_scanner.py`):

```python
class TestUrlDetection:      # 5 tests — regex patterns
class TestProbe:             # 4-5 tests — mock requests.get/post
class TestScan:              # 7 tests — mock API responses, pagination, errors
```

HTTP calls are always mocked via `@patch("job_finder.web.ats_prober.requests.get")` (or `.post` for Workday).

### SmartRecruiters API Quirks

- **Must include `Accept: application/json` header** — without it, returns YAML-like schema documentation
- Pagination: `offset` + `limit` query params, response has `totalFound`
- Public posting API: no auth needed
- Slug is case-sensitive and often has version suffixes (e.g. `LinkedIn3`, `PaloAltoNetworks2`)
- Some slugs return `totalFound: 0` with HTTP 200 (valid company, no current postings) — probe must check `totalFound > 0`

### Workday API Quirks

- **POST-based** (unlike all other platforms which are GET)
- Slug format: `"{subdomain}/{board}"` — must be split to construct URL
- Tenant: derived from subdomain prefix before `.wd` (e.g. `walmart` from `walmart.wd5`)
- API URL: `https://{subdomain}.myworkdayjobs.com/wday/cxs/{tenant}/{board}/jobs`
- Request body: `{"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}`
- Response: `{"total": N, "jobPostings": [{title, externalPath, locationsText}]}`

### AI Navigator Quirks

- `_take_snapshot()` combines accessibility tree (truncated to 2500 chars) + prioritized links with hrefs (up to 20)
- High-priority links ("Job Search", "Browse Openings", etc.) sorted first
- `_derive_search_term()` returns the most common non-stop-word from target titles (currently "data")
- Recipe validation is best-effort: executes steps until first failure, then extracts from whatever page state exists
- If validation produces jobs, the recipe is cached (even if some steps failed — recipe is trimmed to successful steps)
- `RecipeStaleError` triggers recipe invalidation and re-discovery on next crawl

---

## Session Artifacts

### Commits Made
1. `23090ca feat: Workday ATS scanner + AI-navigated careers crawler` — Workday scanner, AI navigator, escalation fix, Migration 37, infrastructure fixes
2. `(latest) feat: SmartRecruiters ATS scanner` — SmartRecruiters scanner, 17 tests

### Tests Added
- `tests/test_workday_scanner.py` — 19 tests
- `tests/test_ai_career_navigator.py` — 18 tests (updated: keyword placeholder test reflects `_derive_search_term`)
- `tests/test_smartrecruiters_scanner.py` — 17 tests

### Pre-Existing Test Failures (Not Caused by This Session)
- `test_data_enricher.py::TestPipelineIntegration::test_enrich_job_called_before_haiku_in_pipeline` — assertion fails on clean master
- `test_scoring.py::TestHaikuPipelineIntegration::*` (4 tests) — "Liveness gate: archiving expired" blocks scoring in test
- `test_scoring.py::TestBorderlineReeval::*` (4 tests) — same liveness gate issue
- `test_scoring.py::TestExclusionFilterIntegration::*` (1 test) — same issue

### Untracked Garbage Files (Safe to Delete)
These appeared before this session — likely from prior shell escaping issues:
```
0, 0), 5, 5s}, 6d}, actual, datetime('now', {final_url[, Recipe
```
Run: `rm 0 "0)" 5 "5s}" "6d}" actual "datetime('now'" "{final_url[" Recipe` to clean up.
