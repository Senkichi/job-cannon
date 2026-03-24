"""Parse TrueUp weekly job digest emails into Job objects.

TrueUp sends HTML emails from hello@trueup.io with job cards containing
title, company, and location. Links go through url{N}.trueup.io tracking
redirects. No salary information is included.

Only jobs shown in the email body (~7 per digest) are parsed — the "View all
open jobs" link requires HTTP requests which are out of scope for parsers.
"""

import logging
import re
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse, parse_qs

from bs4 import BeautifulSoup

from job_finder.models import Job

logger = logging.getLogger(__name__)

# Matches TrueUp tracking redirect URLs (varying subdomains)
TRUEUP_LINK_RE = re.compile(r"url\d+\.trueup\.io/ls/click", re.IGNORECASE)

# Navigation/footer links to exclude
_EXCLUDE_TEXTS = frozenset({
    "view all open jobs", "view all open jobs  →", "trueup",
    "update preferences", "unsubscribe", "my trueup",
})


def parse_trueup_alert(
    body: str, email_date: Optional[datetime] = None
) -> list[Job]:
    """Parse a TrueUp weekly digest email into Job objects.

    Args:
        body: Email body from Gmail API (HTML).
        email_date: When the email was sent.

    Returns:
        List of parsed Job objects (may be empty).
    """
    if not body or not body.strip():
        return []

    soup = BeautifulSoup(body, "html.parser")

    # Find all TrueUp tracking links
    all_links = soup.find_all("a", href=TRUEUP_LINK_RE)
    if not all_links:
        return []

    # Group links by card container. Each job card has a <table> parent
    # containing a title link and a company link.
    cards = _find_job_cards(all_links)

    jobs = []
    seen: set[tuple[str, str]] = set()  # (title, company) dedup

    for card in cards:
        title = card.get("title")
        company = card.get("company")
        if not title or not company:
            continue

        key = (title.lower(), company.lower())
        if key in seen:
            continue
        seen.add(key)

        source_url = card.get("url", "")
        source_id = _extract_source_id(source_url)

        jobs.append(Job(
            title=title,
            company=company,
            location=card.get("location", "Unknown"),
            source="trueup",
            source_url=source_url,
            source_id=source_id,
            posted_date=email_date,
        ))

    return jobs


def _find_job_cards(links) -> list[dict]:
    """Group TrueUp links into job card dicts.

    Each card in the email has two relevant <a> tags:
    1. Title link (in a div with font-weight:600)
    2. Company link (in the next div)

    We walk up from each link to find its card container (a <div> that
    contains a <table>), then extract fields from the card.
    """
    processed_containers = set()
    cards = []

    for link in links:
        # Skip navigation/footer links
        link_text = link.get_text(strip=True).lower()
        if link_text in _EXCLUDE_TEXTS:
            continue

        # Find the card container: walk up to a div that contains a table
        container = _find_card_container(link)
        if container is None:
            continue

        # Avoid processing the same card twice
        container_id = id(container)
        if container_id in processed_containers:
            continue
        processed_containers.add(container_id)

        card = _extract_card_fields(container)
        if card:
            cards.append(card)

    return cards


def _find_card_container(element):
    """Walk up from an element to find the job card container div.

    A card container is a <div> that directly contains a <table> — this
    matches TrueUp's card layout without relying on CSS styles.
    """
    current = element.parent
    for _ in range(10):  # limit traversal depth
        if current is None or current.name == "body":
            break
        if current.name == "div" and current.find("table", recursive=False):
            return current
        current = current.parent
    return None


def _extract_card_fields(container) -> Optional[dict]:
    """Extract job fields from a card container div."""
    # Find all trueup links in this card
    card_links = container.find_all("a", href=TRUEUP_LINK_RE)
    # Filter out footer/nav links
    job_links = [
        a for a in card_links
        if a.get_text(strip=True).lower() not in _EXCLUDE_TEXTS
        and len(a.get_text(strip=True)) > 1
    ]

    if len(job_links) < 2:
        return None

    title = job_links[0].get_text(strip=True)
    company = job_links[1].get_text(strip=True)
    url = job_links[0].get("href", "")

    # Validate: title should be a reasonable job title
    if not title or len(title) > 150 or len(title) < 3:
        return None
    if not company or len(company) > 80:
        return None

    # Extract location: look for a div with font-weight:500 style,
    # or fall back to scanning for location-like text
    location = _extract_location(container, title, company)

    return {
        "title": title,
        "company": company,
        "location": location,
        "url": url,
    }


def _extract_location(container, title: str, company: str) -> str:
    """Extract location text from a card container.

    Looks for a div with font-weight:500 in its style (TrueUp's location
    pattern), then falls back to scanning for location-like text.
    """
    title_lower = title.lower()
    company_lower = company.lower()

    for div in container.find_all("div"):
        style = div.get("style", "")
        if "font-weight:500" in style or "font-weight: 500" in style:
            text = div.get_text(strip=True)
            if text and 2 < len(text) < 150:
                return text

    # Fallback: look for all-caps or comma-separated location patterns
    for div in container.find_all("div"):
        text = div.get_text(strip=True)
        if not text or len(text) < 3 or len(text) > 150:
            continue
        if text.lower() == title_lower or text.lower() == company_lower:
            continue
        # Check for location-like patterns: "CITY, STATE" or "US, CA, CITY"
        if re.match(r"^[A-Z\s,/]+$", text) and "," in text:
            return text

    return "Unknown"


def _extract_source_id(url: str) -> str:
    """Extract a source_id from a TrueUp tracking redirect URL.

    Uses the upn query parameter (unique per job link). Truncated for sanity.
    """
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        if "upn" in qs:
            return qs["upn"][0][:64]
    except Exception:
        logger.debug("trueup source_id extraction failed", exc_info=True)
    # Fallback: use the last path segment
    try:
        parts = urlparse(url).path.rstrip("/").split("/")
        if parts:
            return parts[-1][:64]
    except Exception:
        pass
    return ""
