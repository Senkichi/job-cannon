"""Tests for the result-count / category-landing tile filter (#211).

Three layers of enforcement, one predicate:

  1. ``is_listing_tile`` predicate — unit pack: tiles match, real postings
     (including numeric-prefixed legitimate titles) do not.
  2. ``ParsedJob.from_job`` — raises ``ListingTileError`` (hard drop, I-14).
  3. ``_extract_jobs_from_soup`` static-tier early-drop — a category-link
     <a> tile is rejected before it ever reaches persistence.

Root cause (#211): the static crawler harvested careers-page *category landing*
links — anchor text "84 Data Scientist Jobs" — which ordered-words-matched the
target "Data Scientist" and slipped the keyword gate, then scored as a real
posting. The fix rejects the tile shape at the source boundary.
"""

from __future__ import annotations

import contextlib
from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup

from job_finder.models import Job
from job_finder.parsed_job import ListingTileError, ParsedJob
from job_finder.web.careers_crawler._static_tier import _extract_jobs_from_soup
from job_finder.web.careers_crawler._title_filters import _is_listing_tile, is_listing_tile

# ---------------------------------------------------------------------------
# Layer 1: predicate unit pack
# ---------------------------------------------------------------------------

# Real count tiles / category-landing titles — MUST match.
_TILE_TITLES = [
    "84 Data Scientist Jobs",  # the exact #211 Capital One offender
    "71 Business Analyst Jobs",
    "1,200+ openings",
    "12 results",
    "5 positions",
    "3 roles",
    "27 opportunities",
    "1 job",  # singular noun variant
    "100+ Jobs",
    "  9 Software Engineer Positions  ",  # surrounding whitespace tolerated
    "250 OPENINGS",  # case-insensitive
]

# Legitimate postings (some numeric-prefixed) — MUST NOT match.
_NON_TILE_TITLES = [
    "Data Scientist",
    "Senior Software Engineer",
    "100 Women in Finance — Analyst",  # numeric prefix, no listing-noun end
    "3D Artist",  # leading digit glued to a word, no space, no listing noun
    "5G Network Engineer",
    "Jobs Data Analyst",  # listing noun mid-string, not end-anchored
    "Director of Open Roles Strategy",  # "roles" mid-string
    "Engineer — 401k and 12 other benefits",  # number not leading
    "",  # empty
    "Lead Positions Manager",  # no leading count
]


@pytest.mark.parametrize("title", _TILE_TITLES)
def test_listing_tile_predicate_matches_tiles(title):
    assert _is_listing_tile(title) is True, f"expected tile match for {title!r}"


@pytest.mark.parametrize("title", _NON_TILE_TITLES)
def test_listing_tile_predicate_rejects_real_titles(title):
    assert _is_listing_tile(title) is False, f"expected non-match for {title!r}"


def test_public_alias_is_same_callable():
    assert is_listing_tile is _is_listing_tile


# ---------------------------------------------------------------------------
# Layer 2: ParsedJob.from_job hard-drop (I-14)
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _clean_patches():
    """Disable I-10 (company denylist) so the tile validator is what fires."""
    with (
        patch("job_finder.parsed_job.load_config", return_value={}),
        patch("job_finder.parsed_job.get_company_denylist", return_value=frozenset()),
    ):
        yield


def _make_job(title: str, source: str = "careers_crawl") -> Job:
    return Job(
        title=title,
        company="Capital One",
        location="",
        source=source,
        source_url="https://www.capitalonecareers.com/category/data-science-jobs/234/24980/1",
        source_id="",
    )


def test_from_job_raises_on_listing_tile():
    job = _make_job("84 Data Scientist Jobs", source="careers_page")
    with _clean_patches(), pytest.raises(ListingTileError):
        ParsedJob.from_job(job)


def test_from_job_does_not_raise_on_real_title():
    job = _make_job("Data Scientist", source="careers_page")
    with _clean_patches():
        result = ParsedJob.from_job(job)
    assert isinstance(result, ParsedJob)
    assert result.title == "Data Scientist"


def test_from_job_does_not_raise_on_numeric_prefixed_real_title():
    job = _make_job("100 Women in Finance — Analyst")
    with _clean_patches():
        result = ParsedJob.from_job(job)
    assert isinstance(result, ParsedJob)


# ---------------------------------------------------------------------------
# Layer 3: static-tier early-drop
# ---------------------------------------------------------------------------

_CATEGORY_PAGE_HTML = """
<html><body>
  <ul class="results">
    <li><a href="/category/data-science-jobs/234/24980/1">84 Data Scientist Jobs</a></li>
    <li><a href="/jobs/data-scientist-r12345">Data Scientist</a></li>
  </ul>
</body></html>
"""


def test_static_tier_drops_category_tile_keeps_real_posting():
    soup = BeautifulSoup(_CATEGORY_PAGE_HTML, "html.parser")
    results = _extract_jobs_from_soup(
        soup,
        base_url="https://www.capitalonecareers.com/",
        target_titles=["Data Scientist"],
        exclusions=[],
    )
    titles = [r["title"] for r in results]
    # The category tile "84 Data Scientist Jobs" keyword-matches the target but
    # must be dropped; the real posting survives.
    assert "84 Data Scientist Jobs" not in titles
    assert "Data Scientist" in titles
