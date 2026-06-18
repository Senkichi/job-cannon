"""Tests for the notification egress module (#438).

Covers the pluggable two-backend façade: desktop toast + optional
email-to-self. No real notifications fire and no SMTP socket opens — the
desktop-notifier class and ``smtplib.SMTP_SSL`` are monkeypatched, and
``secrets.get_secret`` is stubbed.
"""

from __future__ import annotations

import dataclasses

import pytest

from job_finder.web import notifications
from job_finder.web.notifications import NotificationResult, notify


class _FakeNotifier:
    """Stand-in for DesktopNotifierSync recording the most recent send."""

    last: dict | None = None

    def __init__(self, *args, **kwargs):
        pass

    def send(self, *, title, message, **kwargs):
        _FakeNotifier.last = {"title": title, "message": message, **kwargs}


class _FakeSMTP:
    """Stand-in for smtplib.SMTP_SSL recording login + send_message calls."""

    instances: list = []

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.logins: list = []
        self.sent: list = []
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        self.logins.append((user, password))

    def send_message(self, msg):
        self.sent.append(msg)


@pytest.fixture(autouse=True)
def _reset_fakes():
    _FakeNotifier.last = None
    _FakeSMTP.instances = []
    yield


def _desktop_only_config():
    return {"notifications": {"desktop": {"enabled": True}, "email": {"enabled": False}}}


def _email_config(to="me@example.com"):
    return {
        "notifications": {
            "desktop": {"enabled": False},
            "email": {
                "enabled": True,
                "to": to,
                "smtp_host": "smtp.gmail.com",
                "smtp_port": 465,
            },
        },
        "sources": {"imap": {"email": "me@example.com"}},
    }


def test_desktop_only_returns_single_result(monkeypatch):
    monkeypatch.setattr(notifications, "DesktopNotifierSync", _FakeNotifier)
    results = notify("Title", "Body", config=_desktop_only_config())
    assert len(results) == 1
    assert results[0].backend == "desktop"
    assert results[0].ok is True
    assert _FakeNotifier.last == {
        "title": "Title",
        "message": "Body",
        "urgency": notifications._urgency_for("warning"),
    }


def test_email_send_uses_app_password(monkeypatch):
    monkeypatch.setattr(notifications.smtplib, "SMTP_SSL", _FakeSMTP)
    monkeypatch.setattr(notifications, "get_secret", lambda name, *, config=None: "app-pw-123")
    results = notify("Subj", "Body", config=_email_config())
    assert len(results) == 1
    assert results[0].backend == "email"
    assert results[0].ok is True
    assert len(_FakeSMTP.instances) == 1
    smtp = _FakeSMTP.instances[0]
    assert smtp.logins == [("me@example.com", "app-pw-123")]
    assert len(smtp.sent) == 1


def test_blank_recipient_skips_smtp(monkeypatch):
    monkeypatch.setattr(notifications.smtplib, "SMTP_SSL", _FakeSMTP)
    monkeypatch.setattr(notifications, "get_secret", lambda name, *, config=None: "app-pw-123")
    results = notify("Subj", "Body", config=_email_config(to=""))
    # No SMTP attempt at all.
    assert _FakeSMTP.instances == []
    assert len(results) == 1
    assert results[0].backend == "email"
    assert results[0].ok is False
    assert "recipient" in (results[0].detail or "")


def test_raising_backend_does_not_suppress_other(monkeypatch):
    # Desktop backend raises; email must still be attempted and returned.
    def _boom(*args, **kwargs):
        raise RuntimeError("no display surface")

    monkeypatch.setattr(notifications, "_send_desktop", _boom)
    monkeypatch.setattr(notifications.smtplib, "SMTP_SSL", _FakeSMTP)
    monkeypatch.setattr(notifications, "get_secret", lambda name, *, config=None: "app-pw-123")
    config = {
        "notifications": {
            "desktop": {"enabled": True},
            "email": {
                "enabled": True,
                "to": "me@example.com",
                "smtp_host": "smtp.gmail.com",
                "smtp_port": 465,
            },
        },
        "sources": {"imap": {"email": "me@example.com"}},
    }
    results = notify("T", "B", config=config)
    by_backend = {r.backend: r for r in results}
    assert by_backend["desktop"].ok is False
    assert by_backend["email"].ok is True


def test_email_backend_failure_is_isolated(monkeypatch):
    # SMTP refuses the connection — ok=False, never raises out of notify().
    def _refused(host, port):
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(notifications.smtplib, "SMTP_SSL", _refused)
    monkeypatch.setattr(notifications, "get_secret", lambda name, *, config=None: "app-pw-123")
    results = notify("T", "B", config=_email_config())
    assert len(results) == 1
    assert results[0].backend == "email"
    assert results[0].ok is False
    assert results[0].detail


def test_notification_result_is_frozen():
    r = NotificationResult("desktop", True, None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.ok = False  # type: ignore[misc]


def test_no_backends_enabled_returns_empty():
    config = {"notifications": {"desktop": {"enabled": False}, "email": {"enabled": False}}}
    assert notify("T", "B", config=config) == ()
