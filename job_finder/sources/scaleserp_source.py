"""ScaleSerp source - fetches jobs from Google Jobs via ScaleSerp API.

ScaleSerp is a cheaper SerpAPI-compatible alternative (~$2/1K vs $15/1K).
Supports engine=google_jobs with full index access and pagination.
Response schema is SerpAPI-compatible (jobs_results[], same field names).

See: https://www.scaleserp.com/
"""

from job_finder.sources.serpapi_source import SerpAPISource


class ScaleSerpSource(SerpAPISource):
    """Fetch jobs from Google Jobs via ScaleSerp API."""

    BASE_URL = "https://api.scaleserp.com/search"

    def __init__(self, api_key: str, max_pages: int = 5):
        super().__init__(api_key, source_name="scaleserp", max_pages=max_pages)
