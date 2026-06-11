"""Unit tests for the pure direct-link resolution helpers."""

from __future__ import annotations

from job_finder.web.direct_link import (
    apply_url_for,
    is_ats_or_careers_url,
    pick_direct_link,
    promote_existing_direct_url,
    resolve_direct_link,
    resolve_primary_posting,
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


# ── resolve_primary_posting (strict-gated data merge) ─────────────────────────


def test_primary_posting_strict_returns_matched_posting():
    postings = [
        _posting("Senior Data Scientist", src="https://jobs.lever.co/acme/1"),
        _posting("Product Manager", src="https://jobs.lever.co/acme/2"),
    ]
    posting, url, confidence = resolve_primary_posting(postings, "Senior Data Scientist")
    assert posting is postings[0]
    assert url == "https://jobs.lever.co/acme/1"
    assert confidence == "strict"


def test_primary_posting_ambiguous_returns_no_posting():
    """Contamination guard: ambiguous title match must not expose a posting."""
    postings = [
        _posting("Data Scientist", src="https://jobs.lever.co/acme/1"),
        _posting("Data Scientist", src="https://jobs.lever.co/acme/2"),
    ]
    posting, url, confidence = resolve_primary_posting(postings, "Data Scientist")
    assert posting is None
    assert url == "https://jobs.lever.co/acme/1"
    assert confidence == "loose"


def test_primary_posting_no_exact_match_returns_no_posting():
    postings = [_posting("Staff Data Scientist", src="https://jobs.lever.co/acme/9")]
    posting, url, confidence = resolve_primary_posting(postings, "Data Scientist")
    assert posting is None
    assert url == "https://jobs.lever.co/acme/9"
    assert confidence == "loose"


def test_primary_posting_location_disambiguates_multi_location_board():
    """Same title in N locations: the job's location picks the strict match."""
    nyc = dict(_posting("Data Scientist", src="https://jobs.lever.co/acme/1"), location="New York")
    lon = dict(
        _posting("Data Scientist", src="https://jobs.lever.co/acme/2"), location="London, UK"
    )
    posting, url, confidence = resolve_primary_posting(
        [nyc, lon], "Data Scientist", "New York, NY"
    )
    assert posting is nyc
    assert url == "https://jobs.lever.co/acme/1"
    assert confidence == "strict"


def test_primary_posting_location_still_ambiguous_stays_loose():
    """Two postings sharing the job's location token: no strict promotion."""
    a = dict(_posting("Data Scientist", src="https://jobs.lever.co/acme/1"), location="Remote, US")
    b = dict(_posting("Data Scientist", src="https://jobs.lever.co/acme/2"), location="Remote, EU")
    posting, _url, confidence = resolve_primary_posting([a, b], "Data Scientist", "Remote")
    assert posting is None
    assert confidence == "loose"


def test_primary_posting_no_job_location_stays_loose():
    a = dict(_posting("Data Scientist", src="https://jobs.lever.co/acme/1"), location="New York")
    b = dict(_posting("Data Scientist", src="https://jobs.lever.co/acme/2"), location="London")
    posting, _url, confidence = resolve_primary_posting([a, b], "Data Scientist", "")
    assert posting is None
    assert confidence == "loose"


def test_primary_posting_none_when_no_links():
    assert resolve_primary_posting([_posting("Data Scientist")], "Data Scientist") is None
    assert resolve_primary_posting([], "Data Scientist") is None


# ── apply_url_for (Apply-button precedence) ───────────────────────────────────

_AGG = '["https://www.linkedin.com/jobs/view/1", "https://jooble.org/x"]'


def test_apply_strict_direct_url_wins():
    job = {
        "direct_url": "https://jobs.lever.co/acme/1",
        "direct_url_confidence": "strict",
        "source_urls": _AGG,
    }
    assert apply_url_for(job) == "https://jobs.lever.co/acme/1"


def test_apply_loose_falls_back_by_default():
    job = {
        "direct_url": "https://jobs.lever.co/acme/1",
        "direct_url_confidence": "loose",
        "source_urls": _AGG,
    }
    assert apply_url_for(job) == "https://www.linkedin.com/jobs/view/1"


def test_apply_loose_wins_when_flag_enabled():
    job = {
        "direct_url": "https://jobs.lever.co/acme/1",
        "direct_url_confidence": "loose",
        "source_urls": _AGG,
    }
    assert apply_url_for(job, loose_apply_default=True) == "https://jobs.lever.co/acme/1"


def test_apply_no_direct_url_uses_first_source_url():
    assert apply_url_for({"source_urls": _AGG}) == "https://www.linkedin.com/jobs/view/1"


def test_apply_accepts_parsed_list_and_missing_keys():
    assert apply_url_for({"source_urls": ["https://a.example/1"]}) == "https://a.example/1"
    assert apply_url_for({}) is None
    assert apply_url_for({"source_urls": None}) is None
    assert apply_url_for({"source_urls": "not-json"}) is None


def test_apply_direct_url_without_confidence_is_ignored():
    job = {"direct_url": "https://jobs.lever.co/acme/1", "source_urls": _AGG}
    assert apply_url_for(job) == "https://www.linkedin.com/jobs/view/1"


# ── apply_url_for staleness fallback (Phase 5) ────────────────────────────────


def test_apply_expired_strict_direct_url_falls_back_to_aggregator():
    """An expired job's primary posting is dead — skip direct_url even when the
    column still holds a strict link (the window before the reconciler NULLs it)
    and send the user to the aggregator listing instead."""
    job = {
        "direct_url": "https://jobs.lever.co/acme/1",
        "direct_url_confidence": "strict",
        "expiry_status": "expired",
        "source_urls": _AGG,
    }
    assert apply_url_for(job) == "https://www.linkedin.com/jobs/view/1"


def test_apply_expired_direct_url_with_no_source_urls_returns_none():
    """Expired direct_url and no aggregator fallback → no Apply target at all
    (better than a guaranteed 404)."""
    job = {
        "direct_url": "https://jobs.lever.co/acme/1",
        "direct_url_confidence": "strict",
        "expiry_status": "expired",
        "source_urls": "[]",
    }
    assert apply_url_for(job) is None


def test_apply_live_strict_direct_url_still_wins():
    """Non-expired strict link is unaffected by the staleness guard (regression
    on the happy path)."""
    job = {
        "direct_url": "https://jobs.lever.co/acme/1",
        "direct_url_confidence": "strict",
        "expiry_status": "live",
        "source_urls": _AGG,
    }
    assert apply_url_for(job) == "https://jobs.lever.co/acme/1"
