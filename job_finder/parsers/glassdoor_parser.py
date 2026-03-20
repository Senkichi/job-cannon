"""Parse Glassdoor job alert emails into Job objects.

Glassdoor sends HTML emails from noreply@glassdoor.com with job cards.
Each card contains:
- Company name in span.gd-628b46d9ce
- Job title in p.gd-6c2846d4dc
- Location in p.gd-28d35bae2f (first occurrence per card)
- Salary in p.gd-28d35bae2f (second occurrence, format: "$XXK - $XXK (Employer est.)")
- Link wrapping each card with glassdoor.com/partner/jobListing.htm URL
- Job listing ID in URL param: jobListingId=XXXXX

Note: Glassdoor CSS class names may change over time. If parsing breaks,
inspect a recent email and update the class constants below.
"""

import logging
import re
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from job_finder.models import Job

logger = logging.getLogger(__name__)

# Meta-email patterns checked against the first 200 characters of the body.
# Checking only the preamble avoids false positives where job titles contain
# phrases like "30+ new" (per Research Pitfall 4).
_META_PATTERNS = [
    re.compile(r"^\d+\+?\s+new\s+jobs?\s+match", re.IGNORECASE | re.MULTILINE),
    re.compile(r"job alert digest|weekly digest", re.IGNORECASE),
    re.compile(r"you have \d+ new jobs?", re.IGNORECASE),
    re.compile(r"^\d+ jobs? found", re.IGNORECASE | re.MULTILINE),
]


def _is_meta_email(body: str) -> bool:
    """Return True if the email preamble matches known meta-email patterns.

    Only inspects the first 200 characters of the body to avoid false positives
    from job titles or descriptions that contain pattern-matching words.

    Args:
        body: Email body text.

    Returns:
        True if the body looks like a digest/count summary, not a job alert.
    """
    preamble = body[:200]
    return any(pattern.search(preamble) for pattern in _META_PATTERNS)

# CSS class names from Glassdoor email templates (as of March 2026)
# Update these if Glassdoor changes their email template
#
# AUDIT 2026-03-15: Classes verified against synthetic sample. Re-verify when
# Glassdoor changes template. Fallback: if zero jobs found and body has
# jobListing anchors, log warning for manual re-inspection.
COMPANY_CLASS = "gd-628b46d9ce"
TITLE_CLASS = "gd-6c2846d4dc"
DETAIL_CLASS = "gd-28d35bae2f"  # used for both location and salary


def parse_glassdoor_alert(body: str, email_date: Optional[datetime] = None) -> list[Job]:
    """Parse a Glassdoor job alert email body (HTML) into Job objects.

    Args:
        body: HTML email body from Gmail API.
        email_date: When the email was sent.

    Returns:
        List of parsed Job objects, or [] for meta-email digests.
    """
    soup = BeautifulSoup(body, "html.parser")
    jobs = []

    # Each job card is wrapped in an <a> tag linking to glassdoor.com/partner/jobListing.htm
    job_links = soup.find_all("a", href=re.compile(r"glassdoor\.com/partner/jobListing"))

    # If no job cards found AND body matches meta-email patterns, log explicitly.
    # (The parser already returns [] implicitly when job_links is empty — this adds
    # auditability so log analysis can distinguish meta-emails from HTML changes.)
    if not job_links and _is_meta_email(soup.get_text()):
        logger.debug("Skipping meta-email (pollution filter)")
        return []

    for link in job_links:
        job = _parse_job_card(link, email_date)
        if job:
            jobs.append(job)

    # Fallback drift detection: job card links existed but nothing extracted.
    # This means CSS classes have likely changed on Glassdoor's email template.
    if len(jobs) == 0 and job_links:
        logger.warning(
            "Glassdoor parser: found %d job card links but extracted 0 jobs"
            " — CSS classes may have changed. Current classes:"
            " COMPANY=%s, TITLE=%s, DETAIL=%s",
            len(job_links),
            COMPANY_CLASS,
            TITLE_CLASS,
            DETAIL_CLASS,
        )

    return jobs


def _parse_job_card(link_tag, email_date: Optional[datetime]) -> Optional[Job]:
    """Parse a single job card from a Glassdoor alert."""
    href = link_tag.get("href", "")

    # Extract job listing ID from URL
    source_id = _extract_listing_id(href)

    # Find company name
    company_el = link_tag.find("span", class_=COMPANY_CLASS)
    company = company_el.get_text(strip=True) if company_el else "Unknown"

    # Find job title
    title_el = link_tag.find("p", class_=TITLE_CLASS)
    title = title_el.get_text(strip=True) if title_el else None

    if not title:
        return None

    # Find location and salary (both use DETAIL_CLASS)
    detail_els = link_tag.find_all("p", class_=DETAIL_CLASS)
    location = "Unknown"
    salary_min = None
    salary_max = None

    for el in detail_els:
        text = el.get_text(strip=True)
        if _looks_like_salary(text):
            salary_min, salary_max = _parse_salary(text)
        elif text and not _looks_like_salary(text):
            # First non-salary detail is location
            if location == "Unknown":
                location = text

    # Build clean Glassdoor URL
    clean_url = f"https://www.glassdoor.com/job-listing/j?jl={source_id}" if source_id else href

    return Job(
        title=title,
        company=company,
        location=location,
        source="glassdoor",
        source_url=clean_url,
        source_id=source_id,
        salary_min=salary_min,
        salary_max=salary_max,
        posted_date=email_date,
    )


def _extract_listing_id(url: str) -> str:
    """Extract jobListingId from Glassdoor URL params."""
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        ids = params.get("jobListingId", [])
        # Also check if it's embedded in the path via &amp; encoding
        if not ids:
            match = re.search(r"jobListingId=(\d+)", url)
            if match:
                return match.group(1)
        return ids[0] if ids else ""
    except Exception:
        logger.debug("glassdoor description extraction failed", exc_info=True)
        return ""


def _looks_like_salary(text: str) -> bool:
    """Check if a text string looks like a salary range."""
    return bool(re.search(r"\$\d+K?\s*-\s*\$\d+K?", text))


def _parse_salary(text: str) -> tuple[Optional[int], Optional[int]]:
    """Parse salary from Glassdoor format: '$178K - $250K (Employer est.)'"""
    match = re.search(r"\$(\d+)K?\s*-\s*\$(\d+)K?", text)
    if match:
        low = int(match.group(1))
        high = int(match.group(2))
        if low < 1000:
            low *= 1000
        if high < 1000:
            high *= 1000
        return low, high
    return None, None
