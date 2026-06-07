"""Unit tests for the pure direct-link resolution helpers."""

from __future__ import annotations

from job_finder.web.direct_link import (
    is_ats_or_careers_url,
    pick_direct_link,
    promote_existing_direct_url,
    resolve_direct_link,
)


def test_is_ats_url_recognizes_known_platforms():
    assert is_ats_or_careers_url("https://boards.greenhouse.io/acme/jobs/1")
    assert is_ats_or_careers_url("https://jobs.lever.co/acme/abc-123")
    assert is_ats_or_careers_url("https://jobs.ashbyhq.com/acme/xyz")
    assert is_ats_or_careers_url("https://acme.wd5.myworkdayjobs.com/ext/job/1")
    assert is_ats_or_careers_url("https://careers.smartrecruiters.com/Acme/123")


def test_is_ats_url_rejects_aggregators():
    assert not is_ats_or_careers_url("https://www.linkedin.com/jobs/view/123")
    assert not is_ats_or_careers_url("https://www.glassdoor.com/job/abc")
    assert not is_ats_or_careers_url("https://jooble.org/jdp/123")
    assert not is_ats_or_careers_url("")
    assert not is_ats_or_careers_url(None)


def test_promote_returns_first_ats_url():
    urls = [
        "https://www.linkedin.com/jobs/view/123",
        "https://jobs.lever.co/acme/abc-123",
        "https://boards.greenhouse.io/acme/jobs/1",
    ]
    assert promote_existing_direct_url(urls) == "https://jobs.lever.co/acme/abc-123"


def test_promote_returns_none_when_only_aggregators():
    urls = ["https://www.linkedin.com/jobs/view/123", "https://jooble.org/x"]
    assert promote_existing_direct_url(urls) is None
    assert promote_existing_direct_url([]) is None


def _posting(title, url=None, src=None):
    p = {"title": title}
    if url is not None:
        p["url"] = url
    if src is not None:
        p["source_url"] = src
    return p


def test_resolve_strict_unique_exact_title():
    postings = [
        _posting("Senior Data Scientist", src="https://jobs.lever.co/acme/1"),
        _posting("Product Manager", src="https://jobs.lever.co/acme/2"),
    ]
    assert resolve_direct_link(postings, "Senior Data Scientist") == (
        "https://jobs.lever.co/acme/1",
        "strict",
    )


def test_resolve_strict_uses_abbreviation_expansion():
    postings = [_posting("Sr DS", src="https://jobs.lever.co/acme/1")]
    assert resolve_direct_link(postings, "Senior Data Scientist") == (
        "https://jobs.lever.co/acme/1",
        "strict",
    )


def test_resolve_ambiguous_exact_title_falls_back_to_loose():
    postings = [
        _posting("Data Scientist", src="https://jobs.lever.co/acme/1"),
        _posting("Data Scientist", src="https://jobs.lever.co/acme/2"),
    ]
    assert resolve_direct_link(postings, "Data Scientist") == (
        "https://jobs.lever.co/acme/1",
        "loose",
    )


def test_resolve_loose_when_no_exact_match():
    postings = [_posting("Staff Data Scientist", src="https://jobs.lever.co/acme/9")]
    assert resolve_direct_link(postings, "Data Scientist") == (
        "https://jobs.lever.co/acme/9",
        "loose",
    )


def test_resolve_reads_careers_url_key():
    postings = [_posting("Data Scientist", url="https://acme.com/careers/1")]
    assert resolve_direct_link(postings, "Data Scientist") == (
        "https://acme.com/careers/1",
        "strict",
    )


def test_resolve_skips_posting_without_link():
    postings = [_posting("Data Scientist")]  # no url, no source_url
    assert resolve_direct_link(postings, "Data Scientist") is None
    assert resolve_direct_link([], "Data Scientist") is None


def test_pick_prefers_existing_ats_source_url_strict():
    cand = pick_direct_link(
        source_urls=["https://boards.greenhouse.io/acme/jobs/1"],
        ats_result={
            "direct_url": "https://jobs.lever.co/acme/2",
            "direct_url_confidence": "loose",
        },
        careers_result={},
    )
    assert cand == ("https://boards.greenhouse.io/acme/jobs/1", "strict")


def test_pick_uses_ats_result_when_no_promotion():
    cand = pick_direct_link(
        source_urls=["https://www.linkedin.com/jobs/view/1"],
        ats_result={
            "direct_url": "https://jobs.lever.co/acme/2",
            "direct_url_confidence": "strict",
        },
        careers_result={
            "direct_url": "https://acme.com/careers/9",
            "direct_url_confidence": "strict",
        },
    )
    assert cand == ("https://jobs.lever.co/acme/2", "strict")


def test_pick_falls_back_to_careers():
    cand = pick_direct_link(
        source_urls=["https://www.linkedin.com/jobs/view/1"],
        ats_result={},
        careers_result={
            "direct_url": "https://acme.com/careers/9",
            "direct_url_confidence": "loose",
        },
    )
    assert cand == ("https://acme.com/careers/9", "loose")


def test_pick_returns_none_when_nothing_resolves():
    assert pick_direct_link(["https://www.linkedin.com/jobs/view/1"], {}, {}) is None
    assert pick_direct_link([], {}, {}) is None
