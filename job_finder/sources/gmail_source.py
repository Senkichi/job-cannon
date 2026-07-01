"""Gmail source - fetches and parses job alert emails via the Gmail API.

Requires OAuth credentials (credentials.json) and a saved token (token.json).
Run `python -m job_finder.gmail_auth` to set up authentication.
"""

import base64
import logging
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from googleapiclient.discovery import build

import job_finder.web.autoheal.override_loader as _override_loader
from job_finder.models import Job
from job_finder.parsers import extract_primary, extract_with_fallback

# Re-export shared email-sender symbols from email_senders.py
# These are kept through Stage 4, removed only in Stage 5
from job_finder.sources.email_senders import (
    _archive_parse_failure,
    _should_archive_failure,
    resolve_sender_label,
    resolve_sender_parsers,
)
from job_finder.web.autoheal.recipe_extractor import RecipeExtractor
from job_finder.web.user_data_dirs import token_path

logger = logging.getLogger(__name__)


def _resolve_token_path() -> str:
    """Return the canonical token.json path, migrating legacy CWD file if needed.

    If the user-data token doesn't exist but a CWD-relative token.json does,
    move it to the user-data location so the app continues working after an
    upgrade from a CWD-based setup.

    Returns:
        String path to the canonical token.json location.
    """
    canonical = token_path()
    if not canonical.exists():
        cwd_token = Path("token.json")
        if cwd_token.exists():
            canonical.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(cwd_token), str(canonical))
            logger.info(
                "Migrated token.json from working directory to user-data directory: %s",
                canonical,
            )
    return str(canonical)


TOKEN_PATH: str = _resolve_token_path()
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailSource:
    """Fetch and parse job alert emails from Gmail."""

    def __init__(self, token_path: str | None = None):
        # Resolve token path at construction time (not import time) so that
        # JOB_CANNON_USER_DATA_DIR test overrides are honoured.
        resolved = token_path if token_path is not None else _resolve_token_path()
        self.service = self._authenticate(resolved)
        self.parse_failures: list[dict] = []
        self.extraction_records: list[dict] = []

    def _authenticate(self, token_path: str):
        """Load saved OAuth credentials and build the Gmail service."""
        try:
            from job_finder.gmail_auth import AuthenticationError, get_credentials

            creds = get_credentials(token_path)
            return build("gmail", "v1", credentials=creds)
        except AuthenticationError as exc:
            raise RuntimeError(str(exc)) from exc

    def fetch_jobs(
        self,
        lookback_days: int = 7,
        processed_message_ids: set[str] | None = None,
        config: dict | None = None,
    ) -> tuple[list[Job], list[str]]:
        """Fetch all job alert emails from the last N days and parse them.

        Args:
            lookback_days: How many days back to search.
            processed_message_ids: Set of Gmail message IDs already processed
                in a previous sync. Matching messages are skipped to avoid
                re-fetching and re-parsing. Pass None (default) to process all.
            config: Full config dict (or None). Used to resolve user-overridden
                sender FROM addresses (``sources.gmail.senders``); None uses the
                built-in defaults unchanged.

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

        sender_parsers = resolve_sender_parsers(config)
        sender_label = resolve_sender_label(config)

        for sender, parser_fn in sender_parsers.items():
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
                    # Phase C: email override pre-check (dormant when no override files present).
                    # With no override, falls through to extract_with_fallback unchanged.
                    _label = sender_label.get(sender, sender)
                    _recipe = _override_loader.html_recipe(_label)
                    _legacy_count = None
                    _extractor = "legacy"
                    if _recipe is not None:
                        _recipe_jobs = RecipeExtractor(_recipe, job_source="email_recipe")(body)
                    else:
                        _recipe_jobs = []
                    if _recipe_jobs:
                        # Phase D shadow guard: the primary parser runs too; counts
                        # are compared post-ingestion (see health_monitor).
                        _legacy_count = len(extract_primary(parser_fn, body, email_date))
                        _extractor = "override"
                        jobs = _recipe_jobs
                    else:
                        jobs = extract_with_fallback(parser_fn, body, email_date)
                    all_jobs.extend(jobs)
                    self.extraction_records.append(
                        {
                            "label": _label,
                            "raw_text": body,
                            "job_count": len(jobs),
                            "legacy_count": _legacy_count,
                            "extractor": _extractor,
                        }
                    )

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

    def _get_message(self, message_id: str) -> dict | None:
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

    def _extract_body(self, message: dict) -> str | None:
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

    def _extract_date(self, message: dict) -> datetime | None:
        """Extract the email date from headers."""
        headers = message.get("payload", {}).get("headers", [])
        for header in headers:
            if header["name"].lower() == "date":
                try:
                    from email.utils import parsedate_to_datetime

                    return (
                        parsedate_to_datetime(header["value"]).astimezone(UTC).replace(tzinfo=None)
                    )
                except Exception:
                    logger.debug("email date parse failed", exc_info=True)

        # Fallback to internalDate
        internal_date = message.get("internalDate")
        if internal_date:
            return datetime.fromtimestamp(int(internal_date) / 1000, tz=UTC).replace(tzinfo=None)

        return None
