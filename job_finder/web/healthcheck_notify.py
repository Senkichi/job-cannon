"""Healthcheck notification egress with fire-once dedup.

Out-of-process deadman probe: when ``job-cannon healthcheck --notify`` is invoked
on a cadence by the OS scheduler, this module wires the verdict to the existing
``notify()`` egress (desktop toast + optional email-to-self) with dedup so that
a single outage triggers exactly one alert and one recovery notice — not a flood
of per-interval notifications.

State is persisted in a tiny JSON file under ``user_data_root() / "logs"``:
``{"last_status": "ok"|"degraded"|"down", "notified_at": "<utc-iso>"}``. The file
is written atomically via temp + ``os.replace`` (mirroring the heartbeat write).
All I/O is best-effort: a raising ``notify()``, an unwritable state path, or a
failed ``load_config()`` never propagates out of ``run_healthcheck`` and never
changes its exit code.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from job_finder.json_utils import utc_now_iso
from job_finder.web import user_data_dirs

if TYPE_CHECKING:
    from job_finder.web.healthcheck import HealthVerdict

logger = logging.getLogger(__name__)


def notify_state_path() -> Path:
    """Return the path to the healthcheck notify state file.

    The file lives under ``user_data_root() / "logs"`` and honors
    ``JOB_CANNON_USER_DATA_DIR``. Mirrors the ``logs``-dir convention used by
    the server markers.

    Returns:
        Path to ``healthcheck-notify.json`` under the user-data logs directory.
    """
    return user_data_dirs.user_data_root() / "logs" / "healthcheck-notify.json"


def _load_state(path: Path) -> dict:
    """Read the notify state file; return {} on any error (best-effort).

    Args:
        path: Path to the state file.

    Returns:
        Dict with ``last_status`` and ``notified_at`` keys, or {} on error.
    """
    try:
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(path: Path, state: dict) -> None:
    """Write the notify state file atomically via temp + os.replace.

    Best-effort: any error is logged and swallowed — a wedged disk must not
    crash the healthcheck probe.

    Args:
        path: Path to the state file.
        state: Dict with ``last_status`` and ``notified_at`` keys.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f"{path.name}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        logger.debug("healthcheck notify state write failed", exc_info=True)


def maybe_notify(verdict: HealthVerdict, *, include_degraded: bool = False) -> bool:
    """Fire a notification on status transitions, with fire-once-per-outage dedup.

    Returns ``True`` iff a notification was actually dispatched. The dedup
    logic:

    - Determine the *alerting* set: ``{"down"}`` normally, ``{"down",
      "degraded"}`` when ``include_degraded``.
    - Read prior state via ``notify_state_path()``.
    - **Fire-once-per-outage:** if ``verdict.status`` is in the alerting set
      AND ``verdict.status != state.get("last_status")`` (a transition INTO the
      bad state, or a change of bad state), build the alert and call
      ``notify(...)``, then persist ``last_status = verdict.status``.
    - **Recovery notice:** if ``verdict.status == "ok"`` AND
      ``state.get("last_status")`` was in the alerting set, send a one-shot
      "recovered" notification and persist ``last_status = "ok"``.
    - **Suppress while persisting:** if ``verdict.status == state.get("last_status")``,
      do nothing (return ``False``) — this is what turns a per-15-minute cadence
      into one alert per outage.

    Config load is best-effort and local: ``try: from job_finder.config import
    load_config; config = load_config()`` else ``config = {}``; pass to
    ``notify(...)``. The whole ``notify(...)`` call is wrapped so a dead egress
    can never propagate.

    Args:
        verdict: HealthVerdict from ``compute_verdict``.
        include_degraded: If True, also notify on DEGRADED verdicts.

    Returns:
        True iff a notification was dispatched; False otherwise.
    """
    from job_finder.web.notifications import notify

    alerting_set = {"down"} if not include_degraded else {"down", "degraded"}
    state_path = notify_state_path()
    state = _load_state(state_path)
    last_status = state.get("last_status")

    # Transition into alerting state (or change of bad state)
    if verdict.status in alerting_set and verdict.status != last_status:
        title = f"Job Cannon: app {verdict.status.upper()}"
        body_lines = list(verdict.reasons)
        if verdict.degraded_sources:
            body_lines.append(f"Degraded sources: {', '.join(verdict.degraded_sources)}")
        body = "\n".join(body_lines) if body_lines else "No additional details."
        severity = "critical" if verdict.status == "down" else "warning"

        try:
            from job_finder.config import load_config

            config = load_config(allow_missing=True)
        except Exception:
            config = {}

        try:
            notify(title, body, severity=severity, config=config)
        except Exception:
            logger.warning("healthcheck notify failed", exc_info=True)
            return False

        try:
            _write_state(state_path, {"last_status": verdict.status, "notified_at": utc_now_iso()})
        except Exception:
            logger.debug("healthcheck notify state write failed", exc_info=True)
        return True

    # Recovery notice: OK after a bad state
    if verdict.status == "ok" and last_status in alerting_set:
        title = "Job Cannon: recovered"
        body = "The app is now healthy."
        try:
            from job_finder.config import load_config

            config = load_config(allow_missing=True)
        except Exception:
            config = {}

        try:
            notify(title, body, severity="info", config=config)
        except Exception:
            logger.warning("healthcheck recovery notify failed", exc_info=True)
            return False

        try:
            _write_state(state_path, {"last_status": "ok", "notified_at": utc_now_iso()})
        except Exception:
            logger.debug("healthcheck recovery state write failed", exc_info=True)
        return True

    # No status change: suppress (persist current state to keep the file fresh)
    if verdict.status == last_status:
        if verdict.status in alerting_set or verdict.status == "ok":
            try:
                _write_state(
                    state_path, {"last_status": verdict.status, "notified_at": utc_now_iso()}
                )
            except Exception:
                logger.debug("healthcheck state refresh failed", exc_info=True)
        return False

    # First run with no prior state: persist but don't notify (wait for a transition)
    if last_status is None:
        try:
            _write_state(state_path, {"last_status": verdict.status, "notified_at": utc_now_iso()})
        except Exception:
            logger.debug("healthcheck initial state write failed", exc_info=True)
        return False

    return False
