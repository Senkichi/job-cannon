"""Tests for the Oracle Recruiting Cloud (Fusion CE) platform scanner.

Covers URL detection (host + site → "{host}|{site}" slug, region variants,
negatives), the scanner's requisitionList parsing + offset pagination +
BoardGoneError, the canonical job-dict mapping, and dispatch registration.
"""

from unittest.mock import MagicMock, patch

import pytest

from job_finder.web.ats_detection import extract_ats_from_url_best
from job_finder.web.ats_platforms import SCANNERS_BY_NAME, scan_oracle_cloud
from job_finder.web.ats_platforms import _platforms_oracle_cloud as orc
from job_finder.web.ats_platforms._registry import BoardGoneError

_HOST = "ibtcjb.fa.ocs.oraclecloud.com"


# ── URL detection ────────────────────────────────────────────────────────────


def test_ce_url_with_site_returns_host_and_site():
    url = f"https://{_HOST}/hcmUI/CandidateExperience/en/sites/CX_1/requisitions"
    assert extract_ats_from_url_best(url) == ("oracle_cloud", f"{_HOST}|CX_1", 5)


def test_ce_url_without_site_defaults_cx1():
    url = f"https://{_HOST}/hcmUI/CandidateExperience/en/"
    assert extract_ats_from_url_best(url) == ("oracle_cloud", f"{_HOST}|CX_1", 5)


def test_rest_api_url_extracts_site_number():
    url = (
        f"https://{_HOST}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
        "?finder=findReqs;siteNumber=CX_2,limit=25"
    )
    assert extract_ats_from_url_best(url) == ("oracle_cloud", f"{_HOST}|CX_2", 5)


def test_region_variant_host_preserved():
    host = "evfc.fa.us2.oraclecloud.com"
    url = f"https://{host}/hcmUI/CandidateExperience/en/sites/CX_3/jobs"
    assert extract_ats_from_url_best(url) == ("oracle_cloud", f"{host}|CX_3", 5)


def test_host_lowercased():
    url = "https://IBTCJB.FA.OCS.ORACLECLOUD.COM/hcmUI/CandidateExperience/en/sites/CX_1/"
    assert extract_ats_from_url_best(url) == ("oracle_cloud", f"{_HOST}|CX_1", 5)


def test_oracle_marketing_host_returns_none():
    # Oracle's own product/marketing host is not a Fusion pod -> not an ATS.
    assert extract_ats_from_url_best("https://www.oracle.com/careers/") is None


def test_non_oracle_url_returns_none():
    assert extract_ats_from_url_best("https://boards.greenhouse.io/acme") == (
        "greenhouse",
        "acme",
        5,
    )
    assert extract_ats_from_url_best("https://acme.com/careers") is None


# ── Scanner behavior ─────────────────────────────────────────────────────────


def _resp(status: int, payload: dict | None = None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else {}
    return r


def _page(reqs: list[dict], total: int) -> dict:
    return {"items": [{"TotalJobsCount": total, "requisitionList": reqs}]}


def _req(rid: str, title: str, **extra) -> dict:
    base = {
        "Id": rid,
        "Title": title,
        "PostedDate": "2026-06-23",
        "PrimaryLocation": "Austin, TX, United States",
        "WorkplaceTypeCode": "ORA_ON_SITE",
        "ShortDescriptionStr": "A short blurb.",
    }
    base.update(extra)
    return base


@patch("job_finder.web.ats_platforms._platforms_oracle_cloud.requests.get")
def test_fetch_postings_single_page(mock_get):
    mock_get.return_value = _resp(200, _page([_req("1", "Data Analyst")], total=1))
    out = orc._fetch_postings(f"{_HOST}|CX_1")
    assert [r["Id"] for r in out] == ["1"]


@patch("job_finder.web.ats_platforms._platforms_oracle_cloud.requests.get")
def test_fetch_postings_paginates(mock_get, monkeypatch):
    # Shrink the page size so two small pages exercise the offset loop.
    monkeypatch.setattr(orc, "_PAGE_SIZE", 2)
    mock_get.side_effect = [
        _resp(200, _page([_req("1", "A"), _req("2", "B")], total=3)),
        _resp(200, _page([_req("3", "C")], total=3)),
    ]
    out = orc._fetch_postings(f"{_HOST}|CX_1")
    assert [r["Id"] for r in out] == ["1", "2", "3"]
    assert mock_get.call_count == 2


@patch("job_finder.web.ats_platforms._platforms_oracle_cloud.requests.get")
def test_first_page_404_raises_board_gone(mock_get):
    mock_get.return_value = _resp(404)
    with pytest.raises(BoardGoneError):
        orc._fetch_postings(f"{_HOST}|CX_1")


@patch("job_finder.web.ats_platforms._platforms_oracle_cloud.requests.get")
def test_empty_board_is_clean_miss(mock_get):
    mock_get.return_value = _resp(200, _page([], total=0))
    assert orc._fetch_postings(f"{_HOST}|CX_1") == []


def test_posting_to_job_builds_canonical_dict():
    posting = _req("42251", "Junior Intelligence Analyst", WorkplaceTypeCode="ORA_REMOTE")
    job = orc._posting_to_job(posting, f"{_HOST}|CX_1")
    assert job["title"] == "Junior Intelligence Analyst"
    assert job["company_source"] == "Oracle Cloud"
    assert job["location"] == "Austin, TX, United States"
    assert job["posted_date"] == "2026-06-23"
    assert job["is_remote"] is True
    assert job["source_id"] == "42251"
    assert job["source_url"] == (
        f"https://{_HOST}/hcmUI/CandidateExperience/en/sites/CX_1/job/42251"
    )
    assert job["salary_min"] is None and job["salary_max"] is None


def test_posting_to_job_onsite_is_not_remote():
    job = orc._posting_to_job(_req("9", "Engineer"), f"{_HOST}|CX_1")
    assert job["is_remote"] is False


def test_posting_missing_id_is_skipped():
    assert orc._posting_to_job({"Title": "x"}, f"{_HOST}|CX_1") is None


# ── Dispatch registration ────────────────────────────────────────────────────


def test_oracle_cloud_registered_for_dispatch():
    assert "oracle_cloud" in SCANNERS_BY_NAME
    from job_finder.web.ats_scanner._run import _PLATFORM_SCANNERS

    assert "oracle_cloud" in _PLATFORM_SCANNERS


@patch("job_finder.web.ats_platforms._platforms_oracle_cloud.requests.get")
def test_scan_oracle_cloud_title_gate(mock_get):
    mock_get.return_value = _resp(
        200,
        _page(
            [_req("1", "Senior Data Analyst"), _req("2", "Line Cook")],
            total=2,
        ),
    )
    jobs = scan_oracle_cloud(f"{_HOST}|CX_1", ["Data Analyst"], [])
    assert [j["title"] for j in jobs] == ["Senior Data Analyst"]
