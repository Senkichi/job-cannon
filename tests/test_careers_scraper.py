"""Tests for careers_scraper.py module.

Covers:
- find_careers_url: detect /careers, /jobs links from homepage HTML
- find_careers_url: handle relative URLs, ATS redirects, no match
- find_careers_url: proactive subdomain probing (careers.*, jobs.*, etc.)
- scrape_careers_page: extract keyword-matched job listings from static HTML
- scrape_careers_page: exclusion keyword filtering, JS-rendered fallback
"""

from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers: build mock response objects
# ---------------------------------------------------------------------------


def _mock_response(url, text, status_code=200):
    """Create a mock requests.Response."""
    resp = MagicMock()
    resp.url = url
    resp.text = text
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    return resp


# ---------------------------------------------------------------------------
# Tests: find_careers_url
# ---------------------------------------------------------------------------


class TestFindCareersUrl:
    def test_finds_careers_link_from_homepage(self):
        """/careers link on homepage is returned as absolute URL."""
        from job_finder.web.careers_scraper import find_careers_url

        html = '<html><body><a href="/careers">Careers</a></body></html>'
        resp = _mock_response("https://example.com/", html)

        with patch("job_finder.web.careers_scraper.requests.get", return_value=resp):
            result = find_careers_url("https://example.com/")

        assert result == "https://example.com/careers"

    def test_finds_jobs_link_from_homepage(self):
        """/jobs link on homepage is returned as absolute URL."""
        from job_finder.web.careers_scraper import find_careers_url

        html = '<html><body><a href="/jobs">Jobs</a></body></html>'
        resp = _mock_response("https://example.com/", html)

        with patch("job_finder.web.careers_scraper.requests.get", return_value=resp):
            result = find_careers_url("https://example.com/")

        assert result == "https://example.com/jobs"

    def test_returns_none_when_no_careers_link(self):
        """Returns None when no careers-related links found on homepage."""
        from job_finder.web.careers_scraper import find_careers_url

        html = '<html><body><a href="/about">About</a><a href="/contact">Contact</a></body></html>'
        resp = _mock_response("https://example.com/", html)

        with patch("job_finder.web.careers_scraper.requests.get", return_value=resp):
            result = find_careers_url("https://example.com/")

        assert result is None

    def test_handles_relative_url_with_domain(self):
        """Relative /careers path is combined with homepage domain."""
        from job_finder.web.careers_scraper import find_careers_url

        html = '<html><body><a href="/openings">Open Roles</a></body></html>'
        resp = _mock_response("https://startup.io/", html)

        with patch("job_finder.web.careers_scraper.requests.get", return_value=resp):
            result = find_careers_url("https://startup.io/")

        assert result == "https://startup.io/openings"

    def test_detects_ats_redirect_returns_none(self):
        """Returns None when homepage redirects to known ATS domain (Research Pitfall 6)."""
        from job_finder.web.careers_scraper import find_careers_url

        html = "<html><body>Redirected to Lever</body></html>"
        # Simulate redirect to Lever
        resp = _mock_response("https://jobs.lever.co/acme", html)

        with patch("job_finder.web.careers_scraper.requests.get", return_value=resp):
            result = find_careers_url("https://example.com/careers")

        # ATS redirect: return None (caller should extract slug from r.url instead)
        assert result is None

    def test_handles_absolute_url_link(self):
        """Absolute /careers URL on homepage is returned as-is."""
        from job_finder.web.careers_scraper import find_careers_url

        html = '<html><body><a href="https://example.com/careers">Join Us</a></body></html>'
        resp = _mock_response("https://example.com/", html)

        with patch("job_finder.web.careers_scraper.requests.get", return_value=resp):
            result = find_careers_url("https://example.com/")

        assert result == "https://example.com/careers"

    def test_returns_none_on_request_exception(self):
        """Returns None gracefully when requests.get raises an exception."""
        from job_finder.web.careers_scraper import find_careers_url

        with patch(
            "job_finder.web.careers_scraper.requests.get", side_effect=Exception("timeout")
        ):
            result = find_careers_url("https://example.com/")

        assert result is None

    def test_greenhouse_redirect_returns_none(self):
        """Returns None when redirected to Greenhouse ATS domain."""
        from job_finder.web.careers_scraper import find_careers_url

        html = "<html><body>Greenhouse</body></html>"
        resp = _mock_response("https://boards.greenhouse.io/acme", html)

        with patch("job_finder.web.careers_scraper.requests.get", return_value=resp):
            result = find_careers_url("https://example.com/")

        assert result is None

    def test_ashby_redirect_returns_none(self):
        """Returns None when redirected to Ashby ATS domain."""
        from job_finder.web.careers_scraper import find_careers_url

        html = "<html><body>Ashby</body></html>"
        resp = _mock_response("https://jobs.ashbyhq.com/acme", html)

        with patch("job_finder.web.careers_scraper.requests.get", return_value=resp):
            result = find_careers_url("https://example.com/")

        assert result is None

    def test_join_link_detected(self):
        """/join link is recognized as a careers page pattern."""
        from job_finder.web.careers_scraper import find_careers_url

        html = '<html><body><a href="/join">Join our team</a></body></html>'
        resp = _mock_response("https://example.com/", html)

        with patch("job_finder.web.careers_scraper.requests.get", return_value=resp):
            result = find_careers_url("https://example.com/")

        assert result == "https://example.com/join"


# ---------------------------------------------------------------------------
# Tests: scrape_careers_page
# ---------------------------------------------------------------------------


class TestScrapeCareersPage:
    def test_extracts_matching_job_title_links(self):
        """scrape_careers_page returns jobs whose title matches target keywords."""
        from job_finder.web.careers_scraper import scrape_careers_page

        html = """
        <html><body>
          <a href="/jobs/1">Staff Data Scientist</a>
          <a href="/jobs/2">Software Engineer</a>
          <a href="/jobs/3">Senior Data Scientist</a>
        </body></html>
        """
        resp = _mock_response("https://example.com/careers", html)

        with patch("job_finder.web.careers_scraper.requests.get", return_value=resp):
            results = scrape_careers_page(
                "https://example.com/careers",
                target_titles=["Data Scientist"],
                exclusions=[],
            )

        assert len(results) == 2
        titles = [r["title"] for r in results]
        assert "Staff Data Scientist" in titles
        assert "Senior Data Scientist" in titles

    def test_excludes_jobs_matching_exclusion_keywords(self):
        """scrape_careers_page excludes jobs matching exclusion keywords."""
        from job_finder.web.careers_scraper import scrape_careers_page

        html = """
        <html><body>
          <a href="/jobs/1">Staff Data Scientist</a>
          <a href="/jobs/2">Data Scientist - Intern</a>
        </body></html>
        """
        resp = _mock_response("https://example.com/careers", html)

        with patch("job_finder.web.careers_scraper.requests.get", return_value=resp):
            results = scrape_careers_page(
                "https://example.com/careers",
                target_titles=["Data Scientist"],
                exclusions=["Intern"],
            )

        assert len(results) == 1
        assert results[0]["title"] == "Staff Data Scientist"

    def test_returns_empty_list_for_js_rendered_pages(self):
        """scrape_careers_page returns [] for JS-rendered pages with no <a> job links."""
        from job_finder.web.careers_scraper import scrape_careers_page

        # JS-rendered page: no actual job links in the HTML
        html = "<html><body><div id='root'></div><script>loadJobs()</script></body></html>"
        resp = _mock_response("https://example.com/careers", html)

        with patch("job_finder.web.careers_scraper.requests.get", return_value=resp):
            results = scrape_careers_page(
                "https://example.com/careers",
                target_titles=["Data Scientist"],
                exclusions=[],
            )

        assert results == []

    def test_returns_empty_list_on_request_error(self):
        """scrape_careers_page returns [] gracefully on request failure."""
        from job_finder.web.careers_scraper import scrape_careers_page

        with patch(
            "job_finder.web.careers_scraper.requests.get", side_effect=Exception("timeout")
        ):
            results = scrape_careers_page(
                "https://example.com/careers",
                target_titles=["Data Scientist"],
                exclusions=[],
            )

        assert results == []

    def test_result_dict_has_required_keys(self):
        """Each result dict has 'title' and 'url' keys."""
        from job_finder.web.careers_scraper import scrape_careers_page

        html = '<html><body><a href="/jobs/1">Data Scientist</a></body></html>'
        resp = _mock_response("https://example.com/careers", html)

        with patch("job_finder.web.careers_scraper.requests.get", return_value=resp):
            results = scrape_careers_page(
                "https://example.com/careers",
                target_titles=["Data Scientist"],
                exclusions=[],
            )

        assert len(results) == 1
        assert "title" in results[0]
        assert "url" in results[0]

    def test_handles_empty_target_titles_returns_all(self):
        """scrape_careers_page with empty target_titles returns all job links."""
        from job_finder.web.careers_scraper import scrape_careers_page

        html = """
        <html><body>
          <a href="/jobs/1">Data Scientist</a>
          <a href="/jobs/2">Engineer</a>
        </body></html>
        """
        resp = _mock_response("https://example.com/careers", html)

        with patch("job_finder.web.careers_scraper.requests.get", return_value=resp):
            results = scrape_careers_page(
                "https://example.com/careers",
                target_titles=[],
                exclusions=[],
            )

        # Empty target_titles means no filter — all links returned
        assert len(results) == 2


# ---------------------------------------------------------------------------
# Tests: Haiku fallback in find_careers_url
# ---------------------------------------------------------------------------


class TestHaikuFallback:
    """Tests for Haiku fallback in find_careers_url."""

    def test_haiku_fallback_called_when_no_heuristic_match(self):
        """Haiku fallback fires when no /careers or /jobs links found."""
        from job_finder.web.careers_scraper import find_careers_url

        html = '<html><body><a href="/about">About</a></body></html>'
        resp = _mock_response("https://example.com/", html)

        mock_client = MagicMock()
        mock_conn = MagicMock()
        mock_config = {}

        with (
            patch("job_finder.web.careers_scraper.requests.get", return_value=resp),
            patch(
                "job_finder.web.careers_scraper._find_careers_url_with_haiku",
                return_value="https://example.com/work-here",
            ) as mock_haiku,
        ):
            result = find_careers_url("https://example.com/", conn=mock_conn, config=mock_config)

        mock_haiku.assert_called_once()
        assert result == "https://example.com/work-here"

    def test_haiku_not_called_when_heuristic_succeeds(self):
        """Haiku fallback is NOT called when heuristic finds a careers link."""
        from job_finder.web.careers_scraper import find_careers_url

        html = '<html><body><a href="/careers">Careers</a></body></html>'
        resp = _mock_response("https://example.com/", html)

        mock_client = MagicMock()
        mock_conn = MagicMock()

        with (
            patch("job_finder.web.careers_scraper.requests.get", return_value=resp),
            patch("job_finder.web.careers_scraper._find_careers_url_with_haiku") as mock_haiku,
        ):
            result = find_careers_url("https://example.com/", conn=mock_conn, config={})

        mock_haiku.assert_not_called()
        assert result == "https://example.com/careers"

    def test_haiku_not_called_without_client(self):
        """Haiku fallback is NOT called when client param is None (backward compat)."""
        from job_finder.web.careers_scraper import find_careers_url

        html = '<html><body><a href="/about">About</a></body></html>'
        resp = _mock_response("https://example.com/", html)

        with (
            patch("job_finder.web.careers_scraper.requests.get", return_value=resp),
            patch("job_finder.web.careers_scraper._find_careers_url_with_haiku") as mock_haiku,
        ):
            result = find_careers_url("https://example.com/")

        mock_haiku.assert_not_called()
        assert result is None

    def test_haiku_returns_none_fallback_returns_none(self):
        """find_careers_url returns None when Haiku fallback also returns None."""
        from job_finder.web.careers_scraper import find_careers_url

        html = '<html><body><a href="/about">About</a></body></html>'
        resp = _mock_response("https://example.com/", html)

        with (
            patch("job_finder.web.careers_scraper.requests.get", return_value=resp),
            patch(
                "job_finder.web.careers_scraper._find_careers_url_with_haiku", return_value=None
            ),
        ):
            result = find_careers_url("https://example.com/", conn=MagicMock(), config={})

        assert result is None


# ---------------------------------------------------------------------------
# Tests: Rich JD extraction via job link following
# ---------------------------------------------------------------------------


class TestRichJdExtraction:
    """Tests for rich JD extraction via job link following."""

    def test_scrape_careers_page_fetches_job_descriptions(self):
        """scrape_careers_page follows job links to fetch full descriptions."""
        from job_finder.web.careers_scraper import scrape_careers_page

        careers_html = '<html><body><a href="/jobs/1">Data Scientist</a></body></html>'
        job_html = "<html><body><p>We are looking for a data scientist...</p></body></html>"

        careers_resp = _mock_response("https://example.com/careers", careers_html)
        job_resp = _mock_response("https://example.com/jobs/1", job_html)

        def side_effect(url, **kwargs):
            if "careers" in url:
                return careers_resp
            return job_resp

        with (
            patch("job_finder.web.careers_scraper.requests.get", side_effect=side_effect),
            patch("job_finder.web.careers_scraper.time.sleep"),
        ):
            results = scrape_careers_page(
                "https://example.com/careers",
                target_titles=["Data Scientist"],
                exclusions=[],
            )

        assert len(results) == 1
        assert results[0]["description"] != ""
        assert "data scientist" in results[0]["description"].lower()

    def test_auth_wall_returns_empty_description(self):
        """Auth-wall job pages return empty description."""
        from job_finder.web.careers_scraper import scrape_careers_page

        careers_html = '<html><body><a href="/jobs/1">Data Scientist</a></body></html>'
        job_html = "<html><body><p>Sign in or join to continue</p></body></html>"

        careers_resp = _mock_response("https://example.com/careers", careers_html)
        job_resp = _mock_response("https://example.com/jobs/1", job_html)

        def side_effect(url, **kwargs):
            if "careers" in url:
                return careers_resp
            return job_resp

        with (
            patch("job_finder.web.careers_scraper.requests.get", side_effect=side_effect),
            patch("job_finder.web.careers_scraper.time.sleep"),
        ):
            results = scrape_careers_page(
                "https://example.com/careers",
                target_titles=["Data Scientist"],
                exclusions=[],
            )

        assert len(results) == 1
        assert results[0]["description"] == ""

    def test_result_dicts_have_description_key(self):
        """Every result dict includes a 'description' key."""
        from job_finder.web.careers_scraper import scrape_careers_page

        html = '<html><body><a href="/jobs/1">Data Scientist</a></body></html>'
        resp = _mock_response("https://example.com/careers", html)
        job_resp = _mock_response(
            "https://example.com/jobs/1", "<html><body>JD text</body></html>"
        )

        def side_effect(url, **kwargs):
            if "careers" in url:
                return resp
            return job_resp

        with (
            patch("job_finder.web.careers_scraper.requests.get", side_effect=side_effect),
            patch("job_finder.web.careers_scraper.time.sleep"),
        ):
            results = scrape_careers_page(
                "https://example.com/careers",
                target_titles=["Data Scientist"],
                exclusions=[],
            )

        assert "description" in results[0]


# ---------------------------------------------------------------------------
# Tests: Haiku job extraction fallback in scrape_careers_page
# ---------------------------------------------------------------------------


class TestHaikuJobExtraction:
    """Tests for _extract_jobs_with_haiku fallback."""

    def test_haiku_fallback_called_when_no_jobs_found(self):
        """Haiku fallback fires when HTML parsing finds 0 matching jobs."""
        from job_finder.web.careers_scraper import scrape_careers_page

        # Careers page with no matching job links
        html = '<html><body><a href="/about">About Us</a></body></html>'
        resp = _mock_response("https://example.com/careers", html)

        with (
            patch("job_finder.web.careers_scraper.requests.get", return_value=resp),
            patch(
                "job_finder.web.careers_scraper._extract_jobs_with_haiku",
                return_value=[{"title": "Data Scientist", "url": "", "description": ""}],
            ) as mock_haiku,
        ):
            results = scrape_careers_page(
                "https://example.com/careers",
                target_titles=["Data Scientist"],
                exclusions=[],
                conn=MagicMock(),
                config={},
            )

        mock_haiku.assert_called_once()
        assert len(results) == 1

    def test_haiku_not_called_when_jobs_found(self):
        """Haiku fallback is NOT called when HTML parsing finds jobs."""
        from job_finder.web.careers_scraper import scrape_careers_page

        html = '<html><body><a href="/jobs/1">Data Scientist</a></body></html>'
        resp = _mock_response("https://example.com/careers", html)
        job_resp = _mock_response("https://example.com/jobs/1", "<html><body>JD</body></html>")

        def side_effect(url, **kwargs):
            if "careers" in url:
                return resp
            return job_resp

        with (
            patch("job_finder.web.careers_scraper.requests.get", side_effect=side_effect),
            patch("job_finder.web.careers_scraper.time.sleep"),
            patch("job_finder.web.careers_scraper._extract_jobs_with_haiku") as mock_haiku,
        ):
            results = scrape_careers_page(
                "https://example.com/careers",
                target_titles=["Data Scientist"],
                exclusions=[],
                conn=MagicMock(),
                config={},
            )

        mock_haiku.assert_not_called()
        assert len(results) == 1

    def test_haiku_not_called_without_client(self):
        """Haiku fallback is NOT called without client (backward compat)."""
        from job_finder.web.careers_scraper import scrape_careers_page

        html = '<html><body><a href="/about">About</a></body></html>'
        resp = _mock_response("https://example.com/careers", html)

        with (
            patch("job_finder.web.careers_scraper.requests.get", return_value=resp),
            patch("job_finder.web.careers_scraper._extract_jobs_with_haiku") as mock_haiku,
        ):
            results = scrape_careers_page(
                "https://example.com/careers",
                target_titles=["Data Scientist"],
                exclusions=[],
            )

        mock_haiku.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: careers subdomain detection
# ---------------------------------------------------------------------------


class TestCareersSubdomainDetection:
    def test_redirect_to_careers_subdomain_returned_directly(self):
        """Homepage redirecting to careers.example.com is returned as-is."""
        from job_finder.web.careers_scraper import find_careers_url

        html = "<html><body>Careers</body></html>"
        resp = _mock_response("https://careers.example.com/", html)

        with patch("job_finder.web.careers_scraper.requests.get", return_value=resp):
            result = find_careers_url("https://example.com/")

        assert result == "https://careers.example.com/"

    def test_redirect_to_jobs_subdomain_returned_directly(self):
        """Homepage redirecting to jobs.example.com is returned as-is."""
        from job_finder.web.careers_scraper import find_careers_url

        html = "<html><body>Jobs</body></html>"
        resp = _mock_response("https://jobs.example.com/", html)

        with patch("job_finder.web.careers_scraper.requests.get", return_value=resp):
            result = find_careers_url("https://example.com/")

        assert result == "https://jobs.example.com/"

    def test_absolute_link_to_careers_subdomain_detected(self):
        """Absolute href pointing to careers.company.com is returned."""
        from job_finder.web.careers_scraper import find_careers_url

        html = '<html><body><a href="https://careers.company.com/">Work with us</a></body></html>'
        resp = _mock_response("https://company.com/", html)

        with patch("job_finder.web.careers_scraper.requests.get", return_value=resp):
            result = find_careers_url("https://company.com/")

        assert result == "https://careers.company.com/"

    def test_careers_subdomain_not_returned_for_ats_domains(self):
        """jobs.lever.co is ATS — subdomain match must not short-circuit ATS check."""
        from job_finder.web.careers_scraper import find_careers_url

        html = "<html><body>Lever</body></html>"
        # Final URL is an ATS domain starting with "jobs." — ATS check wins
        resp = _mock_response("https://jobs.lever.co/acme", html)

        with patch("job_finder.web.careers_scraper.requests.get", return_value=resp):
            result = find_careers_url("https://example.com/")

        assert result is None

    def test_work_subdomain_detected(self):
        """Homepage redirecting to work.company.com is returned as-is."""
        from job_finder.web.careers_scraper import find_careers_url

        html = "<html><body>Work</body></html>"
        resp = _mock_response("https://work.company.com/", html)

        with patch("job_finder.web.careers_scraper.requests.get", return_value=resp):
            result = find_careers_url("https://company.com/")

        assert result == "https://work.company.com/"


# ---------------------------------------------------------------------------
# Tests: meta-refresh detection
# ---------------------------------------------------------------------------


class TestMetaRefreshDetection:
    def test_meta_refresh_to_careers_subdomain_followed(self):
        """Meta-refresh pointing to careers subdomain URL is returned."""
        from job_finder.web.careers_scraper import find_careers_url

        html = (
            "<html><head>"
            '<meta http-equiv="refresh" content="0; url=https://careers.example.com/">'
            "</head><body></body></html>"
        )
        resp = _mock_response("https://example.com/", html)

        with patch("job_finder.web.careers_scraper.requests.get", return_value=resp):
            result = find_careers_url("https://example.com/")

        assert result == "https://careers.example.com/"

    def test_meta_refresh_to_careers_path_followed(self):
        """Meta-refresh pointing to /careers path is returned."""
        from job_finder.web.careers_scraper import find_careers_url

        html = (
            "<html><head>"
            '<meta http-equiv="refresh" content="0;url=/careers">'
            "</head><body></body></html>"
        )
        resp = _mock_response("https://example.com/", html)

        with patch("job_finder.web.careers_scraper.requests.get", return_value=resp):
            result = find_careers_url("https://example.com/")

        assert result == "https://example.com/careers"

    def test_meta_refresh_to_ats_domain_not_followed(self):
        """Meta-refresh pointing to ATS domain returns None (not followed)."""
        from job_finder.web.careers_scraper import find_careers_url

        html = (
            "<html><head>"
            '<meta http-equiv="refresh" content="0; url=https://jobs.lever.co/acme">'
            "</head><body></body></html>"
        )
        resp = _mock_response("https://example.com/", html)

        with patch("job_finder.web.careers_scraper.requests.get", return_value=resp):
            result = find_careers_url("https://example.com/")

        assert result is None

    def test_meta_refresh_to_unrelated_url_ignored(self):
        """Meta-refresh to an unrelated URL (no careers pattern) is ignored; falls through."""
        from job_finder.web.careers_scraper import find_careers_url

        html = (
            "<html><head>"
            '<meta http-equiv="refresh" content="0; url=https://marketing.example.com/">'
            '</head><body><a href="/careers">Careers</a></body></html>'
        )
        resp = _mock_response("https://example.com/", html)

        with patch("job_finder.web.careers_scraper.requests.get", return_value=resp):
            result = find_careers_url("https://example.com/")

        # Meta-refresh to non-careers URL is ignored; link scraping finds /careers
        assert result == "https://example.com/careers"


# ---------------------------------------------------------------------------
# Tests: _extract_base_domain
# ---------------------------------------------------------------------------


class TestExtractBaseDomain:
    def test_strips_www_prefix(self):
        from job_finder.web.careers_scraper import _extract_base_domain

        assert _extract_base_domain("https://www.google.com/") == "google.com"

    def test_no_www_passthrough(self):
        from job_finder.web.careers_scraper import _extract_base_domain

        assert _extract_base_domain("https://nvidia.com/en-us/") == "nvidia.com"

    def test_empty_url_returns_none(self):
        from job_finder.web.careers_scraper import _extract_base_domain

        assert _extract_base_domain("") is None


# ---------------------------------------------------------------------------
# Tests: subdomain probing
# ---------------------------------------------------------------------------


class TestSubdomainProbing:
    """Tests for the proactive subdomain probe step in find_careers_url."""

    def _bare_homepage_response(self):
        """Mock a homepage with no careers links (like google.com)."""
        html = "<html><body><input type='text' name='q'></body></html>"
        return _mock_response("https://www.example.com/", html)

    def _head_response(self, url, status_code=200):
        resp = MagicMock()
        resp.url = url
        resp.status_code = status_code
        return resp

    def test_subdomain_probe_finds_careers_subdomain(self):
        """HEAD to careers.example.com returns 200 — function returns it."""
        from job_finder.web.careers_scraper import find_careers_url

        get_resp = self._bare_homepage_response()
        head_resp = self._head_response("https://careers.example.com/")

        with patch("job_finder.web.careers_scraper.requests.get", return_value=get_resp):
            with patch("job_finder.web.careers_scraper.requests.head", return_value=head_resp):
                result = find_careers_url("https://www.example.com/")

        assert result == "https://careers.example.com/"

    def test_subdomain_probe_skips_404(self):
        """All subdomain probes return 404 — falls through to None."""
        from job_finder.web.careers_scraper import find_careers_url

        get_resp = self._bare_homepage_response()
        head_resp = self._head_response("https://careers.example.com/", status_code=404)

        with patch("job_finder.web.careers_scraper.requests.get", return_value=get_resp):
            with patch("job_finder.web.careers_scraper.requests.head", return_value=head_resp):
                result = find_careers_url("https://www.example.com/")

        assert result is None

    def test_subdomain_probe_validates_final_url(self):
        """Probe returns 200 but final URL is main site (redirect bounce) — rejected."""
        from job_finder.web.careers_scraper import find_careers_url

        get_resp = self._bare_homepage_response()
        # careers.example.com redirected back to www.example.com
        head_resp = self._head_response("https://www.example.com/", status_code=200)

        with patch("job_finder.web.careers_scraper.requests.get", return_value=get_resp):
            with patch("job_finder.web.careers_scraper.requests.head", return_value=head_resp):
                result = find_careers_url("https://www.example.com/")

        assert result is None

    def test_subdomain_probe_accepts_careers_path_redirect(self):
        """Probe redirects to www.example.com/careers — accepted via path match."""
        from job_finder.web.careers_scraper import find_careers_url

        get_resp = self._bare_homepage_response()
        head_resp = self._head_response("https://www.example.com/careers", status_code=200)

        with patch("job_finder.web.careers_scraper.requests.get", return_value=get_resp):
            with patch("job_finder.web.careers_scraper.requests.head", return_value=head_resp):
                result = find_careers_url("https://www.example.com/")

        assert result == "https://www.example.com/careers"

    def test_subdomain_probe_skips_ats_redirect(self):
        """Probe redirects to jobs.lever.co — rejected as ATS domain."""
        from job_finder.web.careers_scraper import find_careers_url

        get_resp = self._bare_homepage_response()
        head_resp = self._head_response("https://jobs.lever.co/acme", status_code=200)

        with patch("job_finder.web.careers_scraper.requests.get", return_value=get_resp):
            with patch("job_finder.web.careers_scraper.requests.head", return_value=head_resp):
                result = find_careers_url("https://www.example.com/")

        assert result is None

    def test_subdomain_probe_not_called_when_link_found(self):
        """If heuristic finds /careers link, no HEAD probes are made."""
        from job_finder.web.careers_scraper import find_careers_url

        html = '<html><body><a href="/careers">Join us</a></body></html>'
        get_resp = _mock_response("https://www.example.com/", html)

        with patch("job_finder.web.careers_scraper.requests.get", return_value=get_resp):
            with patch("job_finder.web.careers_scraper.requests.head") as mock_head:
                result = find_careers_url("https://www.example.com/")

        assert result == "https://www.example.com/careers"
        mock_head.assert_not_called()

    def test_subdomain_probe_handles_connection_error(self):
        """Network error during HEAD probe is swallowed — tries next subdomain."""
        from requests.exceptions import ConnectionError

        from job_finder.web.careers_scraper import find_careers_url

        get_resp = self._bare_homepage_response()

        def head_side_effect(url, **kwargs):
            if "careers." in url:
                raise ConnectionError("DNS resolution failed")
            # jobs. subdomain works
            return self._head_response("https://jobs.example.com/")

        with patch("job_finder.web.careers_scraper.requests.get", return_value=get_resp):
            with patch(
                "job_finder.web.careers_scraper.requests.head", side_effect=head_side_effect
            ):
                result = find_careers_url("https://www.example.com/")

        assert result == "https://jobs.example.com/"


# ---------------------------------------------------------------------------
# Cascade dispatch tests — _find_careers_url_with_haiku + _extract_jobs_with_haiku
# ---------------------------------------------------------------------------


class TestFindCareersUrlCascade:
    """Dispatch pattern tests for _find_careers_url_with_haiku."""

    def test_uses_call_model_when_providers_configured(
        self,
        migrated_db,
        cascade_config_haiku,
        make_model_result,
    ):
        from job_finder.web.careers_scraper import _find_careers_url_with_haiku

        _path, conn = migrated_db

        with (
            patch("job_finder.web.careers_scraper.call_model") as mock_cm,
            patch("job_finder.web.careers_scraper.call_claude") as mock_cc,
        ):
            mock_cm.return_value = make_model_result({"url": "https://example.com/careers"})
            result = _find_careers_url_with_haiku(
                "https://example.com/",
                "<html></html>",
                conn,
                cascade_config_haiku,
            )

        mock_cm.assert_called_once()
        assert mock_cm.call_args.kwargs["tier"] == "haiku"
        assert mock_cm.call_args.kwargs["purpose"] == "careers_scrape"
        mock_cc.assert_not_called()
        assert result == "https://example.com/careers"

    def test_uses_call_claude_when_no_providers(self, migrated_db):
        from job_finder.web.careers_scraper import _find_careers_url_with_haiku

        _path, conn = migrated_db

        with (
            patch("job_finder.web.careers_scraper.call_model") as mock_cm,
            patch("job_finder.web.careers_scraper.call_claude") as mock_cc,
        ):
            mock_cc.return_value = ({"url": "https://example.com/careers"}, 0.001)
            result = _find_careers_url_with_haiku(
                "https://example.com/",
                "<html></html>",
                conn,
                config={},
            )

        mock_cm.assert_not_called()
        mock_cc.assert_called_once()
        assert result == "https://example.com/careers"

    def test_cascade_exhausted_falls_back_to_cli(
        self,
        migrated_db,
        cascade_config_haiku,
    ):
        from job_finder.web.careers_scraper import _find_careers_url_with_haiku
        from job_finder.web.model_provider import ProviderCascadeExhaustedError

        _path, conn = migrated_db

        with (
            patch("job_finder.web.careers_scraper.call_model") as mock_cm,
            patch("job_finder.web.careers_scraper.call_claude") as mock_cc,
        ):
            mock_cm.side_effect = ProviderCascadeExhaustedError("exhausted")
            mock_cc.return_value = ({"url": "https://example.com/careers"}, 0.001)
            result = _find_careers_url_with_haiku(
                "https://example.com/",
                "<html></html>",
                conn,
                cascade_config_haiku,
            )

        mock_cm.assert_called_once()
        mock_cc.assert_called_once()
        assert result == "https://example.com/careers"

    def test_cascade_and_cli_both_fail_returns_none(
        self,
        migrated_db,
        cascade_config_haiku,
    ):
        from job_finder.web.careers_scraper import _find_careers_url_with_haiku
        from job_finder.web.model_provider import ProviderCascadeExhaustedError

        _path, conn = migrated_db

        with (
            patch("job_finder.web.careers_scraper.call_model") as mock_cm,
            patch("job_finder.web.careers_scraper.call_claude") as mock_cc,
        ):
            mock_cm.side_effect = ProviderCascadeExhaustedError("exhausted")
            mock_cc.side_effect = RuntimeError("CLI unavailable")
            result = _find_careers_url_with_haiku(
                "https://example.com/",
                "<html></html>",
                conn,
                cascade_config_haiku,
            )

        assert result is None


class TestExtractJobsCascade:
    """Dispatch pattern tests for _extract_jobs_with_haiku."""

    _JOBS_PAYLOAD = {
        "jobs": [
            {"title": "Data Scientist", "url": "/jobs/1", "location": "Remote"},
            {"title": "Junior Analyst", "url": "/jobs/2", "location": "NYC"},
        ]
    }

    def test_uses_call_model_when_providers_configured(
        self,
        migrated_db,
        cascade_config_haiku,
        make_model_result,
    ):
        from job_finder.web.careers_scraper import _extract_jobs_with_haiku

        _path, conn = migrated_db

        with (
            patch("job_finder.web.careers_scraper.call_model") as mock_cm,
            patch("job_finder.web.careers_scraper.call_claude") as mock_cc,
        ):
            mock_cm.return_value = make_model_result(self._JOBS_PAYLOAD)
            results = _extract_jobs_with_haiku(
                "https://example.com/careers",
                "<html></html>",
                target_titles=["data scientist"],
                exclusions=["junior"],
                conn=conn,
                config=cascade_config_haiku,
            )

        mock_cm.assert_called_once()
        assert mock_cm.call_args.kwargs["tier"] == "haiku"
        assert mock_cm.call_args.kwargs["purpose"] == "careers_scrape"
        mock_cc.assert_not_called()
        titles = [j["title"] for j in results]
        assert "Data Scientist" in titles
        assert "Junior Analyst" not in titles  # excluded

    def test_uses_call_claude_when_no_providers(self, migrated_db):
        from job_finder.web.careers_scraper import _extract_jobs_with_haiku

        _path, conn = migrated_db

        with (
            patch("job_finder.web.careers_scraper.call_model") as mock_cm,
            patch("job_finder.web.careers_scraper.call_claude") as mock_cc,
        ):
            mock_cc.return_value = (self._JOBS_PAYLOAD, 0.001)
            results = _extract_jobs_with_haiku(
                "https://example.com/careers",
                "<html></html>",
                target_titles=["data scientist"],
                exclusions=[],
                conn=conn,
                config={},
            )

        mock_cm.assert_not_called()
        mock_cc.assert_called_once()
        assert results and results[0]["title"] == "Data Scientist"

    def test_cascade_exhausted_falls_back_to_cli(
        self,
        migrated_db,
        cascade_config_haiku,
    ):
        from job_finder.web.careers_scraper import _extract_jobs_with_haiku
        from job_finder.web.model_provider import ProviderCascadeExhaustedError

        _path, conn = migrated_db

        with (
            patch("job_finder.web.careers_scraper.call_model") as mock_cm,
            patch("job_finder.web.careers_scraper.call_claude") as mock_cc,
        ):
            mock_cm.side_effect = ProviderCascadeExhaustedError("exhausted")
            mock_cc.return_value = (self._JOBS_PAYLOAD, 0.001)
            results = _extract_jobs_with_haiku(
                "https://example.com/careers",
                "<html></html>",
                target_titles=["data scientist"],
                exclusions=[],
                conn=conn,
                config=cascade_config_haiku,
            )

        mock_cm.assert_called_once()
        mock_cc.assert_called_once()
        assert results and results[0]["title"] == "Data Scientist"

    def test_cascade_and_cli_both_fail_returns_empty_list(
        self,
        migrated_db,
        cascade_config_haiku,
    ):
        from job_finder.web.careers_scraper import _extract_jobs_with_haiku
        from job_finder.web.model_provider import ProviderCascadeExhaustedError

        _path, conn = migrated_db

        with (
            patch("job_finder.web.careers_scraper.call_model") as mock_cm,
            patch("job_finder.web.careers_scraper.call_claude") as mock_cc,
        ):
            mock_cm.side_effect = ProviderCascadeExhaustedError("exhausted")
            mock_cc.side_effect = RuntimeError("CLI unavailable")
            results = _extract_jobs_with_haiku(
                "https://example.com/careers",
                "<html></html>",
                target_titles=["data scientist"],
                exclusions=[],
                conn=conn,
                config=cascade_config_haiku,
            )

        assert results == []
