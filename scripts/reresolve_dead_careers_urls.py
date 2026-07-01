#!/usr/bin/env python3
"""Re-resolve dead careers URLs for companies with stale careers_url entries.

A dead careers URL (404/410/5xx, redirect to non-careers page, or parked domain)
guarantees a miss no matter how good the extractors are. This script:

1. Derives the dead cohort from a LIVE sweep — queries companies with a careers_url,
   HEAD/GET each, and classifies DEAD = 4xx/5xx, redirect-to-non-careers-page, or
   parked-domain. Does NOT trust a stale flag; does NOT hardcode a company list.
2. Re-resolves from each dead company's homepage_url: re-discovers the current
   careers URL by reusing the existing careers-crawler discovery
   (_ats_link_discovery.best_ats_candidate) and ats_reprobe._find_openings_link.
3. Re-detects after re-resolution: runs extract_ats_from_url_best on the freshly-
   found URL and records the detected platform when found.
4. Records honestly: companies with no findable live careers page get
   miss_reason='careers_url_dead_unresolvable'. Never leaves a misleading null.

Usage:
    uv run python scripts/reresolve_dead_careers_urls.py [--db jobs.db] [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

# Ensure repo root on path for direct script invocation.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from job_finder.json_utils import utc_now_iso
from job_finder.web.ats_detection import extract_ats_from_url_best
from job_finder.web.ats_reprobe import _find_openings_link
from job_finder.web.careers_crawler._ats_link_discovery import best_ats_candidate
from job_finder.web.db_helpers import standalone_connection
from job_finder.web.http_fetch import fetch_with_deadline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("reresolve_dead_careers_urls")

# Constants
_FETCH_TIMEOUT = 10
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _USER_AGENT, "Accept": "text/html,*/*"}

# Parked domain signatures — these indicate the domain is for sale or
# parked, not a live careers page.
_PARKED_SIGNATURES = [
    "this domain is for sale",
    "domain for sale",
    "buy this domain",
    "this domain is parked",
    "parked domain",
    "coming soon",
    "under construction",
    "this page is parked",
]


def _is_parked_domain(html: str) -> bool:
    """Check if HTML indicates a parked/for-sale domain."""
    if not html:
        return False
    html_lower = html.lower()
    return any(sig in html_lower for sig in _PARKED_SIGNATURES)


def _is_careers_page(url: str, html: str | None) -> bool:
    """Check if a URL looks like a careers/jobs page, not a homepage.

    This is the adversarial guard: a redirect to the homepage must NOT be
    accepted as a valid careers_url. Precedence:

    1. A known ATS URL (host + path encode the board identity) is definitionally
       a jobs board — accept regardless of page chrome. This covers subdomain
       boards like ``{slug}.recruitee.com`` whose path is only ``/``.
    2. A careers-y URL path (``/careers``, ``/jobs``, ...) is accepted.
    3. A bare homepage/root path is REJECTED even when the page chrome mentions
       "careers" — a header/footer "Careers" nav link is exactly what a dead
       careers URL redirecting to the homepage looks like. This is the keystone
       false-positive this guard exists to kill.
    4. Otherwise a non-root page is accepted only on a strong jobs-listing phrase
       in the body. A bare "careers" substring is deliberately excluded — it
       appears in virtually every site's nav and would re-open case (3).
    """
    if not url:
        return False

    # (1) A known ATS URL is a jobs board by construction.
    if extract_ats_from_url_best(url):
        return True

    parsed = urlparse(url)
    path = parsed.path.lower().rstrip("/")

    # (2) Careers-related URL path.
    careers_patterns = ["/careers", "/jobs", "/openings", "/positions", "/join"]
    if any(pattern in path for pattern in careers_patterns):
        return True

    # (3) Bare homepage/root — reject regardless of chrome keywords.
    if path in ("", "/"):
        return False

    # (4) Non-root page: require a strong jobs-listing phrase in the body.
    if html:
        job_keywords = [
            "job opening",
            "open position",
            "we're hiring",
            "we are hiring",
            "job listing",
        ]
        html_lower = html.lower()
        return any(keyword in html_lower for keyword in job_keywords)

    return False


def _check_careers_url_liveness(url: str) -> tuple[bool, str, str | None]:
    """Check if a careers URL is live and valid.

    Returns:
        (is_live, status_category, html_or_None)
        is_live: True if the URL is live and looks like a careers page
        status_category: 'live', 'dead_4xx', 'dead_5xx', 'redirect_to_homepage', 'parked'
        html_or_None: HTML content if fetched successfully, else None
    """
    if not url:
        return False, "no_url", None

    try:
        resp = fetch_with_deadline(
            url,
            getter=requests.get,
            timeout=_FETCH_TIMEOUT,
            allow_redirects=True,
            headers=_HEADERS,
        )
    except Exception as exc:
        log.debug("Liveness check failed for %s: %s", url, exc)
        return False, "error", None

    status = resp.status_code
    html = resp.text if resp.text else None

    # Check for parked domain
    if html and _is_parked_domain(html):
        return False, "parked", html

    # Check HTTP status
    if 200 <= status < 300:
        # Check if it's actually a careers page (not homepage redirect)
        if _is_careers_page(resp.url, html):
            return True, "live", html
        else:
            return False, "redirect_to_homepage", html
    elif 400 <= status < 500:
        return False, "dead_4xx", html
    elif 500 <= status < 600:
        return False, "dead_5xx", html
    else:
        return False, f"other_{status}", html


def _discover_careers_url_from_homepage(homepage_url: str) -> str | None:
    """Discover a careers URL from a company homepage.

    Reuses existing discovery logic:
    1. Try to find an ATS link via best_ats_candidate and reconstruct the URL
    2. Fall back to _find_openings_link for generic job listings links

    Returns:
        The discovered careers URL, or None if no valid careers page found.
    """
    if not homepage_url:
        return None

    try:
        resp = fetch_with_deadline(
            homepage_url,
            getter=requests.get,
            timeout=_FETCH_TIMEOUT,
            allow_redirects=True,
            headers=_HEADERS,
        )
    except Exception as exc:
        log.debug("Failed to fetch homepage %s: %s", homepage_url, exc)
        return None

    if resp.status_code != 200 or not resp.text:
        return None

    html = resp.text

    # First try: look for embedded ATS links and reconstruct URLs
    ats_candidate = best_ats_candidate(html, homepage_url)
    if ats_candidate:
        platform, slug = ats_candidate
        # Reconstruct the URL based on the platform
        if platform == "lever":
            reconstructed_url = f"https://jobs.lever.co/{slug}"
        elif platform == "greenhouse":
            reconstructed_url = f"https://boards.greenhouse.io/{slug}"
        elif platform == "ashby":
            reconstructed_url = f"https://jobs.ashbyhq.com/{slug}"
        elif platform == "workable":
            reconstructed_url = f"https://apply.workable.com/{slug}"
        elif platform == "recruitee":
            reconstructed_url = f"https://{slug}.recruitee.com"
        elif platform == "breezy":
            reconstructed_url = f"https://{slug}.breezy.hr"
        elif platform == "jazzhr":
            reconstructed_url = f"https://{slug}.applytojob.com"
        elif platform == "pinpoint":
            reconstructed_url = f"https://{slug}.pinpointhq.com"
        elif platform == "personio":
            reconstructed_url = f"https://{slug}.jobs.personio.com"
        elif platform == "bamboohr":
            reconstructed_url = f"https://{slug}.bamboohr.com"
        elif platform == "teamtailor":
            reconstructed_url = f"https://{slug}.teamtailor.com"
        elif platform == "jobvite":
            reconstructed_url = f"https://jobs.jobvite.com/{slug}"
        elif platform == "rippling":
            reconstructed_url = f"https://ats.rippling.com/{slug}"
        else:
            # For platforms with complex URL patterns, fall back to openings link
            reconstructed_url = None

        if reconstructed_url:
            # Verify the reconstructed URL is actually a careers page
            is_live, status, _ = _check_careers_url_liveness(reconstructed_url)
            if is_live and status == "live":
                return reconstructed_url

    # Second try: find the strongest job-listings link
    openings_link = _find_openings_link(html, homepage_url)
    if openings_link:
        # Verify the link is actually a careers page
        is_live, status, _ = _check_careers_url_liveness(openings_link)
        if is_live and status == "live":
            return openings_link

    return None


def _process_company(
    conn: sqlite3.Connection,
    company_id: int,
    company_name: str,
    careers_url: str | None,
    homepage_url: str | None,
    dry_run: bool,
) -> dict[str, Any]:
    """Process a single company: check careers URL liveness and re-resolve if dead.

    Returns a summary dict with the outcome.
    """
    summary = {
        "company_id": company_id,
        "company_name": company_name,
        "old_careers_url": careers_url,
        "new_careers_url": None,
        "detected_platform": None,
        "outcome": None,
    }

    if not careers_url:
        summary["outcome"] = "no_careers_url"
        return summary

    # Step 1: Check liveness of current careers_url
    is_live, status_category, _html = _check_careers_url_liveness(careers_url)

    if is_live and status_category == "live":
        summary["outcome"] = "already_live"
        return summary

    log.info(
        "Company %s (%s): careers_url is %s",
        company_name,
        company_id,
        status_category,
    )

    # Step 2: Try to re-resolve from homepage
    if not homepage_url:
        summary["outcome"] = "no_homepage_url"
        if not dry_run:
            conn.execute(
                "UPDATE companies SET miss_reason = ?, updated_at = ? WHERE id = ?",
                ("careers_url_dead_unresolvable", utc_now_iso(), company_id),
            )
            conn.commit()
        return summary

    new_url = _discover_careers_url_from_homepage(homepage_url)
    if not new_url:
        summary["outcome"] = "reresolution_failed"
        if not dry_run:
            conn.execute(
                "UPDATE companies SET miss_reason = ?, updated_at = ? WHERE id = ?",
                ("careers_url_dead_unresolvable", utc_now_iso(), company_id),
            )
            conn.commit()
        return summary

    # Step 3: Verify the new URL is actually a careers page
    is_new_live, new_status, _ = _check_careers_url_liveness(new_url)
    if not is_new_live or new_status != "live":
        summary["outcome"] = "new_url_not_live"
        if not dry_run:
            conn.execute(
                "UPDATE companies SET miss_reason = ?, updated_at = ? WHERE id = ?",
                ("careers_url_dead_unresolvable", utc_now_iso(), company_id),
            )
            conn.commit()
        return summary

    # Step 4: Re-detect ATS platform from the new URL
    detected = extract_ats_from_url_best(new_url)
    detected_platform = detected[0] if detected else None

    summary["new_careers_url"] = new_url
    summary["detected_platform"] = detected_platform
    summary["outcome"] = "reresolved"

    if not dry_run:
        # Update careers_url and clear stale state
        conn.execute(
            "UPDATE companies SET careers_url = ?, miss_reason = NULL, updated_at = ? WHERE id = ?",
            (new_url, utc_now_iso(), company_id),
        )
        # If we detected a platform, update ats_platform and ats_probe_status
        if detected_platform:
            # Extract slug from the detection result
            slug = detected[1] if detected else None
            conn.execute(
                """UPDATE companies
                   SET ats_platform = ?, ats_slug = ?, ats_probe_status = 'hit',
                       ats_probe_attempted_at = ?, updated_at = ?
                   WHERE id = ?""",
                (detected_platform, slug, utc_now_iso(), utc_now_iso(), company_id),
            )
        conn.commit()
        log.info(
            "Re-resolved %s (%s): %s -> %s (platform: %s)",
            company_name,
            company_id,
            careers_url,
            new_url,
            detected_platform or "custom",
        )

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Re-resolve dead careers URLs for companies with stale entries."
    )
    parser.add_argument("--db", default="jobs.db", help="Path to jobs.db")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check and log without writing to DB",
    )
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    if not db_path.exists():
        log.error("DB not found: %s", db_path)
        return 2

    summary = {
        "total_companies": 0,
        "already_live": 0,
        "reresolved": 0,
        "reresolution_failed": 0,
        "no_homepage_url": 0,
        "no_careers_url": 0,
        "new_url_not_live": 0,
    }

    with standalone_connection(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row

        # Query all companies with a careers_url
        rows = conn.execute(
            """SELECT id, name_raw, careers_url, homepage_url
               FROM companies
               WHERE careers_url IS NOT NULL AND careers_url != ''
               ORDER BY id"""
        ).fetchall()

        summary["total_companies"] = len(rows)
        log.info("Checking %d companies for dead careers URLs", len(rows))

        for row in rows:
            company_id = row["id"]
            company_name = row["name_raw"]
            careers_url = row["careers_url"]
            homepage_url = row["homepage_url"]

            result = _process_company(
                conn,
                company_id,
                company_name,
                careers_url,
                homepage_url,
                args.dry_run,
            )

            outcome = result["outcome"]
            if outcome in summary:
                summary[outcome] += 1
            else:
                summary[outcome] = summary.get(outcome, 0) + 1

    log.info("Summary: %s", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
