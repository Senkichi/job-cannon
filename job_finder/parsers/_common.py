"""Shared utilities for job alert email parsers.

Centralises the meta-email detection logic that was previously duplicated
across linkedin_parser, glassdoor_parser, and ziprecruiter_parser.

Also provides shared salary parsing (parse_salary_range) used by all four
parsers, eliminating near-identical regex + K-notation conversion code.

Note: indeed_parser uses is_meta_email with _INDEED_META_PATTERNS as extra_patterns
(must NOT filter on "N new jobs" lines, which are real alerts for Indeed).
"""

import re

# ---------------------------------------------------------------------------
# Salary parsing
# ---------------------------------------------------------------------------

# Salary range: "$120K - $150K", "$120,000 - $150,000", "$168K-$255K"
# Handles K-notation, comma-separated full-dollar amounts, and en-dash.
SALARY_RANGE_RE = re.compile(r"\$(\d[\d,]*)\s*[Kk]?\s*[-\u2013]+\s*\$(\d[\d,]*)\s*[Kk]?")


def parse_salary_range(text: str) -> tuple[int | None, int | None]:
    """Extract a salary range from free-form text.

    Handles formats like:
        $168K-$255K / year salary
        $150,000 - $200,000
        $120K - $150K (Employer est.)

    Returns:
        (salary_min, salary_max) as full dollar ints, or (None, None).
    """
    match = SALARY_RANGE_RE.search(text)
    if not match:
        return None, None

    low_str = match.group(1).replace(",", "")
    high_str = match.group(2).replace(",", "")

    try:
        low = int(low_str)
        high = int(high_str)
    except ValueError:
        return None, None

    # Convert K-notation to full dollar values
    if low < 1000:
        low *= 1000
    if high < 1000:
        high *= 1000

    return low, high


def looks_like_salary_range(text: str) -> bool:
    """Return True if *text* contains a salary range pattern ($X - $Y)."""
    return bool(SALARY_RANGE_RE.search(text))


def looks_like_salary_text(text: str) -> bool:
    """Return True if *text* contains any dollar amount (e.g. '$120K')."""
    return bool(re.search(r"\$\d+", text))


# ---------------------------------------------------------------------------
# Meta-email detection
# ---------------------------------------------------------------------------

# Base meta-email patterns checked against the first 200 characters of the
# email body.  Checking only the preamble avoids false positives where job
# titles contain phrases like "30+ new" (per Research Pitfall 4).
BASE_META_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^\d+\+?\s+new\s+jobs?\s+match", re.IGNORECASE | re.MULTILINE),
    re.compile(r"job alert digest|weekly digest", re.IGNORECASE),
    re.compile(r"you have \d+ new jobs?", re.IGNORECASE),
    re.compile(r"^\d+ jobs? found", re.IGNORECASE | re.MULTILINE),
]


def is_meta_email(body: str, extra_patterns: list[re.Pattern[str]] | None = None) -> bool:
    """Return True if the email preamble matches known meta-email patterns.

    Only inspects the first 200 characters of the body to avoid false positives
    from job titles or descriptions that contain pattern-matching words.

    Args:
        body: Email body text.
        extra_patterns: Additional compiled regex patterns to check beyond the
            base set.  Used by parsers that need source-specific patterns
            (e.g. LinkedIn's "you'll receive notifications" check).

    Returns:
        True if the body looks like a digest/count summary, not a job alert.
    """
    preamble = body[:200]
    patterns = (
        BASE_META_PATTERNS if extra_patterns is None else BASE_META_PATTERNS + extra_patterns
    )
    return any(pattern.search(preamble) for pattern in patterns)
