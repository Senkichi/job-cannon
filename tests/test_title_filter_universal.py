"""
Tests for universal title-filter enforcement via ParsedJob.from_job (Phase 48.01).

Acceptance criteria from issue #52:
- ParsedJob.from_job calls is_metadata_blob + clean_title for every caller.
- Fixtures from each of the three documented callsites yield UnresolvedParsedJob
  with 'title_metadata_blob' reason.
- Shim path: a Job with a metadata-blob title passed to upsert_job produces
  UpsertResult.unresolved_reasons containing 'title_metadata_blob'.

Callsite shapes exercised:
  1. AI-nav tier  — UNDP-style labeled-form blob (phrase markers).
  2. careers_scraper.py:322/:602 — Blue State paren-close shape (I-08 regex).
  3. _static_tier.py — req-ID pipe pattern (_REQ_ID_PIPE_RE in is_metadata_blob).
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import tempfile
from unittest.mock import patch

from job_finder.models import Job
from job_finder.parsed_job import ParsedJob, UnresolvedParsedJob

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_job(title: str, source: str = "careers_crawl") -> Job:
    return Job(
        title=title,
        company="Test Corp",
        location="New York, NY",
        source=source,
        source_url="https://example.com/jobs/1",
        source_id="",
    )


@contextlib.contextmanager
def _clean_patches():
    """Disable I-10 (company denylist) so other validators can be exercised."""
    with (
        patch("job_finder.parsed_job.load_config", return_value={}),
        patch("job_finder.parsed_job.get_company_denylist", return_value=frozenset()),
    ):
        yield


# ---------------------------------------------------------------------------
# Three-source blob fixtures
# ---------------------------------------------------------------------------


class TestTitleFilterUniversalBlobs:
    """Phase 48.01: is_metadata_blob + clean_title run in from_job for all callers.

    Each test stands in for one of the three callsite paths that previously
    bypassed the static-tier title filter.
    """

    def test_ai_nav_tier_style_metadata_blob(self):
        """AI-nav tier extraction style: UNDP-style labeled-form blob.

        The AI navigator renders JavaScript-heavy pages; some aggregate sites
        concatenate title + metadata labels as inline text without separators.
        The 'job title', 'post level', and 'apply by' phrase markers in
        _is_metadata_blob catch this shape.
        """
        # Simulates UNDP-style careers page where field labels run into the title.
        blob_title = "Job TitleSenior Software EngineerPost levelNPSA-9Apply byApr-29-26"
        job = _make_job(blob_title, source="careers_crawl")
        with _clean_patches():
            result = ParsedJob.from_job(job)
        assert isinstance(result, UnresolvedParsedJob), (
            f"Expected UnresolvedParsedJob for {blob_title!r}, got {type(result).__name__}"
        )
        assert "title_metadata_blob" in result.unresolved_reasons
        assert result.raw_title == blob_title

    def test_careers_scraper_style_metadata_blob(self):
        """careers_scraper.py:322/:602 extraction style: Blue State paren-close shape.

        The careers_page low-tier path produces ')XX' patterns when inline
        title + location text is concatenated without separator whitespace.
        I-08 (_TITLE_LOCATION_BLEED_RE) catches this shape.  Runs on raw_title
        before clean_title strips the state-code suffix via _NOSEP_TRAIL_LOC_RE.
        """
        # Blue State ')XX' shape: title closes with ')' then bare state code.
        blob_title = "Senior Software Engineer)NY"
        job = _make_job(blob_title, source="careers_page")
        with _clean_patches():
            result = ParsedJob.from_job(job)
        assert isinstance(result, UnresolvedParsedJob), (
            f"Expected UnresolvedParsedJob for {blob_title!r}, got {type(result).__name__}"
        )
        assert "title_metadata_blob" in result.unresolved_reasons
        assert result.raw_title == blob_title

    def test_static_tier_style_metadata_blob(self):
        """_static_tier.py extraction style: req-ID pipe pattern.

        Aggregator-style careers pages concatenate title + req-ID + location
        without separators; the _REQ_ID_PIPE_RE in is_metadata_blob catches
        the 'digits|TitleCase' shape (e.g. 'SQL2354308|Chennai, Tamil Nadu').
        Runs on raw_title before clean_title strips _REQID_PREFIX_RE patterns.
        """
        # Workday/aggregator req-ID pipe shape: digits run + pipe + city name.
        blob_title = "Senior Data Scientist - GenAI SQL2354308|Chennai, Tamil Nadu"
        job = _make_job(blob_title, source="workday")
        with _clean_patches():
            result = ParsedJob.from_job(job)
        assert isinstance(result, UnresolvedParsedJob), (
            f"Expected UnresolvedParsedJob for {blob_title!r}, got {type(result).__name__}"
        )
        assert "title_metadata_blob" in result.unresolved_reasons

    # -----------------------------------------------------------------------
    # Positive case: clean title must not be flagged
    # -----------------------------------------------------------------------

    def test_clean_title_returns_parsed_job(self):
        """A well-formed title produces a clean ParsedJob."""
        job = _make_job("Senior Software Engineer")
        with _clean_patches():
            result = ParsedJob.from_job(job)
        assert isinstance(result, ParsedJob)
        assert not isinstance(result, UnresolvedParsedJob)
        assert "title_metadata_blob" not in result.unresolved_reasons
        assert result.title == "Senior Software Engineer"

    def test_clean_title_with_parenthetical_passes(self):
        """Titles with legitimate parentheticals are not falsely flagged."""
        job = _make_job("Software Engineer (Backend)")
        with _clean_patches():
            result = ParsedJob.from_job(job)
        assert isinstance(result, ParsedJob)
        assert "title_metadata_blob" not in result.unresolved_reasons

    # -----------------------------------------------------------------------
    # clean_title normalisation
    # -----------------------------------------------------------------------

    def test_clean_title_strips_nosep_state_code(self):
        """clean_title removes no-separator trailing state codes from the stored title."""
        # "EngineerNY" shape — _NOSEP_TRAIL_LOC_RE strips "NY"
        job = _make_job("Senior Software EngineerNY")
        with _clean_patches():
            result = ParsedJob.from_job(job)
        # The blob flag should fire (I-08 sees raw_title ← "EngineerNY" matches)
        # but we also verify the stored title is cleaned.
        # "EngineerNY" → I-08 pattern 1 checks raw: ")NY" — but there's no ")",
        # so I-08 won't fire.  is_metadata_blob also won't fire (no markers).
        # clean_title strips "NY" via _NOSEP_TRAIL_LOC_RE.
        assert isinstance(result, ParsedJob)
        assert result.title == "Senior Software Engineer"

    def test_raw_title_preserved_on_unresolved(self):
        """UnresolvedParsedJob.raw_title carries the original pre-clean title."""
        blob_title = "Job TitlePrincipal EngineerPost levelP5"
        job = _make_job(blob_title)
        with _clean_patches():
            result = ParsedJob.from_job(job)
        assert isinstance(result, UnresolvedParsedJob)
        assert result.raw_title == blob_title


# ---------------------------------------------------------------------------
# Shim path: Job → upsert_job → UpsertResult.unresolved_reasons
# ---------------------------------------------------------------------------


class TestShimPathMetadataBlob:
    """Confirms title_metadata_blob propagates from ParsedJob.from_job through
    upsert_job to UpsertResult.unresolved_reasons (Phase 48.07: shim removed).
    """

    def test_from_job_upsert_metadata_blob_title(self):
        """Job with metadata-blob title → UpsertResult.unresolved_reasons includes 'title_metadata_blob'."""
        from job_finder.db import upsert_job
        from job_finder.web.db_migrate import run_migrations

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        run_migrations(path)

        try:
            # UNDP-style blob — caught by 'job title' + 'post level' + 'agency'
            # phrase markers in is_metadata_blob.
            blob_title = "Job TitlePrincipal EngineerPost levelP5Agency TestCo"
            job = _make_job(blob_title, source="careers_crawl")

            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            with (
                patch("job_finder.parsed_job.load_config", return_value={}),
                patch(
                    "job_finder.parsed_job.get_company_denylist",
                    return_value=frozenset(),
                ),
            ):
                parsed = ParsedJob.from_job(job)
                result = upsert_job(conn, parsed)
            conn.close()

            assert "title_metadata_blob" in result.unresolved_reasons, (
                f"Expected 'title_metadata_blob' in unresolved_reasons, "
                f"got: {result.unresolved_reasons}"
            )
        finally:
            if os.path.exists(path):
                os.remove(path)
