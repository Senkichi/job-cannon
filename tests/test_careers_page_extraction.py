"""Tests for careers_page location extraction and title-bleed prevention.

Issue #41 / Phase 46.01:
The careers_page scraper previously called tag.get_text(strip=True) which
concatenates adjacent text nodes without any separator, producing the
"Blue State shape": "Principal Analyst (Evergreen)NY, DC, Oakland".

After the fix:
- Title is whitespace-normalised via " ".join(tag.stripped_strings)
- Location is extracted from child elements with location-indicative classes
  or from sibling text in the parent container
- The Job constructor in _run_html.py receives the extracted location
"""

import re
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(url: str, text: str, status_code: int = 200):
    resp = MagicMock()
    resp.url = url
    resp.text = text
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    return resp


# Regex from acceptance criteria: paren-close immediately followed by two
# uppercase letters — the Blue State bleed shape.
_BLEED_SHAPE = re.compile(r"\)[A-Z]{2}")


# ---------------------------------------------------------------------------
# Fixture HTML: Blue State-shaped card
#
# The <a> tag contains:
#   - direct text node: the job title ending in "(Evergreen)"
#   - child <span class="location">: the location "NY, DC, Oakland"
#
# Without the fix, get_text(strip=True) returns:
#   "Principal Analyst (Evergreen)NY, DC, Oakland"  <- bleed
#
# With the fix, stripped_strings are joined with spaces:
#   "Principal Analyst (Evergreen) NY, DC, Oakland"  <- clean
# And the location span is extracted as a separate field.
# ---------------------------------------------------------------------------

_BLUE_STATE_HTML = """
<html>
  <body>
    <div class="jobs-list">
      <div class="job-card">
        <a href="/careers/principal-analyst-evergreen">
          Principal Analyst (Evergreen)<span class="location">NY, DC, Oakland</span>
        </a>
      </div>
      <div class="job-card">
        <a href="/careers/senior-data-analyst">
          Senior Data Analyst (Evergreen)<span class="location">Remote</span>
        </a>
      </div>
    </div>
  </body>
</html>
"""

# ---------------------------------------------------------------------------
# Fixture HTML: location in parent sibling text node (variant 2)
#
# <li>
#   <a href="/jobs/1">Principal Analyst (Evergreen)</a>
#   NY, DC, Oakland
# </li>
# ---------------------------------------------------------------------------

_SIBLING_TEXT_HTML = """
<html>
  <body>
    <ul>
      <li>
        <a href="/careers/principal-analyst">Principal Analyst (Evergreen)</a>
        NY, DC, Oakland
      </li>
    </ul>
  </body>
</html>
"""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCareersPageLocationExtraction:
    """Verify location extraction and title normalisation in scrape_careers_page."""

    def _call_scrape(self, html: str, careers_url: str = "https://example.com/careers"):
        """Call scrape_careers_page with a mocked HTTP response (no JD fetch)."""
        from job_finder.web.careers_scraper import scrape_careers_page

        page_resp = _mock_response(careers_url, html)
        # Stub out the JD fetch (_fetch_job_description) so tests are offline
        with patch("job_finder.web.careers_scraper.requests.get", return_value=page_resp):
            with patch(
                "job_finder.web.careers_scraper._fetch_job_description",
                return_value="",
            ):
                return scrape_careers_page(
                    careers_url,
                    target_titles=["Analyst"],
                    exclusions=[],
                )

    # ------------------------------------------------------------------
    # Core acceptance-criteria tests (Blue State fixture)
    # ------------------------------------------------------------------

    def test_location_is_non_empty_from_class_span(self):
        """Extracted location is non-empty when a <span class='location'> is present."""
        results = self._call_scrape(_BLUE_STATE_HTML)
        assert results, "Expected at least one job to be returned"
        for job in results:
            assert job.get("location"), (
                f"Expected non-empty location; got {job.get('location')!r} "
                f"for title {job.get('title')!r}"
            )

    def test_title_does_not_match_bleed_shape(self):
        """Title must NOT match r'\\)[A-Z]{2}' (paren-close + two uppercase letters)."""
        results = self._call_scrape(_BLUE_STATE_HTML)
        assert results, "Expected at least one job to be returned"
        for job in results:
            title = job.get("title", "")
            assert not _BLEED_SHAPE.search(title), (
                f"Title still has Blue State bleed shape: {title!r}"
            )

    def test_title_whitespace_normalised(self):
        """Adjacent text nodes are joined with a single space (no run-together words)."""
        results = self._call_scrape(_BLUE_STATE_HTML)
        assert results, "Expected at least one job to be returned"
        for job in results:
            title = job.get("title", "")
            # No double-spaces inside the title
            assert "  " not in title, (
                f"Title contains consecutive spaces (not normalised): {title!r}"
            )
            # Title is not just whitespace
            assert title.strip() == title, (
                f"Title has leading/trailing whitespace: {title!r}"
            )

    # ------------------------------------------------------------------
    # Result dict shape
    # ------------------------------------------------------------------

    def test_result_dict_contains_location_key(self):
        """Every result dict from scrape_careers_page now has a 'location' key."""
        results = self._call_scrape(_BLUE_STATE_HTML)
        assert results, "Expected at least one job to be returned"
        for job in results:
            assert "location" in job, f"'location' key missing from result dict: {job}"

    # ------------------------------------------------------------------
    # Sibling-text variant (location outside the <a> tag)
    # ------------------------------------------------------------------

    def test_location_extracted_from_parent_sibling_text(self):
        """Location is extracted from sibling text nodes in the parent container."""
        results = self._call_scrape(_SIBLING_TEXT_HTML)
        assert results, "Expected at least one job to be returned"
        job = results[0]
        assert job.get("location"), (
            f"Expected non-empty location from parent sibling text; "
            f"got {job.get('location')!r}"
        )

    def test_sibling_text_title_no_bleed(self):
        """Title from sibling-text variant also lacks the bleed shape."""
        results = self._call_scrape(_SIBLING_TEXT_HTML)
        assert results, "Expected at least one job to be returned"
        title = results[0].get("title", "")
        assert not _BLEED_SHAPE.search(title), (
            f"Title has bleed shape in sibling-text variant: {title!r}"
        )

    # ------------------------------------------------------------------
    # Regression guard: plain title links still work
    # ------------------------------------------------------------------

    def test_plain_title_link_unchanged(self):
        """Simple <a>Title</a> links continue to work; location may be empty."""
        html = '<html><body><a href="/jobs/1">Data Analyst</a></body></html>'
        results = self._call_scrape(html)
        assert len(results) == 1
        assert results[0]["title"] == "Data Analyst"
        assert "location" in results[0]  # key present (may be empty string)
