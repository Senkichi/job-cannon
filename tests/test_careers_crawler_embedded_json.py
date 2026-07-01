"""Tests for the embedded JSON extraction tier (issue #562)."""

import pytest

from job_finder.web.careers_crawler import _try_embedded_json_extract_from_html


class TestEmbeddedJsonTier:
    """Tests for the embedded JSON extraction tier."""

    def test_extracts_from_next_data_fixture(self):
        """Walker extracts jobs from __NEXT_DATA__ fixture."""
        fixture_path = "tests/fixtures/next_data_jobs.html"
        with open(fixture_path, "r", encoding="utf-8") as f:
            html = f.read()

        base_url = "https://example.com"
        target_titles = ["software engineer", "data scientist", "product manager"]
        exclusions = []

        result = _try_embedded_json_extract_from_html(
            html, base_url, target_titles, exclusions
        )

        assert result is not None
        assert len(result) == 3
        titles = {job["title"] for job in result}
        assert "Senior Software Engineer" in titles
        assert "Data Scientist" in titles
        assert "Product Manager" in titles

        # URLs should be resolved
        urls = [job["url"] for job in result]
        assert all(url.startswith("https://example.com") for url in urls)

    def test_extracts_from_nuxt_fixture(self):
        """Walker extracts jobs from __NUXT__ fixture."""
        fixture_path = "tests/fixtures/nuxt_jobs.html"
        with open(fixture_path, "r", encoding="utf-8") as f:
            html = f.read()

        base_url = "https://example.com"
        target_titles = ["backend engineer", "frontend developer"]
        exclusions = []

        result = _try_embedded_json_extract_from_html(
            html, base_url, target_titles, exclusions
        )

        assert result is not None
        assert len(result) == 2
        titles = {job["title"] for job in result}
        assert "Senior Backend Engineer" in titles
        assert "Frontend Developer" in titles

    def test_decoy_fixture_returns_none(self):
        """Decoy fixture with product list returns None (false-positive guard)."""
        fixture_path = "tests/fixtures/next_data_decoy.html"
        with open(fixture_path, "r", encoding="utf-8") as f:
            html = f.read()

        base_url = "https://example.com"
        target_titles = ["widget", "gadget"]  # Would match if not for title+url gate
        exclusions = []

        result = _try_embedded_json_extract_from_html(
            html, base_url, target_titles, exclusions
        )

        # Should return None because the array lacks title+url keys
        assert result is None

    def test_title_hygiene_applied(self):
        """Extracted titles run through clean_title for hygiene."""
        fixture_path = "tests/fixtures/next_data_jobs.html"
        with open(fixture_path, "r", encoding="utf-8") as f:
            html = f.read()

        base_url = "https://example.com"
        target_titles = ["software engineer"]
        exclusions = []

        result = _try_embedded_json_extract_from_html(
            html, base_url, target_titles, exclusions
        )

        assert result is not None
        # Title should be cleaned (no location suffix, etc.)
        job = result[0]
        assert "Senior Software Engineer" == job["title"]

    def test_title_filter_applied(self):
        """User's title filter is applied to extracted jobs."""
        fixture_path = "tests/fixtures/next_data_jobs.html"
        with open(fixture_path, "r", encoding="utf-8") as f:
            html = f.read()

        base_url = "https://example.com"
        target_titles = ["software engineer"]  # Only match one
        exclusions = []

        result = _try_embedded_json_extract_from_html(
            html, base_url, target_titles, exclusions
        )

        assert result is not None
        assert len(result) == 1
        assert result[0]["title"] == "Senior Software Engineer"

    def test_empty_html_returns_none(self):
        """Empty HTML returns None (escalate signal)."""
        result = _try_embedded_json_extract_from_html(
            "", "https://example.com", ["engineer"], []
        )
        assert result is None

    def test_no_embedded_json_returns_none(self):
        """HTML without embedded JSON returns None."""
        html = "<html><body>No JSON here</body></html>"
        result = _try_embedded_json_extract_from_html(
            html, "https://example.com", ["engineer"], []
        )
        assert result is None

    def test_malformed_json_returns_none(self):
        """Malformed JSON in script tags returns None (defensive)."""
        html = """
        <html>
        <script id="__NEXT_DATA__" type="application/json">
        { invalid json }
        </script>
        </html>
        """
        result = _try_embedded_json_extract_from_html(
            html, "https://example.com", ["engineer"], []
        )
        assert result is None
