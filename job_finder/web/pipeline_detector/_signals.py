"""Classification + multi-signal scoring for the pipeline detector.

Seven helper functions split into two layers:

  Layer 1 -- single-signal predicates:
    ``_classify_email``    : keyword-based subject/body -> detection_type
    ``_company_in_email``  : word-boundary company-name match
    ``_title_in_email``    : significant-word title overlap
    ``_timing_ok``         : email date within 60 days of first_seen
    ``_sender_is_ats``     : From header domain is a known ATS
    ``_extract_snippet``   : first sentence containing a signal keyword

  Layer 2 -- aggregator:
    ``score_match``        : composes the four signal predicates and
                             returns ``(score, matched_signals)``. **Signal
                             ordering in the returned matched list is part
                             of the read contract** -- it is JSON-encoded
                             into ``pipeline_detections.matched_signals``
                             and consumed by the dashboard. Order is
                             ``[company, title, timing, ats_domain]``.

All functions are pure -- no module-level state, no DB access. The DB
helpers live in ``_db.py``; the email-processing orchestrator that
composes these signals lives in ``__init__.py`` (until S7b's
``_processing.py`` extraction).
"""

import logging
import re
from datetime import datetime

from job_finder.web.pipeline_detector._constants import (
    ATS_DOMAINS,
    CONFIRMATION_KEYWORDS,
    INTERVIEW_KEYWORDS,
    REJECTION_KEYWORDS,
    SIGNAL_KEYWORDS,
    TITLE_STOP_WORDS,
)

logger = logging.getLogger(__name__)


def _classify_email(subject: str, body: str) -> str | None:
    """Classify email as 'rejection', 'interview', 'confirmation', or None.

    Checks subject and body against keyword sets for each detection type.
    Returns the first matching type, or None for unrelated emails.

    Args:
        subject: Email subject line.
        body: Email body text.

    Returns:
        One of 'rejection', 'interview', 'confirmation', or None.
    """
    text = f"{subject} {body}".lower()

    for keyword in REJECTION_KEYWORDS:
        if keyword in text:
            return "rejection"

    for keyword in INTERVIEW_KEYWORDS:
        if keyword in text:
            return "interview"

    for keyword in CONFIRMATION_KEYWORDS:
        if keyword in text:
            return "confirmation"

    return None


def _company_in_email(company: str | None, body: str, subject: str) -> bool:
    """Check if a company name appears in email subject or body.

    Uses word-boundary regex to avoid false positives like 'Apple' in 'Pineapple'.
    Falls back to checking individual significant words (5+ chars).

    Args:
        company: Company name from the jobs DB. Returns False if None or empty.
        body: Email body text.
        subject: Email subject line.

    Returns:
        True if the company is found with word-boundary matching.
    """
    if not company:
        return False

    text = f"{subject} {body}".lower()
    company_lower = company.lower().strip()

    # Strategy 1: word-boundary exact match
    pattern = r"\b" + re.escape(company_lower) + r"\b"
    if re.search(pattern, text):
        return True

    # Strategy 2: ALL significant words (5+ chars) must match
    # e.g., "BetterHelp" (1 word) -> "betterhelp" must match
    # e.g., "Alameda County" (2 words) -> both "alameda" AND "county" must match
    words = company_lower.split()
    sig_words = [w for w in words if len(w) >= 5]
    return bool(
        sig_words and all(re.search(r"\b" + re.escape(word) + r"\b", text) for word in sig_words)
    )


def _title_in_email(title: str, subject: str, body: str) -> bool:
    """Check if significant words from job title appear in subject or body.

    Significant words are those with length >= 4 and not in TITLE_STOP_WORDS.
    Requires 2+ significant words to match when the title has multiple
    significant words, or all to match when it has only 1.

    Args:
        title: Job title from the jobs DB.
        subject: Email subject line.
        body: Email body text.

    Returns:
        True if enough significant title words are found.
    """
    text = f"{subject} {body}".lower()
    title_lower = title.lower()

    sig_words = []
    for word in title_lower.split():
        word = word.strip(".,;:()")
        if len(word) >= 4 and word not in TITLE_STOP_WORDS:
            sig_words.append(word)

    if not sig_words:
        return False

    matched = sum(1 for w in sig_words if w in text)

    # Require 2+ matches when title has multiple significant words,
    # or all matches when it has only 1
    if len(sig_words) == 1:
        return matched == 1
    return matched >= 2


def _timing_ok(email_date: str, job: dict) -> bool:
    """Check if email date is within timing windows of job activity.

    Returns True if:
    - Email is within 60 days of job's first_seen, OR
    - Email is within 30 days of any 'applied' pipeline event (if available).

    Args:
        email_date: ISO timestamp string of the email date.
        job: Job dict including first_seen and optionally pipeline_events info.

    Returns:
        True if the timing aligns, False otherwise.
    """
    try:
        email_dt = datetime.fromisoformat(email_date.replace("Z", "+00:00"))
        # Remove timezone info for naive comparison
        if email_dt.tzinfo is not None:
            email_dt = email_dt.replace(tzinfo=None)
    except (ValueError, TypeError):
        return False

    # Check against first_seen (60-day window)
    first_seen_str = job.get("first_seen", "")
    if first_seen_str:
        try:
            first_seen_dt = datetime.fromisoformat(first_seen_str.replace("Z", "+00:00"))
            if first_seen_dt.tzinfo is not None:
                first_seen_dt = first_seen_dt.replace(tzinfo=None)
            if abs((email_dt - first_seen_dt).days) <= 60:
                return True
        except (ValueError, TypeError):
            pass

    return False


def _sender_is_ats(from_address: str) -> bool:
    """Return True if the sender domain is a known ATS platform.

    Handles both 'email@domain.com' and 'Name <email@domain.com>' formats.
    Checks for exact domain match and subdomain match.

    Args:
        from_address: Full From header value.

    Returns:
        True if the sender domain matches any known ATS.
    """
    if not from_address:
        return False

    match = re.search(r"@([\w.-]+)", from_address)
    if not match:
        return False

    sender_domain = match.group(1).lower()
    return any(sender_domain == d or sender_domain.endswith("." + d) for d in ATS_DOMAINS)


def _extract_snippet(body: str, detection_type: str) -> str:
    """Extract the most relevant sentence from email body.

    Finds the first sentence containing a signal keyword for the detection type.
    Returns up to 200 characters. Falls back to first non-empty sentence.

    Args:
        body: Email body text.
        detection_type: One of 'rejection', 'interview', 'confirmation'.

    Returns:
        Up to 200-character snippet string.
    """
    if not body:
        return ""

    keywords = SIGNAL_KEYWORDS.get(detection_type, [])
    sentences = re.split(r"[.!?\n]+", body)

    for kw in keywords:
        for sentence in sentences:
            if kw.lower() in sentence.lower():
                snippet = sentence.strip()
                return snippet[:200] if len(snippet) > 200 else snippet

    # Fallback: first non-empty sentence
    for sentence in sentences:
        stripped = sentence.strip()
        if stripped:
            return stripped[:200]

    return ""


def score_match(email: dict, job: dict) -> tuple[int, list[str]]:
    """Compute 0-4 confidence score by checking four independent signals.

    Signals (in this exact order -- the returned matched list reflects
    the order, and the order is part of the read contract for
    pipeline_detections.matched_signals):

    1. company: company name appears in email body/subject
    2. title: job title keywords appear in email body/subject
    3. timing: email received within timing window of job activity
    4. ats_domain: From header domain is a known ATS (only if
       detection_type is set -- Pitfall 3)

    Args:
        email: Email dict with keys: subject, body, from_address, date,
            detection_type.
        job: Job dict with keys: company, title, first_seen,
            pipeline_status.

    Returns:
        (score, matched_signals_list) where score is 0-4 and the list
        is in the [company, title, timing, ats_domain] order.
    """
    matched = []

    # Signal 1: company name
    if _company_in_email(
        job.get("company", ""),
        email.get("body", ""),
        email.get("subject", ""),
    ):
        matched.append("company")

    # Signal 2: title match
    if _title_in_email(
        job.get("title", ""),
        email.get("subject", ""),
        email.get("body", ""),
    ):
        matched.append("title")

    # Signal 3: timing
    if _timing_ok(email.get("date", ""), job):
        matched.append("timing")

    # Signal 4: ATS domain -- only counts if detection_type is classified (Pitfall 3)
    if email.get("detection_type") is not None and _sender_is_ats(email.get("from_address", "")):
        matched.append("ats_domain")

    return len(matched), matched
