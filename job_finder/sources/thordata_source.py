"""Thordata source - fetches jobs from Google Jobs via Thordata SERP API.

POST-based API, cheaper than SerpAPI (~$3-5/1K vs $15/1K).
Returns jobs_results[] with title, company_name, location, share_link,
extensions[] (flat array with salary, posting date, schedule type), via, rank.
Does NOT return description or job_highlights — enrichment pipeline fills those.
"""

import logging
import re
from typing import Optional
from urllib.parse import parse_qs, urlparse

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

    def __init__(self, api_key: str, max_age_days: int = 3):
        self.api_key = api_key
        self.max_age_days = max_age_days

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
        """Execute a single Google Jobs search via Thordata."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        payload = {
            "engine": "google_jobs",
            "q": query,
            "location": location,
            "json": "1",
            "hl": "en",
        }

        try:
            resp = requests.post(_BASE_URL, headers=headers, data=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("Thordata search failed for '%s': %s", query, e)
            return []

        jobs = []
        for result in data.get("jobs_results", []):
            job = self._parse_result(result)
            if job:
                jobs.append(job)

        return jobs

    def _parse_result(self, result: dict) -> Optional[Job]:
        """Parse a single Thordata Google Jobs result into a Job.

        Returns None if the job is missing required fields or is older than max_age_days.
        """
        title = result.get("title", "")
        company = result.get("company_name", "")
        if not title or not company:
            return None

        extensions: list[str] = result.get("extensions", [])

        # Recency filter
        age_days = self._parse_posting_age(extensions)
        if age_days is not None and age_days > self.max_age_days:
            logger.debug(
                "Skipping '%s' @ '%s' — posted %d days ago (max %d)",
                title, company, age_days, self.max_age_days,
            )
            return None

        share_link = result.get("share_link", "")
        source_id = self._extract_htidocid(share_link)
        salary_min, salary_max = self._extract_salary_from_extensions(extensions)

        return Job(
            title=title,
            company=company,
            location=result.get("location", ""),
            source="thordata",
            source_url=share_link,
            source_id=source_id,
            salary_min=salary_min,
            salary_max=salary_max,
            description=None,  # enrichment pipeline fills this
        )

    def _parse_posting_age(self, extensions: list[str]) -> Optional[int]:
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

    def _extract_htidocid(self, share_link: str) -> str:
        """Extract the stable htidocid parameter from a Google Jobs share_link URL."""
        if not share_link:
            return ""
        try:
            parsed = urlparse(share_link)
            params = parse_qs(parsed.query)
            htidocid_list = params.get("htidocid", [])
            return htidocid_list[0] if htidocid_list else ""
        except Exception:
            return ""

    def _extract_salary_from_extensions(
        self, extensions: list[str]
    ) -> tuple[Optional[int], Optional[int]]:
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
