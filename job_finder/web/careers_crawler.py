"""Playwright-based careers page crawler for companies with proven relevance.

Provides crawl_careers_batch() — a daily scheduled job that:
1. Loads all companies that have ever had a high-scoring job (haiku_score >= threshold)
2. Tries static HTML extraction first (fast, free)
3. Falls back to Playwright headless Chromium rendering for JS-heavy pages
4. Feeds matched jobs into the existing upsert/score pipeline

Architecture:
- Thread-safe: creates own sqlite3 connections (standalone_connection pattern)
- TESTING guard: returns early when config.get('TESTING') is True
- Browser launched per invocation, not kept alive between runs
- Zero API cost — all extraction is mechanical (JSON-LD, link matching)
"""

import json
import logging
import re
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from job_finder.web.ats_platforms import _title_matches
from job_finder.web.db_helpers import standalone_connection
from job_finder.web.enrichment_tiers import _HEADERS, _TIMEOUT

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FRESHNESS_DAYS = 1            # Re-crawl daily to catch new postings early
_PLAYWRIGHT_TIMEOUT_MS = 15000 # Page load timeout
_JS_SETTLE_MS = 2000           # Wait for JS to finish rendering
_POLITE_DELAY = 1.0            # Seconds between companies

# Minimum text/html ratio to consider a page statically rendered.
# Below this, the page is likely JS-heavy and needs Playwright.
_STATIC_TEXT_RATIO = 0.02
_STATIC_MIN_TEXT_LEN = 500

# Links with these path prefixes are navigation, not job listings
_NAV_PATH_PREFIXES = (
    "/about", "/contact", "/blog", "/news", "/press", "/privacy",
    "/terms", "/legal", "/login", "/signup", "/register", "/faq",
    "/help", "/support", "/accessibility", "/sitemap", "/cookie",
    "/search", "/events",
)

# Regex to strip trailing location text from concatenated title+location
_LOCATION_SUFFIX_RE = re.compile(
    r"\s*[-–—|·•]\s*(?:Remote|Hybrid|On-?site|Anywhere|Multiple|Worldwide).*$",
    re.IGNORECASE,
)

# Broader location suffix: city/state/country patterns at end of title
# Matches: "- New York, NY", "- San Francisco, CA", "- United States", etc.
_CITY_SUFFIX_RE = re.compile(
    r"\s*[-–—|·•]\s*[A-Z][a-z]+(?:\s[A-Z][a-z]+)*(?:,\s*[A-Z]{2,})?\s*$",
)


# ---------------------------------------------------------------------------
# Title cleaning
# ---------------------------------------------------------------------------


def _clean_title(tag, raw_text: str) -> str:
    """Extract clean job title from a link tag, stripping appended location.

    Strategy:
    1. If the <a> has child elements (span/div), use the first text-bearing
       child as the title (common pattern: title span + location span).
    2. Otherwise, strip known location suffix patterns from the raw text.

    Args:
        tag: BeautifulSoup <a> tag.
        raw_text: Full text from tag.get_text(strip=True).

    Returns:
        Cleaned title string.
    """
    # Strategy 1: Check for structured children (span, div, h2, h3, p)
    title_children = tag.find_all(["span", "div", "h2", "h3", "h4", "p"], recursive=False)
    if title_children:
        first_text = title_children[0].get_text(strip=True)
        if first_text and len(first_text) >= 5:
            return first_text

    # Strategy 2: Regex stripping of location suffixes
    cleaned = _LOCATION_SUFFIX_RE.sub("", raw_text)
    cleaned = _CITY_SUFFIX_RE.sub("", cleaned)
    return cleaned.strip() or raw_text


# ---------------------------------------------------------------------------
# Extraction logic (shared between static and Playwright tiers)
# ---------------------------------------------------------------------------


def _extract_jobs_from_soup(
    soup: BeautifulSoup,
    base_url: str,
    target_titles: list[str],
    exclusions: list[str],
) -> list[dict]:
    """Extract job listings from parsed HTML using JSON-LD and link matching.

    Returns list of dicts with 'title', 'url', 'description' keys.
    Description is always empty — the enrichment pipeline handles JD fetching.

    Args:
        soup: Parsed HTML.
        base_url: Base URL for resolving relative hrefs.
        target_titles: Target title keywords for inclusion filter.
        exclusions: Title keywords for exclusion filter.

    Returns:
        List of matched job dicts. May be empty.
    """
    results = []
    seen_urls: set[str] = set()

    # --- Pass 1: JSON-LD structured data ---
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        postings = _extract_jsonld_postings(data)
        for posting in postings:
            title = posting.get("title", "")
            url = posting.get("url") or posting.get("sameAs") or ""
            if not title:
                continue
            if not _title_matches(title, target_titles, exclusions):
                continue
            if url and url.startswith("/"):
                url = urljoin(base_url, url)
            if url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            results.append({"title": title, "url": url, "description": ""})

    # --- Pass 2: Link text matching ---
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue

        raw_text = tag.get_text(strip=True)
        if not raw_text or len(raw_text) < 4:
            continue

        # Resolve URL
        absolute_url = urljoin(base_url, href)
        parsed = urlparse(absolute_url)

        # Filter out navigation links
        if any(parsed.path.lower().startswith(prefix) for prefix in _NAV_PATH_PREFIXES):
            continue

        # Deduplicate by URL
        if absolute_url in seen_urls:
            continue

        # Clean title and apply keyword filter
        title = _clean_title(tag, raw_text)
        if not _title_matches(title, target_titles, exclusions):
            continue

        seen_urls.add(absolute_url)
        results.append({"title": title, "url": absolute_url, "description": ""})

    return results


def _extract_jsonld_postings(data) -> list[dict]:
    """Recursively extract JobPosting entries from JSON-LD data.

    Handles single objects, arrays, ItemList wrappers, and @graph arrays.

    Args:
        data: Parsed JSON-LD data (dict or list).

    Returns:
        List of dicts with at least 'title' key.
    """
    postings = []
    if isinstance(data, list):
        for item in data:
            postings.extend(_extract_jsonld_postings(item))
    elif isinstance(data, dict):
        dtype = data.get("@type", "")
        if dtype == "JobPosting":
            postings.append(data)
        elif dtype == "ItemList":
            for item in data.get("itemListElement", []):
                postings.extend(_extract_jsonld_postings(item))
        elif "@graph" in data:
            postings.extend(_extract_jsonld_postings(data["@graph"]))
    return postings


# ---------------------------------------------------------------------------
# Tier 1: Static fetch
# ---------------------------------------------------------------------------


def _try_static_extract(
    url: str,
    target_titles: list[str],
    exclusions: list[str],
) -> list[dict] | None:
    """Try extracting jobs from static HTML (no JS rendering).

    Returns:
        list[dict] — extracted jobs (may be empty if page is static but has no matches)
        None — page appears JS-heavy, caller should try Playwright
    """
    try:
        resp = requests.get(url, timeout=_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
    except Exception as e:
        logger.debug("Static fetch failed for '%s': %s", url, e)
        return None  # Can't tell if JS or down — let Playwright try

    html = resp.text
    text_len = len(resp.text.strip())
    if text_len == 0:
        return None

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return None

    # Check if page is JS-heavy (low text content relative to HTML size)
    plain_text = soup.get_text(strip=True)
    ratio = len(plain_text) / max(len(html), 1)

    # Extract jobs regardless — JSON-LD works even on JS-heavy pages
    # if the structured data is embedded in the initial HTML
    jobs = _extract_jobs_from_soup(soup, url, target_titles, exclusions)

    if jobs:
        # Found jobs statically — no need for Playwright
        return jobs

    # No jobs found. Determine if Playwright might help.
    if ratio < _STATIC_TEXT_RATIO or len(plain_text) < _STATIC_MIN_TEXT_LEN:
        # Page looks JS-heavy — signal Playwright
        return None

    # Page has plenty of static text but no matching jobs — genuinely empty
    return []


# ---------------------------------------------------------------------------
# Tier 2: Playwright rendering
# ---------------------------------------------------------------------------


def _try_playwright_extract(
    browser,
    url: str,
    target_titles: list[str],
    exclusions: list[str],
) -> list[dict]:
    """Render page with Playwright and extract jobs from rendered DOM.

    Args:
        browser: Playwright Browser instance (already launched).
        url: Careers page URL to render.
        target_titles: Target title keywords.
        exclusions: Exclusion keywords.

    Returns:
        List of matched job dicts. Empty on timeout/error.
    """
    page = None
    try:
        page = browser.new_page()
        page.goto(url, timeout=_PLAYWRIGHT_TIMEOUT_MS, wait_until="domcontentloaded")
        page.wait_for_timeout(_JS_SETTLE_MS)

        html = page.content()
        soup = BeautifulSoup(html, "html.parser")
        return _extract_jobs_from_soup(soup, url, target_titles, exclusions)

    except Exception as e:
        logger.debug("Playwright render failed for '%s': %s", url, e)
        return []
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def crawl_careers_batch(db_path: str, config: dict) -> dict:
    """Crawl careers pages for miss companies using static fetch + Playwright.

    Thread-safe: creates own sqlite3 connections (standalone_connection pattern).
    TESTING guard: returns early when config.get('TESTING') is True.

    Flow:
    1. Load batch of miss companies with careers_url, ordered by staleness
    2. For each company: try static extraction, fall back to Playwright
    3. For each matched job: create Job object and upsert
    4. Score new jobs via scoring_orchestrator (Haiku → Sonnet)
    5. Log activity and update company timestamps

    Args:
        db_path: Absolute path to the SQLite database file.
        config: Application config dict.

    Returns:
        Summary dict with companies_crawled, jobs_found, jobs_new,
        haiku_scored, sonnet_evaluated, playwright_rendered, errors.
    """
    if config.get("TESTING"):
        logger.debug("crawl_careers_batch: TESTING mode — skipping")
        return {
            "companies_crawled": 0,
            "jobs_found": 0,
            "jobs_new": 0,
            "haiku_scored": 0,
            "sonnet_evaluated": 0,
            "playwright_rendered": 0,
            "errors": [],
        }

    profile_cfg = config.get("profile", {})
    target_titles = profile_cfg.get("target_titles", [])
    exclusions_cfg = profile_cfg.get("exclusions", {})
    title_exclusions = (
        exclusions_cfg.get("title_keywords", [])
        if isinstance(exclusions_cfg, dict)
        else []
    )

    summary = {
        "companies_crawled": 0,
        "jobs_found": 0,
        "jobs_new": 0,
        "haiku_scored": 0,
        "sonnet_evaluated": 0,
        "playwright_rendered": 0,
        "errors": [],
    }
    all_new_job_keys: list[str] = []

    # Load all companies that have ever had a high-scoring job
    with standalone_connection(db_path) as conn:
        from job_finder.config import DEFAULT_HAIKU_THRESHOLD

        freshness_days = config.get("careers_crawl", {}).get(
            "freshness_days", _FRESHNESS_DAYS
        )
        haiku_threshold = config.get("scoring", {}).get(
            "haiku_threshold", DEFAULT_HAIKU_THRESHOLD
        )

        companies = conn.execute(
            """SELECT c.id, c.name_raw, c.careers_url FROM companies c
               WHERE c.careers_url IS NOT NULL
                 AND c.scan_enabled = 1
                 AND (c.careers_crawl_last_at IS NULL
                      OR c.careers_crawl_last_at < datetime('now', ? || ' days'))
                 AND EXISTS (
                     SELECT 1 FROM jobs j
                     WHERE j.company_id = c.id AND j.haiku_score >= ?
                 )
               ORDER BY c.careers_crawl_last_at ASC NULLS FIRST""",
            (f"-{freshness_days}", haiku_threshold),
        ).fetchall()

    if not companies:
        logger.info("careers_crawler: no companies due for crawling")
        return summary

    logger.info("careers_crawler: %d companies in batch", len(companies))

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            _crawl_companies(
                browser, companies, db_path, config,
                target_titles, title_exclusions,
                summary, all_new_job_keys,
            )
        finally:
            browser.close()

    # --- Haiku/Sonnet scoring for newly discovered jobs ---
    if all_new_job_keys:
        _score_new_jobs(db_path, config, all_new_job_keys, summary)

    # --- Activity feed entry ---
    try:
        with standalone_connection(db_path) as conn:
            conn.execute(
                """INSERT INTO runs
                   (timestamp, source, jobs_fetched, jobs_new, jobs_scored)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    datetime.now().isoformat(),
                    "careers_crawl",
                    summary["jobs_found"],
                    summary["jobs_new"],
                    summary["haiku_scored"],
                ),
            )
            conn.commit()
    except Exception as e:
        logger.warning("Failed to insert careers_crawl activity entry: %s", e)

    logger.info(
        "careers_crawler complete: %d crawled, %d found, %d new, "
        "%d playwright, %d haiku-scored",
        summary["companies_crawled"],
        summary["jobs_found"],
        summary["jobs_new"],
        summary["playwright_rendered"],
        summary["haiku_scored"],
    )
    return summary


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _crawl_companies(
    browser,
    companies: list,
    db_path: str,
    config: dict,
    target_titles: list[str],
    title_exclusions: list[str],
    summary: dict,
    all_new_job_keys: list[str],
) -> None:
    """Crawl each company and upsert discovered jobs.

    Modifies summary and all_new_job_keys in place.
    """
    from job_finder.db import upsert_job
    from job_finder.models import Job

    for company in companies:
        company_id = company["id"]
        company_name = company["name_raw"]
        careers_url = company["careers_url"]
        now = datetime.now().isoformat()

        logger.info(
            "careers_crawler: crawling %s via %s", company_name, careers_url,
        )

        try:
            # Tier 1: Static extraction
            jobs = _try_static_extract(careers_url, target_titles, title_exclusions)

            used_playwright = False
            if jobs is None:
                # Tier 2: Playwright rendering
                jobs = _try_playwright_extract(
                    browser, careers_url, target_titles, title_exclusions,
                )
                used_playwright = True
                summary["playwright_rendered"] += 1

            company_jobs_found = len(jobs)
            company_jobs_new = 0
            summary["jobs_found"] += company_jobs_found

            # Upsert each matched job
            with standalone_connection(db_path) as upsert_conn:
                for scraped_job in jobs:
                    try:
                        job = Job(
                            title=scraped_job["title"],
                            company=company_name,
                            location="",
                            source="careers_crawl",
                            source_url=scraped_job.get("url") or "",
                            salary_min=None,
                            salary_max=None,
                            description=scraped_job.get("description", ""),
                        )
                        is_new = upsert_job(upsert_conn, job)
                        if is_new:
                            summary["jobs_new"] += 1
                            company_jobs_new += 1
                            all_new_job_keys.append(job.dedup_key)
                    except Exception as job_err:
                        error_msg = f"{company_name} job error: {job_err}"
                        summary["errors"].append(error_msg)
                        logger.warning("careers_crawler job error: %s", error_msg)

            # Update company timestamps and log scan
            with standalone_connection(db_path) as ts_conn:
                ts_conn.execute(
                    """UPDATE companies
                       SET careers_crawl_last_at = ?,
                           last_scanned_at = ?,
                           jobs_found_total = (
                               SELECT COUNT(*) FROM jobs WHERE company_id = ?
                           )
                       WHERE id = ?""",
                    (now, now, company_id, company_id),
                )
                ts_conn.execute(
                    """INSERT INTO company_scan_log
                       (company_id, scanned_at, jobs_found, jobs_matched)
                       VALUES (?, ?, ?, ?)""",
                    (company_id, now, company_jobs_new, company_jobs_found),
                )
                ts_conn.commit()

            summary["companies_crawled"] += 1

            if company_jobs_found:
                logger.info(
                    "careers_crawler: %s — %d jobs found (%d new)%s",
                    company_name,
                    company_jobs_found,
                    company_jobs_new,
                    " [playwright]" if used_playwright else "",
                )

        except Exception as company_err:
            error_msg = f"{company_name}: {company_err}"
            summary["errors"].append(error_msg)
            logger.error("careers_crawler error for '%s': %s", company_name, company_err)

            # Still update timestamp so company doesn't block the queue
            try:
                with standalone_connection(db_path) as err_conn:
                    err_conn.execute(
                        "UPDATE companies SET careers_crawl_last_at = ? WHERE id = ?",
                        (now, company_id),
                    )
                    err_conn.commit()
            except Exception:
                pass

        time.sleep(_POLITE_DELAY)


def _score_new_jobs(
    db_path: str,
    config: dict,
    new_job_keys: list[str],
    summary: dict,
) -> None:
    """Score newly discovered jobs via Haiku → Sonnet pipeline.

    Same pattern as ats_scanner.py scoring section.
    """
    try:
        from job_finder.web.scoring_orchestrator import (
            load_scoring_profile,
            score_and_persist_haiku,
            score_and_persist_sonnet,
        )
    except ImportError:
        logger.debug("scoring_orchestrator not available — skipping scoring")
        return

    try:
        from job_finder.config import DEFAULT_HAIKU_THRESHOLD
        from job_finder.web.model_provider import tier_has_configured_provider
    except ImportError:
        logger.debug("model_provider not available — skipping scoring")
        return

    # Build scoring client
    _scoring_client = None
    try:
        import anthropic
        _scoring_client = anthropic.Anthropic()
    except (ImportError, Exception):
        pass

    if not tier_has_configured_provider("haiku", config, _scoring_client):
        logger.debug("No routable haiku provider — skipping careers_crawl scoring")
        return

    profile = load_scoring_profile(config)
    threshold = config.get("scoring", {}).get(
        "haiku_threshold", DEFAULT_HAIKU_THRESHOLD
    )
    sonnet_queue: list[str] = []

    with standalone_connection(db_path) as conn:
        for dedup_key in new_job_keys:
            try:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE dedup_key = ?", (dedup_key,)
                ).fetchone()
                if row is None:
                    continue

                result = score_and_persist_haiku(
                    conn, dict(row), config, _scoring_client, profile,
                )
                if result is None:
                    continue
                summary["haiku_scored"] = summary.get("haiku_scored", 0) + 1
                if result.get("score", 0) >= threshold:
                    sonnet_queue.append(dedup_key)
            except Exception as e:
                logger.warning(
                    "careers_crawl Haiku scoring error for '%s': %s", dedup_key, e,
                )

        # Sonnet evaluation for above-threshold jobs
        if sonnet_queue and score_and_persist_sonnet is not None:
            for dedup_key in sonnet_queue:
                try:
                    row = conn.execute(
                        "SELECT * FROM jobs WHERE dedup_key = ?", (dedup_key,)
                    ).fetchone()
                    if row is None:
                        continue
                    job_row = dict(row)
                    if not job_row.get("jd_full"):
                        continue
                    s_result = score_and_persist_sonnet(
                        conn, job_row, config, _scoring_client, profile,
                    )
                    if s_result is not None:
                        summary["sonnet_evaluated"] = (
                            summary.get("sonnet_evaluated", 0) + 1
                        )
                except Exception as e:
                    logger.warning(
                        "careers_crawl Sonnet scoring error for '%s': %s",
                        dedup_key, e,
                    )
