"""Unit tests for job_finder.web.onboarding.imap_test (STRANGE-WIZ-04, success criterion 4).

All four outcomes mocked — no real network. Live smoke is in tests/test_onboarding_imap_test_live.py.
"""

import socket
from unittest.mock import MagicMock, patch

import pytest
from imapclient.exceptions import LoginError

from job_finder.web.onboarding.imap_test import ImapTestResult, check_imap


@pytest.fixture
def fake_creds():
    return {
        "host": "imap.gmail.com",
        "port": 993,
        "email": "fake@gmail.com",
        "app_password": "xxxx xxxx xxxx xxxx",
    }


def _build_mock_client(folder_count: int = 5):
    """Helper: return a MagicMock configured to act as an IMAPClient context manager."""
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.list_folders.return_value = [
        (b"\\HasNoChildren", b"/", f"FOLDER_{i}") for i in range(folder_count)
    ]
    return client


def test_success_returns_ok_true_with_folder_count(fake_creds):
    """Happy path: LOGIN succeeds, list_folders returns ≥1 folder, LOGOUT runs."""
    with patch("job_finder.web.onboarding.imap_test.IMAPClient") as mock_cls:
        mock_cls.return_value = _build_mock_client(folder_count=5)
        result = check_imap(**fake_creds)

    assert result.ok is True
    assert result.error_kind is None
    assert result.folder_count == 5
    assert "imap.gmail.com" in result.message
    assert "fake@gmail.com" in result.message


def test_success_calls_login_list_logout_but_not_fetch(fake_creds):
    """STRICT contract: smoke test calls LOGIN, list_folders, LOGOUT — NEVER fetch/search/select_folder."""
    with patch("job_finder.web.onboarding.imap_test.IMAPClient") as mock_cls:
        mock_client = _build_mock_client(folder_count=3)
        mock_cls.return_value = mock_client
        check_imap(**fake_creds)

    mock_client.login.assert_called_once_with("fake@gmail.com", "xxxx xxxx xxxx xxxx")
    mock_client.list_folders.assert_called_once()
    mock_client.logout.assert_called_once()
    mock_client.fetch.assert_not_called()
    mock_client.search.assert_not_called()
    mock_client.select_folder.assert_not_called()


def test_login_error_returns_auth_kind(fake_creds):
    """imapclient.exceptions.LoginError → error_kind='auth', message='Authentication failed — check your app password'."""
    with patch("job_finder.web.onboarding.imap_test.IMAPClient") as mock_cls:
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.__exit__.return_value = False
        mock_client.login.side_effect = LoginError("AUTHENTICATIONFAILED")
        mock_cls.return_value = mock_client

        result = check_imap(**fake_creds)

    assert result.ok is False
    assert result.error_kind == "auth"
    assert result.message == "Authentication failed — check your app password"


def test_gaierror_returns_host_kind(fake_creds):
    """socket.gaierror → error_kind='host', message includes the host name."""
    with patch("job_finder.web.onboarding.imap_test.IMAPClient") as mock_cls:
        mock_cls.side_effect = socket.gaierror(8, "nodename nor servname provided")

        result = check_imap(**fake_creds)

    assert result.ok is False
    assert result.error_kind == "host"
    assert "imap.gmail.com" in result.message


def test_timeout_returns_timeout_kind(fake_creds):
    """socket.timeout → error_kind='timeout', message names the timeout value."""
    with patch("job_finder.web.onboarding.imap_test.IMAPClient") as mock_cls:
        mock_cls.side_effect = TimeoutError("login took too long")

        result = check_imap(**fake_creds)

    assert result.ok is False
    assert result.error_kind == "timeout"
    assert "10s" in result.message


def test_unexpected_oserror_returns_other_kind(fake_creds):
    """Any other OSError → error_kind='other', message names the exception class."""
    with patch("job_finder.web.onboarding.imap_test.IMAPClient") as mock_cls:
        mock_cls.side_effect = ConnectionRefusedError("connection refused")

        result = check_imap(**fake_creds)

    assert result.ok is False
    assert result.error_kind == "other"
    assert "ConnectionRefusedError" in result.message


def test_zero_folders_returns_other_not_ok(fake_creds):
    """D-09 contract: list_folders returning 0 folders is not a success."""
    with patch("job_finder.web.onboarding.imap_test.IMAPClient") as mock_cls:
        mock_cls.return_value = _build_mock_client(folder_count=0)

        result = check_imap(**fake_creds)

    assert result.ok is False
    assert result.error_kind == "other"
    assert result.folder_count == 0


def test_app_password_never_in_message(fake_creds, caplog):
    """T-42-01/T-42-02: app password must NEVER appear in result.message OR captured logs, regardless of outcome."""
    creds = {**fake_creds, "app_password": "SUPER_SECRET_PW_12345"}
    with patch("job_finder.web.onboarding.imap_test.IMAPClient") as mock_cls:
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.__exit__.return_value = False
        mock_client.login.side_effect = LoginError("AUTHENTICATIONFAILED")
        mock_cls.return_value = mock_client

        with caplog.at_level("DEBUG"):
            result = check_imap(**creds)

    # message must NEVER contain the password
    assert "SUPER_SECRET_PW_12345" not in result.message

    # logs must NEVER contain the password (T-42-01)
    full_log = "\n".join(record.getMessage() for record in caplog.records)
    assert "SUPER_SECRET_PW_12345" not in full_log


def test_imap_test_result_is_frozen():
    """ImapTestResult is @dataclass(frozen=True) — assignment raises."""
    r = ImapTestResult(ok=True, error_kind=None, message="x", folder_count=1)
    with pytest.raises(Exception):
        r.ok = False  # type: ignore[misc]
