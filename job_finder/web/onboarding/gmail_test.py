"""Gmail OAuth smoke test — parallel to `imap_test.check_imap` for F1.

Verifies that `token.json` exists, can be refreshed, includes the
`gmail.readonly` scope, and that a lightweight API call (users.getProfile)
succeeds. This is the auth-half of the inbox-wiring system check
documented in `.planning/no-key-compensation/FOLLOWUPS.md` (F1).

Mirrors the shape of `ImapTestResult` so the Settings tile can render
either result with the same template fragment.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

ErrorKind = Literal["no_token", "scope", "refresh", "api", "other"] | None  # None on success

_GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


@dataclass(frozen=True)
class GmailTestResult:
    """Outcome of a Gmail OAuth smoke test.

    Attributes:
        ok: True if token loaded, refreshed if needed, scope verified, and
            a getProfile() call succeeded.
        error_kind: Discriminator for the Settings template — one of
            "no_token", "scope", "refresh", "api", "other", or None.
        message: Human-readable diagnostic the template renders verbatim.
            Never contains the token bytes or refresh token.
        email_address: The Gmail account address (from getProfile), or None
            on failure.
    """

    ok: bool
    error_kind: ErrorKind
    message: str
    email_address: str | None = None


def check_oauth(token_path: str = "token.json") -> GmailTestResult:  # noqa: S107 - file path, not a password
    """Load credentials, refresh if needed, verify scope, run getProfile().

    Pure auth check — does NOT fetch any messages. The full `gmail_source.GmailSource`
    constructor also calls `users.getProfile`-equivalent via `build("gmail","v1")`,
    but here we want a fast, isolated smoke test that returns structured
    diagnostics instead of raising.

    Args:
        token_path: Path to the saved OAuth token JSON file.

    Returns:
        GmailTestResult with status + diagnostic message. Never raises.
    """
    if not Path(token_path).exists():
        return GmailTestResult(
            ok=False,
            error_kind="no_token",
            message=(
                f"Token file not found: {token_path}. Run "
                "`python -m job_finder.gmail_auth` to authenticate."
            ),
        )

    try:
        from job_finder.gmail_auth import AuthenticationError, _check_token_scopes, get_credentials
    except Exception as exc:  # pragma: no cover - import errors are environment bugs
        logger.info("gmail_test: import failure %s", type(exc).__name__)
        return GmailTestResult(
            ok=False,
            error_kind="other",
            message=f"Could not load gmail_auth module: {type(exc).__name__}",
        )

    granted = _check_token_scopes(token_path)
    if granted and _GMAIL_READONLY_SCOPE not in granted:
        logger.info("gmail_test: missing gmail.readonly scope in %s", token_path)
        return GmailTestResult(
            ok=False,
            error_kind="scope",
            message=(
                "Token is missing the gmail.readonly scope. Re-run "
                "`python -m job_finder.gmail_auth` to re-authenticate."
            ),
        )

    try:
        creds = get_credentials(token_path)
    except AuthenticationError as exc:
        logger.info("gmail_test: AuthenticationError")
        return GmailTestResult(
            ok=False,
            error_kind="refresh",
            message=str(exc),
        )
    except Exception as exc:
        logger.info("gmail_test: get_credentials failed %s", type(exc).__name__)
        return GmailTestResult(
            ok=False,
            error_kind="other",
            message=f"Could not load Gmail credentials: {type(exc).__name__}",
        )

    try:
        from googleapiclient.discovery import build

        service = build("gmail", "v1", credentials=creds)
        profile = service.users().getProfile(userId="me").execute()
        email = profile.get("emailAddress") or "(unknown)"
        logger.info("gmail_test: success for %s", email)
        return GmailTestResult(
            ok=True,
            error_kind=None,
            message=f"Authenticated as {email}",
            email_address=email,
        )
    except Exception as exc:
        logger.info("gmail_test: API call failed %s", type(exc).__name__)
        return GmailTestResult(
            ok=False,
            error_kind="api",
            message=f"Gmail API call failed: {type(exc).__name__}",
        )
