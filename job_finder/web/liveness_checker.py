"""URL liveness checker for stored job listings.

Nightly scheduled job that verifies stored job URLs are still live.
Marks expired listings to prevent wasted review time and Sonnet
evaluation tokens.
"""

from __future__ import annotations

import logging
import re
import time
from enum import Enum

import requests

logger = logging.getLogger(__name__)


class LivenessStatus(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    UNCERTAIN = "uncertain"
    ERROR = "error"


# Regex patterns for expired/closed job pages.
# Case-insensitive. Checked against the first 5000 chars of the response body.
_EXPIRED_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE) for p in [
        r"this job is no longer available",
        r"this position has been filled",
        r"this job has been closed",
        r"this listing has expired",
        r"no longer accepting applications",
        r"position is no longer open",
        r"this job posting has been removed",
        r"sorry,? this job has already been filled",
        r"this role has been filled",
        r"this job has expired",
        r"the position you are looking for is no longer available",
        r"this requisition is no longer active",
        r"job not found",
        r"posting not found",
        r"this opening has been closed",
        # Greenhouse-specific
        r"There are no jobs matching your search",
        # Lever-specific
        r"This position is no longer available",
        # German
        r"Diese Stelle ist nicht mehr verf[uü]gbar",
        r"Diese Position wurde bereits besetzt",
        # French
        r"Cette offre n['']est plus disponible",
    ]
]

# Apply button patterns (presence = likely active)
_APPLY_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE) for p in [
        r"apply\s+(now|for this|to this)",
        r'type="submit"[^>]*>.*?apply',
        r"submit.{0,20}application",
    ]
]

# Greenhouse error redirect pattern
_GREENHOUSE_ERROR_RE = re.compile(r"[?&]error=true")

# Minimum body length -- pages with < 300 chars are likely redirects/stubs
_MIN_BODY_LENGTH = 300

_REQUEST_TIMEOUT = 15
_BATCH_DELAY = 0.5  # seconds between requests


def check_url_liveness(url: str) -> tuple[LivenessStatus, str]:
    """Check if a single job URL is still live.

    Returns:
        Tuple of (status, reason).
    """
    if _GREENHOUSE_ERROR_RE.search(url):
        return LivenessStatus.EXPIRED, "greenhouse_error_redirect"

    try:
        resp = requests.get(
            url,
            timeout=_REQUEST_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; JobCannon/1.0)"},
            allow_redirects=True,
        )
    except requests.RequestException as e:
        return LivenessStatus.ERROR, str(e)[:200]

    # Hard 404/410
    if resp.status_code in (404, 410):
        return LivenessStatus.EXPIRED, f"http_{resp.status_code}"

    if resp.status_code == 403:
        return LivenessStatus.UNCERTAIN, "http_403_blocked"

    if resp.status_code >= 500:
        return LivenessStatus.ERROR, f"http_{resp.status_code}"

    body = resp.text[:5000]

    # Check body length
    if len(body.strip()) < _MIN_BODY_LENGTH:
        return LivenessStatus.EXPIRED, "empty_page"

    # Check expired patterns
    for pattern in _EXPIRED_PATTERNS:
        if pattern.search(body):
            return LivenessStatus.EXPIRED, f"pattern:{pattern.pattern[:50]}"

    # Check for apply button (positive signal)
    has_apply = any(p.search(body) for p in _APPLY_PATTERNS)
    if has_apply:
        return LivenessStatus.ACTIVE, "apply_button_found"

    # If we got a 200 with substantial content but no apply button and no
    # expired message, it's uncertain (could be a careers page redirect).
    if resp.status_code == 200 and len(body.strip()) > _MIN_BODY_LENGTH:
        return LivenessStatus.ACTIVE, "page_ok"

    return LivenessStatus.UNCERTAIN, "no_clear_signal"


def run_liveness_check(db_path: str, config: dict | None = None) -> dict:
    """Check liveness of active job URLs. Nightly scheduled job.

    Checks jobs that are:
    - pipeline_status in ('discovered', 'reviewing', 'applied')
    - Not already marked stale or archived
    - Have a source_url
    - Haven't been checked in the last N days (configurable)

    Returns:
        Summary dict with counts.
    """
    from job_finder.web.db_helpers import standalone_connection

    cfg = (config or {}).get("liveness", {})
    batch_limit = cfg.get("batch_limit", 200)
    check_interval_days = cfg.get("check_interval_days", 3)

    summary = {"checked": 0, "active": 0, "expired": 0, "uncertain": 0, "errors": 0}

    with standalone_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT dedup_key, source_urls
            FROM jobs
            WHERE pipeline_status IN ('discovered', 'reviewing', 'applied')
              AND is_stale = 0
              AND source_urls IS NOT NULL
              AND source_urls != '[]'
              AND (liveness_checked_at IS NULL
                   OR liveness_checked_at < datetime('now', ?))
            ORDER BY liveness_checked_at ASC NULLS FIRST
            LIMIT ?
            """,
            (f"-{check_interval_days} days", batch_limit),
        ).fetchall()

        logger.info("Liveness check: %d URLs to verify", len(rows))

        for row in rows:
            dedup_key = row["dedup_key"]

            # Extract first URL from JSON array
            import json as _json
            try:
                urls = _json.loads(row["source_urls"])
                url = urls[0] if isinstance(urls, list) and urls else None
            except (ValueError, TypeError):
                url = None

            if not url:
                continue

            status, reason = check_url_liveness(url)
            summary["checked"] += 1
            summary[status.value] = summary.get(status.value, 0) + 1

            # Update job record
            conn.execute(
                """
                UPDATE jobs
                SET liveness_checked_at = datetime('now'),
                    liveness_status = ?,
                    liveness_reason = ?
                WHERE dedup_key = ?
                """,
                (status.value, reason, dedup_key),
            )

            # If expired, mark stale and create pipeline event
            if status == LivenessStatus.EXPIRED:
                conn.execute(
                    "UPDATE jobs SET is_stale = 1 WHERE dedup_key = ?",
                    (dedup_key,),
                )
                conn.execute(
                    """
                    INSERT INTO pipeline_events
                        (job_id, old_status, new_status, source, evidence, created_at)
                    VALUES (?, 'active', 'expired', 'liveness_checker', ?, datetime('now'))
                    """,
                    (dedup_key, reason),
                )
                logger.info(
                    "Expired: %s (%s)", dedup_key, reason
                )

            conn.commit()
            time.sleep(_BATCH_DELAY)

    logger.info("Liveness check complete: %s", summary)
    return summary
