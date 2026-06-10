"""Thordata source - fetches jobs from Google Jobs via Thordata SERP API.

POST-based API, cheaper than SerpAPI (~$3-5/1K vs $15/1K).
Uses engine=google_jobs (SerpApi-compatible) which returns a top-level
jobs_results[] array. Each item has: title, company_name, location,
share_link, extensions[] (flat array with salary, posting date, schedule
type), via, rank.
Does NOT return description or job_highlights — enrichment pipeline fills those.

Pagination: uses the `start` offset param (same as SerpAPI google_jobs).
Each page returns up to _PAGE_SIZE results; a short page signals last page.
"""

import logging
import re
import time

import requests

from job_finder.models import Job

logger = logging.getLogger(__name__)

_BASE_URL = "https://scraperapi.thordata.com/request"

# Matches: "204K–276K a year", "$160K-$180K", "204,000–276,000 a year"
# The en-dash (–) and em-dash (—) and hyphen (-) are all covered.
_SALARY_RE = re.compile(
    r"\$?(\d[\d,]*)\s*[K]?\s*[–\-—]\s*\$?(\d[\d,]*)\s*[K]?",
    re.IGNORECASE,
)

# Matches age strings like "29 days ago", "1 day ago", "2 weeks ago", etc.
_AGE_RE = re.compile(
    r"(?:just posted|today|(\d+)\s*(hour|day|week|month)s?\s*ago)",
    re.IGNORECASE,
)


class ThordataSource:
    """Fetch jobs from Google Jobs via Thordata SERP API."""

    _PAGE_SIZE = 10  # google_jobs returns up to 10 results per page

    def __init__(self, api_key: str, max_age_days: int = 3, max_pages: int = 3):
        self.api_key = api_key
        self.max_age_days = max_age_days
        self.max_pages = max_pages

    def fetch_jobs(self, queries: list[dict]) -> list[Job]:
        """Run multiple search queries and return combined results.

        Args:
            queries: List of dicts with 'query' and 'location' keys.

        Returns:
            List of Job objects passing the recency filter.
        """
        all_jobs: list[Job] = []
        for q in queries:
            jobs = self._search(q.get("query", ""), q.get("location", ""))
            all_jobs.extend(jobs)
        return all_jobs

    def _search(self, query: str, location: str = "") -> list[Job]:
        """Execute a Google Jobs search via Thordata with bounded pagination.

        Uses engine=google_jobs (SerpApi-compatible) and reads the top-level
        jobs_results[] key. Paginates via the `start` offset parameter until
        the page is shorter than _PAGE_SIZE or max_pages is reached.
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/x-www-form-urlencoded",
        }

        all_jobs: list[Job] = []

        for page in range(self.max_pages):
            payload = {
                "engine": "google_jobs",
                "q": query,
                "json": "1",
                "hl": "en",
                "gl": "us",
                "start": page * self._PAGE_SIZE,
            }
            if location:
                payload["location"] = location

            try:
                resp = requests.post(_BASE_URL, headers=headers, data=payload, timeout=30)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.warning("Thordata search failed for '%s' (page %d): %s", query, page, e)
                break

            # B: Distinguish "engine returned no jobs key" from "empty jobs list"
            if "jobs_results" not in data:
                logger.warning(
                    "Thordata '%s' @ '%s' (page %d): 200 response missing 'jobs_results'"
                    " key — engine mismatch or expired account (keys: %s)",
                    query,
                    location,
                    page,
                    sorted(data.keys()),
                )
                break

            raw_results: list[dict] = data["jobs_results"]
            logger.info(
                "Thordata '%s' @ '%s' (page %d): %d raw results from API",
                query,
                location,
                page,
                len(raw_results),
            )

            for result in raw_results:
                job = self._parse_result(result)
                if job:
                    all_jobs.append(job)

            # Short page → last page of results
            if len(raw_results) < self._PAGE_SIZE:
                break

            # Small delay between pages
            if page < self.max_pages - 1:
                time.sleep(0.5)

        logger.info(
            "Thordata '%s' @ '%s': %d jobs after age filter (max_age_days=%d)",
            query,
            location,
            len(all_jobs),
            self.max_age_days,
        )
        return all_jobs

    def _parse_result(self, result: dict) -> Job | None:
        """Parse a single Thordata Google Jobs result into a Job.

        Returns None if the job is missing required fields or is older than max_age_days.
        """
        from job_finder.web.ats_company import classify_company_name

        title = result.get("title", "")
        company = result.get("company_name", "")
        if not title or not company:
            return None

        decision = classify_company_name(company)
        if decision.action == "reject":
            logger.info(
                "Thordata: skipping '%s' — company '%s' rejected (%s)",
                title,
                company[:60],
                decision.reason,
            )
            return None
        # Keep the original company name — jobs.company is the raw source-of-truth.

        extensions: list[str] = result.get("extensions", [])

        # Recency filter
        age_days = self._parse_posting_age(extensions)
        if age_days is not None and age_days > self.max_age_days:
            logger.info(
                "Skipping '%s' @ '%s' — posted %d days ago (max %d)",
                title,
                company,
                age_days,
                self.max_age_days,
            )
            return None

        link = result.get("link", "")
        salary_min, salary_max = self._extract_salary_from_extensions(extensions)

        return Job(
            title=title,
            company=company,
            location=result.get("location", ""),
            source="thordata",
            source_url=link,
            # No source_id: the Thordata Google-Jobs docid is a search-result
            # token, not a per-job-stable platform ID (I-11).
            salary_min=salary_min,
            salary_max=salary_max,
            description=None,  # enrichment pipeline fills this
        )

    def _parse_posting_age(self, extensions: list[str]) -> int | None:
        """Scan extensions for a posting age string and return days as int.

        Returns None if no age string is found (job is treated as includeable).

        Handles:
          "Just posted" / "Today"     → 0
          "X hours ago"               → 0
          "X day(s) ago"              → X
          "X week(s) ago"             → X * 7
          "X month(s) ago"            → X * 30
        """
        for ext in extensions:
            m = _AGE_RE.search(ext)
            if m is None:
                continue
            full_match = m.group(0).lower()
            if "just posted" in full_match or "today" in full_match:
                return 0
            count_str = m.group(1)
            unit = m.group(2).lower() if m.group(2) else ""
            if not count_str:
                return 0
            count = int(count_str)
            if "hour" in unit:
                return 0
            if "day" in unit:
                return count
            if "week" in unit:
                return count * 7
            if "month" in unit:
                return count * 30
        return None

    def _extract_salary_from_extensions(
        self, extensions: list[str]
    ) -> tuple[int | None, int | None]:
        """Scan extensions for a salary range string and return (min, max) in USD.

        Handles formats like:
          "204K–276K a year"
          "$160K-$180K"
          "204,000–276,000 a year"

        Returns (None, None) if no salary string is found.
        """
        for ext in extensions:
            m = _SALARY_RE.search(ext)
            if not m:
                continue
            try:
                low = int(m.group(1).replace(",", ""))
                high = int(m.group(2).replace(",", ""))
                # Detect K suffix in the matched substring (covers all dash variants)
                matched = ext[m.start() : m.end()]
                has_k = bool(re.search(r"[Kk]", matched))
                if has_k:
                    if low < 1000:
                        low *= 1000
                    if high < 1000:
                        high *= 1000
                return low, high
            except (ValueError, IndexError):
                continue
        return None, None
