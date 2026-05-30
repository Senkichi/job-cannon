"""Live Gmail IMAP smoke for imap_test.py (STRANGE-WIZ-04, success criterion 4 LIVE row).

Skipped unless BOTH env vars are set:
  - JOB_CANNON_TEST_GMAIL_EMAIL    → real Gmail address
  - JOB_CANNON_TEST_GMAIL_APP_PW   → matching app password

Run locally with:
  JOB_CANNON_TEST_GMAIL_EMAIL=... JOB_CANNON_TEST_GMAIL_APP_PW=... uv run --active pytest tests/test_onboarding_imap_test_live.py -v

Per VALIDATION.md "Sampling Rate" → "Before /gsd-verify-work: ... manual live IMAP smoke test recorded".
"""

import os

import pytest

from job_finder.web.onboarding.imap_test import check_imap

_ENV_EMAIL = "JOB_CANNON_TEST_GMAIL_EMAIL"
_ENV_PW = "JOB_CANNON_TEST_GMAIL_APP_PW"


@pytest.mark.skipif(
    not os.environ.get(_ENV_EMAIL) or not os.environ.get(_ENV_PW),
    reason=f"{_ENV_EMAIL} and {_ENV_PW} not set — live Gmail smoke disabled",
)
def test_real_gmail_login_succeeds():
    """Real Gmail LOGIN → list_folders → LOGOUT. Asserts ≥1 folder."""
    email = os.environ[_ENV_EMAIL]
    app_pw = os.environ[_ENV_PW]

    result = check_imap(
        host="imap.gmail.com",
        port=993,
        email=email,
        app_password=app_pw,
        timeout=15,
    )

    assert result.ok is True, f"Expected ok=True but got: {result.error_kind}: {result.message}"
    assert result.error_kind is None
    assert result.folder_count is not None and result.folder_count >= 1


@pytest.mark.skipif(
    not os.environ.get(_ENV_EMAIL),
    reason=f"{_ENV_EMAIL} not set — live Gmail auth-failure smoke disabled",
)
def test_real_gmail_wrong_password_returns_auth_error():
    """Real Gmail login with a deliberately-wrong app password → error_kind='auth'.

    Only requires the email env var (not the password) — we supply a known-bad password.
    """
    email = os.environ[_ENV_EMAIL]

    result = check_imap(
        host="imap.gmail.com",
        port=993,
        email=email,
        app_password="zzzz zzzz zzzz zzzz",
        timeout=15,
    )

    assert result.ok is False
    assert result.error_kind == "auth"
    assert result.message == "Authentication failed — check your app password"
