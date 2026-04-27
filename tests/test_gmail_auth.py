"""Tests for enhanced gmail_auth.py OAuth flow with upgrade detection and validation.

Covers:
- _check_token_scopes: reads actual granted scopes from token.json
- authenticate: detects Gmail-only tokens and forces re-auth
- authenticate: prints scope checklist after successful auth
- _validate_drive_api: tests Drive API access and prints Console link on failure
- _ensure_drive_folder: auto-creates folder, saves folder_id to config.yaml
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

        # After refresh, make it valid
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
        # Token should be persisted
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
        with open(token_path, "w") as f:
            f.write('{"token": "fake"}')

        mock_creds = MagicMock()
        mock_creds.scopes = [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/drive.file",
        ]

        with patch(
            "job_finder.gmail_auth.Credentials.from_authorized_user_file",
            return_value=mock_creds,
        ):
            result = _check_token_scopes(token_path)

        assert result == {
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/drive.file",
        }

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
        with open(token_path, "w") as f:
            f.write("not valid json{")

        with patch(
            "job_finder.gmail_auth.Credentials.from_authorized_user_file",
            side_effect=Exception("invalid JSON"),
        ):
            result = _check_token_scopes(token_path)

        assert result == set()


class TestAuthenticateUpgradeDetection:
    """authenticate() detects Gmail-only tokens and forces re-auth."""

    def test_deletes_token_when_drive_scope_missing(self, tmp_path, capsys):
        """authenticate deletes token.json when Gmail-only token detected."""
        from job_finder import gmail_auth

        token_path = str(tmp_path / "token.json")
        creds_path = str(tmp_path / "credentials.json")
        with open(token_path, "w") as f:
            f.write('{"token": "fake"}')
        with open(creds_path, "w") as f:
            f.write('{"installed": {}}')

        mock_gmail_only_creds = MagicMock()
        mock_gmail_only_creds.scopes = ["https://www.googleapis.com/auth/gmail.readonly"]

        mock_new_creds = MagicMock()
        mock_new_creds.scopes = [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/drive.file",
        ]
        mock_new_creds.valid = True
        mock_new_creds.expired = False
        mock_new_creds.to_json.return_value = '{"token": "new"}'

        mock_flow = MagicMock()
        mock_flow.run_local_server.return_value = mock_new_creds

        original_token_path = gmail_auth.TOKEN_PATH
        original_creds_path = gmail_auth.CREDENTIALS_PATH
        try:
            gmail_auth.TOKEN_PATH = token_path
            gmail_auth.CREDENTIALS_PATH = creds_path

            with (
                patch.object(
                    gmail_auth.Credentials,
                    "from_authorized_user_file",
                    return_value=mock_gmail_only_creds,
                ),
                patch(
                    "google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file",
                    return_value=mock_flow,
                ),
                patch(
                    "job_finder.gmail_auth._validate_drive_api",
                ),
                patch(
                    "job_finder.gmail_auth._ensure_drive_folder",
                ),
            ):
                gmail_auth.authenticate()

            # Token should have been deleted and re-created
            assert Path(token_path).exists(), "Token should be re-created after re-auth"
        finally:
            gmail_auth.TOKEN_PATH = original_token_path
            gmail_auth.CREDENTIALS_PATH = original_creds_path

    def test_prints_upgrading_message_when_gmail_only_token(self, tmp_path, capsys):
        """authenticate prints 'Upgrading to include Drive scope' for Gmail-only token."""
        from job_finder import gmail_auth

        token_path = str(tmp_path / "token.json")
        creds_path = str(tmp_path / "credentials.json")
        with open(token_path, "w") as f:
            f.write('{"token": "fake"}')
        with open(creds_path, "w") as f:
            f.write('{"installed": {}}')

        mock_gmail_only_creds = MagicMock()
        mock_gmail_only_creds.scopes = ["https://www.googleapis.com/auth/gmail.readonly"]

        mock_new_creds = MagicMock()
        mock_new_creds.scopes = [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/drive.file",
        ]
        mock_new_creds.valid = True
        mock_new_creds.expired = False
        mock_new_creds.to_json.return_value = '{"token": "new"}'

        mock_flow = MagicMock()
        mock_flow.run_local_server.return_value = mock_new_creds

        original_token_path = gmail_auth.TOKEN_PATH
        original_creds_path = gmail_auth.CREDENTIALS_PATH
        try:
            gmail_auth.TOKEN_PATH = token_path
            gmail_auth.CREDENTIALS_PATH = creds_path

            with (
                patch.object(
                    gmail_auth.Credentials,
                    "from_authorized_user_file",
                    return_value=mock_gmail_only_creds,
                ),
                patch(
                    "google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file",
                    return_value=mock_flow,
                ),
                patch(
                    "job_finder.gmail_auth._validate_drive_api",
                ),
                patch(
                    "job_finder.gmail_auth._ensure_drive_folder",
                ),
            ):
                gmail_auth.authenticate()
        finally:
            gmail_auth.TOKEN_PATH = original_token_path
            gmail_auth.CREDENTIALS_PATH = original_creds_path

        captured = capsys.readouterr()
        assert "Upgrading" in captured.out or "upgrade" in captured.out.lower(), (
            f"Should print upgrade message, got: {captured.out}"
        )


class TestAuthenticateScopeChecklist:
    """authenticate() prints scope checklist after successful auth."""

    def test_prints_scope_checklist_after_auth(self, tmp_path, capsys):
        """authenticate prints scope checklist with checkmarks after successful auth."""
        from job_finder import gmail_auth

        token_path = str(tmp_path / "token.json")
        creds_path = str(tmp_path / "credentials.json")
        with open(creds_path, "w") as f:
            f.write('{"installed": {}}')

        mock_new_creds = MagicMock()
        mock_new_creds.scopes = [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/drive.file",
        ]
        mock_new_creds.valid = True
        mock_new_creds.expired = False
        mock_new_creds.to_json.return_value = '{"token": "new"}'

        mock_flow = MagicMock()
        mock_flow.run_local_server.return_value = mock_new_creds

        original_token_path = gmail_auth.TOKEN_PATH
        original_creds_path = gmail_auth.CREDENTIALS_PATH
        try:
            gmail_auth.TOKEN_PATH = token_path
            gmail_auth.CREDENTIALS_PATH = creds_path

            with (
                patch(
                    "google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file",
                    return_value=mock_flow,
                ),
                patch(
                    "job_finder.gmail_auth._validate_drive_api",
                ),
                patch(
                    "job_finder.gmail_auth._ensure_drive_folder",
                ),
            ):
                gmail_auth.authenticate()
        finally:
            gmail_auth.TOKEN_PATH = original_token_path
            gmail_auth.CREDENTIALS_PATH = original_creds_path

        captured = capsys.readouterr()
        assert "Scope checklist" in captured.out or "checklist" in captured.out.lower(), (
            f"Should print scope checklist, got: {captured.out}"
        )
        # Should show checkmarks for both scopes
        assert "[x]" in captured.out or "✓" in captured.out or "check" in captured.out.lower(), (
            f"Should show checkmarks, got: {captured.out}"
        )


class TestValidateDriveApi:
    """_validate_drive_api tests Drive API access."""

    def test_prints_success_on_successful_call(self, capsys):
        """_validate_drive_api prints success message when Drive API responds."""
        from job_finder.gmail_auth import _validate_drive_api

        mock_creds = MagicMock()
        mock_service = MagicMock()
        mock_service.files.return_value.list.return_value.execute.return_value = {"files": []}

        with patch("job_finder.gmail_auth.build", return_value=mock_service):
            _validate_drive_api(mock_creds)

        captured = capsys.readouterr()
        assert "verified" in captured.out.lower() or "success" in captured.out.lower(), (
            f"Should print success message, got: {captured.out}"
        )

    def test_prints_console_link_on_403(self, capsys):
        """_validate_drive_api prints Console link when HttpError 403 received."""
        from googleapiclient.errors import HttpError

        from job_finder.gmail_auth import _validate_drive_api

        mock_creds = MagicMock()
        mock_service = MagicMock()

        # Simulate 403 HttpError
        mock_resp = MagicMock()
        mock_resp.status = 403
        mock_service.files.return_value.list.return_value.execute.side_effect = HttpError(
            resp=mock_resp, content=b'{"error": {"code": 403}}'
        )

        with patch("job_finder.gmail_auth.build", return_value=mock_service):
            _validate_drive_api(mock_creds)

        captured = capsys.readouterr()
        assert "console.cloud.google.com" in captured.out, (
            f"Should print Console link on 403, got: {captured.out}"
        )


class TestEnsureDriveFolder:
    """_ensure_drive_folder auto-creates folder and saves folder_id to config.yaml."""

    def test_skips_when_folder_id_already_configured(self, tmp_path, capsys):
        """_ensure_drive_folder skips folder creation when folder_id is set."""
        from job_finder.gmail_auth import _ensure_drive_folder

        config_path = str(tmp_path / "config.yaml")
        with open(config_path, "w") as f:
            f.write("drive:\n  folder_id: existing-folder-id\n")

        mock_creds = MagicMock()

        _ensure_drive_folder(mock_creds, config_path=config_path)

        captured = capsys.readouterr()
        assert "already" in captured.out.lower() or "configured" in captured.out.lower(), (
            f"Should print 'already configured' message, got: {captured.out}"
        )

    def test_creates_folder_and_saves_folder_id_to_config(self, tmp_path):
        """_ensure_drive_folder creates Drive folder and saves folder_id to config.yaml."""
        from job_finder.gmail_auth import _ensure_drive_folder

        config_path = str(tmp_path / "config.yaml")
        with open(config_path, "w") as f:
            f.write('drive:\n  folder_id: ""\n')

        mock_creds = MagicMock()
        mock_service = MagicMock()
        mock_service.files.return_value.create.return_value.execute.return_value = {
            "id": "new-folder-id-abc"
        }

        with patch("job_finder.gmail_auth.build", return_value=mock_service):
            _ensure_drive_folder(mock_creds, config_path=config_path)

        # Verify folder_id was saved to config.yaml
        import yaml

        with open(config_path) as f:
            saved_config = yaml.safe_load(f)

        assert saved_config["drive"]["folder_id"] == "new-folder-id-abc", (
            f"folder_id should be saved to config.yaml, got: {saved_config}"
        )

    def test_handles_missing_config_yaml_gracefully(self, tmp_path, capsys):
        """_ensure_drive_folder handles missing config.yaml gracefully."""
        from job_finder.gmail_auth import _ensure_drive_folder

        config_path = str(tmp_path / "no_config.yaml")  # does not exist

        mock_creds = MagicMock()
        mock_service = MagicMock()
        mock_service.files.return_value.create.return_value.execute.return_value = {
            "id": "new-folder-id-xyz"
        }

        with patch("job_finder.gmail_auth.build", return_value=mock_service):
            # Should NOT raise -- graceful degradation
            _ensure_drive_folder(mock_creds, config_path=config_path)

        captured = capsys.readouterr()
        # Should print the folder_id so user can add it manually
        assert "new-folder-id-xyz" in captured.out, (
            f"Should print folder_id for manual config, got: {captured.out}"
        )
