"""Unit tests for job_finder.web.onboarding.gmail_test (F1).

Mirrors the test_onboarding_imap_test shape — all outcomes mocked, no
real network.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from job_finder.web.onboarding.gmail_test import GmailTestResult, check_oauth

_GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


def test_returns_no_token_when_file_missing(tmp_path):
    """Missing token.json → error_kind='no_token', message names the path."""
    missing = tmp_path / "no_such_token.json"

    result = check_oauth(str(missing))

    assert result.ok is False
    assert result.error_kind == "no_token"
    assert str(missing) in result.message
    assert "python -m job_finder.gmail_auth" in result.message


def test_returns_scope_when_drive_only(tmp_path):
    """Token granted but lacks gmail.readonly → error_kind='scope'."""
    token = tmp_path / "token.json"
    token.write_text("{}")

    with patch("job_finder.gmail_auth._check_token_scopes") as scopes:
        scopes.return_value = {"https://www.googleapis.com/auth/drive.file"}
        result = check_oauth(str(token))

    assert result.ok is False
    assert result.error_kind == "scope"
    assert "gmail.readonly" in result.message


def test_returns_refresh_when_auth_error(tmp_path):
    """AuthenticationError from get_credentials → error_kind='refresh'."""
    token = tmp_path / "token.json"
    token.write_text("{}")

    from job_finder.gmail_auth import AuthenticationError

    with (
        patch("job_finder.gmail_auth._check_token_scopes") as scopes,
        patch("job_finder.gmail_auth.get_credentials") as creds,
    ):
        scopes.return_value = {_GMAIL_SCOPE}
        creds.side_effect = AuthenticationError("Token refresh failed: expired")
        result = check_oauth(str(token))

    assert result.ok is False
    assert result.error_kind == "refresh"
    assert "Token refresh failed" in result.message


def test_returns_api_when_get_profile_raises(tmp_path):
    """getProfile raising → error_kind='api', message names exception class."""
    token = tmp_path / "token.json"
    token.write_text("{}")

    fake_creds = MagicMock()

    with (
        patch("job_finder.gmail_auth._check_token_scopes") as scopes,
        patch("job_finder.gmail_auth.get_credentials") as creds,
        patch("googleapiclient.discovery.build") as build,
    ):
        scopes.return_value = {_GMAIL_SCOPE}
        creds.return_value = fake_creds

        service = MagicMock()
        users = MagicMock()
        get_profile_req = MagicMock()
        get_profile_req.execute.side_effect = ConnectionError("network down")
        users.getProfile.return_value = get_profile_req
        service.users.return_value = users
        build.return_value = service

        result = check_oauth(str(token))

    assert result.ok is False
    assert result.error_kind == "api"
    assert "ConnectionError" in result.message


def test_returns_ok_with_email_on_success(tmp_path):
    """Happy path: scope present, credentials load, getProfile returns the address."""
    token = tmp_path / "token.json"
    token.write_text("{}")

    fake_creds = MagicMock()

    with (
        patch("job_finder.gmail_auth._check_token_scopes") as scopes,
        patch("job_finder.gmail_auth.get_credentials") as creds,
        patch("googleapiclient.discovery.build") as build,
    ):
        scopes.return_value = {_GMAIL_SCOPE}
        creds.return_value = fake_creds

        service = MagicMock()
        users = MagicMock()
        get_profile_req = MagicMock()
        get_profile_req.execute.return_value = {"emailAddress": "user@gmail.com"}
        users.getProfile.return_value = get_profile_req
        service.users.return_value = users
        build.return_value = service

        result = check_oauth(str(token))

    assert result.ok is True
    assert result.error_kind is None
    assert result.email_address == "user@gmail.com"
    assert "user@gmail.com" in result.message


def test_result_is_frozen():
    """GmailTestResult is @dataclass(frozen=True) — assignment raises."""
    r = GmailTestResult(ok=True, error_kind=None, message="x")
    with pytest.raises(Exception):
        r.ok = False  # type: ignore[misc]
