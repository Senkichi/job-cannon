"""Pure resolution logic for the direct company-posting link.

No DB, no network. Four responsibilities:
  - classify a URL as an ATS/careers (company-owned) link vs an aggregator;
  - promote an already-known ATS source_url to the direct link (free, no scan);
  - pick the best (matched_posting, url, confidence) from postings an ATS scan /
    careers scrape already fetched, tagging strict (unique exact-title, or
    location-disambiguated among exact-title duplicates) vs loose (first-match);
  - choose the Apply-button target for a job row (apply_url_for) — the single
    enforcement point for the strict-direct_url > source_urls[0] precedence.

Title normalization consistently uses ats_platforms._title_match._normalize_title
(NOT normalizers.normalize_title, the dedup-key normalizer) — the two differ,
and mixing them silently changes match behavior.

The strict/loose tag is an experiment: both bars are evaluated on the same
posting set so the user can compare link quality in real use and later drop
the losing branch. Data merging is strict-gated: a loose match yields a LINK
only — callers must never merge posting data (jd_full, salary, ...) from it.
"""

from __future__ import annotations

import json
import re
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


# Tokens too generic to disambiguate a location on their own ("United States",
# "Greater Boston Area"). Remote/Hybrid are deliberately KEPT — they are the
# signal for remote-vs-office duplicates of the same title.
_LOCATION_STOPWORDS = frozenset(
    {"united", "states", "usa", "the", "and", "area", "greater", "metro"}
)


def _location_tokens(text: str | None) -> set[str]:
    """Lowercased alphanumeric tokens (len >= 3) of a freeform location string."""
    if not text:
        return set()
    return {t for t in re.findall(r"[a-z0-9]{3,}", text.lower()) if t not in _LOCATION_STOPWORDS}


def resolve_primary_posting(
    postings: list[dict],
    job_title: str,
    job_location: str = "",
) -> tuple[dict | None, str, str] | None:
    """Return (matched_posting, url, confidence) for the best primary posting.

    confidence:
      strict — exactly one posting's normalized title equals the job's; or,
               among several exact-title postings (same role posted in N
               locations), exactly one shares a location token with the job.
               matched_posting is that posting — safe to merge data from.
      loose  — ambiguous or no exact-title match: the first plausible link,
               with matched_posting=None. Callers MUST NOT merge posting data
               on a loose match — only the link itself is worth showing.

    Postings without a usable link are ignored. Returns None when no posting
    carries a link.
    """
    linked = [(p, _posting_link(p)) for p in (postings or [])]
    linked = [(p, url) for p, url in linked if url]
    if not linked:
        return None

    target = _normalize_title(job_title or "")
    exact = [(p, url) for p, url in linked if _normalize_title(p.get("title", "")) == target]
    if len(exact) == 1:
        posting, url = exact[0]
        return (posting, url, "strict")

    if len(exact) > 1:
        # Same title posted in several locations — disambiguate by location.
        job_tokens = _location_tokens(job_location)
        if job_tokens:
            located = [
                (p, url) for p, url in exact if job_tokens & _location_tokens(p.get("location"))
            ]
            if len(located) == 1:
                posting, url = located[0]
                return (posting, url, "strict")
        # Still ambiguous — link the first exact-title posting, merge nothing.
        return (None, exact[0][1], "loose")

    # No exact-title match — fall back to the first linked posting, merge nothing.
    return (None, linked[0][1], "loose")


def resolve_direct_link(postings: list[dict], job_title: str) -> tuple[str, str] | None:
    """Return (url, confidence) for the best direct posting link, or None.

    Thin wrapper over resolve_primary_posting for callers that only need the
    link. confidence is 'strict' (unambiguous title match) or 'loose'.
    """
    resolved = resolve_primary_posting(postings, job_title)
    if resolved is None:
        return None
    _posting, url, confidence = resolved
    return (url, confidence)


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


def _row_value(job_row, key: str):
    """Tolerant field access for plain dicts and sqlite3.Row objects."""
    try:
        return job_row[key]
    except (KeyError, IndexError, TypeError):
        return None


def apply_url_for(job_row, *, loose_apply_default: bool = False) -> str | None:
    """Return the Apply-button target for a job row.

    The single enforcement point for the apply precedence:
      1. a 'strict' direct_url (the verified company posting) always wins;
      2. a 'loose' direct_url wins only when loose_apply_default is set
         (config: direct_link.loose_apply_default — default false, loose
         links stay on the provenance badge until the strict/loose quality
         experiment concludes);
      3. otherwise the first source_url (the aggregator listing).

    Staleness fallback (Phase 5): an expired job's direct_url is skipped even if
    still populated — the company posting is gone, so we send the user to the
    aggregator listing (which often outlives the ATS posting and at least shows
    context). The reconciler NULLs direct_url on expiry, but this guard also
    covers the window before the next reconcile pass runs.

    Accepts dicts and sqlite3.Row; source_urls may be a JSON string (raw row)
    or an already-parsed list.
    """
    direct = _row_value(job_row, "direct_url")
    confidence = _row_value(job_row, "direct_url_confidence")
    expired = _row_value(job_row, "expiry_status") == "expired"
    if (
        direct
        and not expired
        and (confidence == "strict" or (confidence == "loose" and loose_apply_default))
    ):
        return direct

    raw = _row_value(job_row, "source_urls")
    if isinstance(raw, str):
        try:
            urls = json.loads(raw)
        except (ValueError, TypeError):
            urls = []
    else:
        urls = raw if isinstance(raw, list) else []
    for url in urls:
        if url and isinstance(url, str):
            return url
    return None
