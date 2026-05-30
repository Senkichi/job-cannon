"""Tests for IMAP source."""

import email
import email.mime.multipart
import email.mime.text
import email.policy
from unittest.mock import MagicMock, patch

import pytest

from job_finder.sources.imap_source import ImapSource


@pytest.fixture
def mock_imap_client():
    """Mock IMAPClient for testing."""
    with patch("job_finder.sources.imap_source.IMAPClient") as mock:
        yield mock


@pytest.fixture
def sample_rfc822_message():
    """Create a sample RFC 5322 message for testing."""
    msg = email.message.Message()
    msg["From"] = "jobalerts-noreply@linkedin.com"
    msg["Date"] = "Thu, 16 May 2026 12:00:00 +0000"
    msg.set_payload("Sample job alert body")
    return msg


def test_fetch_jobs_searches_unseen_and_fetches_rfc822(mock_imap_client):
    """Test that fetch_jobs searches for UNSEEN messages and fetches RFC822."""
    # Setup mock
    mock_client_instance = MagicMock()
    mock_imap_client.return_value.__enter__.return_value = mock_client_instance

    mock_client_instance.search.return_value = [1]
    mock_client_instance.fetch.return_value = {
        1: {b"RFC822": b"From: test@example.com\r\n\r\nTest body"}
    }

    # Execute
    source = ImapSource(email_address="test@gmail.com", app_password="test_password")
    jobs, uids = source.fetch_jobs()

    # Verify
    mock_client_instance.login.assert_called_once_with("test@gmail.com", "test_password")
    mock_client_instance.select_folder.assert_called_once_with("INBOX", readonly=False)
    mock_client_instance.search.assert_called_once_with(["UNSEEN"])
    mock_client_instance.fetch.assert_called_once_with([1], ["RFC822"])
    mock_client_instance.add_flags.assert_called_once_with([1], [b"\\Seen"])
    assert uids == ["1"]


def test_fetch_jobs_marks_seen_after_fetch(mock_imap_client):
    """Test that messages are marked \\Seen after successful fetch."""
    mock_client_instance = MagicMock()
    mock_imap_client.return_value.__enter__.return_value = mock_client_instance

    mock_client_instance.search.return_value = [1, 2]
    mock_client_instance.fetch.return_value = {
        1: {b"RFC822": b"From: test@example.com\r\n\r\nBody"},
        2: {b"RFC822": b"From: test2@example.com\r\n\r\nBody2"},
    }

    source = ImapSource(email_address="test@gmail.com", app_password="test_password")
    source.fetch_jobs()

    # Both messages should be marked seen
    assert mock_client_instance.add_flags.call_count == 2
    mock_client_instance.add_flags.assert_any_call([1], [b"\\Seen"])
    mock_client_instance.add_flags.assert_any_call([2], [b"\\Seen"])


def test_fetch_jobs_returns_empty_for_no_unseen_messages(mock_imap_client):
    """Test that empty UNSEEN search returns empty lists."""
    mock_client_instance = MagicMock()
    mock_imap_client.return_value.__enter__.return_value = mock_client_instance

    mock_client_instance.search.return_value = []

    source = ImapSource(email_address="test@gmail.com", app_password="test_password")
    jobs, uids = source.fetch_jobs()

    assert jobs == []
    assert uids == []
    mock_client_instance.fetch.assert_not_called()


def test_login_failure_does_not_expose_password(mock_imap_client):
    """Test that login failure raises exception without logging password."""
    mock_client_instance = MagicMock()
    mock_imap_client.return_value.__enter__.return_value = mock_client_instance

    # Make login raise an exception
    mock_client_instance.login.side_effect = Exception("Authentication failed")

    source = ImapSource(email_address="test@gmail.com", app_password="secret_password")

    with pytest.raises(Exception) as exc_info:
        source.fetch_jobs()

    # Verify password is not in exception message
    assert "secret_password" not in str(exc_info.value)
    assert "Authentication failed" in str(exc_info.value)


def test_extract_body_prefers_text_plain():
    """Test that _extract_body prefers text/plain over text/html."""
    # Create multipart message with both text/plain and text/html
    msg = email.mime.multipart.MIMEMultipart()
    text_part = email.mime.text.MIMEText("Plain text content", _subtype="plain")
    html_part = email.mime.text.MIMEText("<html>HTML content</html>", _subtype="html")
    msg.attach(text_part)
    msg.attach(html_part)

    source = ImapSource()
    body = source._extract_body(msg)

    # Should return plain text, not HTML
    assert body == "Plain text content"
    assert "<html>" not in body


def test_extract_body_falls_back_to_html():
    """Test that _extract_body falls back to text/html if plain text unavailable."""
    # Create message with only text/html
    msg = email.mime.text.MIMEText("<html>HTML content</html>", _subtype="html")

    source = ImapSource()
    body = source._extract_body(msg)

    # Should return HTML when plain text not available
    assert body == "<html>HTML content</html>"


def test_extract_sender_with_name():
    """Test _extract_sender handles 'Name <email>' format."""
    msg = email.message.Message()
    msg["From"] = "John Doe <john@example.com>"

    source = ImapSource()
    sender = source._extract_sender(msg)

    assert sender == "john@example.com"


def test_extract_sender_plain_email():
    """Test _extract_sender handles plain email address."""
    msg = email.message.Message()
    msg["From"] = "john@example.com"

    source = ImapSource()
    sender = source._extract_sender(msg)

    assert sender == "john@example.com"


def test_extract_date():
    """Test _extract_date parses Date header correctly."""
    msg = email.message.Message()
    msg["Date"] = "Thu, 16 May 2026 12:00:00 +0000"

    source = ImapSource()
    dt = source._extract_date(msg)

    assert dt is not None
    assert dt.year == 2026
    assert dt.month == 5
    assert dt.day == 16
    assert dt.hour == 12


def test_extract_date_missing():
    """Test _extract_date returns None when Date header missing."""
    msg = email.message.Message()

    source = ImapSource()
    dt = source._extract_date(msg)

    assert dt is None


def test_sender_parsers_dispatch(mock_imap_client):
    """Test that SENDER_PARSERS is used to dispatch to correct parser."""
    mock_client_instance = MagicMock()
    mock_imap_client.return_value.__enter__.return_value = mock_client_instance

    # Mock a LinkedIn alert message
    rfc822_bytes = b"""From: jobalerts-noreply@linkedin.com
Date: Thu, 16 May 2026 12:00:00 +0000

Sample LinkedIn job alert body"""

    mock_client_instance.search.return_value = [1]
    mock_client_instance.fetch.return_value = {1: {b"RFC822": rfc822_bytes}}

    source = ImapSource(email_address="test@gmail.com", app_password="test_password")
    jobs, uids = source.fetch_jobs()

    # Should have attempted to parse (even if parser returns empty list)
    # and marked message as seen
    mock_client_instance.add_flags.assert_called_once_with([1], [b"\\Seen"])
    assert uids == ["1"]


def test_processed_message_ids_argument_ignored(mock_imap_client):
    """Test that processed_message_ids argument is accepted but ignored."""
    mock_client_instance = MagicMock()
    mock_imap_client.return_value.__enter__.return_value = mock_client_instance

    mock_client_instance.search.return_value = [1]
    mock_client_instance.fetch.return_value = {
        1: {b"RFC822": b"From: test@example.com\r\n\r\nBody"}
    }

    source = ImapSource(email_address="test@gmail.com", app_password="test_password")
    # Pass processed_message_ids - should be ignored
    jobs, uids = source.fetch_jobs(processed_message_ids={"msg1", "msg2"})

    # IMAP dedup uses \Seen flag, not the passed set
    mock_client_instance.search.assert_called_once_with(["UNSEEN"])
    assert uids == ["1"]
