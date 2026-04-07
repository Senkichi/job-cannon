"""ATS platform job scanning.

Provides keyword-matched job scanning for Lever, Greenhouse, and Ashby.
Extracted from ats_scanner.py (Plan 02 split).
"""

import json
import logging

import requests

from job_finder.web.ats_prober import _PROBE_TIMEOUT

logger = logging.getLogger(__name__)


def _title_matches(title: str, target_titles: list[str], exclusions: list[str]) -> bool:
    """Return True if title matches any target keyword and no exclusion keyword.

    Pure Python case-insensitive substring matching. Zero AI API calls.
    Used by Plan 02 (ATS scan functions) and Plan 03 (careers scraper).

    Args:
        title: Job title to evaluate.
        target_titles: List of keywords; title must match at least one
                        (OR semantics). If empty, all titles pass.
        exclusions: List of keywords; title must match none (AND NOT semantics).

    Returns:
        True if title should be included in results, False if filtered out.
    """
    title_lower = title.lower()

    # Must match at least one target title keyword (empty = no filter)
    if target_titles:
        if not any(t.lower() in title_lower for t in target_titles):
            return False

    # Must not match any exclusion keyword
    if any(ex.lower() in title_lower for ex in exclusions):
        return False

    return True


def scan_lever(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan Lever API for keyword-matched job postings.

    Fetches all active postings for the given slug and applies _title_matches
    keyword filter. Zero AI API calls — pure keyword matching.

    API: GET https://api.lever.co/v0/postings/{slug}?mode=json

    Args:
        slug: Lever company slug (e.g. 'stripe').
        target_titles: Target title keywords for inclusion filter.
        exclusions: Title keywords for exclusion filter.

    Returns:
        List of job dicts with keys: title, company_source, location,
        description, source_url, salary_min, salary_max, comp_json.
        Empty list on error or no matches.
    """
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        resp = requests.get(url, timeout=_PROBE_TIMEOUT)
    except Exception as e:
        logger.warning("scan_lever('%s') request failed: %s", slug, e)
        return []

    if resp.status_code != 200:
        logger.debug("scan_lever('%s') returned HTTP %d", slug, resp.status_code)
        return []

    try:
        postings = resp.json()
    except Exception as e:
        logger.warning("scan_lever('%s') JSON parse error: %s", slug, e)
        return []

    if not isinstance(postings, list):
        return []

    results = []
    for posting in postings:
        title = posting.get("text", "")
        if not _title_matches(title, target_titles, exclusions):
            continue

        # Extract salary range when present
        salary_range = posting.get("salaryRange") or {}
        salary_min = salary_range.get("min") if salary_range else None
        salary_max = salary_range.get("max") if salary_range else None

        # Store compensation JSON for equity/bonus/benefits details
        comp_json = json.dumps(salary_range) if salary_range else None

        # Location from categories.location
        categories = posting.get("categories") or {}
        location = categories.get("location") or categories.get("team") or ""

        results.append({
            "title": title,
            "company_source": "Lever",
            "location": location,
            "description": posting.get("descriptionPlain") or "",
            "source_url": posting.get("hostedUrl") or "",
            "salary_min": salary_min,
            "salary_max": salary_max,
            "comp_json": comp_json,
        })

    logger.debug("scan_lever('%s'): %d postings fetched, %d matched", slug, len(postings), len(results))
    return results


def scan_greenhouse(board_token: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan Greenhouse API for keyword-matched job postings.

    Fetches all active jobs with content and pay transparency data.
    CRITICAL: pay_input_ranges values are in cents — divide by 100 for dollars
    (Research Pitfall 7).

    API: GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true&pay_transparency=true

    Args:
        board_token: Greenhouse board token (e.g. 'airbnb').
        target_titles: Target title keywords for inclusion filter.
        exclusions: Title keywords for exclusion filter.

    Returns:
        List of job dicts with keys: title, company_source, location,
        description, source_url, salary_min, salary_max, comp_json.
        Empty list on error or no matches.
    """
    url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true&pay_transparency=true"
    try:
        resp = requests.get(url, timeout=_PROBE_TIMEOUT)
    except Exception as e:
        logger.warning("scan_greenhouse('%s') request failed: %s", board_token, e)
        return []

    if resp.status_code != 200:
        logger.debug("scan_greenhouse('%s') returned HTTP %d", board_token, resp.status_code)
        return []

    try:
        data = resp.json()
    except Exception as e:
        logger.warning("scan_greenhouse('%s') JSON parse error: %s", board_token, e)
        return []

    postings = data.get("jobs", []) if isinstance(data, dict) else []

    results = []
    for posting in postings:
        title = posting.get("title", "")
        if not _title_matches(title, target_titles, exclusions):
            continue

        # CRITICAL: Greenhouse pay values are in cents — divide by 100 for dollars
        # (Research Pitfall 7: Greenhouse uses cents to avoid floating point issues)
        salary_min = None
        salary_max = None
        comp_json = None
        pay_ranges = posting.get("pay_input_ranges") or []
        if pay_ranges:
            first_range = pay_ranges[0]
            min_cents = first_range.get("min_cents")
            max_cents = first_range.get("max_cents")
            if min_cents is not None:
                salary_min = min_cents // 100
            if max_cents is not None:
                salary_max = max_cents // 100
            comp_json = json.dumps(pay_ranges)

        location_obj = posting.get("location") or {}
        location = location_obj.get("name") or "" if isinstance(location_obj, dict) else ""

        # Content is the full job description HTML
        description = posting.get("content") or ""

        results.append({
            "title": title,
            "company_source": "Greenhouse",
            "location": location,
            "description": description,
            "source_url": posting.get("absolute_url") or "",
            "salary_min": salary_min,
            "salary_max": salary_max,
            "comp_json": comp_json,
        })

    logger.debug(
        "scan_greenhouse('%s'): %d postings fetched, %d matched",
        board_token, len(postings), len(results),
    )
    return results


def scan_ashby(job_board_name: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan Ashby API for keyword-matched job postings.

    Preserves exact slug casing — Ashby slugs are case-sensitive
    (Research Pitfall 3: jobs.ashbyhq.com/OpenAI != jobs.ashbyhq.com/openai).

    API: GET https://api.ashbyhq.com/posting-api/job-board/{job_board_name}?includeCompensation=true

    Args:
        job_board_name: Ashby job board name with exact casing (e.g. 'OpenAI', 'Ramp').
        target_titles: Target title keywords for inclusion filter.
        exclusions: Title keywords for exclusion filter.

    Returns:
        List of job dicts with keys: title, company_source, location,
        description, source_url, salary_min, salary_max, comp_json.
        Empty list on error or no matches.
    """
    # NOTE: No lowercasing — Ashby slugs are case-sensitive (Research Pitfall 3)
    url = f"https://api.ashbyhq.com/posting-api/job-board/{job_board_name}?includeCompensation=true"
    try:
        resp = requests.get(url, timeout=_PROBE_TIMEOUT)
    except Exception as e:
        logger.warning("scan_ashby('%s') request failed: %s", job_board_name, e)
        return []

    if resp.status_code != 200:
        logger.debug("scan_ashby('%s') returned HTTP %d", job_board_name, resp.status_code)
        return []

    try:
        data = resp.json()
    except Exception as e:
        logger.warning("scan_ashby('%s') JSON parse error: %s", job_board_name, e)
        return []

    postings = data.get("jobs", []) if isinstance(data, dict) else []

    results = []
    for posting in postings:
        title = posting.get("title", "")
        if not _title_matches(title, target_titles, exclusions):
            continue

        # Extract compensation data
        salary_min = None
        salary_max = None
        comp_json = None
        compensation = posting.get("compensation")
        if compensation:
            comp_json = json.dumps(compensation)
            # Extract base salary from summaryComponents
            summary_components = compensation.get("summaryComponents") or []
            for component in summary_components:
                if component.get("compensationType") == "base_salary":
                    salary_min = component.get("minValue")
                    salary_max = component.get("maxValue")
                    break

        # Location: use location field, fall back to empty string
        location = posting.get("location") or ""
        if not location and posting.get("isRemote"):
            location = "Remote"

        description = posting.get("descriptionHtml") or posting.get("descriptionPlain") or ""

        results.append({
            "title": title,
            "company_source": "Ashby",
            "location": location,
            "description": description,
            "source_url": posting.get("jobUrl") or "",
            "salary_min": salary_min,
            "salary_max": salary_max,
            "comp_json": comp_json,
        })

    logger.debug(
        "scan_ashby('%s'): %d postings fetched, %d matched",
        job_board_name, len(postings), len(results),
    )
    return results
