"""Tests for the positive title contract + deterministic repair + retroactive re-sweep.

Covers the fail-closed title-hygiene architecture (I-16/I-17):
  * title_contract_violation — high-precision shape + non-posting predicate
  * clean_title repair — recovers a real title from a scraped card tail
  * title_jd_mismatch — silent-wrong-title cross-validation
  * ParsedJob.from_job integration — repair vs quarantine routing
  * _run_title_resweep_if_stale — retroactive heal + declassify + watermark
  * aggregator-domain scrape blocklist

The "must pass" legitimate cases are the ones the adversarial review proved a
naive blocklist would destroy (CJK titles, pipe titles, year-cohort intern
titles, verbose government titles) — they are the regression guard against the
contract over-firing.
"""

from __future__ import annotations

import json

import pytest

from job_finder.web.careers_crawler._title_contract import (
    TITLE_HYGIENE_VERSION,
    TITLE_INVALID_SHAPE,
    TITLE_NON_POSTING,
    title_contract_violation,
    title_jd_mismatch,
)
from job_finder.web.careers_crawler._title_filters import clean_title

# ---------------------------------------------------------------------------
# title_contract_violation — shape violations (must quarantine)
# ---------------------------------------------------------------------------

_SHAPE_VIOLATIONS = [
    "View Job Senior Data Scientist Apply Now",  # unrepairable leading+trailing chrome
    "Senior\tData Scientist",  # control/tab char
    "Senior\nAnalyst",  # newline
    "Data Scientist Posted Jun 15, 2026 in NYC end",  # embedded full date mid-string
    "Engineer 2026-06-15 role",  # embedded ISO date
    "Analyst Apply Now",  # CTA
    "Engineer →",  # trailing arrow glyph
    "",  # empty
    "   ",  # whitespace-only
]


@pytest.mark.parametrize("title", _SHAPE_VIOLATIONS)
def test_shape_violations_quarantined(title):
    assert title_contract_violation(title) == TITLE_INVALID_SHAPE


# ---------------------------------------------------------------------------
# title_contract_violation — non-posting funnel entries (must quarantine)
# ---------------------------------------------------------------------------

_NON_POSTING = [
    "Talent Network: Lead Data Scientist",
    "Talent Community - Engineering",
    "Talent Pool - Customer Excellence Senior Analyst",
    "General Application",
    "Speculative Application - Data",
    "Join Our Talent Network",
    "Future Opportunities in Analytics",
    "Expression of Interest",
]


@pytest.mark.parametrize("title", _NON_POSTING)
def test_non_posting_quarantined(title):
    assert title_contract_violation(title) == TITLE_NON_POSTING


# ---------------------------------------------------------------------------
# title_contract_violation — legitimate titles (MUST pass; the over-fire guard)
# ---------------------------------------------------------------------------

_LEGIT = [
    "Senior Data Scientist",
    "Data Scientist / AI Engineer",
    "Strategic Finance & Analytics Manager | USA | Remote",  # pipes are fine
    "AI Transformation Senior Manager | Retail | Agentic Commerce",
    "[Summer 2026] People Data Scientist Intern",  # lone year is fine
    "Graduate 2026 PhD Software Engineer II",
    "Business Analyst, Fall 2026 (Co-op/Internship)",
    "Staff Research Associate 2 (9612C), California Institute for Quantitative Biosciences",
    "[쿠팡] 쿠팡이츠 Business Development Analyst",  # mixed CJK + ASCII, tolerated
    "医药代表精英储备岗位-深圳",  # full CJK title
    "Talent Acquisition Specialist",  # "talent" but NOT "talent network"
    "Community Manager",  # "community" but NOT "talent community"
    "iOS Developer",
    "3D Artist",
]


@pytest.mark.parametrize("title", _LEGIT)
def test_legit_titles_pass(title):
    assert title_contract_violation(title) is None


# ---------------------------------------------------------------------------
# clean_title repair — the censused card-tail junk is recovered to a clean title
# ---------------------------------------------------------------------------

_REPAIR_CASES = [
    ("Data Scientist / IA Engineer Jun 15, 2026 View Job →", "Data Scientist / IA Engineer"),
    ("Senior Data Scientist View Job →", "Senior Data Scientist"),
    ("Machine Learning Engineer Apply Now", "Machine Learning Engineer"),
]


@pytest.mark.parametrize("raw,expected", _REPAIR_CASES)
def test_clean_title_repairs_card_tail(raw, expected):
    assert clean_title(raw) == expected
    # And the repaired title satisfies the contract.
    assert title_contract_violation(clean_title(raw)) is None


def test_clean_title_idempotent():
    raw = "Data Scientist / IA Engineer Jun 15, 2026 View Job →"
    once = clean_title(raw)
    assert clean_title(once) == once


def test_repair_never_empties_a_title():
    # A title that is ENTIRELY chrome must not be reduced to "" (head < min keeps original).
    raw = "View Job →"
    out = clean_title(raw)
    assert out  # non-empty
    # It is still quarantined by the contract (unrepairable).
    assert title_contract_violation(out) is not None


# ---------------------------------------------------------------------------
# title_jd_mismatch — silent-wrong-title cross-validation (high precision)
# ---------------------------------------------------------------------------


def test_jd_mismatch_zero_overlap_flags():
    title = "Engineering Roles"  # a section heading, not a posting
    jd = (
        "We are hiring a marketing coordinator to manage social media campaigns, "
        "draft newsletters, coordinate with the brand team, and report on funnel "
        "metrics. The ideal candidate has agency experience and copywriting skills. "
    ) * 2
    assert title_jd_mismatch(title, jd) is True


def test_jd_match_does_not_flag():
    title = "Senior Data Scientist"
    jd = (
        "As a senior data scientist you will build models, run experiments, and "
        "partner with engineering on production ML systems. " * 4
    )
    assert title_jd_mismatch(title, jd) is False


def test_jd_mismatch_short_jd_never_flags():
    assert title_jd_mismatch("Engineering Roles", "short") is False


def test_jd_mismatch_no_jd_never_flags():
    assert title_jd_mismatch("Engineering Roles", None) is False


def test_jd_mismatch_single_token_title_never_flags():
    # A one-content-word title ("Staff UX Researcher" -> just "researcher") is too
    # easy to false-flag; the >= 2-token requirement must suppress it.
    jd = "We are hiring a marketing coordinator for social campaigns. " * 6
    assert title_jd_mismatch("Staff UX Researcher", jd) is False


def test_jd_mismatch_stem_prefix_tolerates_morphology():
    # "researcher" should match a JD that only says "research" (stem prefix).
    jd = "You will lead UX research across the product org and mentor the team. " * 5
    assert title_jd_mismatch("User Researcher Lead", jd) is False


# ---------------------------------------------------------------------------
# ParsedJob.from_job integration — repair vs quarantine routing
# ---------------------------------------------------------------------------


def _from_job(title):
    from job_finder.models import Job
    from job_finder.parsed_job import ParsedJob

    job = Job(
        title=title, company="Acme Corp", location="", source="careers_page", source_url="http://x"
    )
    return ParsedJob.from_job(job)


def test_from_job_repairs_real_title_buried_in_card():
    from job_finder.parsed_job import ParsedJob

    p = _from_job("Data Scientist / IA Engineer Jun 15, 2026 View Job →")
    assert isinstance(p, ParsedJob)
    assert p.title == "Data Scientist / IA Engineer"
    assert p.unresolved_reasons == []


def test_from_job_quarantines_non_posting():
    from job_finder.parsed_job import UnresolvedParsedJob

    p = _from_job("Talent Network: Lead Data Scientist Jun 16, 2026 View Job →")
    assert isinstance(p, UnresolvedParsedJob)
    assert TITLE_NON_POSTING in p.unresolved_reasons


def test_from_job_quarantines_unrepairable_shape():
    from job_finder.parsed_job import UnresolvedParsedJob

    p = _from_job("View Job Senior Data Scientist Apply Now")
    assert isinstance(p, UnresolvedParsedJob)
    assert TITLE_INVALID_SHAPE in p.unresolved_reasons


def test_from_job_clean_title_unaffected():
    from job_finder.parsed_job import ParsedJob

    p = _from_job("Senior Data Scientist")
    assert isinstance(p, ParsedJob)
    assert p.unresolved_reasons == []


# ---------------------------------------------------------------------------
# Retroactive re-sweep — heals legacy rows + declassifies + stamps watermark
# ---------------------------------------------------------------------------


def _insert_job(conn, dedup_key, title, *, classification="apply", reasons="[]", jd=None):
    conn.execute(
        "INSERT INTO jobs (dedup_key, title, company, location, sources, unresolved_reasons, "
        "classification, sub_scores_json, fit_analysis, jd_full, first_seen, last_seen, "
        "pipeline_status) VALUES (?, ?, ?, '', '[\"careers_page\"]', ?, ?, '{}', 'fit', ?, "
        "'2026-01-01', '2026-01-01', 'discovered')",
        (dedup_key, title, "Acme Corp", reasons, classification, jd),
    )
    conn.commit()


def test_resweep_heals_legacy_rows(migrated_db):
    from job_finder.web.migrations._post_hooks import _run_title_resweep_if_stale

    _path, conn = migrated_db

    # Legacy junk that predates the contract: stored clean (reasons='[]'), scored apply.
    _insert_job(conn, "acme|junk", "Mangled Engineer Jun 15, 2026 View Job →")
    _insert_job(conn, "acme|funnel", "Talent Network: Lead Data Scientist")
    _insert_job(conn, "acme|clean", "Senior Data Scientist")  # control — must stay untouched

    # Reset the watermark below the live version to arm the sweep (the fixture's
    # migration run already stamped it to current on the empty template).
    conn.execute("UPDATE schema_meta SET value = '0' WHERE key = 'title_hygiene_version'")
    conn.commit()

    _run_title_resweep_if_stale(conn)

    # NOTE: a rewrite triggers run_retroactive_dedup, which re-derives every
    # row's dedup_key to normalized form — so look rows up by title, not by the
    # raw keys we inserted.
    def by_title(prefix):
        return conn.execute(
            "SELECT title, raw_title, unresolved_reasons, classification FROM jobs "
            "WHERE title LIKE ?",
            (prefix + "%",),
        ).fetchone()

    # Repaired row: title cleaned, original preserved, declassified (re-score), not quarantined.
    junk = by_title("Mangled Engineer")
    assert junk["title"] == "Mangled Engineer"
    assert junk["raw_title"] == "Mangled Engineer Jun 15, 2026 View Job →"
    assert junk["classification"] is None
    assert json.loads(junk["unresolved_reasons"]) == []

    # Non-posting row: quarantined, declassified, title unchanged, raw_title untouched.
    funnel = by_title("Talent Network")
    assert TITLE_NON_POSTING in json.loads(funnel["unresolved_reasons"])
    assert funnel["classification"] is None
    assert funnel["raw_title"] is None

    # Control clean row: completely untouched.
    clean = by_title("Senior Data Scientist")
    assert clean["title"] == "Senior Data Scientist"
    assert clean["classification"] == "apply"
    assert clean["raw_title"] is None
    assert json.loads(clean["unresolved_reasons"]) == []

    # Watermark advanced.
    wm = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'title_hygiene_version'"
    ).fetchone()[0]
    assert int(wm) == TITLE_HYGIENE_VERSION


def test_resweep_idempotent(migrated_db):
    from job_finder.web.migrations._post_hooks import _run_title_resweep_if_stale

    _path, conn = migrated_db
    _insert_job(conn, "acme|clean2", "Staff Data Scientist")
    # Watermark already at current (fixture migrated) → sweep is a no-op.
    _run_title_resweep_if_stale(conn)
    row = conn.execute(
        "SELECT classification FROM jobs WHERE dedup_key = 'acme|clean2'"
    ).fetchone()
    assert row["classification"] == "apply"  # untouched


# ---------------------------------------------------------------------------
# Aggregator-domain scrape blocklist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,blocked",
    [
        ("https://jobflarely.liveblog365.com/jobs", True),
        ("https://liveblog365.com/x", True),
        ("https://foo.nerdleveltech.com/careers", True),
        ("https://boards.greenhouse.io/acme", False),
        ("https://acme.com/careers", False),
        ("", False),
    ],
)
def test_blocklisted_scrape_host(url, blocked):
    from job_finder.web.careers_scraper import _is_blocklisted_scrape_host

    assert _is_blocklisted_scrape_host(url) is blocked
