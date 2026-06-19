"""SerpAPI source - fetches jobs from Google Jobs via SerpAPI.

Requires a SerpAPI key (free tier: 100 searches/month).
Google Jobs aggregates from LinkedIn, Indeed, Glassdoor, ZipRecruiter, etc.
"""

import logging
import time

import requests

logger = logging.getLogger(__name__)

from job_finder.models import Job
from job_finder.sources._error_envelope import (
    VendorAccountError,
    detect_vendor_error_envelope,
)


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
                if resp.status_code in (401, 403):
                    raise RuntimeError(
                        f"SerpAPI key rejected (HTTP {resp.status_code}) — check your API key"
                    )
                resp.raise_for_status()
                data = resp.json()
                reason = detect_vendor_error_envelope(data, source=self.source_name)
                if reason:
                    raise VendorAccountError(reason)
            except RuntimeError:
                raise
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
        salary_fields = self._capture_salary(result)

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
            # No source_id: SerpAPI's job_id is a search-result token, not a
            # per-job-stable platform ID (I-11).
            **salary_fields,
            description=description,
        )

    def _capture_salary(self, result: dict) -> dict:
        """Capture the SerpAPI Google-Jobs salary string as an observation (D-1/D-2/D-3).

        Replaces the bespoke salary regex (plan §1.2 item 3) with delegation to
        the single normalizer. The FULL ``detected_extensions.salary`` text is
        passed so period cues survive ("$150K–$200K a year" → annual; an hourly
        snippet → annualized via the salvage ladder). Returns the salary-related
        ``Job`` kwargs (canonical pair only when the ladder resolves it; junk
        quarantines to a NULL pair with the observation retained). Provenance is
        ``feed_string`` — a SERP snippet, lowest trust (D-4).
        """
        from job_finder.salary_normalizer import parse_salary_text, salary_capture_fields

        ext = result.get("detected_extensions", {})
        salary = ext.get("salary", "")
        return salary_capture_fields(parse_salary_text(salary, provenance="feed_string"))
