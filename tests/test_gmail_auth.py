"""Tests for gmail_auth.py OAuth flow (Gmail-only, no Drive scope).

Covers:
- _check_token_scopes: reads actual granted scopes from token.json
- authenticate: basic flow, token saved under user-data dir
- authenticate: prints Gmail scope checklist only (no Drive entry)
- get_credentials: load, refresh, error paths
- No Drive scope, no _validate_drive_api, no _ensure_drive_folder
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# google_auth_oauthlib may not be installed in test environments.
# Mock it at the sys.modules level so gmail_auth can be imported and the
# lazy import inside authenticate() resolves to the mock.
if "google_auth_oauthlib" not in sys.modules:
    _mock_oauthlib = MagicMock()
    sys.modules["google_auth_oauthlib"] = _mock_oauthlib
    sys.modules["google_auth_oauthlib.flow"] = _mock_oauthlib.flow


class TestGetCredentials:
    """get_credentials() loads and refreshes OAuth credentials non-interactively."""

    def test_returns_valid_credentials(self, tmp_path):
        """get_credentials returns credentials when token is valid."""
        from job_finder.gmail_auth import get_credentials

        token_path = str(tmp_path / "token.json")
        Path(token_path).write_text('{"token": "fake"}')

        mock_creds = MagicMock()
        mock_creds.valid = True

        with patch(
            "job_finder.gmail_auth.Credentials.from_authorized_user_file",
            return_value=mock_creds,
        ):
            result = get_credentials(token_path)

        assert result is mock_creds

    def test_refreshes_expired_token(self, tmp_path):
        """get_credentials refreshes an expired token and persists it."""
        from job_finder.gmail_auth import get_credentials

        token_path = str(tmp_path / "token.json")
        Path(token_path).write_text('{"token": "fake"}')

        mock_creds = MagicMock()
        mock_creds.valid = False
        mock_creds.expired = True
        mock_creds.refresh_token = "refresh_tok"
        mock_creds.to_json.return_value = '{"token": "refreshed"}'

        def _refresh(request):
            mock_creds.valid = True

        mock_creds.refresh.side_effect = _refresh

        with patch(
            "job_finder.gmail_auth.Credentials.from_authorized_user_file",
            return_value=mock_creds,
        ):
            result = get_credentials(token_path)

        assert result is mock_creds
        mock_creds.refresh.assert_called_once()
        assert Path(token_path).read_text() == '{"token": "refreshed"}'

    def test_raises_on_missing_token(self, tmp_path):
        """get_credentials raises AuthenticationError when token file is missing."""
        from job_finder.gmail_auth import AuthenticationError, get_credentials

        token_path = str(tmp_path / "no_token.json")

        with pytest.raises(AuthenticationError, match="not found"):
            get_credentials(token_path)

    def test_raises_on_refresh_failure(self, tmp_path):
        """get_credentials raises AuthenticationError when token refresh fails."""
        from job_finder.gmail_auth import AuthenticationError, get_credentials

        token_path = str(tmp_path / "token.json")
        Path(token_path).write_text('{"token": "fake"}')

        mock_creds = MagicMock()
        mock_creds.valid = False
        mock_creds.expired = True
        mock_creds.refresh_token = "refresh_tok"
        mock_creds.refresh.side_effect = Exception("network error")

        with (
            patch(
                "job_finder.gmail_auth.Credentials.from_authorized_user_file",
                return_value=mock_creds,
            ),
            pytest.raises(AuthenticationError, match="refresh failed"),
        ):
            get_credentials(token_path)

    def test_raises_on_invalid_no_refresh_token(self, tmp_path):
        """get_credentials raises when token is invalid and has no refresh token."""
        from job_finder.gmail_auth import AuthenticationError, get_credentials

        token_path = str(tmp_path / "token.json")
        Path(token_path).write_text('{"token": "fake"}')

        mock_creds = MagicMock()
        mock_creds.valid = False
        mock_creds.expired = False
        mock_creds.refresh_token = None

        with (
            patch(
                "job_finder.gmail_auth.Credentials.from_authorized_user_file",
                return_value=mock_creds,
            ),
            pytest.raises(AuthenticationError, match="cannot be refreshed"),
        ):
            get_credentials(token_path)


class TestCheckTokenScopes:
    """_check_token_scopes reads actual granted scopes from token.json."""

    def test_returns_set_of_granted_scopes(self, tmp_path):
        """_check_token_scopes returns set of scopes from token.json."""
        from job_finder.gmail_auth import _check_token_scopes

        token_path = str(tmp_path / "token.json")
        Path(token_path).write_text('{"token": "fake"}')

        mock_creds = MagicMock()
        mock_creds.scopes = ["https://www.googleapis.com/auth/gmail.readonly"]

        with patch(
            "job_finder.gmail_auth.Credentials.from_authorized_user_file",
            return_value=mock_creds,
        ):
            result = _check_token_scopes(token_path)

        assert result == {"https://www.googleapis.com/auth/gmail.readonly"}

    def test_returns_empty_set_when_no_token(self, tmp_path):
        """_check_token_scopes returns empty set when token.json does not exist."""
        from job_finder.gmail_auth import _check_token_scopes

        token_path = str(tmp_path / "no_token.json")
        result = _check_token_scopes(token_path)

        assert result == set()

    def test_returns_empty_set_when_token_malformed(self, tmp_path):
        """_check_token_scopes returns empty set when token.json is malformed."""
        from job_finder.gmail_auth import _check_token_scopes

        token_path = str(tmp_path / "token.json")
        Path(token_path).write_text("not valid json{")

        with patch(
            "job_finder.gmail_auth.Credentials.from_authorized_user_file",
            side_effect=Exception("invalid JSON"),
        ):
            result = _check_token_scopes(token_path)

        assert result == set()


class TestScopesNodriveScope:
    """SCOPES must contain only gmail.readonly — no Drive scope."""

    def test_scopes_contains_only_gmail_readonly(self):
        """SCOPES has exactly one entry: gmail.readonly."""
        from job_finder.gmail_auth import SCOPES

        assert SCOPES == ["https://www.googleapis.com/auth/gmail.readonly"]

    def test_no_drive_in_scopes(self):
        """No drive scope present in SCOPES."""
        from job_finder.gmail_auth import SCOPES

        for scope in SCOPES:
            assert "drive" not in scope.lower(), f"Drive scope found: {scope}"

    def test_no_validate_drive_api(self):
        """_validate_drive_api is not present (Drive validation removed)."""
        import job_finder.gmail_auth as mod

        assert not hasattr(mod, "_validate_drive_api"), (
            "_validate_drive_api should not exist after Drive removal"
        )

    def test_no_ensure_drive_folder(self):
        """_ensure_drive_folder is not present (Drive folder creation removed)."""
        import job_finder.gmail_auth as mod

        assert not hasattr(mod, "_ensure_drive_folder"), (
            "_ensure_drive_folder should not exist after Drive removal"
        )


class TestAuthenticateTokenPaths:
    """authenticate() writes token.json to user-data dir, not CWD."""

    def test_token_saved_under_user_data_dir(self, tmp_path, monkeypatch):
        """authenticate saves token.json under JOB_CANNON_USER_DATA_DIR."""
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))

        from job_finder import gmail_auth
        from job_finder.web.user_data_dirs import token_path as canonical_token_path

        canonical = str(canonical_token_path())
        creds_path = str(tmp_path / "credentials.json")
        (tmp_path / "credentials.json").write_text('{"installed": {}}')

        mock_new_creds = MagicMock()
        mock_new_creds.scopes = ["https://www.googleapis.com/auth/gmail.readonly"]
        mock_new_creds.valid = True
        mock_new_creds.expired = False
        mock_new_creds.to_json.return_value = '{"token": "new"}'

        mock_flow = MagicMock()
        mock_flow.run_local_server.return_value = mock_new_creds

        original_token = gmail_auth.TOKEN_PATH
        original_creds = gmail_auth.CREDENTIALS_PATH
        try:
            gmail_auth.TOKEN_PATH = canonical
            gmail_auth.CREDENTIALS_PATH = creds_path

            with (
                patch(
                    "google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file",
                    return_value=mock_flow,
                ),
            ):
                gmail_auth.authenticate()

            assert Path(canonical).exists(), "Token should be saved at canonical path"
            # Must NOT be written to CWD
            assert not Path("token.json").exists(), "Token must not be written to CWD"
        finally:
            gmail_auth.TOKEN_PATH = original_token
            gmail_auth.CREDENTIALS_PATH = original_creds

    def test_no_config_yaml_write_in_authenticate(self, tmp_path, monkeypatch):
        """authenticate() never writes config.yaml (Drive folder removal check)."""
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))

        from job_finder import gmail_auth

        canonical_token = str(tmp_path / "token.json")
        creds_path = str(tmp_path / "credentials.json")
        (tmp_path / "credentials.json").write_text('{"installed": {}}')

        mock_new_creds = MagicMock()
        mock_new_creds.scopes = ["https://www.googleapis.com/auth/gmail.readonly"]
        mock_new_creds.valid = True
        mock_new_creds.expired = False
        mock_new_creds.to_json.return_value = '{"token": "new"}'

        mock_flow = MagicMock()
        mock_flow.run_local_server.return_value = mock_new_creds

        config_yaml = tmp_path / "config.yaml"
        original_token = gmail_auth.TOKEN_PATH
        original_creds = gmail_auth.CREDENTIALS_PATH
        try:
            gmail_auth.TOKEN_PATH = canonical_token
            gmail_auth.CREDENTIALS_PATH = creds_path

            with patch(
                "google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file",
                return_value=mock_flow,
            ):
                gmail_auth.authenticate()

            assert not config_yaml.exists(), "authenticate() must not write config.yaml"
        finally:
            gmail_auth.TOKEN_PATH = original_token
            gmail_auth.CREDENTIALS_PATH = original_creds


class TestAuthenticateScopeChecklist:
    """authenticate() prints Gmail-only scope checklist."""

    def test_prints_gmail_scope_checklist(self, tmp_path, capsys):
        """authenticate prints scope checklist with Gmail checkmark only."""
        from job_finder import gmail_auth

        token_path = str(tmp_path / "token.json")
        creds_path = str(tmp_path / "credentials.json")
        (tmp_path / "credentials.json").write_text('{"installed": {}}')

        mock_new_creds = MagicMock()
        mock_new_creds.valid = True
        mock_new_creds.expired = False
        mock_new_creds.to_json.return_value = '{"token": "new"}'

        mock_flow = MagicMock()
        mock_flow.run_local_server.return_value = mock_new_creds

        gmail_scope = "https://www.googleapis.com/auth/gmail.readonly"

        original_token = gmail_auth.TOKEN_PATH
        original_creds = gmail_auth.CREDENTIALS_PATH
        try:
            gmail_auth.TOKEN_PATH = token_path
            gmail_auth.CREDENTIALS_PATH = creds_path

            with (
                patch(
                    "google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file",
                    return_value=mock_flow,
                ),
                patch(
                    "job_finder.gmail_auth._check_token_scopes",
                    return_value={gmail_scope},
                ),
            ):
                gmail_auth.authenticate()
        finally:
            gmail_auth.TOKEN_PATH = original_token
            gmail_auth.CREDENTIALS_PATH = original_creds

        captured = capsys.readouterr()
        assert "Scope checklist" in captured.out
        assert "[x]" in captured.out
        # Drive must not appear in output
        assert "drive" not in captured.out.lower(), (
            f"Drive should not appear in scope checklist output: {captured.out}"
        )
