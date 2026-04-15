"""Gmail source - fetches and parses job alert emails via the Gmail API.

Requires OAuth credentials (credentials.json) and a saved token (token.json).
Run `python -m job_finder.gmail_auth` to set up authentication.
"""

import base64
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from job_finder.models import Job
from job_finder.parsers.linkedin_parser import parse_linkedin_alert
from job_finder.parsers.glassdoor_parser import parse_glassdoor_alert
from job_finder.parsers.indeed_parser import parse_indeed_alert, parse_indeed_match_alert
from job_finder.parsers.ziprecruiter_parser import parse_ziprecruiter_alert
from job_finder.parsers.greenhouse_parser import parse_greenhouse_alert
from job_finder.parsers.trueup_parser import parse_trueup_alert
from job_finder.parsers.monster_parser import parse_monster_alert

logger = logging.getLogger(__name__)

# Map sender addresses to parser functions
SENDER_PARSERS = {
    "jobalerts-noreply@linkedin.com": parse_linkedin_alert,
    "jobs-noreply@linkedin.com": parse_linkedin_alert,
    "noreply@glassdoor.com": parse_glassdoor_alert,
    "alert@indeed.com": parse_indeed_alert,
    "donotreply@match.indeed.com": parse_indeed_match_alert,
    "no-reply@ziprecruiter.com": parse_ziprecruiter_alert,
    "no-reply@us.greenhouse-jobs.com": parse_greenhouse_alert,
    "hello@trueup.io": parse_trueup_alert,
    "monster@notifications.monster.com": parse_monster_alert,
}

TOKEN_PATH = "token.json"
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Intentional hardcoded default — parse failures are a local debugging artifact,
# not user data, so this path does not belong in config.yaml.  Override by
# passing a different directory to _archive_parse_failure if needed.
PARSE_FAILURES_DIR = "data/parse_failures"

# Meta-email indicator phrases (checked against lowercased first 200 chars of body)
_ARCHIVE_META_INDICATORS = [
    "job alert digest",
    "weekly digest",
    "unsubscribe from",
    "confirm your email",
    "email preferences",
]


def _should_archive_failure(body: str, jobs: list, sender: str) -> bool:
    """Return True if this parser result should trigger failure archival.

    Archival is triggered when:
    - Parser found zero jobs (jobs is empty)
    - Body is non-meta (not a digest/confirmation email)
    - Body is long enough to be a real email (>= 500 chars after stripping)

    Args:
        body: Raw email body string.
        jobs: List of Job objects returned by the parser (empty = parse failure).
        sender: Sender email address.

    Returns:
        True if the failure should be archived.
    """
    if jobs:
        return False
    if not body or len(body.strip()) < 500:
        return False
    preamble = body[:200].lower()
    if any(indicator in preamble for indicator in _ARCHIVE_META_INDICATORS):
        return False
    return True


def _archive_parse_failure(sender: str, body: str, *, failures_dir: str = PARSE_FAILURES_DIR) -> None:
    """Archive HTML body from a failed parse to PARSE_FAILURES_DIR.

    Filename: {sender_domain}_{ISO_timestamp}.html
    Creates directory if needed. Logs warning on write failure — never raises.

    Args:
        sender: Sender email address (used for filename prefix).
        body: Raw email body HTML to archive.
        failures_dir: Directory to write failure files into (default: PARSE_FAILURES_DIR).
    """
    try:
        os.makedirs(failures_dir, exist_ok=True)
        domain = sender.split("@")[-1].replace(".", "_") if "@" in sender else sender
        ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        path = f"{failures_dir}/{domain}_{ts}.html"
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        logger.info("Parse failure archived: %s", path)
    except Exception as e:
        logger.warning("Failed to archive parse failure: %s", e)


class GmailSource:
    """Fetch and parse job alert emails from Gmail."""

    def __init__(self, token_path: str = TOKEN_PATH):
        self.service = self._authenticate(token_path)
        self.parse_failures: list[dict] = []

    def _authenticate(self, token_path: str):
        """Load saved OAuth credentials and build the Gmail service."""
        try:
            from job_finder.gmail_auth import get_credentials, AuthenticationError
            creds = get_credentials(token_path)
            return build("gmail", "v1", credentials=creds)
        except AuthenticationError as exc:
            raise RuntimeError(str(exc)) from exc

    def fetch_jobs(
        self,
        lookback_days: int = 7,
        processed_message_ids: set[str] | None = None,
    ) -> tuple[list[Job], list[str]]:
        """Fetch all job alert emails from the last N days and parse them.

        Args:
            lookback_days: How many days back to search.
            processed_message_ids: Set of Gmail message IDs already processed
                in a previous sync. Matching messages are skipped to avoid
                re-fetching and re-parsing. Pass None (default) to process all.

        Returns:
            Tuple of (jobs, processed_ids) where:
            - jobs: List of parsed Job objects from all sources.
            - processed_ids: List of message IDs that were fetched and attempted
                (both successful parses and parse failures). Does not include
                IDs skipped via processed_message_ids or API fetch failures.
        """
        all_jobs: list[Job] = []
        newly_processed: list[str] = []
        after_date = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y/%m/%d")

        for sender, parser_fn in SENDER_PARSERS.items():
            query = f"from:{sender} after:{after_date}"
            messages = self._search_messages(query)

            # Skip messages already processed in a previous sync
            if processed_message_ids:
                before = len(messages)
                messages = [m for m in messages if m["id"] not in processed_message_ids]
                skipped = before - len(messages)
                if skipped:
                    logger.info(
                        "Gmail: skipping %d already-processed messages from %s",
                        skipped,
                        sender,
                    )

            for msg_meta in messages:
                msg_id = msg_meta["id"]
                msg = self._get_message(msg_id)
                if not msg:
                    # API error -- don't mark processed; allow retry on next sync
                    continue

                body = self._extract_body(msg)
                email_date = self._extract_date(msg)

                if body:
                    # Only mark processed when body extraction succeeded.
                    # body=None means the API response was malformed; allow
                    # retry on the next sync rather than permanently silencing.
                    newly_processed.append(msg_id)
                    jobs = parser_fn(body, email_date)
                    all_jobs.extend(jobs)

                    if _should_archive_failure(body, jobs, sender):
                        _archive_parse_failure(sender, body)
                        self.parse_failures.append({"sender": sender, "message_id": msg_id})

        return all_jobs, newly_processed

    def _search_messages(self, query: str, max_messages: int = 500) -> list[dict]:
        """Search Gmail and return matching message metadata.

        Args:
            query: Gmail search query string.
            max_messages: Upper bound on messages to return. Prevents
                runaway pagination on broad queries.
        """
        messages = []
        page_token = None

        while True:
            try:
                result = (
                    self.service.users()
                    .messages()
                    .list(userId="me", q=query, pageToken=page_token, maxResults=100)
                    .execute()
                )
            except Exception as e:
                logger.warning(
                    "Gmail API error during pagination (collected %d so far): %s",
                    len(messages),
                    e,
                )
                break
            messages.extend(result.get("messages", []))
            if len(messages) >= max_messages:
                logger.info(
                    "Gmail pagination cap reached (%d messages), stopping",
                    max_messages,
                )
                messages = messages[:max_messages]
                break
            page_token = result.get("nextPageToken")
            if not page_token:
                break

        return messages

    def _get_message(self, message_id: str) -> Optional[dict]:
        """Fetch a single message by ID."""
        try:
            return (
                self.service.users()
                .messages()
                .get(userId="me", id=message_id, format="full")
                .execute()
            )
        except Exception as e:
            logger.warning("failed to fetch message %s: %s", message_id, e)
            return None

    def _extract_body(self, message: dict) -> Optional[str]:
        """Extract the email body (prefers text/plain, falls back to text/html)."""
        payload = message.get("payload", {})

        # Try top-level body first
        body_data = payload.get("body", {}).get("data")
        if body_data:
            return base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")

        # Check parts (multipart emails)
        parts = payload.get("parts", [])
        text_body = None
        html_body = None

        for part in parts:
            mime = part.get("mimeType", "")
            data = part.get("body", {}).get("data")
            if not data:
                # Check nested parts (multipart/alternative inside multipart/mixed)
                for subpart in part.get("parts", []):
                    sub_data = subpart.get("body", {}).get("data")
                    sub_mime = subpart.get("mimeType", "")
                    if sub_data and sub_mime == "text/plain":
                        text_body = base64.urlsafe_b64decode(sub_data).decode(
                            "utf-8", errors="replace"
                        )
                    elif sub_data and sub_mime == "text/html":
                        html_body = base64.urlsafe_b64decode(sub_data).decode(
                            "utf-8", errors="replace"
                        )
                continue

            if mime == "text/plain":
                text_body = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            elif mime == "text/html":
                html_body = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

        # Prefer plain text for LinkedIn (cleaner parsing), HTML for Glassdoor
        return text_body or html_body

    def _extract_date(self, message: dict) -> Optional[datetime]:
        """Extract the email date from headers."""
        headers = message.get("payload", {}).get("headers", [])
        for header in headers:
            if header["name"].lower() == "date":
                try:
                    from email.utils import parsedate_to_datetime
                    return parsedate_to_datetime(header["value"])
                except Exception:
                    logger.debug("email date parse failed", exc_info=True)

        # Fallback to internalDate
        internal_date = message.get("internalDate")
        if internal_date:
            return datetime.fromtimestamp(int(internal_date) / 1000, tz=timezone.utc)

        return None
