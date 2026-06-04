"""Legitimacy scanner — scam / MLM pattern detection for job descriptions.

Scans ``jd_full`` for known scam, MLM, and get-rich-quick patterns and
returns a brief note string when a suspicious pattern is matched, or
``None`` when the text is clean.

Design rationale (§13 / D-12 from the ingestion-contract-enforcement spec):
  - Conservative pattern set: false negatives are preferred over false
    positives. The scanner sets the flag; ``derive_classification`` reads it
    and routes the job to ``'reject'``.
  - String-pattern matching only — no model-based classifier.

False-positive override: clearing a flag is done by NULLing
``jobs.legitimacy_note`` via the ``/admin/review`` UI or a manual
``UPDATE jobs SET legitimacy_note = NULL WHERE dedup_key = '...'``.
Note that the scanner will re-flag on the next rescore if the suspicious
pattern is still present in ``jd_full``.

Reference: .planning/specs/2026-05-29-ingestion-contract-enforcement.md
           §13 Commit 49.07; D-12; Open Question #7; R-09.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Pattern set (conservative — bias toward false negatives)
# ---------------------------------------------------------------------------

# Exact lowercase substring patterns.  Each entry is a 2-tuple:
# (pattern_string, short_tag) so the returned note is human-readable.
_SCAM_SUBSTRINGS: tuple[tuple[str, str], ...] = (
    ("unlimited income potential", "MLM language"),
    ("be your own boss", "MLM language"),
    ("work from home opportunity", "MLM language"),
    ("join our team of leaders", "MLM language"),
    ("financial freedom", "MLM language"),
    ("residual income", "MLM language"),
    ("recruit your downline", "pyramid/MLM"),
    ("earnings depend on", "pyramid/MLM"),
    ("crypto trading opportunity", "crypto/get-rich-quick"),
)

# Compiled regex patterns; matched against the lowercased full text.
# Each entry is a 2-tuple: (compiled_pattern, short_tag).
_SCAM_REGEXES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"earn \$\d{3,}/day"), "get-rich-quick"),
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def scan_legitimacy(jd_full: str) -> str | None:
    """Scan a job description for scam / MLM red-flag patterns.

    Returns the FIRST matched pattern as a brief note string
    (e.g. ``"suspicious_pattern: MLM language"``), or ``None`` when no
    suspicious pattern is found.  Only the first match is returned — this
    is a flag, not a comprehensive analysis.

    Args:
        jd_full: Raw job-description text (may be empty or None-coerced
            to empty string by the caller).

    Returns:
        A non-empty note string when a pattern is matched; ``None``
        otherwise.
    """
    if not jd_full:
        return None

    text = jd_full.lower()

    for pattern, tag in _SCAM_SUBSTRINGS:
        if pattern in text:
            return f"suspicious_pattern: {tag}"

    for regex, tag in _SCAM_REGEXES:
        if regex.search(text):
            return f"suspicious_pattern: {tag}"

    return None
