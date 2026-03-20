"""Parse Indeed job alert emails into Job objects.

Indeed alert emails come from alert@indeed.com. Real emails arrive as plain
text via gmail_source._extract_body() which prefers text/plain over text/html.
The plain-text format uses engage.indeed.com tracking redirect URLs.

This parser uses a two-strategy approach:
  1. Plain-text strategy (primary): parse plain-text Indeed alert emails that
     contain engage.indeed.com URLs. Job blocks are delimited by these URLs.
  2. HTML strategy (fallback): legacy BeautifulSoup link/card strategies for
     any HTML-format emails (e.g., fabricated test fixtures or future changes).

If neither strategy finds jobs, a warning is logged.
"""

import logging
import re
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse, parse_qs

from bs4 import BeautifulSoup

from job_finder.models import Job

logger = logging.getLogger(__name__)

# Matches Indeed job page URLs (not just the domain) — used by HTML fallback
INDEED_JOB_URL_RE = re.compile(
    r"indeed\.com/(viewjob|rc/clk|job/)",
    re.IGNORECASE,
)

# Matches engage.indeed.com tracking redirect URLs (plain-text format)
INDEED_ENGAGE_URL_RE = re.compile(
    r"https://engage\.indeed\.com/f/a/\S+",
    re.IGNORECASE,
)

# Salary range: "$120K - $150K" or "$120,000 - $150,000"
SALARY_RE = re.compile(
    r"\$(\d[\d,]*)\s*[Kk]?\s*[-\u2013]+\s*\$(\d[\d,]*)\s*[Kk]?"
)

# Hourly rate: "$25/hr" or "$25.50 / hour"
HOURLY_RE = re.compile(r"\$(\d[\d.]+)\s*(?:\/\s*(?:hr|hour))", re.IGNORECASE)

# Meta-email patterns checked against the first 200 characters only.
# IMPORTANT: Do NOT filter on "new jobs" or "N new jobs" — those ARE real alerts.
# Only filter administrative / digest emails with NO individual job cards.
_META_PATTERNS = [
    re.compile(r"job alert digest|weekly digest", re.IGNORECASE),
    re.compile(r"confirm.*email.*(?:address|subscription)", re.IGNORECASE),
    re.compile(r"unsubscribe from.*alerts?$", re.IGNORECASE | re.MULTILINE),
]

# Generic link texts that are NOT job titles
_GENERIC_LINK_TEXTS = frozenset(
    {"apply", "view", "click here", "apply now", "see more", "learn more",
     "view job", "apply for job", "see all jobs", "view all jobs"}
)

# Plain-text: header line separating preamble from job listings
# e.g. "10+ new analytics manager jobs in San Francisco Bay Area, CA"
# e.g. "1 new lead data analyst job in San Francisco Bay Area, CA"
_PLAINTEXT_HEADER_RE = re.compile(
    r"^\d+\+?\s+new\s+.+\s+jobs?\s+in\s+",
    re.IGNORECASE | re.MULTILINE,
)

# Plain-text: noise lines to skip when extracting job fields
_AGE_LINE_RE = re.compile(
    r"^(Just posted|\d+\+?\s+days?\s+ago)$",
    re.IGNORECASE,
)

# Plain-text: footer start marker — stop processing job blocks here
_FOOTER_RE = re.compile(
    r"^(\u00a9|\(c\)|Indeed Tower)",
    re.IGNORECASE | re.MULTILINE,
)

# Location pattern: state abbreviations, "Remote", "Hybrid", city+state patterns
_LOCATION_RE = re.compile(
    r"\b(remote|hybrid|onsite|on-site|[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*,\s*[A-Z]{2}|[A-Z]{2}\s*\d{5})\b"
)


def _is_meta_email(preamble: str) -> bool:
    """Return True if the email preamble matches known meta-email patterns.

    Only inspects the first 200 characters of the body to avoid false positives
    from job titles or descriptions that contain pattern-matching words.
    """
    return any(pattern.search(preamble) for pattern in _META_PATTERNS)


def _looks_like_html(body: str) -> bool:
    """Return True if the body contains HTML tags (indicating HTML format)."""
    return bool(re.search(r"<(?:a|table|div|tr|td|span|html|body)\b", body, re.IGNORECASE))


def _extract_job_id_from_engage_url(url: str) -> str:
    """Extract the encoded job ID from an engage.indeed.com/f/a/... URL.

    Returns the path segment immediately after /f/a/ as the source_id.
    For  https://engage.indeed.com/f/a/JOB1_ENCODED_URL_STRING  this returns
    JOB1_ENCODED_URL_STRING.
    """
    try:
        parsed = urlparse(url)
        # Path looks like /f/a/ENCODED_SEGMENT
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 3 and parts[0] == "f" and parts[1] == "a":
            return parts[2]
        if parts:
            return parts[-1]
    except Exception:
        logger.debug("indeed field extraction failed", exc_info=True)
    return ""


def _parse_plaintext(body: str, email_date: Optional[datetime]) -> list[Job]:
    """Parse a plain-text Indeed alert email body into Job objects.

    Algorithm:
    1. Find the job count header line ("N+ new X jobs in Y") to locate where
       job listings start. Discard everything before it (preamble).
    2. Stop at the footer marker (copyright line).
    3. Find all engage.indeed.com URLs in the job section. Each URL is the
       trailing link of a job block.
    4. For each URL, the job block is the text between the previous URL end
       and the start of this URL.
    5. Parse each block: title (line 0), company-location (line 1), salary
       (any line with $), ignoring noise lines (age, "Easily apply").
    """
    # Find header line; use its end as the start of job content
    header_match = _PLAINTEXT_HEADER_RE.search(body)
    if header_match:
        # Start parsing from end of header line
        header_line_end = body.index("\n", header_match.start())
        job_section_start = header_line_end + 1
    else:
        job_section_start = 0

    # Truncate at footer marker
    footer_match = _FOOTER_RE.search(body, job_section_start)
    if footer_match:
        job_section = body[job_section_start:footer_match.start()]
    else:
        job_section = body[job_section_start:]

    # Find all engage.indeed.com URLs in the job section
    url_matches = list(INDEED_ENGAGE_URL_RE.finditer(job_section))
    if not url_matches:
        return []

    jobs = []
    prev_end = 0  # tracks end of previous URL match within job_section

    for url_match in url_matches:
        url = url_match.group(0)
        block_text = job_section[prev_end:url_match.start()]
        prev_end = url_match.end()

        job = _parse_plaintext_job_block(block_text, url, email_date)
        if job:
            jobs.append(job)

    return jobs


def _parse_plaintext_job_block(
    block_text: str,
    url: str,
    email_date: Optional[datetime],
) -> Optional[Job]:
    """Parse a single job block (text preceding an engage.indeed.com URL).

    Returns a Job if the block looks like a real job listing (has title +
    company-location), or None if it's a preamble/footer link block.
    """
    # Split into non-empty lines, filtering noise
    raw_lines = [line.strip() for line in block_text.split("\n") if line.strip()]
    content_lines = []
    salary_line = None

    for line in raw_lines:
        # Skip age lines ("Just posted", "1 day ago", etc.)
        if _AGE_LINE_RE.match(line):
            continue
        # Skip "Easily apply" noise
        if line.lower() == "easily apply":
            continue
        # Check for salary before adding to content lines
        if "$" in line and _extract_salary_from_text(line) != (None, None):
            salary_line = line
            continue
        content_lines.append(line)

    # Need at least title + company-location to qualify as a job block
    if len(content_lines) < 2:
        return None

    title = content_lines[0]

    # Skip lines that look like URLs (preamble browse/unsubscribe blocks)
    if title.startswith("http"):
        return None

    # Skip very long titles (description snippets, not titles)
    if len(title) > 150:
        return None

    # Parse company and location from "Company - City, ST" format
    company_location = content_lines[1]
    if " - " in company_location:
        # Use rfind to handle company names containing dashes (e.g., "Turn/River - SF, CA")
        dash_idx = company_location.rfind(" - ")
        company = company_location[:dash_idx].strip()
        location = company_location[dash_idx + 3:].strip()
    else:
        company = company_location
        location = "Unknown"

    # Extract salary from salary line if present
    salary_min, salary_max = _extract_salary_from_text(salary_line) if salary_line else (None, None)

    # Extract source_id from the engage URL path
    source_id = _extract_job_id_from_engage_url(url)

    return Job(
        title=title,
        company=company,
        location=location,
        source="indeed",
        source_url=url,
        source_id=source_id,
        salary_min=salary_min,
        salary_max=salary_max,
        posted_date=email_date,
    )


def parse_indeed_alert(body: str, email_date: Optional[datetime] = None) -> list[Job]:
    """Parse an Indeed job alert email body into Job objects.

    Uses plain-text parsing as the primary strategy (real Indeed emails arrive
    as plain text via Gmail API). Falls back to HTML strategies for legacy or
    fabricated HTML content.

    Meta-emails (digests, subscription confirmations without jobs) are filtered.

    Args:
        body: Email body from Gmail API. May be None or HTML or plain text.
        email_date: When the email was sent.

    Returns:
        List of parsed Job objects (may be empty if no jobs found).
    """
    if not body or not body.strip():
        return []

    if _is_meta_email(body[:200]):
        logger.debug("Indeed parser: skipping meta-email (digest/admin)")
        return []

    # Plain-text strategy (primary): real Indeed emails are plain text
    if not _looks_like_html(body):
        jobs = _parse_plaintext(body, email_date)
        if jobs:
            return jobs

    # HTML strategy (fallback): legacy HTML format or future changes
    soup = BeautifulSoup(body, "html.parser")

    jobs = _parse_with_link_strategy(soup, email_date)

    if not jobs:
        jobs = _parse_with_card_strategy(soup, email_date)

    if not jobs:
        logger.warning(
            "Indeed parser: no jobs found -- email format may have changed. "
            "Archive the raw email and update indeed_parser.py."
        )

    return jobs


def _parse_with_link_strategy(
    soup: BeautifulSoup, email_date: Optional[datetime]
) -> list[Job]:
    """Strategy 1: Find all anchor tags with Indeed job URLs."""
    jobs = []
    seen_urls: set[str] = set()

    job_links = soup.find_all("a", href=INDEED_JOB_URL_RE)

    for link in job_links:
        href = link.get("href", "")
        if not href or href in seen_urls:
            continue
        seen_urls.add(href)

        job = _extract_job_from_link(link, href, email_date)
        if job:
            jobs.append(job)

    return jobs


def _parse_with_card_strategy(
    soup: BeautifulSoup, email_date: Optional[datetime]
) -> list[Job]:
    """Strategy 2 (fallback): Find td/div containers with Indeed job links."""
    jobs = []
    seen_urls: set[str] = set()

    candidates = soup.find_all(["td", "div"], limit=200)

    for el in candidates:
        links = el.find_all("a", href=INDEED_JOB_URL_RE)
        if not links:
            continue

        href = links[0].get("href", "")
        if not href or href in seen_urls:
            continue
        seen_urls.add(href)

        text = el.get_text(separator="\n", strip=True)
        if len(text) < 5 or len(text) > 3000:
            continue

        job = _extract_job_from_container(el, links[0], href, email_date)
        if job:
            jobs.append(job)

    return jobs


def _extract_job_from_link(
    link_tag, href: str, email_date: Optional[datetime]
) -> Optional[Job]:
    """Extract job data from a job link and its surrounding context."""
    title = _extract_title_from_link(link_tag)
    if not title:
        return None

    # Find best container for context extraction
    container = (
        link_tag.find_parent("tr")
        or link_tag.find_parent("td")
        or link_tag.find_parent("div")
        or link_tag.parent
    )

    company = _extract_company_from_context(container, title) if container else "Unknown"
    location = _extract_location_from_context(container) if container else "Unknown"
    salary_min, salary_max = _extract_salary_from_text(
        container.get_text(separator=" ", strip=True) if container else ""
    )
    source_id = _extract_job_id(href)

    return Job(
        title=title,
        company=company,
        location=location,
        source="indeed",
        source_url=href,
        source_id=source_id,
        salary_min=salary_min,
        salary_max=salary_max,
        posted_date=email_date,
    )


def _extract_job_from_container(
    container, link_tag, href: str, email_date: Optional[datetime]
) -> Optional[Job]:
    """Extract job data from a container element (card strategy)."""
    title = _extract_title_from_link(link_tag)
    if not title:
        # Try text-based extraction from container
        lines = [
            line.strip()
            for line in container.get_text(separator="\n", strip=True).split("\n")
            if line.strip() and len(line.strip()) > 3
        ]
        if lines:
            title = lines[0]
        if not title or len(title) > 120:
            return None

    company = _extract_company_from_context(container, title)
    location = _extract_location_from_context(container)
    salary_min, salary_max = _extract_salary_from_text(
        container.get_text(separator=" ", strip=True)
    )
    source_id = _extract_job_id(href)

    return Job(
        title=title,
        company=company,
        location=location,
        source="indeed",
        source_url=href,
        source_id=source_id,
        salary_min=salary_min,
        salary_max=salary_max,
        posted_date=email_date,
    )


def _extract_title_from_link(link_tag) -> Optional[str]:
    """Extract job title from an anchor tag."""
    # Check heading elements inside the link first
    for tag in ["h1", "h2", "h3", "h4", "strong", "b"]:
        el = link_tag.find(tag)
        if el:
            text = el.get_text(strip=True)
            if text and 3 <= len(text) <= 120:
                return text

    # Try link text directly
    link_text = link_tag.get_text(strip=True)
    if link_text and 3 <= len(link_text) <= 120:
        if link_text.lower() not in _GENERIC_LINK_TEXTS:
            return link_text

    # Check aria-label
    aria = link_tag.get("aria-label", "")
    if aria and 3 <= len(aria) <= 120:
        if aria.lower() not in _GENERIC_LINK_TEXTS:
            return aria

    return None


def _extract_company_from_context(container, title_text: str) -> str:
    """Find company name in elements near the job link.

    Indeed typically puts company name in a nearby span/td/div element.
    """
    if container is None:
        return "Unknown"

    title_lower = title_text.lower()

    # Search immediate children and descendants (spans, divs, tds, ps)
    for el in container.find_all(["span", "td", "div", "p"]):
        text = el.get_text(strip=True)
        # Company name heuristics: 2-60 chars, not the title, not salary-like
        if not text or len(text) < 2 or len(text) > 60:
            continue
        if text.lower() == title_lower:
            continue
        if _looks_like_salary_text(text):
            continue
        if _looks_like_location(text):
            continue
        # Skip generic navigation text
        if text.lower() in {"view job", "apply", "apply now", "see more", "click here"}:
            continue
        # Skip if it looks like a URL
        if "http" in text or "www." in text:
            continue
        return text

    return "Unknown"


def _extract_location_from_context(container) -> str:
    """Find location text in elements near the job link."""
    if container is None:
        return "Unknown"

    for el in container.find_all(["span", "td", "div", "p"]):
        text = el.get_text(strip=True)
        if text and len(text) < 100 and _looks_like_location(text):
            return text

    return "Unknown"


def _looks_like_location(text: str) -> bool:
    """Return True if text matches common location patterns."""
    return bool(_LOCATION_RE.search(text))


def _looks_like_salary_text(text: str) -> bool:
    """Return True if text contains a salary value."""
    return bool(re.search(r"\$\d+", text))


def _extract_salary_from_text(text: str) -> tuple[Optional[int], Optional[int]]:
    """Parse salary range from text. Returns (salary_min, salary_max)."""
    match = SALARY_RE.search(text)
    if match:
        low_str = match.group(1).replace(",", "")
        high_str = match.group(2).replace(",", "")
        try:
            low = int(low_str)
            high = int(high_str)
            # Convert K notation to full values
            if low < 1000:
                low *= 1000
            if high < 1000:
                high *= 1000
            return low, high
        except ValueError:
            pass

    # Try hourly rate
    match = HOURLY_RE.search(text)
    if match:
        try:
            hourly = float(match.group(1))
            annual = int(hourly * 2080)  # 40hr/wk * 52wk
            return annual, annual
        except ValueError:
            pass

    return None, None


def _extract_job_id(url: str) -> str:
    """Extract the Indeed job key from a job URL.

    Tries jk= query parameter first (viewjob?jk=XXXX), then rc/clk?jk=XXXX,
    then falls back to the last path segment.
    """
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        if "jk" in qs:
            return qs["jk"][0]
        # Try path-based job ID
        path_parts = [p for p in parsed.path.split("/") if p]
        if path_parts:
            return path_parts[-1]
    except Exception:
        logger.debug("indeed field extraction failed", exc_info=True)

    return ""
