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

import logging
import re
import time
from typing import Optional

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
# Ollama context window limit for validation. Intentionally less than _MAX_JD_CHARS
# (8000) because the validator prompt already consumes tokens and we want to leave
# room for the model's JSON response without truncating mid-reasoning.
_VALIDATE_MAX_CHARS = 6000


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
    """Search DuckDuckGo and return results [{title, href, body}].

    When DDGS exhausts every engine (google, yandex, yahoo, grokipedia, ...)
    it returns an empty list without raising. Previously this was invisible
    to operators — ddgs itself logs engine errors at INFO level from its
    own logger, buried in noise. We surface the aggregate failure at WARNING
    so it's greppable as 'DDGS: all engines returned empty'.
    """
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
    except Exception as exc:
        logger.debug("DDG search failed for '%s': %s", query[:60], exc)
        return []

    if not results:
        logger.warning("DDGS: all engines returned empty for query '%s'", query[:80])
    return results


def _rank_urls(search_results: list[dict]) -> list[str]:
    """Extract, deduplicate, filter, and rank URLs from search results.

    Uses is_blocked_domain and domain_priority from the centralized domain_policy
    module rather than the previously duplicated local _BLOCKED_DOMAINS /
    _PRIORITY_DOMAINS constants. This ensures all callers share the same policy.
    """
    # Imported here to keep module-level imports clean and avoid circular refs
    from job_finder.web.domain_policy import is_blocked_domain, domain_priority

    seen = set()
    urls = []
    for r in search_results:
        href = r.get("href", "")
        if not href or href in seen or is_blocked_domain(href):
            continue
        seen.add(href)
        urls.append(href)

    # Sort by domain priority: lower index = higher priority (ATS platforms first)
    urls.sort(key=domain_priority)
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
    """Fetch a URL with Playwright and return cleaned text content.

    Uses extract_content_from_html() from enrichment_tiers for density-based
    content extraction (trafilatura), falling back to noise-tag stripping.
    Auth-wall detection via is_short_auth_page() and is_chrome_or_login_page().

    LinkedIn URLs are tried with the lightweight fetch_linkedin_jd() extractor
    first (no Playwright needed). Falls through to Playwright if that fails.
    """
    # LinkedIn shortcut: try lightweight extractor first (no Playwright needed)
    if "linkedin.com/jobs/" in url:
        try:
            from job_finder.web.enrichment_tiers import fetch_linkedin_jd
            li_text = fetch_linkedin_jd(url)
            if li_text and len(li_text) >= 300:
                return li_text[:_MAX_JD_CHARS * 2]
        except Exception as exc:
            logger.debug("LinkedIn lightweight extractor failed for %s: %s", url[:80], exc)
        # Fall through to Playwright if LinkedIn extractor fails

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(_PAGE_LOAD_WAIT_MS)

        html = page.content()

        from job_finder.web.enrichment_tiers import (
            extract_content_from_html,
            is_short_auth_page,
            is_chrome_or_login_page,
        )
        text = extract_content_from_html(html)
        if not text:
            return None

        if is_short_auth_page(text):
            logger.debug("Short auth-wall detected on %s", url[:80])
            return None

        if is_chrome_or_login_page(text):
            logger.debug("Chrome/login page detected on %s", url[:80])
            return None

        return text[:_MAX_JD_CHARS * 2]  # Keep extra for validation, trim later

    except Exception as exc:
        logger.debug("Playwright fetch failed for %s: %s", url[:80], exc)
        return None


# ---------------------------------------------------------------------------
# OllamaProvider-backed LLM calls
# _call_ollama() deleted — all LLM calls now go through OllamaProvider which
# is instantiated once in run_agentic_backfill() and passed down. This ensures:
# 1. Consistent routing through the multi-provider infrastructure
# 2. ModelResult.data is already parsed JSON (no redundant json.loads())
# 3. Health check happens exactly once at startup (not per-job)
# ---------------------------------------------------------------------------


def _generate_queries(
    title: str,
    company: str,
    n: int,
    provider,  # OllamaProvider — typed as Any to avoid import at module level
    model: str = "qwen2.5:14b",
) -> list[str]:
    """Ask OllamaProvider to generate search queries for a job posting.

    Args:
        title: Job title.
        company: Company name.
        n: Number of queries to generate.
        provider: OllamaProvider instance (passed from run_agentic_backfill).
        model: Ollama model to use.

    Returns:
        List of search query strings. Falls back to heuristic queries on failure.
    """
    system = _QUERY_GEN_PROMPT.format(n=n)
    user_msg = f"Job title: {title}\nCompany: {company}"
    max_tokens = 512

    # Inner try/except: handles mid-run transient failures (model timeout,
    # malformed JSON from a specific query) without crashing the outer loop.
    try:
        result = provider.call(
            model, system, [{"role": "user", "content": user_msg}], max_tokens=max_tokens
        )
        # result.data is already parsed as a dict by OllamaProvider.call() —
        # do NOT call json.loads() on it (would raise TypeError on dict input).
        data = result.data
    except Exception as exc:
        # DEFECT 019 FIX: log at WARNING (not swallowed) so operators can distinguish
        # transient Ollama failures (503, timeout) from normal parse-failure fallback.
        logger.warning(
            "OllamaProvider provider error in _generate_queries for '%s' @ '%s': %s — "
            "falling back to heuristic queries",
            title[:40], company[:20], exc,
        )
        return _fallback_queries(title, company)

    # Handle both list and dict response shapes from Ollama
    if isinstance(data, list) and all(isinstance(q, str) for q in data):
        return data[:n]
    if isinstance(data, dict):
        for key in ("queries", "search_queries", "results"):
            if key in data and isinstance(data[key], list):
                return [str(q) for q in data[key][:n]]

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
    text: str,
    title: str,
    company: str,
    model: str,
    provider,  # OllamaProvider
) -> tuple[bool, float]:
    """Ask OllamaProvider if page content matches the target job.

    Args:
        text: Page text to validate (will be truncated to keep context reasonable).
        title: Target job title.
        company: Target company name.
        model: Ollama model tag.
        provider: OllamaProvider instance passed from enrich_single_job.

    Returns:
        Tuple of (is_match, confidence). Returns (False, 0.0) on any failure.
    """
    system = _VALIDATE_PROMPT.format(title=title, company=company)
    # Truncate page text to _VALIDATE_MAX_CHARS (not _MAX_JD_CHARS) to leave
    # token budget for the model's JSON response without truncating mid-reasoning.
    user_msg = text[:_VALIDATE_MAX_CHARS]

    # Inner try/except: handles mid-run transient failures per-URL
    try:
        result = provider.call(
            model, system, [{"role": "user", "content": user_msg}], max_tokens=256
        )
        # result.data is already a parsed dict — no json.loads() needed
        data = result.data
    except Exception as exc:
        logger.warning("OllamaProvider call failed in _validate_page: %s", exc)
        return False, 0.0

    try:
        is_match = bool(data.get("is_match", False))
        confidence = float(data.get("confidence", 0.0))
        reason = data.get("reason", "")
        if reason:
            logger.debug("Validation: match=%s conf=%.2f reason=%s", is_match, confidence, reason)
        return is_match, confidence
    except (TypeError, ValueError, AttributeError):
        return False, 0.0


# ---------------------------------------------------------------------------
# Main agentic loop (per job)
# ---------------------------------------------------------------------------


def enrich_single_job(
    job_row: dict,
    page,
    model: str,
    provider,  # OllamaProvider — passed from run_agentic_backfill
) -> Optional[str]:
    """Run the agentic enrichment loop for a single job.

    Args:
        job_row: Job dict with title, company fields.
        page: Playwright page object (reused across jobs).
        model: Ollama model to use for query gen + validation.
        provider: OllamaProvider instance instantiated once in run_agentic_backfill.
            Passed down to _generate_queries() and _validate_page() so each
            function can call provider.call() without needing its own LLM setup.

    Returns:
        The job description text if found, None otherwise.
    """
    title = job_row.get("title", "")
    company = job_row.get("company", "")

    if not title or not company:
        return None

    # Step 1: Generate search queries via OllamaProvider
    queries = _generate_queries(title, company, n=_MAX_SEARCH_QUERIES, provider=provider, model=model)
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

    # Failure reason counters for observability
    fetch_ok = 0
    company_miss = 0
    low_conf = 0
    auth_walls = 0

    for i, url in enumerate(all_urls[:_MAX_FETCH_ATTEMPTS]):
        text = _fetch_page_text(page, url)
        if not text:
            auth_walls += 1
            continue

        fetch_ok += 1

        # Quick heuristic: verify at least one meaningful company token appears in
        # the page before paying Ollama inference cost.
        # Uses shared company_tokens() + company_name_in_text() from enrichment_tiers
        # (same logic used by fetch_ddg_jds for DDG tier validation).
        from job_finder.web.enrichment_tiers import company_tokens as _company_tokens, company_name_in_text
        tokens = _company_tokens(company)
        if not tokens:
            # DEFECT 015 FIX: fail CLOSED — degenerate company name (all stop-words).
            # Skip rather than burn inference budget on a heuristic that cannot operate.
            logger.debug(
                "Agentic: skipping %s (company '%s' yields no meaningful tokens)",
                url[:60], company[:30],
            )
            company_miss += 1
            continue
        if not company_name_in_text(company, text):
            # Bypass for long pages with short company names — worth the Ollama cost
            if len(tokens) <= 2 and len(text) > 2000:
                logger.debug("Agentic: bypassing company check for long page %s", url[:60])
            else:
                logger.debug("Agentic: skipping %s (company name not found)", url[:60])
                company_miss += 1
                continue

        # Validate with OllamaProvider — provider passed through from caller
        is_match, confidence = _validate_page(text, title, company, model=model, provider=provider)

        if is_match and confidence > best_confidence:
            best_text = text
            best_confidence = confidence
            if confidence >= 0.8:
                logger.info("Agentic: high-confidence match at %s (%.2f)", url[:60], confidence)
                break
        elif not is_match:
            low_conf += 1

    # Log failure breakdown at INFO level for observability
    logger.info(
        "Agentic: '%s' @ '%s' — urls=%d, fetched=%d, company_mismatch=%d, "
        "low_confidence=%d, auth_wall=%d",
        title[:40], company[:20], len(all_urls), fetch_ok, company_miss,
        low_conf, auth_walls,
    )

    if best_text and best_confidence >= 0.5:
        # Trim to JD limit
        return best_text[:_MAX_JD_CHARS]

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

    Architecture notes:
    - OllamaProvider is instantiated ONCE here at the top, guarded by
      try/except (ImportError, RuntimeError). If Ollama is unreachable,
      returns 0 cleanly without crashing — callers (_make_tracked_job) see
      a successful return with no side effects.
    - DB connections are scoped per-operation (short SELECT + per-job UPDATE)
      rather than held open across minutes of Playwright network I/O. This
      prevents SQLite lock contention with the Flask request thread.
    - Optimistic concurrency UPDATE prevents overwriting state changed by
      another process between SELECT and write (checks enrichment_tier = 'exhausted').

    Args:
        db_path: Path to SQLite database.
        config: Application config dict. OllamaProvider reads
            config['providers']['ollama']['base_url'] (default: localhost:11434).
        limit: Maximum jobs to process.
        model: Ollama model for query gen + validation.

    Returns:
        Number of jobs successfully enriched. Always returns 0 when
        prerequisites (Ollama, Playwright) are unavailable.
    """
    # Guard: instantiate OllamaProvider and import Playwright before any DB or
    # network work. ImportError covers missing playwright/ollama packages;
    # RuntimeError covers Ollama service unreachable (OllamaProvider._check_health).
    try:
        from job_finder.web.providers.ollama_provider import OllamaProvider
        from playwright.sync_api import sync_playwright
        from job_finder.web.db_helpers import standalone_connection
        provider = OllamaProvider(config=config)
    except (ImportError, RuntimeError) as exc:
        logger.warning("Agentic backfill unavailable: %s", exc)
        return 0

    # Short-lived SELECT: open connection, fetch rows, close before Playwright work.
    # Holding the connection open across minutes of network I/O is unsafe for
    # concurrent SQLite (WAL mode helps but doesn't eliminate lock contention).
    with standalone_connection(db_path) as conn:
        # v3.0 (Phase 34 Plan 3 Commit A): ORDER BY classification_rank + sub_score_sum
        # replaces ORDER BY haiku_score. Highest-priority (apply) rows processed first.
        rows = conn.execute(
            """SELECT * FROM jobs
               WHERE enrichment_tier = 'exhausted'
                 AND jd_full IS NULL
               ORDER BY
                   CASE classification
                       WHEN 'apply'    THEN 4
                       WHEN 'consider' THEN 3
                       WHEN 'skip'     THEN 2
                       WHEN 'reject'   THEN 1
                       ELSE 0
                   END DESC,
                   (COALESCE(json_extract(sub_scores_json, '$.title_fit'), 0) +
                    COALESCE(json_extract(sub_scores_json, '$.location_fit'), 0) +
                    COALESCE(json_extract(sub_scores_json, '$.comp_fit'), 0) +
                    COALESCE(json_extract(sub_scores_json, '$.domain_match'), 0) +
                    COALESCE(json_extract(sub_scores_json, '$.seniority_match'), 0) +
                    COALESCE(json_extract(sub_scores_json, '$.skills_match'), 0)) DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()

    if not rows:
        # DEFECT 018 FIX: emit same structured summary as normal exit so monitoring
        # rules have a single log pattern to match ("Agentic enrichment complete").
        logger.info("Agentic enrichment complete: 0/0 jobs enriched (no exhausted jobs)")
        return 0

    total = len(rows)
    logger.info("Agentic enrichment: %d jobs to process", total)

    enriched_count = 0

    with sync_playwright() as pw:
        browser, page = _create_browser(pw)

        try:
            for i, row in enumerate(rows, 1):
                job = dict(row)
                title = job.get("title", "?")[:55]
                company = job.get("company", "?")[:25]
                dedup_key = job.get("dedup_key")

                logger.info("[%d/%d] %s @ %s", i, total, title, company)

                t0 = time.time()
                # Provider passed through: run_agentic_backfill -> enrich_single_job
                # -> _generate_queries / _validate_page. Single instantiation of
                # OllamaProvider shared across all jobs in this batch run.
                jd = enrich_single_job(job, page, model=model, provider=provider)
                elapsed = time.time() - t0

                if jd:
                    # Per-job write connection: open, UPDATE with optimistic concurrency
                    # check, close. The WHERE clause prevents overwriting state changed
                    # by another process between our initial SELECT and this write.
                    # DEFECT 001 FIX: capture rowcount INSIDE the `with` block as a plain
                    # int before the connection closes. Reading cursor.rowcount after
                    # standalone_connection.__exit__() is implementation-defined behaviour.
                    with standalone_connection(db_path) as write_conn:
                        cursor = write_conn.execute(
                            "UPDATE jobs SET jd_full = ?, enrichment_tier = 'agentic' "
                            "WHERE dedup_key = ? AND enrichment_tier = 'exhausted'",
                            (jd, dedup_key),
                        )
                        write_conn.commit()
                        rows_updated = cursor.rowcount  # read while connection is open

                    if rows_updated == 0:
                        # Another process advanced enrichment_tier between our SELECT
                        # and this UPDATE. Log WARNING so the operator can manually
                        # persist the JD if needed (we have it in memory here).
                        logger.warning(
                            "Agentic: optimistic concurrency miss for dedup_key=%s "
                            "(JD found, %d chars, but tier changed — not persisted)",
                            dedup_key,
                            len(jd),
                        )
                    else:
                        enriched_count += 1
                        logger.info("  -> FOUND %d chars (%.1fs)", len(jd), elapsed)
                else:
                    # Mark as agentic-exhausted so we don't retry.
                    # If rowcount == 0 here, another process already advanced the tier
                    # — skip silently (no data was found anyway, so no recovery needed).
                    with standalone_connection(db_path) as write_conn:
                        write_conn.execute(
                            "UPDATE jobs SET enrichment_tier = 'agentic_exhausted' "
                            "WHERE dedup_key = ? AND enrichment_tier = 'exhausted'",
                            (dedup_key,),
                        )
                        write_conn.commit()
                    logger.info("  -> NOT FOUND (%.1fs)", elapsed)

        finally:
            browser.close()

    # DEFECT 008 FIX: guard division with `total or 1` so a future refactor that
    # removes the early-exit guard cannot cause ZeroDivisionError here.
    logger.info(
        "Agentic enrichment complete: %d/%d jobs enriched (%.0f%%)",
        enriched_count, total, 100 * enriched_count / (total or 1),
    )
    return enriched_count
