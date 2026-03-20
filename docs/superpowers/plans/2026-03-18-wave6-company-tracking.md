# Wave 6: Company Tracking Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fill gaps in company tracking: auto-discover homepages, enhance careers page scraping with Haiku fallback, and ensure rich JD extraction from company sites.

**Architecture:** Homepage discovery via ATS slug heuristic + free web search. Careers scraper enhanced with Haiku fallback for URL discovery and job extraction. Integrated into existing scheduler scan cycle.

**Tech Stack:** Python, requests, BeautifulSoup, Anthropic Haiku, SQLite

**Spec:** `docs/superpowers/specs/2026-03-18-wave6-company-tracking-design.md`

---

## Chunk 1: Homepage Auto-Discovery

### Task 1: Create homepage discovery module

**Files:**
- Create: `job_finder/web/homepage_discoverer.py`
- Test: `tests/test_homepage_discoverer.py`

- [ ] **Step 1: Write failing test for slug-based homepage discovery**

```python
def test_discover_homepage_from_slug(monkeypatch):
    """discover_homepage should try {slug}.com and return it if it resolves."""
    import requests
    from unittest.mock import Mock
    from job_finder.web.homepage_discoverer import discover_homepage

    mock_resp = Mock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "text/html"}
    mock_resp.url = "https://ramp.com/"

    monkeypatch.setattr(requests, "head", lambda *a, **kw: mock_resp)

    result = discover_homepage("Ramp", "ashby", "ramp", [])
    assert result == "https://ramp.com"


def test_discover_homepage_falls_back_to_search(monkeypatch):
    """discover_homepage should fall back to web search when slug.com fails."""
    import requests
    from unittest.mock import Mock
    from job_finder.web.homepage_discoverer import discover_homepage

    # Slug.com fails (404)
    fail_resp = Mock()
    fail_resp.status_code = 404

    # DDG search returns a result
    search_html = '<a href="https://www.examplecorp.com/">ExampleCorp - Official Site</a>'
    search_resp = Mock()
    search_resp.status_code = 200
    search_resp.text = search_html

    # Validation HEAD succeeds
    valid_resp = Mock()
    valid_resp.status_code = 200
    valid_resp.headers = {"content-type": "text/html"}
    valid_resp.url = "https://www.examplecorp.com/"

    call_count = {"n": 0}
    def mock_get(*args, **kwargs):
        call_count["n"] += 1
        if "duckduckgo" in args[0]:
            return search_resp
        return valid_resp

    def mock_head(*args, **kwargs):
        if "examplecorp" in args[0]:
            return valid_resp
        return fail_resp

    monkeypatch.setattr(requests, "get", mock_get)
    monkeypatch.setattr(requests, "head", mock_head)

    result = discover_homepage("ExampleCorp", None, None, [])
    assert result is not None
    assert "examplecorp" in result.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_homepage_discoverer.py -v`

Expected: FAIL (module doesn't exist)

- [ ] **Step 3: Implement homepage_discoverer.py**

```python
"""Homepage auto-discovery for company records.

Two-tier approach:
1. Slug heuristic: try https://{slug}.com, validate with HEAD request
2. Free web search: DuckDuckGo HTML search, extract first result URL, validate

Rate limited: 1s delay between external requests.
"""

import logging
import re
import time
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_TIMEOUT = 8
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; JobFinder/1.0; +https://github.com/job-finder)"
}


def discover_homepage(
    company_name: str,
    ats_platform: Optional[str],
    ats_slug: Optional[str],
    source_urls: list[str],
) -> Optional[str]:
    """Auto-discover a company's homepage URL.

    Tier 1: Try https://{slug}.com if slug available.
    Tier 2: DuckDuckGo HTML search for "{company_name} official website".

    Returns validated homepage URL string, or None if not found.
    """
    # Tier 1: Slug heuristic
    if ats_slug:
        slug_clean = ats_slug.lower().strip()
        candidate = f"https://{slug_clean}.com"
        if _validate_homepage(candidate):
            return candidate.rstrip("/")

    # Tier 2: Free web search
    url = _search_homepage_ddg(company_name)
    if url:
        return url.rstrip("/")

    return None


def _validate_homepage(url: str) -> bool:
    """Validate a URL resolves to a real HTML page (not parked/error)."""
    try:
        resp = requests.head(url, timeout=_TIMEOUT, headers=_HEADERS, allow_redirects=True)
        if resp.status_code != 200:
            return False
        content_type = resp.headers.get("content-type", "")
        return "text/html" in content_type
    except Exception:
        return False


def _search_homepage_ddg(company_name: str) -> Optional[str]:
    """Search DuckDuckGo HTML for company homepage URL."""
    try:
        query = f"{company_name} official website"
        resp = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        # DuckDuckGo HTML results have class "result__a" for links
        for link in soup.find_all("a", class_="result__a", limit=5):
            href = link.get("href", "")
            # DDG wraps URLs in redirect — extract the actual URL
            actual_url = _extract_ddg_url(href)
            if actual_url and _is_likely_homepage(actual_url, company_name):
                if _validate_homepage(actual_url):
                    return actual_url

        return None

    except Exception as e:
        logger.debug("DDG homepage search failed for '%s': %s", company_name, e)
        return None


def _extract_ddg_url(href: str) -> Optional[str]:
    """Extract actual URL from DuckDuckGo redirect wrapper."""
    # DDG HTML links often point to //duckduckgo.com/l/?uddg=ENCODED_URL
    if "uddg=" in href:
        from urllib.parse import parse_qs, urlparse as _urlparse
        parsed = _urlparse(href)
        params = parse_qs(parsed.query)
        urls = params.get("uddg", [])
        return urls[0] if urls else None
    # Direct URL
    if href.startswith("http"):
        return href
    return None


def _is_likely_homepage(url: str, company_name: str) -> bool:
    """Heuristic: is this URL likely the company's homepage (not Wikipedia, LinkedIn, etc.)?"""
    domain = urlparse(url).netloc.lower()
    # Reject known non-homepage domains
    skip_domains = {"wikipedia.org", "linkedin.com", "glassdoor.com", "indeed.com",
                    "ziprecruiter.com", "crunchbase.com", "bloomberg.com", "yelp.com"}
    if any(d in domain for d in skip_domains):
        return False
    return True


def run_homepage_backfill(
    db_path: str,
    limit: int = 50,
    delay: float = 1.0,
) -> dict:
    """Discover homepages for companies that don't have one.

    Args:
        db_path: Path to SQLite database.
        limit: Max companies to process per run.
        delay: Seconds between companies (rate limiting).

    Returns:
        Dict with discovered, failed, skipped counts.
    """
    import sqlite3

    summary = {"discovered": 0, "failed": 0, "skipped": 0}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        companies = conn.execute(
            """SELECT id, name_raw, ats_platform, ats_slug
               FROM companies
               WHERE homepage_url IS NULL
               LIMIT ?""",
            (limit,),
        ).fetchall()

        for company in companies:
            company_id = company["id"]
            name = company["name_raw"]
            platform = company["ats_platform"]
            slug = company["ats_slug"]

            try:
                url = discover_homepage(name, platform, slug, [])
                if url:
                    conn.execute(
                        "UPDATE companies SET homepage_url = ? WHERE id = ?",
                        (url, company_id),
                    )
                    conn.commit()
                    summary["discovered"] += 1
                    logger.info("Homepage discovered for '%s': %s", name, url)
                else:
                    summary["failed"] += 1
            except Exception as e:
                logger.debug("Homepage discovery failed for '%s': %s", name, e)
                summary["failed"] += 1

            time.sleep(delay)

    finally:
        conn.close()

    logger.info(
        "Homepage backfill: %d discovered, %d failed, %d skipped",
        summary["discovered"], summary["failed"], summary["skipped"],
    )
    return summary
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_homepage_discoverer.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add job_finder/web/homepage_discoverer.py tests/test_homepage_discoverer.py
git commit -m "feat: add homepage auto-discovery module with DDG search fallback"
```

### Task 2: Wire homepage discovery into scheduler

**Files:**
- Modify: `job_finder/web/scheduler.py`
- Modify: `job_finder/web/ats_scanner.py` (add homepage discovery pre-step to run_ats_scan)

- [ ] **Step 1: Add homepage discovery as pre-step in run_ats_scan**

At the beginning of `run_ats_scan()`, before the ATS API loop, add:

```python
    # Pre-step: discover homepages for companies that don't have one
    try:
        from job_finder.web.homepage_discoverer import run_homepage_backfill
        hp_result = run_homepage_backfill(db_path, limit=20, delay=1.0)
        logger.info("Homepage discovery: %d found", hp_result.get("discovered", 0))
    except Exception as e:
        logger.debug("Homepage discovery pre-step failed (non-fatal): %s", e)
```

This runs up to 20 homepage discoveries per scan cycle (with 1s delay = 20s max).

- [ ] **Step 2: Run full test suite**

Run: `pytest tests/ -x -q 2>&1 | tail -5`

Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add job_finder/web/ats_scanner.py
git commit -m "feat: wire homepage discovery into ATS scan cycle"
```

## Chunk 2: Enhanced Careers Scraping

### Task 3: Add Haiku fallback to careers URL discovery

**Files:**
- Modify: `job_finder/web/careers_scraper.py`

- [ ] **Step 1: Read the current careers_scraper.py to understand the existing structure**

Read `job_finder/web/careers_scraper.py` fully before modifying.

- [ ] **Step 2: Add Haiku fallback to find_careers_url**

After the existing heuristic link-finding logic in `find_careers_url`, add a Haiku fallback:

```python
    # Heuristic found nothing — try Haiku extraction
    try:
        import anthropic
        from job_finder.web.claude_client import call_claude, cost_gate

        # Cost gate check
        if not cost_gate(None, {}, "haiku"):
            return None

        client = anthropic.Anthropic()
        truncated_html = soup.get_text(separator="\n", strip=True)[:3000]

        system = "You are a web navigation expert. Given homepage text, identify the URL for the company's careers or jobs page. Return ONLY the URL, nothing else. If not found, return 'none'."
        user_msg = f"Homepage URL: {homepage_url}\n\nHomepage text:\n{truncated_html}"

        result, _cost = call_claude(
            client=client,
            model="claude-haiku-4-5-20251001",
            system=system,
            messages=[{"role": "user", "content": user_msg}],
            output_schema=None,
            conn=None,
            job_id=None,
            purpose="careers_scrape",
            config={},
            max_tokens=256,
        )

        if result and isinstance(result, dict):
            url_text = result.get("text", "")
        elif isinstance(result, str):
            url_text = result
        else:
            url_text = ""

        url_text = url_text.strip()
        if url_text and url_text.lower() != "none" and url_text.startswith("http"):
            return url_text

    except Exception as e:
        logger.debug("Haiku careers URL fallback failed: %s", e)
```

- [ ] **Step 3: Commit**

```bash
git add job_finder/web/careers_scraper.py
git commit -m "feat: add Haiku fallback for careers URL discovery"
```

### Task 4: Add rich JD extraction to scrape_careers_page

**Files:**
- Modify: `job_finder/web/careers_scraper.py`

- [ ] **Step 1: Enhance scrape_careers_page to follow job links and extract full JDs**

For each matched job, follow the URL and extract the full description text (reuse `_fetch_direct_jd` pattern from `data_enricher.py`):

```python
    # For each matched job, try to fetch the full JD
    for job in matched_jobs:
        job_url = job.get("url", "")
        if job_url:
            try:
                jd_text = _fetch_job_description(job_url)
                if jd_text and len(jd_text) > 100:
                    job["description"] = jd_text
                time.sleep(1)  # Rate limit between job page fetches
            except Exception:
                pass
```

Add the `_fetch_job_description` helper (similar to `data_enricher._fetch_direct_jd`):

```python
def _fetch_job_description(url: str) -> Optional[str]:
    """Fetch and extract text from a job listing page."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=8)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup.find_all(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        return text[:8000] if text else None
    except Exception:
        return None
```

- [ ] **Step 2: Update run_ats_scan HTML fallback to pass through descriptions**

In `ats_scanner.py`, in the HTML fallback loop (around line 1219), change:

Before: `description="",`
After: `description=scraped_job.get("description", ""),`

- [ ] **Step 3: Add Haiku fallback for job extraction**

When `scrape_careers_page` finds 0 jobs via HTML parsing, call Haiku with the careers page HTML:

```python
    if not matched_jobs:
        # Haiku fallback: extract job listings from unstructured HTML
        try:
            matched_jobs = _extract_jobs_with_haiku(careers_html, target_titles, exclusions)
        except Exception as e:
            logger.debug("Haiku job extraction fallback failed: %s", e)
```

- [ ] **Step 4: Run full test suite**

Run: `pytest tests/ -x -q 2>&1 | tail -5`

Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add job_finder/web/careers_scraper.py job_finder/web/ats_scanner.py
git commit -m "feat: rich JD extraction + Haiku fallback for careers page scraping"
```

### Task 5: Pipeline verification checklist

- [ ] **Step 1: Verify auto-add pipeline**

```python
python -c "
import sqlite3
conn = sqlite3.connect('jobs.db')
# Check companies added recently
recent = conn.execute(\"SELECT COUNT(*) FROM companies WHERE created_at > datetime('now', '-7 days')\").fetchone()[0]
# Check homepages
homepages = conn.execute('SELECT COUNT(*) FROM companies WHERE homepage_url IS NOT NULL').fetchone()[0]
total = conn.execute('SELECT COUNT(*) FROM companies').fetchone()[0]
print(f'Companies added in last 7 days: {recent}')
print(f'Companies with homepage: {homepages}/{total}')
conn.close()
"
```

- [ ] **Step 2: Run a test homepage backfill**

```python
python -c "
from job_finder.web.homepage_discoverer import run_homepage_backfill
result = run_homepage_backfill('jobs.db', limit=5, delay=1.0)
print(result)
"
```

Verify some homepages are discovered.

- [ ] **Step 3: Commit any adjustments from verification**
