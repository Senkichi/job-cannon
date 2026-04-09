"""DataForSEO source — fetches jobs from Google Jobs via DataForSEO SERP API.

Async task-queue API (no live endpoint). Flow:
  1. POST tasks to task_post (billed here)
  2. Poll tasks_ready every 30s until all task IDs appear
  3. GET task_get/advanced/{id} for each completed task (free)

Pricing: $0.0006 per 10 results (normal priority), $0.0012 (high priority).
At depth=200 with 8 queries: ~$0.10/run max (billed per result returned), ~$8.64/month max.
Docs: https://docs.dataforseo.com/v3/serp/google/jobs/overview/
"""

import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from job_finder.models import Job

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.dataforseo.com"

# Matches: "$204K–$276K a year", "$160K-$180K", "204,000–276,000 a year"
# Groups: (low_digits)(low_K?)(separator)(high_digits)(high_K?)
# K suffix is captured per-group so mixed formats like "$160K–$200,000" work correctly.
# Maps human-readable location strings (as used in config.yaml) to the
# comma-separated hierarchical format DataForSEO's location_name field requires.
# Values of None mean "no location filter" — the US-wide location_code=2840 fallback
# is used instead.  Add entries here when new locations are added to config.yaml.
_LOCATION_NAME_MAP: dict[str, str | None] = {
    "San Francisco Bay Area": "San Francisco,California,United States",
    "Remote": None,  # remote queries use US-wide search; no DataForSEO location code for "remote"
}

_SALARY_RE = re.compile(
    r"\$?(\d[\d,]*)\s*([Kk])?\s*[–\-—]\s*\$?(\d[\d,]*)\s*([Kk])?",
)

# Poll timing constants — used by _collect_results when poll_interval > 0.
# Tests set poll_interval_seconds=0 which bypasses sleeping entirely.
_POLL_INITIAL_DELAY_SECONDS = 45   # wait before first poll (tasks need 60-90s)


class DataForSEOSource:
    """Fetch jobs from Google Jobs via DataForSEO SERP API."""

    def __init__(
        self,
        api_key: str,
        max_age_days: int = 7,
        depth: int = 200,
        priority: int = 1,
        poll_interval_seconds: int = 30,
        poll_timeout_seconds: int = 360,
    ):
        """Initialise the DataForSEO source.

        Args:
            api_key: Pre-encoded base64 "login:password" credential.
            max_age_days: Skip jobs older than this many days.
            depth: Number of results to request per query.
            priority: Task priority (1=normal, 2=high).
            poll_interval_seconds: Seconds to sleep between poll retries when
                ``poll_interval_seconds > 0``. Pass ``0`` to disable all
                sleeping (used in tests). In ``fetch_jobs()``, an additional
                ``_POLL_INITIAL_DELAY_SECONDS`` (45s) wait fires before the
                first poll when ``poll_interval_seconds > 0``; this delay does
                not apply when calling ``collect_results()`` directly.
            poll_timeout_seconds: Maximum seconds to wait before abandoning
                tasks that have not appeared in tasks_ready.
        """
        self._auth = api_key  # pre-encoded base64 "login:password"
        self.max_age_days = max_age_days
        self.depth = depth
        self.priority = priority
        self.poll_interval = poll_interval_seconds
        self.poll_timeout = poll_timeout_seconds

    @property
    def _headers(self) -> dict:
        """Common HTTP headers for all DataForSEO API requests."""
        return {
            "Authorization": f"Basic {self._auth}",
            "Content-Type": "application/json",
        }

    def submit_tasks(self, queries: list[dict]) -> list[str]:
        """Submit search tasks and return task IDs. Non-blocking (~2s).

        Args:
            queries: List of dicts with 'query' and optional 'location' keys.

        Returns:
            List of task UUID strings. Empty if no valid queries or submission fails.
        """
        if not queries:
            return []
        return self._submit_tasks(queries)

    def collect_results(self, task_ids: list[str]) -> list[Job]:
        """Poll for completed tasks and fetch results. Blocks until ready or timeout.

        Args:
            task_ids: Task UUIDs returned by submit_tasks().

        Returns:
            List of Job objects. Empty if task_ids is empty or all tasks time out.
        """
        if not task_ids:
            return []
        return self._collect_results(task_ids)

    def fetch_jobs(self, queries: list[dict]) -> list[Job]:
        """Submit all queries as tasks, poll until complete, return combined jobs.

        Backward-compatible entry point. For overlapped execution, call
        submit_tasks() and collect_results() separately — the caller is
        responsible for any initial wait before collect_results().

        Args:
            queries: List of dicts with 'query' and optional 'location' keys.

        Returns:
            List of Job objects from DataForSEO.
        """
        task_ids = self.submit_tasks(queries)
        if not task_ids:
            return []
        # Wait for DataForSEO server-side processing before first poll.
        # In the overlapped run_ingestion path, collect_results() is called
        # directly and tasks have already been processing for ~90s, so this
        # delay only fires in the sequential fetch_jobs() path.
        if self.poll_interval > 0:
            time.sleep(_POLL_INITIAL_DELAY_SECONDS)
        return self.collect_results(task_ids)

    def _submit_tasks(self, queries: list[dict]) -> list[str]:
        """POST all queries as a single batch. Returns list of task UUIDs."""
        payload = []
        for q in queries:
            keyword = q.get("query", "")
            if not keyword:
                logger.debug("DataForSEO: skipping query with empty keyword")
                continue
            task: dict = {
                "keyword": keyword,
                "language_code": "en",
                "depth": self.depth,
                "priority": self.priority,
            }
            location = q.get("location", "")
            if location:
                # Translate to DataForSEO's required hierarchical format.
                # Unknown locations pass through as-is (may be valid already).
                mapped = _LOCATION_NAME_MAP.get(location, location)
                if mapped:
                    task["location_name"] = mapped
                else:
                    task["location_code"] = 2840  # United States (used for "Remote" queries)
            else:
                task["location_code"] = 2840  # United States

            payload.append(task)

        try:
            resp = requests.post(
                f"{_BASE_URL}/v3/serp/google/jobs/task_post",
                headers=self._headers,
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("DataForSEO task_post failed: %s", e)
            return []

        task_ids = []
        for task in data.get("tasks", []):
            if task.get("status_code") == 20100:
                task_ids.append(task["id"])
            else:
                logger.warning(
                    "DataForSEO task creation failed: status=%s message=%s",
                    task.get("status_code"),
                    task.get("status_message"),
                )

        return task_ids

    def _collect_results(self, task_ids: list[str]) -> list[Job]:
        """Poll tasks_ready until all IDs appear, then fetch each. Returns all jobs."""
        pending = set(task_ids)
        collected: list[Job] = []
        deadline = time.monotonic() + self.poll_timeout

        while pending:
            if self.poll_interval > 0:
                time.sleep(self.poll_interval)
            ready_ids = self._get_ready_task_ids()
            for task_id in ready_ids:
                if task_id in pending:
                    jobs = self._fetch_task_results(task_id)
                    collected.extend(jobs)
                    pending.discard(task_id)
            if pending:
                logger.debug("DataForSEO: %d tasks still pending", len(pending))
            if time.monotonic() >= deadline:
                break

        if pending:
            logger.warning(
                "DataForSEO: %d tasks did not complete within %ds timeout: %s",
                len(pending),
                self.poll_timeout,
                list(pending),
            )

        # Deduplicate by job_id within this run — two queries can return the same listing
        seen_ids: set[str] = set()
        deduped: list[Job] = []
        for job in collected:
            if job.source_id and job.source_id in seen_ids:
                logger.debug("DataForSEO: dropping duplicate job_id %s within run", job.source_id)
                continue
            if job.source_id:
                seen_ids.add(job.source_id)
            deduped.append(job)
        return deduped

    def _get_ready_task_ids(self) -> list[str]:
        """Call tasks_ready endpoint. Returns list of completed task UUIDs."""
        try:
            resp = requests.get(
                f"{_BASE_URL}/v3/serp/google/jobs/tasks_ready",
                headers=self._headers,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("DataForSEO tasks_ready failed: %s", e)
            return []

        if data.get("status_code") != 20000:
            logger.warning(
                "DataForSEO tasks_ready non-200 status: %s %s",
                data.get("status_code"),
                data.get("status_message"),
            )
            return []

        result = []
        for task in data.get("tasks", []):
            for item in task.get("result") or []:
                task_id = item.get("id")
                if task_id:
                    result.append(task_id)

        return result

    def _fetch_task_results(self, task_id: str) -> list[Job]:
        """GET task_get/advanced/{id}. Returns parsed Job objects."""
        try:
            resp = requests.get(
                f"{_BASE_URL}/v3/serp/google/jobs/task_get/advanced/{task_id}",
                headers=self._headers,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("DataForSEO task_get failed for %s: %s", task_id, e)
            return []

        tasks = data.get("tasks", [])
        if not tasks:
            return []

        task = tasks[0]
        status_code = task.get("status_code")

        if status_code == 40102:
            # No results found — normal, not an error
            return []

        if status_code in (40401, 40403):
            logger.warning(
                "DataForSEO task %s: %s %s",
                task_id,
                status_code,
                task.get("status_message"),
            )
            return []

        if status_code != 20000:
            logger.warning(
                "DataForSEO task %s returned status %s: %s",
                task_id,
                status_code,
                task.get("status_message"),
            )
            return []

        result_list = task.get("result") or []
        if not result_list:
            return []

        items = result_list[0].get("items") or []
        jobs = []
        for item in items:
            job = self._parse_item(item)
            if job is not None:
                jobs.append(job)

        return jobs

    def _parse_item(self, item: dict) -> Optional[Job]:
        """Parse a single google_jobs_item dict into a Job. Returns None if filtered."""
        from job_finder.web.ats_company import classify_company_name

        title = item.get("title", "")
        company = item.get("employer_name", "")

        if not title or not company:
            return None

        decision = classify_company_name(company)
        if decision.action == "reject":
            logger.info(
                "DataForSEO: skipping '%s' — company '%s' rejected (%s)",
                title, company[:60], decision.reason,
            )
            return None
        # Keep the original company name — jobs.company is the raw source-of-truth.

        location = item.get("location", "")
        source_url = item.get("source_url", "")
        source_id = item.get("job_id", "")

        posted_date = self._parse_timestamp(item.get("timestamp", ""))

        if posted_date is not None:
            age_days = (datetime.now(timezone.utc) - posted_date).days
            if age_days > self.max_age_days:
                logger.info(
                    "Skipping '%s' @ '%s' — %d days old (max %d)",
                    title,
                    company,
                    age_days,
                    self.max_age_days,
                )
                return None

        salary_min, salary_max = self._extract_salary(item.get("salary"))

        return Job(
            title=title,
            company=company,
            location=location,
            source="dataforseo",
            source_url=source_url,
            source_id=source_id,
            salary_min=salary_min,
            salary_max=salary_max,
            description=None,  # enrichment pipeline fills this
            posted_date=posted_date,
        )

    def _parse_timestamp(self, ts: str) -> Optional[datetime]:
        """Parse DataForSEO timestamp string to UTC-aware datetime.

        DataForSEO format: "2026-03-23 23:06:53 +00:00"
        Python 3.11+ fromisoformat handles this format directly.
        """
        if not ts:
            return None
        try:
            return datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            return None

    def _extract_salary(
        self, salary_str: Optional[str]
    ) -> tuple[Optional[int], Optional[int]]:
        """Parse salary string into (min, max) USD integers.

        Handles formats like:
          "$160K–$200K a year"
          "$160K-$200K"
          "$160,000–$200,000 a year"

        Returns (None, None) if null or no match.
        """
        if not salary_str:
            return None, None

        m = _SALARY_RE.search(salary_str)
        if not m:
            return None, None

        try:
            low = int(m.group(1).replace(",", ""))
            high = int(m.group(3).replace(",", ""))
            if m.group(2) and low < 1000:   # K suffix on low group
                low *= 1000
            if m.group(4) and high < 1000:  # K suffix on high group
                high *= 1000
            return low, high
        except (ValueError, IndexError):
            return None, None
