"""Shared utilities for job alert email parsers.

Centralises the meta-email detection logic that was previously duplicated
across linkedin_parser, glassdoor_parser, and ziprecruiter_parser.

Note: indeed_parser intentionally uses its own pattern set (it must NOT
filter on "N new jobs" lines, which are real alerts for Indeed).
"""

import re

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
    patterns = BASE_META_PATTERNS if extra_patterns is None else BASE_META_PATTERNS + extra_patterns
    return any(pattern.search(preamble) for pattern in patterns)
