"""Tests for DataForSEOSource — Google Jobs SERP via DataForSEO API.

Coverage:
- Field mapping (_parse_item)
- Age filter (max_age_days against timestamp field)
- Salary extraction (_extract_salary)
- Timestamp parsing (_parse_timestamp)
- Task submission (_submit_tasks)
- Ready-task polling (_get_ready_task_ids)
- Task result fetching (_fetch_task_results)
- End-to-end fetch_jobs flow
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, call, patch

import pytest

from job_finder.sources.dataforseo_source import DataForSEOSource

# ---------------------------------------------------------------------------
# Helpers / shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def source():
    return DataForSEOSource(
        api_key="dGVzdDp0ZXN0",  # base64("test:test")
        max_age_days=7,
        depth=200,
        priority=1,
        poll_interval_seconds=0,  # No real sleeping in tests
        poll_timeout_seconds=5,
    )


def _ts_days_ago(days: int) -> str:
    """Return a DataForSEO-format timestamp string N days ago."""
    dt = datetime.now(UTC) - timedelta(days=days)
    # Format: "2026-04-01 12:00:00 +00:00"
    return dt.strftime("%Y-%m-%d %H:%M:%S +00:00")


def _make_item(**overrides) -> dict:
    """Build a valid google_jobs_item dict with sensible defaults."""
    base = {
        "type": "google_jobs_item",
        "rank_group": 1,
        "rank_absolute": 1,
        "position": "right",
        "xpath": "/html[1]/body[1]/div[1]",
        "job_id": "gys8I-Zhk2IO1l5VAAAAAA==",
        "title": "Staff Data Scientist",
        "employer_name": "Acme Corp",
        "employer_url": None,
        "employer_image_url": None,
        "location": "San Francisco, CA",
        "source_name": "via LinkedIn",
        "source_url": "https://linkedin.com/jobs/view/12345",
        "salary": None,
        "contract_type": "Full-time",
        "timestamp": _ts_days_ago(2),  # 2 days ago — within max_age_days=7
        "time_ago": "2 days ago",
        "rectangle": None,
    }
    base.update(overrides)
    return base


def _make_task_get_response(items: list[dict], status_code: int = 20000) -> dict:
    """Build a task_get/advanced response envelope."""
    return {
        "status_code": 20000,
        "status_message": "Ok.",
        "tasks": [
            {
                "id": "task-uuid-001",
                "status_code": status_code,
                "status_message": "Ok." if status_code == 20000 else "No Results.",
                "result": [
                    {
                        "keyword": "Staff Data Scientist",
                        "items_count": len(items),
                        "items": items,
                    }
                ]
                if status_code == 20000
                else None,
            }
        ],
    }


def _make_mock_response(json_data: dict, raise_for_status=None) -> MagicMock:
    """Build a mock requests.Response."""
    mock_resp = MagicMock()
    if raise_for_status is not None:
        mock_resp.raise_for_status.side_effect = raise_for_status
    else:
        mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = json_data
    return mock_resp


# ---------------------------------------------------------------------------
# Test: _parse_item field mapping
# ---------------------------------------------------------------------------


class TestParseItem:
    def test_extracts_title(self, source):
        job = source._parse_item(_make_item(title="Senior ML Engineer"))
        assert job is not None
        assert job.title == "Senior ML Engineer"

    def test_extracts_company(self, source):
        job = source._parse_item(_make_item(employer_name="Stripe"))
        assert job is not None
        assert job.company == "Stripe"

    def test_extracts_location(self, source):
        job = source._parse_item(_make_item(location="New York, NY"))
        assert job is not None
        assert job.location == "New York, NY"

    def test_extracts_source_id(self, source):
        job = source._parse_item(_make_item(job_id="abc123=="))
        assert job is not None
        assert job.source_id == "abc123=="

    def test_extracts_source_url(self, source):
        url = "https://greenhouse.io/jobs/456"
        job = source._parse_item(_make_item(source_url=url))
        assert job is not None
        assert job.source_url == url

    def test_source_field_is_dataforseo(self, source):
        job = source._parse_item(_make_item())
        assert job is not None
        assert job.source == "dataforseo"

    def test_description_is_none(self, source):
        """DataForSEO never returns description — enrichment fills it."""
        job = source._parse_item(_make_item())
        assert job is not None
        assert job.description is None

    def test_missing_title_returns_none(self, source):
        assert source._parse_item(_make_item(title="")) is None

    def test_missing_employer_returns_none(self, source):
        assert source._parse_item(_make_item(employer_name="")) is None


# ---------------------------------------------------------------------------
# Test: age filter
# ---------------------------------------------------------------------------


class TestAgeFilter:
    def test_rejects_job_older_than_max_age(self, source):
        """Timestamp 8 days ago with max_age_days=7 → None."""
        job = source._parse_item(_make_item(timestamp=_ts_days_ago(8)))
        assert job is None

    def test_accepts_job_within_max_age(self, source):
        """Timestamp 6 days ago with max_age_days=7 → Job."""
        job = source._parse_item(_make_item(timestamp=_ts_days_ago(6)))
        assert job is not None

    def test_accepts_job_with_no_timestamp(self, source):
        """Empty timestamp → no filter applied → Job included."""
        job = source._parse_item(_make_item(timestamp=""))
        assert job is not None

    def test_accepts_job_posted_today(self, source):
        """Timestamp from today → 0 days old → Job accepted."""
        job = source._parse_item(_make_item(timestamp=_ts_days_ago(0)))
        assert job is not None


# ---------------------------------------------------------------------------
# Test: salary extraction
# ---------------------------------------------------------------------------


class TestSalaryExtraction:
    def test_k_range_with_en_dash(self, source):
        low, high = source._extract_salary("$160K–$200K a year")
        assert low == 160000
        assert high == 200000

    def test_k_range_with_hyphen(self, source):
        low, high = source._extract_salary("$160K-$200K")
        assert low == 160000
        assert high == 200000

    def test_full_numbers_with_commas(self, source):
        low, high = source._extract_salary("$160,000–$200,000 a year")
        assert low == 160000
        assert high == 200000

    def test_none_returns_none_none(self, source):
        assert source._extract_salary(None) == (None, None)

    def test_no_match_returns_none_none(self, source):
        assert source._extract_salary("Competitive") == (None, None)

    def test_mixed_k_and_full_number(self, source):
        """$160K–$200,000: K only on low side."""
        low, high = source._extract_salary("$160K–$200,000 a year")
        assert low == 160000
        assert high == 200000


# ---------------------------------------------------------------------------
# Test: timestamp parsing
# ---------------------------------------------------------------------------


class TestParseTimestamp:
    def test_parses_dataforseo_format(self, source):
        dt = source._parse_timestamp("2026-04-01 12:00:00 +00:00")
        assert dt is not None
        assert dt.tzinfo is not None  # must be timezone-aware
        assert dt.year == 2026
        assert dt.month == 4
        assert dt.day == 1

    def test_returns_none_on_empty_string(self, source):
        assert source._parse_timestamp("") is None

    def test_returns_none_on_invalid(self, source):
        assert source._parse_timestamp("not a date") is None


# ---------------------------------------------------------------------------
# Test: _submit_tasks
# ---------------------------------------------------------------------------


class TestSubmitTasks:
    def test_posts_to_correct_url(self, source):
        mock_resp = _make_mock_response(
            {
                "tasks": [{"id": "id-001", "status_code": 20100}],
            }
        )
        with patch(
            "job_finder.sources.dataforseo_source.requests.post", return_value=mock_resp
        ) as mock_post:
            source._submit_tasks([{"query": "DS", "location": "SF"}])

        called_url = mock_post.call_args[0][0]
        assert called_url.endswith("/v3/serp/google/jobs/task_post")

    def test_sends_basic_auth(self, source):
        mock_resp = _make_mock_response(
            {
                "tasks": [{"id": "id-001", "status_code": 20100}],
            }
        )
        with patch(
            "job_finder.sources.dataforseo_source.requests.post", return_value=mock_resp
        ) as mock_post:
            source._submit_tasks([{"query": "DS", "location": "SF"}])

        headers = mock_post.call_args[1]["headers"]
        assert headers["Authorization"] == "Basic dGVzdDp0ZXN0"

    def test_sends_all_queries_in_one_request(self, source):
        """3 queries → 1 POST call with 3-element body."""
        mock_resp = _make_mock_response(
            {
                "tasks": [
                    {"id": "id-001", "status_code": 20100},
                    {"id": "id-002", "status_code": 20100},
                    {"id": "id-003", "status_code": 20100},
                ],
            }
        )
        queries = [
            {"query": "DS", "location": "SF"},
            {"query": "MLE", "location": "NYC"},
            {"query": "DE", "location": "Remote"},
        ]
        with patch(
            "job_finder.sources.dataforseo_source.requests.post", return_value=mock_resp
        ) as mock_post:
            source._submit_tasks(queries)

        assert mock_post.call_count == 1
        payload = mock_post.call_args[1]["json"]
        assert len(payload) == 3

    def test_embeds_depth_and_priority(self, source):
        mock_resp = _make_mock_response(
            {
                "tasks": [{"id": "id-001", "status_code": 20100}],
            }
        )
        with patch(
            "job_finder.sources.dataforseo_source.requests.post", return_value=mock_resp
        ) as mock_post:
            source._submit_tasks([{"query": "DS", "location": "SF"}])

        payload = mock_post.call_args[1]["json"]
        assert payload[0]["depth"] == 200
        assert payload[0]["priority"] == 1

    def test_uses_location_name_from_query(self, source):
        """Unknown location strings pass through as-is."""
        mock_resp = _make_mock_response(
            {
                "tasks": [{"id": "id-001", "status_code": 20100}],
            }
        )
        with patch(
            "job_finder.sources.dataforseo_source.requests.post", return_value=mock_resp
        ) as mock_post:
            source._submit_tasks([{"query": "DS", "location": "New York,New York,United States"}])

        payload = mock_post.call_args[1]["json"]
        assert payload[0].get("location_name") == "New York,New York,United States"
        assert "location_code" not in payload[0]

    def test_maps_sf_bay_area_to_dataforseo_location_name(self, source):
        """'San Francisco Bay Area' is translated to the hierarchical DataForSEO format."""
        mock_resp = _make_mock_response(
            {
                "tasks": [{"id": "id-001", "status_code": 20100}],
            }
        )
        with patch(
            "job_finder.sources.dataforseo_source.requests.post", return_value=mock_resp
        ) as mock_post:
            source._submit_tasks([{"query": "DS", "location": "San Francisco Bay Area"}])

        payload = mock_post.call_args[1]["json"]
        assert payload[0].get("location_name") == "San Francisco,California,United States"
        assert "location_code" not in payload[0]

    def test_remote_location_uses_us_location_code(self, source):
        """'Remote' has no valid DataForSEO location name — falls back to US-wide location_code."""
        mock_resp = _make_mock_response(
            {
                "tasks": [{"id": "id-001", "status_code": 20100}],
            }
        )
        with patch(
            "job_finder.sources.dataforseo_source.requests.post", return_value=mock_resp
        ) as mock_post:
            source._submit_tasks([{"query": "DS", "location": "Remote"}])

        payload = mock_post.call_args[1]["json"]
        assert payload[0].get("location_code") == 2840
        assert "location_name" not in payload[0]

    def test_uses_location_code_when_no_location(self, source):
        mock_resp = _make_mock_response(
            {
                "tasks": [{"id": "id-001", "status_code": 20100}],
            }
        )
        with patch(
            "job_finder.sources.dataforseo_source.requests.post", return_value=mock_resp
        ) as mock_post:
            source._submit_tasks([{"query": "DS"}])

        payload = mock_post.call_args[1]["json"]
        assert payload[0].get("location_code") == 2840
        assert "location_name" not in payload[0]

    def test_returns_empty_on_http_error(self, source):
        import requests as req

        with patch(
            "job_finder.sources.dataforseo_source.requests.post", side_effect=req.HTTPError("403")
        ):
            result = source._submit_tasks([{"query": "DS", "location": "SF"}])
        assert result == []

    def test_returns_empty_on_json_error(self, source):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.side_effect = ValueError("not json")
        with patch("job_finder.sources.dataforseo_source.requests.post", return_value=mock_resp):
            result = source._submit_tasks([{"query": "DS", "location": "SF"}])
        assert result == []

    def test_collects_task_ids_from_20100_tasks(self, source):
        mock_resp = _make_mock_response(
            {
                "tasks": [
                    {"id": "id-aaa", "status_code": 20100},
                    {"id": "id-bbb", "status_code": 20100},
                ],
            }
        )
        with patch("job_finder.sources.dataforseo_source.requests.post", return_value=mock_resp):
            result = source._submit_tasks(
                [
                    {"query": "DS", "location": "SF"},
                    {"query": "MLE", "location": "NY"},
                ]
            )
        assert result == ["id-aaa", "id-bbb"]

    def test_skips_tasks_with_non_20100_status(self, source):
        """1 success + 1 error task → only 1 ID returned."""
        mock_resp = _make_mock_response(
            {
                "tasks": [
                    {"id": "id-aaa", "status_code": 20100},
                    {"id": "id-bbb", "status_code": 40210, "status_message": "Insufficient funds"},
                ],
            }
        )
        with patch("job_finder.sources.dataforseo_source.requests.post", return_value=mock_resp):
            result = source._submit_tasks(
                [
                    {"query": "DS", "location": "SF"},
                    {"query": "MLE", "location": "NY"},
                ]
            )
        assert result == ["id-aaa"]


# ---------------------------------------------------------------------------
# Test: _get_ready_task_ids
# ---------------------------------------------------------------------------


class TestGetReadyTaskIds:
    def test_returns_ready_ids(self, source):
        mock_resp = _make_mock_response(
            {
                "status_code": 20000,
                "tasks": [
                    {
                        "result": [
                            {"id": "id-aaa", "se": "google", "se_type": "jobs"},
                            {"id": "id-bbb", "se": "google", "se_type": "jobs"},
                            {"id": "id-ccc", "se": "google", "se_type": "jobs"},
                        ],
                    }
                ],
            }
        )
        with patch("job_finder.sources.dataforseo_source.requests.get", return_value=mock_resp):
            result = source._get_ready_task_ids()
        assert result == ["id-aaa", "id-bbb", "id-ccc"]

    def test_returns_empty_on_http_error(self, source):
        import requests as req

        with patch(
            "job_finder.sources.dataforseo_source.requests.get", side_effect=req.HTTPError("503")
        ):
            result = source._get_ready_task_ids()
        assert result == []

    def test_returns_empty_on_non_20000_status(self, source):
        mock_resp = _make_mock_response(
            {
                "status_code": 40202,
                "status_message": "Rate limit exceeded",
                "tasks": [],
            }
        )
        with patch("job_finder.sources.dataforseo_source.requests.get", return_value=mock_resp):
            result = source._get_ready_task_ids()
        assert result == []


# ---------------------------------------------------------------------------
# Test: _fetch_task_results
# ---------------------------------------------------------------------------


class TestFetchTaskResults:
    def test_returns_jobs_for_20000_task(self, source):
        mock_resp = _make_mock_response(
            _make_task_get_response([_make_item(), _make_item(job_id="other-id")])
        )
        with patch("job_finder.sources.dataforseo_source.requests.get", return_value=mock_resp):
            jobs = source._fetch_task_results("task-uuid-001")
        assert len(jobs) == 2

    def test_returns_empty_for_40102_no_results(self, source):
        mock_resp = _make_mock_response(_make_task_get_response([], status_code=40102))
        with patch("job_finder.sources.dataforseo_source.requests.get", return_value=mock_resp):
            jobs = source._fetch_task_results("task-uuid-001")
        assert jobs == []

    def test_returns_empty_for_expired_task(self, source):
        mock_resp = _make_mock_response(
            {
                "status_code": 20000,
                "tasks": [
                    {
                        "id": "task-uuid-001",
                        "status_code": 40403,
                        "status_message": "Results expired",
                        "result": None,
                    }
                ],
            }
        )
        with patch("job_finder.sources.dataforseo_source.requests.get", return_value=mock_resp):
            jobs = source._fetch_task_results("task-uuid-001")
        assert jobs == []

    def test_age_filter_applied(self, source):
        """Items older than max_age_days are excluded; recent items are kept."""
        items = [
            _make_item(job_id="old-job", timestamp=_ts_days_ago(10)),  # too old
            _make_item(job_id="new-job", timestamp=_ts_days_ago(3)),  # within range
        ]
        mock_resp = _make_mock_response(_make_task_get_response(items))
        with patch("job_finder.sources.dataforseo_source.requests.get", return_value=mock_resp):
            jobs = source._fetch_task_results("task-uuid-001")
        assert len(jobs) == 1
        assert jobs[0].source_id == "new-job"


# ---------------------------------------------------------------------------
# Test: fetch_jobs (integration-style, all HTTP mocked)
# ---------------------------------------------------------------------------


class TestFetchJobs:
    def _make_tasks_ready_response(self, task_ids: list[str]) -> dict:
        return {
            "status_code": 20000,
            "tasks": [
                {
                    "result": [{"id": tid} for tid in task_ids],
                }
            ],
        }

    def test_submits_then_polls_then_collects(self, source):
        """Happy path: task_post → tasks_ready → task_get for each ID."""
        task_post_resp = _make_mock_response(
            {
                "tasks": [
                    {"id": "id-001", "status_code": 20100},
                    {"id": "id-002", "status_code": 20100},
                ],
            }
        )
        tasks_ready_resp = _make_mock_response(
            self._make_tasks_ready_response(["id-001", "id-002"])
        )
        task_get_resp_1 = _make_mock_response(
            _make_task_get_response([_make_item(job_id="job-id-001")])
        )
        task_get_resp_2 = _make_mock_response(
            _make_task_get_response([_make_item(job_id="job-id-002")])
        )

        post_call = MagicMock(return_value=task_post_resp)
        # tasks_ready is first GET, then task_get twice (different jobs each)
        get_call = MagicMock(side_effect=[tasks_ready_resp, task_get_resp_1, task_get_resp_2])

        with (
            patch("job_finder.sources.dataforseo_source.requests.post", post_call),
            patch("job_finder.sources.dataforseo_source.requests.get", get_call),
        ):
            jobs = source.fetch_jobs(
                [
                    {"query": "DS", "location": "SF"},
                    {"query": "MLE", "location": "NY"},
                ]
            )

        assert post_call.call_count == 1
        assert get_call.call_count == 3  # 1 tasks_ready + 2 task_get
        assert len(jobs) == 2

    def test_returns_empty_when_no_tasks_submitted(self, source):
        """task_post returns no valid task IDs → fetch_jobs returns []."""
        mock_resp = _make_mock_response(
            {
                "tasks": [
                    {"id": "id-001", "status_code": 40210, "status_message": "Insufficient funds"},
                ],
            }
        )
        with patch("job_finder.sources.dataforseo_source.requests.post", return_value=mock_resp):
            jobs = source.fetch_jobs([{"query": "DS", "location": "SF"}])
        assert jobs == []

    def test_partial_results_on_timeout(self, source):
        """2 tasks submitted, only 1 completes before timeout → return completed task's jobs."""
        # source has poll_timeout_seconds=5 and poll_interval_seconds=0
        task_post_resp = _make_mock_response(
            {
                "tasks": [
                    {"id": "id-001", "status_code": 20100},
                    {"id": "id-002", "status_code": 20100},
                ],
            }
        )
        # tasks_ready only ever returns id-001 (id-002 never appears)
        tasks_ready_resp = _make_mock_response(self._make_tasks_ready_response(["id-001"]))
        task_get_resp = _make_mock_response(_make_task_get_response([_make_item()]))

        # Use a source with very short timeout so we don't loop forever
        short_source = DataForSEOSource(
            api_key="dGVzdDp0ZXN0",
            max_age_days=7,
            depth=200,
            priority=1,
            poll_interval_seconds=0,
            poll_timeout_seconds=0,  # zero timeout — exits immediately after first poll
        )

        post_call = MagicMock(return_value=task_post_resp)
        get_call = MagicMock(side_effect=[tasks_ready_resp, task_get_resp])

        with (
            patch("job_finder.sources.dataforseo_source.requests.post", post_call),
            patch("job_finder.sources.dataforseo_source.requests.get", get_call),
        ):
            jobs = short_source.fetch_jobs(
                [
                    {"query": "DS", "location": "SF"},
                    {"query": "MLE", "location": "NY"},
                ]
            )

        # Only 1 task completed (id-001), so 1 job returned
        assert len(jobs) == 1

    def test_returns_empty_for_empty_queries(self, source):
        jobs = source.fetch_jobs([])
        assert jobs == []

    def test_deduplicates_within_run(self, source):
        """Two tasks returning the same job_id → only 1 Job in output."""
        same_item = _make_item(job_id="duplicate-id")
        task_post_resp = _make_mock_response(
            {
                "tasks": [
                    {"id": "id-001", "status_code": 20100},
                    {"id": "id-002", "status_code": 20100},
                ],
            }
        )
        tasks_ready_resp = _make_mock_response(
            self._make_tasks_ready_response(["id-001", "id-002"])
        )
        task_get_resp = _make_mock_response(_make_task_get_response([same_item]))

        post_call = MagicMock(return_value=task_post_resp)
        get_call = MagicMock(side_effect=[tasks_ready_resp, task_get_resp, task_get_resp])

        with (
            patch("job_finder.sources.dataforseo_source.requests.post", post_call),
            patch("job_finder.sources.dataforseo_source.requests.get", get_call),
        ):
            jobs = source.fetch_jobs(
                [
                    {"query": "DS", "location": "SF"},
                    {"query": "DS", "location": "NYC"},
                ]
            )

        assert len(jobs) == 1
        assert jobs[0].source_id == "duplicate-id"


# ---------------------------------------------------------------------------
# Test: public submit_tasks / collect_results split interface
# ---------------------------------------------------------------------------


class TestSubmitAndCollect:
    """Tests for the public submit_tasks / collect_results split interface."""

    def test_submit_tasks_returns_task_ids(self, source):
        """submit_tasks() delegates to _submit_tasks and returns IDs."""
        mock_resp = _make_mock_response(
            {
                "tasks": [
                    {"id": "id-001", "status_code": 20100},
                    {"id": "id-002", "status_code": 20100},
                ],
            }
        )
        with patch("job_finder.sources.dataforseo_source.requests.post", return_value=mock_resp):
            result = source.submit_tasks(
                [
                    {"query": "DS", "location": "SF"},
                    {"query": "MLE", "location": "NY"},
                ]
            )
        assert result == ["id-001", "id-002"]

    def test_submit_tasks_returns_empty_for_empty_queries(self, source):
        """Empty queries list → [] without any HTTP call."""
        result = source.submit_tasks([])
        assert result == []

    def test_collect_results_returns_empty_for_empty_task_ids(self, source):
        """Empty task_ids → [] without any HTTP call."""
        result = source.collect_results([])
        assert result == []

    def test_collect_results_polls_and_returns_jobs(self, source):
        """collect_results() polls tasks_ready, fetches each task, returns jobs."""
        tasks_ready_resp = _make_mock_response(
            {
                "status_code": 20000,
                "tasks": [{"result": [{"id": "id-001"}, {"id": "id-002"}]}],
            }
        )
        task_get_resp_1 = _make_mock_response(
            _make_task_get_response([_make_item(job_id="job-001")])
        )
        task_get_resp_2 = _make_mock_response(
            _make_task_get_response([_make_item(job_id="job-002")])
        )
        get_call = MagicMock(side_effect=[tasks_ready_resp, task_get_resp_1, task_get_resp_2])
        with patch("job_finder.sources.dataforseo_source.requests.get", get_call):
            jobs = source.collect_results(["id-001", "id-002"])
        assert len(jobs) == 2
        assert {j.source_id for j in jobs} == {"job-001", "job-002"}

    def test_fetch_jobs_equivalent_to_submit_then_collect(self, source):
        """fetch_jobs(queries) produces same result as submit_tasks+collect_results."""
        queries = [{"query": "DS", "location": "SF"}]

        def _make_post_resp():
            return _make_mock_response(
                {
                    "tasks": [{"id": "id-001", "status_code": 20100}],
                }
            )

        def _make_get_side_effects():
            return [
                _make_mock_response(
                    {
                        "status_code": 20000,
                        "tasks": [{"result": [{"id": "id-001"}]}],
                    }
                ),
                _make_mock_response(_make_task_get_response([_make_item(job_id="job-001")])),
            ]

        # Run via fetch_jobs (composed path)
        with (
            patch(
                "job_finder.sources.dataforseo_source.requests.post",
                return_value=_make_post_resp(),
            ),
            patch(
                "job_finder.sources.dataforseo_source.requests.get",
                MagicMock(side_effect=_make_get_side_effects()),
            ),
        ):
            combined_jobs = source.fetch_jobs(queries)

        # Run via separate submit + collect
        with (
            patch(
                "job_finder.sources.dataforseo_source.requests.post",
                return_value=_make_post_resp(),
            ),
            patch(
                "job_finder.sources.dataforseo_source.requests.get",
                MagicMock(side_effect=_make_get_side_effects()),
            ),
        ):
            task_ids = source.submit_tasks(queries)
            split_jobs = source.collect_results(task_ids)

        assert len(combined_jobs) == len(split_jobs) == 1
        assert combined_jobs[0].source_id == split_jobs[0].source_id == "job-001"

    def test_fetch_jobs_sleeps_initial_delay_before_first_poll(self):
        """fetch_jobs() sleeps _POLL_INITIAL_DELAY_SECONDS once; collect_results uses uniform poll_interval."""
        # Use poll_interval_seconds=30 (production-like).
        retry_interval = 30
        slow_source = DataForSEOSource(
            api_key="dGVzdDp0ZXN0",
            max_age_days=7,
            depth=200,
            priority=1,
            poll_interval_seconds=retry_interval,
            poll_timeout_seconds=300,
        )

        # POST: one task submitted
        task_post_resp = _make_mock_response(
            {
                "tasks": [{"id": "id-001", "status_code": 20100}],
            }
        )
        # GET poll: task ready on first attempt
        tasks_ready_resp = _make_mock_response(
            {
                "status_code": 20000,
                "tasks": [{"result": [{"id": "id-001"}]}],
            }
        )
        task_get_resp = _make_mock_response(
            _make_task_get_response([_make_item(job_id="job-001")])
        )

        from job_finder.sources.dataforseo_source import _POLL_INITIAL_DELAY_SECONDS

        with (
            patch("job_finder.sources.dataforseo_source.time.sleep") as mock_sleep,
            patch(
                "job_finder.sources.dataforseo_source.requests.post", return_value=task_post_resp
            ),
            patch(
                "job_finder.sources.dataforseo_source.requests.get",
                MagicMock(side_effect=[tasks_ready_resp, task_get_resp]),
            ),
        ):
            jobs = slow_source.fetch_jobs([{"query": "DS", "location": "SF"}])

        assert len(jobs) == 1
        assert jobs[0].source_id == "job-001"

        # Two sleeps: initial delay (from fetch_jobs) + uniform poll_interval (from collect_results)
        assert mock_sleep.call_count == 2
        sleep_calls = mock_sleep.call_args_list
        assert sleep_calls[0] == call(_POLL_INITIAL_DELAY_SECONDS)  # 45s in fetch_jobs
        assert sleep_calls[1] == call(retry_interval)  # uniform poll in collect_results

    def test_collect_results_uses_uniform_poll_interval(self):
        """collect_results() directly uses uniform poll_interval — no initial delay."""
        retry_interval = 30
        slow_source = DataForSEOSource(
            api_key="dGVzdDp0ZXN0",
            max_age_days=7,
            depth=200,
            priority=1,
            poll_interval_seconds=retry_interval,
            poll_timeout_seconds=300,
        )
        # First poll: task not yet ready
        not_ready_resp = _make_mock_response(
            {
                "status_code": 20000,
                "tasks": [{"result": []}],
            }
        )
        # Second poll: task ready
        ready_resp = _make_mock_response(
            {
                "status_code": 20000,
                "tasks": [{"result": [{"id": "id-001"}]}],
            }
        )
        task_get_resp = _make_mock_response(
            _make_task_get_response([_make_item(job_id="job-001")])
        )

        get_call = MagicMock(side_effect=[not_ready_resp, ready_resp, task_get_resp])

        from job_finder.sources.dataforseo_source import _POLL_INITIAL_DELAY_SECONDS

        with (
            patch("job_finder.sources.dataforseo_source.time.sleep") as mock_sleep,
            patch("job_finder.sources.dataforseo_source.requests.get", get_call),
        ):
            jobs = slow_source.collect_results(["id-001"])

        assert len(jobs) == 1
        assert jobs[0].source_id == "job-001"

        # Both sleeps use the uniform poll_interval — no initial delay in collect_results
        assert mock_sleep.call_count == 2
        sleep_calls = mock_sleep.call_args_list
        assert sleep_calls[0] == call(retry_interval)  # uniform interval, not initial delay
        assert sleep_calls[1] == call(retry_interval)
        assert all(c != call(_POLL_INITIAL_DELAY_SECONDS) for c in sleep_calls)
