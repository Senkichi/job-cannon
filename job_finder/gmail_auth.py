"""Gmail + Google Drive OAuth authentication flow.

Run this once to authenticate with Gmail and Google Drive APIs:
    python -m job_finder.gmail_auth

Prerequisites:
1. Go to https://console.cloud.google.com/
2. Create a project, enable the Gmail API and Google Drive API
3. Create OAuth 2.0 credentials (Desktop App type)
4. Download credentials.json into the project root

This will open a browser for you to sign in, then save token.json locally.

Authentication features:
- Detects Gmail-only tokens and automatically upgrades them to include Drive scope
- Validates Drive API access with a test call after authentication
- Prints a scope checklist showing which permissions were granted
- Auto-creates the Drive folder and saves folder_id to config.yaml

Upgrading from Gmail-only token:
    If token.json only has Gmail scope, this script detects it and forces
    re-auth with the Drive scope included. The old token is deleted first.
"""

import logging
from pathlib import Path

import yaml
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.file",
]
CREDENTIALS_PATH = "credentials.json"
TOKEN_PATH = "token.json"

_GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
_DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
_DRIVE_CONSOLE_URL = "https://console.cloud.google.com/apis/library/drive.googleapis.com"

logger = logging.getLogger(__name__)


def _check_token_scopes(token_path: str) -> set:
    """Read the actual granted scopes from token.json without masking.

    Loads credentials with scopes=None to read the actual scopes recorded
    in the token file, not the scopes we would request.

    Args:
        token_path: Path to the saved OAuth token JSON file.

    Returns:
        Set of scope strings granted in the token. Returns empty set if the
        token file is missing, malformed, or any other error occurs.
    """
    try:
        if not Path(token_path).exists():
            return set()
        creds = Credentials.from_authorized_user_file(token_path, scopes=None)
        return set(creds.scopes or [])
    except Exception as e:
        logger.debug("get_granted_scopes failed: %s", e)
        return set()


def _validate_drive_api(creds) -> None:
    """Test Drive API access with a lightweight files.list call.

    Prints a success message on OK. Prints the Console link on HttpError 403
    (Drive API not enabled).

    Args:
        creds: Authenticated Google OAuth2 credentials.
    """
    try:
        service = build("drive", "v3", credentials=creds)
        service.files().list(pageSize=1, fields="files(id)").execute()
        print("Drive API access verified.")
    except HttpError as exc:
        print(f"Drive API error: {exc}")
        print(f"Enable the Drive API at: {_DRIVE_CONSOLE_URL}")
    except Exception as exc:
        print(f"Could not verify Drive API access: {exc}")


def _ensure_drive_folder(creds, config_path: str = "config.yaml") -> None:
    """Ensure a Drive folder exists and folder_id is saved to config.yaml.

    Reads config.yaml to check if folder_id is already configured. If not,
    creates a new folder named 'Job Finder Resumes' and saves the folder_id
    back to config.yaml.

    Args:
        creds: Authenticated Google OAuth2 credentials.
        config_path: Path to config.yaml.
    """
    # Load config to check for existing folder_id
    try:
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}
    except FileNotFoundError:
        config = None

    # Check if folder is already configured
    if config is not None:
        folder_id = config.get("drive", {}).get("folder_id", "")
        if folder_id:
            print("Drive folder already configured.")
            return

    # Create the folder in Drive
    service = build("drive", "v3", credentials=creds)
    folder_metadata = {
        "name": "Job Finder Resumes",
        "mimeType": "application/vnd.google-apps.folder",
    }
    folder = service.files().create(body=folder_metadata, fields="id").execute()
    folder_id = folder.get("id", "")
    print(f"Created Drive folder: Job Finder Resumes (id: {folder_id})")

    if config is None:
        # config.yaml not found — instruct user to add manually
        print(f"Could not find {config_path}. Add this to your config.yaml manually:")
        print("  drive:")
        print(f'    folder_id: "{folder_id}"')
        return

    # Save folder_id back to config.yaml
    if "drive" not in config:
        config["drive"] = {}
    config["drive"]["folder_id"] = folder_id

    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True)
    print(f"Saved folder_id to {config_path}")


class AuthenticationError(Exception):
    """Raised when Google OAuth credentials are unavailable or expired."""


def get_credentials(token_path: str = TOKEN_PATH) -> Credentials:
    """Load and refresh Google OAuth credentials.

    Non-interactive — suitable for background services. Raises
    AuthenticationError if the token is missing, revoked, or
    cannot be refreshed.
    """
    if not Path(token_path).exists():
        raise AuthenticationError(
            f"Token file not found: {token_path}. Run: python -m job_finder.gmail_auth"
        )

    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds.valid:
        return creds

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            # Persist refreshed token so next caller doesn't re-refresh
            Path(token_path).write_text(creds.to_json())
            return creds
        except Exception as exc:
            raise AuthenticationError(
                f"Token refresh failed: {exc}. Run: python -m job_finder.gmail_auth"
            ) from exc

    raise AuthenticationError(
        "Token is invalid and cannot be refreshed. Run: python -m job_finder.gmail_auth"
    )


def authenticate():
    """Run the OAuth flow for Gmail + Google Drive APIs and save the token.

    Detects Gmail-only tokens and forces re-auth with Drive scope.
    After authentication, prints a scope checklist, validates Drive API
    access, and auto-creates the Drive folder if needed.
    """
    creds = None

    # Upgrade detection: check existing token for Drive scope
    if Path(TOKEN_PATH).exists():
        existing_scopes = _check_token_scopes(TOKEN_PATH)
        if existing_scopes and _DRIVE_FILE_SCOPE not in existing_scopes:
            print("Upgrading to include Drive scope...")
            print("Deleting old Gmail-only token and requesting new credentials.")
            Path(TOKEN_PATH).unlink()
            creds = None
        elif existing_scopes:
            # Drive scope already present — load normally
            creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    # Refresh or re-authenticate
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("Refreshing expired token...")
            try:
                creds.refresh(Request())
            except Exception as exc:
                print(f"Token refresh failed ({exc}) — re-authenticating via browser...")
                Path(TOKEN_PATH).unlink(missing_ok=True)
                creds = None

        if not creds:
            if not Path(CREDENTIALS_PATH).exists():
                print(f"Error: {CREDENTIALS_PATH} not found!")
                print("Download it from Google Cloud Console:")
                print("  1. Go to https://console.cloud.google.com/apis/credentials")
                print("  2. Create OAuth 2.0 Client ID (Desktop App)")
                print(f"  3. Download and save as {CREDENTIALS_PATH}")
                return

            print("Opening browser for authentication...")
            from google_auth_oauthlib.flow import InstalledAppFlow

            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)

        # Save token
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
        print(f"Token saved to {TOKEN_PATH}")

    # Print scope checklist
    granted_scopes = _check_token_scopes(TOKEN_PATH)
    gmail_check = "[x]" if _GMAIL_SCOPE in granted_scopes else "[ ]"
    drive_check = "[x]" if _DRIVE_FILE_SCOPE in granted_scopes else "[ ]"
    print("Scope checklist:")
    print(f"  {gmail_check} Gmail read access")
    print(f"  {drive_check} Google Drive file access")

    print("Authentication successful!")
    print(f"Token valid: {creds.valid}")

    # Validate Drive API access
    _validate_drive_api(creds)

    # Ensure Drive folder is configured
    _ensure_drive_folder(creds)


if __name__ == "__main__":
    authenticate()
