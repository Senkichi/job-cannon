"""Gmail OAuth authentication flow.

Run this once to authenticate with the Gmail API:
    python -m job_finder.gmail_auth

Prerequisites:
1. Go to https://console.cloud.google.com/
2. Create a project, enable the Gmail API
3. Create OAuth 2.0 credentials (Desktop App type)
4. Download credentials.json into your user-data directory

This will open a browser for you to sign in, then save token.json to your
user-data directory (see job_finder.web.user_data_dirs.token_path).

Authentication features:
- Non-interactive get_credentials() for background services
- Prints a scope checklist showing which permissions were granted
"""

import logging
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from job_finder.web.user_data_dirs import credentials_path, token_path

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
]

_GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

logger = logging.getLogger(__name__)

# Resolved at import time so callers and tests can reference the canonical paths.
# Tests override JOB_CANNON_USER_DATA_DIR to redirect these to tmp_path.
TOKEN_PATH: str = str(token_path())
CREDENTIALS_PATH: str = str(credentials_path())


def _check_token_scopes(token_path_str: str) -> set:
    """Read the actual granted scopes from token.json without masking.

    Loads credentials with scopes=None to read the actual scopes recorded
    in the token file, not the scopes we would request.

    Args:
        token_path_str: Path to the saved OAuth token JSON file.

    Returns:
        Set of scope strings granted in the token. Returns empty set if the
        token file is missing, malformed, or any other error occurs.
    """
    try:
        if not Path(token_path_str).exists():
            return set()
        creds = Credentials.from_authorized_user_file(token_path_str, scopes=None)
        return set(creds.scopes or [])
    except Exception as e:
        logger.debug("get_granted_scopes failed: %s", e)
        return set()


class AuthenticationError(Exception):
    """Raised when Google OAuth credentials are unavailable or expired."""


def get_credentials(token_path_str: str = TOKEN_PATH) -> Credentials:
    """Load and refresh Google OAuth credentials.

    Non-interactive — suitable for background services. Raises
    AuthenticationError if the token is missing, revoked, or
    cannot be refreshed.
    """
    if not Path(token_path_str).exists():
        raise AuthenticationError(
            f"Token file not found: {token_path_str}. Run: python -m job_finder.gmail_auth"
        )

    creds = Credentials.from_authorized_user_file(str(token_path_str), SCOPES)

    if creds.valid:
        return creds

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            # Persist refreshed token so next caller doesn't re-refresh
            Path(token_path_str).write_text(creds.to_json())
            return creds
        except Exception as exc:
            raise AuthenticationError(
                f"Token refresh failed: {exc}. Run: python -m job_finder.gmail_auth"
            ) from exc

    raise AuthenticationError(
        "Token is invalid and cannot be refreshed. Run: python -m job_finder.gmail_auth"
    )


def authenticate() -> None:
    """Run the OAuth flow for the Gmail API and save the token.

    After authentication, prints a scope checklist.
    """
    creds = None

    # Load existing token if present
    if Path(TOKEN_PATH).exists():
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
        Path(TOKEN_PATH).parent.mkdir(parents=True, exist_ok=True)
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
        print(f"Token saved to {TOKEN_PATH}")

    # Print scope checklist
    granted_scopes = _check_token_scopes(TOKEN_PATH)
    gmail_check = "[x]" if _GMAIL_SCOPE in granted_scopes else "[ ]"
    print("Scope checklist:")
    print(f"  {gmail_check} Gmail read access")

    print("Authentication successful!")
    print(f"Token valid: {creds.valid}")


if __name__ == "__main__":
    authenticate()
