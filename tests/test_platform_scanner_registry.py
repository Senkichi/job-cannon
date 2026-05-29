"""Tests for the PlatformScanner registry and shared scan driver.

Exercises the driver against a mock ``PlatformScanner`` so the spine is
verified in isolation from any platform's HTTP shape. Per-platform
fetchers stay tested via the existing scanner-specific test files.
"""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import MagicMock, patch

import pytest
import requests

from job_finder.web.ats_platforms._registry import (
    PlatformScanner,
    _http_get_json,
    run_platform_scan,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_scanner(
    *,
    postings: list[dict] | None = None,
    title_key: str = "title",
    posting_to_job: Callable | None = None,
    name: str = "mock",
    company_source: str = "Mock",
) -> PlatformScanner:
    """Build a PlatformScanner whose fetch returns a fixed list."""

    def _fetch(_slug):
        return list(postings or [])

    def _title_of(p):
        return p.get(title_key, "")

    def _default_posting_to_job(p, slug):
        return {
            "title": p.get(title_key, ""),
            "company_source": company_source,
            "location": p.get("location", ""),
            "description": "",
            "source_url": f"https://mock/{slug}/{p.get('id', '')}",
            "salary_min": None,
            "salary_max": None,
            "comp_json": None,
        }

    return PlatformScanner(
        name=name,
        company_source=company_source,
        fetch_postings=_fetch,
        title_of=_title_of,
        posting_to_job=posting_to_job or _default_posting_to_job,
    )


# ---------------------------------------------------------------------------
# run_platform_scan
# ---------------------------------------------------------------------------


class TestRunPlatformScan:
    def test_empty_postings_returns_empty_list(self):
        scanner = _make_mock_scanner(postings=[])
        assert run_platform_scan(scanner, "anyslug", ["foo"], []) == []

    def test_all_postings_matched_returns_all(self):
        scanner = _make_mock_scanner(
            postings=[
                {"title": "Senior Data Scientist", "id": "1"},
                {"title": "Staff Data Scientist", "id": "2"},
            ]
        )
        results = run_platform_scan(scanner, "acme", ["data scientist"], [])
        assert len(results) == 2
        assert results[0]["title"] == "Senior Data Scientist"
        assert results[0]["source_url"] == "https://mock/acme/1"
        assert results[1]["title"] == "Staff Data Scientist"

    def test_title_filter_excludes_non_matching(self):
        scanner = _make_mock_scanner(
            postings=[
                {"title": "Senior Data Scientist", "id": "1"},
                {"title": "Marketing Manager", "id": "2"},
            ]
        )
        results = run_platform_scan(scanner, "acme", ["data scientist"], [])
        assert len(results) == 1
        assert results[0]["title"] == "Senior Data Scientist"

    def test_exclusion_filter_drops_excluded(self):
        scanner = _make_mock_scanner(
            postings=[
                {"title": "Senior Data Scientist", "id": "1"},
                {"title": "Junior Data Scientist", "id": "2"},
            ]
        )
        results = run_platform_scan(scanner, "acme", ["data scientist"], ["junior"])
        assert len(results) == 1
        assert results[0]["title"] == "Senior Data Scientist"

    def test_posting_to_job_returning_none_skips(self):
        def _to_job(posting, _slug):
            if posting.get("title", "").startswith("Senior"):
                return None
            return {"title": posting.get("title"), "company_source": "Mock"}

        scanner = _make_mock_scanner(
            postings=[
                {"title": "Senior Data Scientist", "id": "1"},
                {"title": "Staff Data Scientist", "id": "2"},
            ],
            posting_to_job=_to_job,
        )
        results = run_platform_scan(scanner, "acme", ["data scientist"], [])
        assert len(results) == 1
        assert results[0]["title"] == "Staff Data Scientist"

    def test_empty_target_titles_allows_all_through(self):
        """With empty target_titles, the title gate accepts every title."""
        scanner = _make_mock_scanner(
            postings=[
                {"title": "Anything Goes", "id": "1"},
                {"title": "Anything Else", "id": "2"},
            ]
        )
        results = run_platform_scan(scanner, "acme", [], [])
        assert len(results) == 2

    def test_fetch_returning_generator_works(self):
        """fetch_postings can return any iterable; driver lists it."""

        def _fetch(_slug):
            yield {"title": "Data Scientist", "id": "1"}
            yield {"title": "Data Engineer", "id": "2"}

        scanner = PlatformScanner(
            name="gen",
            company_source="Gen",
            fetch_postings=_fetch,  # type: ignore[arg-type]
            title_of=lambda p: p.get("title", ""),
            posting_to_job=lambda p, _s: {"title": p["title"]},
        )
        results = run_platform_scan(scanner, "acme", ["data scientist"], [])
        assert len(results) == 1


# ---------------------------------------------------------------------------
# _http_get_json
# ---------------------------------------------------------------------------


class TestHttpGetJson:
    @patch("job_finder.web.ats_platforms._registry.requests.get")
    def test_success_returns_parsed_json(self, mock_get):
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"jobs": [{"id": "1"}]}
        mock_get.return_value = mock_resp

        result = _http_get_json("https://x", log_label="scan_x", slug="foo")
        assert result == {"jobs": [{"id": "1"}]}

    @patch("job_finder.web.ats_platforms._registry.requests.get")
    def test_non_200_returns_none(self, mock_get):
        mock_get.return_value = MagicMock(status_code=404)
        assert _http_get_json("https://x", log_label="scan_x", slug="foo") is None

    @patch("job_finder.web.ats_platforms._registry.requests.get")
    def test_exception_returns_none(self, mock_get):
        mock_get.side_effect = Exception("connection refused")
        assert _http_get_json("https://x", log_label="scan_x", slug="foo") is None

    @patch("job_finder.web.ats_platforms._registry.requests.get")
    def test_json_parse_error_returns_none(self, mock_get):
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.side_effect = ValueError("bad json")
        mock_get.return_value = mock_resp
        assert _http_get_json("https://x", log_label="scan_x", slug="foo") is None

    @patch("job_finder.web.ats_platforms._registry.time.sleep")
    @patch("job_finder.web.ats_platforms._registry.requests.get")
    def test_timeout_without_retry_returns_none(self, mock_get, mock_sleep):
        mock_get.side_effect = requests.exceptions.Timeout("read timeout")
        assert _http_get_json("https://x", log_label="scan_x", slug="foo") is None
        assert mock_get.call_count == 1
        mock_sleep.assert_not_called()

    @patch("job_finder.web.ats_platforms._registry.time.sleep")
    @patch("job_finder.web.ats_platforms._registry.requests.get")
    def test_timeout_with_retry_retries_once_then_succeeds(self, mock_get, mock_sleep):
        success_resp = MagicMock(status_code=200)
        success_resp.json.return_value = {"ok": True}
        mock_get.side_effect = [requests.exceptions.Timeout("first attempt"), success_resp]

        result = _http_get_json(
            "https://x",
            log_label="scan_x",
            slug="foo",
            retry_on_timeout=True,
        )
        assert result == {"ok": True}
        assert mock_get.call_count == 2
        mock_sleep.assert_called_once_with(2)

    @patch("job_finder.web.ats_platforms._registry.time.sleep")
    @patch("job_finder.web.ats_platforms._registry.requests.get")
    def test_timeout_with_retry_gives_up_after_second_attempt(self, mock_get, mock_sleep):
        mock_get.side_effect = [
            requests.exceptions.Timeout("first"),
            requests.exceptions.Timeout("second"),
        ]
        result = _http_get_json(
            "https://x",
            log_label="scan_x",
            slug="foo",
            retry_on_timeout=True,
        )
        assert result is None
        assert mock_get.call_count == 2
        mock_sleep.assert_called_once_with(2)

    @patch("job_finder.web.ats_platforms._registry.requests.get")
    def test_params_and_headers_forwarded(self, mock_get):
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = []
        mock_get.return_value = mock_resp
        _http_get_json(
            "https://x",
            log_label="scan_x",
            slug="foo",
            params={"offset": 0, "limit": 100},
            headers={"Accept": "application/json"},
        )
        _, kwargs = mock_get.call_args
        assert kwargs["params"] == {"offset": 0, "limit": 100}
        assert kwargs["headers"] == {"Accept": "application/json"}


# ---------------------------------------------------------------------------
# Platform SCANNER constants — surface check
# ---------------------------------------------------------------------------


class TestPlatformScannerConstants:
    """Confirm each platform module exports a well-formed SCANNER."""

    @pytest.mark.parametrize(
        "module_path,expected_name,expected_company_source",
        [
            ("job_finder.web.ats_platforms._platforms_lever", "lever", "Lever"),
            (
                "job_finder.web.ats_platforms._platforms_greenhouse",
                "greenhouse",
                "Greenhouse",
            ),
            ("job_finder.web.ats_platforms._platforms_ashby", "ashby", "Ashby"),
            ("job_finder.web.ats_platforms._platforms_workday", "workday", "Workday"),
            (
                "job_finder.web.ats_platforms._platforms_smartrecruiters",
                "smartrecruiters",
                "SmartRecruiters",
            ),
            (
                "job_finder.web.ats_platforms._platforms_recruitee",
                "recruitee",
                "Recruitee",
            ),
            ("job_finder.web.ats_platforms._platforms_breezy", "breezy", "Breezy"),
            ("job_finder.web.ats_platforms._platforms_jazzhr", "jazzhr", "JazzHR"),
            ("job_finder.web.ats_platforms._platforms_pinpoint", "pinpoint", "Pinpoint"),
            ("job_finder.web.ats_platforms._platforms_personio", "personio", "Personio"),
            ("job_finder.web.ats_platforms._platforms_bamboohr", "bamboohr", "BambooHR"),
            (
                "job_finder.web.ats_platforms._platforms_teamtailor",
                "teamtailor",
                "Teamtailor",
            ),
        ],
    )
    def test_scanner_constant_well_formed(
        self, module_path, expected_name, expected_company_source
    ):
        import importlib

        mod = importlib.import_module(module_path)
        scanner = mod.SCANNER
        assert isinstance(scanner, PlatformScanner)
        assert scanner.name == expected_name
        assert scanner.company_source == expected_company_source
        # The three callables must all exist and be callable.
        assert callable(scanner.fetch_postings)
        assert callable(scanner.title_of)
        assert callable(scanner.posting_to_job)
