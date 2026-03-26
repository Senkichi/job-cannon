"""Parse Glassdoor job alert emails into Job objects.

Glassdoor sends HTML emails from noreply@glassdoor.com with job cards.

Two formats are handled:
1. CSS-class format (pre-2026): span.gd-* and p.gd-* elements
2. Positional format (2026+): classless spans and p tags in fixed order

Each card is wrapped in an <a> tag linking to glassdoor.com/partner/jobListing.htm
with a jobListingId URL parameter.

CSS-class format:
- Company name in span.gd-628b46d9ce
- Job title in p.gd-6c2846d4dc
- Location in p.gd-28d35bae2f (first occurrence per card)
- Salary in p.gd-28d35bae2f (second occurrence, format: "$XXK - $XXK (Employer est.)")

Positional format:
- Company name in inner <span> (sibling of rating span "X.X ★")
- Job title in first <p> tag
- Location in second <p> tag (if not salary/age)
- Salary in <p> tag containing "$"

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
from job_finder.parsers._common import (
    is_meta_email as _is_meta_email,
    looks_like_salary_range,
    parse_salary_range,
)

logger = logging.getLogger(__name__)

# CSS class names from Glassdoor email templates (pre-2026 format)
# These classes are absent in the 2026+ positional format.
COMPANY_CLASS = "gd-628b46d9ce"
TITLE_CLASS = "gd-6c2846d4dc"
DETAIL_CLASS = "gd-28d35bae2f"  # used for both location and salary

# Positional format: rating pattern (e.g. "3.6 ★" or "4.1 ★")
_RATING_RE = re.compile(r'^\s*\d+\.\d+\s*\u2605?\s*$')

# Positional format: age/recency pattern (e.g. "Just posted", "17h", "1d", "5d")
_AGE_RE = re.compile(r'^(Just posted|\d+[hd]?)$', re.IGNORECASE)


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
    """Parse a single job card from a Glassdoor alert.

    Tries CSS-class extraction first (pre-2026 format). Falls back to
    positional extraction when CSS classes are absent (2026+ format).
    """
    href = link_tag.get("href", "")

    # Extract job listing ID from URL
    source_id = _extract_listing_id(href)

    # Find company name
    company_el = link_tag.find("span", class_=COMPANY_CLASS)
    company = company_el.get_text(strip=True) if company_el else "Unknown"

    # Find job title
    title_el = link_tag.find("p", class_=TITLE_CLASS)
    title = title_el.get_text(strip=True) if title_el else None

    # If CSS-class extraction yielded no title, fall back to positional extraction.
    # This handles the 2026+ Glassdoor email format where all CSS classes are absent.
    if not title:
        return _parse_job_card_positional(link_tag, email_date)

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


def _parse_job_card_positional(link_tag, email_date: Optional[datetime]) -> Optional[Job]:
    """Parse a single job card using positional extraction (2026+ classless format).

    In the new Glassdoor format, all CSS classes are absent. Company name and
    rating are in nested <span> tags; job fields are in sequential <p> tags.

    Company extraction: iterate all <span> tags in the card, collect those
    whose .string (direct text, no children) is non-empty. Filter out spans
    whose text matches the rating pattern (e.g. "3.6 ★"). Take first remaining.

    Field extraction from <p> tags:
    - First p that is neither salary nor age = title
    - Among remaining: first non-salary non-age = location
    - First p containing "$" with salary range = salary
    """
    href = link_tag.get("href", "")
    source_id = _extract_listing_id(href)

    # --- Company ---
    company = "Unknown"
    for span in link_tag.find_all("span"):
        direct_text = span.string
        if direct_text is None:
            continue
        direct_text = direct_text.strip()
        if not direct_text:
            continue
        if _RATING_RE.match(direct_text):
            continue
        company = direct_text
        break

    # --- Title, location, salary from <p> tags ---
    all_ps = link_tag.find_all("p")
    title = None
    location = "Unknown"
    salary_min = None
    salary_max = None

    # Find title: first p that is not a salary and not an age marker
    title_idx = None
    for i, p in enumerate(all_ps):
        text = p.get_text(strip=True)
        if not text:
            continue
        if _looks_like_salary(text):
            continue
        if _AGE_RE.match(text):
            continue
        title = text
        title_idx = i
        break

    if not title:
        return None

    # Among remaining p tags after title: classify location, salary, age
    for p in all_ps[title_idx + 1:]:
        text = p.get_text(strip=True)
        if not text:
            continue
        if _AGE_RE.match(text):
            continue
        if _looks_like_salary(text):
            salary_min, salary_max = _parse_salary(text)
        elif location == "Unknown":
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
    """Check if a text string looks like a salary range.

    Delegates to the shared ``looks_like_salary_range`` in ``_common.py``.
    """
    return looks_like_salary_range(text)


def _parse_salary(text: str) -> tuple[Optional[int], Optional[int]]:
    """Parse salary from Glassdoor format: '$178K - $250K (Employer est.)'

    Delegates to the shared ``parse_salary_range`` in ``_common.py``.
    """
    return parse_salary_range(text)
