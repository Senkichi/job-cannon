"""ATS platform job scanning — public surface and shared title-matching.

The 12 ``scan_*`` functions defined here delegate to the
``PlatformScanner`` registry in ``ats_platforms_internal``. The per-
platform HTTP / pagination / response-parsing lives in the registry's
``_platforms_*`` modules; this file keeps the public scan function
names + signatures (so existing callers and the ``ats_scanner``
re-exports stay unchanged) and owns the title-normalization /
keyword-match machinery that is shared across platforms and many
other ingestion paths.

``_fetch_workday_description`` and ``_fetch_smartrecruiters_description``
remain here (not in the registry) because ``tests/test_workday_scanner.py``
and ``tests/test_smartrecruiters_scanner.py`` import them directly.

Originally extracted from ats_scanner.py (Plan 02 split). The registry
split landed in polish-review F1 (2026-05-26).
"""

import logging
import re
from functools import lru_cache

import requests

from job_finder.web.ats_platforms_internal._platforms_ashby import SCANNER as _ASHBY_SCANNER
from job_finder.web.ats_platforms_internal._platforms_bamboohr import SCANNER as _BAMBOOHR_SCANNER
from job_finder.web.ats_platforms_internal._platforms_breezy import SCANNER as _BREEZY_SCANNER
from job_finder.web.ats_platforms_internal._platforms_greenhouse import SCANNER as _GREENHOUSE_SCANNER
from job_finder.web.ats_platforms_internal._platforms_jazzhr import SCANNER as _JAZZHR_SCANNER
from job_finder.web.ats_platforms_internal._platforms_lever import SCANNER as _LEVER_SCANNER
from job_finder.web.ats_platforms_internal._platforms_personio import SCANNER as _PERSONIO_SCANNER
from job_finder.web.ats_platforms_internal._platforms_pinpoint import SCANNER as _PINPOINT_SCANNER
from job_finder.web.ats_platforms_internal._platforms_recruitee import SCANNER as _RECRUITEE_SCANNER
from job_finder.web.ats_platforms_internal._platforms_smartrecruiters import (
    SCANNER as _SMARTRECRUITERS_SCANNER,
)
from job_finder.web.ats_platforms_internal._platforms_teamtailor import (
    SCANNER as _TEAMTAILOR_SCANNER,
)
from job_finder.web.ats_platforms_internal._platforms_workday import SCANNER as _WORKDAY_SCANNER
from job_finder.web.ats_platforms_internal._platforms_workable import SCANNER as _WORKABLE_SCANNER
from job_finder.web.ats_platforms_internal._platforms_jobvite import SCANNER as _JOBVITE_SCANNER
from job_finder.web.ats_platforms_internal._platforms_paylocity import SCANNER as _PAYLOCITY_SCANNER
from job_finder.web.ats_platforms_internal._platforms_rippling import SCANNER as _RIPPLING_SCANNER
from job_finder.web.ats_platforms_internal._registry import run_platform_scan
from job_finder.web.ats_prober import _PROBE_TIMEOUT
from job_finder.web.description_formatter import strip_html_to_text

logger = logging.getLogger(__name__)


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
        (r"Sr\.?", "Senior"),
        (r"Jr\.?", "Junior"),
        (r"Mgr\.?", "Manager"),
        (r"Mgmt\.?", "Management"),
        (r"Eng\.?", "Engineer"),
        (r"Engr\.?", "Engineer"),
        (r"Dev\.?", "Developer"),
        (r"Arch\.?", "Architect"),
        (r"Ops\b", "Operations"),
        (r"Admin\b", "Administrator"),
        (r"Dir\.?", "Director"),
        (r"VP\b", "Vice President"),
        (r"DS\b", "Data Scientist"),
        (r"DA\b", "Data Analyst"),
        (r"DE\b", "Data Engineer"),
        (r"PM\b", "Product Manager"),
        (r"TPM\b", "Technical Program Manager"),
        (r"EM\b", "Engineering Manager"),
        (r"MLE\b", "Machine Learning Engineer"),
        (r"ML\b", "Machine Learning"),
        (r"AI\b", "Artificial Intelligence"),
        (r"SRE\b", "Site Reliability Engineer"),
        (r"SWE\b", "Software Engineer"),
        (r"SE\b", "Software Engineer"),
        (r"IC\b", "Individual Contributor"),
        (r"QA\b", "Quality Assurance"),
        (r"UX\b", "User Experience"),
        (r"UI\b", "User Interface"),
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


# ---------------------------------------------------------------------------
# Platform scanners — thin delegations to the PlatformScanner registry
# ---------------------------------------------------------------------------


def scan_lever(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan Lever API for keyword-matched job postings.

    API: GET https://api.lever.co/v0/postings/{slug}?mode=json

    Returns:
        List of job dicts with keys: title, company_source, location,
        description, source_url, salary_min, salary_max, comp_json.
        Empty list on error or no matches.
    """
    return run_platform_scan(_LEVER_SCANNER, slug, target_titles, exclusions)


def scan_greenhouse(
    board_token: str, target_titles: list[str], exclusions: list[str]
) -> list[dict]:
    """Scan Greenhouse API for keyword-matched job postings.

    Fetches all active jobs with content and pay transparency data.
    CRITICAL: pay_input_ranges values are in cents — divide by 100 for dollars
    (Research Pitfall 7).

    API: GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true&pay_transparency=true
    """
    return run_platform_scan(_GREENHOUSE_SCANNER, board_token, target_titles, exclusions)


def scan_ashby(job_board_name: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan Ashby API for keyword-matched job postings.

    Preserves exact slug casing — Ashby slugs are case-sensitive
    (Research Pitfall 3: jobs.ashbyhq.com/OpenAI != jobs.ashbyhq.com/openai).
    Single retry on transient timeout (Ashby intermittency, see registry).

    API: GET https://api.ashbyhq.com/posting-api/job-board/{job_board_name}?includeCompensation=true
    """
    return run_platform_scan(_ASHBY_SCANNER, job_board_name, target_titles, exclusions)


def scan_workday(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan Workday CXS API for keyword-matched job postings.

    Workday exposes a standardized POST JSON API across all tenants.
    Slug format: "{subdomain}/{board}" (e.g. "walmart.wd5/WalmartExternal").
    Per-job description is fetched via ``_fetch_workday_description``.
    """
    return run_platform_scan(_WORKDAY_SCANNER, slug, target_titles, exclusions)


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
    Per-job description is fetched via ``_fetch_smartrecruiters_description``.
    """
    return run_platform_scan(_SMARTRECRUITERS_SCANNER, slug, target_titles, exclusions)


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


# ---------------------------------------------------------------------------
# Stage 4 — ATS scanner expansion (NO-KEY-COMPENSATION-PLAN.md §Stage 4).
#
# Feasibility spike outcomes (per PLAN.md §Stage 4 acceptance):
#
#   Recruitee  — FEASIBLE. Public JSON at https://{slug}.recruitee.com/api/offers/
#                returning {"offers": [...]}. No auth, no key, no rate limit
#                documented (~120 req/min observed by 3rd-party scrapers).
#                Verified via outscal/OpenJobs and Jhatchi/Cyber-Job-Hunter
#                adapters on GitHub (both production-running).
#
#   Breezy     — FEASIBLE. Public JSON at https://{slug}.breezy.hr/json
#                returning a bare list of position objects. No auth. List
#                endpoint omits description (detail-page enrichment needed
#                for jd_full). Verified via kalil0321/ats-scrapers adapter.
#
#   JazzHR     — FEASIBLE. Public JSON at
#                https://{slug}.applytojob.com/apply/jobs/feed?json=1
#                returning {"jobs": [...]} OR a bare list (tenant-dependent).
#                No auth. Verified via ItsmeBlackOps/dailyDashboard adapter.
#                (The historical .xml feed still exists at the same path
#                without `?json=1` but JSON is canonical for new consumers.)
#
#   Pinpoint   — FEASIBLE. Public JSON at https://{slug}.pinpointhq.com/postings.json
#                returning {"data": [...]}. No auth. Single-shot (no pagination).
#                Verified via kalil0321/ats-scrapers PinpointScraper + several
#                production peviitor-ro adapters hitting the same endpoint.
#
#   Personio   — FEASIBLE. Public XML feed at
#                https://{slug}.jobs.personio.{de,com}/xml. Standard XML schema
#                (<position> elements with <id>/<name>/<office>/<jobDescriptions>).
#                Some tenants also expose /search.json but XML is the canonical
#                public source per Personio's own docs (SammyTheSalmon/personio).
#                Verified via leonfoeck/Werki-Checker, ever-jobs, working-group-two.
#
#   BambooHR   — FEASIBLE (HTML scrape). The historical
#                https://{slug}.bamboohr.com/careers/list JSON endpoint was
#                deprecated in 2024 — every tenant now serves the embedded
#                careers widget at /jobs/embed2.php as static HTML. We parse the
#                widget with BeautifulSoup (already a project dependency); jobs
#                are <li id="bhrPositionID_{id}"> elements with title, location,
#                and detail-page link. Verified via kalil0321/ats-scrapers
#                BambooHRScraper. Description/jd_full requires a per-job
#                detail-page fetch — deferred (Stage 4 only does listings).
#
#   Teamtailor — FEASIBLE. Public unkeyed JSON at
#                https://{slug}.teamtailor.com/api/jobs returning JSON:API-shaped
#                {"data": [{"attributes": {...}}, ...]}. The X-Api-Version /
#                X-Api-Key flow at api.teamtailor.com/v1/jobs is the keyed
#                organization-level API — orthogonal to the public per-tenant
#                feed. Verified via ahmedmobarak1994/jobscannercloud
#                TeamtailorSource (_fetch_public path). HANDOFF.md previously
#                flagged this as keyed-only; that was wrong.
# ---------------------------------------------------------------------------


def scan_recruitee(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan Recruitee public offers API for keyword-matched job postings.

    API: GET https://{slug}.recruitee.com/api/offers/ → {"offers": [...]}.
    No auth required. The list response includes title, locations, description,
    and careers_url; no detail-page fetch is needed for jd_full.
    """
    return run_platform_scan(_RECRUITEE_SCANNER, slug, target_titles, exclusions)


def scan_breezy(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan Breezy HR public JSON feed for keyword-matched job postings.

    API: GET https://{slug}.breezy.hr/json → flat list of position objects.
    No auth required. The list endpoint omits the full description (only a
    short summary is exposed); jd_full enrichment happens later via the
    description URL.
    """
    return run_platform_scan(_BREEZY_SCANNER, slug, target_titles, exclusions)


def scan_jazzhr(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan JazzHR public feed for keyword-matched job postings.

    API: GET https://{slug}.applytojob.com/apply/jobs/feed?json=1
        → {"jobs": [...]} OR a bare list, depending on tenant config.
    """
    return run_platform_scan(_JAZZHR_SCANNER, slug, target_titles, exclusions)


def scan_pinpoint(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan Pinpoint public postings JSON for keyword-matched job postings.

    API: GET https://{slug}.pinpointhq.com/postings.json → {"data": [...]}.
    No auth. Single-shot — Pinpoint returns every active posting in one
    response with no pagination.
    """
    return run_platform_scan(_PINPOINT_SCANNER, slug, target_titles, exclusions)


def scan_personio(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan Personio public XML feed for keyword-matched job postings.

    API: GET https://{slug}.jobs.personio.{de,com}/xml → workzag-jobs
    document with <position> children. Tries .de first, falls back to .com
    on 404 (some tenants migrated TLDs).
    """
    return run_platform_scan(_PERSONIO_SCANNER, slug, target_titles, exclusions)


def scan_bamboohr(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan BambooHR public careers widget HTML for keyword-matched postings.

    API: GET https://{slug}.bamboohr.com/jobs/embed2.php → HTML widget.
    The historical /careers/list JSON endpoint was deprecated in 2024 — the
    widget HTML is now the only public source. Listing does NOT include job
    descriptions (deferred to enrichment).
    """
    return run_platform_scan(_BAMBOOHR_SCANNER, slug, target_titles, exclusions)


def scan_teamtailor(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan Teamtailor public jobs API for keyword-matched postings.

    API: GET https://{slug}.teamtailor.com/api/jobs → JSON:API document
    {"data": [{"attributes": {...}, "links": {...}}, ...]}. Per-tenant
    public unkeyed feed (not the keyed api.teamtailor.com path).
    """
    return run_platform_scan(_TEAMTAILOR_SCANNER, slug, target_titles, exclusions)


# Round 6 (2026-05-27 audit B2-roadmap) — Workable / Jobvite / Paylocity / Rippling.


def scan_workable(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan Workable public widget endpoint for keyword-matched postings.

    API: GET https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true
    → {"name": ..., "jobs": [...]}. Empty-jobs path is a clean miss.
    """
    return run_platform_scan(_WORKABLE_SCANNER, slug, target_titles, exclusions)


def scan_jobvite(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan Jobvite — stub. Always returns [].

    Jobvite hosted career pages have no public unauthenticated JSON API
    and frequently redirect to tenant-custom domains. A real scraper
    requires per-tenant HTML parsing; deferred. The stub exists so the
    platform is registered for URL-evidence promotion via B2 fast-path.
    See _platforms_jobvite.py for the full rationale.
    """
    return run_platform_scan(_JOBVITE_SCANNER, slug, target_titles, exclusions)


def scan_paylocity(guid: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan Paylocity public v2 job feed for keyword-matched postings.

    API: GET https://recruiting.paylocity.com/recruiting/v2/api/feed/jobs/{guid}
    → {"organization": ..., "jobs": [...]}. Each job includes summary,
    requirements, benefits as separate sections (stitched into description).
    The "slug" is the tenant GUID extracted from the careers URL path.
    """
    return run_platform_scan(_PAYLOCITY_SCANNER, guid, target_titles, exclusions)


def scan_rippling(slug: str, target_titles: list[str], exclusions: list[str]) -> list[dict]:
    """Scan Rippling public board API for keyword-matched postings.

    API: GET https://ats.rippling.com/api/v2/board/{slug}/jobs → paginated
    {"items": [...], "page": ..., "totalPages": ...}. Description is NOT
    in the list endpoint; enrichment fills jd_full asynchronously.
    """
    return run_platform_scan(_RIPPLING_SCANNER, slug, target_titles, exclusions)
