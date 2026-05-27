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

# No-separator trailing location: title ends with a 2+ uppercase character
# run preceded immediately by a lowercase letter or closing paren (no space
# or hyphen between them). The trailing run must either run to end-of-string
# alone, be followed by a comma-separated list of TitleCase tokens, or be
# followed by a parenthesized qualifier — these three shapes match real
# concatenated location text like "(Evergreen)NY, DC, Oakland",
# "ScientistUSA(Remote)", or "(Backend)CA". The ALLCAPS-only end-of-string
# requirement protects single-word PascalCase titles like "iOS Developer"
# (where stripping starting at "OS" would leave just "i").
_NOSEP_TRAIL_LOC_RE = re.compile(
    r"""
    (?<=[a-z\)])                            # title-end context: lowercase or close paren
    (?=[A-Z][A-Z])                          # next chars are 2+ caps
    [A-Z]{2,}                               # an ALLCAPS run (state code, country, etc.)
    (?:
        (?:,\s*[A-Za-z][A-Za-z]+)+          # comma-separated list of TitleCase words
        |
        \([^)]*\)                           # parenthesized qualifier (Remote)
    )?
    \s*$                                    # must reach end of string
    """,
    re.VERBOSE,
)

# Leading "logo placeholder" letters: 1-2 ALLCAPS letters glued to the start
# of the real title with no separator, where the next character starts a
# Capital+lowercase word. Catches aggregator garbage like "CSenior Vice
# President" (Citigroup logo letter), "EHApplication Development" (Evernorth
# Health), "NLead Analyst" (NewYork-Presbyterian). Safe because "IT
# Specialist", "AI Engineer", "MSI - Marvell" all have a space or non-
# lowercase second char that breaks the lookahead.
_LEADING_LOGO_LETTERS_RE = re.compile(r"^([A-Z]{1,2})(?=[A-Z][a-z])")


def _strip_leading_logo_letters(s: str) -> str:
    """Strip 1-2 leading ALLCAPS letters that are a logo placeholder."""
    return _LEADING_LOGO_LETTERS_RE.sub("", s, count=1)


def _clean_title(tag, raw_text: str) -> str:
    """Extract clean job title from a link tag, stripping appended location.

    Strategy:
    1. A real heading tag (h1-h6) anywhere inside the link wins — careers
       pages with structured markup (e.g. Blue State, Greenhouse-style
       wrappers) put the title in a heading and the location in a sibling
       span, which get_text(strip=True) would otherwise concatenate without
       whitespace.
    2. First direct child element (existing behavior for span/div-only
       markup).
    3. Regex stripping on the raw text — handles dash/pipe-separated
       location suffixes, no-separator state lists, and leading logo
       placeholder letters.

    Args:
        tag: BeautifulSoup <a> tag.
        raw_text: Full text from tag.get_text(strip=True).

    Returns:
        Cleaned title string.
    """
    # Strategy 1: a heading anywhere inside (recursive). Many careers pages
    # nest the heading inside a wrapper div which would otherwise hide it
    # from recursive=False.
    heading = tag.find(["h1", "h2", "h3", "h4", "h5", "h6"])
    if heading is not None:
        heading_text = heading.get_text(strip=True)
        if heading_text and len(heading_text) >= 5:
            return _strip_leading_logo_letters(heading_text)

    # Strategy 2: direct text-bearing child (existing behavior, preserved
    # so structured markup without a heading tag still works).
    title_children = tag.find_all(["span", "div", "h2", "h3", "h4", "p"], recursive=False)
    if title_children:
        first_text = title_children[0].get_text(strip=True)
        if first_text and len(first_text) >= 5:
            return _strip_leading_logo_letters(first_text)

    # Strategy 3: regex strip on the raw text. Order matters — strip
    # separator-based location patterns first, then the no-separator
    # trailing-uppercase run, then leading logo letters last.
    cleaned = _LOCATION_SUFFIX_RE.sub("", raw_text)
    cleaned = _CITY_SUFFIX_RE.sub("", cleaned)
    cleaned = _NOSEP_TRAIL_LOC_RE.sub("", cleaned)
    cleaned = _strip_leading_logo_letters(cleaned)
    return cleaned.strip() or raw_text
