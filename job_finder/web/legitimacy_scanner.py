"""Legitimacy scanner — pattern-based scam/MLM detector for job descriptions.

Scans jd_full for known scam / MLM / get-rich-quick patterns and returns a
brief note string when a match is detected. The note is written to
jobs.legitimacy_note by the scoring orchestrator BEFORE persist_job_assessment
reads it, so the ``if legitimacy_note: reject`` branch in
``derive_classification`` (job_finder/db/_classification.py) fires correctly.

Conservative by design: false negatives are preferred over false positives.
All patterns are literal substring matches (case-insensitive) or regexes.
The first matched pattern wins — this is a flag, not a full-text classifier.

False-positive recovery: clear a false-positive legitimacy_note by manually
NULLing the field via /admin/review or a direct SQL UPDATE on the row.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Pattern registry
# ---------------------------------------------------------------------------

# Literal substring patterns (compared case-insensitively against jd_full).
# Format: (tag, literal_substring)
_LITERAL_PATTERNS: tuple[tuple[str, str], ...] = (
    ("mlm_income", "unlimited income potential"),
    ("mlm_boss", "be your own boss"),
    ("mlm_wfh", "work from home opportunity"),
    ("mlm_leader", "join our team of leaders"),
    ("mlm_freedom", "financial freedom"),
    ("mlm_residual", "residual income"),
    ("mlm_downline", "recruit your downline"),
    ("mlm_earnings", "earnings depend on"),
    ("crypto", "crypto trading opportunity"),
)

# Regex patterns (compiled once at import time).
# Format: (tag, compiled_regex)
_REGEX_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("earn_per_day", re.compile(r"earn\s+\$\d{3,}/day", re.IGNORECASE)),
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def scan_legitimacy(jd_full: str | None) -> str | None:
    """Scan a job description for scam / MLM / get-rich-quick patterns.

    Returns a brief note string (e.g. ``"suspicious_pattern: mlm_income"``)
    when the FIRST matching pattern is found; returns ``None`` otherwise.
    An empty or None ``jd_full`` always returns ``None``.

    The returned string is written to ``jobs.legitimacy_note`` by the scoring
    orchestrator, which causes ``derive_classification`` to return ``"reject"``
    for the job.

    Args:
        jd_full: Full job description text. May be None or empty.

    Returns:
        A short note string on match, or None on no match.
    """
    if not jd_full:
        return None

    lower = jd_full.lower()

    for tag, literal in _LITERAL_PATTERNS:
        if literal in lower:
            return f"suspicious_pattern: {tag}"

    for tag, regex in _REGEX_PATTERNS:
        if regex.search(jd_full):
            return f"suspicious_pattern: {tag}"

    return None
