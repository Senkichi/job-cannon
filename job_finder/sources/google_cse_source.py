"""Google Programmable Search Engine (CSE) backend for portal SERP queries.

Free substitute for the DataForSEO `site:portal.com keyword` query path. CSE has
a hard 100 queries/day quota on the free tier; this module enforces a 95-query
defense-in-depth gate so users don't get surprised by a 429 from Google.

**Quota tracking** (F2, 2026-05-22):

When ``db_path`` is supplied at construction, each query write a zero-cost row
to ``scoring_costs`` (``provider='google_cse'``, ``purpose='cse_query'``,
``cost_usd=0``) and the day-count is read back from the same table with a
``DATE(timestamp, 'localtime')=DATE('now', 'localtime')`` filter. This survives Flask restarts — the
original in-process counter reset to 0 every boot, which on a long-uptime
install could let the same calendar day burn through >100 queries by accident.

When ``db_path`` is ``None`` (tests, ad-hoc usage from a CLI / REPL) the source
falls back to the in-process counter. Same semantics, no persistence.

The shape matches DataForSEOSource just enough that `fetch_serp_portals` can
swap backends: a single `fetch_jobs(queries)` entry point taking
`{"query": "site:... keyword", "location": ""}` dicts and returning `list[Job]`.
CSE has no batch endpoint, so each query is one round-trip.
"""

from __future__ import annotations

import logging
from datetime import date

import requests

from job_finder.json_utils import utc_now_iso
from job_finder.models import Job
from job_finder.sources._error_envelope import (
    VendorAccountError,
    detect_vendor_error_envelope,
)

logger = logging.getLogger(__name__)

_API_URL = "https://www.googleapis.com/customsearch/v1"
_REQUEST_TIMEOUT = 15
_USER_AGENT = "Mozilla/5.0 (compatible; JobCannon/1.0)"

# Per-day quota cap on the CSE free tier. Google enforces 100; we stop at 95 so
# concurrent calls from the same install (e.g. a manual benchmark run while a
# scheduled ingest is mid-flight) can't blow past the ceiling.
_QUOTA_LIMIT_PER_DAY = 95

# scoring_costs row identifier for CSE quota events. provider='google_cse' is
# already excluded from cost_gate spend sums via claude_client.FREE_PROVIDERS;
# cost_usd is always 0 — these rows only exist as a quota ledger.
_PROVIDER_NAME = "google_cse"
_PURPOSE_NAME = "cse_query"
_MODEL_NAME = "cse_query"


class GoogleCSESource:
    """Run `site:portal.com keyword` queries through Google Programmable Search.

    Free tier: 100 queries/day, no batch endpoint. Each `fetch_jobs` call
    issues one HTTP request per query and counts against the daily quota.
    The quota gate logs at WARNING and short-circuits remaining queries once
    the daily count reaches `_QUOTA_LIMIT_PER_DAY` (default 95).
    """

    def __init__(
        self,
        api_key: str,
        cse_id: str,
        *,
        quota_limit_per_day: int = _QUOTA_LIMIT_PER_DAY,
        db_path: str | None = None,
    ):
        self._api_key = api_key
        self._cse_id = cse_id
        self._quota_limit = quota_limit_per_day
        self._db_path = db_path
        # In-process fallback (used only when ``db_path`` is None — tests +
        # CLI usage). When ``db_path`` is set the DB ledger is authoritative
        # and these fields are not read.
        self._quota_used = 0
        self._quota_day = date.today()

    def fetch_jobs(self, queries: list[dict]) -> list[Job]:
        """Run each query as a CSE search; map the top results into Job objects.

        Args:
            queries: Same shape as DataForSEOSource — list of
                ``{"query": "site:portal.com keyword", "location": ""}`` dicts.
                The `location` field is ignored by CSE.

        Returns:
            Deduplicated list of Job objects. Each result's ``source`` is set
            to ``portal_serp_cse`` so the SERP-portal post-processor in
            ``portal_search_source`` can attribute the portal name from the
            URL pattern.
        """
        if not (self._api_key and self._cse_id):
            return []
        if not queries:
            return []

        self._roll_quota_if_new_day()

        all_jobs: list[Job] = []
        seen_urls: set[str] = set()

        for entry in queries:
            used = self._current_quota_used()
            if used >= self._quota_limit:
                logger.warning(
                    "CSE quota nearly exhausted (%d/%d used today); skipping "
                    "remaining site: queries",
                    used,
                    self._quota_limit,
                )
                break

            query = entry.get("query") or ""
            if not query:
                continue

            try:
                resp = requests.get(
                    _API_URL,
                    params={
                        "key": self._api_key,
                        "cx": self._cse_id,
                        "q": query,
                        "num": 10,
                    },
                    headers={"User-Agent": _USER_AGENT},
                    timeout=_REQUEST_TIMEOUT,
                )
                self._record_quota_use()
                resp.raise_for_status()
                payload = resp.json()
            except Exception as exc:
                logger.warning("CSE query failed for %r: %s", query, exc)
                continue

            reason = detect_vendor_error_envelope(payload, source="google_cse")
            if reason:
                raise VendorAccountError(reason)

            for item in payload.get("items", []) or []:
                url = item.get("link") or ""
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)

                title, company = _split_title_company(item.get("title") or "")
                if not title:
                    continue

                all_jobs.append(
                    Job(
                        title=title,
                        company=company,
                        location="Remote",
                        source="portal_serp_cse",
                        source_url=url,
                        description=_truncate(item.get("snippet") or ""),
                    )
                )

        final_used = self._current_quota_used()
        logger.info(
            "CSE: %d queries used today (%d remaining), %d jobs returned",
            final_used,
            max(0, self._quota_limit - final_used),
            len(all_jobs),
        )
        return all_jobs

    def _current_quota_used(self) -> int:
        """Return today's CSE query count from whichever ledger is active."""
        if self._db_path is None:
            return self._quota_used
        try:
            from job_finder.web.db_helpers import standalone_connection

            with standalone_connection(self._db_path) as conn:
                row = conn.execute(
                    """SELECT COUNT(*) FROM scoring_costs
                       WHERE provider=? AND DATE(timestamp, 'localtime')=DATE('now', 'localtime')""",
                    (_PROVIDER_NAME,),
                ).fetchone()
                return int(row[0]) if row and row[0] is not None else 0
        except Exception as exc:
            logger.warning(
                "CSE quota DB read failed (%s); falling back to in-process counter",
                type(exc).__name__,
            )
            return self._quota_used

    def _record_quota_use(self) -> None:
        """Append a quota-ledger row to scoring_costs, or bump the in-process counter."""
        if self._db_path is None:
            self._quota_used += 1
            return
        try:
            from job_finder.web.db_helpers import standalone_connection

            with standalone_connection(self._db_path) as conn:
                conn.execute(
                    """INSERT INTO scoring_costs
                       (job_id, purpose, model, input_tokens, output_tokens,
                        cost_usd, timestamp, provider)
                       VALUES (NULL, ?, ?, 0, 0, 0, ?, ?)""",
                    (
                        _PURPOSE_NAME,
                        _MODEL_NAME,
                        utc_now_iso(),
                        _PROVIDER_NAME,
                    ),
                )
                conn.commit()
        except Exception as exc:
            logger.warning(
                "CSE quota DB write failed (%s); bumping in-process counter as fallback",
                type(exc).__name__,
            )
            self._quota_used += 1

    def _roll_quota_if_new_day(self) -> None:
        """In-process counter roll-over only. DB-backed path is calendar-correct
        by virtue of ``DATE(timestamp, 'localtime')=DATE('now', 'localtime')`` in the query."""
        if self._db_path is not None:
            return
        today = date.today()
        if today != self._quota_day:
            self._quota_day = today
            self._quota_used = 0


def _split_title_company(raw_title: str) -> tuple[str, str]:
    """Best-effort split of a SERP result title into (job_title, company).

    CSE result titles typically look like "Senior Engineer - Acme Corp" or
    "Acme Corp hiring Senior Engineer". We split on common separators; when no
    separator is recognised we return the whole string as the title with an
    empty company (the downstream portal-detection step can still attribute
    by URL pattern).
    """
    for sep in (" - ", " | ", " at ", " — "):
        if sep in raw_title:
            left, _, right = raw_title.partition(sep)
            left, right = left.strip(), right.strip()
            if left and right:
                return left, right
    return raw_title.strip(), ""


def _truncate(text: str, max_len: int = 2000) -> str:
    return text if len(text) <= max_len else text[:max_len]
