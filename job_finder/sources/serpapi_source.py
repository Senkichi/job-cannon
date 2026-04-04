"""SerpAPI source - fetches jobs from Google Jobs via SerpAPI.

Requires a SerpAPI key (free tier: 100 searches/month).
Google Jobs aggregates from LinkedIn, Indeed, Glassdoor, ZipRecruiter, etc.
"""

import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

from job_finder.models import Job


class SerpAPISource:
    """Fetch jobs from Google Jobs via SerpAPI."""

    BASE_URL = "https://serpapi.com/search.json"

    def __init__(self, api_key: str, source_name: str = "serpapi"):
        self.api_key = api_key
        self.source_name = source_name

    def fetch_jobs(self, queries: list[dict]) -> list[Job]:
        """Run multiple search queries and return combined results.

        Args:
            queries: List of dicts with 'query' and 'location' keys.

        Returns:
            List of Job objects.
        """
        all_jobs = []
        for q in queries:
            jobs = self._search(q["query"], q.get("location", ""))
            all_jobs.extend(jobs)
        return all_jobs

    def _search(self, query: str, location: str = "") -> list[Job]:
        """Execute a single Google Jobs search."""
        params = {
            "engine": "google_jobs",
            "q": query,
            "location": location,
            "api_key": self.api_key,
            "hl": "en",
        }

        try:
            resp = requests.get(self.BASE_URL, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("SerpAPI search failed for '%s': %s", query, e)
            return []

        jobs = []
        for result in data.get("jobs_results", []):
            job = self._parse_result(result)
            if job:
                jobs.append(job)

        return jobs

    def _parse_result(self, result: dict) -> Optional[Job]:
        """Parse a single SerpAPI Google Jobs result into a Job."""
        title = result.get("title", "")
        company = result.get("company_name", "")
        location = result.get("location", "")

        if not title or not company:
            return None

        # Extract salary if available
        salary_min, salary_max = self._extract_salary(result)

        # Build description from highlights
        description_parts = []
        for highlight in result.get("job_highlights", []):
            items = highlight.get("items", [])
            description_parts.extend(items)
        description = "\n".join(description_parts) if description_parts else None

        # Parse detected extensions for posting date
        extensions = result.get("detected_extensions", {})
        posted_at = extensions.get("posted_at", "")

        # Try to get a direct apply link
        apply_links = result.get("apply_options", [])
        source_url = apply_links[0].get("link", "") if apply_links else ""
        if not source_url:
            source_url = result.get("share_link", result.get("job_id", ""))

        return Job(
            title=title,
            company=company,
            location=location,
            source=self.source_name,
            source_url=source_url,
            source_id=result.get("job_id", ""),
            salary_min=salary_min,
            salary_max=salary_max,
            description=description,
        )

    def _extract_salary(self, result: dict) -> tuple[Optional[int], Optional[int]]:
        """Extract salary from SerpAPI result extensions."""
        ext = result.get("detected_extensions", {})
        salary = ext.get("salary", "")
        if not salary:
            return None, None

        import re

        # Format: "$150K–$200K a year" or "$150,000-$200,000"
        match = re.search(r"\$(\d[\d,]*)\s*[K–-]+\s*\$(\d[\d,]*)", salary, re.IGNORECASE)
        if match:
            low = int(match.group(1).replace(",", ""))
            high = int(match.group(2).replace(",", ""))
            if low < 1000:
                low *= 1000
            if high < 1000:
                high *= 1000
            return low, high

        return None, None
