"""Data enrichment module for sparse job records.

Cost-ordered enrichment pipeline with 7 tiers. Each tier is only attempted
after all cheaper tiers have been exhausted. Per-job enrichment_tier column
tracks the highest tier attempted so future calls resume from the next tier.

Enrichment tiers (in order):
  1. free — Direct URL fetch, ATS API query, HTML careers scrape
  2. ddg  — DuckDuckGo Instant Answer API (free, no key)
  3. haiku — Haiku extraction from accumulated fragments
  4. serpapi — SerpAPI Google Jobs search (paid, optional key)
  5. sonnet — Sonnet deep extraction from all accumulated fragments
  6. exhausted — All tiers attempted; never re-enrich

Per-field cost ceilings:
  jd_full:    escalates all the way to sonnet (critical for AI scoring)
  salary_min: capped at haiku (not worth SerpAPI/Sonnet for salary alone)
  salary_max: capped at haiku

Company info enrichment (for Sonnet-scored jobs only) uses DuckDuckGo.

Design principles:
  - Never raises — all errors are caught and logged.
  - Returns empty dict when nothing can be enriched.
  - Skips enrichment when job already has all scoring-relevant data.
  - Persists enrichment_tier atomically with enriched fields in one UPDATE.
  - Jobs with enrichment_tier set resume from the NEXT tier up.
  - Exhausted jobs are returned immediately without any API calls.

Exports:
    TIER_ORDER: Ordered list of enrichment tier names.
    enrich_job: Enrich a sparse job record with cost-ordered tier fallback.
    enrich_company_info: Enrich company metadata via DuckDuckGo.
    run_enrichment_backfill: Backfill unenriched jobs from the DB.
"""

import json
import logging
import re
from typing import Optional, Any

import requests
from bs4 import BeautifulSoup

from job_finder.config import DEFAULT_MODEL_HAIKU, DEFAULT_MODEL_SONNET
from job_finder.web.claude_client import call_claude, cost_gate

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Strict cost ordering: free (URL -> ATS -> careers) -> DDG -> Haiku -> SerpAPI -> Sonnet
TIER_ORDER = ["free", "ddg", "haiku", "serpapi", "sonnet", "exhausted"]

# Allowlist of jobs table columns that _persist() may write. Prevents AI-extracted
# dict keys from injecting arbitrary column names into dynamic SQL SET clauses.
_ENRICHABLE_COLUMNS = frozenset({"jd_full", "salary_min", "salary_max", "location"})

# Per-field cost ceilings: highest tier allowed to search for this field.
# After this tier fails for a field, it is abandoned (not escalated further).
FIELD_TIER_CEILINGS = {
    "jd_full": "sonnet",      # worth escalating all the way (critical for scoring)
    "salary_min": "haiku",    # cap at Haiku — not worth SerpAPI/Sonnet for salary alone
    "salary_max": "haiku",
}

# DuckDuckGo Instant Answer API endpoint
_DDG_API_URL = "https://api.duckduckgo.com/"

# SerpAPI Google Jobs endpoint
_SERPAPI_URL = "https://serpapi.com/search.json"

# HTTP headers for external requests
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; JobFinder/1.0; +https://github.com/job-finder)"
    )
}

# Tags to strip from HTML before extracting text
_NOISE_TAGS = ["script", "style", "nav", "footer", "header", "noscript", "aside"]

# Maximum characters to return from direct JD fetch
_MAX_JD_CHARS = 8000

# Auth-wall signatures: if page text contains any of these (case-insensitive),
# the fetched page is a login/CAPTCHA wall, not a real JD. Return None.
_AUTH_WALL_SIGNATURES = [
    "we're signing you in",
    "sign in or join",
    "please verify you are a human",
    "access denied",
]

# Timeout for external API calls (seconds)
_TIMEOUT = 10


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def enrich_job(
    job_row: dict,
    serpapi_key: Optional[str] = None,
    anthropic_client: Any = None,
    conn: Any = None,
    config: Optional[dict] = None,
) -> dict:
    """Enrich a sparse job record using the cost-ordered tier pipeline.

    Tiers: free (URL -> ATS -> careers) -> DDG -> Haiku -> SerpAPI -> Sonnet.
    Resumes from the next tier after job_row['enrichment_tier'] if set.
    Returns {} immediately for exhausted jobs.

    Persists enrichment_tier + enriched fields atomically to DB after each
    tier that produces data (if conn is provided). Returns the enriched dict.

    Args:
        job_row: Job record dict. Must have 'title' and 'company'.
        serpapi_key: Optional SerpAPI API key for SerpAPI tier.
        anthropic_client: Optional Anthropic client for Haiku/Sonnet tiers.
        conn: Optional SQLite connection for DB persistence and cost recording.
        config: Optional application config dict.

    Returns:
        Dict of enriched fields to UPDATE into the jobs table.
        Returns empty dict if nothing was enriched or job already has data.
    """
    if config is None:
        config = {}

    try:
        # Exhausted jobs: skip immediately
        current_tier = job_row.get("enrichment_tier")
        if current_tier == "exhausted":
            return {}

        # Auto-promote long descriptions to jd_full (DQ-02)
        if not job_row.get("jd_full") and job_row.get("description") and len(job_row["description"]) > 200:
            job_row["jd_full"] = job_row["description"]
            if conn is not None and job_row.get("dedup_key"):
                try:
                    conn.execute(
                        "UPDATE jobs SET jd_full = ? WHERE dedup_key = ? AND jd_full IS NULL",
                        (job_row["description"][:8000], job_row.get("dedup_key")),
                    )
                    conn.commit()
                except Exception as e:
                    logger.debug("Description promotion DB write failed: %s", e)

        # Check if enrichment is needed
        missing = _find_missing_fields(job_row)
        if not missing:
            return {}

        # Determine start tier (resume from next tier after last attempted)
        start_idx = _start_tier_index(current_tier)

        title = job_row.get("title", "")
        company = job_row.get("company", "")

        # Accumulate fragments across tiers (each tier adds its text/data)
        fragments: dict = {}

        # ---------------------------------------------------------------
        # Tier 0: free — URL fetch + ATS API + careers scrape
        # ---------------------------------------------------------------
        if start_idx <= TIER_ORDER.index("free"):
            try:
                # Sub-tier A: Direct URL fetch
                source_urls = _parse_source_urls(job_row.get("source_urls"))
                for url in source_urls:
                    jd_text = _fetch_direct_jd(url)
                    if jd_text:
                        fragments["url_jd"] = jd_text
                        break

                # Sub-tier B: ATS API query (if company has confirmed ATS slug)
                if conn is not None and job_row.get("company_id"):
                    ats_result = _query_ats_api(job_row, conn, config)
                    if ats_result:
                        fragments.update(ats_result)

                # Sub-tier C: HTML careers scrape (if company has homepage_url)
                if conn is not None and job_row.get("company_id"):
                    careers_result = _scrape_careers(job_row, conn, config)
                    if careers_result:
                        # Don't overwrite ATS result
                        for k, v in careers_result.items():
                            if k not in fragments:
                                fragments[k] = v

                # Resolve what free tier found
                enriched = _resolve_from_fragments(fragments, missing, job_row)
                if enriched:
                    _persist(conn, job_row, enriched, "free")
                    return enriched

            except Exception as e:
                logger.debug("Free tier enrichment failed for '%s': %s", title, e)

        # ---------------------------------------------------------------
        # Tier 1: ddg — DuckDuckGo Instant Answer API
        # ---------------------------------------------------------------
        # Check if remaining missing fields are all below DDG (nothing to do)
        remaining = _find_missing_fields({**job_row, **_resolve_from_fragments(fragments, missing, job_row)})
        if not remaining:
            enriched = _resolve_from_fragments(fragments, missing, job_row)
            _persist(conn, job_row, enriched, "free")
            return enriched

        if start_idx <= TIER_ORDER.index("ddg"):
            try:
                query = f"{title} {company} job description"
                ddg_text = _search_duckduckgo(query)
                if ddg_text:
                    fragments["ddg"] = ddg_text

                # Resolve what DDG tier found (via Haiku extraction later if needed)
                # DDG doesn't directly provide structured data; it feeds the Haiku tier.
                # If DDG returned nothing, we still continue to Haiku with empty ddg fragment.

            except Exception as e:
                logger.debug("DDG tier failed for '%s': %s", title, e)

        # ---------------------------------------------------------------
        # Tier 2: haiku — Extract structured data from accumulated fragments
        # ---------------------------------------------------------------
        if start_idx <= TIER_ORDER.index("haiku") and anthropic_client is not None:
            try:
                # Compose search text from all fragments collected so far
                search_input = _compose_fragment_text(fragments, title, company)
                haiku_result = _extract_with_haiku(
                    search_input, job_row, anthropic_client, conn, config
                )
                if haiku_result:
                    for k, v in haiku_result.items():
                        fragments[k] = v

                # Check what is still missing after Haiku
                salary_fields = {"salary_min", "salary_max"}
                enriched_so_far = _resolve_from_fragments(fragments, missing, job_row)
                still_missing_after_haiku = [
                    f for f in missing if f not in enriched_so_far
                ]

                if not still_missing_after_haiku:
                    # All fields satisfied — return now
                    _persist(conn, job_row, enriched_so_far, "haiku")
                    return enriched_so_far

                # Check salary ceiling: if ONLY salary fields remain missing after Haiku,
                # stop escalating (salary ceiling is Haiku).
                if all(f in salary_fields for f in still_missing_after_haiku):
                    # Only salary remains missing — don't escalate to SerpAPI/Sonnet for salary
                    _persist(conn, job_row, enriched_so_far if enriched_so_far else {}, "haiku")
                    return enriched_so_far

                # Otherwise, some non-salary field (jd_full) is still missing —
                # partial results from Haiku are accumulated in fragments for use by
                # SerpAPI/Sonnet; continue escalation.

            except Exception as e:
                logger.debug("Haiku tier failed for '%s': %s", title, e)

        # ---------------------------------------------------------------
        # Tier 3: serpapi — Google Jobs search (paid)
        # ---------------------------------------------------------------
        # SerpAPI only runs if JD is still missing (salary ceiling is Haiku)
        jd_still_missing = not (
            job_row.get("jd_full")
            or fragments.get("url_jd")
            or fragments.get("jd_full")
        )

        if start_idx <= TIER_ORDER.index("serpapi") and serpapi_key and jd_still_missing:
            try:
                query = f"{title} {company}"
                serpapi_result = _search_serpapi(query, serpapi_key)
                if serpapi_result:
                    for k, v in serpapi_result.items():
                        if k not in fragments:
                            fragments[k] = v

                    enriched = _resolve_from_fragments(
                        {**fragments, **serpapi_result}, missing, job_row
                    )
                    if enriched:
                        _persist(conn, job_row, enriched, "serpapi")
                        return enriched

            except Exception as e:
                logger.debug("SerpAPI tier failed for '%s': %s", title, e)

        # ---------------------------------------------------------------
        # Tier 4: sonnet — Deep extraction from all accumulated fragments
        # ---------------------------------------------------------------
        # Sonnet only runs if JD is still missing
        jd_still_missing = not (
            job_row.get("jd_full")
            or fragments.get("url_jd")
            or fragments.get("jd_full")
        )

        if (
            start_idx <= TIER_ORDER.index("sonnet")
            and anthropic_client is not None
            and jd_still_missing
        ):
            try:
                # Check cost gate before calling Sonnet
                gate_ok = cost_gate(conn, config, "sonnet")
                if gate_ok:
                    sonnet_result = _extract_with_sonnet(
                        fragments, job_row, anthropic_client, conn, config
                    )
                    if sonnet_result:
                        enriched = _filter_non_none(sonnet_result)
                        if enriched:
                            _persist(conn, job_row, enriched, "sonnet")
                            return enriched

            except Exception as e:
                logger.debug("Sonnet tier failed for '%s': %s", title, e)

        # All tiers exhausted
        _persist(conn, job_row, {}, "exhausted")
        return {}

    except Exception as e:
        logger.warning("enrich_job failed for '%s': %s", job_row.get("title"), e)
        return {}


def enrich_company_info(company_name: str) -> dict:
    """Enrich company info via DuckDuckGo (for Sonnet-scored jobs only).

    Returns dict with optional keys: company_size, industry, funding_stage.
    Best-effort — returns empty dict on failure. DDG reliability is LOW per
    research (sparse company data), so callers should not depend on results.

    Args:
        company_name: The company name to look up.

    Returns:
        Dict with any of: company_size (str), industry (str), funding_stage (str).
        May be empty if no data found.
    """
    try:
        query = f"{company_name} company size employees industry"
        ddg_text = _search_duckduckgo(query)
        if not ddg_text:
            return {}

        result = {}

        # Extract employee count pattern: "X employees" or "X,000 employees"
        employee_match = re.search(
            r"(\d[\d,]*)\s*(?:to\s*\d[\d,]*)?\s+employees?", ddg_text, re.IGNORECASE
        )
        if employee_match:
            count_str = employee_match.group(1).replace(",", "")
            try:
                count = int(count_str)
                if count < 50:
                    result["company_size"] = "startup"
                elif count < 500:
                    result["company_size"] = "small"
                elif count < 5000:
                    result["company_size"] = "mid-size"
                else:
                    result["company_size"] = "large"
            except ValueError:
                pass

        # Extract industry keywords
        industry_keywords = {
            "software": ["software", "saas", "tech", "technology", "platform"],
            "finance": ["finance", "fintech", "banking", "financial"],
            "healthcare": ["healthcare", "health", "medical", "pharma"],
            "e-commerce": ["e-commerce", "ecommerce", "retail", "marketplace"],
            "media": ["media", "entertainment", "streaming", "content"],
        }
        text_lower = ddg_text.lower()
        for industry, keywords in industry_keywords.items():
            if any(kw in text_lower for kw in keywords):
                result["industry"] = industry
                break

        return result

    except Exception as e:
        logger.debug("enrich_company_info failed for '%s': %s", company_name, e)
        return {}


def run_enrichment_backfill(
    db_path: str,
    serpapi_key: Optional[str] = None,
    config: Optional[dict] = None,
    limit: int = 100,
) -> int:
    """Backfill unenriched jobs using the cost-ordered tier pipeline.

    Queries jobs where enrichment_tier IS NULL or in a resumable state
    (not 'exhausted', 'serpapi', or 'sonnet' — those are already done).
    Processes up to `limit` jobs per call.

    Args:
        db_path: Absolute path to the SQLite database file.
        serpapi_key: Optional SerpAPI API key.
        config: Optional application config dict.
        limit: Max number of jobs to process per call.

    Returns:
        Number of jobs that were enriched (had fields added).
    """
    import sqlite3

    if config is None:
        config = {}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        rows = conn.execute(
            """SELECT * FROM jobs
               WHERE enrichment_tier IS NULL
                  OR enrichment_tier NOT IN ('exhausted', 'serpapi', 'sonnet')
               LIMIT ?""",
            (limit,),
        ).fetchall()

        enriched_count = 0
        for row in rows:
            job_row = dict(row)
            result = enrich_job(
                job_row,
                serpapi_key=serpapi_key,
                conn=conn,
                config=config,
            )
            if result:
                enriched_count += 1

        return enriched_count

    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Private helpers: tier execution
# ---------------------------------------------------------------------------


def _fetch_direct_jd(url: str) -> Optional[str]:
    """Attempt a direct HTTP GET and return cleaned job description text.

    Strips noisy HTML tags and returns cleaned text capped at 8000 chars.

    Args:
        url: The job URL to fetch.

    Returns:
        Cleaned text up to 8000 chars, or None on any error.
    """
    try:
        response = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        # Strip noisy tags
        for tag in soup.find_all(_NOISE_TAGS):
            tag.decompose()

        text = soup.get_text(separator="\n", strip=True)

        # Reject auth-wall / CAPTCHA pages that return login HTML instead of JD
        text_lower = text.lower()
        if any(sig in text_lower for sig in _AUTH_WALL_SIGNATURES):
            logger.debug("Auth-wall detected for '%s', rejecting", url)
            return None

        return text[:_MAX_JD_CHARS] if text.strip() else None

    except Exception as e:
        logger.debug("Direct fetch failed for '%s': %s", url, e)
        return None


def _query_ats_api(job_row: dict, conn: Any, config: dict) -> dict:
    """Query ATS API (Lever/Greenhouse/Ashby) for job data if company has a slug.

    Looks up the company record from the DB. If ats_probe_status='hit',
    calls the appropriate ATS scan function with a loose title match derived
    from significant words in the job title.

    Args:
        job_row: Job row dict with company_id field.
        conn: Open SQLite connection.
        config: Application config dict.

    Returns:
        Dict with any of: jd_full, salary_min, salary_max. Empty if not found.
    """
    try:
        company_id = job_row.get("company_id")
        if not company_id:
            return {}

        company_row = conn.execute(
            "SELECT ats_platform, ats_slug, ats_probe_status FROM companies WHERE id = ?",
            (company_id,),
        ).fetchone()

        if not company_row:
            return {}

        if dict(company_row).get("ats_probe_status") != "hit":
            return {}

        platform = dict(company_row).get("ats_platform")
        slug = dict(company_row).get("ats_slug")
        if not platform or not slug:
            return {}

        # Derive loose target titles from significant words in job title
        title = job_row.get("title", "")
        target_titles = [w for w in title.split() if len(w) > 3]

        exclusions = config.get("scoring", {}).get("exclusions", [])

        # Lazy import with ImportError guard
        try:
            from job_finder.web.ats_scanner import scan_lever, scan_greenhouse, scan_ashby
        except ImportError:
            return {}

        postings = []
        if platform == "lever":
            postings = scan_lever(slug, target_titles, exclusions)
        elif platform == "greenhouse":
            postings = scan_greenhouse(slug, target_titles, exclusions)
        elif platform == "ashby":
            postings = scan_ashby(slug, target_titles, exclusions)

        if not postings:
            return {}

        # Take the first matching posting
        posting = postings[0]
        result = {}
        if posting.get("description"):
            result["jd_full"] = posting["description"][:_MAX_JD_CHARS]
        if posting.get("salary_min"):
            result["salary_min"] = posting["salary_min"]
        if posting.get("salary_max"):
            result["salary_max"] = posting["salary_max"]

        return result

    except Exception as e:
        logger.debug("ATS API query failed: %s", e)
        return {}


def _scrape_careers(job_row: dict, conn: Any, config: dict) -> dict:
    """Scrape company careers page for matching job listing.

    Looks up company homepage_url from DB. If found, uses find_careers_url
    and scrape_careers_page to extract JD from the HTML careers page.

    Args:
        job_row: Job row dict with company_id field.
        conn: Open SQLite connection.
        config: Application config dict.

    Returns:
        Dict with any of: jd_full. Empty if not found.
    """
    try:
        company_id = job_row.get("company_id")
        if not company_id:
            return {}

        company_row = conn.execute(
            "SELECT homepage_url FROM companies WHERE id = ?",
            (company_id,),
        ).fetchone()

        if not company_row:
            return {}

        homepage_url = dict(company_row).get("homepage_url")
        if not homepage_url:
            return {}

        # Lazy import with ImportError guard
        try:
            from job_finder.web.careers_scraper import find_careers_url, scrape_careers_page
        except ImportError:
            return {}

        careers_url = find_careers_url(homepage_url)
        if not careers_url:
            return {}

        title = job_row.get("title", "")
        target_titles = [w for w in title.split() if len(w) > 3]
        exclusions = config.get("scoring", {}).get("exclusions", [])

        postings = scrape_careers_page(careers_url, target_titles, exclusions)
        if not postings:
            return {}

        posting = postings[0]
        result = {}
        if posting.get("description"):
            result["jd_full"] = posting["description"][:_MAX_JD_CHARS]

        return result

    except Exception as e:
        logger.debug("Careers scrape failed: %s", e)
        return {}


def _extract_with_sonnet(
    fragments: dict,
    job_row: dict,
    client: Any,
    conn: Any,
    config: dict,
) -> dict:
    """Use Sonnet to deep-extract structured data from ALL accumulated fragments.

    Assembles all text fragments from prior tiers into a single context string
    (budget 4000-6000 chars). Prompts Sonnet to extract jd_full and salary
    from sparse/fragmented signals.

    Args:
        fragments: Dict of all text fragments accumulated from prior tiers.
        job_row: Job row dict for context (title, company).
        client: Anthropic client instance.
        conn: SQLite connection for cost recording.
        config: Application config dict.

    Returns:
        Dict with any of: jd_full, salary_min, salary_max. Empty dict on failure.
    """
    try:
        title = job_row.get("title", "")
        company = job_row.get("company", "")

        # Assemble all fragments into context string (cap at 5000 chars total)
        context_parts = []
        for key, text in fragments.items():
            if text and isinstance(text, str):
                label = key.replace("_", " ").upper()
                context_parts.append(f"[{label}]\n{str(text)[:1500]}")
        context_text = "\n\n".join(context_parts)[:5000]

        if not context_text:
            context_text = f"Job posting: {title} at {company}"

        system_prompt = (
            "You are an expert job data extractor. Given fragments of information "
            "about a job posting (from web searches, ATS APIs, and careers pages), "
            "extract structured information as a JSON object. "
            "Extract only what is explicitly stated — do not invent data. "
            "Return ONLY a JSON object with these optional fields: "
            "jd_full (string, full job description), "
            "salary_min (integer, USD annual), "
            "salary_max (integer, USD annual). "
            "Omit fields that cannot be determined from the provided text."
        )

        user_prompt = (
            f"Job: {title} at {company}\n\n"
            f"Information fragments from multiple sources:\n{context_text}\n\n"
            f"Extract job details as JSON. Include only fields that are explicitly mentioned."
        )

        model = (
            config.get("scoring", {})
            .get("models", {})
            .get("sonnet", DEFAULT_MODEL_SONNET)
        )

        job_id = job_row.get("dedup_key")

        result, _cost = call_claude(
            client=client,
            model=model,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            output_schema=None,
            conn=conn,
            job_id=job_id,
            purpose="enrich_job_sonnet",
            config=config,
            max_tokens=1024,
        )

        if isinstance(result, dict):
            enriched = {}
            for key, value in result.items():
                if value is not None and key in ("jd_full", "salary_min", "salary_max"):
                    if key in ("salary_min", "salary_max") and isinstance(value, (int, float)):
                        enriched[key] = int(value)
                    elif key == "jd_full" and isinstance(value, str) and value.strip():
                        enriched[key] = value
            return enriched

        return {}

    except Exception as e:
        logger.debug("Sonnet extraction failed: %s", e)
        return {}


def _search_serpapi(query: str, api_key: str) -> Optional[dict]:
    """Search Google Jobs via SerpAPI for job details.

    Args:
        query: Search query string (e.g., "Data Scientist Acme Corp").
        api_key: SerpAPI API key.

    Returns:
        Dict with job data (jd_full, salary_min, salary_max, location) or None
        if no results or an error occurs.
    """
    try:
        params = {
            "engine": "google_jobs",
            "q": query,
            "api_key": api_key,
            "num": 1,
        }
        response = requests.get(_SERPAPI_URL, params=params, timeout=_TIMEOUT)
        response.raise_for_status()

        data = response.json()
        jobs = data.get("jobs_results", [])
        if not jobs:
            return None

        job = jobs[0]
        result = {}

        # Extract job description
        description = job.get("description")
        if description:
            result["jd_full"] = description[:_MAX_JD_CHARS]

        # Extract location
        location = job.get("location")
        if location:
            result["location"] = location

        # Extract salary from detected_extensions
        extensions = job.get("detected_extensions", {})
        salary_str = extensions.get("salary", "")
        if salary_str:
            salary_range = _parse_salary_string(salary_str)
            if salary_range:
                result.update(salary_range)

        return result if result else None

    except Exception as e:
        logger.debug("SerpAPI search failed for '%s': %s", query, e)
        return None


def _search_duckduckgo(query: str) -> Optional[str]:
    """Query DuckDuckGo Instant Answer API for job/company info.

    Args:
        query: Search query string.

    Returns:
        AbstractText content string, or None if no useful content found.
    """
    try:
        params = {
            "q": query,
            "format": "json",
            "no_redirect": "1",
            "no_html": "1",
            "skip_disambig": "1",
        }
        response = requests.get(_DDG_API_URL, params=params, headers=_HEADERS, timeout=_TIMEOUT)
        response.raise_for_status()

        data = response.json()

        # Try AbstractText first (most informative)
        abstract = data.get("AbstractText", "")
        if abstract:
            return abstract

        # Fall back to first RelatedTopic text
        topics = data.get("RelatedTopics", [])
        for topic in topics:
            if isinstance(topic, dict) and topic.get("Text"):
                return topic["Text"]

        return None

    except Exception as e:
        logger.debug("DuckDuckGo search failed for '%s': %s", query, e)
        return None


def _extract_with_haiku(
    search_text: str,
    job_row: dict,
    client: Any,
    conn: Any,
    config: dict,
) -> dict:
    """Use Haiku to extract structured job data from accumulated fragment text.

    Sends search_text to Haiku with a structured extraction prompt to parse
    out salary range, location, and job description summary.

    Args:
        search_text: Aggregated text from DDG and other free-tier sources.
        job_row: Job row dict for context (title, company).
        client: Anthropic client instance.
        conn: SQLite connection for cost recording.
        config: Application config dict.

    Returns:
        Dict with any of: jd_full, salary_min, salary_max, location.
        Returns empty dict on failure.
    """
    try:
        title = job_row.get("title", "")
        company = job_row.get("company", "")

        system_prompt = (
            "You are a job data extractor. Given text about a job posting, extract "
            "structured information as a JSON object. Extract only what is explicitly "
            "stated — do not invent data. Return ONLY a JSON object with these optional "
            "fields: jd_full (string, job description summary), salary_min (integer, USD), "
            "salary_max (integer, USD), location (string). Omit fields that are not present."
        )

        user_prompt = (
            f"Job: {title} at {company}\n\n"
            f"Text to extract from:\n{search_text[:2000]}\n\n"
            f"Extract job details as JSON. Include only fields that are explicitly mentioned."
        )

        model = (
            config.get("scoring", {})
            .get("models", {})
            .get("haiku", DEFAULT_MODEL_HAIKU)
        )

        job_id = job_row.get("dedup_key")

        result, _cost = call_claude(
            client=client,
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
            # Remove None values and ensure salary fields are integers
            enriched = {}
            for key, value in result.items():
                if value is not None and key in ("jd_full", "salary_min", "salary_max", "location"):
                    if key in ("salary_min", "salary_max") and isinstance(value, (int, float)):
                        enriched[key] = int(value)
                    elif isinstance(value, str) and value.strip():
                        enriched[key] = value
            return enriched

        return {}

    except Exception as e:
        logger.debug("Haiku extraction failed: %s", e)
        return {}


# ---------------------------------------------------------------------------
# Private helpers: tier logic utilities
# ---------------------------------------------------------------------------


def _find_missing_fields(job_row: dict) -> list:
    """Return list of missing scoring-relevant field names.

    A job needs enrichment if any of these are missing:
    - jd_full: full job description (needed for Sonnet)
    - salary_min: minimum salary

    Returns empty list if all fields are present (no enrichment needed).
    """
    missing = []
    if not job_row.get("jd_full"):
        missing.append("jd_full")
    if job_row.get("salary_min") is None:
        missing.append("salary_min")
    return missing


def _filter_non_none(d: dict) -> dict:
    """Return a new dict with None values removed."""
    return {k: v for k, v in d.items() if v is not None}


def _parse_salary_string(salary_str: str) -> Optional[dict]:
    """Parse a salary string like '$140K-$180K/yr' into min/max integers.

    Args:
        salary_str: Salary string from SerpAPI detected_extensions.

    Returns:
        Dict with salary_min and/or salary_max as integers, or None if parsing fails.
    """
    try:
        # Remove currency symbols and whitespace
        cleaned = salary_str.upper().replace("$", "").replace(",", "").strip()

        # Handle K (thousands) and M (millions)
        def parse_amount(s: str) -> Optional[int]:
            s = s.strip()
            if s.endswith("K"):
                return int(float(s[:-1]) * 1000)
            elif s.endswith("M"):
                return int(float(s[:-1]) * 1_000_000)
            else:
                try:
                    return int(float(s))
                except ValueError:
                    return None

        result = {}

        # Range pattern: "140K-180K" or "140,000-180,000"
        range_match = re.search(r"([\d.]+[KM]?)\s*[-–]\s*([\d.]+[KM]?)", cleaned)
        if range_match:
            low = parse_amount(range_match.group(1))
            high = parse_amount(range_match.group(2))
            if low:
                result["salary_min"] = low
            if high:
                result["salary_max"] = high
            return result if result else None

        # Single value: "$140K"
        single_match = re.search(r"([\d.]+[KM]?)", cleaned)
        if single_match:
            val = parse_amount(single_match.group(1))
            if val:
                return {"salary_min": val}

        return None

    except Exception:
        logger.debug("_parse_salary_string failed", exc_info=True)
        return None


def _start_tier_index(current_tier: Optional[str]) -> int:
    """Return the index in TIER_ORDER to start from based on current_tier.

    If current_tier is None or 'free', start from 0 (beginning).
    Otherwise, start from the tier AFTER current_tier.

    Args:
        current_tier: The enrichment_tier value from the job row.

    Returns:
        Index into TIER_ORDER to start enrichment from.
    """
    if current_tier is None:
        return 0
    try:
        idx = TIER_ORDER.index(current_tier)
        return idx + 1  # Resume from NEXT tier
    except ValueError:
        return 0


def _parse_source_urls(source_urls_json: Optional[str]) -> list:
    """Parse source_urls JSON field into a list of URL strings.

    Args:
        source_urls_json: JSON string like '["https://..."]' or None.

    Returns:
        List of URL strings. Empty list if None or unparseable.
    """
    if not source_urls_json:
        return []
    try:
        urls = json.loads(source_urls_json)
        return [u for u in urls if isinstance(u, str)]
    except (json.JSONDecodeError, TypeError):
        return []


def _compose_fragment_text(fragments: dict, title: str, company: str) -> str:
    """Compose a single text string from all accumulated fragments.

    Args:
        fragments: Dict of fragment texts collected from prior tiers.
        title: Job title for fallback context.
        company: Company name for fallback context.

    Returns:
        Aggregated text string for use in Haiku/Sonnet extraction.
    """
    parts = []
    for key, text in fragments.items():
        if text and isinstance(text, str):
            parts.append(str(text)[:1000])

    if parts:
        return "\n\n".join(parts)
    return f"Job posting: {title} at {company}"


def _resolve_from_fragments(
    fragments: dict,
    missing: list,
    job_row: dict,
) -> dict:
    """Build an enriched dict from fragments for the fields that are missing.

    Looks for direct matches: fragments['jd_full'] -> jd_full,
    fragments['url_jd'] -> jd_full, fragments['salary_min'] -> salary_min, etc.

    Args:
        fragments: Dict of collected data from free-tier sources.
        missing: List of field names that are still missing.
        job_row: Original job row for reference.

    Returns:
        Dict of {field: value} for fields that fragments can satisfy.
    """
    enriched = {}
    for field in missing:
        # Direct key match
        if field in fragments and fragments[field] is not None:
            enriched[field] = fragments[field]
        # url_jd maps to jd_full
        elif field == "jd_full" and fragments.get("url_jd"):
            enriched["jd_full"] = fragments["url_jd"]

    return _filter_non_none(enriched)


def _persist(conn: Any, job_row: dict, enriched: dict, tier_name: str) -> None:
    """Persist enriched fields + enrichment_tier atomically in a single UPDATE.

    Only writes to DB if conn is provided. If enriched is empty, still
    updates enrichment_tier to track progress (unless conn is None).

    Args:
        conn: Open SQLite connection. If None, skip persistence.
        job_row: Job row dict (must have 'dedup_key').
        enriched: Dict of {column_name: value} to update.
        tier_name: The enrichment tier name to record.
    """
    if conn is None:
        return

    dedup_key = job_row.get("dedup_key")
    if not dedup_key:
        return

    try:
        if enriched:
            # Filter to allowlisted columns only — prevents AI-extracted keys from
            # injecting arbitrary column names into the dynamic SQL SET clause.
            safe_enriched = {k: v for k, v in enriched.items() if k in _ENRICHABLE_COLUMNS}
            if safe_enriched != enriched:
                unknown = set(enriched) - _ENRICHABLE_COLUMNS
                logger.warning("_persist: dropping non-allowlisted columns: %s", unknown)
        else:
            safe_enriched = {}

        if safe_enriched:
            set_clauses = ", ".join(f"{k} = ?" for k in safe_enriched)
            set_clauses += ", enrichment_tier = ?"
            values = list(safe_enriched.values()) + [tier_name, dedup_key]
            conn.execute(
                f"UPDATE jobs SET {set_clauses} WHERE dedup_key = ?",
                values,
            )
        else:
            conn.execute(
                "UPDATE jobs SET enrichment_tier = ? WHERE dedup_key = ?",
                (tier_name, dedup_key),
            )
        conn.commit()
    except Exception as e:
        logger.warning("Failed to persist enrichment for '%s': %s", dedup_key, e)
