"""Centralized domain policy for the job enrichment pipeline.

Defines which domains are blocked (aggregator sites that gate content behind
logins or Cloudflare walls) and which are prioritized (ATS platforms and job
boards with reliable full JD content).

Design constraints:
- Zero imports from any job_finder.web.* module (only Python stdlib permitted).
  This prevents circular import risk: data_enricher -> domain_policy <- enrichment_tiers
  <- data_enricher is safe only when domain_policy has no back-edges into the graph.
- All data is defined as module-level constants.
- PRIORITY_DOMAINS is a list[str], NOT a frozenset — the ordering is load-bearing
  for domain_priority() which uses enumerate() to assign rank scores.
"""

__all__ = ["BLOCKED_DOMAINS", "PRIORITY_DOMAINS", "is_blocked_domain", "domain_priority"]

# ---------------------------------------------------------------------------
# Blocked domains: aggregator/job-board sites that gate content behind login
# walls, Cloudflare challenges, or other scraping barriers. Fetching these
# in the free-tier direct-fetch pipeline is wasteful (always 403/challenge).
#
# MEMBERSHIP CONSTRAINT (from spec):
# - glassdoor.com and glassdoor.co.uk: Cloudflare 403 on all direct endpoints
# - indeed.com: often shows interstitial / login wall
# - ziprecruiter.com: rate-limited, paywalled content
# - dice.com: gated postings
# - linkedin.com must NOT be added here — fetch_linkedin_jd() handles it via
#   the specialized guest-page extractor path in data_enricher's free tier.
#   Adding linkedin.com would cause is_blocked_domain() to skip all LinkedIn
#   URL fetching, breaking the free-tier LinkedIn JD extraction.
# ---------------------------------------------------------------------------

BLOCKED_DOMAINS: frozenset[str] = frozenset({
    # Aggregators that gate content behind logins or Cloudflare walls
    "glassdoor.com",
    "glassdoor.co.uk",
    "indeed.com",
    "ziprecruiter.com",
    "dice.com",
    # Content farms / salary databases / career advice — never contain real JDs.
    # These appear in DDG search results and waste fetch attempts.
    "dailyremote.com",
    "jobted.com",
    "h1bdata.info",
    "h1bdata.net",
    "mastersindatascience.org",
    "careerjet.com",
    "syntaxacademy.com",
    "mrxjobs.com",
    "beamjobs.com",        # resume examples, not JDs
    "freelancer.co.uk",    # freelance marketplace, not JDs
    "simplyhired.com",     # gated aggregator (403s on direct fetch)
    "workopolis.com",      # gated aggregator (403s on direct fetch)
    "talent.com",          # job aggregator search results, not JDs
    "regionalhelpwanted.com",  # job aggregator
    "fishbowlapp.com",    # gated social network (403s)
    "thehomebase.ai",     # job aggregator listings
    "imogate.com",        # generic job listings
    "bigdatakb.com",      # job aggregator/scraper
})

# ---------------------------------------------------------------------------
# Priority domains: sites that reliably serve full JDs when fetched.
# ORDER IS LOAD-BEARING — domain_priority() uses enumerate() so index 0 is
# highest priority. ATS platforms (direct API-backed pages) come first, then
# LinkedIn public job pages, then general job boards.
# ---------------------------------------------------------------------------

PRIORITY_DOMAINS: list[str] = [
    "greenhouse.io",              # ATS — always full JD
    "lever.co",                   # ATS — always full JD
    "ashbyhq.com",                # ATS — always full JD
    "myworkdayjobs.com",          # ATS — full JD behind JS render
    "jobs.smartrecruiters.com",   # ATS — full JD
    "linkedin.com/jobs",          # LinkedIn public job pages (Playwright fetch)
    "builtin.com",                # Tech-focused job board
    "workingnomads.com",          # Remote-focused job board
    "ycombinator.com/companies",  # YC company listings with JDs
]


# ---------------------------------------------------------------------------
# Public helper functions
# ---------------------------------------------------------------------------


def is_blocked_domain(url: str) -> bool:
    """Return True if the URL's hostname matches a blocked domain (case-insensitive).

    Checks the **hostname only** (not the full URL string) to avoid false positives
    from paths that happen to contain a blocked domain name as a word
    (e.g. "https://acme.com/jobs/glassdoor-reviews" must NOT be blocked).

    Used by both the free-tier pipeline (data_enricher) and the agentic enricher
    to skip URLs that reliably return auth walls or Cloudflare challenges.

    Args:
        url: Any URL string (may be empty).

    Returns:
        True if the URL should be skipped; False if safe to fetch.
    """
    if not url:
        return False
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
    except Exception:
        return False
    host_lower = host.lower()
    return any(domain in host_lower for domain in BLOCKED_DOMAINS)


def domain_priority(url: str) -> int:
    """Return a priority rank for a URL (lower = higher priority).

    Iterates PRIORITY_DOMAINS with enumerate(); returns the index of the first
    matching domain string, or 100 if no match. Callers sort ascending so that
    ATS platforms (index 0–4) are tried before general job boards.

    Args:
        url: Any URL string.

    Returns:
        Integer priority rank. 0 = highest priority; 100 = unknown domain.
    """
    url_lower = url.lower()
    for i, domain in enumerate(PRIORITY_DOMAINS):
        if domain in url_lower:
            return i
    return 100
