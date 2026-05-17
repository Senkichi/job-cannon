r"""IMAP credentials smoke test for the wizard (STRANGE-WIZ-04, Phase 42).

Performs LOGIN → list_folders() → LOGOUT only. Does NOT call fetch() or search() —
fetching unseen messages would mark them \Seen and break the first scheduled ingest
(CONTEXT.md line 155).

SECURITY (T-42-01, T-42-02 mitigations):
- The app_password parameter MUST NEVER appear in any log line.
- The logger emits only `type(e).__name__` on failure — never repr(e) or vars(e).
- The returned ImapTestResult.message is a human-readable, credential-free string
  the wizard template renders verbatim. NEVER include the password in this string.
"""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass
from typing import Literal

from imapclient import IMAPClient
from imapclient.exceptions import LoginError

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 10

ErrorKind = Literal["auth", "host", "timeout", "other"] | None  # None on success


@dataclass(frozen=True)
class ImapTestResult:
    """Outcome of an IMAP smoke test.

    Attributes:
        ok: True if login + list_folders + logout completed.
        error_kind: Discriminator for the wizard template — one of "auth", "host",
            "timeout", "other", or None on success.
        message: Human-readable diagnostic the wizard renders verbatim. Per T-42-02
            (information disclosure) this string MUST NEVER contain the app password.
        folder_count: Number of folders returned by list_folders. None on failure.
    """

    ok: bool
    error_kind: ErrorKind
    message: str
    folder_count: int | None = None


def check_imap(
    host: str,
    port: int,
    email: str,
    app_password: str,
    timeout: int = _DEFAULT_TIMEOUT_SECONDS,
) -> ImapTestResult:
    """LOGIN → list_folders() → LOGOUT smoke test.

    D-09 exception → message mapping (success criterion 4):
        imapclient.exceptions.LoginError → "auth" → "Authentication failed — check your app password"
        socket.gaierror                  → "host" → f"Could not reach {host} — network issue?"
        socket.timeout                   → "timeout" → "Login timed out (10s)"
        other OSError                    → "other" → "IMAP connection failed: <type-name>"

    NEVER logs the app_password. NEVER includes the password in the returned message.
    """
    try:
        with IMAPClient(host, port=port, ssl=True, timeout=timeout) as client:
            client.login(email, app_password)
            folders = client.list_folders()
            # Per D-09: assert >= 1 folder returned
            if len(folders) < 1:
                logger.info("imap_test: login OK but list_folders returned 0 folders for %s", email)
                return ImapTestResult(
                    ok=False,
                    error_kind="other",
                    message="Login succeeded but the account returned no folders — unexpected for Gmail",
                    folder_count=0,
                )
            client.logout()
            logger.info("imap_test: success for %s — %d folders listed", email, len(folders))
            return ImapTestResult(
                ok=True,
                error_kind=None,
                message=f"Connected to {host} as {email} — {len(folders)} folders found",
                folder_count=len(folders),
            )
    except LoginError:
        # T-42-01: NEVER log the exception payload — it could contain the credential.
        logger.info("imap_test: LoginError for %s", email)
        return ImapTestResult(
            ok=False,
            error_kind="auth",
            message="Authentication failed — check your app password",
        )
    except socket.gaierror:
        logger.info("imap_test: gaierror for host=%s email=%s", host, email)
        return ImapTestResult(
            ok=False,
            error_kind="host",
            message=f"Could not reach {host} — network issue?",
        )
    except socket.timeout:
        logger.info("imap_test: timeout for host=%s email=%s after %ds", host, email, timeout)
        return ImapTestResult(
            ok=False,
            error_kind="timeout",
            message=f"Login timed out ({timeout}s)",
        )
    except (OSError, Exception) as e:
        # T-42-01: log only the exception class name, never repr(e) or vars(e).
        logger.info("imap_test: failure %s for host=%s email=%s", type(e).__name__, host, email)
        return ImapTestResult(
            ok=False,
            error_kind="other",
            message=f"IMAP connection failed: {type(e).__name__}",
        )
