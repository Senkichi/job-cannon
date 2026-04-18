"""Unit tests for job_finder.web.enrichment_tiers — new surface area only.

Covers:
- is_short_auth_page() boundary conditions (len < 2000 + signal, len >= 2000, no signal)
- search_serpapi() 2-tuple return contract
  - (None, []) when no results
  - (None, []) on generic exception
  - apply_options filtered via is_blocked_domain()
  - apply_options sorted by domain_priority()
  - "url_jd" key written when fetch_direct_jd succeeds on ATS URL
"""

import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# is_short_auth_page()
# ---------------------------------------------------------------------------


class TestIsShortAuthPage:
    """Boundary tests for is_short_auth_page().

    Contract:
    - Returns True  when len(text) < 2000 AND any _AUTH_WALL_SIGNATURES in text[:500].lower()
    - Returns False when len(text) >= 2000 (even if signal present)
    - Returns False when signal absent (even if short)
    - Returns False for empty string
    """

    def test_short_page_with_sign_in_signal(self):
        """len < 2000 + 'sign in' in first 500 chars → True."""
        from job_finder.web.enrichment_tiers import is_short_auth_page

        text = "Please sign in to continue." + "x" * 50
        assert len(text) < 2000
        assert is_short_auth_page(text) is True

    def test_short_page_with_log_in_signal(self):
        """'log in' signal in short page → True."""
        from job_finder.web.enrichment_tiers import is_short_auth_page

        text = "You must log in to view this job posting."
        assert is_short_auth_page(text) is True

    def test_short_page_with_captcha_signal(self):
        """'captcha' signal in short page → True."""
        from job_finder.web.enrichment_tiers import is_short_auth_page

        text = "captcha verification required before proceeding."
        assert is_short_auth_page(text) is True

    def test_short_page_with_just_a_moment_signal(self):
        """'just a moment' (Cloudflare) in short page → True."""
        from job_finder.web.enrichment_tiers import is_short_auth_page

        text = "Just a moment... Cloudflare is verifying your browser."
        assert is_short_auth_page(text) is True

    def test_len_1999_with_signal_returns_true(self):
        """Exactly 1999 chars with signal → True (boundary: < 2000)."""
        from job_finder.web.enrichment_tiers import is_short_auth_page

        signal = "sign in"
        padding = "a" * (1999 - len(signal))
        text = signal + padding
        assert len(text) == 1999
        assert is_short_auth_page(text) is True

    def test_len_2000_with_signal_returns_false(self):
        """Exactly 2000 chars with signal → False (threshold is strict < 2000)."""
        from job_finder.web.enrichment_tiers import is_short_auth_page

        signal = "sign in"
        padding = "a" * (2000 - len(signal))
        text = signal + padding
        assert len(text) == 2000
        assert is_short_auth_page(text) is False

    def test_len_2001_with_signal_returns_false(self):
        """2001 chars with signal → False (long page, not a short auth wall)."""
        from job_finder.web.enrichment_tiers import is_short_auth_page

        text = "sign in " + "x" * 2000
        assert len(text) > 2000
        assert is_short_auth_page(text) is False

    def test_short_page_no_signal_returns_false(self):
        """Short page without any auth signal → False."""
        from job_finder.web.enrichment_tiers import is_short_auth_page

        text = "Senior Data Scientist at Acme Corp. We are looking for a talented DS."
        assert len(text) < 2000
        assert is_short_auth_page(text) is False

    def test_empty_string_returns_false(self):
        """Empty string → False (no text = no auth wall)."""
        from job_finder.web.enrichment_tiers import is_short_auth_page

        assert is_short_auth_page("") is False

    def test_signal_case_insensitive(self):
        """Signal check is case-insensitive on text[:500].lower()."""
        from job_finder.web.enrichment_tiers import is_short_auth_page

        text = "SIGN IN TO CONTINUE" + "x" * 50
        assert is_short_auth_page(text) is True

    def test_signal_beyond_500_chars_ignored(self):
        """Signal beyond the first 500 chars is NOT detected (only text[:500] is checked)."""
        from job_finder.web.enrichment_tiers import is_short_auth_page

        # 500 chars of legitimate text, then auth signal
        clean_prefix = "a" * 500
        text = clean_prefix + "sign in" + "b" * 100
        assert len(text) < 2000
        # Signal is at position 500+, outside the checked window
        assert is_short_auth_page(text) is False

    def test_access_denied_signal(self):
        """'access denied' signal in short page → True."""
        from job_finder.web.enrichment_tiers import is_short_auth_page

        text = "Access denied. You do not have permission."
        assert is_short_auth_page(text) is True

    def test_verify_you_are_human_signal(self):
        """'verify you are a human' signal → True."""
        from job_finder.web.enrichment_tiers import is_short_auth_page

        text = "Please verify you are a human to continue."
        assert is_short_auth_page(text) is True


# ---------------------------------------------------------------------------
# search_serpapi() — 2-tuple return contract
# ---------------------------------------------------------------------------


class TestSearchSerpapiTupleReturn:
    """Verify search_serpapi() always returns a 2-tuple (dict|None, list[str])."""

    def _make_response(self, jobs_results):
        """Helper: MagicMock HTTP response with given jobs_results."""
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"jobs_results": jobs_results}
        return resp

    def test_empty_results_returns_none_empty_list(self):
        """No jobs_results → (None, [])."""
        from job_finder.web.enrichment_tiers import search_serpapi

        resp = self._make_response([])
        with patch("job_finder.web.enrichment_tiers.requests.get", return_value=resp):
            result, urls = search_serpapi("Data Scientist Acme Corp", "key")
        assert result is None
        assert urls == []

    def test_exception_returns_none_empty_list(self):
        """Generic exception → (None, [])."""
        from job_finder.web.enrichment_tiers import search_serpapi

        with patch("job_finder.web.enrichment_tiers.requests.get", side_effect=Exception("net error")):
            result, urls = search_serpapi("query", "key")
        assert result is None
        assert urls == []

    def test_result_is_tuple_with_dict_and_list(self):
        """Successful call returns (dict, list) not just dict."""
        from job_finder.web.enrichment_tiers import search_serpapi

        resp = self._make_response([
            {
                "title": "Data Scientist",
                "company_name": "Acme Corp",
                "description": "Build ML models at scale.",
            }
        ])
        with patch("job_finder.web.enrichment_tiers.requests.get", return_value=resp):
            result = search_serpapi("Data Scientist Acme Corp", "key")

        assert isinstance(result, tuple)
        assert len(result) == 2
        result_dict, apply_urls = result
        assert isinstance(result_dict, dict)
        assert isinstance(apply_urls, list)

    def test_apply_options_blocked_domains_filtered(self):
        """Glassdoor/Indeed apply_options are filtered out by is_blocked_domain()."""
        from job_finder.web.enrichment_tiers import search_serpapi

        resp = self._make_response([
            {
                "title": "Data Scientist",
                "company_name": "Acme Corp",
                "description": "Good job description.",
                "apply_options": [
                    {"link": "https://www.glassdoor.com/job/12345"},
                    {"link": "https://www.indeed.com/viewjob?jk=abc"},
                    {"link": "https://boards.greenhouse.io/acme/jobs/1"},
                ],
            }
        ])
        with patch("job_finder.web.enrichment_tiers.requests.get", return_value=resp), \
             patch("job_finder.web.enrichment_tiers.fetch_direct_jd", return_value=None):
            _, apply_urls = search_serpapi("Data Scientist Acme Corp", "key")

        # Glassdoor and Indeed must be filtered out
        for url in apply_urls:
            assert "glassdoor" not in url
            assert "indeed" not in url
        # Greenhouse must survive
        assert any("greenhouse" in url for url in apply_urls)

    def test_apply_options_sorted_by_domain_priority(self):
        """apply_options sorted greenhouse < lever < builtin (priority ascending)."""
        from job_finder.web.enrichment_tiers import search_serpapi
        from job_finder.web.domain_policy import domain_priority

        greenhouse_url = "https://boards.greenhouse.io/acme/jobs/1"
        builtin_url = "https://builtin.com/job/acme/ds/123"
        lever_url = "https://jobs.lever.co/acme/abc"

        resp = self._make_response([
            {
                "title": "DS",
                "company_name": "Acme",
                "description": "job description",
                "apply_options": [
                    {"link": builtin_url},    # lower priority
                    {"link": greenhouse_url}, # highest priority
                    {"link": lever_url},      # second priority
                ],
            }
        ])
        with patch("job_finder.web.enrichment_tiers.requests.get", return_value=resp), \
             patch("job_finder.web.enrichment_tiers.fetch_direct_jd", return_value=None):
            _, apply_urls = search_serpapi("DS Acme", "key")

        # Verify sorted by domain_priority ascending
        priorities = [domain_priority(u) for u in apply_urls]
        assert priorities == sorted(priorities)
        # Greenhouse must come first
        assert apply_urls[0] == greenhouse_url

    def test_url_jd_key_written_when_ats_fetch_succeeds(self):
        """fetch_direct_jd success on ATS URL → result dict has 'url_jd' key."""
        from job_finder.web.enrichment_tiers import search_serpapi

        fetched_jd = "Full job description from greenhouse.io ATS backend." * 10

        resp = self._make_response([
            {
                "title": "DS",
                "company_name": "Acme",
                # No "description" key — so jd_full absent, triggering ATS fetch
                "apply_options": [
                    {"link": "https://boards.greenhouse.io/acme/jobs/1"},
                ],
            }
        ])
        with patch("job_finder.web.enrichment_tiers.requests.get", return_value=resp), \
             patch("job_finder.web.enrichment_tiers.fetch_direct_jd", return_value=fetched_jd):
            result_dict, apply_urls = search_serpapi("DS Acme", "key")

        assert result_dict is not None
        assert "url_jd" in result_dict
        assert result_dict["url_jd"] == fetched_jd

    def test_url_jd_fetched_even_when_jd_full_already_present(self):
        """DEFECT 014 FIX: ATS fetch is attempted even when Google Jobs description is present.

        ATS canonical pages often carry a longer, more structured JD than the snippet
        returned by Google Jobs. Both jd_full (from description) and url_jd (from ATS)
        are returned in result_dict so _resolve_from_fragments() can choose the better one.
        """
        from job_finder.web.enrichment_tiers import search_serpapi

        ats_jd = "Full ATS job description with structured requirements and responsibilities."
        resp = self._make_response([
            {
                "title": "DS",
                "company_name": "Acme",
                "description": "Short Google Jobs snippet.",
                "apply_options": [
                    {"link": "https://boards.greenhouse.io/acme/jobs/1"},
                ],
            }
        ])
        mock_fetch = MagicMock(return_value=ats_jd)
        with patch("job_finder.web.enrichment_tiers.requests.get", return_value=resp), \
             patch("job_finder.web.enrichment_tiers.fetch_direct_jd", mock_fetch):
            result_dict, _ = search_serpapi("DS Acme", "key")

        # fetch_direct_jd SHOULD be called even though jd_full is present
        mock_fetch.assert_called_once_with("https://boards.greenhouse.io/acme/jobs/1")
        assert result_dict is not None
        assert result_dict.get("jd_full") == "Short Google Jobs snippet."
        assert result_dict.get("url_jd") == ats_jd

    def test_no_apply_options_returns_empty_list(self):
        """Job with no apply_options → apply_urls is []."""
        from job_finder.web.enrichment_tiers import search_serpapi

        resp = self._make_response([
            {
                "title": "DS",
                "company_name": "Acme",
                "description": "Some job description text.",
            }
        ])
        with patch("job_finder.web.enrichment_tiers.requests.get", return_value=resp):
            result_dict, apply_urls = search_serpapi("DS Acme", "key")

        assert apply_urls == []


# ---------------------------------------------------------------------------
# search_ddg_web() — DDG web search upgrade
# ---------------------------------------------------------------------------


class TestSearchDdgWeb:
    """Tests for search_ddg_web() using the ddgs library."""

    def test_returns_urls_and_snippets(self):
        """Successful search returns filtered URLs and concatenated snippets."""
        from job_finder.web.enrichment_tiers import search_ddg_web

        mock_results = [
            {"href": "https://boards.greenhouse.io/acme/jobs/1", "title": "DS at Acme", "body": "Job description body."},
            {"href": "https://example.com/job/2", "title": "Another", "body": "Another body text."},
        ]

        with patch("job_finder.web.enrichment_tiers.DDGS") as MockDDGS, \
             patch("job_finder.web.enrichment_tiers.time.sleep"):
            mock_ddgs_instance = MagicMock()
            mock_ddgs_instance.__enter__ = MagicMock(return_value=mock_ddgs_instance)
            mock_ddgs_instance.__exit__ = MagicMock(return_value=False)
            mock_ddgs_instance.text.return_value = mock_results
            MockDDGS.return_value = mock_ddgs_instance

            result = search_ddg_web("Data Scientist", "Acme Corp")

        assert "ddg_urls" in result
        assert "ddg_snippet" in result
        assert len(result["ddg_urls"]) > 0
        assert "Job description body." in result["ddg_snippet"]

    def test_blocked_domains_filtered(self):
        """Blocked domains (glassdoor, indeed) are removed from results."""
        from job_finder.web.enrichment_tiers import search_ddg_web

        mock_results = [
            {"href": "https://www.glassdoor.com/job/123", "title": "t", "body": "b"},
            {"href": "https://www.indeed.com/viewjob?jk=abc", "title": "t", "body": "b"},
            {"href": "https://boards.greenhouse.io/acme/jobs/1", "title": "t", "body": "b"},
        ]

        with patch("job_finder.web.enrichment_tiers.DDGS") as MockDDGS, \
             patch("job_finder.web.enrichment_tiers.time.sleep"):
            mock_ddgs_instance = MagicMock()
            mock_ddgs_instance.__enter__ = MagicMock(return_value=mock_ddgs_instance)
            mock_ddgs_instance.__exit__ = MagicMock(return_value=False)
            mock_ddgs_instance.text.return_value = mock_results
            MockDDGS.return_value = mock_ddgs_instance

            result = search_ddg_web("Data Scientist", "Acme Corp")

        for url in result["ddg_urls"]:
            assert "glassdoor" not in url
            assert "indeed" not in url
        assert any("greenhouse" in url for url in result["ddg_urls"])

    def test_urls_sorted_by_priority(self):
        """ATS platforms (greenhouse, lever) sorted before generic sites."""
        from job_finder.web.enrichment_tiers import search_ddg_web
        from job_finder.web.domain_policy import domain_priority

        mock_results = [
            {"href": "https://example.com/job/1", "title": "t", "body": "b"},
            {"href": "https://boards.greenhouse.io/acme/jobs/1", "title": "t", "body": "b"},
            {"href": "https://jobs.lever.co/acme/abc", "title": "t", "body": "b"},
        ]

        with patch("job_finder.web.enrichment_tiers.DDGS") as MockDDGS, \
             patch("job_finder.web.enrichment_tiers.time.sleep"):
            mock_ddgs_instance = MagicMock()
            mock_ddgs_instance.__enter__ = MagicMock(return_value=mock_ddgs_instance)
            mock_ddgs_instance.__exit__ = MagicMock(return_value=False)
            mock_ddgs_instance.text.return_value = mock_results
            MockDDGS.return_value = mock_ddgs_instance

            result = search_ddg_web("Data Scientist", "Acme Corp")

        priorities = [domain_priority(u) for u in result["ddg_urls"]]
        assert priorities == sorted(priorities)

    def test_empty_results_returns_empty(self):
        """No search results → empty URLs and empty snippet."""
        from job_finder.web.enrichment_tiers import search_ddg_web

        with patch("job_finder.web.enrichment_tiers.DDGS") as MockDDGS, \
             patch("job_finder.web.enrichment_tiers.time.sleep"):
            mock_ddgs_instance = MagicMock()
            mock_ddgs_instance.__enter__ = MagicMock(return_value=mock_ddgs_instance)
            mock_ddgs_instance.__exit__ = MagicMock(return_value=False)
            mock_ddgs_instance.text.return_value = []
            MockDDGS.return_value = mock_ddgs_instance

            result = search_ddg_web("Data Scientist", "Acme Corp")

        assert result["ddg_urls"] == []
        assert result["ddg_snippet"] == ""

    def test_exception_returns_partial_results(self):
        """Exception on one query doesn't crash — returns results from other query."""
        from job_finder.web.enrichment_tiers import search_ddg_web

        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("rate limited")
            return [{"href": "https://example.com/job/1", "title": "t", "body": "b"}]

        with patch("job_finder.web.enrichment_tiers.DDGS") as MockDDGS, \
             patch("job_finder.web.enrichment_tiers.time.sleep"):
            mock_ddgs_instance = MagicMock()
            mock_ddgs_instance.__enter__ = MagicMock(return_value=mock_ddgs_instance)
            mock_ddgs_instance.__exit__ = MagicMock(return_value=False)
            mock_ddgs_instance.text.side_effect = side_effect
            MockDDGS.return_value = mock_ddgs_instance

            result = search_ddg_web("Data Scientist", "Acme Corp")

        assert len(result["ddg_urls"]) == 1

    def test_max_8_urls(self):
        """Results are capped at 8 URLs."""
        from job_finder.web.enrichment_tiers import search_ddg_web

        mock_results = [
            {"href": f"https://example{i}.com/job", "title": "t", "body": "b"}
            for i in range(12)
        ]

        with patch("job_finder.web.enrichment_tiers.DDGS") as MockDDGS, \
             patch("job_finder.web.enrichment_tiers.time.sleep"):
            mock_ddgs_instance = MagicMock()
            mock_ddgs_instance.__enter__ = MagicMock(return_value=mock_ddgs_instance)
            mock_ddgs_instance.__exit__ = MagicMock(return_value=False)
            mock_ddgs_instance.text.return_value = mock_results
            MockDDGS.return_value = mock_ddgs_instance

            result = search_ddg_web("Data Scientist", "Acme Corp")

        assert len(result["ddg_urls"]) <= 8


# ---------------------------------------------------------------------------
# fetch_ddg_jds() — URL fetch from DDG results
# ---------------------------------------------------------------------------


class TestFetchDdgJds:
    """Tests for fetch_ddg_jds() URL fetching logic."""

    # Mock JD text must contain a JD content marker (e.g., "responsibilities")
    # to pass the has_jd_content() check added for quality validation.
    _MOCK_JD = "About the role: Key responsibilities include data analysis. Qualifications: 3+ years experience. " * 5

    def test_linkedin_url_routes_to_linkedin_extractor(self):
        """LinkedIn URLs use fetch_linkedin_jd(), not fetch_direct_jd()."""
        from job_finder.web.enrichment_tiers import fetch_ddg_jds

        long_jd = self._MOCK_JD

        with patch("job_finder.web.enrichment_tiers.fetch_linkedin_jd") as mock_li, \
             patch("job_finder.web.enrichment_tiers.fetch_direct_jd") as mock_direct:
            mock_li.return_value = long_jd

            jd_text, source_url = fetch_ddg_jds(["https://www.linkedin.com/jobs/view/123456/"])

        mock_li.assert_called_once_with("https://www.linkedin.com/jobs/view/123456/")
        mock_direct.assert_not_called()
        assert jd_text == long_jd
        assert source_url == "https://www.linkedin.com/jobs/view/123456/"

    def test_non_linkedin_url_routes_to_direct_fetch(self):
        """Non-LinkedIn URLs use fetch_direct_jd()."""
        from job_finder.web.enrichment_tiers import fetch_ddg_jds

        long_jd = self._MOCK_JD

        with patch("job_finder.web.enrichment_tiers.fetch_linkedin_jd") as mock_li, \
             patch("job_finder.web.enrichment_tiers.fetch_direct_jd") as mock_direct:
            mock_direct.return_value = long_jd

            jd_text, source_url = fetch_ddg_jds(["https://boards.greenhouse.io/acme/jobs/1"])

        mock_direct.assert_called_once()
        mock_li.assert_not_called()
        assert jd_text == long_jd

    def test_short_jd_rejected(self):
        """JDs under 200 chars are rejected."""
        from job_finder.web.enrichment_tiers import fetch_ddg_jds

        with patch("job_finder.web.enrichment_tiers.fetch_direct_jd") as mock_direct:
            mock_direct.return_value = "Short text"  # < 200 chars

            jd_text, source_url = fetch_ddg_jds(["https://example.com/job/1"])

        assert jd_text is None
        assert source_url is None

    def test_empty_urls_returns_none(self):
        """Empty URL list returns (None, None)."""
        from job_finder.web.enrichment_tiers import fetch_ddg_jds

        jd_text, source_url = fetch_ddg_jds([])
        assert jd_text is None
        assert source_url is None

    def test_max_4_attempts(self):
        """Only tries up to 4 URLs."""
        from job_finder.web.enrichment_tiers import fetch_ddg_jds

        urls = [f"https://example{i}.com/job" for i in range(8)]

        with patch("job_finder.web.enrichment_tiers.fetch_direct_jd") as mock_direct:
            mock_direct.return_value = None  # All fail

            fetch_ddg_jds(urls)

        assert mock_direct.call_count == 4

    def test_returns_first_successful(self):
        """Returns first URL that yields a valid JD."""
        from job_finder.web.enrichment_tiers import fetch_ddg_jds

        long_jd = self._MOCK_JD

        with patch("job_finder.web.enrichment_tiers.fetch_direct_jd") as mock_direct:
            mock_direct.side_effect = [None, long_jd]

            jd_text, source_url = fetch_ddg_jds([
                "https://example1.com/job",
                "https://example2.com/job",
            ])

        assert jd_text == long_jd
        assert source_url == "https://example2.com/job"

    def test_blocked_domain_skipped(self):
        """Blocked domains are skipped in fetch loop."""
        from job_finder.web.enrichment_tiers import fetch_ddg_jds

        with patch("job_finder.web.enrichment_tiers.fetch_direct_jd") as mock_direct, \
             patch("job_finder.web.enrichment_tiers.fetch_linkedin_jd") as mock_li:
            # Should not be called because glassdoor is blocked
            mock_direct.return_value = "A" * 300

            jd_text, source_url = fetch_ddg_jds(["https://www.glassdoor.com/job/123"])

        mock_direct.assert_not_called()
        mock_li.assert_not_called()
        assert jd_text is None


# ---------------------------------------------------------------------------
# Cascade dispatch tests — extract_with_haiku + extract_with_sonnet
# ---------------------------------------------------------------------------


class TestExtractWithHaikuCascade:
    """Dispatch pattern tests for extract_with_haiku."""

    _JOB_ROW = {
        "dedup_key": "acme|data-scientist|remote",
        "title": "Data Scientist",
        "company": "Acme",
    }
    _HAIKU_PAYLOAD = {
        "jd_full": "Full job description text from the model.",
        "salary_min": 120000,
        "salary_max": 180000,
        "location": "Remote",
    }

    def test_uses_call_model_when_providers_configured(
        self, migrated_db, cascade_config_haiku, make_model_result,
    ):
        from job_finder.web.enrichment_tiers import extract_with_haiku
        _path, conn = migrated_db

        with patch("job_finder.web.enrichment_tiers.call_model") as mock_cm, \
             patch("job_finder.web.enrichment_tiers.call_claude") as mock_cc:
            mock_cm.return_value = make_model_result(self._HAIKU_PAYLOAD)
            result = extract_with_haiku(
                "ddg snippet text", self._JOB_ROW, conn, cascade_config_haiku,
            )

        mock_cm.assert_called_once()
        assert mock_cm.call_args.kwargs["tier"] == "haiku"
        assert mock_cm.call_args.kwargs["purpose"] == "enrich_job"
        mock_cc.assert_not_called()
        assert result["jd_full"].startswith("Full job description")
        assert result["salary_min"] == 120000

    def test_uses_call_claude_when_no_providers(self, migrated_db):
        from job_finder.web.enrichment_tiers import extract_with_haiku
        _path, conn = migrated_db

        with patch("job_finder.web.enrichment_tiers.call_model") as mock_cm, \
             patch("job_finder.web.enrichment_tiers.call_claude") as mock_cc:
            mock_cc.return_value = (self._HAIKU_PAYLOAD, 0.001)
            result = extract_with_haiku(
                "ddg snippet", self._JOB_ROW, conn, config={},
            )

        mock_cm.assert_not_called()
        mock_cc.assert_called_once()
        assert result["jd_full"].startswith("Full job description")

    def test_cascade_exhausted_falls_back_to_cli(
        self, migrated_db, cascade_config_haiku,
    ):
        from job_finder.web.enrichment_tiers import extract_with_haiku
        from job_finder.web.model_provider import ProviderCascadeExhaustedError
        _path, conn = migrated_db

        with patch("job_finder.web.enrichment_tiers.call_model") as mock_cm, \
             patch("job_finder.web.enrichment_tiers.call_claude") as mock_cc:
            mock_cm.side_effect = ProviderCascadeExhaustedError("exhausted")
            mock_cc.return_value = (self._HAIKU_PAYLOAD, 0.001)
            result = extract_with_haiku(
                "ddg snippet", self._JOB_ROW, conn, cascade_config_haiku,
            )

        mock_cm.assert_called_once()
        mock_cc.assert_called_once()
        assert result["jd_full"].startswith("Full job description")

    def test_cascade_and_cli_both_fail_returns_empty_dict(
        self, migrated_db, cascade_config_haiku,
    ):
        from job_finder.web.enrichment_tiers import extract_with_haiku
        from job_finder.web.model_provider import ProviderCascadeExhaustedError
        _path, conn = migrated_db

        with patch("job_finder.web.enrichment_tiers.call_model") as mock_cm, \
             patch("job_finder.web.enrichment_tiers.call_claude") as mock_cc:
            mock_cm.side_effect = ProviderCascadeExhaustedError("exhausted")
            mock_cc.side_effect = RuntimeError("CLI unavailable")
            result = extract_with_haiku(
                "ddg snippet", self._JOB_ROW, conn, cascade_config_haiku,
            )

        assert result == {}


class TestExtractWithSonnetCascade:
    """Dispatch pattern tests for extract_with_sonnet."""

    _JOB_ROW = {
        "dedup_key": "acme|data-scientist|remote",
        "title": "Data Scientist",
        "company": "Acme",
    }
    _SONNET_PAYLOAD = {
        "jd_full": "Aggregated full JD.",
        "salary_min": 150000,
        "salary_max": 200000,
    }
    _FRAGMENTS = {
        "ddg_snippet": "Snippet about the role.",
        "ats_text": "ATS API text.",
    }

    def test_uses_call_model_when_providers_configured(
        self, migrated_db, cascade_config_sonnet, make_model_result,
    ):
        from job_finder.web.enrichment_tiers import extract_with_sonnet
        _path, conn = migrated_db

        with patch("job_finder.web.enrichment_tiers.call_model") as mock_cm, \
             patch("job_finder.web.enrichment_tiers.call_claude") as mock_cc:
            mock_cm.return_value = make_model_result(self._SONNET_PAYLOAD)
            result = extract_with_sonnet(
                self._FRAGMENTS, self._JOB_ROW, conn, cascade_config_sonnet,
            )

        mock_cm.assert_called_once()
        assert mock_cm.call_args.kwargs["tier"] == "sonnet"
        assert mock_cm.call_args.kwargs["purpose"] == "enrich_job_sonnet"
        mock_cc.assert_not_called()
        assert result["jd_full"] == "Aggregated full JD."
        assert result["salary_min"] == 150000

    def test_uses_call_claude_when_no_providers(self, migrated_db):
        from job_finder.web.enrichment_tiers import extract_with_sonnet
        _path, conn = migrated_db

        with patch("job_finder.web.enrichment_tiers.call_model") as mock_cm, \
             patch("job_finder.web.enrichment_tiers.call_claude") as mock_cc:
            mock_cc.return_value = (self._SONNET_PAYLOAD, 0.004)
            result = extract_with_sonnet(
                self._FRAGMENTS, self._JOB_ROW, conn, config={},
            )

        mock_cm.assert_not_called()
        mock_cc.assert_called_once()
        assert result["jd_full"] == "Aggregated full JD."

    def test_cascade_exhausted_falls_back_to_cli(
        self, migrated_db, cascade_config_sonnet,
    ):
        from job_finder.web.enrichment_tiers import extract_with_sonnet
        from job_finder.web.model_provider import ProviderCascadeExhaustedError
        _path, conn = migrated_db

        with patch("job_finder.web.enrichment_tiers.call_model") as mock_cm, \
             patch("job_finder.web.enrichment_tiers.call_claude") as mock_cc:
            mock_cm.side_effect = ProviderCascadeExhaustedError("exhausted")
            mock_cc.return_value = (self._SONNET_PAYLOAD, 0.004)
            result = extract_with_sonnet(
                self._FRAGMENTS, self._JOB_ROW, conn, cascade_config_sonnet,
            )

        mock_cm.assert_called_once()
        mock_cc.assert_called_once()
        assert result["jd_full"] == "Aggregated full JD."

    def test_cascade_and_cli_both_fail_returns_empty_dict(
        self, migrated_db, cascade_config_sonnet,
    ):
        from job_finder.web.enrichment_tiers import extract_with_sonnet
        from job_finder.web.model_provider import ProviderCascadeExhaustedError
        _path, conn = migrated_db

        with patch("job_finder.web.enrichment_tiers.call_model") as mock_cm, \
             patch("job_finder.web.enrichment_tiers.call_claude") as mock_cc:
            mock_cm.side_effect = ProviderCascadeExhaustedError("exhausted")
            mock_cc.side_effect = RuntimeError("CLI unavailable")
            result = extract_with_sonnet(
                self._FRAGMENTS, self._JOB_ROW, conn, cascade_config_sonnet,
            )

        assert result == {}
