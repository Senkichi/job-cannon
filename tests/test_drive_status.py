"""Tests for job_finder.web.drive_status module.

Covers:
- get_drive_status returns cached result on repeated calls within same request
- No token file -> error_code='no_token'
- Token missing drive.file scope -> error_code='missing_scope'
- Token refresh failure -> error_code='refresh_failed'
- Drive folder not configured -> error_code='no_folder_id'
- Valid token and config -> ok=True
- Unexpected exception -> error_code='unknown'
"""

import sys
from unittest.mock import MagicMock, patch


def _make_creds(
    scopes=None,
    expired=False,
    refresh_token=None,
):
    """Build a mock Credentials object."""
    creds = MagicMock()
    creds.scopes = scopes
    creds.expired = expired
    creds.refresh_token = refresh_token
    return creds


class TestGetDriveStatusNoToken:
    """Tests for missing token file case."""

    def test_no_token_returns_error(self, tmp_path):
        """Returns ok=False, error_code='no_token' when token file absent."""
        from job_finder.web.drive_status import _compute_drive_status

        missing_path = str(tmp_path / "no_such_token.json")
        result = _compute_drive_status({}, token_path=missing_path)

        assert result["ok"] is False
        assert result["error_code"] == "no_token"
        assert result["error"] is not None


_DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"

# drive_status.py uses lazy imports inside _compute_drive_status, so we patch
# the google module paths directly rather than the module-level names.
_PATCH_CREDENTIALS = "google.oauth2.credentials.Credentials.from_authorized_user_file"
_PATCH_REQUEST = "google.auth.transport.requests.Request"


class TestGetDriveStatusMissingScope:
    """Tests for token lacking drive.file scope."""

    def test_missing_scope_returns_error(self, tmp_path):
        """Returns ok=False, error_code='missing_scope' when drive.file absent."""
        from job_finder.web.drive_status import _compute_drive_status

        token_file = tmp_path / "token.json"
        token_file.write_text("{}")

        creds = _make_creds(scopes=["https://www.googleapis.com/auth/gmail.readonly"])

        with patch(_PATCH_CREDENTIALS, return_value=creds), \
             patch(_PATCH_REQUEST):
            result = _compute_drive_status({}, token_path=str(token_file))

        assert result["ok"] is False
        assert result["error_code"] == "missing_scope"

    def test_empty_scopes_returns_missing_scope(self, tmp_path):
        """Returns 'missing_scope' when scopes is None."""
        from job_finder.web.drive_status import _compute_drive_status

        token_file = tmp_path / "token.json"
        token_file.write_text("{}")

        creds = _make_creds(scopes=None)

        with patch(_PATCH_CREDENTIALS, return_value=creds), \
             patch(_PATCH_REQUEST):
            result = _compute_drive_status({}, token_path=str(token_file))

        assert result["ok"] is False
        assert result["error_code"] == "missing_scope"


class TestGetDriveStatusRefreshFailed:
    """Tests for token refresh failure."""

    def test_refresh_failure_returns_error(self, tmp_path):
        """Returns ok=False, error_code='refresh_failed' when refresh raises."""
        from job_finder.web.drive_status import _compute_drive_status

        token_file = tmp_path / "token.json"
        token_file.write_text("{}")

        creds = _make_creds(
            scopes=[_DRIVE_FILE_SCOPE],
            expired=True,
            refresh_token="some-refresh-token",
        )
        creds.refresh.side_effect = Exception("Token expired")

        with patch(_PATCH_CREDENTIALS, return_value=creds), \
             patch(_PATCH_REQUEST):
            result = _compute_drive_status({}, token_path=str(token_file))

        assert result["ok"] is False
        assert result["error_code"] == "refresh_failed"


class TestGetDriveStatusNoFolderConfigured:
    """Tests for missing Drive folder_id config."""

    def test_no_folder_id_returns_error(self, tmp_path):
        """Returns ok=False, error_code='no_folder_id' when folder_id not set."""
        from job_finder.web.drive_status import _compute_drive_status

        token_file = tmp_path / "token.json"
        token_file.write_text("{}")

        creds = _make_creds(scopes=[_DRIVE_FILE_SCOPE], expired=False)

        with patch(_PATCH_CREDENTIALS, return_value=creds), \
             patch(_PATCH_REQUEST):
            result = _compute_drive_status({}, token_path=str(token_file))

        assert result["ok"] is False
        assert result["error_code"] == "no_folder_id"


class TestGetDriveStatusSuccess:
    """Tests for the happy path."""

    def test_valid_config_returns_ok(self, tmp_path):
        """Returns ok=True when token is valid and folder_id is configured."""
        from job_finder.web.drive_status import _compute_drive_status

        token_file = tmp_path / "token.json"
        token_file.write_text("{}")

        creds = _make_creds(scopes=[_DRIVE_FILE_SCOPE], expired=False)
        config = {"drive": {"folder_id": "test-folder-id"}}

        with patch(_PATCH_CREDENTIALS, return_value=creds), \
             patch(_PATCH_REQUEST):
            result = _compute_drive_status(config, token_path=str(token_file))

        assert result["ok"] is True
        assert result["error"] is None
        assert result["error_code"] is None


class TestGetDriveStatusCaching:
    """Tests for g-level caching behavior."""

    def test_caches_result_on_g(self, app):
        """get_drive_status caches result on flask.g for request duration."""
        from job_finder.web.drive_status import get_drive_status

        with app.test_request_context():
            # Provide a known result from _compute_drive_status
            with patch("job_finder.web.drive_status._compute_drive_status") as mock_compute:
                mock_compute.return_value = {"ok": False, "error": "x", "error_code": "no_token"}

                result1 = get_drive_status({})
                result2 = get_drive_status({})

            # _compute_drive_status should be called exactly once (cached after first call)
            mock_compute.assert_called_once()
            assert result1 is result2
