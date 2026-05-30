"""Tests for the dashboard inbox banner helper.

Locks in the tightened banner rule (2026-05-22): banner fires on any
configured-source auth failure OR a 24h activity-window RED. Unconfigured
installs never banner.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import patch

from job_finder.web.blueprints.dashboard import _get_inbox_banner
from job_finder.web.onboarding.gmail_test import GmailTestResult
from job_finder.web.onboarding.imap_test import ImapTestResult
from job_finder.web.onboarding.inbox_check import InboxCheckResult

_BASE = InboxCheckResult(
    status="green",
    summary="ok",
    reason="ok",
    source_kind="gmail",
    gmail_auth=GmailTestResult(ok=True, error_kind=None, message="ok", email_address="u@x"),
    imap_auth=None,
    window_hours=24,
    emails_in_window=5,
    jobs_in_window=12,
)


def _result(**overrides) -> InboxCheckResult:
    """Frozen-dataclass override helper — preserves types Pyright can verify."""
    return dataclasses.replace(_BASE, **overrides)


def test_unconfigured_never_banners():
    fake = _result(status="unconfigured", source_kind="none", gmail_auth=None, imap_auth=None)
    with patch("job_finder.web.onboarding.inbox_check.run_inbox_check", return_value=fake):
        assert _get_inbox_banner({}, None) is None


def test_green_status_no_banner():
    fake = _result(status="green")
    with patch("job_finder.web.onboarding.inbox_check.run_inbox_check", return_value=fake):
        assert _get_inbox_banner({}, None) is None


def test_yellow_status_no_banner():
    """Yellow is informational, not banner-worthy — only RED or auth-fail banners."""
    fake = _result(
        status="yellow", summary="emails came in but 0 jobs", emails_in_window=3, jobs_in_window=0
    )
    with patch("job_finder.web.onboarding.inbox_check.run_inbox_check", return_value=fake):
        assert _get_inbox_banner({}, None) is None


def test_red_status_banners_with_status_summary():
    fake = _result(
        status="red", summary="No job alerts in the last 24 hours", reason="check senders"
    )
    with patch("job_finder.web.onboarding.inbox_check.run_inbox_check", return_value=fake):
        banner = _get_inbox_banner({}, None)
    assert banner is not None
    assert banner["summary"] == "No job alerts in the last 24 hours"
    assert banner["reason"] == "check senders"


def test_gmail_auth_failed_alone_banners_even_if_status_not_red():
    """status=green from a passing IMAP can still banner when Gmail auth is broken."""
    failed_gmail = GmailTestResult(ok=False, error_kind="no_token", message="token.json missing")
    ok_imap = ImapTestResult(ok=True, error_kind=None, message="logged in")
    fake = _result(
        status="green",
        source_kind="both",
        gmail_auth=failed_gmail,
        imap_auth=ok_imap,
        summary="all good per imap",
    )
    with patch("job_finder.web.onboarding.inbox_check.run_inbox_check", return_value=fake):
        banner = _get_inbox_banner({}, None)
    assert banner is not None
    assert banner["summary"] == "Gmail authentication failed"
    assert "token.json missing" in banner["reason"]


def test_imap_auth_failed_alone_banners():
    failed_imap = ImapTestResult(ok=False, error_kind="auth", message="bad app password")
    ok_gmail = GmailTestResult(ok=True, error_kind=None, message="ok", email_address="u@x")
    fake = _result(
        status="green",
        source_kind="both",
        gmail_auth=ok_gmail,
        imap_auth=failed_imap,
    )
    with patch("job_finder.web.onboarding.inbox_check.run_inbox_check", return_value=fake):
        banner = _get_inbox_banner({}, None)
    assert banner is not None
    assert banner["summary"] == "IMAP authentication failed"
    assert "bad app password" in banner["reason"]


def test_both_auth_failed_uses_status_red_summary():
    """When status is already RED (all-auth-fail path), use the run_inbox_check summary
    rather than the per-source synthesized one."""
    failed_gmail = GmailTestResult(ok=False, error_kind="no_token", message="token.json missing")
    failed_imap = ImapTestResult(ok=False, error_kind="auth", message="bad app password")
    fake = _result(
        status="red",
        source_kind="both",
        summary="Email source not reachable",
        reason="token.json missing",
        gmail_auth=failed_gmail,
        imap_auth=failed_imap,
    )
    with patch("job_finder.web.onboarding.inbox_check.run_inbox_check", return_value=fake):
        banner = _get_inbox_banner({}, None)
    assert banner is not None
    assert banner["summary"] == "Email source not reachable"


def test_uses_24h_window():
    """The banner helper must call run_inbox_check with window_hours=24 (tightened
    from the prior 168h). Locked in so future refactors don't quietly widen it."""
    captured: dict = {}

    def fake_check(_config, _conn, **kwargs):
        captured.update(kwargs)
        return _result(status="green")

    with patch("job_finder.web.onboarding.inbox_check.run_inbox_check", side_effect=fake_check):
        _get_inbox_banner({}, None)

    assert captured.get("window_hours") == 24


def test_exception_returns_none_silently():
    """Banner check must never crash the dashboard — exceptions degrade to no banner."""
    with patch(
        "job_finder.web.onboarding.inbox_check.run_inbox_check",
        side_effect=RuntimeError("boom"),
    ):
        assert _get_inbox_banner({}, None) is None
