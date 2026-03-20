"""Tests for homepage_discoverer.py module.

Covers:
- discover_homepage: slug heuristic (success, parked domain, non-HTML, 404)
- discover_homepage: DDG HTML search fallback (success, wikipedia skip, failure)
- discover_homepage: both tiers fail returns None
- discover_homepage: no slug skips directly to DDG
- discover_homepages_batch: DB update, cap at 50 companies
"""

import os
import sqlite3
import tempfile

import pytest
from unittest.mock import MagicMock, patch, call


# ---------------------------------------------------------------------------
# Helpers: build mock response objects
# ---------------------------------------------------------------------------

def _mock_response(url, text, status_code=200, content_type="text/html; charset=utf-8"):
    """Create a mock requests.Response."""
    resp = MagicMock()
    resp.url = url
    resp.text = text
    resp.status_code = status_code
    resp.headers = {"Content-Type": content_type}
    resp.raise_for_status = MagicMock()
    # Make iter_content work for streaming
    resp.iter_content.return_value = iter([text.encode("utf-8") if text else b""])
    return resp


def _mock_head_response(url, status_code=200, content_type="text/html; charset=utf-8"):
    """Create a mock requests.Response for HEAD requests."""
    resp = MagicMock()
    resp.url = url
    resp.status_code = status_code
    resp.headers = {"Content-Type": content_type}
    resp.raise_for_status = MagicMock()
    return resp


DDG_HTML_WITH_RESULT = """
<html>
<body>
<div class="results">
  <div class="result">
    <a class="result__a" href="https://ramp.com">Ramp - Corporate Card</a>
  </div>
  <div class="result">
    <a class="result__a" href="https://en.wikipedia.org/wiki/Ramp">Ramp - Wikipedia</a>
  </div>
</div>
</body>
</html>
"""

DDG_HTML_WIKIPEDIA_FIRST = """
<html>
<body>
<div class="results">
  <div class="result">
    <a class="result__a" href="https://en.wikipedia.org/wiki/Acme">Acme - Wikipedia</a>
  </div>
  <div class="result">
    <a class="result__a" href="https://acme.com">Acme Corp</a>
  </div>
</div>
</body>
</html>
"""

DDG_HTML_NO_RESULTS = """
<html>
<body>
<div class="results">
</div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Tests: discover_homepage
# ---------------------------------------------------------------------------

class TestDiscoverHomepage:

    def test_slug_heuristic_success(self):
        """Slug heuristic: HEAD 200 + HTML content-type + non-parked body returns URL."""
        from job_finder.web.homepage_discoverer import discover_homepage

        head_resp = _mock_head_response("https://ramp.com", 200, "text/html; charset=utf-8")
        get_resp = _mock_response("https://ramp.com", "<html><body>Ramp corporate card</body></html>")

        with patch("job_finder.web.homepage_discoverer.requests.head", return_value=head_resp), \
             patch("job_finder.web.homepage_discoverer.requests.get", return_value=get_resp):
            result = discover_homepage("Ramp", "ashby", "ramp", [])

        assert result == "https://ramp.com"

    def test_slug_heuristic_parked_domain(self):
        """Slug heuristic: HEAD 200 but body contains parked domain signature — falls through."""
        from job_finder.web.homepage_discoverer import discover_homepage

        head_resp = _mock_head_response("https://acme.com", 200, "text/html")
        get_resp = _mock_response("https://acme.com", "<html><body>This domain is for sale!</body></html>")
        # DDG fallback also fails
        ddg_resp = _mock_response("https://html.duckduckgo.com/html/", DDG_HTML_NO_RESULTS)

        with patch("job_finder.web.homepage_discoverer.requests.head", return_value=head_resp), \
             patch("job_finder.web.homepage_discoverer.requests.get", side_effect=[get_resp, ddg_resp]):
            result = discover_homepage("Acme Corp", "greenhouse", "acme", [])

        # Falls through to DDG which also finds nothing
        assert result is None

    def test_slug_heuristic_non_html(self):
        """Slug heuristic: HEAD 200 but Content-Type is not HTML — falls through."""
        from job_finder.web.homepage_discoverer import discover_homepage

        head_resp = _mock_head_response("https://acme.com", 200, "text/plain")
        ddg_resp = _mock_response("https://html.duckduckgo.com/html/", DDG_HTML_NO_RESULTS)

        with patch("job_finder.web.homepage_discoverer.requests.head", return_value=head_resp), \
             patch("job_finder.web.homepage_discoverer.requests.get", return_value=ddg_resp):
            result = discover_homepage("Acme Corp", "greenhouse", "acme", [])

        assert result is None

    def test_slug_heuristic_404(self):
        """Slug heuristic: HEAD returning 404 falls through to DDG."""
        from job_finder.web.homepage_discoverer import discover_homepage

        head_resp = _mock_head_response("https://acme-corp.com", 404)
        ddg_resp = _mock_response("https://html.duckduckgo.com/html/", DDG_HTML_NO_RESULTS)

        with patch("job_finder.web.homepage_discoverer.requests.head", return_value=head_resp), \
             patch("job_finder.web.homepage_discoverer.requests.get", return_value=ddg_resp):
            result = discover_homepage("Acme Corp", "greenhouse", "acme-corp", [])

        assert result is None

    def test_ddg_fallback_success(self):
        """DDG fallback: valid result__a link with non-Wikipedia URL is validated and returned."""
        from job_finder.web.homepage_discoverer import discover_homepage

        # DDG GET response
        ddg_resp = _mock_response("https://html.duckduckgo.com/html/", DDG_HTML_WITH_RESULT)
        # Validation HEAD request for ramp.com
        validate_resp = _mock_head_response("https://ramp.com", 200, "text/html")

        with patch("job_finder.web.homepage_discoverer.requests.head", return_value=validate_resp), \
             patch("job_finder.web.homepage_discoverer.requests.get", return_value=ddg_resp):
            result = discover_homepage("Ramp", None, None, [])

        assert result == "https://ramp.com"

    def test_ddg_fallback_skips_wikipedia(self):
        """DDG fallback: Wikipedia as first result is skipped; second valid result is used."""
        from job_finder.web.homepage_discoverer import discover_homepage

        ddg_resp = _mock_response("https://html.duckduckgo.com/html/", DDG_HTML_WIKIPEDIA_FIRST)
        validate_resp = _mock_head_response("https://acme.com", 200, "text/html")

        with patch("job_finder.web.homepage_discoverer.requests.head", return_value=validate_resp), \
             patch("job_finder.web.homepage_discoverer.requests.get", return_value=ddg_resp):
            result = discover_homepage("Acme", None, None, [])

        assert result == "https://acme.com"

    def test_both_tiers_fail_returns_none(self):
        """Both slug heuristic (404) and DDG (no results) failing returns None."""
        from job_finder.web.homepage_discoverer import discover_homepage

        head_resp = _mock_head_response("https://unknown.com", 404)
        ddg_resp = _mock_response("https://html.duckduckgo.com/html/", DDG_HTML_NO_RESULTS)

        with patch("job_finder.web.homepage_discoverer.requests.head", return_value=head_resp), \
             patch("job_finder.web.homepage_discoverer.requests.get", return_value=ddg_resp):
            result = discover_homepage("Unknown Co", "greenhouse", "unknown", [])

        assert result is None

    def test_no_slug_skips_to_ddg(self):
        """No ats_slug provided: tier 1 is skipped, DDG is called directly."""
        from job_finder.web.homepage_discoverer import discover_homepage

        ddg_resp = _mock_response("https://html.duckduckgo.com/html/", DDG_HTML_WITH_RESULT)
        validate_resp = _mock_head_response("https://ramp.com", 200, "text/html")

        with patch("job_finder.web.homepage_discoverer.requests.head", return_value=validate_resp) as mock_head, \
             patch("job_finder.web.homepage_discoverer.requests.get", return_value=ddg_resp) as mock_get:
            result = discover_homepage("Ramp", None, None, [])

        # Should have called GET (for DDG) and HEAD (for validation only — not slug check)
        assert result == "https://ramp.com"
        mock_get.assert_called_once()  # Only DDG search GET

    def test_request_exception_returns_none(self):
        """Network error in both tiers returns None gracefully."""
        from job_finder.web.homepage_discoverer import discover_homepage

        with patch("job_finder.web.homepage_discoverer.requests.head", side_effect=Exception("timeout")), \
             patch("job_finder.web.homepage_discoverer.requests.get", side_effect=Exception("timeout")):
            result = discover_homepage("Acme", "greenhouse", "acme", [])

        assert result is None


# ---------------------------------------------------------------------------
# Tests: discover_homepages_batch
# ---------------------------------------------------------------------------

class TestDiscoverHomepagesBatch:

    def _make_db_with_companies(self, count: int, with_homepage: int = 0) -> str:
        """Create a temp SQLite DB with `count` companies, `with_homepage` already having homepage_url."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE companies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                name_raw TEXT NOT NULL,
                homepage_url TEXT DEFAULT NULL,
                ats_platform TEXT DEFAULT NULL,
                ats_slug TEXT DEFAULT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company TEXT NOT NULL,
                source_url TEXT DEFAULT ''
            )
        """)

        for i in range(count):
            homepage = f"https://company{i}.com" if i < with_homepage else None
            conn.execute(
                "INSERT INTO companies (name, name_raw, homepage_url, ats_platform, ats_slug) VALUES (?, ?, ?, ?, ?)",
                (f"Company {i}", f"company-{i}", homepage, "greenhouse", f"co{i}")
            )

        conn.commit()
        conn.close()
        return path

    def test_batch_updates_db(self):
        """Batch function discovers homepages and updates DB homepage_url."""
        from job_finder.web.homepage_discoverer import discover_homepages_batch

        path = self._make_db_with_companies(3)

        try:
            with patch("job_finder.web.homepage_discoverer.discover_homepage") as mock_discover, \
                 patch("job_finder.web.homepage_discoverer.time.sleep"):
                mock_discover.side_effect = [
                    "https://company0.com",
                    "https://company1.com",
                    None,  # company2 not found
                ]
                result = discover_homepages_batch(path)

            assert result["companies_checked"] == 3
            assert result["homepages_found"] == 2

            conn = sqlite3.connect(path)
            rows = conn.execute("SELECT id, homepage_url FROM companies ORDER BY id").fetchall()
            conn.close()

            assert rows[0][1] == "https://company0.com"
            assert rows[1][1] == "https://company1.com"
            assert rows[2][1] is None
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_batch_caps_at_50(self):
        """Batch function processes at most 50 companies per run even with 60 in DB."""
        from job_finder.web.homepage_discoverer import discover_homepages_batch

        path = self._make_db_with_companies(60)

        try:
            with patch("job_finder.web.homepage_discoverer.discover_homepage") as mock_discover, \
                 patch("job_finder.web.homepage_discoverer.time.sleep"):
                mock_discover.return_value = None
                result = discover_homepages_batch(path)

            assert result["companies_checked"] == 50
            assert mock_discover.call_count == 50
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_batch_skips_companies_with_existing_homepage(self):
        """Batch only processes companies where homepage_url IS NULL."""
        from job_finder.web.homepage_discoverer import discover_homepages_batch

        path = self._make_db_with_companies(5, with_homepage=3)  # 3 have homepage, 2 don't

        try:
            with patch("job_finder.web.homepage_discoverer.discover_homepage") as mock_discover, \
                 patch("job_finder.web.homepage_discoverer.time.sleep"):
                mock_discover.return_value = None
                result = discover_homepages_batch(path)

            assert result["companies_checked"] == 2
            assert mock_discover.call_count == 2
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_batch_returns_summary_dict(self):
        """Batch returns dict with companies_checked, homepages_found, errors keys."""
        from job_finder.web.homepage_discoverer import discover_homepages_batch

        path = self._make_db_with_companies(0)

        try:
            result = discover_homepages_batch(path)

            assert "companies_checked" in result
            assert "homepages_found" in result
            assert "errors" in result
        finally:
            if os.path.exists(path):
                os.remove(path)
