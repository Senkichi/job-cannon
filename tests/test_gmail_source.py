"""Tests for GmailSource.fetch_jobs() message-level deduplication.

Covers:
- Skipping already-processed message IDs passed via processed_message_ids
- Returning newly-processed IDs in the tuple's second element
- Backward compatibility when processed_message_ids is omitted
- API fetch failures not counted as processed (allow retry)
"""

import pytest
from unittest.mock import MagicMock, patch, call

from job_finder.sources.gmail_source import GmailSource


# Minimal single-sender parser map used throughout tests to avoid
# 8x amplification from SENDER_PARSERS looping over all real senders.
_SINGLE_SENDER = {"test@example.com": lambda body, date: []}


def _fake_message(msg_id: str) -> dict:
    """Minimal Gmail API message dict with an empty body."""
    return {
        "id": msg_id,
        "payload": {
            "body": {"data": ""},
            "parts": [],
            "headers": [],
        },
    }


@pytest.fixture
def source() -> GmailSource:
    """GmailSource instance with mocked OAuth authentication."""
    with patch.object(GmailSource, "_authenticate", return_value=MagicMock()):
        s = GmailSource()
    return s


class TestFetchJobsDedup:
    """fetch_jobs() deduplication via processed_message_ids parameter."""

    def test_skips_known_message_ids(self, source: GmailSource) -> None:
        """Messages in processed_message_ids are not fetched via _get_message."""
        known_ids = {"msg1", "msg2"}
        search_results = [{"id": "msg1"}, {"id": "msg2"}, {"id": "msg3"}]

        source._search_messages = MagicMock(return_value=search_results)
        source._get_message = MagicMock(return_value=_fake_message("msg3"))
        source._extract_body = MagicMock(return_value="")
        source._extract_date = MagicMock(return_value=None)

        with patch("job_finder.sources.gmail_source.SENDER_PARSERS", _SINGLE_SENDER):
            jobs, processed = source.fetch_jobs(processed_message_ids=known_ids)

        source._get_message.assert_called_once_with("msg3")
        assert jobs == []

    def test_returns_processed_ids(self, source: GmailSource) -> None:
        """Processed message IDs appear in the second element of the return tuple.

        Only messages with a successfully extracted body are counted as processed;
        empty/None bodies fall through for retry on the next sync.
        """
        search_results = [{"id": "msg1"}, {"id": "msg2"}]

        source._search_messages = MagicMock(return_value=search_results)
        source._get_message = MagicMock(side_effect=lambda mid: _fake_message(mid))
        # Return a non-empty body so the if-body guard passes and IDs are tracked
        source._extract_body = MagicMock(return_value="email body content")
        source._extract_date = MagicMock(return_value=None)

        with patch("job_finder.sources.gmail_source.SENDER_PARSERS", _SINGLE_SENDER):
            jobs, processed = source.fetch_jobs()

        assert set(processed) == {"msg1", "msg2"}
        assert jobs == []

    def test_no_dedup_arg_fetches_all(self, source: GmailSource) -> None:
        """Without processed_message_ids, all messages from search are fetched."""
        search_results = [{"id": "msg1"}, {"id": "msg2"}, {"id": "msg3"}]

        source._search_messages = MagicMock(return_value=search_results)
        source._get_message = MagicMock(return_value=_fake_message("x"))
        source._extract_body = MagicMock(return_value="")
        source._extract_date = MagicMock(return_value=None)

        with patch("job_finder.sources.gmail_source.SENDER_PARSERS", _SINGLE_SENDER):
            jobs, processed = source.fetch_jobs()  # no processed_message_ids

        assert source._get_message.call_count == 3

    def test_api_failure_not_marked_processed(self, source: GmailSource) -> None:
        """Messages where _get_message returns None are excluded from processed_ids."""
        search_results = [{"id": "msg1"}, {"id": "msg2"}]

        source._search_messages = MagicMock(return_value=search_results)
        source._get_message = MagicMock(return_value=None)  # All API calls fail

        with patch("job_finder.sources.gmail_source.SENDER_PARSERS", _SINGLE_SENDER):
            jobs, processed = source.fetch_jobs()

        assert processed == []
        assert jobs == []

    def test_partial_api_failure(self, source: GmailSource) -> None:
        """Only messages where body extraction succeeds appear in processed_ids.

        msg2 fails at the API layer (_get_message→None); msg1 and msg3 succeed
        with a parseable body. Only msg1 and msg3 are tracked as processed.
        """
        search_results = [{"id": "msg1"}, {"id": "msg2"}, {"id": "msg3"}]

        # msg2 fails; msg1 and msg3 succeed
        def _side_effect(mid: str) -> dict | None:
            return None if mid == "msg2" else _fake_message(mid)

        source._search_messages = MagicMock(return_value=search_results)
        source._get_message = MagicMock(side_effect=_side_effect)
        # Non-empty body so the if-body guard passes for msg1 and msg3
        source._extract_body = MagicMock(return_value="email body content")
        source._extract_date = MagicMock(return_value=None)

        with patch("job_finder.sources.gmail_source.SENDER_PARSERS", _SINGLE_SENDER):
            jobs, processed = source.fetch_jobs()

        assert set(processed) == {"msg1", "msg3"}

    def test_empty_body_not_marked_processed(self, source: GmailSource) -> None:
        """Messages where _extract_body returns None/empty are NOT added to
        processed_ids — they should be retried on the next sync."""
        search_results = [{"id": "msg1"}]

        source._search_messages = MagicMock(return_value=search_results)
        source._get_message = MagicMock(return_value=_fake_message("msg1"))
        source._extract_body = MagicMock(return_value=None)  # Body extraction failed
        source._extract_date = MagicMock(return_value=None)

        with patch("job_finder.sources.gmail_source.SENDER_PARSERS", _SINGLE_SENDER):
            jobs, processed = source.fetch_jobs()

        assert processed == [], "Empty-body message should not be marked as processed"

    def test_empty_processed_set_fetches_all(self, source: GmailSource) -> None:
        """Passing an empty set for processed_message_ids fetches all messages."""
        search_results = [{"id": "msg1"}, {"id": "msg2"}]

        source._search_messages = MagicMock(return_value=search_results)
        source._get_message = MagicMock(return_value=_fake_message("x"))
        source._extract_body = MagicMock(return_value="")
        source._extract_date = MagicMock(return_value=None)

        with patch("job_finder.sources.gmail_source.SENDER_PARSERS", _SINGLE_SENDER):
            jobs, processed = source.fetch_jobs(processed_message_ids=set())

        assert source._get_message.call_count == 2

    def test_parse_failure_includes_message_id(self, source: GmailSource) -> None:
        """Parse failures record message_id for dedup guard in pipeline_runner."""
        search_results = [{"id": "msg1"}]

        # Return a message with a body long enough to trigger _should_archive_failure
        long_body = "x" * 600  # >500 chars, no meta indicators

        def _parser(body: str, date) -> list:
            return []  # zero jobs = parse failure

        source._search_messages = MagicMock(return_value=search_results)
        source._get_message = MagicMock(return_value=_fake_message("msg1"))
        source._extract_body = MagicMock(return_value=long_body)
        source._extract_date = MagicMock(return_value=None)

        test_sender = {"test@example.com": _parser}

        with patch("job_finder.sources.gmail_source.SENDER_PARSERS", test_sender):
            with patch("job_finder.sources.gmail_source._archive_parse_failure"):
                jobs, processed = source.fetch_jobs()

        assert len(source.parse_failures) == 1
        assert source.parse_failures[0]["message_id"] == "msg1"
        assert source.parse_failures[0]["sender"] == "test@example.com"
