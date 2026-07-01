"""Parse JobRight (jobright.ai) job-match alert emails into Job objects.

JobRight is an AI job-search copilot that emails a daily/periodic digest of
ranked job matches from ``noreply@jobright.ai``. Each match links to a job
detail page on ``jobright.ai`` (path ``/jobs/info/<id>`` or ``/job/<id>``).

Meta-email handling differs from most parsers here on purpose. The shared
``is_meta_email`` BASE patterns reject preambles like "you have N new jobs" /
"N new jobs match" — but for JobRight *that digest IS the job-bearing email*,
so gating on it at the top could silently drop real matches (Indeed only dodges
this because its "N new <role> jobs in <loc>" header happens not to match those
patterns; JobRight's preamble is unknown, so we must not assume). Instead we
parse job links first and *never* reject a job-bearing email on a preamble
false-positive. The zero-yield WARNING (issue #259 convention) is suppressed
only for narrow, genuinely-non-job "account" preambles (verify/confirm/reset).

Heuristics were written WITHOUT a captured real email (none were in the inbox
at authoring time), so — like ``ziprecruiter_parser`` — they are unverified
against a live message. To harden them, drop a sanitized real alert at
``tests/fixtures/emails/jobright.eml``; the round-trip test in
``tests/test_imap_parser_roundtrip.py`` then exercises this parser against it.
Until then a runtime zero-yield WARNING fires when a live email matches nothing,
so a heuristic mismatch is observable in the logs.

This parser is HTML-only. Both GmailSource and ImapSource ``_extract_body``
prefer the ``text/plain`` alternative when a multipart email carries one, so a
real JobRight email whose plaintext part is non-empty yields 0 jobs here plus
the zero-yield WARNING (an honest "capture a sample" signal) — NOT a silent
mis-parse. JobRight is intentionally excluded from the generic positional
URL-fallback (``_positional_fallback._JOB_URL_RE``) because that line-based
heuristic mis-attributes JobRight's plaintext layout; ingesting nothing beats
ingesting garbage rows.

If the structure is unrecognized, an empty list is returned (graceful
degradation) rather than raising.
"""

import logging
import re
from datetime import datetime
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from job_finder.models import Job
from job_finder.parsers._common import _PLACEHOLDER_STRINGS, parse_salary_range

logger = logging.getLogger(__name__)


# JobRight job-detail links. Matches direct links on the jobright.ai domain
# (and any subdomain, e.g. click./email.) whose path is a job page:
# ``/jobs/info/<id>``, ``/jobs/<id>``, or ``/job/<id>``. Requires a trailing
# path segment so the bare ``/jobs`` listing/CTA link does not match.
JOBRIGHT_JOB_URL_RE = re.compile(
    r"https?://\S*jobright\.ai/jobs?/\S+",
    re.IGNORECASE,
)

# Preambles (first 200 chars) that mark a genuinely-non-job JobRight email —
# account lifecycle / marketing, never a match digest. Used ONLY to suppress
# the zero-yield WARNING, never to reject before parsing. Deliberately excludes
# any "N new jobs" / "matches" phrasing (that IS the digest we want to parse).
_JOBRIGHT_ACCOUNT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"verify your (?:email|account)", re.IGNORECASE),
    re.compile(r"confirm your (?:email|subscription|account)", re.IGNORECASE),
    re.compile(r"reset your password", re.IGNORECASE),
    re.compile(r"welcome to jobright", re.IGNORECASE),
)

# Link texts that are navigation/CTA, never a job title.
_GENERIC_LINK_TEXTS: frozenset[str] = frozenset(
    {
        "apply",
        "apply now",
        "view",
        "view job",
        "view jobs",
        "view all jobs",
        "browse jobs",
        "see more",
        "learn more",
        "click here",
        "get started",
        "see all matches",
        "view matches",
        "unsubscribe",
    }
)

# Location cues, split into two patterns because case sensitivity differs:
#  - keywords (remote/hybrid/onsite) are case-insensitive;
#  - "City, ST" / ZIP MUST stay case-sensitive, else IGNORECASE degrades the
#    two-letter state code [A-Z]{2} into "any two letters" and misclassifies
#    company names ending in ", Co" / ", In" as locations (the Indeed sibling
#    parser deliberately keeps its location regex case-sensitive for this reason).
_LOCATION_KEYWORD_RE = re.compile(r"\b(remote|hybrid|onsite|on-site)\b", re.IGNORECASE)
_LOCATION_CITYSTATE_RE = re.compile(
    r"[A-Z][a-zA-Z]+(?:[\s.\-]+[A-Za-z]+)*,\s*[A-Z]{2}\b|\b[A-Z]{2}\s*\d{5}\b"
)

# Non-company badges JobRight renders inside a card (its AI match score is the
# product's signature and often precedes the company). These must never be
# mistaken for the company. Kept content-based (not class-based) so it does not
# overfit the synthetic fixture; "%" / "match score" avoids false-rejecting a
# real employer literally named "Match Group".
_BADGE_RE = re.compile(
    r"\d+\s*%"
    r"|\bmatch\s+score\b"
    r"|\b(?:strong|good|fair|weak)\s+match\b"
    r"|\b\d+\+?\s+(?:day|days|hour|hours|week|weeks|month|months)\s+ago\b"
    r"|\bjust\s+posted\b"
    r"|\b(?:full|part)[\s-]?time\b"
    r"|\binternship\b|\btemporary\b",
    re.IGNORECASE,
)

_MIN_WARN_BODY_LEN = 500


def parse_jobright_alert(body: str, email_date: datetime | None = None) -> list[Job]:
    """Parse a JobRight job-match alert email (HTML) into Job objects.

    Best-effort: if the structure is unrecognized, logs a warning (for
    non-trivial bodies that are not account emails) and returns an empty list
    rather than raising.

    Args:
        body: HTML email body from the Gmail/IMAP source.
        email_date: When the email was sent (stored as ``posted_date``).

    Returns:
        List of parsed Job objects (may be empty).
    """
    if not body or not body.strip():
        return []

    try:
        soup = BeautifulSoup(body, "html.parser")

        jobs = _parse_with_link_strategy(soup, email_date)
        if not jobs:
            jobs = _parse_with_card_strategy(soup, email_date)

        if not jobs and len(body.strip()) > _MIN_WARN_BODY_LEN and not _is_account_email(body):
            logger.warning(
                "JobRight parser: no jobs found -- email format may have changed. "
                "Capture a sanitized email at tests/fixtures/emails/jobright.eml and "
                "update jobright_parser.py."
            )

        return jobs

    except Exception as e:
        logger.warning("JobRight parser: unexpected error during parsing: %s", e)
        return []


def _is_account_email(body: str) -> bool:
    """Return True if the preamble marks a non-job account/marketing email.

    Only the first 200 chars are inspected (mirrors ``is_meta_email``) so a
    footer "unsubscribe" in a real digest never trips it.
    """
    preamble = body[:200]
    return any(pat.search(preamble) for pat in _JOBRIGHT_ACCOUNT_PATTERNS)


def _parse_with_link_strategy(soup: BeautifulSoup, email_date: datetime | None) -> list[Job]:
    """Strategy 1: one Job per distinct anchor pointing at a JobRight job page."""
    jobs: list[Job] = []
    seen_urls: set[str] = set()

    for link in soup.find_all("a", href=JOBRIGHT_JOB_URL_RE):
        href = link.get("href", "")
        if not href or href in seen_urls:
            continue
        seen_urls.add(href)

        job = _extract_job_from_link(link, href, email_date)
        if job:
            jobs.append(job)

    return jobs


def _parse_with_card_strategy(soup: BeautifulSoup, email_date: datetime | None) -> list[Job]:
    """Strategy 2 (fallback): find td/div cards that contain a JobRight job link.

    Used when the link text alone isn't a usable title — the surrounding
    container text supplies the fields instead.
    """
    jobs: list[Job] = []
    seen_urls: set[str] = set()

    for el in soup.find_all(["td", "div"], limit=200):
        links = el.find_all("a", href=JOBRIGHT_JOB_URL_RE)
        if not links:
            continue

        href = links[0].get("href", "")
        if not href or href in seen_urls:
            continue

        text = el.get_text(separator="\n", strip=True)
        if len(text) < 5 or len(text) > 3000:
            continue

        job = _extract_job_from_container(el, links[0], href, email_date)
        if job:
            seen_urls.add(href)
            jobs.append(job)

    return jobs


def _card_container(link_tag):
    """Return the smallest ancestor scoped to THIS job's card.

    Prefer tr → td → div, but reject any candidate that also wraps a *different*
    JobRight job link: a shared multi-column row (two cards as sibling <td> in
    one <tr>) would otherwise bleed a sibling card's company/location/salary into
    this one (the first-DOM-match field heuristics can't tell the cards apart).
    Distinct hrefs are counted so a card's own title link + "View job" CTA (same
    href) still reads as one card. Falls back to the link's immediate parent.
    """
    for name in ("tr", "td", "div"):
        cand = link_tag.find_parent(name)
        if cand is None:
            continue
        hrefs = {a.get("href") for a in cand.find_all("a", href=JOBRIGHT_JOB_URL_RE)}
        if len(hrefs) <= 1:
            return cand
    return link_tag.parent


def _extract_job_from_link(link_tag, href: str, email_date: datetime | None) -> Job | None:
    """Extract a Job from a job-link anchor and its surrounding container."""
    title = _extract_title_from_link(link_tag)
    if not title:
        return None

    container = _card_container(link_tag)

    company = _extract_company_from_context(container, title) if container else "Unknown"
    location = _extract_location_from_context(container) if container else "Unknown"
    salary_text = container.get_text(separator=" ", strip=True) if container else ""
    salary_min, salary_max = parse_salary_range(salary_text)

    return Job(
        title=title,
        company=company,
        location=location,
        source="jobright",
        source_url=href,
        source_id=_extract_job_id(href),
        salary_min=salary_min,
        salary_max=salary_max,
        posted_date=email_date,
    )


def _extract_job_from_container(
    container, link_tag, href: str, email_date: datetime | None
) -> Job | None:
    """Extract a Job from a card container when the link text isn't a title."""
    # Re-scope to this link's own card so a broad matched container (or a shared
    # multi-column row) can't bleed a sibling card's fields in.
    container = _card_container(link_tag) or container
    title = _extract_title_from_link(link_tag)
    if not title:
        lines = [
            line.strip()
            for line in container.get_text(separator="\n", strip=True).split("\n")
            if len(line.strip()) > 3
        ]
        title = lines[0] if lines else None
        if (
            not title
            or len(title) > 120
            or title.lower() in _PLACEHOLDER_STRINGS
            or title.lower() in _GENERIC_LINK_TEXTS
        ):
            return None

    company = _extract_company_from_context(container, title)
    location = _extract_location_from_context(container)
    salary_min, salary_max = parse_salary_range(container.get_text(separator=" ", strip=True))

    return Job(
        title=title,
        company=company,
        location=location,
        source="jobright",
        source_url=href,
        source_id=_extract_job_id(href),
        salary_min=salary_min,
        salary_max=salary_max,
        posted_date=email_date,
    )


def _extract_title_from_link(link_tag) -> str | None:
    """Pull a job title from an anchor: heading child, link text, then aria-label."""
    for tag in ("h1", "h2", "h3", "h4", "strong", "b"):
        el = link_tag.find(tag)
        if el:
            text = el.get_text(strip=True)
            if text and 3 <= len(text) <= 120 and text.lower() not in _PLACEHOLDER_STRINGS:
                return text

    link_text = link_tag.get_text(strip=True)
    if (
        link_text
        and 3 <= len(link_text) <= 120
        and link_text.lower() not in _GENERIC_LINK_TEXTS
        and link_text.lower() not in _PLACEHOLDER_STRINGS
    ):
        return link_text

    aria = link_tag.get("aria-label", "")
    if aria and 3 <= len(aria) <= 120 and aria.lower() not in _GENERIC_LINK_TEXTS:
        return aria

    return None


def _extract_company_from_context(container, title_text: str) -> str:
    """Find the company name in elements near the job link."""
    if container is None:
        return "Unknown"

    title_lower = title_text.lower()
    for el in container.find_all(["span", "td", "div", "p"]):
        # Skip card-wrapper elements: an element that contains the job link is
        # the whole card, not a single field — its aggregated text would win the
        # length filter and swallow every field.
        if el.find("a", href=JOBRIGHT_JOB_URL_RE):
            continue
        text = el.get_text(strip=True)
        if not text or len(text) < 2 or len(text) > 60:
            continue
        low = text.lower()
        if low == title_lower or low in _PLACEHOLDER_STRINGS or low in _GENERIC_LINK_TEXTS:
            continue
        if _looks_like_location(text) or _BADGE_RE.search(text):
            continue
        if "http" in low or "www." in low or "$" in text:
            continue
        return text

    return "Unknown"


def _extract_location_from_context(container) -> str:
    """Find location text in elements near the job link."""
    if container is None:
        return "Unknown"

    for el in container.find_all(["span", "td", "div", "p"]):
        if el.find("a", href=JOBRIGHT_JOB_URL_RE):
            continue
        text = el.get_text(strip=True)
        if text and len(text) < 100 and _looks_like_location(text):
            return text

    return "Unknown"


def _looks_like_location(text: str) -> bool:
    """Return True if *text* matches a common location pattern."""
    return bool(_LOCATION_KEYWORD_RE.search(text) or _LOCATION_CITYSTATE_RE.search(text))


def _extract_job_id(url: str) -> str:
    """Extract the JobRight job id from a job URL.

    JobRight detail URLs look like ``jobright.ai/jobs/info/<id>`` (also
    ``/jobs/<id>`` / ``/job/<id>``). The id is the segment right after the
    ``jobs/info`` / ``job(s)`` marker — NOT blindly the last path segment, which
    would return an ``/apply`` action suffix or a click-tracker hash for wrapped
    URLs like ``click.jobright.ai/CL0/https://jobright.ai/jobs/info/<id>/1/abc``.
    Falls back to the last path segment only if the marker isn't found.
    """
    try:
        markers = re.findall(r"jobright\.ai/jobs?/(?:info/)?([^/?#\s]+)", url, re.IGNORECASE)
        if markers:
            return markers[-1]
        parsed = urlparse(url)
        path_parts = [p for p in parsed.path.split("/") if p]
        if path_parts:
            return path_parts[-1]
    except Exception:
        logger.debug("jobright source_id extraction failed", exc_info=True)
    return ""
