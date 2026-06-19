"""PlatformScanner registry + shared scan driver.

A ``PlatformScanner`` value object captures everything per-platform that
changes between Lever / Greenhouse / Ashby / etc.: how to fetch the
posting list, how to extract the title, and how to turn one raw posting
into the canonical job dict. The driver (``run_platform_scan``) owns the
title-match gate and the final result-count log line that every
historical ``scan_*`` function used to emit.

The shared HTTP helper ``_http_get_json`` consolidates the
GET → status-200 → JSON-parse spine that every simple-shape scanner
duplicates. It supports a single timeout retry (used by Ashby) and
optional ``params`` / ``headers``. Platforms with shapes the helper
cannot express (Workday POST + pagination, Personio XML + multi-TLD,
BambooHR HTML) own their own HTTP inside ``fetch_postings``.

Tests intercept HTTP via ``patch("...requests.get"|"requests.post", ...)``
on any module in the import graph — ``requests`` is a singleton, and
this module imports it eagerly so module-qualified ``requests.get(...)``
calls here pick up the patch the same as the historical ``scan_*`` body.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import requests

from job_finder.web.ats_prober import _PROBE_TIMEOUT

logger = logging.getLogger(__name__)


# ── Structured-field CAPTURE helpers (#451) ──────────────────────────────────
# Shared raw-as-provided extraction for the is_remote / employment_type /
# department capture columns. Both helpers are pure and return None when the
# value is absent — capture never synthesizes a value the payload does not
# carry (epic #393, CAPTURE stage).


def coerce_remote_bool(value: Any) -> bool | None:
    """Coerce a provider ``isRemote`` / ``remote`` value to a tri-state bool.

    ``None`` (field absent) stays ``None`` — distinct from an explicit
    ``False`` — so the NULL-is-unknown semantics of the ``is_remote`` column
    survive. Any present value is coerced with ``bool()``.
    """
    if value is None:
        return None
    return bool(value)


def label_or_str(value: Any) -> str | None:
    """Extract a raw string from a provider field that may be an object.

    SmartRecruiters emits ``typeOfEmployment`` / ``department`` as
    ``{"id": ..., "label": ...}`` objects; Ashby / Lever emit plain strings.
    Returns the ``label`` for dict inputs, the string itself for str inputs,
    and ``None`` for anything empty or absent.
    """
    if isinstance(value, dict):
        label = value.get("label")
        return label or None
    if isinstance(value, str):
        return value or None
    return None


@dataclass(frozen=True, slots=True)
class PlatformScanner:
    """Per-platform contract for the shared scan driver.

    Attributes:
        name: Lowercase platform key matching ``companies.ats_platform``
            (e.g. ``"lever"``, ``"greenhouse"``). Used in log messages.
        company_source: Display-cased platform name written into the
            ``company_source`` field of each job dict (e.g. ``"Lever"``).
        fetch_postings: ``slug -> list[dict]``. Owns all HTTP + pagination +
            response-format-specific parsing. Must catch its own exceptions
            and return ``[]`` on any error so one platform's outage cannot
            crash a whole multi-company scan.
        title_of: ``posting -> str``. Pulls the title string out of one
            raw posting dict for the title-match gate.
        posting_to_job: ``(posting, slug) -> dict | None``. Builds the
            canonical job dict ``{title, company_source, location,
            description, source_url, salary_min, salary_max, comp_json}``
            for one posting. Returning ``None`` skips the posting (e.g.
            BambooHR's "anchor missing" case).
    """

    name: str
    company_source: str
    fetch_postings: Callable[[str], list[dict]]
    title_of: Callable[[dict], str]
    posting_to_job: Callable[[dict, str], dict | None]


def run_platform_scan(
    scanner: PlatformScanner,
    slug: str,
    target_titles: list[str],
    exclusions: list[str],
    *,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    """Run one platform scan: fetch → raw capture → title gate → normalize → log.

    The behavior matches the historical per-platform ``scan_*`` body:
    every raw posting that ``_title_matches`` accepts is normalized via
    ``scanner.posting_to_job`` and appended to the result list. The
    debug-level count log fires once at the end with the same shape the
    Lever / Greenhouse / Ashby / Pinpoint scanners already used.

    Args:
        scanner: The platform's ``PlatformScanner`` value object.
        slug: Per-company platform identifier (e.g. Lever's
            ``"stripe"``, Workday's ``"walmart.wd5/WalmartExternal"``).
        target_titles: Title-match keywords for inclusion. Empty list
            allows all titles through (the config layer is expected to
            forbid this; the gate respects it for completeness).
        exclusions: Title-match keywords for exclusion. AND-NOT semantics.
        conn: Optional DB connection.  When provided the raw pre-filter
            API response is recorded via ``record_extraction`` with
            ``detect=True`` so an empty response on a previously-productive
            platform is detected as a true break.  The ~19 callers in
            ``ats_platforms/__init__.py`` and ``ats_reconciler.py`` omit
            this argument (``conn=None``) and are unaffected.

    Returns:
        Canonical job dicts for matched postings. Empty list on fetch
        error or no matches.
    """
    # Lazy import — once ats_platforms.py's scan_X bodies delegate to this
    # driver (F1 Commit 2), the import graph becomes
    # ats_platforms -> _registry -> ats_platforms. A module-level
    # ``from ats_platforms import _title_matches`` would race that cycle;
    # the function-local import resolves only after ats_platforms is
    # fully loaded and is cheap because Python caches the module lookup.
    from job_finder.web.ats_platforms import _title_matches

    postings = list(scanner.fetch_postings(slug))

    # --- Autoheal Phase B: capture raw pre-filter API response ---
    # detect=True is honest here: len(postings)==0 on a platform that
    # previously returned jobs is a genuine API break (shape changed,
    # auth expired, …), not a post-filter false-alarm.
    if conn is not None:
        try:
            from job_finder.web.autoheal import MIN_MEANINGFUL_LEN
            from job_finder.web.autoheal.health_monitor import record_extraction

            raw = json.dumps(postings)[:50000]
            # The health-monitor's MIN_MEANINGFUL_LEN gate was designed for
            # email bodies where a very short body is a meta/empty email.
            # For ATS, any API response — including [] — is a meaningful
            # result.  Pad to the threshold so genuine empty-API breaks on
            # a previously-productive platform actually fire the break counter.
            if len(raw) < MIN_MEANINGFUL_LEN:
                raw = raw.ljust(MIN_MEANINGFUL_LEN)

            record_extraction(
                conn,
                f"ats:{scanner.name}",
                "ats",
                raw,
                job_count=len(postings),
                detect=True,
            )
        except Exception:
            pass  # observability must never break ingestion

    results: list[dict] = []
    for posting in postings:
        title = scanner.title_of(posting)
        if not _title_matches(title, target_titles, exclusions):
            continue
        job_dict = scanner.posting_to_job(posting, slug)
        if job_dict is not None:
            results.append(job_dict)

    logger.debug(
        "scan_%s('%s'): %d postings fetched, %d matched",
        scanner.name,
        slug,
        len(postings),
        len(results),
    )
    return results


def _http_get_json(
    url: str,
    log_label: str,
    slug: str,
    *,
    retry_on_timeout: bool = False,
    params: dict | None = None,
    headers: dict | None = None,
) -> Any:
    """GET + 200-check + JSON-parse, with optional single timeout retry.

    Replaces the GET → status check → ``resp.json()`` try/except spine
    that every simple-shape scanner duplicates. Returns the parsed JSON
    on success, ``None`` on any failure (connection error, timeout,
    non-200, JSON parse error). Callers turn ``None`` into ``[]``.

    The ``retry_on_timeout`` knob exists for Ashby: a 2026-05-26 incident
    showed Ashby returning Read timeouts for ~20 tenants in sequence over
    a 9-minute window. A fresh attempt 2s later typically succeeds. One
    retry is enough; more would double the run time of a sustained
    outage with no benefit.

    Args:
        url: Target URL.
        log_label: Per-scanner label for warning/debug log lines
            (e.g. ``"scan_lever"``).
        slug: Per-company identifier; included in log lines.
        retry_on_timeout: When True, swallow a single
            ``requests.exceptions.Timeout`` and retry once after 2 s.
        params: Optional query parameters passed to ``requests.get``.
        headers: Optional request headers passed to ``requests.get``.

    Returns:
        Parsed JSON value (dict, list, etc.) on success; ``None`` on any
        failure path.
    """
    resp = None
    for attempt in (1, 2):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=_PROBE_TIMEOUT)
            break
        except requests.exceptions.Timeout as exc:
            if retry_on_timeout and attempt == 1:
                logger.debug("%s('%s') timed out attempt 1, retrying in 2s", log_label, slug)
                time.sleep(2)
                continue
            logger.warning("%s('%s') timed out: %s", log_label, slug, exc)
            return None
        except Exception as exc:
            logger.warning("%s('%s') request failed: %s", log_label, slug, exc)
            return None

    if resp is None:
        return None

    if resp.status_code != 200:
        logger.debug("%s('%s') returned HTTP %d", log_label, slug, resp.status_code)
        return None

    try:
        return resp.json()
    except Exception as exc:
        logger.warning("%s('%s') JSON parse error: %s", log_label, slug, exc)
        return None
