"""Google Drive upload utility for resume generation.

Provides authenticated Drive service building and file upload with optional
conversion to Google Docs format.

Shares SCOPES with gmail_auth.py -- both modules use token.json as the
credential store. If token.json lacks the drive.file scope, get_drive_service
raises a clear ValueError instructing the user to re-authenticate.

Usage:
    from job_finder.web.drive_uploader import get_drive_service, upload_to_drive

    service = get_drive_service()  # loads token.json, validates drive.file scope
    url = upload_to_drive(service, "My Resume - Acme Corp", buffer, folder_id="...")
"""

import io

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

# Combined OAuth scopes -- must match SCOPES in job_finder/gmail_auth.py.
# Defined here directly to avoid importing the full gmail_auth module (which
# triggers google_auth_oauthlib at import time, an unnecessary dependency).
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.file",
]

_DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_GDOC_MIME = "application/vnd.google-apps.document"


def get_drive_service(token_path: str = "token.json"):
    """Build and return an authenticated Google Drive v3 service.

    Loads credentials from token_path using scopes=None for honest scope
    detection -- the actual granted scopes are read from the token file itself,
    not masked by the scopes parameter. Validates that the drive.file scope is
    present. Refreshes an expired token automatically.

    Args:
        token_path: Path to the saved OAuth token JSON file.

    Returns:
        Authenticated Google Drive v3 service resource.

    Raises:
        FileNotFoundError: If token_path does not exist.
        ValueError: If token lacks the drive.file scope, or if token refresh fails.
    """
    from pathlib import Path

    if not Path(token_path).exists():
        raise FileNotFoundError(
            f"Token file not found: '{token_path}'. "
            "Run: python -m job_finder.gmail_auth"
        )

    # Load with scopes=None to read the actual granted scopes from token.json
    # (not masked by the scopes parameter)
    creds = Credentials.from_authorized_user_file(token_path, scopes=None)

    # Validate that drive.file scope was actually granted during auth
    if not creds.scopes or _DRIVE_FILE_SCOPE not in creds.scopes:
        raise ValueError(
            f"Token at '{token_path}' lacks the required drive.file scope. "
            "Run: python -m job_finder.gmail_auth"
        )

    # Refresh if expired
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:
            raise ValueError(
                f"Failed to refresh token: {exc}. "
                "Run: python -m job_finder.gmail_auth"
            ) from exc

    return build("drive", "v3", credentials=creds)


def upload_to_drive(
    service,
    doc_name: str,
    buffer: io.BytesIO,
    folder_id: str,
    convert_to_gdoc: bool = True,
) -> str:
    """Upload a .docx BytesIO buffer to Google Drive.

    Args:
        service: Authenticated Drive v3 service resource.
        doc_name: Display name for the file (without extension for Google Docs,
                  .docx suffix appended automatically when convert_to_gdoc=False).
        buffer: BytesIO containing the .docx file content.
        folder_id: Google Drive folder ID to upload into.
        convert_to_gdoc: If True, convert to Google Docs format. If False,
                         store as .docx binary.

    Returns:
        webViewLink URL for the created file. Falls back to a constructed URL
        using the file ID if the API does not return webViewLink.

    Raises:
        HttpError: If the Drive API returns an error (e.g. 403 quota exceeded,
                   404 folder not found, 401 unauthorized).
    """
    buffer.seek(0)

    file_metadata: dict = {
        "name": doc_name,
        "parents": [folder_id],
    }

    if convert_to_gdoc:
        file_metadata["mimeType"] = _GDOC_MIME
    else:
        file_metadata["name"] = doc_name + ".docx"

    media = MediaIoBaseUpload(buffer, mimetype=_DOCX_MIME, resumable=False)

    try:
        created = (
            service.files()
            .create(body=file_metadata, media_body=media, fields="id,webViewLink")
            .execute()
        )
    except HttpError:
        raise

    web_view_link = created.get("webViewLink")
    if web_view_link:
        return web_view_link

    # Fallback: construct URL from file ID
    file_id = created.get("id", "")
    return f"https://drive.google.com/file/d/{file_id}/view"
