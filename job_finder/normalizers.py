"""Foundation-layer normalization utilities for job dedup keys.

Contains pure normalization functions (no web-layer dependencies) that can be
imported by both job_finder.models and job_finder.web.dedup_normalizer without
creating an upward dependency from the foundation layer into the web layer.
"""

import html
import re

# ---------------------------------------------------------------------------
# Company name deterministic cleanup regexes
# ---------------------------------------------------------------------------

_HTML_TAG_RE = re.compile(r"<[^>]+>")

# Leading numeric prefix junk: "1. ", "123) ", "42 - " at start of string.
# Only stripped when the remainder after the match is non-empty.
_LEADING_NUMERIC_JUNK_RE = re.compile(r"^\d+[\.\-\)\s]+")


# ---------------------------------------------------------------------------
# Company suffix stripping
# Strip common legal entity suffixes, with or without preceding comma/period.
# Pattern: optional whitespace + optional comma + whitespace + suffix + optional period
# ---------------------------------------------------------------------------

_COMPANY_SUFFIXES = re.compile(
    r"""
    [,\s]+                          # optional comma then whitespace before suffix
    (?:
        inc\.?
        | incorporated\.?
        | llc\.?
        | corp\.?
        | corporation\.?
        | ltd\.?
        | limited\.?
        | co\.?
        | company\.?
        | technologies\.?
        | technology\.?
        | tech\.?
        | group\.?
        | holdings?\.?
        | services?\.?
        | solutions?\.?
    )
    \s*$                            # must be at end of string
    """,
    re.IGNORECASE | re.VERBOSE,
)

# ---------------------------------------------------------------------------
# Title abbreviation expansion
# Each tuple is (compiled_pattern, replacement_string).
# Order matters: sr. before sr (to handle period variant first).
# ---------------------------------------------------------------------------

_TITLE_ABBREVS = [
    # Seniority — match the abbreviation (with optional trailing period) surrounded
    # by word boundaries or end of string. Using (?:...) to capture the optional period
    # as part of the match so it does not remain in the output.
    (re.compile(r"\bsr\.(?=\s|$)", re.IGNORECASE), "senior"),
    (re.compile(r"\bjr\.(?=\s|$)", re.IGNORECASE), "junior"),
    (re.compile(r"\bmgr\.(?=\s|$)", re.IGNORECASE), "manager"),
    (re.compile(r"\beng\.(?=\s|$)", re.IGNORECASE), "engineering"),
    (re.compile(r"\bdir\.(?=\s|$)", re.IGNORECASE), "director"),
    (re.compile(r"\bvp\.(?=\s|$)", re.IGNORECASE), "vice president"),
    (re.compile(r"\bswe\.(?=\s|$)", re.IGNORECASE), "software engineer"),
    (re.compile(r"\bpm\.(?=\s|$)", re.IGNORECASE), "product manager"),
    # Also match without period (word boundary)
    (re.compile(r"\bsr\b(?!\.)", re.IGNORECASE), "senior"),
    (re.compile(r"\bjr\b(?!\.)", re.IGNORECASE), "junior"),
    (re.compile(r"\bmgr\b(?!\.)", re.IGNORECASE), "manager"),
]

# ---------------------------------------------------------------------------
# Title level suffix stripping
# Strip "(IC5)", "L5", "Level 3", "- Level III" etc. at end of title.
# ---------------------------------------------------------------------------

_TITLE_STRIP_SUFFIX = re.compile(
    r"""
    \s*
    (?:
        \(IC\d+\)                   # (IC5), (IC6)
        | \bIC\d+\b                 # IC5, IC6 without parens
        | \bL\d+\b                  # L5, L6, L7
        | \bLevel\s+\d+\b           # Level 3, Level 4
        | \bLvl\.?\s*\d+\b         # Lvl 3, Lvl. 4
        | [-–]\s*Level\s+\d+        # - Level 3
        | [-–]\s*L\d+               # - L5
        | \bI{1,3}V?\b             # Roman numerals I, II, III, IV at word boundary
        | \bVII?\b                  # VI, VII
    )
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize_company(company: str) -> str:
    """Normalize a company name for dedup key generation.

    Applies deterministic cleanup in order: HTML entity decode, HTML tag
    strip, whitespace collapse, leading numeric prefix strip, lowercase,
    then legal suffix stripping. All steps preserve the dedup invariant:
    same real company always maps to the same canonical name.

    Args:
        company: Raw company name string.

    Returns:
        Lowercased, suffix-stripped company name. Always lowercase — do not
        use as a display value; the raw input or name_raw column serves that
        purpose.
    """
    # 1. Decode HTML entities (e.g. "&amp;" -> "&", "&#34;" -> '"')
    normalized = html.unescape(company)
    # 2. Strip HTML tags (e.g. "<b>Acme</b>" -> "Acme")
    normalized = _HTML_TAG_RE.sub("", normalized)
    # 3. Collapse repeated whitespace
    normalized = " ".join(normalized.split())
    # 4. Strip leading numeric prefix junk only when remainder is non-empty
    #    e.g. "1. Acme Corp" -> "Acme Corp", but "100" stays "100"
    m = _LEADING_NUMERIC_JUNK_RE.match(normalized)
    if m and normalized[m.end() :].strip():
        normalized = normalized[m.end() :]
    # 5. Strip and lowercase (original behavior)
    normalized = normalized.strip().lower()
    # 6. Strip legal suffixes repeatedly (e.g. "Acme Corp. Inc." -> "acme")
    prev = None
    while normalized != prev:
        prev = normalized
        normalized = _COMPANY_SUFFIXES.sub("", normalized).strip()
    return normalized


def normalize_title(title: str) -> str:
    """Normalize a job title for dedup key generation.

    Expands common abbreviations (Sr. -> Senior) and strips level suffixes
    (IC5, Level 3) to reduce formatting noise.

    Args:
        title: Raw job title string.

    Returns:
        Lowercased, normalized title.
    """
    normalized = title.strip()

    # Strip level suffixes first (e.g., "Staff Engineer (IC5)" -> "Staff Engineer")
    normalized = _TITLE_STRIP_SUFFIX.sub("", normalized).strip()

    # Expand abbreviations
    for pattern, replacement in _TITLE_ABBREVS:
        normalized = pattern.sub(replacement, normalized)

    # Normalize whitespace and lowercase
    normalized = " ".join(normalized.split()).lower()
    return normalized
