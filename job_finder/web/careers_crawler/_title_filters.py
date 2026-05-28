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

# Prefixes that map to "search form / search results" pages. We keep the bare
# path filtered (the form itself is nav), but allow `<prefix>/<segment>` shapes
# through — some ATS sites (e.g. ByteDance, `joinbytedance.com/search/<id>`)
# encode job-detail URLs under the same path, and over-aggressive prefix
# filtering eats every tile on the listing page.
_NAV_PREFIXES_WITH_SUBPATH_JOBS = ("/search",)


def _is_nav_path(path: str) -> bool:
    """Return True if `path` is a known navigation link (not a job listing).

    Most prefixes in `_NAV_PATH_PREFIXES` are matched as plain prefixes — any
    sub-path under `/about`, `/blog`, etc. is nav. The prefixes listed in
    `_NAV_PREFIXES_WITH_SUBPATH_JOBS` are filtered only when the path IS the
    prefix (optionally with a trailing slash); deeper paths under them are
    treated as job-detail URLs and let through.

    See FOLLOWUPS round-15 Gap #3 (ByteDance `/search/<id>` tiles).
    """
    path_lower = path.lower()
    for prefix in _NAV_PATH_PREFIXES:
        if not path_lower.startswith(prefix):
            continue
        if prefix in _NAV_PREFIXES_WITH_SUBPATH_JOBS:
            rest = path_lower[len(prefix) :].strip("/")
            if rest:
                # `/search/<id>` — job detail, not nav.
                continue
        return True
    return False

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

# Workday-style req ID glued to the end of the title with no separator:
# "Senior Technical Data Analyst - Operations E2E Data Intelligent Systems
# JR2018470US, CA, Santa Clara" — title runs into "JR2018470" (req ID) then
# straight into "US, CA, Santa Clara" (location). The req-id is the anchor:
# 2+ ALLCAPS letters followed by digits, preceded by a lowercase letter or
# closing paren (so we don't false-match an internal token like "E2E" which
# is preceded by whitespace). Strips from the req-id to end-of-string,
# absorbing any trailing location text that came glued on with it.
#
# The lookbehind plus `[A-Z]{2,}\d+` requirement keeps the false-positive
# rate low: bare ALLCAPS runs (state codes) are already handled by
# _NOSEP_TRAIL_LOC_RE, and natural title tokens like "E2E" / "AI" / "iOS"
# either have whitespace before them or lack the digit suffix.
#
# See FOLLOWUPS round 15 Gap #1 (NVIDIA Workday concatenation).
_REQID_PREFIX_RE = re.compile(r"(?<=[a-z\)])[A-Z]{2,}\d+.*$")

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


# Heuristic guards for detecting that a "title" is actually a glued metadata
# blob (description + location + posting date + req ID concatenated together).
# These come up on aggregator-style careers pages where the underlying HTML
# lays out fields as adjacent inline siblings — get_text(strip=True) merges
# them all without separators. See FOLLOWUPS.md 2026-05-27 audit.

# Maximum plausible job title length in characters. Real titles top out around
# 110 chars even with senior/staff/principal modifiers + parenthesized scopes.
# Beyond 140 the candidate is almost certainly a metadata blob.
_MAX_TITLE_LEN = 140

# Phrase markers that only appear in description/metadata text — never in a
# real title. Case-insensitive substring match.
_METADATA_PHRASE_MARKERS = (
    "posted ",  # "Posted 10 days ago"
    "apply by",  # "Apply byApr-29-26"
    "agency",  # "AgencyUNDP" (labeled-form aggregator)
    "post level",  # UNDP-style label
    "job title",  # UNDP-style label glued in
    "more accessible",  # "Innovation and Automation" body text
    "description ",  # Generic description leader
    "required:",  # Glued-in body text
    "responsibilities",  # Glued-in body text
)

# Currency symbol indicates compensation got concatenated into the title.
_HAS_DOLLAR_RE = re.compile(r"\$\s*\d")

# Req-ID-followed-by-pipe pattern: "SQL2354308|Chennai, Tamil Nadu" —
# digits run followed by a pipe and TitleCase text.
_REQ_ID_PIPE_RE = re.compile(r"\d{4,}\s*\|\s*[A-Z]")


def _is_metadata_blob(title: str) -> bool:
    """Detect titles that are actually concatenated metadata/description text.

    Used by careers_crawl extraction to skip aggregator pages where the
    surrounding markup glues the title together with location, req ID,
    posting date, and description preview without separator whitespace.
    Those rows produce titles like "Senior Data Scientist - GenAI...
    SQL2354308|Chennai, Tamil Nadu" or "Job TitleTech Lead AnalystPost
    levelNPSA-9Apply byApr-29-26AgencyUNDP..." that are useless for
    scoring or display.

    Conservative: prefers false negatives (let some glued blobs through)
    over false positives (drop a legitimate long title). Run this AFTER
    _clean_title has stripped suffixes and logo letters — short legitimate
    titles will never trip it.
    """
    if not title:
        return False
    if len(title) > _MAX_TITLE_LEN:
        return True
    lowered = title.lower()
    if any(marker in lowered for marker in _METADATA_PHRASE_MARKERS):
        return True
    if _HAS_DOLLAR_RE.search(title):
        return True
    if _REQ_ID_PIPE_RE.search(title):
        return True
    return False


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
    # separator-based location patterns first, then the Workday-style
    # req-id glob (which also absorbs trailing location text that came
    # glued on with the req-id), then the no-separator trailing-
    # uppercase run, then leading logo letters last.
    cleaned = _LOCATION_SUFFIX_RE.sub("", raw_text)
    cleaned = _strip_city_suffix_guarded(cleaned)
    cleaned = _REQID_PREFIX_RE.sub("", cleaned)
    cleaned = _NOSEP_TRAIL_LOC_RE.sub("", cleaned)
    cleaned = _strip_leading_logo_letters(cleaned)
    return cleaned.strip() or raw_text


def _strip_city_suffix_guarded(text: str) -> str:
    """Apply _CITY_SUFFIX_RE only when the text BEFORE the dash looks like a
    real job title — short + ALLCAPS prefixes are almost always brand
    abbreviations, not job titles, and stripping the brand off them is a
    silent data-loss bug.

    Concrete case that motivated this guard: 'MSI - Marvell Semiconductor'
    has the same trailing-TitleCase-after-dash shape as
    'Senior Engineer - San Francisco', but the suffix is a brand name, not a
    location. Heuristic: a legitimate job-title prefix is at least 5 chars
    long AND contains at least one lowercase letter (so 'MSI', 'IBM', 'AWS'
    are skipped). Tradeoff: we leak some bare-city suffixes through when the
    preceding title is short — but those titles are still informative, where
    'MSI' alone has zero brand signal.

    See FOLLOWUPS.md 2026-05-27 audit ("_CITY_SUFFIX_RE over-strip").
    """
    match = _CITY_SUFFIX_RE.search(text)
    if not match:
        return text
    before = text[: match.start()].strip()
    if len(before) >= 5 and any(c.islower() for c in before):
        return before
    return text
