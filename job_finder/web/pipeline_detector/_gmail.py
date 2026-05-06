"""Gmail integration for pipeline detection.

Two functions live here:
  - ``_get_gmail_service`` -- authenticate via the cached OAuth token and
    return a Gmail API service handle. Returns ``None`` on any auth
    failure so the detection job never crashes the APScheduler thread.
  - ``_fetch_pipeline_emails`` -- run the three Gmail query patterns
    (rejection / interview / confirmation), fetch full message content,
    and return a flat list of dicts ready for ``_process_email``.

Body extraction prefers ``text/plain`` over ``text/html`` (HTML is
HTML-stripped when no plaintext part exists). Date parsing falls back
through ``parsedate_to_datetime`` -> ``internalDate`` -> ``datetime.now()``.
"""

import logging
from datetime import UTC, datetime

from job_finder.web.pipeline_detector._constants import (
    CONFIRMATION_QUERY,
    INTERVIEW_QUERY,
    REJECTION_QUERY,
)

logger = logging.getLogger(__name__)


def _get_gmail_service(config: dict):
    """Authenticate and return the Gmail API service.

    Returns None on any auth failure -- detection job must not crash
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
