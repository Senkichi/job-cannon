"""IMAP source - fetches and parses job alert emails via IMAP + app password.

Replaces Gmail API source as the default ingest path for strangers.
Uses IMAP LOGIN with app password (no OAuth required).
"""

import email
import email.policy
import logging
from datetime import UTC, datetime

from imapclient import IMAPClient

from job_finder.models import Job
from job_finder.sources.gmail_source import (
    SENDER_PARSERS,
    _archive_parse_failure,
    _should_archive_failure,
)

logger = logging.getLogger(__name__)


class ImapSource:
    """Fetch job alert emails from Gmail via IMAP using app password."""

    def __init__(
        self,
        host: str = "imap.gmail.com",
        port: int = 993,
        email_address: str = "",
        app_password: str = "",
        folder: str = "INBOX",
    ):
        """Initialize IMAP source with connection parameters.

        Args:
            host: IMAP server hostname (default: imap.gmail.com).
            port: IMAP server port (default: 993 for SSL).
            email_address: Gmail address for LOGIN.
            app_password: App-specific password for LOGIN.
            folder: IMAP folder to search (default: INBOX).
        """
        self.host = host
        self.port = port
        self.email_address = email_address
        self.app_password = app_password
        self.folder = folder
        self.parse_failures: list[dict] = []

    def fetch_jobs(
        self, lookback_days: int = 7, processed_message_ids: set[str] | None = None
    ) -> tuple[list[Job], list[str]]:
        r"""Fetch and parse job alert emails from IMAP.

        Args:
            lookback_days: Ignored - kept for interface compatibility.
                IMAP uses UNSEEN flag for dedup, not time-based filtering.
            processed_message_ids: Ignored - kept for interface compatibility.
                IMAP uses \Seen flag for dedup, not external ID tracking.

        Returns:
            Tuple of (list of Job objects, list of processed UID strings).
        """
        all_jobs: list[Job] = []
        processed_uids: list[str] = []

        try:
            with IMAPClient(self.host, port=self.port, ssl=True) as client:
                client.login(self.email_address, self.app_password)
                client.select_folder(self.folder, readonly=False)

                # Search for unseen messages only
                uids = client.search(["UNSEEN"])

                if not uids:
                    logger.info("No unseen messages found")
                    return [], []

                # Fetch RFC822 payloads for all unseen messages
                messages = client.fetch(uids, ["RFC822"])

                for uid, msg_data in messages.items():
                    rfc822_bytes = msg_data[b"RFC822"]
                    message = email.message_from_bytes(
                        rfc822_bytes, policy=email.policy.default
                    )

                    # Extract email components
                    sender = self._extract_sender(message)
                    body = self._extract_body(message)
                    email_date = self._extract_date(message)

                    if not sender or not body:
                        logger.warning(
                            "Skipping message with missing sender or body: UID %s", uid
                        )
                        # Mark seen to avoid reprocessing
                        client.add_flags([uid], [b"\\Seen"])
                        processed_uids.append(str(uid))
                        continue

                    # Find matching parser
                    sender_lower = sender.lower()
                    parser_fn = None
                    for sender_key, parser in SENDER_PARSERS.items():
                        if sender_key in sender_lower:
                            parser_fn = parser
                            break

                    if parser_fn is None:
                        logger.info("No parser found for sender: %s", sender)
                        client.add_flags([uid], [b"\\Seen"])
                        processed_uids.append(str(uid))
                        continue

                    # Parse the email body
                    try:
                        jobs = parser_fn(body, email_date)
                        all_jobs.extend(jobs)

                        # Archive parse failures if needed
                        if _should_archive_failure(body, jobs, sender):
                            _archive_parse_failure(sender, body)
                    except Exception as e:
                        logger.error(
                            "Parser error for sender %s (UID %s): %s",
                            sender,
                            uid,
                            e,
                            exc_info=True,
                        )
                        # Record failure for tracking
                        self.parse_failures.append(
                            {"sender": sender, "message_id": str(uid), "error": str(e)}
                        )

                    # Mark message as seen after processing
                    client.add_flags([uid], [b"\\Seen"])
                    processed_uids.append(str(uid))

        except Exception as e:
            logger.error("IMAP fetch error: %s", e, exc_info=True)
            # Re-raise to let caller handle connection failures
            raise

        return all_jobs, processed_uids

    def _extract_sender(self, message: email.message.Message) -> str:
        """Extract sender email address from message.

        Args:
            message: Email message object.

        Returns:
            Sender email address or empty string if not found.
        """
        from_header = message.get("From", "")
        # Extract email from "Name <email@domain.com>" format
        if "<" in from_header and ">" in from_header:
            return from_header.split("<")[1].split(">")[0].strip()
        return from_header.strip()

    def _extract_body(self, message: email.message.Message) -> str | None:
        """Extract email body text, preferring plain text over HTML.

        Args:
            message: Email message object.

        Returns:
            Body text as string, or None if extraction fails.
        """
        body = None

        # Walk through message parts to find text/plain or text/html
        for part in message.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))

            # Skip attachments
            if "attachment" in content_disposition:
                continue

            if content_type == "text/plain" and body is None:
                # Prefer plain text if available
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        body = payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
                except Exception:
                    continue
            elif content_type == "text/html" and body is None:
                # Fall back to HTML if plain text not found
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        body = payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
                except Exception:
                    continue

        return body

    def _extract_date(self, message: email.message.Message) -> datetime | None:
        """Extract date from message headers.

        Args:
            message: Email message object.

        Returns:
            Datetime object or None if parsing fails.
        """
        date_header = message.get("Date")
        if not date_header:
            return None

        try:
            from email.utils import parsedate_to_datetime

            dt = parsedate_to_datetime(date_header)
            # Ensure timezone-aware
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt
        except Exception:
            return None
