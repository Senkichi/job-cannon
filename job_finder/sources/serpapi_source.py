"""SerpAPI source - fetches jobs from Google Jobs via SerpAPI.

Requires a SerpAPI key (free tier: 100 searches/month).
Google Jobs aggregates from LinkedIn, Indeed, Glassdoor, ZipRecruiter, etc.
"""

import logging
import time

import requests

logger = logging.getLogger(__name__)

from job_finder.models import Job


class SerpAPISource:
    """Fetch jobs from Google Jobs via SerpAPI."""

    BASE_URL = "https://serpapi.com/search.json"
    _PAGE_SIZE = 10  # Google Jobs returns 10 results per page

    def __init__(self, api_key: str, source_name: str = "serpapi", max_pages: int = 5):
        self.api_key = api_key
        self.source_name = source_name
        self.max_pages = max_pages

    def fetch_jobs(self, queries: list[dict], delay: float = 1.0) -> list[Job]:
        """Run multiple search queries and return combined results.

        Args:
            queries: List of dicts with 'query' and 'location' keys.
            delay: Seconds to wait between consecutive requests. SerpAPI
                enforces a per-second rate limit; 1.0s keeps burst traffic
                within that limit.

        Returns:
            List of Job objects.
        """
        all_jobs = []
        for i, q in enumerate(queries):
            if i > 0:
                time.sleep(delay)
            jobs = self._search(q["query"], q.get("location", ""))
            all_jobs.extend(jobs)
        return all_jobs

    def _search(self, query: str, location: str = "") -> list[Job]:
        """Execute a single Google Jobs search with pagination.

        Google Jobs returns 10 results per page. Paginates using the `start`
        parameter until no more results or max_pages is reached.
        Each page costs one API credit.
        """
        all_jobs: list[Job] = []

        for page in range(self.max_pages):
            params = {
                "engine": "google_jobs",
                "q": query,
                "location": location,
                "api_key": self.api_key,
                "hl": "en",
                "start": page * self._PAGE_SIZE,
            }

            try:
                resp = requests.get(self.BASE_URL, params=params, timeout=30)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.warning(
                    "SerpAPI search failed for '%s' (page %d): %s",
                    query,
                    page,
                    e,
                )
                break

            results = data.get("jobs_results", [])
            if not results:
                break

            for result in results:
                job = self._parse_result(result)
                if job:
                    all_jobs.append(job)

            # Stop if this page had fewer results than a full page
            if len(results) < self._PAGE_SIZE:
                break

            # Small delay between pages to respect rate limits
            if page < self.max_pages - 1:
                time.sleep(0.5)

        logger.info(
            "%s '%s': %d jobs across %d page(s)",
            self.source_name,
            query,
            len(all_jobs),
            min(page + 1, self.max_pages),
        )
        return all_jobs

    def _parse_result(self, result: dict) -> Job | None:
        """Parse a single SerpAPI Google Jobs result into a Job."""
        from job_finder.web.ats_company import classify_company_name

        title = result.get("title", "")
        company = result.get("company_name", "")
        location = result.get("location", "")

        if not title or not company:
            return None

        decision = classify_company_name(company)
        if decision.action == "reject":
            logger.info(
                "SerpAPI: skipping '%s' — company '%s' rejected (%s)",
                title,
                company[:60],
                decision.reason,
            )
            return None
        # Keep the original company name — jobs.company is the raw source-of-truth.
        # Normalization for lookup happens at upsert_company() at the write boundary.

        # Extract salary if available
        salary_min, salary_max = self._extract_salary(result)

        # Build description from highlights
        description_parts = []
        for highlight in result.get("job_highlights", []):
            items = highlight.get("items", [])
            description_parts.extend(items)
        description = "\n".join(description_parts) if description_parts else None

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

    def _extract_salary(self, result: dict) -> tuple[int | None, int | None]:
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
