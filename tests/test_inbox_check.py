"""Unit tests for job_finder.web.onboarding.inbox_check (F1)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from job_finder.web.onboarding.gmail_test import GmailTestResult
from job_finder.web.onboarding.imap_test import ImapTestResult
from job_finder.web.onboarding.inbox_check import (
    InboxCheckResult,
    run_inbox_check,
)


@pytest.fixture
def conn():
    """In-memory SQLite with the email_parse_log schema from m001."""
    c = sqlite3.connect(":memory:")
    c.execute(
        """CREATE TABLE email_parse_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL UNIQUE,
            sender TEXT NOT NULL,
            processed_at TEXT NOT NULL,
            jobs_found INTEGER DEFAULT 0,
            error TEXT DEFAULT NULL
        )"""
    )
    yield c
    c.close()


def _insert_row(c, message_id, sender, processed_at, jobs_found=0, error=None):
    c.execute(
        "INSERT INTO email_parse_log (message_id, sender, processed_at, jobs_found, error)"
        " VALUES (?, ?, ?, ?, ?)",
        (message_id, sender, processed_at, jobs_found, error),
    )
    c.commit()


def test_unconfigured_when_neither_gmail_nor_imap_enabled(conn):
    config = {"sources": {"gmail": {"enabled": False}, "imap": {"enabled": False}}}
    result = run_inbox_check(config, conn)
    assert result.status == "unconfigured"
    assert result.source_kind == "none"


def test_red_when_gmail_auth_fails(conn):
    config = {"sources": {"gmail": {"enabled": True}}}
    fake = GmailTestResult(
        ok=False, error_kind="no_token", message="Token file not found: token.json"
    )

    with patch("job_finder.web.onboarding.inbox_check.check_oauth", return_value=fake):
        result = run_inbox_check(config, conn)

    assert result.status == "red"
    assert result.source_kind == "gmail"
    assert "Token file not found" in result.reason


def test_red_when_auth_ok_but_no_emails(conn):
    """Auth passes, email_parse_log empty in window → RED with 'No job alerts' summary."""
    config = {"sources": {"gmail": {"enabled": True}}}
    fake = GmailTestResult(
        ok=True, error_kind=None, message="Authenticated", email_address="u@x.com"
    )

    with patch("job_finder.web.onboarding.inbox_check.check_oauth", return_value=fake):
        result = run_inbox_check(config, conn, window_hours=72)

    assert result.status == "red"
    assert "No job alerts" in result.summary
    assert result.emails_in_window == 0


def test_yellow_when_emails_arrived_but_zero_jobs(conn):
    """Emails arrived but every row has jobs_found=0 → YELLOW."""
    now = datetime(2026, 5, 22, 12, 0, 0)
    recent = (now - timedelta(hours=2)).isoformat()
    _insert_row(conn, "m1", "gmail", recent, jobs_found=0, error="parse_failure")
    _insert_row(conn, "m2", "jobalerts-noreply@linkedin.com", recent, jobs_found=0)

    config = {"sources": {"gmail": {"enabled": True}}}
    fake = GmailTestResult(
        ok=True, error_kind=None, message="Authenticated", email_address="u@x.com"
    )

    with patch("job_finder.web.onboarding.inbox_check.check_oauth", return_value=fake):
        result = run_inbox_check(config, conn, window_hours=72, now=now)

    assert result.status == "yellow"
    assert result.emails_in_window == 2
    assert result.jobs_in_window == 0
    assert "produced 0 jobs" in result.summary


def test_green_when_auth_ok_and_jobs_in_window(conn):
    """Auth + ≥1 job-bearing row in window → GREEN."""
    now = datetime(2026, 5, 22, 12, 0, 0)
    recent = (now - timedelta(hours=10)).isoformat()
    _insert_row(conn, "m1", "gmail", recent, jobs_found=12)

    config = {"sources": {"gmail": {"enabled": True}}}
    fake = GmailTestResult(
        ok=True, error_kind=None, message="Authenticated", email_address="u@x.com"
    )

    with patch("job_finder.web.onboarding.inbox_check.check_oauth", return_value=fake):
        result = run_inbox_check(config, conn, window_hours=72, now=now)

    assert result.status == "green"
    assert result.emails_in_window == 1
    assert result.jobs_in_window == 12
    assert "healthy" in result.reason.lower()


def test_window_excludes_old_rows(conn):
    """Rows older than the window must not count."""
    now = datetime(2026, 5, 22, 12, 0, 0)
    old = (now - timedelta(hours=100)).isoformat()
    _insert_row(conn, "m1", "gmail", old, jobs_found=99)

    config = {"sources": {"gmail": {"enabled": True}}}
    fake = GmailTestResult(
        ok=True, error_kind=None, message="Authenticated", email_address="u@x.com"
    )

    with patch("job_finder.web.onboarding.inbox_check.check_oauth", return_value=fake):
        result = run_inbox_check(config, conn, window_hours=72, now=now)

    assert result.status == "red"
    assert result.emails_in_window == 0


def test_both_sources_green_when_one_auth_ok(conn):
    """If both sources are enabled and at least one auth succeeds, fall through to activity."""
    now = datetime(2026, 5, 22, 12, 0, 0)
    recent = (now - timedelta(hours=1)).isoformat()
    _insert_row(conn, "m1", "gmail", recent, jobs_found=5)

    config = {
        "sources": {
            "gmail": {"enabled": True},
            "imap": {"enabled": True, "email": "x@x.com"},
        }
    }
    gmail_ok = GmailTestResult(ok=True, error_kind=None, message="ok")
    imap_fail = ImapTestResult(ok=False, error_kind="auth", message="bad pw")

    with (
        patch("job_finder.web.onboarding.inbox_check.check_oauth", return_value=gmail_ok),
        patch("job_finder.web.onboarding.inbox_check.check_imap", return_value=imap_fail),
        patch("job_finder.web.onboarding.inbox_check.get_secret", return_value="pw"),
    ):
        result = run_inbox_check(config, conn, window_hours=72, now=now)

    assert result.status == "green"
    assert result.source_kind == "both"
    assert result.gmail_auth is gmail_ok
    assert result.imap_auth is imap_fail


def test_red_when_both_sources_auth_fail(conn):
    """If every configured source's auth fails, the verdict is RED before activity is checked."""
    now = datetime(2026, 5, 22, 12, 0, 0)
    config = {
        "sources": {
            "gmail": {"enabled": True},
            "imap": {"enabled": True, "email": "x@x.com"},
        }
    }
    gmail_fail = GmailTestResult(ok=False, error_kind="refresh", message="Refresh failed")
    imap_fail = ImapTestResult(ok=False, error_kind="auth", message="bad pw")

    with (
        patch("job_finder.web.onboarding.inbox_check.check_oauth", return_value=gmail_fail),
        patch("job_finder.web.onboarding.inbox_check.check_imap", return_value=imap_fail),
        patch("job_finder.web.onboarding.inbox_check.get_secret", return_value="pw"),
    ):
        result = run_inbox_check(config, conn, now=now)

    assert result.status == "red"
    assert result.source_kind == "both"
    # Reason should be the first failure encountered (gmail in iteration order)
    assert "Refresh failed" in result.reason


def test_imap_only_missing_credentials_returns_red(conn):
    """IMAP enabled with no email/password is an auth failure (without calling check_imap)."""
    config = {"sources": {"imap": {"enabled": True, "email": ""}}}

    with (
        patch("job_finder.web.onboarding.inbox_check.check_imap") as imap_fn,
        patch("job_finder.web.onboarding.inbox_check.get_secret", return_value=""),
    ):
        result = run_inbox_check(config, conn)

    assert result.status == "red"
    assert result.source_kind == "imap"
    imap_fn.assert_not_called()


def test_db_error_in_activity_query_degrades_quietly(conn):
    """If the activity query raises, treat as 0 emails (RED) rather than crashing."""
    config = {"sources": {"gmail": {"enabled": True}}}
    fake = GmailTestResult(ok=True, error_kind=None, message="ok")
    conn.execute("DROP TABLE email_parse_log")  # forces the query to fail

    with patch("job_finder.web.onboarding.inbox_check.check_oauth", return_value=fake):
        result = run_inbox_check(config, conn)

    assert result.status == "red"
    assert result.emails_in_window == 0


def test_result_is_frozen():
    r = InboxCheckResult(status="green", summary="x", reason="y", source_kind="gmail")
    with pytest.raises(Exception):
        r.status = "red"  # type: ignore[misc]
