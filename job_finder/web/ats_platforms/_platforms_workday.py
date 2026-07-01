"""Workday platform scanner (registry form).

Workday exposes a standardized POST JSON API across all tenants at
``/wday/cxs/{tenant}/{board}/jobs``. Slug format is ``"{subdomain}/{board}"``
(e.g. ``"walmart.wd5/WalmartExternal"``).

Per-job description requires a secondary GET against the detail
endpoint. ``_fetch_workday_description`` lives in ``ats_platforms.py``
because it is imported directly by ``tests/test_workday_scanner.py``;
this module calls it via a lazy import to avoid a circular dependency.

Layer-1 emission (Phase 48.02):
  - ``source_id``: the posting's ``externalPath`` (unique per job per
    Workday board; the Workday requisition ID is embedded in the path,
    e.g. ``"/job/Senior-Data-Scientist_R-12345"``). Using ``externalPath``
    rather than attempting to parse ``bulletFields`` array entries avoids
    reliance on tenant-specific field names while still providing a stable
    per-posting identifier.
  - ``posted_date``: parsed from ``postedOn`` (date string, typically
    ``"MM/DD/YYYY"`` or ISO ``"YYYY-MM-DD"``). Treated as UTC midnight.
  - ``locations_structured``: Layer-1 ``JobLocation`` list parsed from
    ``locationsText`` with workplace-type detection (REMOTE/HYBRID) and
    best-effort ``City, ST`` extraction for US addresses.
"""

from __future__ import annotations

import logging
import re
import time
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta

import requests

from job_finder.web.ats_platforms._registry import (
    BOARD_GONE_STATUSES,
    BoardGoneError,
    PlatformScanner,
    _auth_block_statuses,
    coerce_remote_bool,
    label_or_str,
)
from job_finder.web.ats_prober import _PROBE_TIMEOUT
from job_finder.web.location_canonical import JobLocation, WorkplaceType, dedupe_locations

logger = logging.getLogger(__name__)

_PAGE_SIZE = 20
# Per-board page budget. At _PAGE_SIZE=20 the default of 100 pages covers
# boards up to 2,000 postings before discovery is marked incomplete. Tenants
# larger than the budget still return their first ``budget * _PAGE_SIZE``
# postings (so discovery is non-empty) with ``complete=False`` — the
# reconciler's completeness gate then declines expiry-reconciliation for that
# tenant, but discovery is no longer silently zeroed.  Tunable via
# ``config.ats.workday_max_pages`` threaded through ``run_ats_scan`` /
# ``reconcile_all_companies`` (issue #216).
_DEFAULT_MAX_PAGES = 100
_DETAIL_FETCH_SLEEP_S = 0.1

# Per-run page-budget override, set by the scan / reconcile entry points from
# ``config.ats.workday_max_pages`` and consumed by ``_fetch_postings`` (the
# registry's ``slug -> list`` contract leaves no room for an explicit arg).
# A ContextVar (not a module global) keeps the override thread-local so the
# APScheduler reconcile thread and a concurrent Flask-triggered scan cannot
# clobber each other's budget.
_max_pages_override: ContextVar[int | None] = ContextVar(
    "workday_max_pages_override", default=None
)


def set_max_pages(max_pages: int | None) -> object:
    """Set the per-run Workday page budget; returns the reset token.

    Pass the resolved ``config.ats.workday_max_pages`` value at the top of a
    scan / reconcile run, then ``reset_max_pages(token)`` in a ``finally`` so
    the override never leaks past the run. ``None`` (or a non-positive value)
    falls back to ``_DEFAULT_MAX_PAGES``.
    """
    return _max_pages_override.set(max_pages)


def reset_max_pages(token: object) -> None:
    """Restore the page-budget ContextVar to its prior value."""
    _max_pages_override.reset(token)  # type: ignore[arg-type]


def _resolve_max_pages(max_pages: int | None) -> int:
    """Pick the effective page budget: explicit arg → ContextVar → default."""
    candidate = max_pages if max_pages is not None else _max_pages_override.get()
    if candidate is None or candidate <= 0:
        return _DEFAULT_MAX_PAGES
    return candidate


# Pacing for the LIST endpoint between successive page fetches. Pre-F1
# (commit b99e1d9) the list-endpoint cadence was incidentally paced by
# the per-matched-posting detail-fetch sleep in the same per-page loop.
# Restoring an explicit inter-page delay preserves the polite-pacing
# intent for high-page-count Workday tenants. See
# .planning/specs/2026-05-26-polish-review-audit.md (MAJOR — Workday +
# SmartRecruiters pagination).
_PAGE_FETCH_SLEEP_S = 0.1

# ---------------------------------------------------------------------------
# Location-parsing helpers (Layer-1)
# ---------------------------------------------------------------------------

_REMOTE_RE = re.compile(r"\bremote\b", re.IGNORECASE)
_HYBRID_RE = re.compile(r"\bhybrid\b", re.IGNORECASE)

# "City Name, XX" where XX is a 2-letter code (US state or CA province).
# Anchored so "Hybrid - San Francisco, CA" doesn't match the raw text;
# callers strip workplace-type prefixes before applying this pattern.
_US_CITY_STATE_RE = re.compile(r"^([A-Za-z][A-Za-z\s\-\.\']+),\s*([A-Z]{2})\s*$")

# Tokens that are purely workplace-type keywords (handled specially).
_WORKPLACE_ONLY_TOKENS: frozenset[str] = frozenset({"remote", "hybrid", "onsite", "on-site"})

# Prefixes that Workday sometimes prepends to a city ("Hybrid - City, ST").
_WORKPLACE_PREFIX_RE = re.compile(r"^(?:remote|hybrid|onsite|on-site)\s*[-–—]\s*", re.IGNORECASE)


def _detect_workplace_type(text: str) -> WorkplaceType:
    """Infer WorkplaceType from a location token string."""
    if _REMOTE_RE.search(text):
        return "REMOTE"
    if _HYBRID_RE.search(text):
        return "HYBRID"
    return "UNSPECIFIED"


def _to_canonical(posting: dict) -> list[JobLocation]:
    """Layer-1 mapping: Workday posting → list[JobLocation].

    Parses ``locationsText`` (a flat semicolon/pipe-separated string) into
    ``JobLocation`` objects. Each segment is:
      - A pure workplace-type keyword (``"Remote"``, ``"Hybrid"``) →
        workplace-type-only ``JobLocation`` with ``unresolved=True``.
      - A ``"City, ST"`` US pattern (after stripping any leading keyword
        prefix) → fully-structured ``JobLocation`` with ``unresolved=False``.
      - Anything else → ``unresolved=True`` preserving ``raw``.

    Multi-location postings (e.g. ``"New York, NY; Remote"``) produce one
    ``JobLocation`` per resolved segment. Duplicates are removed by
    ``dedupe_locations``.
    """
    locations_text = (posting.get("locationsText") or "").strip()
    if not locations_text:
        return []

    # Split on semicolons and pipes (Workday uses both as multi-location
    # separators depending on tenant configuration).
    segments = [s.strip() for s in re.split(r"[;|]", locations_text) if s.strip()]

    results: list[JobLocation] = []
    for segment in segments:
        workplace_type = _detect_workplace_type(segment)

        # Pure keyword segments — no city/region data to extract.
        if segment.lower() in _WORKPLACE_ONLY_TOKENS:
            results.append(
                JobLocation(
                    city=None,
                    region=None,
                    region_code=None,
                    country=None,
                    country_code=None,
                    workplace_type=workplace_type,
                    raw=segment,
                    unresolved=True,
                )
            )
            continue

        # Strip any leading workplace-type prefix before trying city parse.
        clean = _WORKPLACE_PREFIX_RE.sub("", segment).strip()

        m = _US_CITY_STATE_RE.match(clean)
        if m:
            city = m.group(1).strip()
            region_code = m.group(2).upper()
            results.append(
                JobLocation(
                    city=city,
                    region=None,
                    region_code=region_code,
                    country="United States",
                    country_code="US",
                    workplace_type=workplace_type,
                    raw=segment,
                    unresolved=False,
                )
            )
        else:
            # Can't structurally resolve — preserve raw for audit/display.
            results.append(
                JobLocation(
                    city=None,
                    region=None,
                    region_code=None,
                    country=None,
                    country_code=None,
                    workplace_type=workplace_type,
                    raw=segment,
                    unresolved=True,
                )
            )

    return dedupe_locations(results)


# Relative postedOn strings — what most real tenants emit (#364). At audit
# time (2026-06-11) 734 of the last 30 days' Workday jobs had NULL
# posted_date because only the two absolute formats below were recognised.
# "30+" parses as a 30-day floor: genuinely lossy, still a useful
# "not fresh" signal.
_RELATIVE_POSTED_RE = re.compile(
    r"^(?:posted\s+)?(?:(today)|(yesterday)|(\d+)\+?\s+days?\s+ago)$",
    re.IGNORECASE,
)


def _parse_posted_date(value: str | None) -> tuple[datetime | None, str | None]:
    """Parse a Workday ``postedOn`` string to ``(naive UTC datetime, precision)``.

    Formats seen across Workday tenants:
      - ``"MM/DD/YYYY"`` / ``"YYYY-MM-DD"`` absolute dates → ``'exact'``
      - ``"Posted Today"`` / ``"Posted Yesterday"`` /
        ``"Posted N Days Ago"`` / ``"Posted 30+ Days Ago"`` relative
        strings → date-level value computed against UTC now, ``'approximate'``
        (#364). This parses what the platform actually said — it is NOT
        synthesis from first_seen (D-08).

    Anything else (or empty) → ``(None, None)``; a NULL is written to
    ``posted_date``.
    """
    if not value:
        return None, None
    text = value.strip()
    # ISO date: "YYYY-MM-DD"
    try:
        return datetime.strptime(text, "%Y-%m-%d"), "exact"
    except ValueError:
        pass
    # US date: "MM/DD/YYYY"
    try:
        return datetime.strptime(text, "%m/%d/%Y"), "exact"
    except ValueError:
        pass
    m = _RELATIVE_POSTED_RE.match(text)
    if m:
        today_m, yesterday_m, n_days = m.groups()
        days = 0 if today_m else 1 if yesterday_m else int(n_days)
        utc_today = datetime.now(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=None
        )
        return utc_today - timedelta(days=days), "approximate"
    logger.debug("scan_workday: unrecognised postedOn format %r — skipping", value)
    return None, None


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------


def _fetch_postings_with_completeness(
    slug: str, max_pages: int | None = None
) -> tuple[list[dict], bool]:
    """POST + paginate over Workday CXS list endpoint, tracking completeness.

    Pagination runs up to a **page budget** (``max_pages``, default
    :data:`_DEFAULT_MAX_PAGES`). Boards larger than the budget still return
    the first ``max_pages * _PAGE_SIZE`` postings — discovery is never
    silently zeroed for a large tenant (issue #216) — but with
    ``complete=False`` so the reconciler declines expiry-reconciliation.

    Returns ``(postings, complete)`` where ``complete`` is ``True`` only
    when the board was **fully** fetched:

    - First-page error (network / HTTP / JSON) → ``([], False)``.
    - ``total`` exceeds what the page budget can fetch → ``(partial, False)``.
      ``partial`` holds every posting that did land (NOT ``[]``) so discovery
      gets the first N pages instead of nothing.
    - Pagination stops on a mid-run error before ``total_fetched >= total``
      → ``(partial, False)``.
    - Genuine empty board (``total=0``) → ``([], True)``.

    A ``([], False)`` (error) is therefore distinguishable from a
    ``([], True)`` (true zero): callers that must not mass-expire on a fetch
    failure key off ``complete``, not ``len(postings)``.

    Args:
        slug: ``"subdomain/board"`` Workday slug.
        max_pages: Per-board page budget. ``None`` resolves from the
            per-run ContextVar override (set by the scan / reconcile entry
            points from ``config.ats.workday_max_pages``) and finally
            :data:`_DEFAULT_MAX_PAGES`.

    A warning is logged whenever the board exceeds the page budget so
    operators can see which tenants are only partially discovered.
    """
    effective_max_pages = _resolve_max_pages(max_pages)

    parts = slug.split("/", 1)
    if len(parts) != 2:
        logger.warning("scan_workday: invalid slug format '%s'", slug)
        return [], False

    subdomain, board = parts
    dot_wd_idx = subdomain.find(".wd")
    tenant = subdomain[:dot_wd_idx] if dot_wd_idx > 0 else subdomain

    api_url = f"https://{subdomain}.myworkdayjobs.com/wday/cxs/{tenant}/{board}/jobs"
    offset = 0
    out: list[dict] = []
    total_fetched = 0
    saw_total = False
    total = 0
    pages_fetched = 0

    while pages_fetched < effective_max_pages:
        # Inter-page pacing — does not run on the first iteration; the wait
        # is before the *next* POST, not before the current one.
        if offset > 0:
            time.sleep(_PAGE_FETCH_SLEEP_S)

        body = {
            "appliedFacets": {},
            "limit": _PAGE_SIZE,
            "offset": offset,
            "searchText": "",
        }
        try:
            resp = requests.post(
                api_url,
                json=body,
                headers={"Content-Type": "application/json"},
                timeout=_PROBE_TIMEOUT,
            )
        except Exception as exc:
            logger.warning("scan_workday('%s') request failed: %s", slug, exc)
            break

        if resp.status_code != 200:
            # First-page 404/410 = the tenant/board no longer resolves: raise so
            # the scan path can demote a stale hit (Walmart-class). Any other
            # non-200 (403/5xx) or a 404/410 mid-pagination (we already have
            # postings) is treated as transient/partial — break + report
            # incomplete, exactly as before.
            if resp.status_code in BOARD_GONE_STATUSES and total_fetched == 0:
                raise BoardGoneError(resp.status_code, slug)
            if resp.status_code in _auth_block_statuses():
                logger.warning(
                    "scan_workday('%s') possible auth/anti-bot wall: HTTP %d",
                    slug,
                    resp.status_code,
                )
            else:
                logger.debug("scan_workday('%s') returned HTTP %d", slug, resp.status_code)
            break

        try:
            data = resp.json()
        except Exception as exc:
            logger.warning("scan_workday('%s') JSON parse error: %s", slug, exc)
            break

        # Capture `total` ONLY from the first page. The Workday CXS API
        # reports the real board size on the offset=0 response but returns
        # total=0 on every subsequent page (while still serving 20 valid
        # postings). Re-reading it each iteration overwrote `total` with 0 on
        # page 2, so the `total_fetched >= total` break below fired after just
        # 40 postings — silently capping EVERY board at 2 pages regardless of
        # size (Nvidia 2000, Salesforce 1461, Adobe 1091 all truncated to 40).
        if not saw_total:
            total = data.get("total", 0)
            saw_total = True
        pages_fetched += 1

        postings = data.get("jobPostings", [])
        if not postings:
            break

        # Stash the slug-derived URL parts on each posting so _posting_to_job
        # can build source_url + call the detail endpoint without re-parsing.
        for posting in postings:
            posting["__workday_subdomain"] = subdomain
            posting["__workday_tenant"] = tenant
            posting["__workday_board"] = board
        out.extend(postings)

        total_fetched += len(postings)
        offset += _PAGE_SIZE

        if total_fetched >= total:
            break

    complete = saw_total and total_fetched >= total
    if saw_total and not complete and total > total_fetched:
        logger.warning(
            "scan_workday('%s') board has %d postings; fetched %d in %d pages "
            "(budget %d pages) — discovery partial, reconciliation will skip "
            "this tenant",
            slug,
            total,
            total_fetched,
            pages_fetched,
            effective_max_pages,
        )
    return out, complete


def _fetch_postings(slug: str) -> list[dict]:
    """POST + paginate over Workday CXS list endpoint.

    Returns the raw posting list; description fetches happen later in
    ``_posting_to_job`` so the title-match gate runs first and we only
    pay for detail fetches on matched postings.

    Thin wrapper around :func:`_fetch_postings_with_completeness` — the
    completeness signal is consumed by the ATS reconciler but is not
    needed by the standard scanner flow. The page budget is read from the
    per-run ContextVar (``set_max_pages``) since the registry's
    ``slug -> list`` contract leaves no room for an explicit argument.
    """
    return _fetch_postings_with_completeness(slug)[0]


def _posting_to_job(posting: dict, _slug: str) -> dict:
    # Lazy import — _fetch_workday_description lives in ats_platforms.py because
    # tests/test_workday_scanner.py imports it directly, and the registry must
    # not depend on the flat module at import time (would risk a cycle once
    # the flat module delegates back to run_platform_scan).
    from job_finder.web.ats_platforms import _fetch_workday_description

    subdomain = posting.get("__workday_subdomain", "")
    tenant = posting.get("__workday_tenant", "")
    board = posting.get("__workday_board", "")
    external_path = posting.get("externalPath", "")
    location = posting.get("locationsText", "")

    # externalPath from the CXS API already begins with "/job/...".
    # Do NOT prepend another "/job/" — earlier templates emitted
    # "/job//job/..." URLs that 406'd at the API.
    source_url = (
        f"https://{subdomain}.myworkdayjobs.com/en-US/{board}{external_path}"
        if external_path
        else ""
    )

    description = (
        _fetch_workday_description(subdomain, tenant, board, external_path)
        if external_path
        else ""
    )

    # Polite pacing between per-job detail fetches.
    time.sleep(_DETAIL_FETCH_SLEEP_S)

    # --- Layer-1 emission (Phase 48.02) ------------------------------------
    # source_id: use externalPath as the stable per-job identifier.
    # externalPath is unique per posting per board (e.g. "/job/Title_R-12345")
    # and is already the key used to build source_url and fetch descriptions.
    # Using the full path avoids parsing the requisition suffix, which varies
    # by tenant configuration.
    source_id: str | None = external_path if external_path else None

    # posted_date: parsed from postedOn (varies by tenant format). Relative
    # strings yield 'approximate' precision; absolute dates 'exact' (#364).
    posted_date, posted_date_precision = _parse_posted_date(posting.get("postedOn"))

    # locations_structured: Layer-1 parse of locationsText with
    # workplace-type detection and best-effort City, ST extraction.
    locations_structured = _to_canonical(posting)
    # -----------------------------------------------------------------------

    # ── Structured-field CAPTURE (#451) — raw-as-provided, no synthesis ───────
    # The Workday CXS list payload does not reliably surface remote /
    # employment-type / department fields; read the candidate keys defensively
    # so any tenant that does emit them is captured, and fall to None otherwise.
    is_remote = coerce_remote_bool(
        posting.get("isRemote") if posting.get("isRemote") is not None else posting.get("remote")
    )
    employment_type = (
        label_or_str(posting.get("employmentType"))
        or label_or_str(posting.get("typeOfEmployment"))
        or label_or_str(posting.get("jobType"))
    )
    department = label_or_str(posting.get("department")) or label_or_str(posting.get("team"))

    return {
        "title": posting.get("title", ""),
        "company_source": "Workday",
        "location": location,
        "locations_structured": locations_structured,
        "description": description,
        "source_url": source_url,
        "source_id": source_id,
        "posted_date": posted_date,
        "posted_date_precision": posted_date_precision,
        "salary_min": None,
        "salary_max": None,
        "comp_json": None,
        "is_remote": is_remote,
        "employment_type": employment_type,
        "department": department,
    }


SCANNER = PlatformScanner(
    name="workday",
    company_source="Workday",
    fetch_postings=_fetch_postings,
    title_of=lambda posting: posting.get("title", ""),
    posting_to_job=_posting_to_job,
)
