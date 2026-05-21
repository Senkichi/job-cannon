"""Google Programmable Search Engine (CSE) backend for portal SERP queries.

Free substitute for the DataForSEO `site:portal.com keyword` query path. CSE has
a hard 100 queries/day quota on the free tier; this module enforces a 95-query
defense-in-depth gate so users don't get surprised by a 429 from Google.

Quota tracking is in-process for this iteration. The PLAN.md acceptance criterion
("Quota gate trips at >=95 queries with the documented warning log") only
requires a working gate, not the DB-backed counter that the load-bearing
decision §8 sketches. Once `ingestion_runner._fetch_portal_search` is wired to
this backend (deferred — see HANDOFF.md), the counter should move to
`scoring_costs(provider='google_cse', cost_usd=0)` so it survives restarts. For
now the in-process counter is sufficient because the ingestion runner does not
yet exercise this backend.

The shape matches DataForSEOSource just enough that `fetch_serp_portals` can
swap backends: a single `fetch_jobs(queries)` entry point taking
`{"query": "site:... keyword", "location": ""}` dicts and returning `list[Job]`.
CSE has no batch endpoint, so each query is one round-trip.
"""

from __future__ import annotations

import logging
from datetime import date

import requests

from job_finder.models import Job

logger = logging.getLogger(__name__)

_API_URL = "https://www.googleapis.com/customsearch/v1"
_REQUEST_TIMEOUT = 15
_USER_AGENT = "Mozilla/5.0 (compatible; JobCannon/1.0)"

# Per-day quota cap on the CSE free tier. Google enforces 100; we stop at 95 so
# concurrent calls from the same install (e.g. a manual benchmark run while a
# scheduled ingest is mid-flight) can't blow past the ceiling.
_QUOTA_LIMIT_PER_DAY = 95


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
    ):
        self._api_key = api_key
        self._cse_id = cse_id
        self._quota_limit = quota_limit_per_day
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
            if self._quota_used >= self._quota_limit:
                logger.warning(
                    "CSE quota nearly exhausted (%d/%d used today); skipping "
                    "remaining site: queries",
                    self._quota_used,
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
                self._quota_used += 1
                resp.raise_for_status()
                payload = resp.json()
            except Exception as exc:
                logger.warning("CSE query failed for %r: %s", query, exc)
                continue

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

        logger.info(
            "CSE: %d queries used today (%d remaining), %d jobs returned",
            self._quota_used,
            max(0, self._quota_limit - self._quota_used),
            len(all_jobs),
        )
        return all_jobs

    def _roll_quota_if_new_day(self) -> None:
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
