"""ATS platform job scanning.

Provides keyword-matched job scanning for Lever, Greenhouse, Ashby, and Workday.
Extracted from ats_scanner.py (Plan 02 split).
"""

import json
import logging
import re
import time
from functools import lru_cache

import requests

from job_finder.web.ats_prober import _PROBE_TIMEOUT
from job_finder.web.description_formatter import strip_html_to_text

logger = logging.getLogger(__name__)

# Delay between per-job detail fetches inside a single scanner run.
# Keeps request rate polite; matches careers_crawler's inter-company sleep pattern.
_DETAIL_FETCH_SLEEP_S = 0.1


# ---------------------------------------------------------------------------
# Title normalization + word-boundary matching
# ---------------------------------------------------------------------------
# Recruiters use shorthand ("Sr DS", "ML Eng", "PM, Growth") that the old
# verbatim-substring matcher missed entirely. _normalize_title expands the
# common abbreviations BEFORE the keyword check, so a config keyword of
# "Data Scientist" hits both "Senior Data Scientist" and "Sr DS".
#
# Order does not matter -- patterns are non-overlapping. Add new entries
# here when a new abbreviation shows up in a posting you would have wanted
# to catch.
#
# Each entry is (compiled regex, replacement). Regexes use \b word boundaries
# so "DS" does not match "DSP" or "SDS"; the replacement is the canonical
# spelled-out form lowercased once at module load.
_TITLE_EXPANSIONS: list[tuple[re.Pattern, str]] = [
    (re.compile(rf"\b{abbr}\b", re.IGNORECASE), full.lower())
    for abbr, full in [
        (r"Sr\.?",  "Senior"),
        (r"Jr\.?",  "Junior"),
        (r"Mgr\.?", "Manager"),
        (r"Mgmt\.?","Management"),
        (r"Eng\.?", "Engineer"),
        (r"Engr\.?","Engineer"),
        (r"Dev\.?", "Developer"),
        (r"Arch\.?","Architect"),
        (r"Ops\b",  "Operations"),
        (r"Admin\b","Administrator"),
        (r"Dir\.?", "Director"),
        (r"VP\b",   "Vice President"),
        (r"DS\b",   "Data Scientist"),
        (r"DA\b",   "Data Analyst"),
        (r"DE\b",   "Data Engineer"),
        (r"PM\b",   "Product Manager"),
        (r"TPM\b",  "Technical Program Manager"),
        (r"EM\b",   "Engineering Manager"),
        (r"MLE\b",  "Machine Learning Engineer"),
        (r"ML\b",   "Machine Learning"),
        (r"AI\b",   "Artificial Intelligence"),
        (r"SRE\b",  "Site Reliability Engineer"),
        (r"SWE\b",  "Software Engineer"),
        (r"SE\b",   "Software Engineer"),
        (r"IC\b",   "Individual Contributor"),
        (r"QA\b",   "Quality Assurance"),
        (r"UX\b",   "User Experience"),
        (r"UI\b",   "User Interface"),
    ]
]


_PUNCT_RUN = re.compile(r"[^\w\s]+")
_WS_RUN = re.compile(r"\s+")


def _normalize_title(title: str) -> str:
    """Lowercase, expand common recruiter abbreviations, normalize whitespace.

    After abbreviation expansion ("Sr." -> "Senior"), the original
    punctuation may strand inside a multi-word keyword's match window
    -- "Sr. DS" expands to "Senior. Data Scientist", which a literal-space
    regex for "Senior Data Scientist" will not match. We therefore collapse
    runs of punctuation to a single space and runs of whitespace to one
    space before lowercasing.

    Idempotent: applying twice produces the same output as applying once.
    The expansions never produce abbreviations the same regexes would
    re-match, and the whitespace collapse is already at a fixed point.
    """
    out = title
    for pat, sub in _TITLE_EXPANSIONS:
        out = pat.sub(sub, out)
    out = _PUNCT_RUN.sub(" ", out)
    out = _WS_RUN.sub(" ", out).strip()
    return out.lower()


@lru_cache(maxsize=512)
def _compile_word_boundary(keyword: str) -> re.Pattern:
    r"""Return a compiled \bkeyword\b regex (case-insensitive).

    Cached because the same target_titles list is reused across every job
    in a scan -- a single scan of 850 companies x ~50 jobs each compiles
    each keyword's pattern once, not 42,500 times.

    The keyword is normalized through _normalize_title first so that a
    config entry of "Sr Data Scientist" gets matched as
    "senior data scientist" -- consistent with how candidate titles are
    matched. re.escape() is applied AFTER normalization to defang any
    regex metacharacters that survive normalization.
    """
    norm = _normalize_title(keyword)
    return re.compile(rf"\b{re.escape(norm)}\b", re.IGNORECASE)


def _title_matches(title: str, target_titles: list[str], exclusions: list[str]) -> bool:
    r"""Return True if title matches any target keyword and no exclusion keyword.

    Two-stage matcher:

    1. **Normalize**: both the candidate title and each keyword are passed
       through _normalize_title, which lowercases and expands common
       abbreviations (Sr -> Senior, DS -> Data Scientist, MLE -> Machine
       Learning Engineer, etc.). This lets "Sr DS, Growth" match a
       configured keyword of "Senior Data Scientist".

    2. **Word-boundary match**: \bkeyword\b regex instead of plain
       substring. Prevents short keywords like "Lead" from matching inside
       "Leadership" or "Misleading", and short ones like "Data" from
       matching "Database".

    Args:
        title: Job title to evaluate.
        target_titles: Keywords; title must match at least one (OR
            semantics). If empty, all titles pass -- but configs reaching
            this code path with an empty list have bypassed the
            config.validate_target_titles guard.
        exclusions: Keywords; title must match none (AND NOT semantics).
            Exclusion wins over inclusion.

    Returns:
        True if title should be included in results, False if filtered out.
    """
    normalized = _normalize_title(title)

    if target_titles:
        if not any(_compile_word_boundary(t).search(normalized) for t in target_titles):
            return False

    return not any(_compile_word_boundary(ex).search(normalized) for ex in exclusions)


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

        results.append(
            {
                "title": title,
                "company_source": "Lever",
                "location": location,
                "description": posting.get("descriptionPlain") or "",
                "source_url": posting.get("hostedUrl") or "",
                "salary_min": salary_min,
                "salary_max": salary_max,
                "comp_json": comp_json,
            }
        )

    logger.debug(
        "scan_lever('%s'): %d postings fetched, %d matched", slug, len(postings), len(results)
    )
    return results


def scan_greenhouse(
    board_token: str, target_titles: list[str], exclusions: list[str]
) -> list[dict]:
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

        results.append(
            {
                "title": title,
                "company_source": "Greenhouse",
                "location": location,
                "description": description,
                "source_url": posting.get("absolute_url") or "",
                "salary_min": salary_min,
                "salary_max": salary_max,
                "comp_json": comp_json,
            }
        )

    logger.debug(
        "scan_greenhouse('%s'): %d postings fetched, %d matched",
        board_token,
        len(postings),
        len(results),
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
    url = (
        f"https://api.ashbyhq.com/posting-api/job-board/{job_board_name}?includeCompensation=true"
    )
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

        description = posting.get("descriptionPlain") or posting.get("descriptionHtml") or ""

        results.append(
            {
                "title": title,
                "company_source": "Ashby",
                "location": location,
                "description": description,
                "source_url": posting.get("jobUrl") or "",
                "salary_min": salary_min,
                "salary_max": salary_max,
                "comp_json": comp_json,
            }
        )

    logger.debug(
        "scan_ashby('%s'): %d postings fetched, %d matched",
        job_board_name,
        len(postings),
        len(results),
    )
    return results


def scan_workday(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan Workday CXS API for keyword-matched job postings.

    Workday exposes a standardized POST JSON API across all tenants.
    Slug format: "{subdomain}/{board}" (e.g. "walmart.wd5/WalmartExternal").

    API: POST https://{subdomain}.myworkdayjobs.com/wday/cxs/{tenant}/{board}/jobs
    Body: {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}
    Response: {"total": N, "jobPostings": [{title, externalPath, locationsText, ...}]}

    Args:
        slug: Workday slug in "subdomain/board" format.
        target_titles: Target title keywords for inclusion filter.
        exclusions: Title keywords for exclusion filter.

    Returns:
        List of job dicts with keys: title, company_source, location,
        description, source_url, salary_min, salary_max, comp_json.
        Empty list on error or no matches.
    """
    parts = slug.split("/", 1)
    if len(parts) != 2:
        logger.warning("scan_workday: invalid slug format '%s'", slug)
        return []

    subdomain, board = parts

    # Derive tenant from subdomain: prefix before ".wd"
    dot_wd_idx = subdomain.find(".wd")
    tenant = subdomain[:dot_wd_idx] if dot_wd_idx > 0 else subdomain

    api_url = f"https://{subdomain}.myworkdayjobs.com/wday/cxs/{tenant}/{board}/jobs"
    page_size = 20
    max_results = 200
    offset = 0
    results = []
    total_fetched = 0

    while offset < max_results:
        body = {
            "appliedFacets": {},
            "limit": page_size,
            "offset": offset,
            "searchText": "",
        }
        try:
            resp = requests.post(
                api_url,
                json=body,
                headers={"Content-Type": "application/json"},
                timeout=_PROBE_TIMEOUT,
            )
        except Exception as e:
            logger.warning("scan_workday('%s') request failed: %s", slug, e)
            break

        if resp.status_code != 200:
            logger.debug("scan_workday('%s') returned HTTP %d", slug, resp.status_code)
            break

        try:
            data = resp.json()
        except Exception as e:
            logger.warning("scan_workday('%s') JSON parse error: %s", slug, e)
            break

        total = data.get("total", 0)
        postings = data.get("jobPostings", [])
        if not postings:
            break

        for posting in postings:
            title = posting.get("title", "")
            if not _title_matches(title, target_titles, exclusions):
                continue

            location = posting.get("locationsText", "")
            external_path = posting.get("externalPath", "")
            # externalPath from the CXS API already begins with "/job/...".
            # Do NOT prepend another "/job/" — the previous template emitted
            # "/job//job/..." URLs that 406'd at the API and rendered to a
            # Workday SPA shell whose only static text is <title>Workday</title>.
            source_url = (
                f"https://{subdomain}.myworkdayjobs.com/en-US/{board}{external_path}"
                if external_path
                else ""
            )

            # Fetch the full description via the Workday detail endpoint.
            # Without this, Workday jobs land in the DB with jd_full=NULL and
            # the score-tier scorer can never evaluate them (skips on empty JD).
            description = (
                _fetch_workday_description(subdomain, tenant, board, external_path)
                if external_path
                else ""
            )

            results.append(
                {
                    "title": title,
                    "company_source": "Workday",
                    "location": location,
                    "description": description,
                    "source_url": source_url,
                    "salary_min": None,
                    "salary_max": None,
                    "comp_json": None,
                }
            )

            time.sleep(_DETAIL_FETCH_SLEEP_S)

        total_fetched += len(postings)
        offset += page_size

        # Stop if we've fetched all available results
        if total_fetched >= total:
            break

    logger.debug(
        "scan_workday('%s'): %d total, %d fetched, %d matched",
        slug,
        total_fetched,
        total_fetched,
        len(results),
    )
    return results


def _fetch_workday_description(subdomain: str, tenant: str, board: str, external_path: str) -> str:
    """Fetch the full job description via Workday CXS detail endpoint.

    Workday's list endpoint returns only titles and metadata; the full HTML
    description lives at a separate per-job URL. Returns empty string on any
    failure (no exceptions leak to the caller) so one broken job doesn't kill
    a whole scan.

    Args:
        subdomain: Workday subdomain (e.g. 'walmart.wd5').
        tenant: Derived tenant (prefix before '.wd').
        board: Job board name (second half of slug).
        external_path: Posting path from the list response (e.g. '/job/Analyst_R-123').

    Returns:
        Plain-text job description (HTML stripped), or "" if fetch failed.
    """
    # external_path begins with "/job/..." — no static "/job/" prefix here.
    detail_url = f"https://{subdomain}.myworkdayjobs.com/wday/cxs/{tenant}/{board}{external_path}"
    try:
        resp = requests.get(
            detail_url,
            headers={"Accept": "application/json"},
            timeout=_PROBE_TIMEOUT,
        )
        if resp.status_code != 200:
            return ""
        data = resp.json()
    except Exception as exc:
        logger.debug("scan_workday detail fetch failed for %s: %s", external_path, exc)
        return ""

    # Common shape: {"jobPostingInfo": {"jobDescription": "<html>..."}}
    info = data.get("jobPostingInfo") or {}
    html = info.get("jobDescription") or ""
    if not html:
        return ""

    return strip_html_to_text(html) if "<" in html else html


def scan_smartrecruiters(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan SmartRecruiters Posting API for keyword-matched job postings.

    SmartRecruiters exposes a public REST API (no auth required) that returns
    JSON job listings with offset-based pagination.

    API: GET https://api.smartrecruiters.com/v1/companies/{slug}/postings?offset={N}&limit=100

    Args:
        slug: SmartRecruiters company identifier (e.g. 'LinkedIn3', 'AbbVie').
        target_titles: Target title keywords for inclusion filter.
        exclusions: Title keywords for exclusion filter.

    Returns:
        List of job dicts with keys: title, company_source, location,
        description, source_url, salary_min, salary_max, comp_json.
        Empty list on error or no matches.
    """
    base_url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    page_size = 100
    max_results = 500
    offset = 0
    results = []
    total_fetched = 0

    while offset < max_results:
        try:
            resp = requests.get(
                base_url,
                params={"offset": offset, "limit": page_size},
                headers={"Accept": "application/json"},
                timeout=_PROBE_TIMEOUT,
            )
        except Exception as e:
            logger.warning("scan_smartrecruiters('%s') request failed: %s", slug, e)
            break

        if resp.status_code != 200:
            logger.debug("scan_smartrecruiters('%s') returned HTTP %d", slug, resp.status_code)
            break

        try:
            data = resp.json()
        except Exception as e:
            logger.warning("scan_smartrecruiters('%s') JSON parse error: %s", slug, e)
            break

        total_found = data.get("totalFound", 0)
        postings = data.get("content", [])
        if not postings:
            break

        for posting in postings:
            title = posting.get("name", "")
            if not _title_matches(title, target_titles, exclusions):
                continue

            loc = posting.get("location", {})
            if isinstance(loc, dict):
                parts = [loc.get("city", ""), loc.get("region", ""), loc.get("country", "")]
                location = ", ".join(p for p in parts if p)
            else:
                location = ""

            posting_id = posting.get("id", "")
            source_url = (
                f"https://jobs.smartrecruiters.com/{slug}/{posting_id}" if posting_id else ""
            )

            # Fetch the full description via the posting detail endpoint.
            # The list endpoint returns only name + id + location; without a
            # secondary fetch, jd_full stays NULL and the scorer skips the job.
            description = (
                _fetch_smartrecruiters_description(slug, posting_id) if posting_id else ""
            )

            results.append(
                {
                    "title": title,
                    "company_source": "SmartRecruiters",
                    "location": location,
                    "description": description,
                    "source_url": source_url,
                    "salary_min": None,
                    "salary_max": None,
                    "comp_json": None,
                }
            )

            time.sleep(_DETAIL_FETCH_SLEEP_S)

        total_fetched += len(postings)
        offset += page_size

        if total_fetched >= total_found:
            break

    logger.debug(
        "scan_smartrecruiters('%s'): %d total, %d fetched, %d matched",
        slug,
        total_fetched,
        total_fetched,
        len(results),
    )
    return results


def _fetch_smartrecruiters_description(slug: str, posting_id: str) -> str:
    """Fetch the full job description via SmartRecruiters Posting detail API.

    The posting detail response has `jobAd.sections.*.text` fields; we
    concatenate the main job description and qualifications sections.
    Returns empty string on any failure so one broken job doesn't kill the scan.

    Args:
        slug: SmartRecruiters company identifier.
        posting_id: Posting UUID from the list response.

    Returns:
        Plain-text job description (HTML stripped), or "" on failure.
    """
    detail_url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{posting_id}"
    try:
        resp = requests.get(
            detail_url,
            headers={"Accept": "application/json"},
            timeout=_PROBE_TIMEOUT,
        )
        if resp.status_code != 200:
            return ""
        data = resp.json()
    except Exception as exc:
        logger.debug("scan_smartrecruiters detail fetch failed for %s: %s", posting_id, exc)
        return ""

    sections = (data.get("jobAd") or {}).get("sections") or {}
    parts: list[str] = []
    for key in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"):
        section = sections.get(key) or {}
        text = section.get("text") or ""
        if text:
            parts.append(text)

    combined = "\n\n".join(parts)
    if not combined:
        return ""

    return strip_html_to_text(combined) if "<" in combined else combined
