"""Parse LinkedIn job alert emails into Job objects.

LinkedIn sends two types of alert emails:
1. jobalerts-noreply@linkedin.com - Job Alert digests (structured, ~6 jobs per email)
2. jobs-noreply@linkedin.com - "Explore new jobs" recommendations

Both use plain text format with a consistent pattern:
    Title
    Company
    Location

    [metadata line like "1 school alum" or "This company is actively hiring"]
    View job: https://www.linkedin.com/comm/jobs/view/{JOB_ID}/...
    ---------------------------------------------------------

"""

import logging
import re
from datetime import datetime
from typing import Optional

from job_finder.models import Job
from job_finder.parsers._common import is_meta_email, parse_salary_range

logger = logging.getLogger(__name__)

# Regex to extract LinkedIn job ID from the tracking URL
LINKEDIN_JOB_ID_RE = re.compile(r"/jobs/view/(\d+)/")

# LinkedIn-specific meta-email pattern (supplements the shared base set).
_LINKEDIN_EXTRA_PATTERNS = [
    re.compile(r"you.ll receive notifications", re.IGNORECASE),
]

# Detects actual job listing URLs in a LinkedIn email body.
_VIEW_JOB_RE = re.compile(r"View job:\s*https://", re.IGNORECASE)


def _is_meta_email(body: str) -> bool:
    """Return True if the email preamble matches known meta-email patterns.

    LinkedIn's newer digest format starts with a count line ("30+ new jobs
    match your preferences.") that would normally match BASE_META_PATTERNS,
    but the email body also contains actual job listings.  If "View job:" URLs
    are present the email is a real alert regardless of the preamble count.

    Delegates to the shared ``is_meta_email`` with LinkedIn's extra pattern.
    """
    if _VIEW_JOB_RE.search(body):
        return False
    return is_meta_email(body, extra_patterns=_LINKEDIN_EXTRA_PATTERNS)


def parse_linkedin_alert(body: str, email_date: Optional[datetime] = None) -> list[Job]:
    """Parse a LinkedIn job alert email body into Job objects.

    Args:
        body: Plain text email body from Gmail API.
        email_date: When the email was sent (used as approximate posting date).

    Returns:
        List of parsed Job objects, or [] for meta-email digests.
    """
    # Reject meta-emails (digest summaries, count notifications) before any parsing.
    # These are not individual job listings and would produce garbage job rows.
    if _is_meta_email(body):
        logger.debug("Skipping meta-email (pollution filter)")
        return []

    jobs = []

    # Split on the separator line
    blocks = re.split(r"-{10,}", body)

    for block in blocks:
        job = _parse_block(block.strip(), email_date)
        if job:
            jobs.append(job)

    return jobs


def _parse_block(block: str, email_date: Optional[datetime]) -> Optional[Job]:
    """Parse a single job block from a LinkedIn alert."""
    if not block:
        return None

    # Find the "View job:" URL
    url_match = re.search(r"View job:\s*(https://\S+)", block)
    if not url_match:
        return None

    raw_url = url_match.group(1)

    # Extract the LinkedIn job ID
    id_match = LINKEDIN_JOB_ID_RE.search(raw_url)
    source_id = id_match.group(1) if id_match else ""

    # Build a clean direct URL (strips tracking params)
    clean_url = f"https://www.linkedin.com/jobs/view/{source_id}/" if source_id else raw_url

    # Everything before "View job:" is the job info
    info_text = block[: url_match.start()].strip()

    # Split into non-empty lines
    lines = [line.strip() for line in info_text.split("\n") if line.strip()]

    if len(lines) < 2:
        return None

    # Filter out metadata lines (alumni counts, "actively hiring", salary snippets)
    # and preamble lines from the new LinkedIn digest format (count line, section headers).
    content_lines = []
    for line in lines:
        # Legacy metadata
        if re.match(r"^\d+ school alum", line, re.IGNORECASE):
            continue
        if "actively hiring" in line.lower():
            continue
        # Preamble: "Your job alert for …"
        if re.match(r"^Your job alert", line, re.IGNORECASE):
            continue
        # Count lines: "30+ new jobs match…" / "New jobs match…"
        if re.match(r"^\d+\+?\s+new\s+jobs?", line, re.IGNORECASE):
            continue
        if re.match(r"^New jobs", line, re.IGNORECASE):
            continue
        # New digest format section headers and navigation links
        if re.match(r"^Manage\b", line, re.IGNORECASE):
            continue
        if re.match(r"^Results from\b", line, re.IGNORECASE):
            continue
        # Navigation/footer links that bleed into blocks — never job titles
        if re.match(r"^See all jobs", line, re.IGNORECASE):
            continue
        if re.match(r"^View all jobs", line, re.IGNORECASE):
            continue
        if re.match(r"^https?://", line):
            continue
        content_lines.append(line)

    if len(content_lines) < 2:
        return None

    # Pattern: Title, Company, Location (in that order)
    title = content_lines[0]
    company = content_lines[1]
    location = content_lines[2] if len(content_lines) >= 3 else "Unknown"

    # Sanity check: a job title must not contain a URL (catches any navigation
    # lines that survived the filter loop above, e.g. mid-line URL patterns)
    if "https://" in title or "http://" in title:
        return None

    # Try to extract salary from the email snippet/subject if present
    salary_min, salary_max = _extract_salary(block)

    return Job(
        title=title,
        company=company,
        location=location,
        source="linkedin",
        source_url=clean_url,
        source_id=source_id,
        salary_min=salary_min,
        salary_max=salary_max,
        posted_date=email_date,
    )


def _extract_salary(text: str) -> tuple[Optional[int], Optional[int]]:
    """Try to extract salary range from text.

    Delegates to the shared ``parse_salary_range`` in ``_common.py``.
    Kept as a thin wrapper so internal callers and tests can continue
    importing ``_extract_salary`` from this module.
    """
    return parse_salary_range(text)
