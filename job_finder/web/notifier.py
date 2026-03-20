"""Windows 11 desktop notifications via win11toast.

All notifications fire in daemon threads to never block the caller.
Fails silently if win11toast is unavailable (non-Windows, import error).
Each notification type is gated by per-type toggle in config.yaml.

Notifications also have a 24-hour per-(dedup_key, type) cooldown guard so the
same event cannot spam the user across multiple scheduler runs.
"""

import logging
import threading
from datetime import datetime, timedelta

from job_finder.config import DEFAULT_SERVER_HOST, DEFAULT_SERVER_PORT

logger = logging.getLogger(__name__)

_NOTIFY_LOCK = threading.Lock()
_NOTIFY_SEEN: dict[tuple[str, str], datetime] = {}
_NOTIFY_COOLDOWN_HOURS = 24


def _can_notify(dedup_key: str, notification_type: str) -> bool:
    """Return True if the cooldown has elapsed since last notification for this (key, type) pair.

    Thread-safe: protected by _NOTIFY_LOCK.
    Module-level state resets on app restart (acceptable for single-user app).

    Args:
        dedup_key: Job dedup_key or other unique identifier.
        notification_type: One of 'high_score', 'pipeline_change'.

    Returns:
        True if the notification may fire, False if still within cooldown.
    """
    now = datetime.now()
    cache_key = (dedup_key, notification_type)
    with _NOTIFY_LOCK:
        last_fired = _NOTIFY_SEEN.get(cache_key)
        if last_fired is not None:
            if (now - last_fired) < timedelta(hours=_NOTIFY_COOLDOWN_HOURS):
                return False
        _NOTIFY_SEEN[cache_key] = now
        return True


def send_notification(title: str, body: str, url: str | None = None) -> None:
    """Send a Windows 11 toast notification in a daemon thread.

    Fails silently if win11toast is not available.

    Args:
        title: Notification title text.
        body: Notification body text.
        url: Optional URL to open when notification is clicked.
    """
    def _send():
        try:
            from win11toast import toast
            kwargs = {"on_click": url} if url else {}
            toast(title, body, **kwargs)
        except Exception as e:
            logger.debug("Notification failed (non-fatal): %s", e)

    t = threading.Thread(target=_send, daemon=True)
    t.start()


def _build_app_url(config: dict, path: str) -> str:
    """Build a localhost URL from server config.

    Args:
        config: Full JF_CONFIG dict (reads server.host and server.port).
        path: URL path to append (e.g. '/jobs/some-key').

    Returns:
        Full URL string like 'http://127.0.0.1:5000/jobs/some-key'.
    """
    server = config.get("server", {})
    host = server.get("host", DEFAULT_SERVER_HOST)
    port = server.get("port", DEFAULT_SERVER_PORT)
    return f"http://{host}:{port}{path}"


def _is_enabled(config: dict, notification_type: str) -> bool:
    """Check if a notification type is enabled in config.

    Defaults to True if the notifications section or the specific key is absent.

    Args:
        config: Full JF_CONFIG dict.
        notification_type: One of 'high_score', 'pipeline_change', 'budget_alert'.

    Returns:
        True if the notification type is enabled.
    """
    return config.get("notifications", {}).get(notification_type, True)


def notify_high_score(
    job_title: str,
    company: str,
    score: float,
    dedup_key: str,
    config: dict,
) -> None:
    """Notify when a new job scores above threshold.

    Args:
        job_title: Job title from the DB.
        company: Company name from the DB.
        score: Haiku score (0-100).
        dedup_key: Job dedup_key for building the URL.
        config: Full JF_CONFIG dict (reads notifications.high_score toggle).
    """
    if not _is_enabled(config, "high_score"):
        return
    if not _can_notify(dedup_key, "high_score"):
        return
    from urllib.parse import quote
    url = _build_app_url(config, f"/jobs/{quote(dedup_key, safe='')}")
    send_notification(
        "Job Finder — High Score Job",
        f"{job_title} at {company} ({score:.0f}/100)",
        url=url,
    )


def notify_pipeline_change(
    detection_type: str,
    job_title: str,
    company: str,
    dedup_key: str,
    config: dict,
) -> None:
    """Notify when an auto-detected pipeline change occurs.

    Args:
        detection_type: Classification result ('rejection', 'interview_invite',
                        'application_confirmation', 'offer', etc.).
        job_title: Job title from the DB.
        company: Company name from the DB.
        dedup_key: Job dedup_key for building the URL.
        config: Full JF_CONFIG dict (reads notifications.pipeline_change toggle).
    """
    if not _is_enabled(config, "pipeline_change"):
        return
    if not _can_notify(dedup_key, "pipeline_change"):
        return
    from urllib.parse import quote
    url = _build_app_url(config, f"/jobs/{quote(dedup_key, safe='')}")
    type_labels = {
        "rejection": "Rejection detected",
        "interview_invite": "Interview invite",
        "application_confirmation": "Application confirmed",
        "offer": "Offer received",
        # pipeline_detector.py uses these types:
        "interview": "Interview invite",
        "confirmation": "Application confirmed",
    }
    label = type_labels.get(
        detection_type,
        detection_type.replace("_", " ").title(),
    )
    send_notification(
        f"Job Finder — {label}",
        f"{job_title} at {company}",
        url=url,
    )


def notify_budget_alert(percent: float, config: dict) -> None:
    """Notify when budget reaches 80% or 100%.

    Args:
        percent: Current monthly spend as percentage of budget cap.
        config: Full JF_CONFIG dict (reads notifications.budget_alert toggle).
    """
    if not _is_enabled(config, "budget_alert"):
        return
    level = "reached" if percent >= 100 else f"at {percent:.0f}%"
    send_notification(
        "Job Finder — Budget Alert",
        f"Monthly API budget {level} ({percent:.0f}%)",
        url=_build_app_url(config, "/settings"),
    )
