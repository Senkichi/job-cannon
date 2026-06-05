"""Phase 48.01 — Title filter universality tests.

Verifies that ``_is_metadata_blob`` + ``_TITLE_LOCATION_BLEED_RE`` (I-08)
run inside ``ParsedJob.from_job`` for every ingestion path, not only the
static tier that already filtered them before writing a Job.

Three fixture titles — one per documented callsite that historically bypassed
the filter (F-09 from the 2026-05-29 ingestion-contract audit):

    1. ai_nav_tier style — very long concatenated blob (> 140 chars).
       ``ai_career_navigator`` extracts titles from accessibility snapshots;
       those can merge title + req-ID + location + description without
       separators when the page's a11y tree is dense.

    2. careers_scraper style — title with an inline metadata marker.
       The ``careers_page`` low-tier path (careers_scraper.py :322/:602)
       extracts link text without passing it through ``_clean_title`` or
       ``_is_metadata_blob``.

    3. static_tier style — Blue State paren+state code (") NY" shape).
       The static tier's link path applies ``_is_metadata_blob`` early, but
       the JSON-LD pass above it does not. Any blob that slips through to
       ``ParsedJob.from_job`` must still be caught downstream.

All three yield ``UnresolvedParsedJob`` with ``'title_metadata_blob'`` in
``unresolved_reasons``.

One positive fixture (clean short title) verifies the non-blob path still
produces a clean ``ParsedJob``.

One shim-path test verifies that a ``Job`` with a blob title passed to
``upsert_job`` produces ``UpsertResult.unresolved_reasons`` containing
``'title_metadata_blob'`` — confirming the shim wires through correctly.
"""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from job_finder.db import upsert_job
from job_finder.models import Job
from job_finder.parsed_job import ParsedJob, UnresolvedParsedJob
from job_finder.web.db_migrate import run_migrations

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_job(
    title: str,
    company: str = "AcmeCo",
    location: str = "Remote",
    source: str = "careers_crawl",
    source_url: str = "https://acme.com/careers/1",
) -> Job:
    """Return a minimal valid Job instance with the given title."""
    return Job(
        title=title,
        company=company,
        location=location,
        source=source,
        source_url=source_url,
    )


def _clean_patches():
    """Context manager that disables I-10 denylist so other validators fire."""
    import contextlib

    @contextlib.contextmanager
    def ctx():
        with (
            patch("job_finder.parsed_job.load_config", return_value={}),
            patch("job_finder.parsed_job.get_company_denylist", return_value=frozenset()),
        ):
            yield

    return ctx()


# ---------------------------------------------------------------------------
# Fixture titles — one per callsite style
# ---------------------------------------------------------------------------

# 1. AI-nav tier style: very long concatenated blob (> 140 chars).
#    Represents what ai_career_navigator produces when the a11y snapshot
#    merges job title + req-ID + location + posting-date text.
_AI_NAV_BLOB_TITLE = (
    "Senior Data Scientist - GenAI & Machine Learning "
    "SQL2354308|Chennai, Tamil Nadu Hybrid Full Time "
    "Required 5+ years Python, Spark, Kafka and large-scale "
    "distributed systems experience"
)

# 2. careers_scraper / low-tier path style: marker phrase embedded in title.
#    The low-tier extraction at careers_scraper.py :322/:602 does not call
#    _clean_title or _is_metadata_blob, so "posted N days ago" survives into
#    the Job object.
_SCRAPER_BLOB_TITLE = "Tech Lead Analyst posted 10 days ago"

# 3. static-tier style: Blue State paren+state code shape.
#    The static tier's link-text path catches this via _is_metadata_blob;
#    the JSON-LD path does not. When it reaches ParsedJob.from_job, the
#    I-08 regex must catch it.
_STATIC_BLOB_TITLE = "Software Engineer) NY"

# Positive control: clean short title with no metadata markers.
_CLEAN_TITLE = "Staff Backend Engineer"


# ---------------------------------------------------------------------------
# TestTitleFilterUniversal — ParsedJob.from_job enforces for every caller
# ---------------------------------------------------------------------------


class TestTitleFilterUniversal:
    """ParsedJob.from_job runs is_metadata_blob + I-08 for every ingestion path."""

    def test_ai_nav_style_blob_returns_unresolved(self):
        """AI-nav tier title blob (>140 chars) → UnresolvedParsedJob."""
        assert len(_AI_NAV_BLOB_TITLE) > 140, "fixture must exceed _MAX_TITLE_LEN threshold"
        job = _make_job(title=_AI_NAV_BLOB_TITLE)
        with _clean_patches():
            result = ParsedJob.from_job(job)
        assert isinstance(result, UnresolvedParsedJob), (
            f"Expected UnresolvedParsedJob, got {type(result).__name__}"
        )
        assert "title_metadata_blob" in result.unresolved_reasons

    def test_scraper_style_blob_returns_unresolved(self):
        """careers_scraper title with 'posted N days ago' marker → UnresolvedParsedJob."""
        assert "posted " in _SCRAPER_BLOB_TITLE.lower(), (
            "fixture must contain 'posted ' marker so _is_metadata_blob fires"
        )
        job = _make_job(title=_SCRAPER_BLOB_TITLE)
        with _clean_patches():
            result = ParsedJob.from_job(job)
        assert isinstance(result, UnresolvedParsedJob), (
            f"Expected UnresolvedParsedJob, got {type(result).__name__}"
        )
        assert "title_metadata_blob" in result.unresolved_reasons

    def test_static_tier_style_blob_returns_unresolved(self):
        """Blue State paren+state code (') NY') → UnresolvedParsedJob via I-08."""
        assert ")" in _STATIC_BLOB_TITLE, "fixture must contain paren-close shape"
        job = _make_job(title=_STATIC_BLOB_TITLE)
        with _clean_patches():
            result = ParsedJob.from_job(job)
        assert isinstance(result, UnresolvedParsedJob), (
            f"Expected UnresolvedParsedJob, got {type(result).__name__}"
        )
        assert "title_metadata_blob" in result.unresolved_reasons

    def test_clean_title_returns_parsed_job(self):
        """A clean short title → ParsedJob with no title_metadata_blob reason."""
        job = _make_job(title=_CLEAN_TITLE)
        with _clean_patches():
            result = ParsedJob.from_job(job)
        assert isinstance(result, ParsedJob), f"Expected ParsedJob, got {type(result).__name__}"
        assert not isinstance(result, UnresolvedParsedJob)
        assert "title_metadata_blob" not in result.unresolved_reasons

    def test_raw_title_preserved_on_blob(self):
        """UnresolvedParsedJob.raw_title holds the original pre-clean title."""
        job = _make_job(title=_SCRAPER_BLOB_TITLE)
        with _clean_patches():
            result = ParsedJob.from_job(job)
        assert isinstance(result, UnresolvedParsedJob)
        assert result.raw_title == _SCRAPER_BLOB_TITLE

    def test_all_three_callsite_fixtures_trigger_title_metadata_blob(self):
        """Smoke: each callsite fixture yields 'title_metadata_blob'."""
        for label, title in [
            ("ai_nav", _AI_NAV_BLOB_TITLE),
            ("scraper", _SCRAPER_BLOB_TITLE),
            ("static_tier", _STATIC_BLOB_TITLE),
        ]:
            job = _make_job(title=title)
            with _clean_patches():
                result = ParsedJob.from_job(job)
            assert isinstance(result, UnresolvedParsedJob), (
                f"[{label}] Expected UnresolvedParsedJob for title={title!r}"
            )
            assert "title_metadata_blob" in result.unresolved_reasons, (
                f"[{label}] 'title_metadata_blob' missing from {result.unresolved_reasons}"
            )


# ---------------------------------------------------------------------------
# TestShimPath — upsert_job(conn, Job) wires through ParsedJob.from_job
# ---------------------------------------------------------------------------


@pytest.fixture()
def migrated_conn() -> Iterator[sqlite3.Connection]:
    """Temp DB with all migrations applied, for upsert_job shim tests."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")  # noqa: SIM115 — delete=False path reused; closed below, unlinked in finally
    tmp.close()
    path = Path(tmp.name)
    try:
        run_migrations(str(path))
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        yield conn
        conn.close()
    finally:
        path.unlink(missing_ok=True)


class TestShimPath:
    """upsert_job(conn, Job) shim routes through ParsedJob.from_job.

    Verifies that the 47.02 shim (Job → ParsedJob.from_job internal
    conversion) propagates title_metadata_blob through to UpsertResult.
    """

    def test_blob_title_via_upsert_job_produces_unresolved_reasons(
        self, migrated_conn: sqlite3.Connection
    ) -> None:
        """Job with metadata-blob title → UpsertResult.unresolved_reasons has 'title_metadata_blob'."""
        job = _make_job(title=_AI_NAV_BLOB_TITLE, company="ShimBlobCo")
        with (
            patch("job_finder.parsed_job.load_config", return_value={}),
            patch("job_finder.parsed_job.get_company_denylist", return_value=frozenset()),
            patch("job_finder.config.get_company_denylist", return_value=frozenset()),
            patch("job_finder.config.load_config", return_value={}),
        ):
            result = upsert_job(migrated_conn, job)
        assert "title_metadata_blob" in result.unresolved_reasons, (
            f"Expected 'title_metadata_blob' in unresolved_reasons, "
            f"got: {result.unresolved_reasons}"
        )

    def test_clean_title_via_upsert_job_has_empty_unresolved_reasons(
        self, migrated_conn: sqlite3.Connection
    ) -> None:
        """Job with clean title → UpsertResult.unresolved_reasons is empty."""
        job = _make_job(title=_CLEAN_TITLE, company="ShimCleanCo")
        with (
            patch("job_finder.parsed_job.load_config", return_value={}),
            patch("job_finder.parsed_job.get_company_denylist", return_value=frozenset()),
            patch("job_finder.config.get_company_denylist", return_value=frozenset()),
            patch("job_finder.config.load_config", return_value={}),
        ):
            result = upsert_job(migrated_conn, job)
        assert result.unresolved_reasons == [], (
            f"Expected empty unresolved_reasons for clean title, got: {result.unresolved_reasons}"
        )
