"""Tests for IMAP source — safety contract and criteria builder."""

import email
import email.mime.multipart
import email.mime.text
import email.policy
from unittest.mock import MagicMock, patch

import pytest

from job_finder.sources.gmail_source import SENDER_PARSERS
from job_finder.sources.imap_source import ImapSource, _build_from_search_criteria

# ---------------------------------------------------------------------------
# _build_from_search_criteria — pure function, no mocking needed
# ---------------------------------------------------------------------------


def test_criteria_single_sender():
    """Single sender → UNSEEN + plain FROM clause (no OR wrapper)."""
    criteria = _build_from_search_criteria(["alert@indeed.com"])
    assert criteria == ["UNSEEN", ["FROM", "alert@indeed.com"]]


def test_criteria_two_senders():
    """Two senders → UNSEEN + binary OR."""
    criteria = _build_from_search_criteria(["a@example.com", "b@example.com"])
    assert criteria == [
        "UNSEEN",
        ["OR", ["FROM", "a@example.com"], ["FROM", "b@example.com"]],
    ]


def test_criteria_three_senders():
    """Three senders → UNSEEN + right-folded OR."""
    criteria = _build_from_search_criteria(["a@x.com", "b@x.com", "c@x.com"])
    # right-fold: OR(a, OR(b, c))
    assert criteria == [
        "UNSEEN",
        ["OR", ["FROM", "a@x.com"], ["OR", ["FROM", "b@x.com"], ["FROM", "c@x.com"]]],
    ]


def test_criteria_empty_raises():
    """Empty sender list is a programming error — must raise."""
    with pytest.raises(ValueError, match="non-empty"):
        _build_from_search_criteria([])


def test_criteria_all_known_senders_present():
    """Criteria derived from SENDER_PARSERS must include a FROM clause for every known sender."""
    from job_finder.sources.imap_source import _build_from_search_criteria

    senders = list(SENDER_PARSERS.keys())
    criteria = _build_from_search_criteria(senders)

    # Every known sender must appear somewhere in the criteria tree
    criteria_str = str(criteria)
    for sender in senders:
        assert sender in criteria_str, f"Sender {sender!r} missing from search criteria"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_imap_client():
    """Mock IMAPClient context manager."""
    with patch("job_finder.sources.imap_source.IMAPClient") as mock:
        yield mock


def _make_rfc822(sender: str, body: str = "Sample body") -> bytes:
    """Build minimal RFC 5322 bytes for a message."""
    return (f"From: {sender}\r\nDate: Thu, 16 May 2026 12:00:00 +0000\r\n\r\n{body}").encode()


# ---------------------------------------------------------------------------
# (a) Search criteria include FROM scoping for all known senders
# ---------------------------------------------------------------------------


def test_search_uses_from_scoped_criteria(mock_imap_client):
    """fetch_jobs must search with FROM criteria covering every SENDER_PARSERS key."""
    mock_instance = MagicMock()
    mock_imap_client.return_value.__enter__.return_value = mock_instance
    mock_instance.search.return_value = []

    source = ImapSource(email_address="test@gmail.com", app_password="pw")
    source.fetch_jobs()

    assert mock_instance.search.call_count == 1
    (passed_criteria,) = mock_instance.search.call_args.args

    criteria_str = str(passed_criteria)
    for sender in SENDER_PARSERS:
        assert sender in criteria_str, f"Sender {sender!r} absent from search criteria"


def test_search_includes_unseen_flag(mock_imap_client):
    """UNSEEN must be the first element of the search criteria."""
    mock_instance = MagicMock()
    mock_imap_client.return_value.__enter__.return_value = mock_instance
    mock_instance.search.return_value = []

    source = ImapSource(email_address="test@gmail.com", app_password="pw")
    source.fetch_jobs()

    (passed_criteria,) = mock_instance.search.call_args.args
    assert passed_criteria[0] == "UNSEEN"


# ---------------------------------------------------------------------------
# (b) Fetch uses BODY.PEEK[]
# ---------------------------------------------------------------------------


def test_fetch_uses_body_peek(mock_imap_client):
    """fetch_jobs must request BODY.PEEK[] — not RFC822."""
    mock_instance = MagicMock()
    mock_imap_client.return_value.__enter__.return_value = mock_instance
    mock_instance.search.return_value = [1]
    mock_instance.fetch.return_value = {
        1: {b"BODY[]": _make_rfc822("jobalerts-noreply@linkedin.com")}
    }

    source = ImapSource(email_address="test@gmail.com", app_password="pw")
    source.fetch_jobs()

    mock_instance.fetch.assert_called_once()
    _, fetch_args = mock_instance.fetch.call_args.args  # (uids, data_items)
    assert fetch_args == ["BODY.PEEK[]"], f"Expected ['BODY.PEEK[]'] but got {fetch_args!r}"


def test_fetch_does_not_use_rfc822(mock_imap_client):
    """RFC822 must never appear in a fetch call (it implicitly sets \\Seen)."""
    mock_instance = MagicMock()
    mock_imap_client.return_value.__enter__.return_value = mock_instance
    mock_instance.search.return_value = [1]
    mock_instance.fetch.return_value = {
        1: {b"BODY[]": _make_rfc822("jobalerts-noreply@linkedin.com")}
    }

    source = ImapSource(email_address="test@gmail.com", app_password="pw")
    source.fetch_jobs()

    for c in mock_instance.fetch.call_args_list:
        _, data_items = c.args
        assert "RFC822" not in data_items, "RFC822 found in fetch args — must use BODY.PEEK[]"


# ---------------------------------------------------------------------------
# (c) add_flags(\Seen) called only for known-sender messages
# ---------------------------------------------------------------------------


def test_known_sender_is_flagged_seen(mock_imap_client):
    """After processing a known-sender message, \\Seen must be added to it."""
    mock_instance = MagicMock()
    mock_imap_client.return_value.__enter__.return_value = mock_instance
    mock_instance.search.return_value = [42]
    mock_instance.fetch.return_value = {
        42: {b"BODY[]": _make_rfc822("jobalerts-noreply@linkedin.com", "Jobs here")}
    }

    source = ImapSource(email_address="test@gmail.com", app_password="pw")
    source.fetch_jobs()

    # add_flags must have been called with uid 42
    flagged_uids = []
    for c in mock_instance.add_flags.call_args_list:
        uids_arg, flags_arg = c.args
        if b"\\Seen" in flags_arg:
            flagged_uids.extend(uids_arg)
    assert 42 in flagged_uids, "Known-sender UID 42 was not flagged \\Seen"


def test_multiple_known_senders_bulk_flagged(mock_imap_client):
    """Multiple known-sender messages are flagged in a single bulk call."""
    mock_instance = MagicMock()
    mock_imap_client.return_value.__enter__.return_value = mock_instance
    mock_instance.search.return_value = [10, 20]
    mock_instance.fetch.return_value = {
        10: {b"BODY[]": _make_rfc822("jobalerts-noreply@linkedin.com")},
        20: {b"BODY[]": _make_rfc822("noreply@glassdoor.com")},
    }

    source = ImapSource(email_address="test@gmail.com", app_password="pw")
    source.fetch_jobs()

    # Collect all UIDs that ended up in add_flags calls
    all_flagged: list[int] = []
    for c in mock_instance.add_flags.call_args_list:
        uids_arg, flags_arg = c.args
        if b"\\Seen" in flags_arg:
            all_flagged.extend(uids_arg)
    assert set(all_flagged) == {10, 20}


# ---------------------------------------------------------------------------
# (d) Unknown-sender messages never fetched in full nor flagged
# ---------------------------------------------------------------------------


def test_unknown_sender_not_flagged(mock_imap_client):
    """A message that slips through the FROM scope with no matching parser
    must not receive the \\Seen flag.

    This simulates an IMAP server false-positive match.
    """
    mock_instance = MagicMock()
    mock_imap_client.return_value.__enter__.return_value = mock_instance
    # UID 99 is from an unknown address
    mock_instance.search.return_value = [99]
    mock_instance.fetch.return_value = {
        99: {b"BODY[]": _make_rfc822("random-person@personal.com")}
    }

    source = ImapSource(email_address="test@gmail.com", app_password="pw")
    jobs, uids = source.fetch_jobs()

    # No \\Seen flag must be set for UID 99
    for c in mock_instance.add_flags.call_args_list:
        uids_arg, flags_arg = c.args
        if b"\\Seen" in flags_arg:
            assert 99 not in uids_arg, "Unknown-sender UID 99 was incorrectly flagged \\Seen"

    assert "99" not in uids, "Unknown-sender UID 99 should not be in processed_uids"


def test_no_search_scope_leaks_to_all_unseen(mock_imap_client):
    """The search criteria must not be the bare ["UNSEEN"] which would match
    every unread message in the inbox."""
    mock_instance = MagicMock()
    mock_imap_client.return_value.__enter__.return_value = mock_instance
    mock_instance.search.return_value = []

    source = ImapSource(email_address="test@gmail.com", app_password="pw")
    source.fetch_jobs()

    (passed_criteria,) = mock_instance.search.call_args.args
    # Must have more than just "UNSEEN"
    assert passed_criteria != ["UNSEEN"], (
        "Search criteria is bare ['UNSEEN'] — will match all unread mail"
    )
    assert len(passed_criteria) > 1, "Search criteria must include FROM scoping"


# ---------------------------------------------------------------------------
# Regression / interface tests
# ---------------------------------------------------------------------------


def test_fetch_jobs_returns_empty_for_no_unseen_messages(mock_imap_client):
    """Empty search result returns empty lists without calling fetch."""
    mock_instance = MagicMock()
    mock_imap_client.return_value.__enter__.return_value = mock_instance
    mock_instance.search.return_value = []

    source = ImapSource(email_address="test@gmail.com", app_password="pw")
    jobs, uids = source.fetch_jobs()

    assert jobs == []
    assert uids == []
    mock_instance.fetch.assert_not_called()
    mock_instance.add_flags.assert_not_called()


def test_login_failure_does_not_expose_password(mock_imap_client):
    """Login failures must not expose the app password in the exception message."""
    mock_instance = MagicMock()
    mock_imap_client.return_value.__enter__.return_value = mock_instance
    mock_instance.login.side_effect = Exception("Authentication failed")

    source = ImapSource(email_address="test@gmail.com", app_password="secret_password")
    with pytest.raises(Exception) as exc_info:
        source.fetch_jobs()

    assert "secret_password" not in str(exc_info.value)
    assert "Authentication failed" in str(exc_info.value)


def test_extract_body_prefers_text_plain():
    """_extract_body prefers text/plain over text/html."""
    msg = email.mime.multipart.MIMEMultipart()
    msg.attach(email.mime.text.MIMEText("Plain text content", _subtype="plain"))
    msg.attach(email.mime.text.MIMEText("<html>HTML content</html>", _subtype="html"))

    source = ImapSource()
    body = source._extract_body(msg)

    assert body == "Plain text content"


def test_extract_body_falls_back_to_html():
    """_extract_body falls back to text/html when no plain text part."""
    msg = email.mime.text.MIMEText("<html>HTML content</html>", _subtype="html")

    source = ImapSource()
    assert source._extract_body(msg) == "<html>HTML content</html>"


def test_extract_sender_with_name():
    """_extract_sender handles 'Name <email>' format."""
    msg = email.message.Message()
    msg["From"] = "John Doe <john@example.com>"
    assert ImapSource()._extract_sender(msg) == "john@example.com"


def test_extract_sender_plain_email():
    """_extract_sender handles bare email address."""
    msg = email.message.Message()
    msg["From"] = "john@example.com"
    assert ImapSource()._extract_sender(msg) == "john@example.com"


def test_extract_date():
    """_extract_date parses a standard Date header."""
    msg = email.message.Message()
    msg["Date"] = "Thu, 16 May 2026 12:00:00 +0000"
    dt = ImapSource()._extract_date(msg)
    assert dt is not None
    assert (dt.year, dt.month, dt.day, dt.hour) == (2026, 5, 16, 12)


def test_extract_date_missing():
    """_extract_date returns None when Date header is absent."""
    assert ImapSource()._extract_date(email.message.Message()) is None


def test_processed_message_ids_argument_ignored(mock_imap_client):
    """processed_message_ids is accepted but IMAP dedup uses \\Seen, not the set."""
    mock_instance = MagicMock()
    mock_imap_client.return_value.__enter__.return_value = mock_instance
    mock_instance.search.return_value = [1]
    mock_instance.fetch.return_value = {
        1: {b"BODY[]": _make_rfc822("jobalerts-noreply@linkedin.com")}
    }

    source = ImapSource(email_address="test@gmail.com", app_password="pw")
    _, uids = source.fetch_jobs(processed_message_ids={"msg1", "msg2"})

    # IMAP search is still called with the scoped FROM criteria, not gated on the set
    assert mock_instance.search.call_count == 1
    assert uids == ["1"]
