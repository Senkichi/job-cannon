"""Tests for Workday ATS scanner: URL detection, probing, and scanning."""

from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Tests: Workday URL detection in ats_detection.py
# ---------------------------------------------------------------------------


class TestWorkdayUrlDetection:
    """Tests for Workday URL pattern recognition in extract_ats_from_urls."""

    def test_workday_human_url_returns_workday_and_slug(self):
        """Human-facing myworkdayjobs.com URL returns ('workday', 'subdomain/board')."""
        from job_finder.web.ats_detection import extract_ats_from_urls

        urls = ["https://walmart.wd5.myworkdayjobs.com/WalmartExternal"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "workday"
        assert slug == "walmart.wd5/WalmartExternal"

    def test_workday_human_url_with_en_us_prefix(self):
        """Human URL with en-US locale prefix still extracts correctly."""
        from job_finder.web.ats_detection import extract_ats_from_urls

        urls = ["https://walmart.wd5.myworkdayjobs.com/en-US/WalmartExternal/job/some-path"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "workday"
        assert slug == "walmart.wd5/WalmartExternal"

    def test_workday_api_url_returns_workday_and_slug(self):
        """API URL returns ('workday', 'subdomain/board')."""
        from job_finder.web.ats_detection import extract_ats_from_urls

        urls = ["https://walmart.wd5.myworkdayjobs.com/wday/cxs/walmart/WalmartExternal/jobs"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "workday"
        assert slug == "walmart.wd5/WalmartExternal"

    def test_workday_case_insensitive(self):
        """Workday URL detection is case-insensitive."""
        from job_finder.web.ats_detection import extract_ats_from_urls

        urls = ["https://WALMART.WD5.MYWORKDAYJOBS.COM/WalmartExternal"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "workday"

    def test_workday_url_does_not_match_non_workday(self):
        """Non-Workday URLs are not matched."""
        from job_finder.web.ats_detection import extract_ats_from_urls

        urls = ["https://www.walmart.com/careers"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform is None


# ---------------------------------------------------------------------------
# Tests: _probe_workday
# ---------------------------------------------------------------------------


class TestProbeWorkday:
    """Tests for the Workday probe function."""

    @patch("job_finder.web.ats_prober.requests.post")
    def test_probe_returns_true_on_200(self, mock_post):
        """_probe_workday returns True when API returns 200."""
        from job_finder.web.ats_prober import _probe_workday

        mock_post.return_value = MagicMock(status_code=200)
        assert _probe_workday("walmart.wd5/WalmartExternal") is True

    @patch("job_finder.web.ats_prober.requests.post")
    def test_probe_returns_false_on_404(self, mock_post):
        """_probe_workday returns False when API returns 404."""
        from job_finder.web.ats_prober import _probe_workday

        mock_post.return_value = MagicMock(status_code=404)
        assert _probe_workday("invalid/board") is False

    @patch("job_finder.web.ats_prober.requests.post")
    def test_probe_returns_false_on_exception(self, mock_post):
        """_probe_workday returns False on connection error."""
        from job_finder.web.ats_prober import _probe_workday

        mock_post.side_effect = Exception("connection refused")
        assert _probe_workday("walmart.wd5/WalmartExternal") is False

    def test_probe_returns_false_on_invalid_slug(self):
        """_probe_workday returns False for slug without '/'."""
        from job_finder.web.ats_prober import _probe_workday

        assert _probe_workday("no-slash") is False

    @patch("job_finder.web.ats_prober.requests.post")
    def test_probe_sends_post_request_with_correct_url(self, mock_post):
        """_probe_workday constructs correct API URL from slug."""
        from job_finder.web.ats_prober import _probe_workday

        mock_post.return_value = MagicMock(status_code=200)
        _probe_workday("walmart.wd5/WalmartExternal")
        args, kwargs = mock_post.call_args
        assert (
            args[0]
            == "https://walmart.wd5.myworkdayjobs.com/wday/cxs/walmart/WalmartExternal/jobs"
        )


# ---------------------------------------------------------------------------
# Tests: scan_workday
# ---------------------------------------------------------------------------


@patch("job_finder.web.ats_platforms._fetch_workday_description", return_value="")
class TestScanWorkday:
    """Tests for the Workday job scanner.

    The class-level patch disables the per-job detail fetch so these tests
    stay hermetic and focused on list-endpoint behavior. A separate test
    class (TestFetchWorkdayDescription) exercises the detail fetch itself.
    """

    @pytest.fixture(autouse=True)
    def _no_scan_sleeps(self):
        """Zero scan_workday's per-page and per-posting pacing sleeps.

        _fetch_postings sleeps _PAGE_FETCH_SLEEP_S between pages and
        _DETAIL_FETCH_SLEEP_S per matched posting; test_scan_paginates_correctly
        (2 pages, 25 postings) paid ~2.6s. Patch the constants to 0 (not
        time.sleep — avoids the shared-time-module trap). No test asserts pacing.
        """
        with (
            patch("job_finder.web.ats_platforms._platforms_workday._PAGE_FETCH_SLEEP_S", 0),
            patch("job_finder.web.ats_platforms._platforms_workday._DETAIL_FETCH_SLEEP_S", 0),
        ):
            yield

    @patch("job_finder.web.ats_platforms.requests.post")
    def test_scan_returns_matched_jobs(self, mock_post, _mock_detail):
        """scan_workday returns jobs matching target titles."""
        from job_finder.web.ats_platforms import scan_workday

        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {
            "total": 2,
            "jobPostings": [
                {
                    "title": "Senior Data Scientist",
                    "locationsText": "Sunnyvale, CA",
                    "externalPath": "Senior-Data-Scientist_R-12345",
                },
                {
                    "title": "Retail Associate",
                    "locationsText": "Dallas, TX",
                    "externalPath": "Retail-Associate_R-99999",
                },
            ],
        }
        mock_post.return_value = mock_response

        results = scan_workday(
            "walmart.wd5/WalmartExternal",
            target_titles=["data scientist"],
            exclusions=[],
        )
        assert len(results) == 1
        assert results[0]["title"] == "Senior Data Scientist"
        assert results[0]["company_source"] == "Workday"
        assert results[0]["location"] == "Sunnyvale, CA"
        assert "walmart.wd5.myworkdayjobs.com" in results[0]["source_url"]

    @patch("job_finder.web.ats_platforms.requests.post")
    def test_scan_applies_exclusions(self, mock_post, _mock_detail):
        """scan_workday filters out jobs matching exclusion keywords."""
        from job_finder.web.ats_platforms import scan_workday

        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {
            "total": 1,
            "jobPostings": [
                {
                    "title": "Junior Data Scientist",
                    "locationsText": "Remote",
                    "externalPath": "Junior-DS_R-001",
                },
            ],
        }
        mock_post.return_value = mock_response

        results = scan_workday(
            "walmart.wd5/WalmartExternal",
            target_titles=["data scientist"],
            exclusions=["junior"],
        )
        assert len(results) == 0

    @patch("job_finder.web.ats_platforms.requests.post")
    def test_scan_handles_empty_response(self, mock_post, _mock_detail):
        """scan_workday returns empty list when API returns no postings."""
        from job_finder.web.ats_platforms import scan_workday

        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {"total": 0, "jobPostings": []}
        mock_post.return_value = mock_response

        results = scan_workday(
            "walmart.wd5/WalmartExternal",
            target_titles=["data scientist"],
            exclusions=[],
        )
        assert results == []

    @patch("job_finder.web.ats_platforms.requests.post")
    def test_scan_handles_http_error(self, mock_post, _mock_detail):
        """scan_workday returns empty list on a transient (non-gone) non-200.

        404/410 now bifurcate to BoardGoneError (see the gone test below); a 503
        is a transient block that must still degrade to an empty list, never
        demote a real board."""
        from job_finder.web.ats_platforms import scan_workday

        mock_post.return_value = MagicMock(status_code=503)
        results = scan_workday(
            "acme.wd5/Blocked",
            target_titles=["data scientist"],
            exclusions=[],
        )
        assert results == []

    @patch("job_finder.web.ats_platforms.requests.post")
    def test_scan_raises_board_gone_on_first_page_404(self, mock_post, _mock_detail):
        """A first-page 404/410 propagates BoardGoneError through the public
        scan_workday entry (run_platform_scan calls fetch_postings OUTSIDE its
        try), so scan callers can catch it and demote the stale hit."""
        from job_finder.web.ats_platforms import scan_workday
        from job_finder.web.ats_platforms._registry import BoardGoneError

        mock_post.return_value = MagicMock(status_code=404)
        with pytest.raises(BoardGoneError):
            scan_workday("invalid/board", target_titles=["data scientist"], exclusions=[])

    def test_scan_rejects_invalid_slug_format(self, _mock_detail):
        """scan_workday returns empty list for slug without '/'."""
        from job_finder.web.ats_platforms import scan_workday

        results = scan_workday("no-slash", ["data scientist"], [])
        assert results == []

    @patch("job_finder.web.ats_platforms.requests.post")
    def test_scan_paginates_correctly(self, mock_post, _mock_detail):
        """scan_workday fetches multiple pages when total > page_size."""
        from job_finder.web.ats_platforms import scan_workday

        page1_response = MagicMock(status_code=200)
        page1_response.json.return_value = {
            "total": 25,
            "jobPostings": [
                {"title": f"Data Scientist {i}", "locationsText": "", "externalPath": f"DS-{i}"}
                for i in range(20)
            ],
        }
        page2_response = MagicMock(status_code=200)
        page2_response.json.return_value = {
            "total": 25,
            "jobPostings": [
                {"title": f"Data Scientist {i}", "locationsText": "", "externalPath": f"DS-{i}"}
                for i in range(20, 25)
            ],
        }
        mock_post.side_effect = [page1_response, page2_response]

        results = scan_workday(
            "walmart.wd5/WalmartExternal",
            target_titles=["data scientist"],
            exclusions=[],
        )
        assert len(results) == 25
        assert mock_post.call_count == 2

    @patch("job_finder.web.ats_platforms.requests.post")
    def test_scan_request_exception_returns_empty(self, mock_post, _mock_detail):
        """scan_workday returns empty list on request exception."""
        from job_finder.web.ats_platforms import scan_workday

        mock_post.side_effect = Exception("network error")
        results = scan_workday(
            "walmart.wd5/WalmartExternal",
            target_titles=["data scientist"],
            exclusions=[],
        )
        assert results == []

    @patch("job_finder.web.ats_platforms.requests.post")
    def test_scan_source_url_format(self, mock_post, _mock_detail):
        """scan_workday builds correct source_url from externalPath."""
        from job_finder.web.ats_platforms import scan_workday

        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {
            "total": 1,
            "jobPostings": [
                {
                    "title": "Data Scientist",
                    "locationsText": "Remote",
                    "externalPath": "/job/Data-Scientist_R-12345",
                }
            ],
        }
        mock_post.return_value = mock_response

        results = scan_workday(
            "walmart.wd5/WalmartExternal",
            target_titles=["data scientist"],
            exclusions=[],
        )
        assert results[0]["source_url"] == (
            "https://walmart.wd5.myworkdayjobs.com/en-US/WalmartExternal/job/Data-Scientist_R-12345"
        )


# ---------------------------------------------------------------------------
# Tests: _fetch_postings_with_completeness
# ---------------------------------------------------------------------------


@patch("job_finder.web.ats_platforms._fetch_workday_description", return_value="")
class TestFetchPostingsWithCompleteness:
    """Tests for the completeness signal returned by _fetch_postings_with_completeness.

    Completeness rules:
      - complete=True  when total_fetched >= total (including genuine empty board).
      - complete=False when total exceeds what the page budget can fetch — but
        the partial postings still come back non-empty (issue #216).
      - complete=False when a network/HTTP error prevents any page from arriving.
      - complete=False when pagination stops before total_fetched >= total.
    """

    @pytest.fixture(autouse=True)
    def _no_sleeps(self):
        with (
            patch("job_finder.web.ats_platforms._platforms_workday._PAGE_FETCH_SLEEP_S", 0),
            patch("job_finder.web.ats_platforms._platforms_workday._DETAIL_FETCH_SLEEP_S", 0),
        ):
            yield

    @patch("job_finder.web.ats_platforms._platforms_workday.requests.post")
    def test_fully_fetched_board_is_complete(self, mock_post, _mock_detail):
        """total=3, one page of 3 postings → complete=True."""
        from job_finder.web.ats_platforms._platforms_workday import (
            _fetch_postings_with_completeness,
        )

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "total": 3,
            "jobPostings": [
                {"title": f"Job {i}", "externalPath": f"/job/Job-{i}_R-{i}"} for i in range(3)
            ],
        }
        mock_post.return_value = mock_resp

        postings, complete = _fetch_postings_with_completeness("acme.wd5/AcmeExternal")
        assert complete is True
        assert len(postings) == 3

    @patch("job_finder.web.ats_platforms._platforms_workday.requests.post")
    def test_board_over_budget_is_incomplete_but_non_empty(self, mock_post, _mock_detail):
        """Board larger than the page budget → complete=False AND non-empty (issue #216).

        Regression guard: pre-fix, a board with total > the cap returned an
        EMPTY postings list (discovery silently zeroed). Now discovery gets
        the first ``max_pages`` pages, and only completeness is False.
        """
        from job_finder.web.ats_platforms._platforms_workday import (
            _PAGE_SIZE,
            _fetch_postings_with_completeness,
        )

        # total=500 (25 pages of 20) but budget capped at 3 pages → 60 fetched.
        def _page(_url, **_kwargs):
            offset = _kwargs["json"]["offset"]
            resp = MagicMock(status_code=200)
            resp.json.return_value = {
                "total": 500,
                "jobPostings": [
                    {"title": f"Job {i}", "externalPath": f"/job/Job-{i}_R-{i}"}
                    for i in range(offset, offset + _PAGE_SIZE)
                ],
            }
            return resp

        mock_post.side_effect = _page

        postings, complete = _fetch_postings_with_completeness(
            "acme.wd5/AcmeExternal", max_pages=3
        )
        assert complete is False
        # Discovery is NOT zeroed: first 3 pages (60 postings) came back.
        assert len(postings) == 3 * _PAGE_SIZE
        assert mock_post.call_count == 3

    @patch("job_finder.web.ats_platforms._platforms_workday.requests.post")
    def test_large_board_within_budget_is_complete(self, mock_post, _mock_detail):
        """A >200-posting board fully paginates when within the page budget (issue #216)."""
        from job_finder.web.ats_platforms._platforms_workday import (
            _PAGE_SIZE,
            _fetch_postings_with_completeness,
        )

        total = 250  # 13 pages of 20 (last page is partial) — exceeds the old 200 cap.

        def _page(_url, **_kwargs):
            offset = _kwargs["json"]["offset"]
            end = min(offset + _PAGE_SIZE, total)
            resp = MagicMock(status_code=200)
            resp.json.return_value = {
                "total": total,
                "jobPostings": [
                    {"title": f"Job {i}", "externalPath": f"/job/Job-{i}_R-{i}"}
                    for i in range(offset, end)
                ],
            }
            return resp

        mock_post.side_effect = _page

        postings, complete = _fetch_postings_with_completeness(
            "acme.wd5/AcmeExternal", max_pages=100
        )
        assert complete is True
        assert len(postings) == total

    @patch("job_finder.web.ats_platforms._platforms_workday.requests.post")
    def test_total_reported_only_on_first_page_is_not_truncated(self, mock_post, _mock_detail):
        """Real Workday CXS: ``total`` is populated ONLY on the offset=0 page;
        every subsequent page returns ``total=0`` (with 20 valid postings).

        Regression for the silent 40-job cap: the loop re-read ``total`` each
        page, so page 2 overwrote it with 0 and the ``total_fetched >= total``
        break fired at ``40 >= 0`` — truncating EVERY board to 2 pages
        regardless of size (Nvidia 2000 / Salesforce 1461 / Adobe 1091 all
        cut to 40). All other completeness tests reported the real total on
        every page, so none caught this. The fix captures ``total`` once.
        """
        from job_finder.web.ats_platforms._platforms_workday import (
            _PAGE_SIZE,
            _fetch_postings_with_completeness,
        )

        total = 130  # 7 pages (last partial) — well past the old 2-page/40 cap.

        def _page(_url, **_kwargs):
            offset = _kwargs["json"]["offset"]
            end = min(offset + _PAGE_SIZE, total)
            resp = MagicMock(status_code=200)
            resp.json.return_value = {
                # Real total on the first page, 0 on every later page.
                "total": total if offset == 0 else 0,
                "jobPostings": [
                    {"title": f"Job {i}", "externalPath": f"/job/Job-{i}_R-{i}"}
                    for i in range(offset, end)
                ],
            }
            return resp

        mock_post.side_effect = _page

        postings, complete = _fetch_postings_with_completeness(
            "acme.wd5/AcmeExternal", max_pages=100
        )
        # The whole board comes back — NOT truncated to 40 (the old cap).
        assert len(postings) == total
        assert complete is True

    @patch("job_finder.web.ats_platforms._platforms_workday.requests.post")
    def test_max_pages_contextvar_override_applied(self, mock_post, _mock_detail):
        """set_max_pages ContextVar caps pagination when no explicit arg is passed.

        Mirrors how run_ats_scan / reconcile_all_companies thread
        config.ats.workday_max_pages down to the registry's slug->list scanner.
        """
        from job_finder.web.ats_platforms._platforms_workday import (
            _PAGE_SIZE,
            _fetch_postings_with_completeness,
            reset_max_pages,
            set_max_pages,
        )

        def _page(_url, **_kwargs):
            offset = _kwargs["json"]["offset"]
            resp = MagicMock(status_code=200)
            resp.json.return_value = {
                "total": 500,
                "jobPostings": [
                    {"title": f"Job {i}", "externalPath": f"/job/Job-{i}_R-{i}"}
                    for i in range(offset, offset + _PAGE_SIZE)
                ],
            }
            return resp

        mock_post.side_effect = _page

        token = set_max_pages(2)
        try:
            postings, complete = _fetch_postings_with_completeness("acme.wd5/AcmeExternal")
        finally:
            reset_max_pages(token)

        assert complete is False
        assert len(postings) == 2 * _PAGE_SIZE
        assert mock_post.call_count == 2

    @patch("job_finder.web.ats_platforms._platforms_workday.requests.post")
    def test_empty_board_is_complete(self, mock_post, _mock_detail):
        """total=0, no postings → complete=True (genuine empty board)."""
        from job_finder.web.ats_platforms._platforms_workday import (
            _fetch_postings_with_completeness,
        )

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"total": 0, "jobPostings": []}
        mock_post.return_value = mock_resp

        postings, complete = _fetch_postings_with_completeness("acme.wd5/AcmeExternal")
        assert complete is True
        assert postings == []

    @patch("job_finder.web.ats_platforms._platforms_workday.requests.post")
    def test_first_page_error_is_incomplete(self, mock_post, _mock_detail):
        """Network exception on first page → complete=False."""
        from job_finder.web.ats_platforms._platforms_workday import (
            _fetch_postings_with_completeness,
        )

        mock_post.side_effect = Exception("connection refused")

        postings, complete = _fetch_postings_with_completeness("acme.wd5/AcmeExternal")
        assert complete is False
        assert postings == []

    @patch("job_finder.web.ats_platforms._platforms_workday.requests.post")
    def test_early_pagination_stop_is_incomplete(self, mock_post, _mock_detail):
        """Server error on page 2 before total_fetched >= total → complete=False."""
        from job_finder.web.ats_platforms._platforms_workday import (
            _fetch_postings_with_completeness,
        )

        page1_resp = MagicMock(status_code=200)
        page1_resp.json.return_value = {
            "total": 25,
            "jobPostings": [
                {"title": f"Job {i}", "externalPath": f"/job/Job-{i}_R-{i}"} for i in range(20)
            ],
        }
        page2_resp = MagicMock(status_code=500)
        mock_post.side_effect = [page1_resp, page2_resp]

        postings, complete = _fetch_postings_with_completeness("acme.wd5/AcmeExternal")
        assert complete is False
        assert len(postings) == 20  # page 1 landed; page 2 failed

    def test_invalid_slug_is_incomplete(self, _mock_detail):
        """Slug without '/' → complete=False, empty list."""
        from job_finder.web.ats_platforms._platforms_workday import (
            _fetch_postings_with_completeness,
        )

        postings, complete = _fetch_postings_with_completeness("no-slash")
        assert complete is False
        assert postings == []

    @patch("job_finder.web.ats_platforms._platforms_workday.requests.post")
    def test_first_page_410_raises_board_gone(self, mock_post, _mock_detail):
        """First-page HTTP 410 → BoardGoneError (the tenant/slug no longer
        resolves, e.g. Walmart). The scan path catches this to demote the stale
        hit rather than logging '0 fetched' against a dead board forever."""
        from job_finder.web.ats_platforms._platforms_workday import (
            _fetch_postings_with_completeness,
        )
        from job_finder.web.ats_platforms._registry import BoardGoneError

        mock_post.return_value = MagicMock(status_code=410)
        with pytest.raises(BoardGoneError) as exc_info:
            _fetch_postings_with_completeness("walmart.wd5/WalmartExternal")
        assert exc_info.value.status == 410

    @patch("job_finder.web.ats_platforms._platforms_workday.requests.post")
    def test_first_page_404_raises_board_gone(self, mock_post, _mock_detail):
        """First-page HTTP 404 → BoardGoneError (slug doesn't resolve)."""
        from job_finder.web.ats_platforms._platforms_workday import (
            _fetch_postings_with_completeness,
        )
        from job_finder.web.ats_platforms._registry import BoardGoneError

        mock_post.return_value = MagicMock(status_code=404)
        with pytest.raises(BoardGoneError):
            _fetch_postings_with_completeness("acme.wd5/Gone")

    @patch("job_finder.web.ats_platforms._platforms_workday.requests.post")
    def test_first_page_403_does_not_raise_board_gone(self, mock_post, _mock_detail):
        """First-page HTTP 403 (blocked/rate-limited, NOT gone) → incomplete, no
        raise: a transient block must never demote a real board."""
        from job_finder.web.ats_platforms._platforms_workday import (
            _fetch_postings_with_completeness,
        )

        mock_post.return_value = MagicMock(status_code=403)
        postings, complete = _fetch_postings_with_completeness("acme.wd5/Blocked")
        assert postings == []
        assert complete is False

    @patch("job_finder.web.ats_platforms._platforms_workday.requests.post")
    def test_mid_pagination_410_does_not_raise_board_gone(self, mock_post, _mock_detail):
        """A 410 AFTER page 1 (postings already collected) is a partial break, NOT
        board-gone — we never demote a board that just served real postings."""
        from job_finder.web.ats_platforms._platforms_workday import (
            _fetch_postings_with_completeness,
        )

        page1 = MagicMock(status_code=200)
        page1.json.return_value = {
            "total": 100,
            "jobPostings": [
                {"title": f"Job {i}", "externalPath": f"/job/Job-{i}_R-{i}"} for i in range(20)
            ],
        }
        page2 = MagicMock(status_code=410)
        mock_post.side_effect = [page1, page2]
        postings, complete = _fetch_postings_with_completeness("acme.wd5/Partial")
        assert len(postings) == 20
        assert complete is False

    @patch("job_finder.web.ats_platforms._platforms_workday.requests.post")
    def test_fetch_postings_thin_wrapper_returns_list(self, mock_post, _mock_detail):
        """_fetch_postings is a thin wrapper that discards the completeness flag."""
        from job_finder.web.ats_platforms._platforms_workday import _fetch_postings

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "total": 1,
            "jobPostings": [{"title": "Data Scientist", "externalPath": "/job/DS_R-1"}],
        }
        mock_post.return_value = mock_resp

        result = _fetch_postings("acme.wd5/AcmeExternal")
        assert isinstance(result, list)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Tests: _fetch_workday_description (per-job detail fetch)
# ---------------------------------------------------------------------------


class TestFetchWorkdayDescription:
    """Tests for the Workday per-job detail fetcher.

    The Workday CXS list endpoint returns titles only; the full HTML
    description lives at a separate per-job URL. These tests cover the
    detail-fetch behavior directly.
    """

    @patch("job_finder.web.ats_platforms.requests.get")
    def test_fetches_and_strips_html_description(self, mock_get):
        """_fetch_workday_description returns plain-text JD from HTML."""
        from job_finder.web.ats_platforms import _fetch_workday_description

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "jobPostingInfo": {
                "jobDescription": "<p>Design and build <b>scalable</b> data pipelines.</p>"
            }
        }
        mock_get.return_value = mock_resp

        text = _fetch_workday_description("walmart.wd5", "walmart", "WalmartExternal", "/job/DS-1")
        assert "Design and build" in text
        assert "scalable" in text
        assert "<b>" not in text  # HTML was stripped

    @patch("job_finder.web.ats_platforms.requests.get")
    def test_fetches_plain_text_description_unchanged(self, mock_get):
        """Non-HTML descriptions pass through without stripping."""
        from job_finder.web.ats_platforms import _fetch_workday_description

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "jobPostingInfo": {"jobDescription": "Plain text description here."}
        }
        mock_get.return_value = mock_resp

        text = _fetch_workday_description("walmart.wd5", "walmart", "WalmartExternal", "/job/DS-1")
        assert text == "Plain text description here."

    @patch("job_finder.web.ats_platforms.requests.get")
    def test_404_returns_empty_string(self, mock_get):
        """Detail endpoint 404 returns empty string, no exception."""
        from job_finder.web.ats_platforms import _fetch_workday_description

        mock_get.return_value = MagicMock(status_code=404)
        text = _fetch_workday_description("walmart.wd5", "walmart", "WalmartExternal", "/job/DNE")
        assert text == ""

    @patch("job_finder.web.ats_platforms.requests.get")
    def test_network_exception_returns_empty_string(self, mock_get):
        """Network error returns empty string, no exception."""
        from job_finder.web.ats_platforms import _fetch_workday_description

        mock_get.side_effect = Exception("timeout")
        text = _fetch_workday_description("walmart.wd5", "walmart", "WalmartExternal", "/job/DS-1")
        assert text == ""

    @patch("job_finder.web.ats_platforms.requests.get")
    def test_missing_jobPostingInfo_returns_empty_string(self, mock_get):
        """Response without jobPostingInfo key returns empty string."""
        from job_finder.web.ats_platforms import _fetch_workday_description

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"other": "shape"}
        mock_get.return_value = mock_resp

        text = _fetch_workday_description("walmart.wd5", "walmart", "WalmartExternal", "/job/DS-1")
        assert text == ""

    @patch("job_finder.web.ats_platforms.requests.get")
    @patch("job_finder.web.ats_platforms.requests.post")
    def test_scan_workday_populates_description_from_detail(self, mock_post, mock_get):
        """End-to-end: scan_workday calls detail endpoint and populates description."""
        from job_finder.web.ats_platforms import scan_workday

        # List endpoint returns one matching job
        list_resp = MagicMock(status_code=200)
        list_resp.json.return_value = {
            "total": 1,
            "jobPostings": [
                {
                    "title": "Senior Data Scientist",
                    "locationsText": "Remote",
                    "externalPath": "/job/Senior-DS_R-1",
                }
            ],
        }
        mock_post.return_value = list_resp

        # Detail endpoint returns the JD
        detail_resp = MagicMock(status_code=200)
        detail_resp.json.return_value = {
            "jobPostingInfo": {
                "jobDescription": "Full job description with details about the role."
            }
        }
        mock_get.return_value = detail_resp

        results = scan_workday(
            "walmart.wd5/WalmartExternal",
            target_titles=["data scientist"],
            exclusions=[],
        )
        assert len(results) == 1
        assert "Full job description" in results[0]["description"]
        # Detail URL hit correctly
        args, _ = mock_get.call_args
        assert args[0] == (
            "https://walmart.wd5.myworkdayjobs.com/wday/cxs/walmart/"
            "WalmartExternal/job/Senior-DS_R-1"
        )
