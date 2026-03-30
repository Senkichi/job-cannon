"""Agentic job description enricher using Ollama + DDG search + Playwright.

Recovers job descriptions for 'exhausted' jobs where the standard enrichment
pipeline failed. Uses a multi-step agentic loop:

1. Ollama generates targeted search queries from job metadata
2. DDG search finds candidate URLs (free, no API key needed)
3. Playwright fetches pages with JS rendering
4. Ollama validates whether fetched content is the right job posting
5. Extracts and persists jd_full on success

Designed for batch backfill, not real-time pipeline use (Playwright is heavy).
Requires: Ollama running locally, Playwright + Chromium installed.

Usage:
    from job_finder.web.agentic_enricher import run_agentic_backfill
    count = run_agentic_backfill("jobs.db", config, limit=50)
"""

import json
import logging
import re
import time
from typing import Optional

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_JD_CHARS = 8000
_MAX_SEARCH_QUERIES = 4
_MAX_URLS_PER_QUERY = 3
_MAX_FETCH_ATTEMPTS = 6  # Total URLs to try before giving up
_PAGE_LOAD_WAIT_MS = 3000
_SEARCH_DELAY_S = 1.5  # Between DDG searches to avoid rate limits

# Sites that are known to block scraping or require login
_BLOCKED_DOMAINS = frozenset({
    "glassdoor.com", "glassdoor.co.uk",
    "indeed.com",  # Often shows interstitial
    "ziprecruiter.com",
    "dice.com",
})

# Sites that reliably have full JDs when you can fetch them
_PRIORITY_DOMAINS = [
    "greenhouse.io", "lever.co", "ashbyhq.com",  # ATS platforms
    "myworkdayjobs.com", "jobs.smartrecruiters.com",  # More ATS
    "linkedin.com/jobs",  # Public job pages (no login for viewing)
    "builtin.com",
    "workingnomads.com",
    "ycombinator.com/companies",
]

# System prompt for query generation
_QUERY_GEN_PROMPT = """\
You are a job search assistant. Given a job title and company name, generate \
{n} different web search queries that are likely to find the full job description \
posted on the company's careers page, a job board, or LinkedIn.

Rules:
- Each query should use a DIFFERENT search strategy
- Include the company name and key title words
- Try: company careers page, LinkedIn, Greenhouse/Lever, job boards
- Use quotes around multi-word phrases when helpful
- Output ONLY a JSON array of strings, no explanation

Example output: ["Uber careers Data Analyst Measurement Science", "site:linkedin.com Uber Data Analyst Ads"]
"""

# System prompt for page validation
_VALIDATE_PROMPT = """\
You are validating whether a web page contains the job description for a specific role.

Target job: {title} at {company}

Analyze the text below and respond with ONLY a JSON object:
{{
  "is_match": true/false,
  "confidence": 0.0-1.0,
  "reason": "brief explanation"
}}

Set is_match=true if the page contains a job posting that matches (or very closely \
matches) the target title and company. Allow minor title variations (e.g., "Sr" vs \
"Senior", "Lead" vs "Staff"). Set is_match=false if it's a different role, a job \
listing page with many jobs, or unrelated content.
"""


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def _search_ddg(query: str, max_results: int = 5) -> list[dict]:
    """Search DuckDuckGo and return results [{title, href, body}]."""
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=max_results))
    except Exception as exc:
        logger.debug("DDG search failed for '%s': %s", query[:60], exc)
        return []


def _is_blocked_domain(url: str) -> bool:
    """Check if URL is from a domain known to block scraping."""
    url_lower = url.lower()
    return any(domain in url_lower for domain in _BLOCKED_DOMAINS)


def _domain_priority(url: str) -> int:
    """Lower = higher priority. ATS platforms and known job boards first."""
    url_lower = url.lower()
    for i, domain in enumerate(_PRIORITY_DOMAINS):
        if domain in url_lower:
            return i
    return 100


def _rank_urls(search_results: list[dict]) -> list[str]:
    """Extract, deduplicate, filter, and rank URLs from search results."""
    seen = set()
    urls = []
    for r in search_results:
        href = r.get("href", "")
        if not href or href in seen or _is_blocked_domain(href):
            continue
        seen.add(href)
        urls.append(href)

    # Sort by domain priority (ATS platforms first)
    urls.sort(key=_domain_priority)
    return urls


# ---------------------------------------------------------------------------
# Page fetching (Playwright)
# ---------------------------------------------------------------------------


def _create_browser(playwright):
    """Create a Playwright browser context with realistic fingerprint."""
    browser = playwright.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled"],
    )
    ctx = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
    )
    page = ctx.new_page()
    page.add_init_script(
        'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'
    )
    return browser, page


def _fetch_page_text(page, url: str, timeout_ms: int = 15000) -> Optional[str]:
    """Fetch a URL with Playwright and return cleaned text content."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(_PAGE_LOAD_WAIT_MS)

        html = page.content()
        soup = BeautifulSoup(html, "html.parser")

        # Remove noise
        for tag in soup.find_all(
            ["script", "style", "nav", "footer", "header", "noscript", "aside"]
        ):
            tag.decompose()

        text = soup.get_text(separator="\n", strip=True)

        # Auth-wall detection (same as fetch_direct_jd)
        text_lower = text[:500].lower()
        auth_signals = [
            "sign in", "log in", "create account", "verify you are a human",
            "access denied", "captcha", "please verify", "just a moment",
        ]
        if any(sig in text_lower for sig in auth_signals) and len(text) < 2000:
            logger.debug("Auth wall detected on %s", url[:80])
            return None

        # Too short = probably not a job description
        if len(text) < 300:
            return None

        return text[:_MAX_JD_CHARS * 2]  # Keep extra for validation, trim later

    except Exception as exc:
        logger.debug("Playwright fetch failed for %s: %s", url[:80], exc)
        return None


# ---------------------------------------------------------------------------
# Ollama calls
# ---------------------------------------------------------------------------


def _call_ollama(
    system: str,
    user_msg: str,
    model: str = "qwen2.5:14b",
    max_tokens: int = 512,
) -> Optional[str]:
    """Call Ollama chat API and return the response text."""
    import requests as req

    try:
        resp = req.post(
            "http://localhost:11434/api/chat",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_msg},
                ],
                "format": "json",
                "stream": False,
                "options": {"num_predict": max_tokens},
            },
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]
    except Exception as exc:
        logger.warning("Ollama call failed: %s", exc)
        return None


def _generate_queries(title: str, company: str, n: int = _MAX_SEARCH_QUERIES) -> list[str]:
    """Ask Ollama to generate search queries for a job posting."""
    system = _QUERY_GEN_PROMPT.format(n=n)
    user_msg = f"Job title: {title}\nCompany: {company}"

    response = _call_ollama(system, user_msg)
    if not response:
        # Fallback: generate basic queries without AI
        return _fallback_queries(title, company)

    try:
        data = json.loads(response)
        if isinstance(data, list) and all(isinstance(q, str) for q in data):
            return data[:n]
        # Sometimes Ollama wraps in {"queries": [...]}
        if isinstance(data, dict):
            for key in ("queries", "search_queries", "results"):
                if key in data and isinstance(data[key], list):
                    return [str(q) for q in data[key][:n]]
    except (json.JSONDecodeError, TypeError):
        pass

    return _fallback_queries(title, company)


def _fallback_queries(title: str, company: str) -> list[str]:
    """Generate basic search queries without AI."""
    # Strip parentheticals from title
    clean_title = re.sub(r"\([^)]*\)", "", title).strip()
    return [
        f"{company} careers {clean_title}",
        f'"{clean_title}" "{company}" job description',
        f"site:linkedin.com {company} {clean_title}",
        f"site:greenhouse.io OR site:lever.co {company} {clean_title}",
    ]


def _validate_page(
    text: str, title: str, company: str, model: str = "qwen2.5:14b"
) -> tuple[bool, float]:
    """Ask Ollama if page content matches the target job. Returns (is_match, confidence)."""
    system = _VALIDATE_PROMPT.format(title=title, company=company)
    # Truncate page text to keep Ollama context reasonable
    user_msg = text[:4000]

    response = _call_ollama(system, user_msg, model=model, max_tokens=256)
    if not response:
        return False, 0.0

    try:
        data = json.loads(response)
        is_match = bool(data.get("is_match", False))
        confidence = float(data.get("confidence", 0.0))
        reason = data.get("reason", "")
        if reason:
            logger.debug("Validation: match=%s conf=%.2f reason=%s", is_match, confidence, reason)
        return is_match, confidence
    except (json.JSONDecodeError, TypeError, ValueError):
        return False, 0.0


# ---------------------------------------------------------------------------
# Main agentic loop (per job)
# ---------------------------------------------------------------------------


def enrich_single_job(
    job_row: dict,
    page,
    model: str = "qwen2.5:14b",
) -> Optional[str]:
    """Run the agentic enrichment loop for a single job.

    Args:
        job_row: Job dict with title, company fields.
        page: Playwright page object (reused across jobs).
        model: Ollama model to use for query gen + validation.

    Returns:
        The job description text if found, None otherwise.
    """
    title = job_row.get("title", "")
    company = job_row.get("company", "")

    if not title or not company:
        return None

    # Step 1: Generate search queries
    queries = _generate_queries(title, company)
    logger.info("Agentic: %d queries for '%s' @ '%s'", len(queries), title[:40], company[:20])

    # Step 2: Search and collect candidate URLs
    all_urls: list[str] = []
    for query in queries:
        results = _search_ddg(query, max_results=_MAX_URLS_PER_QUERY)
        urls = _rank_urls(results)
        all_urls.extend(u for u in urls if u not in all_urls)
        time.sleep(_SEARCH_DELAY_S)

    if not all_urls:
        logger.info("Agentic: no URLs found for '%s' @ '%s'", title[:40], company[:20])
        return None

    logger.info("Agentic: %d candidate URLs", len(all_urls))

    # Step 3: Fetch and validate pages
    best_text: Optional[str] = None
    best_confidence: float = 0.0

    for i, url in enumerate(all_urls[:_MAX_FETCH_ATTEMPTS]):
        text = _fetch_page_text(page, url)
        if not text:
            continue

        # Quick heuristic check before calling Ollama
        text_lower = text.lower()
        company_lower = company.lower().split()[0]  # First word of company name
        if company_lower not in text_lower:
            logger.debug("Agentic: skipping %s (company name not found)", url[:60])
            continue

        # Validate with Ollama
        is_match, confidence = _validate_page(text, title, company, model=model)

        if is_match and confidence > best_confidence:
            best_text = text
            best_confidence = confidence
            if confidence >= 0.8:
                logger.info("Agentic: high-confidence match at %s (%.2f)", url[:60], confidence)
                break

    if best_text and best_confidence >= 0.5:
        # Trim to JD limit
        return best_text[:_MAX_JD_CHARS]

    logger.info(
        "Agentic: no valid match for '%s' @ '%s' (best_conf=%.2f)",
        title[:40], company[:20], best_confidence,
    )
    return None


# ---------------------------------------------------------------------------
# Batch backfill
# ---------------------------------------------------------------------------


def run_agentic_backfill(
    db_path: str,
    config: dict,
    limit: int = 50,
    model: str = "qwen2.5:14b",
) -> int:
    """Run agentic enrichment on exhausted jobs missing jd_full.

    Args:
        db_path: Path to SQLite database.
        config: Application config dict.
        limit: Maximum jobs to process.
        model: Ollama model for query gen + validation.

    Returns:
        Number of jobs successfully enriched.
    """
    from playwright.sync_api import sync_playwright

    from job_finder.web.db_helpers import standalone_connection

    with standalone_connection(db_path) as conn:
        rows = conn.execute(
            """SELECT * FROM jobs
               WHERE enrichment_tier = 'exhausted'
                 AND jd_full IS NULL
               ORDER BY haiku_score DESC NULLS LAST
               LIMIT ?""",
            (limit,),
        ).fetchall()

        if not rows:
            print("No exhausted jobs to enrich.")
            return 0

        total = len(rows)
        print(f"Agentic enrichment: {total} jobs to process")
        print()

        enriched_count = 0

        with sync_playwright() as pw:
            browser, page = _create_browser(pw)

            try:
                for i, row in enumerate(rows, 1):
                    job = dict(row)
                    title = job.get("title", "?")[:55]
                    company = job.get("company", "?")[:25]

                    print(f"[{i}/{total}] {title} @ {company}")

                    t0 = time.time()
                    jd = enrich_single_job(job, page, model=model)
                    elapsed = time.time() - t0

                    if jd:
                        # Persist to DB
                        conn.execute(
                            "UPDATE jobs SET jd_full = ?, enrichment_tier = 'agentic' "
                            "WHERE dedup_key = ?",
                            (jd, job["dedup_key"]),
                        )
                        conn.commit()
                        enriched_count += 1
                        print(f"  -> FOUND {len(jd)} chars ({elapsed:.1f}s)")
                    else:
                        # Mark as agentic-exhausted so we don't retry
                        conn.execute(
                            "UPDATE jobs SET enrichment_tier = 'agentic_exhausted' "
                            "WHERE dedup_key = ?",
                            (job["dedup_key"],),
                        )
                        conn.commit()
                        print(f"  -> NOT FOUND ({elapsed:.1f}s)")

                    print()

            finally:
                browser.close()

        print(f"Enriched {enriched_count}/{total} jobs ({100*enriched_count/total:.0f}%)")
        return enriched_count
