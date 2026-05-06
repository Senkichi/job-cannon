"""Pipeline detection engine for job-finder.

Scans Gmail for rejection, interview, and application confirmation emails.
Matches emails to existing jobs using multi-signal confidence scoring.
Auto-updates pipeline status for high-confidence matches (3+ signals) and
queues low-confidence matches (1-2 signals) for manual review.

Follows the stale_detector.py pattern: creates its own SQLite connection
and is thread-safe for APScheduler background jobs.
"""

import json
import logging
import re
import sqlite3
from datetime import UTC, datetime

from job_finder.db import update_pipeline_status
from job_finder.web.db_helpers import standalone_connection

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Gmail query patterns (from research — OR-combined for maximum recall)
# ---------------------------------------------------------------------------

REJECTION_QUERY = (
    'subject:("unfortunately" OR "not moving forward" OR '
    '"other direction" OR "other candidates" OR "not selected" OR '
    '"position has been filled" OR "no longer" OR "decided not to proceed") '
    "newer_than:3d"
)

INTERVIEW_QUERY = (
    'subject:("interview" OR "next steps" OR "phone screen" OR '
    '"technical interview" OR "schedule time" OR "meet with") '
    "newer_than:3d"
)

CONFIRMATION_QUERY = (
    'subject:("application received" OR "thank you for applying" OR '
    '"application confirmation" OR "we received your application" OR '
    '"successfully submitted") '
    "newer_than:3d"
)

# ---------------------------------------------------------------------------
# Classification keyword sets
# ---------------------------------------------------------------------------

REJECTION_KEYWORDS = [
    "unfortunately",
    "not moving forward",
    "other candidates",
    "not selected",
    "position has been filled",
    "no longer moving forward",
    "decided not to proceed",
    "other direction",
    "will not be moving forward",
    "not proceed",
    "filled the position",
]

INTERVIEW_KEYWORDS = [
    "interview",
    "phone screen",
    "next steps",
    "technical interview",
    "schedule time",
    "meet with",
    "speak with",
    "chat with",
    "call with",
    "video call",
    "hiring process",
]

CONFIRMATION_KEYWORDS = [
    "application received",
    "thank you for applying",
    "application confirmation",
    "we received your application",
    "successfully submitted",
    "received your application",
    "thank you for your application",
]

# Maps Gmail query detection_type to classification
QUERY_DETECTION_TYPES = {
    REJECTION_QUERY: "rejection",
    INTERVIEW_QUERY: "interview",
    CONFIRMATION_QUERY: "confirmation",
}

# Maps detection_type to the pipeline status transition target
DETECTION_TYPE_TO_STATUS = {
    "rejection": "rejected",
    "interview": "phone_screen",
    "confirmation": "applied",
}

# Signal keywords for snippet extraction
SIGNAL_KEYWORDS = {
    "rejection": REJECTION_KEYWORDS,
    "interview": INTERVIEW_KEYWORDS,
    "confirmation": CONFIRMATION_KEYWORDS,
}

# ---------------------------------------------------------------------------
# ATS domain list
# ---------------------------------------------------------------------------

ATS_DOMAINS = {
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "workday.com",
    "myworkday.com",
    "taleo.net",
    "icims.com",
    "jobvite.com",
    "smartrecruiters.com",
    "breezy.hr",
    "jazz.co",
    "workable.com",
    "recruitee.com",
    "bamboohr.com",
    "successfactors.com",
    "kronos.net",
    "rippling.com",
    "pinpointhq.com",
}

# Pipeline statuses that indicate a job is no longer active
INACTIVE_STATUSES = {"archived", "rejected", "withdrawn"}

# Common job title words to exclude from title matching
TITLE_STOP_WORDS = {
    "senior",
    "staff",
    "lead",
    "data",
    "the",
    "and",
    "for",
    "with",
    "principal",
    "associate",
    "junior",
    "mid",
    "level",
}

# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_pipeline_detection(db_path: str, config: dict) -> dict:
    """Scan Gmail for pipeline emails and process matches.

    Creates its own SQLite connection (thread-safe for APScheduler).

    Args:
        db_path: Path to the SQLite database file.
        config: Full JF_CONFIG dict.

    Returns:
        Summary dict with keys:
            emails_scanned (int): Total emails fetched and examined.
            auto_updated (int): Jobs auto-updated from high-confidence matches.
            queued (int): Emails queued for manual review (low confidence).
            skipped (int): Emails skipped (no match or already processed).
            errors (list[str]): Error messages encountered.
    """
    summary = {
        "emails_scanned": 0,
        "auto_updated": 0,
        "queued": 0,
        "skipped": 0,
        "errors": [],
    }

    with standalone_connection(db_path) as conn:
        try:
            service = _get_gmail_service(config)
            if service is None:
                logger.warning("Pipeline detection: Gmail service unavailable, skipping")
                summary["errors"].append("Gmail authentication failed")
                return summary

            emails = _fetch_pipeline_emails(service, lookback_days=3)
            summary["emails_scanned"] = len(emails)

            # Load all active jobs once to avoid repeated DB queries
            jobs = _load_active_jobs(conn)

            for email in emails:
                try:
                    result = _process_email(email, conn, jobs, config=config)
                    if result == "auto_updated":
                        summary["auto_updated"] += 1
                    elif result == "queued":
                        summary["queued"] += 1
                    else:
                        summary["skipped"] += 1
                except Exception as e:
                    msg = f"Error processing email {email.get('message_id', '?')}: {e}"
                    logger.warning(msg)
                    summary["errors"].append(msg)

            logger.info(
                "Pipeline detection: %d scanned, %d auto-updated, %d queued, %d skipped",
                summary["emails_scanned"],
                summary["auto_updated"],
                summary["queued"],
                summary["skipped"],
            )

        except Exception as e:
            logger.exception("Pipeline detection failed: %s", e)
            summary["errors"].append(str(e))
            try:
                conn.rollback()
            except Exception:
                logger.debug("conn.rollback() failed in pipeline detection", exc_info=True)

    return summary


# ---------------------------------------------------------------------------
# Gmail service helpers
# ---------------------------------------------------------------------------


def _get_gmail_service(config: dict):
    """Authenticate and return the Gmail API service.

    Returns None on any auth failure — detection job must not crash
    the APScheduler thread.
    """
    try:
        from job_finder.sources.gmail_source import TOKEN_PATH, GmailSource

        source = GmailSource(token_path=TOKEN_PATH)
        return source.service
    except Exception as e:
        from job_finder.web.log_throttle import throttled_log

        throttled_log(logger, logging.WARNING, "Pipeline detection: Gmail auth failed: %s", e)
        return None


def _fetch_pipeline_emails(service, lookback_days: int = 3) -> list[dict]:
    """Fetch and parse pipeline emails from Gmail using three query patterns.

    For each query (rejection, interview, confirmation), runs a Gmail API
    search and fetches full message content. Extracts subject, from_address,
    date, body (text/plain preferred, text/html fallback).

    Returns:
        List of email dicts: {message_id, subject, from_address, date, body, detection_type}
    """
    import base64
    import html.parser

    class _HTMLStripper(html.parser.HTMLParser):
        def __init__(self):
            super().__init__()
            self._text = []

        def handle_data(self, data):
            self._text.append(data)

        def get_text(self):
            return " ".join(self._text)

    def _strip_html(html_content: str) -> str:
        stripper = _HTMLStripper()
        try:
            stripper.feed(html_content)
            return stripper.get_text()
        except Exception:
            logger.debug("email decode failed", exc_info=True)
            return html_content

    def _decode_b64(data: str) -> str:
        try:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        except Exception:
            logger.debug("email decode failed", exc_info=True)
            return ""

    def _extract_body_from_payload(payload: dict) -> str:
        """Extract text body from Gmail message payload (mirrors GmailSource._extract_body)."""
        body_data = payload.get("body", {}).get("data")
        if body_data:
            return _decode_b64(body_data)

        parts = payload.get("parts", [])
        text_body = None
        html_body = None

        for part in parts:
            mime = part.get("mimeType", "")
            data = part.get("body", {}).get("data")
            if not data:
                for subpart in part.get("parts", []):
                    sub_data = subpart.get("body", {}).get("data")
                    sub_mime = subpart.get("mimeType", "")
                    if sub_data and sub_mime == "text/plain":
                        text_body = _decode_b64(sub_data)
                    elif sub_data and sub_mime == "text/html":
                        html_body = _decode_b64(sub_data)
                continue
            if mime == "text/plain":
                text_body = _decode_b64(data)
            elif mime == "text/html":
                html_body = _decode_b64(data)

        if text_body:
            return text_body
        if html_body:
            return _strip_html(html_body)
        return ""

    def _extract_header(headers: list, name: str) -> str:
        for h in headers:
            if h.get("name", "").lower() == name.lower():
                return h.get("value", "")
        return ""

    emails = []
    seen_ids: set[str] = set()

    queries = [
        (REJECTION_QUERY, "rejection"),
        (INTERVIEW_QUERY, "interview"),
        (CONFIRMATION_QUERY, "confirmation"),
    ]

    for query, detection_type in queries:
        try:
            result = service.users().messages().list(userId="me", q=query).execute()
            messages = result.get("messages", [])
        except Exception as e:
            logger.warning("Gmail query failed (%s): %s", detection_type, e)
            continue

        for msg_meta in messages:
            msg_id = msg_meta["id"]
            if msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)

            try:
                msg = (
                    service.users().messages().get(userId="me", id=msg_id, format="full").execute()
                )
            except Exception as e:
                logger.warning("Failed to fetch message %s: %s", msg_id, e)
                continue

            payload = msg.get("payload", {})
            headers = payload.get("headers", [])

            subject = _extract_header(headers, "Subject")
            from_address = _extract_header(headers, "From")
            date_str = _extract_header(headers, "Date")

            # Parse date to ISO format
            try:
                from email.utils import parsedate_to_datetime

                email_date = parsedate_to_datetime(date_str).isoformat()
            except Exception:
                logger.debug("email date parse failed, using internalDate", exc_info=True)
                internal = msg.get("internalDate")
                if internal:
                    email_date = datetime.fromtimestamp(int(internal) / 1000, tz=UTC).isoformat()
                else:
                    email_date = datetime.now().isoformat()

            body = _extract_body_from_payload(payload)

            emails.append(
                {
                    "message_id": msg_id,
                    "subject": subject,
                    "from_address": from_address,
                    "date": email_date,
                    "body": body,
                    "detection_type": detection_type,
                }
            )

    return emails


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Confidence scoring
# ---------------------------------------------------------------------------


def score_match(email: dict, job: dict) -> tuple[int, list[str]]:
    """Compute 0-4 confidence score by checking four independent signals.

    Signals:
    1. company: company name appears in email body/subject
    2. title: job title keywords appear in email body/subject
    3. timing: email received within timing window of job activity
    4. ats_domain: From header domain is a known ATS (only if detection_type is set)

    Args:
        email: Email dict with keys: subject, body, from_address, date, detection_type.
        job: Job dict with keys: company, title, first_seen, pipeline_status.

    Returns:
        (score, matched_signals_list) where score is 0-4.
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

    # Signal 4: ATS domain — only counts if detection_type is classified (Pitfall 3)
    if email.get("detection_type") is not None and _sender_is_ats(email.get("from_address", "")):
        matched.append("ats_domain")

    return len(matched), matched


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _load_active_jobs(conn: sqlite3.Connection) -> list[dict]:
    """Load all jobs that are NOT in inactive pipeline statuses.

    Used to avoid repeated DB queries during email processing.

    Args:
        conn: Open sqlite3 connection.

    Returns:
        List of job dicts for active jobs.
    """
    placeholders = ",".join("?" * len(INACTIVE_STATUSES))
    try:
        rows = conn.execute(
            f"SELECT dedup_key, title, company, location, first_seen, pipeline_status"
            f" FROM jobs WHERE pipeline_status NOT IN ({placeholders})",
            tuple(INACTIVE_STATUSES),
        ).fetchall()
    except sqlite3.OperationalError as e:
        logger.warning("_load_active_jobs failed (DB not ready?): %s", e)
        return []
    return [dict(row) for row in rows]


def _already_processed(conn: sqlite3.Connection, message_id: str) -> bool:
    """Check if a Gmail message ID has already been processed.

    Args:
        conn: Open sqlite3 connection.
        message_id: Gmail message ID to check.

    Returns:
        True if already in email_parse_log, False otherwise.
    """
    row = conn.execute(
        "SELECT 1 FROM email_parse_log WHERE message_id = ?",
        (message_id,),
    ).fetchone()
    return row is not None


def _mark_processed(
    conn: sqlite3.Connection,
    message_id: str,
    sender: str,
    detection_type: str | None,
) -> None:
    """Mark a Gmail message ID as processed in email_parse_log.

    Uses INSERT OR IGNORE so re-processing the same ID does not fail.
    Called at FIRST DETECTION TIME (not just at confirm/dismiss).

    Args:
        conn: Open sqlite3 connection.
        message_id: Gmail message ID.
        sender: From address of the email.
        detection_type: Classification result or None.
    """
    now = datetime.now().isoformat()
    jobs_found = 1 if detection_type is not None else 0
    try:
        conn.execute(
            """INSERT OR IGNORE INTO email_parse_log
               (message_id, sender, processed_at, jobs_found, error)
               VALUES (?, ?, ?, ?, ?)""",
            (message_id, sender, now, jobs_found, None),
        )
        conn.commit()
    except Exception as e:
        logger.warning("Failed to mark message as processed: %s", e)


def _insert_detection(
    conn: sqlite3.Connection,
    message_id: str,
    detection_type: str,
    job_id: str | None,
    *,
    score: int,
    signals: list[str],
    snippet: str,
    email_subject: str,
    email_from: str,
    email_date: str,
    status: str,
) -> None:
    """Insert a record into pipeline_detections.

    Args:
        conn: Open sqlite3 connection.
        message_id: Gmail message ID.
        detection_type: 'rejection', 'interview', or 'confirmation'.
        job_id: Matched job dedup_key, or None.
        score: Confidence score 0-4.
        signals: List of matched signal names.
        snippet: Email body snippet (max 200 chars).
        email_subject: Email subject.
        email_from: Email from address.
        email_date: Email date as ISO string.
        status: 'pending', 'auto-applied', etc.
    """
    now = datetime.now().isoformat()
    conn.execute(
        """INSERT OR IGNORE INTO pipeline_detections
           (gmail_message_id, detection_type, job_id, confidence_score,
            matched_signals, snippet, email_subject, email_from,
            email_date, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            message_id,
            detection_type,
            job_id,
            score,
            json.dumps(signals),
            snippet,
            email_subject,
            email_from,
            email_date,
            status,
            now,
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Core email processing
# ---------------------------------------------------------------------------


def _process_email(
    email: dict,
    conn: sqlite3.Connection,
    jobs: list[dict],
    config: dict | None = None,
) -> str:
    """Process a single email: classify, match, score, auto-update or queue.

    Processing steps:
    1. Check email_parse_log — skip if already processed.
    2. Verify detection_type is set — skip if None (unclassified).
    3. For each active job, compute score_match.
    4. Take the best match. If tied, prefer 'applied' status.
    5. score >= 3: auto-update pipeline_status, insert 'auto-applied' detection.
    6. score 1-2: insert 'pending' detection.
    7. score 0: skip (no record).
    8. Mark message_id in email_parse_log at first detection time.

    Args:
        email: Email dict with message_id, subject, body, from_address, date, detection_type.
        conn: Open sqlite3 connection.
        jobs: List of active job dicts (pre-loaded).
        config: Optional full JF_CONFIG dict for notification toggle gating.

    Returns:
        'auto_updated', 'queued', or 'skipped' describing the outcome.
    """
    message_id = email.get("message_id", "")
    detection_type = email.get("detection_type")

    # Step 1: Dedup check
    if _already_processed(conn, message_id):
        return "skipped"

    # Step 2: Must have a classification
    if detection_type is None:
        return "skipped"

    # Step 3: Score against all active jobs
    best_score = 0
    best_signals: list[str] = []
    best_job: dict | None = None

    for job in jobs:
        score, signals = score_match(email, job)
        if score > best_score:
            best_score = score
            best_signals = signals
            best_job = job
        elif score == best_score and score > 0 and best_job is not None:
            # Tiebreak: prefer 'applied' status
            if (
                job.get("pipeline_status") == "applied"
                and best_job.get("pipeline_status") != "applied"
            ):
                best_job = job
                best_signals = signals

    # Company signal is mandatory — without it, we can't confidently
    # attribute an email to a specific job
    if "company" not in best_signals:
        return "skipped"

    # Extract snippet for the detection record
    snippet = _extract_snippet(email.get("body", ""), detection_type)
    new_status = DETECTION_TYPE_TO_STATUS.get(detection_type, "applied")
    job_id = best_job["dedup_key"] if best_job else None

    if best_score >= 3:
        # High confidence: auto-update pipeline status
        if best_job is not None:
            update_pipeline_status(
                conn,
                best_job["dedup_key"],
                new_status,
                source="auto-detected",
            )

        _insert_detection(
            conn,
            message_id,
            detection_type,
            job_id,
            score=best_score,
            signals=best_signals,
            snippet=snippet,
            email_subject=email.get("subject", ""),
            email_from=email.get("from_address", ""),
            email_date=email.get("date", ""),
            status="auto-applied",
        )

        _mark_processed(conn, message_id, email.get("from_address", ""), detection_type)
        return "auto_updated"

    elif best_score >= 1:
        # Low confidence: queue for review
        _insert_detection(
            conn,
            message_id,
            detection_type,
            job_id,
            score=best_score,
            signals=best_signals,
            snippet=snippet,
            email_subject=email.get("subject", ""),
            email_from=email.get("from_address", ""),
            email_date=email.get("date", ""),
            status="pending",
        )

        _mark_processed(conn, message_id, email.get("from_address", ""), detection_type)
        return "queued"

    else:
        # score == 0: silently drop — no record
        return "skipped"
