"""IMAP source - fetches and parses job alert emails via IMAP + app password.

Replaces Gmail API source as the default ingest path for strangers.
Uses IMAP LOGIN with app password (no OAuth required).

Safety contract
---------------
- The folder is opened **readonly** so no IMAP command can implicitly set \\Seen.
- Messages are searched with a scoped FROM OR-chain so only job-alert senders
  are ever touched; personal mail is never fetched or examined.
- Bodies are fetched with ``BODY.PEEK[]`` which is explicitly non-mutating even
  on writable folders.
- ``\\Seen`` is added only to messages from known job-alert senders after they
  have been processed.  Messages from unknown senders are never fetched or
  flagged.
"""

import email
import email.policy
import logging
from datetime import UTC, datetime
from typing import Any

from imapclient import IMAPClient

import job_finder.web.autoheal.override_loader as _override_loader
from job_finder.models import Job
from job_finder.parsers import extract_with_fallback
from job_finder.sources.gmail_source import (
    SENDER_LABEL,
    SENDER_PARSERS,
    _archive_parse_failure,
    _should_archive_failure,
)
from job_finder.web.autoheal.recipe_extractor import RecipeExtractor

logger = logging.getLogger(__name__)


def _build_from_search_criteria(senders: list[str]) -> list[Any]:
    """Build an IMAP search criteria list that matches UNSEEN messages from any
    of the given sender addresses.

    IMAP OR is binary, so N senders require N-1 nested ORs:

    * 1 sender  → ["UNSEEN", "FROM", addr]
    * 2 senders → ["UNSEEN", "OR", ["FROM", a], ["FROM", b]]
    * 3 senders → ["UNSEEN", "OR", ["FROM", a], ["OR", ["FROM", b], ["FROM", c]]]
    * N senders → right-fold over the list

    The imapclient library serialises nested lists to parenthesised groups, which
    is the correct IMAP syntax.

    Args:
        senders: Non-empty list of sender email address substrings (keys from
            SENDER_PARSERS).  Must have at least one entry.

    Returns:
        Criteria list suitable for passing to ``IMAPClient.search()``.

    Raises:
        ValueError: If senders is empty.
    """
    if not senders:
        raise ValueError("senders must be non-empty")

    from_clauses: list[Any] = [["FROM", addr] for addr in senders]

    if len(from_clauses) == 1:
        from_tree: Any = from_clauses[0]
    else:
        # Right-fold: OR(a, OR(b, OR(c, d)))
        from_tree = from_clauses[-1]
        for clause in reversed(from_clauses[:-1]):
            from_tree = ["OR", clause, from_tree]

    return ["UNSEEN", from_tree]


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
        self.extraction_records: list[dict] = []

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

        # Build scoped sender list once — keyed identically to SENDER_PARSERS.
        known_senders = list(SENDER_PARSERS.keys())
        search_criteria = _build_from_search_criteria(known_senders)

        try:
            with IMAPClient(self.host, port=self.port, ssl=True) as client:
                client.login(self.email_address, self.app_password)
                # readonly=True: the folder is never writable via this connection.
                # BODY.PEEK[] fetches are non-mutating regardless, but this provides
                # defence-in-depth so even a mis-issued RFC822 fetch can't set \Seen.
                client.select_folder(self.folder, readonly=True)

                # Search only for unseen messages from known job-alert senders.
                # Personal mail is never touched.
                uids = client.search(search_criteria)

                if not uids:
                    logger.info("No unseen job-alert messages found")
                    return [], []

                # BODY.PEEK[] is explicitly non-mutating — it never sets \Seen.
                messages = client.fetch(uids, ["BODY.PEEK[]"])

                # UIDs of known-sender messages to mark \Seen after processing.
                uids_to_flag: list[int] = []

                for uid, msg_data in messages.items():
                    raw_bytes = msg_data[b"BODY[]"]
                    message = email.message_from_bytes(raw_bytes, policy=email.policy.default)

                    # Extract email components
                    sender = self._extract_sender(message)
                    body = self._extract_body(message)
                    email_date = self._extract_date(message)

                    if not sender or not body:
                        logger.warning("Skipping message with missing sender or body: UID %s", uid)
                        # Still a known-sender message (matched our FROM scope) —
                        # flag it so we don't re-fetch it on the next run.
                        uids_to_flag.append(uid)
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
                        # The FROM scope should have prevented this.  If the IMAP
                        # server returned a false-positive match, do NOT flag it —
                        # this is not a confirmed known-sender message.
                        logger.info(
                            "No parser found for sender: %s (skipping, not flagging)", sender
                        )
                        continue

                    # Parse the email body
                    try:
                        # Phase C: email override pre-check (dormant when no override files present).
                        # With no override, falls through to extract_with_fallback unchanged.
                        _label = SENDER_LABEL.get(sender_lower, sender_lower)
                        _recipe = _override_loader.html_recipe(_label)
                        if _recipe is not None:
                            _recipe_jobs = RecipeExtractor(_recipe, job_source="email_recipe")(
                                body
                            )
                        else:
                            _recipe_jobs = []
                        if _recipe_jobs:
                            jobs = _recipe_jobs
                        else:
                            jobs = extract_with_fallback(parser_fn, body, email_date)
                        all_jobs.extend(jobs)
                        self.extraction_records.append(
                            {
                                "label": SENDER_LABEL.get(sender, sender),
                                "raw_text": body,
                                "job_count": len(jobs),
                            }
                        )

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

                    # Known-sender message — flag after processing.
                    uids_to_flag.append(uid)
                    processed_uids.append(str(uid))

                # Bulk-flag all processed known-sender messages in one round-trip.
                # Re-select writable only if there is anything to flag.
                if uids_to_flag:
                    client.select_folder(self.folder, readonly=False)
                    client.add_flags(uids_to_flag, [b"\\Seen"])

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
                        body = payload.decode(
                            part.get_content_charset() or "utf-8", errors="ignore"
                        )
                except Exception:
                    continue
            elif content_type == "text/html" and body is None:
                # Fall back to HTML if plain text not found
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        body = payload.decode(
                            part.get_content_charset() or "utf-8", errors="ignore"
                        )
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
            # Normalize to naive UTC regardless of incoming tzinfo
            return dt.astimezone(UTC).replace(tzinfo=None)
        except Exception:
            return None
