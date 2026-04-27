"""ATS URL pattern extraction and slug candidate derivation."""

import logging
import re

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ATS URL Regex Patterns
# Source: github.com/lever/postings-api, developers.greenhouse.io, developers.ashbyhq.com
# ---------------------------------------------------------------------------

# Lever: both jobs.lever.co and api.lever.co patterns
_LEVER_JOBS_URL = re.compile(
    r"https?://jobs\.lever\.co/([^/?#]+)",
    re.IGNORECASE,
)
_LEVER_API_URL = re.compile(
    r"https?://api\.lever\.co/v0/postings/([^/?#]+)",
    re.IGNORECASE,
)

# Greenhouse: human-facing boards.greenhouse.io / job-boards.greenhouse.io
# and API boards-api.greenhouse.io
_GREENHOUSE_BOARDS_URL = re.compile(
    r"https?://(?:job-)?boards\.greenhouse\.io/([^/?#]+)",
    re.IGNORECASE,
)
_GREENHOUSE_API_URL = re.compile(
    r"https?://boards-api\.greenhouse\.io/v1/boards/([^/?#]+)",
    re.IGNORECASE,
)

# Ashby: case-sensitive slug (Research Pitfall 3)
_ASHBY_URL = re.compile(
    r"https?://jobs\.ashbyhq\.com/([^/?#]+)",
    # NOTE: No re.IGNORECASE — Ashby slugs are case-sensitive
)

# Workday: human-facing and API URL patterns
# Human-facing: https://{sub}.myworkdayjobs.com/{board}
# API:          https://{sub}.myworkdayjobs.com/wday/cxs/{tenant}/{board}/jobs
# Slug format: "{subdomain}/{board}" (e.g. "walmart.wd5/WalmartExternal")
_WORKDAY_HUMAN_URL = re.compile(
    r"https?://([^/]+)\.myworkdayjobs\.com/(?:en-US/)?([^/?#]+)",
    re.IGNORECASE,
)
_WORKDAY_API_URL = re.compile(
    r"https?://([^/]+)\.myworkdayjobs\.com/wday/cxs/[^/]+/([^/?#]+)",
    re.IGNORECASE,
)

# SmartRecruiters: public career pages and API
_SMARTRECRUITERS_JOBS_URL = re.compile(
    r"https?://(?:jobs|careers)\.smartrecruiters\.com/([^/?#]+)",
    re.IGNORECASE,
)
_SMARTRECRUITERS_API_URL = re.compile(
    r"https?://api\.smartrecruiters\.com/v1/companies/([^/?#]+)",
    re.IGNORECASE,
)


def extract_ats_from_urls(source_urls: list[str]) -> tuple[str | None, str | None]:
    """Extract ATS platform and slug from a list of job source URLs.

    Checks each URL against Lever, Greenhouse, and Ashby patterns.
    Returns on first match. Ashby slug preserves exact URL casing
    (per Research Pitfall 3 — Ashby slugs are case-sensitive).

    Args:
        source_urls: List of URL strings from a job record's source_urls field.

    Returns:
        Tuple of (platform, slug) where platform is 'lever', 'greenhouse',
        or 'ashby'. Returns (None, None) if no ATS URL is found.
    """
    for url in source_urls:
        # Check Lever (jobs.lever.co first, then api.lever.co)
        m = _LEVER_JOBS_URL.search(url)
        if m:
            return "lever", m.group(1)

        m = _LEVER_API_URL.search(url)
        if m:
            return "lever", m.group(1)

        # Check Greenhouse (boards.greenhouse.io first, then boards-api)
        m = _GREENHOUSE_BOARDS_URL.search(url)
        if m:
            return "greenhouse", m.group(1)

        m = _GREENHOUSE_API_URL.search(url)
        if m:
            return "greenhouse", m.group(1)

        # Check Ashby (case-sensitive — no IGNORECASE flag on pattern)
        m = _ASHBY_URL.search(url)
        if m:
            return "ashby", m.group(1)

        # Check Workday (API URL first — more specific pattern)
        m = _WORKDAY_API_URL.search(url)
        if m:
            return "workday", f"{m.group(1)}/{m.group(2)}"

        m = _WORKDAY_HUMAN_URL.search(url)
        if m:
            # Skip if this matched the /wday/ API path (handled above)
            if "/wday/" not in url:
                return "workday", f"{m.group(1)}/{m.group(2)}"

        # Check SmartRecruiters (API URL first — more specific)
        m = _SMARTRECRUITERS_API_URL.search(url)
        if m:
            return "smartrecruiters", m.group(1)

        m = _SMARTRECRUITERS_JOBS_URL.search(url)
        if m:
            return "smartrecruiters", m.group(1)

    return None, None


def derive_slug_candidates(company_name: str) -> list[str]:
    """Generate ATS slug candidates from a company name.

    Produces hyphenated and concatenated variants after stripping common
    legal suffixes. Used by probe_ats_slugs for speculative probing.

    Examples:
        "Scale AI" -> ["scale-ai", "scaleai"]
        "Stripe, Inc." -> ["stripe"]
        "OpenAI" -> ["openai"]

    Args:
        company_name: Raw company name string.

    Returns:
        List of slug candidate strings (lowercase). At least one candidate.
    """
    # Normalize: lowercase, strip legal suffixes
    name = company_name.lower()
    # Strip common suffixes (inc, llc, corp, ltd, co, company)
    name = re.sub(
        r"[,\s]+(inc\.?|llc\.?|corp\.?|corporation\.?|ltd\.?|limited\.?|co\.?|company\.?)$",
        "",
        name,
        flags=re.IGNORECASE,
    ).strip()

    # Hyphenated slug (primary) — replace non-alphanumeric runs with hyphens
    hyphenated = re.sub(r"[^a-z0-9]+", "-", name).strip("-")

    # Concatenated slug (secondary) — remove all separators
    concatenated = re.sub(r"[^a-z0-9]+", "", name)

    candidates = [hyphenated]
    if concatenated and concatenated != hyphenated:
        candidates.append(concatenated)

    return candidates
