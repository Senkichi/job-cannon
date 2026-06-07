"""Pure resolution logic for the direct company-posting link.

No DB, no network. Three responsibilities:
  - classify a URL as an ATS/careers (company-owned) link vs an aggregator;
  - promote an already-known ATS source_url to the direct link (free, no scan);
  - pick the best (url, confidence) from postings an ATS scan / careers scrape
    already fetched, tagging strict (unique exact-title) vs loose (first-match).

The strict/loose tag is an experiment: both bars are evaluated on the same
posting set so the user can compare link quality in real use and later drop
the losing branch.
"""

from __future__ import annotations

from urllib.parse import urlparse

from job_finder.web.ats_platforms._title_match import _normalize_title

# Host substrings that mark a URL as a company-owned ATS / careers posting.
# Matched against the lowercased netloc. Covers the registered ATS platforms.
_ATS_HOST_MARKERS: tuple[str, ...] = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "myworkdayjobs.com",
    "smartrecruiters.com",
    "recruitee.com",
    "breezy.hr",
    "applytojob.com",  # JazzHR
    "pinpointhq.com",
    "jobs.personio.",  # personio .de/.com
    "bamboohr.com",
    "teamtailor.com",
    "workable.com",
    "jobvite.com",
    "paylocity.com",
    "rippling.com",
)


def is_ats_or_careers_url(url: str | None) -> bool:
    """Return True if the URL host is a known ATS / company careers board."""
    if not url:
        return False
    try:
        netloc = urlparse(url).netloc.lower()
    except (ValueError, AttributeError):
        return False
    if not netloc:
        return False
    return any(marker in netloc for marker in _ATS_HOST_MARKERS)


def promote_existing_direct_url(source_urls: list[str]) -> str | None:
    """Return the first source_url already on an ATS/careers host, else None."""
    for url in source_urls or []:
        if is_ats_or_careers_url(url):
            return url
    return None


def _posting_link(posting: dict) -> str | None:
    """Return a posting's link, tolerating ATS (source_url) vs careers (url) keys."""
    return posting.get("source_url") or posting.get("url") or None


def resolve_direct_link(postings: list[dict], job_title: str) -> tuple[str, str] | None:
    """Return (url, confidence) for the best direct posting link, or None.

    confidence is 'strict' (exactly one posting whose normalized title equals
    the job's normalized title) or 'loose' (the first posting carrying a link).
    Postings without a usable link are ignored.
    """
    linked = [(p, _posting_link(p)) for p in (postings or [])]
    linked = [(p, url) for p, url in linked if url]
    if not linked:
        return None

    target = _normalize_title(job_title or "")
    exact = [url for p, url in linked if _normalize_title(p.get("title", "")) == target]
    if len(exact) == 1:
        return (exact[0], "strict")

    # Ambiguous exact match or none — fall back to the first linked posting.
    return (linked[0][1], "loose")


def pick_direct_link(
    source_urls: list[str],
    ats_result: dict,
    careers_result: dict,
) -> tuple[str, str] | None:
    """Choose the best direct link by source precedence.

    Order: an existing source_url already on an ATS/careers host (strict, free)
    -> the ATS-scan result -> the careers-scrape result. Returns (url, confidence)
    or None.
    """
    promoted = promote_existing_direct_url(source_urls)
    if promoted:
        return (promoted, "strict")

    for result in (ats_result or {}, careers_result or {}):
        url = result.get("direct_url")
        conf = result.get("direct_url_confidence")
        if url and conf in ("strict", "loose"):
            return (url, conf)

    return None
