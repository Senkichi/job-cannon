"""Tests for static-first fallthrough in ats_prober.py (issue #565).

Tests the cheap→expensive ordering:
1. Re-detect known ATS on subdomain
2. Static HTML extract (L1/L4)
3. Embedded-JSON tier (Tier 2.5)
4. Playwright tier (most expensive)

Also tests that Playwright is NOT invoked when earlier tiers succeed.
"""

import os
import sqlite3
import tempfile
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from job_finder.web.ats_prober import _try_static_first_fallthrough, probe_single_company
from job_finder.web.db_migrate import run_migrations


@pytest.fixture
def migrated_db_path():
    """Create a fully migrated temp DB, yield path only, clean up after."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    run_migrations(path)
    yield path
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture
def db_conn(migrated_db_path):
    """Open a connection to the migrated DB, yield (path, conn), close after."""
    conn = sqlite3.connect(migrated_db_path)
    conn.row_factory = sqlite3.Row
    yield migrated_db_path, conn
    conn.close()


def _insert_company(
    conn, name, careers_url, ats_platform=None, ats_slug=None, ats_probe_status="miss"
):
    """Helper: insert a company for testing."""
    now = datetime.now(UTC).isoformat()
    cursor = conn.execute(
        """INSERT INTO companies (name, name_raw, careers_url, ats_platform, ats_slug, ats_probe_status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (name, name, careers_url, ats_platform, ats_slug, ats_probe_status, now, now),
    )
    return cursor.lastrowid


class TestStaticFirstFallthroughOrdering:
    """Test that the fallthrough respects cheap→expensive ordering."""

    def test_static_extract_succeeds_before_playwright(self, db_conn):
        """Test that static extraction succeeds and Playwright is NOT invoked."""
        db_path, conn = db_conn
        company_id = _insert_company(conn, "TestCompany", "https://example.com/careers")
        config = {
            "TESTING": True,
            "profile": {"target_titles": ["Engineer"], "exclusions": {"title_keywords": []}},
        }
        now = datetime.now(UTC).isoformat()

        # Mock static extraction to return jobs
        with patch(
            "job_finder.web.careers_crawler._static_tier._try_static_extract"
        ) as mock_static:
            mock_static.return_value = [
                {"title": "Software Engineer", "url": "https://example.com/job1"}
            ]

            # Mock Playwright to track if it was called
            with patch(
                "job_finder.web.careers_crawler._playwright_tier._try_playwright_extract"
            ) as mock_playwright:
                result = _try_static_first_fallthrough(
                    company_id, "TestCompany", "https://example.com/careers", conn, config, now
                )

                # Static extraction should have been called
                assert mock_static.called
                # Playwright should NOT have been called (static succeeded)
                assert not mock_playwright.called
                # Result should be a hit
                assert result["status"] == "hit"
                assert result["source"] == "static_extract"

        # Verify DB state
        company = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
        assert company["ats_probe_status"] == "hit"
        assert company["scan_enabled"] == 1
        assert company["miss_reason"] is None

    def test_embedded_json_tried_after_static_fails(self, db_conn):
        """Test that embedded-JSON is tried when static extraction fails."""
        db_path, conn = db_conn
        company_id = _insert_company(conn, "TestCompany", "https://example.com/careers")
        config = {
            "TESTING": True,
            "profile": {"target_titles": ["Engineer"], "exclusions": {"title_keywords": []}},
        }
        now = datetime.now(UTC).isoformat()

        # Mock static extraction to return None (JS-heavy page)
        with patch(
            "job_finder.web.careers_crawler._static_tier._try_static_extract"
        ) as mock_static:
            mock_static.return_value = None

            # Mock embedded-JSON to return jobs
            with patch(
                "job_finder.web.careers_crawler._embedded_json_tier._try_embedded_json_extract"
            ) as mock_json:
                mock_json.return_value = [
                    {"title": "Software Engineer", "url": "https://example.com/job1"}
                ]

                # Mock Playwright to track if it was called
                with patch(
                    "job_finder.web.careers_crawler._playwright_tier._try_playwright_extract"
                ) as mock_playwright:
                    result = _try_static_first_fallthrough(
                        company_id, "TestCompany", "https://example.com/careers", conn, config, now
                    )

                    # Static extraction should have been called
                    assert mock_static.called
                    # Embedded-JSON should have been called
                    assert mock_json.called
                    # Playwright should NOT have been called (embedded-JSON succeeded)
                    assert not mock_playwright.called
                    # Result should be a hit
                    assert result["status"] == "hit"
                    assert result["source"] == "embedded_json"

    def test_ats_redetect_on_careers_url(self, db_conn):
        """Test that ATS detection on careers_url is tried first."""
        db_path, conn = db_conn
        company_id = _insert_company(
            conn, "TestCompany", "https://boards.greenhouse.io/testcompany/jobs"
        )
        config = {
            "TESTING": True,
            "profile": {"target_titles": ["Engineer"], "exclusions": {"title_keywords": []}},
        }
        now = datetime.now(UTC).isoformat()

        # Mock ATS detection to find Greenhouse
        with patch("job_finder.web.ats_prober.extract_ats_from_url_best") as mock_detect:
            mock_detect.return_value = ("greenhouse", "testcompany", 5)

            # Mock promote_from_careers_link to succeed (patch at the actual import location)
            with patch(
                "job_finder.web.ats_identity_reconcile.promote_from_careers_link"
            ) as mock_promote:
                mock_promote.return_value = {"outcome": "promoted"}

                # Mock static extraction to track if it was called
                with patch(
                    "job_finder.web.careers_crawler._static_tier._try_static_extract"
                ) as mock_static:
                    result = _try_static_first_fallthrough(
                        company_id,
                        "TestCompany",
                        "https://boards.greenhouse.io/testcompany/jobs",
                        conn,
                        config,
                        now,
                    )

                    # ATS detection should have been called
                    assert mock_detect.called
                    # Promotion should have been called
                    assert mock_promote.called
                    # Static extraction should NOT have been called (ATS detected)
                    assert not mock_static.called
                    # Result should be a hit
                    assert result["status"] == "hit"
                    assert result["source"] == "ats_redetect_careers_url"

    def test_static_no_matches_sets_specific_reason(self, db_conn):
        """Test that static extraction with no matches sets specific miss_reason."""
        db_path, conn = db_conn
        company_id = _insert_company(conn, "TestCompany", "https://example.com/careers")
        config = {
            "TESTING": True,
            "profile": {"target_titles": ["Engineer"], "exclusions": {"title_keywords": []}},
        }
        now = datetime.now(UTC).isoformat()

        # Mock static extraction to return empty list (statically rendered but no matches)
        with patch(
            "job_finder.web.careers_crawler._static_tier._try_static_extract"
        ) as mock_static:
            mock_static.return_value = []

            result = _try_static_first_fallthrough(
                company_id, "TestCompany", "https://example.com/careers", conn, config, now
            )

            # Result should be a miss
            assert result["status"] == "miss"
            assert result["reason"] == "static_no_matches"

        # Verify DB state
        company = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
        assert company["ats_probe_status"] == "miss"
        assert company["miss_reason"] == "static_no_matches"

    def test_playwright_no_matches_sets_specific_reason(self, db_conn):
        """Test that Playwright with no matches sets specific miss_reason."""
        db_path, conn = db_conn
        company_id = _insert_company(conn, "TestCompany", "https://example.com/careers")
        config = {
            "TESTING": True,
            "profile": {"target_titles": ["Engineer"], "exclusions": {"title_keywords": []}},
        }
        now = datetime.now(UTC).isoformat()

        # Mock all tiers to fail
        with patch("job_finder.web.ats_detection.extract_ats_from_url_best") as mock_detect:
            mock_detect.return_value = None

            with patch(
                "job_finder.web.careers_crawler._static_tier._try_static_extract"
            ) as mock_static:
                mock_static.return_value = None

            with patch(
                "job_finder.web.careers_crawler._embedded_json_tier._try_embedded_json_extract"
            ) as mock_json:
                mock_json.return_value = None

            # Mock Playwright tier to return empty list (tier runs but finds no jobs)
            with patch(
                "job_finder.web.careers_crawler._playwright_tier._try_playwright_extract"
            ) as mock_pw_extract:
                mock_pw_extract.return_value = []

                result = _try_static_first_fallthrough(
                    company_id, "TestCompany", "https://example.com/careers", conn, config, now
                )

                # Result should be a miss
                assert result["status"] == "miss"
                assert result["reason"] == "playwright_no_matches"

        # Verify DB state
        company = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
        assert company["ats_probe_status"] == "miss"
        assert company["miss_reason"] == "playwright_no_matches"


class TestProbeSingleCompanyFallthrough:
    """Test that probe_single_company routes to fallthrough appropriately."""

    def test_speculative_probing_exhausted_routes_to_fallthrough(self, db_conn):
        """Test that speculative probing exhaustion routes to fallthrough when careers_url exists."""
        db_path, conn = db_conn
        company_id = _insert_company(conn, "TestCompany", "https://example.com/careers")
        config = {
            "TESTING": True,
            "profile": {"target_titles": ["Engineer"], "exclusions": {"title_keywords": []}},
        }

        # Mock all speculative probes to fail
        with (
            patch("job_finder.web.ats_prober._probe_lever_with_result", return_value=False),
            patch("job_finder.web.ats_prober._probe_greenhouse", return_value=False),
            patch("job_finder.web.ats_prober._probe_ashby", return_value=False),
            patch("job_finder.web.ats_prober._try_static_first_fallthrough") as mock_fallthrough,
        ):
            mock_fallthrough.return_value = {"status": "hit", "source": "static_extract"}

            result = probe_single_company(company_id, conn, config)

            # Fallthrough should have been called
            assert mock_fallthrough.called
            # Result should reflect fallthrough success
            assert result["status"] == "hit"

    def test_speculative_probing_no_careers_url_sets_specific_reason(self, db_conn):
        """Test that companies without careers_url get specific miss_reason."""
        db_path, conn = db_conn
        company_id = _insert_company(conn, "TestCompany", None)  # No careers_url
        config = {
            "TESTING": True,
            "profile": {"target_titles": ["Engineer"], "exclusions": {"title_keywords": []}},
        }

        # Mock all speculative probes to fail
        with (
            patch("job_finder.web.ats_prober._probe_lever_with_result", return_value=False),
            patch("job_finder.web.ats_prober._probe_greenhouse", return_value=False),
            patch("job_finder.web.ats_prober._probe_ashby", return_value=False),
            patch("job_finder.web.ats_prober._try_static_first_fallthrough") as mock_fallthrough,
        ):
            # Fallthrough should NOT be called (no careers_url)
            result = probe_single_company(company_id, conn, config)

            assert not mock_fallthrough.called
            # Result should be a miss with specific reason
            assert result["status"] == "miss"
            assert result["reason"] == "speculative_probing_exhausted_no_careers_url"

        # Verify DB state
        company = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
        assert company["ats_probe_status"] == "miss"
        assert company["miss_reason"] == "speculative_probing_exhausted_no_careers_url"
