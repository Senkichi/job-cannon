"""Inter-page pacing tests for paginating ATS platform scanners (U5).

Pre-F1 (commit b99e1d9) the LIST-endpoint cadence on Workday and
SmartRecruiters was incidentally paced by the per-matched-posting
detail-fetch sleep inside the same per-page loop. F1 split the title-
match gate out of ``_fetch_postings``, which collapsed the inter-page
delay to zero. U5 restored an explicit ``_PAGE_FETCH_SLEEP_S`` before
each page after the first.

These tests assert: for N pages, ``time.sleep`` is called exactly N-1
times with ``_PAGE_FETCH_SLEEP_S`` (one sleep before each page after
the first). Total request count is unaffected.

See .planning/specs/2026-05-26-polish-review-audit.md (MAJOR — Workday +
SmartRecruiters pagination) and
.planning/specs/2026-05-27-polish-review-followups-plan.md (U5).
"""

from unittest.mock import MagicMock, patch


class TestWorkdayPagination:
    def test_fetch_sleeps_between_page_posts(self):
        """U5: _fetch_postings sleeps _PAGE_FETCH_SLEEP_S before each page
        after the first; LIST endpoint is not hammered for multi-page tenants."""
        from job_finder.web.ats_platforms_internal import _platforms_workday

        # Two pages of _PAGE_SIZE=20 postings; total=40 forces a 2nd POST.
        page_1 = {
            "total": 40,
            "jobPostings": [
                {"title": f"Job {i}", "externalPath": f"/job/{i}", "locationsText": ""}
                for i in range(20)
            ],
        }
        page_2 = {
            "total": 40,
            "jobPostings": [
                {"title": f"Job {i + 20}", "externalPath": f"/job/{i + 20}", "locationsText": ""}
                for i in range(20)
            ],
        }

        sleep_calls: list[float] = []
        with (
            patch.object(_platforms_workday, "requests") as mock_req,
            patch.object(
                _platforms_workday.time,
                "sleep",
                side_effect=lambda s: sleep_calls.append(s),
            ),
        ):
            mock_req.post.side_effect = [
                MagicMock(status_code=200, json=lambda: page_1),
                MagicMock(status_code=200, json=lambda: page_2),
            ]
            result = _platforms_workday._fetch_postings("acme.wd1/AcmeExternal")

        assert mock_req.post.call_count == 2
        assert sleep_calls == [_platforms_workday._PAGE_FETCH_SLEEP_S]
        assert len(result) == 40

    def test_single_page_does_not_sleep(self):
        """U5 guard: single-page response triggers zero inter-page sleeps."""
        from job_finder.web.ats_platforms_internal import _platforms_workday

        single_page = {
            "total": 5,
            "jobPostings": [
                {"title": f"Job {i}", "externalPath": f"/job/{i}", "locationsText": ""}
                for i in range(5)
            ],
        }

        sleep_calls: list[float] = []
        with (
            patch.object(_platforms_workday, "requests") as mock_req,
            patch.object(
                _platforms_workday.time,
                "sleep",
                side_effect=lambda s: sleep_calls.append(s),
            ),
        ):
            mock_req.post.return_value = MagicMock(status_code=200, json=lambda: single_page)
            _platforms_workday._fetch_postings("acme.wd1/AcmeExternal")

        assert mock_req.post.call_count == 1
        assert sleep_calls == []


class TestSmartRecruitersPagination:
    def test_fetch_sleeps_between_page_gets(self):
        """U5: same contract as Workday, GET-paginated variant."""
        from job_finder.web.ats_platforms_internal import _platforms_smartrecruiters

        page_1 = {
            "totalFound": 200,
            "content": [{"name": f"J{i}", "id": str(i)} for i in range(100)],
        }
        page_2 = {
            "totalFound": 200,
            "content": [{"name": f"J{i + 100}", "id": str(i + 100)} for i in range(100)],
        }

        sleep_calls: list[float] = []
        with (
            patch.object(_platforms_smartrecruiters, "requests") as mock_req,
            patch.object(
                _platforms_smartrecruiters.time,
                "sleep",
                side_effect=lambda s: sleep_calls.append(s),
            ),
        ):
            mock_req.get.side_effect = [
                MagicMock(status_code=200, json=lambda: page_1),
                MagicMock(status_code=200, json=lambda: page_2),
            ]
            result = _platforms_smartrecruiters._fetch_postings("acme")

        assert mock_req.get.call_count == 2
        assert sleep_calls == [_platforms_smartrecruiters._PAGE_FETCH_SLEEP_S]
        assert len(result) == 200

    def test_single_page_does_not_sleep(self):
        """U5 guard: single-page response triggers zero inter-page sleeps."""
        from job_finder.web.ats_platforms_internal import _platforms_smartrecruiters

        single_page = {
            "totalFound": 3,
            "content": [{"name": f"J{i}", "id": str(i)} for i in range(3)],
        }

        sleep_calls: list[float] = []
        with (
            patch.object(_platforms_smartrecruiters, "requests") as mock_req,
            patch.object(
                _platforms_smartrecruiters.time,
                "sleep",
                side_effect=lambda s: sleep_calls.append(s),
            ),
        ):
            mock_req.get.return_value = MagicMock(status_code=200, json=lambda: single_page)
            _platforms_smartrecruiters._fetch_postings("acme")

        assert mock_req.get.call_count == 1
        assert sleep_calls == []
