"""Company-batched primary-source resolver (scheduled stage, Phase 3).

Every aggregator-discovered job at a company with a verified ATS board gets a
resolution attempt — decoupled from whether enrichment needed to run (closes
the G2 coverage leak), across the full PlatformScanner registry (closes G3's
3-platform limit), at one board fetch per company per run instead of one per
job.

Pipeline per run:
  1. Free promotion — a job whose source_urls already contain an ATS/careers
     link gets it as a strict direct_url. No network, no attempt consumed.
  2. Company-batched board match — candidates (direct_url IS NULL, company
     ats_probe_status='hit', attempts under the cap or past the decay window,
     not expired/closed) are grouped by company; each company's board is
     fetched ONCE via the PlatformScanner registry, and every candidate job
     is matched in memory via resolve_primary_posting. A strict match merges
     authoritative fields (primary_source_merge); a loose match records the
     link only — the contamination invariant from Phase 2.

Attempt semantics (m092 columns):
  - direct_url_checked_at / direct_url_attempts stamp once per board-match
    attempt via db._direct_link.stamp_direct_url_checks (single writer).
  - An empty board fetch counts as an attempt for all of that company's
    candidates: the registry contract returns [] for both "no postings" and
    "fetch failed", so the two are indistinguishable here. The decay window
    below repairs any attempt burned on a transient outage.
  - Re-eligibility is DECAY-BASED, not transition-hooked: a row past
    max_attempts re-enters candidacy once its checked_at ages past
    recheck_days. The alternative — resetting attempts when ats_probe_status
    flips to 'hit' — would need hooks at ~8 scattered status-write sites
    (ats_prober, ats_scanner._probe, ats_identity_reconcile, _upsert) and
    would still miss slug re-keys/heals on already-hit companies. The decay
    window covers all of those from one place (the candidate SQL) at the
    cost of one bounded re-check per job per window. Note that attempts only
    accrue while a company IS 'hit' (candidacy requires it), so the classic
    deadlock — attempts exhausted before the ATS was even discovered —
    cannot occur.

Company gating is strict (pitfall P2): only ats_probe_status='hit' rows are
consulted; the resolver never probes speculatively, keeping the
speculative-miss cohort's ~29% FP rate quarantined in the probe subsystem.

Runs on its own sqlite3 connection (APScheduler thread; stale_detector
pattern). Careers-page (non-ATS) resolution intentionally stays in the free
enrichment tier: per-job HTML scraping is exactly the N-fetches-per-company
shape this module exists to eliminate.

Config (config.yaml > direct_link.resolver, all optional):
  enabled                  gate consulted by the scheduler wrapper (default true)
  max_attempts             skip rows at this many attempts (default 3)
  recheck_days             decay window for re-eligibility (default 30)
  max_companies_per_run    board-fetch cap per run (default 50)
"""

from __future__ import annotations

import json
import logging
import random
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from job_finder.db._direct_link import set_direct_url, stamp_direct_url_checks
from job_finder.json_utils import utc_now_iso
from job_finder.web.direct_link import promote_existing_direct_url, resolve_primary_posting
from job_finder.web.primary_source_merge import merge_primary_posting_fields

logger = logging.getLogger(__name__)

_DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_RECHECK_DAYS = 30
_DEFAULT_MAX_COMPANIES_PER_RUN = 50

# Unresolved jobs at probe-verified companies, attempt-gated with decay
# re-eligibility. Closed/expired rows are excluded — resolving a dead
# posting's Apply target is wasted board traffic. ISO-8601 naive-UTC strings
# compare correctly as text.
_CANDIDATE_SQL = """
    SELECT j.dedup_key, j.title, j.location, j.company_id,
           c.ats_platform, c.ats_slug
    FROM jobs j
    JOIN companies c ON c.id = j.company_id
    WHERE j.direct_url IS NULL
      AND c.ats_probe_status = 'hit'
      AND c.ats_platform IS NOT NULL
      AND c.ats_slug IS NOT NULL AND c.ats_slug != ''
      AND (COALESCE(j.direct_url_attempts, 0) < ?
           OR COALESCE(j.direct_url_checked_at, '') < ?)
      AND (j.expiry_status IS NULL OR j.expiry_status != 'expired')
      AND (j.pipeline_status IS NULL OR j.pipeline_status NOT IN
           ('archived', 'rejected', 'withdrawn', 'dismissed'))
    ORDER BY j.company_id, j.last_seen DESC
"""


def _resolver_settings(config: dict) -> dict:
    section = (config.get("direct_link") or {}).get("resolver") or {}
    return {
        "enabled": bool(section.get("enabled", True)),
        "max_attempts": int(section.get("max_attempts", _DEFAULT_MAX_ATTEMPTS)),
        "recheck_days": int(section.get("recheck_days", _DEFAULT_RECHECK_DAYS)),
        "max_companies_per_run": int(
            section.get("max_companies_per_run", _DEFAULT_MAX_COMPANIES_PER_RUN)
        ),
    }


def _parse_source_urls(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [u for u in raw if isinstance(u, str)]
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [u for u in parsed if isinstance(u, str)] if isinstance(parsed, list) else []


def _promote_existing(conn: sqlite3.Connection, stats: dict) -> None:
    """Stage 1: promote source_urls already on an ATS/careers host (free)."""
    rows = conn.execute(
        "SELECT dedup_key, source_urls FROM jobs WHERE direct_url IS NULL"
    ).fetchall()
    for row in rows:
        stats["scanned"] += 1
        promoted = promote_existing_direct_url(_parse_source_urls(row["source_urls"]))
        if promoted and set_direct_url(conn, row["dedup_key"], promoted, "strict"):
            stats["promoted"] += 1
            stats["resolved"] += 1
            stats["strict"] += 1


def resolve_primary_sources(
    conn: sqlite3.Connection,
    config: dict,
    *,
    max_companies: int | None = None,
    delay_range: tuple[float, float] = (1.0, 2.0),
) -> dict:
    """Run one resolution pass. Returns counters for activity logging.

    Keys: scanned (NULL-direct_url rows examined for promotion), promoted,
    companies_scanned, companies_skipped (platform without a public API /
    unknown — no attempt burned), jobs_checked (board-match attempts),
    resolved, strict, loose, merged (strict matches whose fields folded in).
    """
    settings = _resolver_settings(config)
    if max_companies is None:
        max_companies = settings["max_companies_per_run"]
    now = utc_now_iso()
    decay_cutoff = (
        datetime.now(UTC).replace(tzinfo=None) - timedelta(days=settings["recheck_days"])
    ).isoformat()

    stats = {
        "scanned": 0,
        "promoted": 0,
        "companies_scanned": 0,
        "companies_skipped": 0,
        "jobs_checked": 0,
        "resolved": 0,
        "strict": 0,
        "loose": 0,
        "merged": 0,
    }

    _promote_existing(conn, stats)

    candidates = conn.execute(_CANDIDATE_SQL, (settings["max_attempts"], decay_cutoff)).fetchall()
    by_company: dict[int, dict] = {}
    for row in candidates:
        group = by_company.setdefault(
            row["company_id"],
            {"platform": row["ats_platform"], "slug": row["ats_slug"], "jobs": []},
        )
        group["jobs"].append(row)

    # Deferred import: the platform package pulls in the scanner modules and
    # requests; the scheduler only pays that cost when the job actually runs.
    from job_finder.web.ats_platforms import NON_SCANNABLE_PLATFORMS, SCANNERS_BY_NAME
    from job_finder.web.ats_platforms._registry import run_platform_scan

    fetched_any = False
    for _company_id, group in list(by_company.items())[:max_companies]:
        scanner = SCANNERS_BY_NAME.get(group["platform"])
        if scanner is None or group["platform"] in NON_SCANNABLE_PLATFORMS:
            # No public API (e.g. jobvite) or registry drift — these jobs can
            # never resolve via a board fetch, so no attempt is burned.
            stats["companies_skipped"] += 1
            continue

        if fetched_any and delay_range[1] > 0:
            # S311: politeness jitter between board fetches needs no CSPRNG.
            time.sleep(random.uniform(*delay_range))  # noqa: S311
        fetched_any = True

        # Empty target_titles lets every posting through the registry's title
        # gate — one fetch serves all of this company's candidates, and
        # matching happens in-process below.
        postings = run_platform_scan(scanner, group["slug"], [], [])
        stats["companies_scanned"] += 1

        checked: list[str] = []
        for job in group["jobs"]:
            checked.append(job["dedup_key"])
            stats["jobs_checked"] += 1
            if not postings:
                continue
            resolved = resolve_primary_posting(postings, job["title"] or "", job["location"] or "")
            if resolved is None:
                continue
            posting, url, confidence = resolved
            if set_direct_url(conn, job["dedup_key"], url, confidence):
                stats["resolved"] += 1
                stats["strict" if confidence == "strict" else "loose"] += 1
            # Strict match only: fold the posting's authoritative fields in.
            # merge_primary_posting_fields never raises (logs and returns
            # False), so one bad posting cannot abort the run.
            if posting is not None and merge_primary_posting_fields(
                conn, {"dedup_key": job["dedup_key"]}, posting
            ):
                stats["merged"] += 1

        stamp_direct_url_checks(conn, checked, now)

    logger.info("resolve_primary_sources: %s", stats)
    return stats


def run_primary_source_resolution(db_path: str, config: dict) -> dict:
    """Scheduler entry point — own connection (APScheduler thread safety)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return resolve_primary_sources(conn, config)
    finally:
        conn.close()
