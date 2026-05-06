"""Title hygiene + URL-path navigation filters for careers crawl extraction.

Pure functions. No I/O, no shared state. Imported by the static and
Playwright tier modules to filter and clean candidate job postings
before keyword matching.
"""

from __future__ import annotations

import re

# Links with these path prefixes are navigation, not job listings
_NAV_PATH_PREFIXES = (
    "/about",
    "/contact",
    "/blog",
    "/news",
    "/press",
    "/privacy",
    "/terms",
    "/legal",
    "/login",
    "/signup",
    "/register",
    "/faq",
    "/help",
    "/support",
    "/accessibility",
    "/sitemap",
    "/cookie",
    "/search",
    "/events",
)

# Regex to strip trailing location text from concatenated title+location
_LOCATION_SUFFIX_RE = re.compile(
    r"\s*[-–—|·•]\s*(?:Remote|Hybrid|On-?site|Anywhere|Multiple|Worldwide).*$",
    re.IGNORECASE,
)

# Broader location suffix: city/state/country patterns at end of title
# Matches: "- New York, NY", "- San Francisco, CA", "- United States", etc.
_CITY_SUFFIX_RE = re.compile(
    r"\s*[-–—|·•]\s*[A-Z][a-z]+(?:\s[A-Z][a-z]+)*(?:,\s*[A-Z]{2,})?\s*$",
)


def _clean_title(tag, raw_text: str) -> str:
    """Extract clean job title from a link tag, stripping appended location.

    Strategy:
    1. If the <a> has child elements (span/div), use the first text-bearing
       child as the title (common pattern: title span + location span).
    2. Otherwise, strip known location suffix patterns from the raw text.

    Args:
        tag: BeautifulSoup <a> tag.
        raw_text: Full text from tag.get_text(strip=True).

    Returns:
        Cleaned title string.
    """
    # Strategy 1: Check for structured children (span, div, h2, h3, p)
    title_children = tag.find_all(["span", "div", "h2", "h3", "h4", "p"], recursive=False)
    if title_children:
        first_text = title_children[0].get_text(strip=True)
        if first_text and len(first_text) >= 5:
            return first_text

    # Strategy 2: Regex stripping of location suffixes
    cleaned = _LOCATION_SUFFIX_RE.sub("", raw_text)
    cleaned = _CITY_SUFFIX_RE.sub("", cleaned)
    return cleaned.strip() or raw_text
