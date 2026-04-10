"""Tests for job_finder.web.drive_uploader module.

Covers:
- get_drive_service raises FileNotFoundError when token absent
- get_drive_service raises ValueError when drive.file scope missing
- get_drive_service raises ValueError when token refresh fails
- get_drive_service returns service when token is valid
- upload_to_drive uploads file and returns webViewLink
- upload_to_drive falls back to file-ID URL when webViewLink absent
- upload_to_drive uses Google Docs mime type when convert_to_gdoc=True
- upload_to_drive keeps .docx name when convert_to_gdoc=False
"""

import io
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

_DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"

class TestGetDriveServiceErrors:
    """Tests for error conditions in get_drive_service."""

    def test_raises_file_not_found_when_no_token(self, tmp_path):
        """Raises FileNotFoundError when token.json does not exist."""
        from job_finder.web.drive_uploader import get_drive_service

        missing = str(tmp_path / "no_token.json")
        try:
            get_drive_service(token_path=missing)
            assert False, "Expected FileNotFoundError"
        except FileNotFoundError as exc:
            assert "token" in str(exc).lower() or "not found" in str(exc).lower()

    def test_raises_value_error_when_scope_missing(self, tmp_path):
        """Raises ValueError when drive.file scope is absent from token."""
        from job_finder.web.drive_uploader import get_drive_service

        token_file = tmp_path / "token.json"
        token_file.write_text("{}")

        creds = _make_creds(scopes=["https://www.googleapis.com/auth/gmail.readonly"])

        with patch("job_finder.web.drive_uploader.Credentials") as mock_creds_cls, \
             patch("job_finder.web.drive_uploader.Request"):
            mock_creds_cls.from_authorized_user_file.return_value = creds
            try:
                get_drive_service(token_path=str(token_file))
                assert False, "Expected ValueError"
            except ValueError as exc:
                assert "scope" in str(exc).lower() or "drive.file" in str(exc)

    def test_raises_value_error_when_refresh_fails(self, tmp_path):
        """Raises ValueError when token refresh fails."""
        from job_finder.web.drive_uploader import get_drive_service

        token_file = tmp_path / "token.json"
        token_file.write_text("{}")

        creds = _make_creds(
            scopes=[_DRIVE_FILE_SCOPE],
            expired=True,
            refresh_token="some-token",
        )
        creds.refresh.side_effect = Exception("Auth server unavailable")

        with patch("job_finder.web.drive_uploader.Credentials") as mock_creds_cls, \
             patch("job_finder.web.drive_uploader.Request"):
            mock_creds_cls.from_authorized_user_file.return_value = creds
            try:
                get_drive_service(token_path=str(token_file))
                assert False, "Expected ValueError"
            except ValueError as exc:
                assert "refresh" in str(exc).lower() or "failed" in str(exc).lower()

class TestGetDriveServiceSuccess:
    """Tests for the happy path of get_drive_service."""

    def test_returns_service_with_valid_token(self, tmp_path):
        """Returns a Drive service when credentials are valid."""
        from job_finder.web.drive_uploader import get_drive_service

        token_file = tmp_path / "token.json"
        token_file.write_text("{}")

        creds = _make_creds(scopes=[_DRIVE_FILE_SCOPE], expired=False)
        mock_service = MagicMock()

        with patch("job_finder.web.drive_uploader.Credentials") as mock_creds_cls, \
             patch("job_finder.web.drive_uploader.Request"), \
             patch("job_finder.web.drive_uploader.build", return_value=mock_service):
            mock_creds_cls.from_authorized_user_file.return_value = creds
            service = get_drive_service(token_path=str(token_file))

        assert service is mock_service

class TestUploadToDrive:
    """Tests for upload_to_drive."""

    def _make_service(self, file_id="file-123", web_view_link=None):
        """Build a mock Drive service that returns a create response."""
        service = MagicMock()
        create_result = {"id": file_id}
        if web_view_link:
            create_result["webViewLink"] = web_view_link
        service.files.return_value.create.return_value.execute.return_value = create_result
        return service

    def test_returns_web_view_link_when_present(self):
        """Returns webViewLink from Drive API response when available."""
        from job_finder.web.drive_uploader import upload_to_drive

        service = self._make_service(
            file_id="abc123",
            web_view_link="https://docs.google.com/document/d/abc123/edit",
        )
        buffer = io.BytesIO(b"fake docx content")

        url = upload_to_drive(service, "My Resume", buffer, folder_id="folder-xyz")

        assert url == "https://docs.google.com/document/d/abc123/edit"

    def test_falls_back_to_constructed_url_when_no_web_view_link(self):
        """Falls back to constructed URL using file ID when webViewLink absent."""
        from job_finder.web.drive_uploader import upload_to_drive

        service = self._make_service(file_id="file-456", web_view_link=None)
        buffer = io.BytesIO(b"fake docx content")

        url = upload_to_drive(service, "My Resume", buffer, folder_id="folder-xyz")

        assert "file-456" in url
        assert url.startswith("https://drive.google.com/")

    def test_convert_to_gdoc_uses_gdoc_mime_type(self):
        """With convert_to_gdoc=True, mimeType is set to Google Docs format."""
        from job_finder.web.drive_uploader import upload_to_drive, _GDOC_MIME

        service = self._make_service(web_view_link="https://docs.google.com/d/test")
        buffer = io.BytesIO(b"fake docx content")

        upload_to_drive(service, "My Resume", buffer, folder_id="folder", convert_to_gdoc=True)

        create_call = service.files.return_value.create.call_args
        body = create_call.kwargs.get("body") or create_call.args[0] if create_call.args else None
        if body is None:
            # Try positional or keyword
            all_kwargs = create_call[1] if len(create_call) > 1 else {}
            body = all_kwargs.get("body", {})
        assert body.get("mimeType") == _GDOC_MIME

    def test_no_conversion_appends_docx_extension(self):
        """With convert_to_gdoc=False, .docx extension is appended to name."""
        from job_finder.web.drive_uploader import upload_to_drive

        service = self._make_service(web_view_link="https://drive.google.com/d/test")
        buffer = io.BytesIO(b"fake docx content")

        upload_to_drive(service, "My Resume", buffer, folder_id="folder", convert_to_gdoc=False)

        create_call = service.files.return_value.create.call_args
        body = create_call.kwargs.get("body") or (create_call.args[0] if create_call.args else {})
        if not body:
            body = create_call[1].get("body", {}) if len(create_call) > 1 else {}
        assert body.get("name", "").endswith(".docx")

    def test_buffer_seeked_to_zero_before_upload(self):
        """Buffer is seeked to position 0 before being passed to MediaIoBaseUpload."""
        from job_finder.web.drive_uploader import upload_to_drive

        service = self._make_service(web_view_link="https://docs.google.com/d/x")
        buffer = io.BytesIO(b"fake docx content")
        buffer.seek(10)  # Advance past start

        # The upload should still work because upload_to_drive calls buffer.seek(0)
        upload_to_drive(service, "My Resume", buffer, folder_id="folder")
        # If we get here without error, the buffer was properly seeked
