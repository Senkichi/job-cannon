"""Pluggable notification egress — desktop toast + optional email-to-self.

Job Cannon records health degradation silently (a source flips to ``degraded``
and writes an ``activity_tracker`` row), but nothing reaches the user when they
are away from ``localhost:5000``. This module is the small, best-effort egress
seam both the C2-7 health escalation and the forthcoming supervisor call to push
an alert *out* of the app.

Design (locked, see issue #438):
  * A single ``notify(title, body, *, severity, config)`` façade dispatches to
    one or more configured backends and returns one :class:`NotificationResult`
    per attempted backend. It NEVER raises on delivery failure — a dead display
    surface or a refused SMTP connection must not propagate into the caller.
  * **Desktop** is the primary backend (native OS toast via ``desktop-notifier``:
    WinRT / ``UNUserNotificationCenter`` / libnotify). It degrades to a no-op
    ``ok=False`` result on headless hosts.
  * **Email-to-self** is an OPTIONAL, config-gated durable fallback (off by
    default). It reuses the Gmail app password registered for the IMAP source.

IMAP NOTE: ``job_finder/sources/imap_source.py`` is read-only by contract — it
only *fetches* mail and never sends. The email backend here therefore opens its
**own** outbound ``smtplib`` connection; it shares only the app-password secret
(``sources.imap.app_password``), not the IMAP client or its socket.

Backends are independent and best-effort: a desktop failure must not suppress
the email send and vice versa. No dedup / rate-limiting / quiet-hours here —
that is a future iteration. This is fire-and-forget.
"""

from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

from job_finder.secrets import get_secret

logger = logging.getLogger(__name__)

# desktop-notifier imports cleanly even on headless hosts (the no-op only shows
# up at send time), but we still guard the import so a packaging hiccup can
# never take down the egress path — an unavailable surface is just ok=False.
try:  # pragma: no cover - exercised via monkeypatch in tests
    from desktop_notifier import DesktopNotifierSync, Urgency
except Exception:  # pragma: no cover - desktop-notifier missing/unbuildable
    DesktopNotifierSync = None  # type: ignore[assignment]
    Urgency = None  # type: ignore[assignment]

_APP_NAME = "Job Cannon"


@dataclass(frozen=True)
class NotificationResult:
    """Outcome of a single backend delivery attempt (immutable value object)."""

    backend: str
    ok: bool
    detail: str | None = None


def _urgency_for(severity: str):
    """Map a severity string to a desktop-notifier ``Urgency`` (None if unavail)."""
    if Urgency is None:
        return None
    return {
        "critical": Urgency.Critical,
        "warning": Urgency.Normal,
        "info": Urgency.Low,
    }.get(severity, Urgency.Normal)


def _send_desktop(title: str, body: str, severity: str = "warning") -> NotificationResult:
    """Thin wrapper over ``desktop-notifier``; no-op ``ok=False`` when unreachable.

    Any failure — library missing, no display surface (headless), backend
    error — yields ``ok=False`` rather than raising, so the email backend is
    never starved by a dead toast surface.
    """
    if DesktopNotifierSync is None:
        return NotificationResult("desktop", False, "desktop-notifier unavailable")
    try:
        notifier = DesktopNotifierSync(app_name=_APP_NAME)
        notifier.send(title=title, message=body, urgency=_urgency_for(severity))
        return NotificationResult("desktop", True, None)
    except Exception as exc:  # no display surface / backend error — best-effort
        logger.warning("desktop notification failed: %s", exc)
        return NotificationResult("desktop", False, str(exc))


def _send_email(title: str, body: str, config: dict) -> NotificationResult:
    """Send the alert to the user's own address over a dedicated SMTP connection.

    Reuses the IMAP Gmail app password (``sources.imap.app_password``) but opens
    its **own** outbound socket — the IMAP source is read-only and never sends.
    A blank ``to`` short-circuits before any network attempt.
    """
    email_cfg = (config.get("notifications", {}) or {}).get("email", {}) or {}
    to = (email_cfg.get("to") or "").strip()
    if not to:
        return NotificationResult("email", False, "no recipient configured")

    password = get_secret("sources.imap.app_password", config=config)
    if not password:
        return NotificationResult("email", False, "no app password available")

    # The login/From identity is the IMAP account; fall back to the recipient.
    imap_cfg = (config.get("sources", {}) or {}).get("imap", {}) or {}
    username = (imap_cfg.get("email") or "").strip() or to

    host = email_cfg.get("smtp_host", "smtp.gmail.com")
    port = int(email_cfg.get("smtp_port", 465))

    try:
        msg = EmailMessage()
        msg["Subject"] = title
        msg["From"] = username
        msg["To"] = to
        msg.set_content(body)

        if port == 587:
            with smtplib.SMTP(host, port) as server:
                server.starttls()
                server.login(username, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP_SSL(host, port) as server:
                server.login(username, password)
                server.send_message(msg)
        return NotificationResult("email", True, None)
    except Exception as exc:  # SMTP refused / auth failed — best-effort
        logger.warning("email notification failed: %s", exc)
        return NotificationResult("email", False, str(exc))


def notify(
    title: str,
    body: str,
    *,
    severity: str = "warning",
    config: dict,
) -> tuple[NotificationResult, ...]:
    """Dispatch an alert to every enabled backend; return one result per attempt.

    Resolves enabled backends from ``config["notifications"]``. Each backend is
    independent and best-effort: a backend that raises is converted to an
    ``ok=False`` result and never suppresses the other backend's attempt.
    ``notify`` itself never raises on delivery failure.
    """
    ncfg = (config or {}).get("notifications", {}) or {}
    results: list[NotificationResult] = []

    if (ncfg.get("desktop", {}) or {}).get("enabled", False):
        try:
            results.append(_send_desktop(title, body, severity))
        except Exception as exc:  # defense in depth — never let one backend kill the other
            logger.warning("desktop backend raised: %s", exc)
            results.append(NotificationResult("desktop", False, str(exc)))

    if (ncfg.get("email", {}) or {}).get("enabled", False):
        try:
            results.append(_send_email(title, body, config))
        except Exception as exc:
            logger.warning("email backend raised: %s", exc)
            results.append(NotificationResult("email", False, str(exc)))

    return tuple(results)
