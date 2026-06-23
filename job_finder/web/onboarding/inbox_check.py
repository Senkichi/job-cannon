"""Combined inbox-wiring system check (F1).

Ties together the auth probe (`gmail_test.check_oauth` / `imap_test.check_imap`)
and an `email_parse_log` activity query into a single GREEN/YELLOW/RED status
the Settings page can render and the dashboard can banner.

Status semantics:

- ``green``  — auth OK + ≥1 job-bearing email parsed in the last 72 hours
- ``yellow`` — auth OK + emails arrived but all failed to parse (or yielded 0 jobs)
- ``red``    — auth failed, or zero alert emails in the window
- ``unconfigured`` — neither gmail nor imap is enabled in config

The check is read-only — no DB writes, no network calls except the configured
source's auth probe. Safe to call on demand from a Settings button without
side-effects.

Both Gmail and IMAP are covered: each ingestion path writes a run-level row to
``email_parse_log`` (sender ``gmail`` / ``imap``) and the activity query below is
sender-agnostic, so an IMAP-only install reaches ``green`` once it has parsed a
job-bearing alert in the window.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from job_finder.secrets import get_secret
from job_finder.web.onboarding.gmail_test import GmailTestResult, check_oauth
from job_finder.web.onboarding.imap_test import ImapTestResult, check_imap

logger = logging.getLogger(__name__)

Status = Literal["green", "yellow", "red", "unconfigured"]

_DEFAULT_WINDOW_HOURS = 72


@dataclass(frozen=True)
class InboxCheckResult:
    """Outcome of the combined inbox-wiring check.

    Attributes:
        status: ``green`` / ``yellow`` / ``red`` / ``unconfigured``.
        summary: One-line headline the Settings tile renders verbatim.
        reason: Longer diagnostic; what the user should do next.
        source_kind: ``"gmail"``, ``"imap"``, ``"both"``, or ``"none"``.
        gmail_auth: Per-source auth result; ``None`` if Gmail isn't configured.
        imap_auth: Per-source auth result; ``None`` if IMAP isn't configured.
        window_hours: Activity-query window used (default 72).
        emails_in_window: How many `email_parse_log` rows fell in the window.
        jobs_in_window: Sum of ``jobs_found`` across those rows.
    """

    status: Status
    summary: str
    reason: str
    source_kind: Literal["gmail", "imap", "both", "none"]
    gmail_auth: GmailTestResult | None = None
    imap_auth: ImapTestResult | None = None
    window_hours: int = _DEFAULT_WINDOW_HOURS
    emails_in_window: int = 0
    jobs_in_window: int = 0


def run_inbox_check(
    config: dict,
    conn: sqlite3.Connection,
    *,
    token_path: str = "token.json",  # noqa: S107 - file path, not a password
    window_hours: int = _DEFAULT_WINDOW_HOURS,
    now: datetime | None = None,
) -> InboxCheckResult:
    """Resolve which source is configured, run auth + activity checks, return verdict.

    Args:
        config: Loaded ``config.yaml`` dict.
        conn: Active SQLite connection (read-only usage; no writes).
        token_path: Path to the Gmail OAuth token file. Default ``token.json``.
        window_hours: Activity window in hours. Default 72.
        now: Injected "current time" for tests. Defaults to ``datetime.now()``.

    Returns:
        InboxCheckResult. Never raises — DB errors degrade to a yellow status
        with a diagnostic reason.
    """
    now = now or datetime.now(UTC).replace(tzinfo=None)
    sources = config.get("sources", {}) or {}
    gmail_enabled = bool(sources.get("gmail", {}).get("enabled", False))
    imap_enabled = bool(sources.get("imap", {}).get("enabled", False))

    source_kind: Literal["gmail", "imap", "both", "none"]
    if gmail_enabled and imap_enabled:
        source_kind = "both"
    elif gmail_enabled:
        source_kind = "gmail"
    elif imap_enabled:
        source_kind = "imap"
    else:
        source_kind = "none"

    if source_kind == "none":
        return InboxCheckResult(
            status="unconfigured",
            summary="No email source configured",
            reason=("Enable Gmail or IMAP in Settings → Sources to start ingesting job alerts."),
            source_kind="none",
            window_hours=window_hours,
        )

    # ---- Auth probes ----
    gmail_auth: GmailTestResult | None = None
    imap_auth: ImapTestResult | None = None

    if gmail_enabled:
        gmail_auth = check_oauth(token_path)

    if imap_enabled:
        imap_cfg = sources.get("imap", {}) or {}
        host = imap_cfg.get("host", "imap.gmail.com")
        port = imap_cfg.get("port", 993)
        email = imap_cfg.get("email", "") or ""
        app_password = get_secret("sources.imap.app_password", config=config) or ""
        if not email or not app_password:
            imap_auth = ImapTestResult(
                ok=False,
                error_kind="auth",
                message="IMAP enabled but email or app_password is missing",
            )
        else:
            imap_auth = check_imap(host, port, email, app_password)

    # If every configured source's auth failed → RED.
    auth_results = [r for r in (gmail_auth, imap_auth) if r is not None]
    if auth_results and all(not r.ok for r in auth_results):
        first_failure = next(r for r in auth_results if not r.ok)
        return InboxCheckResult(
            status="red",
            summary="Email source not reachable",
            reason=first_failure.message,
            source_kind=source_kind,
            gmail_auth=gmail_auth,
            imap_auth=imap_auth,
            window_hours=window_hours,
        )

    # ---- Activity query (sender-agnostic: counts gmail + imap run rows) ----
    emails_in_window, jobs_in_window = _count_activity(conn, now, window_hours)

    if emails_in_window == 0:
        return InboxCheckResult(
            status="red",
            summary=f"No job alerts in the last {window_hours} hours",
            reason=(
                "Auth is healthy but no alert emails have been parsed in the "
                "window. Check that your alert providers are still sending "
                "(LinkedIn / Glassdoor / Indeed / etc.) and that the sender "
                "addresses in Settings match what they use."
            ),
            source_kind=source_kind,
            gmail_auth=gmail_auth,
            imap_auth=imap_auth,
            window_hours=window_hours,
            emails_in_window=0,
            jobs_in_window=0,
        )

    if jobs_in_window == 0:
        return InboxCheckResult(
            status="yellow",
            summary=f"{emails_in_window} alerts arrived but produced 0 jobs",
            reason=(
                "Emails are arriving but the parsers couldn't extract jobs from "
                "any of them. Look at recent runs for parse errors, or check "
                "`data/parse_failures/` for archived HTML samples."
            ),
            source_kind=source_kind,
            gmail_auth=gmail_auth,
            imap_auth=imap_auth,
            window_hours=window_hours,
            emails_in_window=emails_in_window,
            jobs_in_window=0,
        )

    return InboxCheckResult(
        status="green",
        summary=f"{emails_in_window} alerts parsed, {jobs_in_window} jobs in the last {window_hours}h",
        reason="Inbox wiring is healthy.",
        source_kind=source_kind,
        gmail_auth=gmail_auth,
        imap_auth=imap_auth,
        window_hours=window_hours,
        emails_in_window=emails_in_window,
        jobs_in_window=jobs_in_window,
    )


def _count_activity(conn: sqlite3.Connection, now: datetime, window_hours: int) -> tuple[int, int]:
    """Return ``(emails, jobs)`` over the window. Degrades to ``(0, 0)`` on error."""
    cutoff = (now - timedelta(hours=window_hours)).isoformat()
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS emails, COALESCE(SUM(jobs_found), 0) AS jobs
               FROM email_parse_log
               WHERE processed_at >= ?""",
            (cutoff,),
        ).fetchone()
        if row is None:
            return (0, 0)
        # sqlite3.Row supports __getitem__; tuple does too.
        emails = int(row[0]) if row[0] is not None else 0
        jobs = int(row[1]) if row[1] is not None else 0
        return (emails, jobs)
    except Exception as exc:
        logger.warning("inbox_check: email_parse_log query failed: %s", type(exc).__name__)
        return (0, 0)
