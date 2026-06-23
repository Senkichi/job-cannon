"""Batch reprobe of frozen custom-miss companies for embedded ATS boards (PR-A3).

A large cohort of ``companies`` sit at ``ats_platform IS NULL`` /
``ats_probe_status='miss'`` with a ``careers_url`` but ``scan_enabled=0`` —
frozen after speculative ATS-slug probing gave up. Many of those *custom* career
pages actually embed a link to a real, supported ATS board (a Workday tenant on a
vanity domain, Greenhouse/Lever/Ashby/SmartRecruiters, or one of the platform
boards the link classifier recognizes after PR-A2). This module statically
fetches each ``careers_url``, runs the existing pure link-discovery classifier
(``best_ats_candidate``), and — on a LIVE-VERIFIED embed — promotes the company
to the matching scanner AND re-enables scanning via
``promote_from_careers_link(..., reenable_scan=True)``.

No new scanner, no LLM, no Playwright. The static fetch is a *coverage floor*: it
catches server-rendered links and misses JS-injected ones. Those are caught later
by the daily Playwright careers crawl once the company is ``scan_enabled`` again
(the promotion re-enables it). Promotion itself is the single audited writer, so
every flip is live-verified and collision-guarded exactly like the crawler path.
"""

from __future__ import annotations

import logging
import sqlite3
import time

import requests

from job_finder.web.ats_identity_reconcile import promote_from_careers_link
from job_finder.web.careers_crawler._ats_link_discovery import best_ats_candidate
from job_finder.web.db_helpers import standalone_connection

logger = logging.getLogger(__name__)

_DEFAULT_MAX_COMPANIES = 500
_DEFAULT_POLITE_DELAY_S = 1.0
_FETCH_TIMEOUT_S = 12
# A real browser UA — some careers CDNs 403 a bare python-requests UA.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_COHORT_SQL = """
    SELECT id, name_raw, careers_url
      FROM companies
     WHERE ats_platform IS NULL
       AND ats_probe_status = 'miss'
       AND careers_url IS NOT NULL
       AND careers_url != ''
     ORDER BY COALESCE(updated_at, created_at) ASC
     LIMIT ?
"""


def _reprobe_settings(config: dict | None) -> dict:
    cfg = ((config or {}).get("ats") or {}).get("reprobe") or {}
    return {
        "enabled": cfg.get("enabled", True),
        "max_companies": int(cfg.get("max_companies_per_run", _DEFAULT_MAX_COMPANIES)),
        "polite_delay_s": float(cfg.get("polite_delay_s", _DEFAULT_POLITE_DELAY_S)),
    }


def _fetch_careers_html(url: str, timeout: int = _FETCH_TIMEOUT_S) -> str | None:
    """Static GET of a careers page. Returns HTML on HTTP 200, else None.

    Tolerant by design: any network error, non-200, or empty body yields None
    (the company is simply left frozen for this pass). No exception escapes.
    """
    try:
        resp = requests.get(
            url,
            timeout=timeout,
            allow_redirects=True,
            headers={"User-Agent": _USER_AGENT, "Accept": "text/html,*/*"},
        )
    except Exception as exc:  # best-effort fetch, never fatal
        logger.debug("reprobe fetch failed url=%s: %s", url, exc)
        return None
    if resp.status_code != 200:
        logger.debug("reprobe fetch non-200 url=%s status=%d", url, resp.status_code)
        return None
    return resp.text or None


def reprobe_custom_miss_cohort(
    db_path: str, config: dict | None = None, *, limit: int | None = None
) -> dict:
    """Statically reprobe the frozen custom-miss cohort for embedded ATS boards.

    Args:
        db_path: Path to the jobs DB.
        config: App config (for ``ats.identity_reconcile`` + ``ats.reprobe``).
        limit: Hard cap on companies processed this run. ``None`` uses the
            configured ``ats.reprobe.max_companies_per_run`` (default 500).

    Returns:
        A summary dict tallying outcomes — never raises on a per-company error.
    """
    summary = {
        "checked": 0,
        "fetched": 0,
        "fetch_errors": 0,
        "embeds_found": 0,
        "promoted": 0,
        "verify_failed": 0,
        "slug_collision": 0,
        "no_candidate": 0,
        "skipped_already_hit": 0,
        "disabled": 0,
    }

    st = _reprobe_settings(config)
    if not st["enabled"]:
        summary["disabled"] = 1
        return summary

    cap = limit if limit is not None else st["max_companies"]
    if cap <= 0:
        return summary

    testing = bool((config or {}).get("TESTING"))
    delay = 0.0 if testing else st["polite_delay_s"]

    with standalone_connection(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(_COHORT_SQL, (cap,)).fetchall()

        for row in rows:
            summary["checked"] += 1
            company_id = row["id"]
            careers_url = row["careers_url"]

            html = _fetch_careers_html(careers_url)
            if html is None:
                summary["fetch_errors"] += 1
                continue
            summary["fetched"] += 1

            candidate = best_ats_candidate(html, careers_url)
            if candidate is None:
                summary["no_candidate"] += 1
                continue
            summary["embeds_found"] += 1

            platform, slug = candidate
            res = promote_from_careers_link(
                conn,
                company_id,
                platform,
                slug,
                page_url=careers_url,
                config=config,
                reenable_scan=True,
            )
            outcome = res.get("outcome")
            if outcome == "promoted":
                summary["promoted"] += 1
                logger.info(
                    "reprobe promoted company_id=%d (%s) -> %s/%s",
                    company_id,
                    row["name_raw"],
                    platform,
                    slug[:60],
                )
            elif outcome == "verify_failed":
                summary["verify_failed"] += 1
            elif outcome == "slug_collision":
                summary["slug_collision"] += 1
            elif outcome == "skipped_already_hit":
                summary["skipped_already_hit"] += 1

            if delay:
                time.sleep(delay)

    logger.info("reprobe_custom_miss_cohort summary: %s", summary)
    return summary
