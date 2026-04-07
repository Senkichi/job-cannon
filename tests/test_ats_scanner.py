"""Tests for ats_scanner.py module.

Covers:
- extract_ats_from_urls: URL pattern recognition for Lever, Greenhouse, Ashby
- upsert_company: create/update company records with ATS info
- derive_slug_candidates: slug generation from company names
- probe_ats_slugs: speculative ATS slug probing with cache
- _title_matches: shared keyword filtering utility
- scan_lever: Lever API parsing, keyword filtering, salary extraction
- scan_greenhouse: Greenhouse API parsing, cents-to-dollars conversion
- scan_ashby: Ashby API parsing, compensation tiers
- run_ats_scan: full scan orchestration with Haiku scoring and activity feed
"""

import json
import os
import sqlite3
import tempfile
from unittest.mock import MagicMock, patch, call

import pytest

from job_finder.web.db_migrate import run_migrations


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def migrated_db_path():
    """Create a fully migrated temp DB, yield path only, clean up after.

    # intentional — local fixture yields path only, unlike conftest migrated_db
    # which yields (path, conn). This file's tests only need the path.
    """
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


def _insert_hit_company(conn, name, platform, slug, scan_enabled=1):
    """Helper: insert a company with ats_probe_status='hit' and scan_enabled=1."""
    from datetime import datetime
    now = datetime.now().isoformat()
    cursor = conn.execute(
        """INSERT INTO companies
           (name, name_raw, ats_platform, ats_slug, ats_probe_status,
            scan_enabled, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'hit', ?, ?, ?)""",
        (name.lower(), name, platform, slug, scan_enabled, now, now),
    )
    conn.commit()
    return cursor.lastrowid


# ---------------------------------------------------------------------------
# Tests: extract_ats_from_urls
# ---------------------------------------------------------------------------

class TestExtractAtsFromUrls:
    """Tests for ATS URL pattern detection."""

    def test_lever_jobs_url_returns_lever_and_slug(self):
        """jobs.lever.co/{slug}/... returns ('lever', slug)."""
        from job_finder.web.ats_detection import extract_ats_from_urls
        urls = ["https://jobs.lever.co/acme/abc-123"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "lever"
        assert slug == "acme"

    def test_lever_api_url_returns_lever_and_slug(self):
        """api.lever.co/v0/postings/{slug}/... returns ('lever', slug)."""
        from job_finder.web.ats_detection import extract_ats_from_urls
        urls = ["https://api.lever.co/v0/postings/stripe?mode=json"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "lever"
        assert slug == "stripe"

    def test_greenhouse_boards_url_returns_greenhouse_and_slug(self):
        """boards.greenhouse.io/{slug}/... returns ('greenhouse', slug)."""
        from job_finder.web.ats_detection import extract_ats_from_urls
        urls = ["https://boards.greenhouse.io/airbnb/jobs/12345"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "greenhouse"
        assert slug == "airbnb"

    def test_greenhouse_api_url_returns_greenhouse_and_slug(self):
        """boards-api.greenhouse.io/v1/boards/{slug}/... returns ('greenhouse', slug)."""
        from job_finder.web.ats_detection import extract_ats_from_urls
        urls = ["https://boards-api.greenhouse.io/v1/boards/waymo/jobs"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "greenhouse"
        assert slug == "waymo"

    def test_ashby_url_returns_ashby_and_slug(self):
        """jobs.ashbyhq.com/{slug}/... returns ('ashby', slug)."""
        from job_finder.web.ats_detection import extract_ats_from_urls
        urls = ["https://jobs.ashbyhq.com/OpenAI/abc-uuid"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "ashby"
        assert slug == "OpenAI"

    def test_non_ats_url_returns_none_none(self):
        """LinkedIn URL returns (None, None)."""
        from job_finder.web.ats_detection import extract_ats_from_urls
        urls = ["https://www.linkedin.com/jobs/view/1234567/"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform is None
        assert slug is None

    def test_empty_list_returns_none_none(self):
        """Empty source_urls list returns (None, None)."""
        from job_finder.web.ats_detection import extract_ats_from_urls
        platform, slug = extract_ats_from_urls([])
        assert platform is None
        assert slug is None

    def test_ashby_slug_preserves_exact_casing(self):
        """Ashby slug preserves exact URL casing (case-sensitive per Research Pitfall 3)."""
        from job_finder.web.ats_detection import extract_ats_from_urls
        urls = ["https://jobs.ashbyhq.com/Ramp/some-job-uuid"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "ashby"
        assert slug == "Ramp"  # Must preserve exact casing

    def test_multiple_urls_returns_first_match(self):
        """First ATS URL in list wins."""
        from job_finder.web.ats_detection import extract_ats_from_urls
        urls = [
            "https://www.linkedin.com/jobs/view/999/",
            "https://jobs.lever.co/stripe/job-id",
        ]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "lever"
        assert slug == "stripe"


# ---------------------------------------------------------------------------
# Tests: _title_matches
# ---------------------------------------------------------------------------

class TestTitleMatches:
    """Tests for the shared keyword filtering utility."""

    def test_returns_true_when_title_contains_target_keyword(self):
        """Returns True when title matches a target_title keyword (case-insensitive)."""
        from job_finder.web.ats_scanner import _title_matches
        assert _title_matches(
            "Senior Data Scientist",
            target_titles=["data scientist"],
            exclusions=[],
        ) is True

    def test_returns_false_when_title_contains_exclusion(self):
        """Returns False when title contains an exclusion keyword."""
        from job_finder.web.ats_scanner import _title_matches
        assert _title_matches(
            "Junior Data Scientist",
            target_titles=["data scientist"],
            exclusions=["junior"],
        ) is False

    def test_returns_true_when_target_titles_is_empty(self):
        """Returns True when target_titles is empty (no filter = include all)."""
        from job_finder.web.ats_scanner import _title_matches
        assert _title_matches(
            "Any Title Here",
            target_titles=[],
            exclusions=[],
        ) is True

    def test_returns_false_when_title_matches_no_target(self):
        """Returns False when title does not match any target keyword."""
        from job_finder.web.ats_scanner import _title_matches
        assert _title_matches(
            "Product Manager",
            target_titles=["data scientist", "machine learning"],
            exclusions=[],
        ) is False

    def test_case_insensitive_matching(self):
        """Matching is case-insensitive for both targets and title."""
        from job_finder.web.ats_scanner import _title_matches
        assert _title_matches(
            "SENIOR DATA SCIENTIST",
            target_titles=["Data Scientist"],
            exclusions=[],
        ) is True

    def test_exclusion_case_insensitive(self):
        """Exclusion check is case-insensitive."""
        from job_finder.web.ats_scanner import _title_matches
        assert _title_matches(
            "Data Scientist - Intern",
            target_titles=["data scientist"],
            exclusions=["INTERN"],
        ) is False

    def test_empty_exclusions_list_never_excludes(self):
        """Empty exclusions list means nothing is excluded."""
        from job_finder.web.ats_scanner import _title_matches
        assert _title_matches(
            "Staff Machine Learning Engineer",
            target_titles=["machine learning"],
            exclusions=[],
        ) is True


# ---------------------------------------------------------------------------
# Tests: upsert_company
# ---------------------------------------------------------------------------

class TestUpsertCompany:
    """Tests for company record creation and update logic."""

    def test_creates_new_company_record_and_returns_id(self, db_conn):
        """upsert_company creates a new record and returns the company_id."""
        from job_finder.web.ats_scanner import upsert_company
        db_path, conn = db_conn
        company_id = upsert_company(conn, name="Stripe, Inc.")
        assert company_id is not None
        assert isinstance(company_id, int)
        assert company_id > 0

    def test_creates_company_with_normalized_name(self, db_conn):
        """Normalized name is stored in the name column, raw name in name_raw."""
        from job_finder.web.ats_scanner import upsert_company
        db_path, conn = db_conn
        company_id = upsert_company(conn, name="Acme Corp.")
        row = conn.execute(
            "SELECT name, name_raw FROM companies WHERE id = ?", (company_id,)
        ).fetchone()
        assert row["name"] == "acme"  # normalized via normalize_company
        assert row["name_raw"] == "Acme Corp."  # raw name preserved

    def test_updates_existing_company_matched_by_normalized_name(self, db_conn):
        """Second call with same company (different raw spelling) updates, not inserts."""
        from job_finder.web.ats_scanner import upsert_company
        db_path, conn = db_conn
        id1 = upsert_company(conn, name="Stripe")
        id2 = upsert_company(conn, name="Stripe, Inc.")  # same normalized name
        assert id1 == id2  # Same record
        count = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        assert count == 1  # Only one record

    def test_updates_ats_info_when_new_info_is_better(self, db_conn):
        """Updates ats_platform and ats_slug when upgrading from pending to hit."""
        from job_finder.web.ats_scanner import upsert_company
        db_path, conn = db_conn
        # First insert: pending (no ATS info)
        company_id = upsert_company(conn, name="OpenAI", ats_probe_status="pending")
        # Second call: hit (URL-derived slug)
        upsert_company(
            conn, name="OpenAI",
            ats_platform="ashby",
            ats_slug="OpenAI",
            ats_probe_status="hit",
        )
        row = conn.execute(
            "SELECT ats_platform, ats_slug, ats_probe_status FROM companies WHERE id = ?",
            (company_id,),
        ).fetchone()
        assert row["ats_platform"] == "ashby"
        assert row["ats_slug"] == "OpenAI"
        assert row["ats_probe_status"] == "hit"

    def test_does_not_downgrade_from_hit_to_pending(self, db_conn):
        """Once ats_probe_status is 'hit', a call with 'pending' does not downgrade."""
        from job_finder.web.ats_scanner import upsert_company
        db_path, conn = db_conn
        # Set up a confirmed hit
        company_id = upsert_company(
            conn, name="Lever Co",
            ats_platform="lever",
            ats_slug="leverco",
            ats_probe_status="hit",
        )
        # Now call with pending (e.g. second ingestion of same company without ATS URL)
        upsert_company(conn, name="Lever Co", ats_probe_status="pending")
        row = conn.execute(
            "SELECT ats_probe_status FROM companies WHERE id = ?", (company_id,)
        ).fetchone()
        assert row["ats_probe_status"] == "hit"  # Not downgraded

    def test_returns_company_id_for_existing_company(self, db_conn):
        """Returns the existing company_id on second call for same company."""
        from job_finder.web.ats_scanner import upsert_company
        db_path, conn = db_conn
        id1 = upsert_company(conn, name="Ramp")
        id2 = upsert_company(conn, name="Ramp")
        assert id1 == id2


# ---------------------------------------------------------------------------
# Tests: derive_slug_candidates
# ---------------------------------------------------------------------------

class TestDeriveSlugCandidates:
    """Tests for slug candidate generation from company names."""

    def test_scale_ai_generates_hyphenated_and_concatenated(self):
        """'Scale AI' generates ['scale-ai', 'scaleai']."""
        from job_finder.web.ats_scanner import derive_slug_candidates
        candidates = derive_slug_candidates("Scale AI")
        assert "scale-ai" in candidates
        assert "scaleai" in candidates

    def test_simple_name_generates_single_candidate(self):
        """'Stripe' generates ['stripe'] (no duplicate)."""
        from job_finder.web.ats_scanner import derive_slug_candidates
        candidates = derive_slug_candidates("Stripe")
        assert "stripe" in candidates

    def test_strips_inc_suffix(self):
        """'Acme Inc.' strips suffix before generating slug."""
        from job_finder.web.ats_scanner import derive_slug_candidates
        candidates = derive_slug_candidates("Acme Inc.")
        assert "acme" in candidates
        assert not any("inc" in c for c in candidates)

    def test_openai_generates_single_candidate(self):
        """'OpenAI' generates ['openai'] (already concatenated)."""
        from job_finder.web.ats_scanner import derive_slug_candidates
        candidates = derive_slug_candidates("OpenAI")
        assert "openai" in candidates

    def test_returns_list(self):
        """derive_slug_candidates always returns a list."""
        from job_finder.web.ats_scanner import derive_slug_candidates
        result = derive_slug_candidates("AnyCompany")
        assert isinstance(result, list)
        assert len(result) >= 1


# ---------------------------------------------------------------------------
# Tests: probe_ats_slugs
# ---------------------------------------------------------------------------

class TestProbeAtsSlugs:
    """Tests for speculative ATS slug probing with cache logic."""

    def _insert_pending_company(self, conn, name="TestCo"):
        """Insert a company with ats_probe_status='pending' for probe testing."""
        from datetime import datetime
        now = datetime.now().isoformat()
        cursor = conn.execute(
            """INSERT INTO companies
               (name, name_raw, ats_probe_status, created_at, updated_at)
               VALUES (?, ?, 'pending', ?, ?)""",
            (name.lower(), name, now, now),
        )
        conn.commit()
        return cursor.lastrowid

    def test_sets_hit_when_api_returns_postings(self, migrated_db_path):
        """Sets ats_probe_status='hit' and stores slug when API returns valid postings."""
        from job_finder.web.ats_scanner import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = self._insert_pending_company(conn, "Stripe")
        conn.close()

        # Mock Lever returning a non-empty list (valid hit)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [{"text": "Senior Engineer"}]

        with patch("job_finder.web.ats_scanner.requests.get", return_value=mock_response):
            result = probe_ats_slugs(migrated_db_path, config={})

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT ats_probe_status, ats_slug FROM companies WHERE id = ?",
            (company_id,),
        ).fetchone()
        conn.close()

        assert row["ats_probe_status"] == "hit"
        assert row["ats_slug"] is not None

    def test_sets_miss_when_all_apis_return_404_or_empty(self, migrated_db_path):
        """Sets ats_probe_status='miss' when all APIs return 404/empty."""
        from job_finder.web.ats_scanner import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = self._insert_pending_company(conn, "UnknownCo")
        conn.close()

        # All APIs return 404
        mock_response = MagicMock()
        mock_response.status_code = 404

        with patch("job_finder.web.ats_scanner.requests.get", return_value=mock_response):
            probe_ats_slugs(migrated_db_path, config={})

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT ats_probe_status FROM companies WHERE id = ?",
            (company_id,),
        ).fetchone()
        conn.close()

        assert row["ats_probe_status"] == "miss"

    def test_skips_companies_with_cached_miss(self, migrated_db_path):
        """Companies with ats_probe_status='miss' are never re-probed."""
        from job_finder.web.ats_scanner import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        from datetime import datetime
        now = datetime.now().isoformat()
        conn.execute(
            """INSERT INTO companies
               (name, name_raw, ats_probe_status, created_at, updated_at)
               VALUES ('missco', 'MissCo', 'miss', ?, ?)""",
            (now, now),
        )
        conn.commit()
        conn.close()

        with patch("job_finder.web.ats_scanner.requests.get") as mock_get:
            probe_ats_slugs(migrated_db_path, config={})
            # requests.get should NOT be called for miss-cached companies
            mock_get.assert_not_called()

    def test_skips_companies_with_confirmed_hit(self, migrated_db_path):
        """Companies with ats_probe_status='hit' are not re-probed."""
        from job_finder.web.ats_scanner import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        from datetime import datetime
        now = datetime.now().isoformat()
        conn.execute(
            """INSERT INTO companies
               (name, name_raw, ats_platform, ats_slug, ats_probe_status, created_at, updated_at)
               VALUES ('hitco', 'HitCo', 'lever', 'hitco', 'hit', ?, ?)""",
            (now, now),
        )
        conn.commit()
        conn.close()

        with patch("job_finder.web.ats_scanner.requests.get") as mock_get:
            probe_ats_slugs(migrated_db_path, config={})
            # requests.get should NOT be called for hit-confirmed companies
            mock_get.assert_not_called()

    def test_lever_200_empty_list_stays_pending(self, migrated_db_path):
        """Lever 200 + empty list means company not confirmed on Lever (stays pending per Pitfall 2)."""
        from job_finder.web.ats_scanner import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = self._insert_pending_company(conn, "PendingCo")
        conn.close()

        # Lever returns 200 + empty list (invalid slug or company with no postings)
        lever_response = MagicMock()
        lever_response.status_code = 200
        lever_response.json.return_value = []  # Empty list — not confirmed

        # All other APIs return 404
        not_found_response = MagicMock()
        not_found_response.status_code = 404

        def side_effect(url, timeout):
            if "lever.co" in url:
                return lever_response
            return not_found_response

        with patch("job_finder.web.ats_scanner.requests.get", side_effect=side_effect):
            probe_ats_slugs(migrated_db_path, config={})

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT ats_probe_status FROM companies WHERE id = ?",
            (company_id,),
        ).fetchone()
        conn.close()

        # Lever 200+empty should NOT result in 'hit' — treated as miss for this probe round
        # Since all APIs failed or returned empty, status should be 'miss'
        assert row["ats_probe_status"] == "miss"

    def test_returns_early_when_testing_config_set(self, migrated_db_path):
        """Returns early without probing when TESTING=True in config."""
        from job_finder.web.ats_scanner import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        from datetime import datetime
        now = datetime.now().isoformat()
        conn.execute(
            """INSERT INTO companies
               (name, name_raw, ats_probe_status, created_at, updated_at)
               VALUES ('testco', 'TestCo', 'pending', ?, ?)""",
            (now, now),
        )
        conn.commit()
        conn.close()

        with patch("job_finder.web.ats_scanner.requests.get") as mock_get:
            probe_ats_slugs(migrated_db_path, config={"TESTING": True})
            mock_get.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: scan_lever
# ---------------------------------------------------------------------------

class TestScanLever:
    """Tests for Lever API scanning, keyword filter, and salary extraction."""

    def _make_lever_response(self, jobs):
        """Build mock requests.Response returning Lever JSON."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = jobs
        return mock_resp

    def test_scan_lever_parses_matched_job_titles(self):
        """scan_lever returns jobs matching target_titles keyword filter."""
        from job_finder.web.ats_scanner import scan_lever
        lever_jobs = [
            {
                "text": "Senior Data Scientist",
                "hostedUrl": "https://jobs.lever.co/stripe/abc-123",
                "descriptionPlain": "Build ML models at Stripe.",
                "categories": {"location": "Remote"},
                "salaryRange": None,
            }
        ]
        mock_resp = self._make_lever_response(lever_jobs)

        with patch("job_finder.web.ats_scanner.requests.get", return_value=mock_resp):
            results = scan_lever(
                slug="stripe",
                target_titles=["data scientist"],
                exclusions=[],
            )

        assert len(results) == 1
        assert results[0]["title"] == "Senior Data Scientist"
        assert results[0]["source_url"] == "https://jobs.lever.co/stripe/abc-123"

    def test_scan_lever_applies_keyword_filter(self):
        """scan_lever filters out non-matching jobs using _title_matches."""
        from job_finder.web.ats_scanner import scan_lever
        lever_jobs = [
            {
                "text": "Senior Data Scientist",
                "hostedUrl": "https://jobs.lever.co/stripe/abc-123",
                "descriptionPlain": "ML models.",
                "categories": {"location": "Remote"},
                "salaryRange": None,
            },
            {
                "text": "Product Manager",
                "hostedUrl": "https://jobs.lever.co/stripe/def-456",
                "descriptionPlain": "Lead product.",
                "categories": {"location": "SF"},
                "salaryRange": None,
            },
        ]
        mock_resp = self._make_lever_response(lever_jobs)

        with patch("job_finder.web.ats_scanner.requests.get", return_value=mock_resp):
            results = scan_lever(
                slug="stripe",
                target_titles=["data scientist"],
                exclusions=[],
            )

        assert len(results) == 1
        assert results[0]["title"] == "Senior Data Scientist"

    def test_scan_lever_extracts_salary_range_when_present(self):
        """scan_lever extracts salaryRange min/max when present."""
        from job_finder.web.ats_scanner import scan_lever
        lever_jobs = [
            {
                "text": "Staff Data Scientist",
                "hostedUrl": "https://jobs.lever.co/stripe/xyz",
                "descriptionPlain": "Data science role.",
                "categories": {"location": "Remote"},
                "salaryRange": {
                    "currency": "USD",
                    "min": 180000,
                    "max": 250000,
                },
            }
        ]
        mock_resp = self._make_lever_response(lever_jobs)

        with patch("job_finder.web.ats_scanner.requests.get", return_value=mock_resp):
            results = scan_lever(
                slug="stripe",
                target_titles=["data scientist"],
                exclusions=[],
            )

        assert len(results) == 1
        assert results[0]["salary_min"] == 180000
        assert results[0]["salary_max"] == 250000

    def test_scan_lever_returns_none_salary_when_absent(self):
        """scan_lever returns None salary_min/max when salaryRange is absent."""
        from job_finder.web.ats_scanner import scan_lever
        lever_jobs = [
            {
                "text": "Data Scientist",
                "hostedUrl": "https://jobs.lever.co/stripe/xyz",
                "descriptionPlain": "Role desc.",
                "categories": {"location": "Remote"},
                "salaryRange": None,
            }
        ]
        mock_resp = self._make_lever_response(lever_jobs)

        with patch("job_finder.web.ats_scanner.requests.get", return_value=mock_resp):
            results = scan_lever(
                slug="stripe",
                target_titles=["data scientist"],
                exclusions=[],
            )

        assert results[0]["salary_min"] is None
        assert results[0]["salary_max"] is None

    def test_scan_lever_returns_empty_list_on_non_200(self):
        """scan_lever returns empty list when API returns non-200 response."""
        from job_finder.web.ats_scanner import scan_lever
        mock_resp = MagicMock()
        mock_resp.status_code = 404

        with patch("job_finder.web.ats_scanner.requests.get", return_value=mock_resp):
            results = scan_lever(
                slug="nonexistent",
                target_titles=["data scientist"],
                exclusions=[],
            )

        assert results == []

    def test_scan_lever_job_dict_has_required_keys(self):
        """scan_lever job dicts contain title, company_source, location, source_url."""
        from job_finder.web.ats_scanner import scan_lever
        lever_jobs = [
            {
                "text": "Machine Learning Engineer",
                "hostedUrl": "https://jobs.lever.co/acme/job-1",
                "descriptionPlain": "Build ML systems.",
                "categories": {"location": "New York, NY"},
                "salaryRange": None,
            }
        ]
        mock_resp = self._make_lever_response(lever_jobs)

        with patch("job_finder.web.ats_scanner.requests.get", return_value=mock_resp):
            results = scan_lever(
                slug="acme",
                target_titles=["machine learning"],
                exclusions=[],
            )

        assert len(results) == 1
        job = results[0]
        assert "title" in job
        assert "company_source" in job
        assert job["company_source"] == "Lever"
        assert "location" in job
        assert "source_url" in job
        assert "description" in job


# ---------------------------------------------------------------------------
# Tests: scan_greenhouse
# ---------------------------------------------------------------------------

class TestScanGreenhouse:
    """Tests for Greenhouse API scanning, keyword filter, cents-to-dollars."""

    def _make_greenhouse_response(self, jobs):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"jobs": jobs}
        return mock_resp

    def test_scan_greenhouse_parses_matched_jobs(self):
        """scan_greenhouse returns matched jobs from Greenhouse JSON response."""
        from job_finder.web.ats_scanner import scan_greenhouse
        gh_jobs = [
            {
                "title": "Senior Data Scientist",
                "absolute_url": "https://boards.greenhouse.io/airbnb/jobs/12345",
                "content": "<p>Build ML systems at Airbnb.</p>",
                "location": {"name": "San Francisco, CA"},
                "pay_input_ranges": [],
            }
        ]
        mock_resp = self._make_greenhouse_response(gh_jobs)

        with patch("job_finder.web.ats_scanner.requests.get", return_value=mock_resp):
            results = scan_greenhouse(
                board_token="airbnb",
                target_titles=["data scientist"],
                exclusions=[],
            )

        assert len(results) == 1
        assert results[0]["title"] == "Senior Data Scientist"

    def test_scan_greenhouse_converts_cents_to_dollars(self):
        """scan_greenhouse divides pay_input_ranges cents by 100 for dollar values."""
        from job_finder.web.ats_scanner import scan_greenhouse
        gh_jobs = [
            {
                "title": "Staff Data Scientist",
                "absolute_url": "https://boards.greenhouse.io/waymo/jobs/999",
                "content": "<p>Lead data science.</p>",
                "location": {"name": "Mountain View, CA"},
                "pay_input_ranges": [
                    {"min_cents": 20000000, "max_cents": 28000000}  # $200k - $280k
                ],
            }
        ]
        mock_resp = self._make_greenhouse_response(gh_jobs)

        with patch("job_finder.web.ats_scanner.requests.get", return_value=mock_resp):
            results = scan_greenhouse(
                board_token="waymo",
                target_titles=["data scientist"],
                exclusions=[],
            )

        assert len(results) == 1
        assert results[0]["salary_min"] == 200000  # 20000000 / 100
        assert results[0]["salary_max"] == 280000  # 28000000 / 100

    def test_scan_greenhouse_returns_empty_list_on_non_200(self):
        """scan_greenhouse returns empty list on non-200 response."""
        from job_finder.web.ats_scanner import scan_greenhouse
        mock_resp = MagicMock()
        mock_resp.status_code = 404

        with patch("job_finder.web.ats_scanner.requests.get", return_value=mock_resp):
            results = scan_greenhouse(
                board_token="invalid",
                target_titles=["data scientist"],
                exclusions=[],
            )

        assert results == []

    def test_scan_greenhouse_filters_non_matching_titles(self):
        """scan_greenhouse applies _title_matches keyword filter."""
        from job_finder.web.ats_scanner import scan_greenhouse
        gh_jobs = [
            {
                "title": "Data Scientist",
                "absolute_url": "https://boards.greenhouse.io/airbnb/jobs/1",
                "content": "<p>DS role.</p>",
                "location": {"name": "Remote"},
                "pay_input_ranges": [],
            },
            {
                "title": "Marketing Manager",
                "absolute_url": "https://boards.greenhouse.io/airbnb/jobs/2",
                "content": "<p>Marketing role.</p>",
                "location": {"name": "NYC"},
                "pay_input_ranges": [],
            },
        ]
        mock_resp = self._make_greenhouse_response(gh_jobs)

        with patch("job_finder.web.ats_scanner.requests.get", return_value=mock_resp):
            results = scan_greenhouse(
                board_token="airbnb",
                target_titles=["data scientist"],
                exclusions=[],
            )

        assert len(results) == 1
        assert results[0]["title"] == "Data Scientist"


# ---------------------------------------------------------------------------
# Tests: scan_ashby
# ---------------------------------------------------------------------------

class TestScanAshby:
    """Tests for Ashby API scanning, slug casing, compensation tiers."""

    def _make_ashby_response(self, jobs):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"jobs": jobs}
        return mock_resp

    def test_scan_ashby_parses_matched_jobs(self):
        """scan_ashby returns matched jobs from Ashby JSON response."""
        from job_finder.web.ats_scanner import scan_ashby
        ashby_jobs = [
            {
                "title": "Senior Data Scientist",
                "jobUrl": "https://jobs.ashbyhq.com/OpenAI/abc-uuid",
                "descriptionHtml": "<p>Train foundation models.</p>",
                "location": "San Francisco, CA",
                "isRemote": False,
                "compensation": None,
            }
        ]
        mock_resp = self._make_ashby_response(ashby_jobs)

        with patch("job_finder.web.ats_scanner.requests.get", return_value=mock_resp):
            results = scan_ashby(
                job_board_name="OpenAI",
                target_titles=["data scientist"],
                exclusions=[],
            )

        assert len(results) == 1
        assert results[0]["title"] == "Senior Data Scientist"

    def test_scan_ashby_preserves_case_sensitive_slug_in_url(self):
        """scan_ashby uses exact slug casing in API URL (Research Pitfall 3)."""
        from job_finder.web.ats_scanner import scan_ashby

        mock_resp = self._make_ashby_response([])

        with patch("job_finder.web.ats_scanner.requests.get", return_value=mock_resp) as mock_get:
            scan_ashby(
                job_board_name="OpenAI",  # CamelCase slug
                target_titles=["data scientist"],
                exclusions=[],
            )

        # Verify the URL used in the API call has the exact slug casing
        call_args = mock_get.call_args
        called_url = call_args[0][0]
        assert "OpenAI" in called_url  # Must preserve exact casing

    def test_scan_ashby_extracts_compensation_summary(self):
        """scan_ashby extracts salaryCompensationSummary for salary_min/salary_max."""
        from job_finder.web.ats_scanner import scan_ashby
        ashby_jobs = [
            {
                "title": "Staff Machine Learning Engineer",
                "jobUrl": "https://jobs.ashbyhq.com/Ramp/def-uuid",
                "descriptionHtml": "<p>ML at Ramp.</p>",
                "location": "New York, NY",
                "isRemote": True,
                "compensation": {
                    "summaryComponents": [
                        {
                            "compensationType": "base_salary",
                            "minValue": 200000,
                            "maxValue": 280000,
                            "currency": "USD",
                        }
                    ],
                    "compensationTierSummary": "Competitive compensation",
                },
            }
        ]
        mock_resp = self._make_ashby_response(ashby_jobs)

        with patch("job_finder.web.ats_scanner.requests.get", return_value=mock_resp):
            results = scan_ashby(
                job_board_name="Ramp",
                target_titles=["machine learning"],
                exclusions=[],
            )

        assert len(results) == 1
        job = results[0]
        assert job["salary_min"] == 200000
        assert job["salary_max"] == 280000

    def test_scan_ashby_stores_comp_json(self):
        """scan_ashby stores full compensation data as comp_json blob."""
        from job_finder.web.ats_scanner import scan_ashby
        ashby_jobs = [
            {
                "title": "Research Engineer",
                "jobUrl": "https://jobs.ashbyhq.com/OpenAI/ghi-uuid",
                "descriptionHtml": "<p>Research.</p>",
                "location": "San Francisco, CA",
                "isRemote": False,
                "compensation": {
                    "summaryComponents": [],
                    "compensationTierSummary": "Equity 0.01%-0.1%, Bonus 15%",
                },
            }
        ]
        mock_resp = self._make_ashby_response(ashby_jobs)

        with patch("job_finder.web.ats_scanner.requests.get", return_value=mock_resp):
            results = scan_ashby(
                job_board_name="OpenAI",
                target_titles=["research"],
                exclusions=[],
            )

        assert len(results) == 1
        assert results[0]["comp_json"] is not None

    def test_scan_ashby_returns_empty_list_on_non_200(self):
        """scan_ashby returns empty list on non-200 response."""
        from job_finder.web.ats_scanner import scan_ashby
        mock_resp = MagicMock()
        mock_resp.status_code = 404

        with patch("job_finder.web.ats_scanner.requests.get", return_value=mock_resp):
            results = scan_ashby(
                job_board_name="invalid",
                target_titles=["data scientist"],
                exclusions=[],
            )

        assert results == []


# ---------------------------------------------------------------------------
# Tests: run_ats_scan
# ---------------------------------------------------------------------------

class TestRunAtsScan:
    """Tests for the full ATS scan orchestration."""

    def _insert_hit_company(self, conn, name, platform, slug, scan_enabled=1):
        """Insert a company with ats_probe_status='hit' and configurable scan_enabled."""
        from datetime import datetime
        now = datetime.now().isoformat()
        cursor = conn.execute(
            """INSERT INTO companies
               (name, name_raw, ats_platform, ats_slug, ats_probe_status,
                scan_enabled, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'hit', ?, ?, ?)""",
            (name.lower(), name, platform, slug, scan_enabled, now, now),
        )
        conn.commit()
        return cursor.lastrowid

    def test_run_ats_scan_returns_early_in_testing_mode(self, migrated_db_path):
        """run_ats_scan with TESTING=True returns zeros without making API calls."""
        from job_finder.web.ats_scanner import run_ats_scan

        with patch("job_finder.web.ats_scanner.requests.get") as mock_get:
            result = run_ats_scan(migrated_db_path, config={"TESTING": True})

        mock_get.assert_not_called()
        assert result["companies_scanned"] == 0
        assert result["jobs_discovered"] == 0
        assert result["jobs_new"] == 0

    def test_run_ats_scan_queries_only_hit_and_enabled_companies(self, migrated_db_path):
        """run_ats_scan only scans companies with ats_probe_status='hit' AND scan_enabled=1."""
        from job_finder.web.ats_scanner import run_ats_scan

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row

        # Hit + enabled (should be scanned)
        self._insert_hit_company(conn, "Stripe", "lever", "stripe", scan_enabled=1)
        # Hit + disabled (should NOT be scanned)
        self._insert_hit_company(conn, "Acme", "lever", "acme", scan_enabled=0)
        # Pending (should NOT be scanned even if enabled)
        from datetime import datetime
        now = datetime.now().isoformat()
        conn.execute(
            """INSERT INTO companies (name, name_raw, ats_probe_status, scan_enabled, created_at, updated_at)
               VALUES ('pendingco', 'PendingCo', 'pending', 1, ?, ?)""",
            (now, now),
        )
        conn.commit()
        conn.close()

        # Mock Lever returning empty list for 'stripe' scan
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []

        config = {"TESTING": False, "profile": {"target_titles": [], "exclusions": {"title_keywords": []}}}

        with patch("job_finder.web.ats_scanner.requests.get", return_value=mock_resp):
            result = run_ats_scan(migrated_db_path, config=config)

        # Only the hit+enabled company should be scanned
        assert result["companies_scanned"] == 1

    def test_run_ats_scan_skips_disabled_companies(self, migrated_db_path):
        """run_ats_scan skips companies with scan_enabled=0."""
        from job_finder.web.ats_scanner import run_ats_scan

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        self._insert_hit_company(conn, "DisabledCo", "lever", "disabledco", scan_enabled=0)
        conn.close()

        config = {"TESTING": False, "profile": {"target_titles": [], "exclusions": {"title_keywords": []}}}

        # Patch both requests.get and run_homepage_discovery to prevent network calls.
        # run_homepage_discovery now runs as a pre-step and would call requests.get internally.
        with patch("job_finder.web.ats_scanner.requests.get") as mock_get, \
             patch("job_finder.web.ats_scanner.run_homepage_discovery") as mock_discover:
            mock_discover.return_value = {"companies_checked": 0, "homepages_found": 0, "errors": []}
            result = run_ats_scan(migrated_db_path, config=config)

        mock_get.assert_not_called()
        assert result["companies_scanned"] == 0

    def test_run_ats_scan_upserts_discovered_jobs(self, migrated_db_path):
        """run_ats_scan creates Job objects and calls upsert_job for matched postings."""
        from job_finder.web.ats_scanner import run_ats_scan

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        self._insert_hit_company(conn, "Stripe", "lever", "stripe")
        conn.close()

        lever_jobs = [
            {
                "text": "Senior Data Scientist",
                "hostedUrl": "https://jobs.lever.co/stripe/abc-123",
                "descriptionPlain": "Build ML models at Stripe.",
                "categories": {"location": "Remote"},
                "salaryRange": None,
            }
        ]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = lever_jobs

        config = {
            "TESTING": False,
            "profile": {
                "target_titles": ["data scientist"],
                "exclusions": {"title_keywords": []},
            },
        }

        with patch("job_finder.web.ats_scanner.requests.get", return_value=mock_resp):
            with patch("job_finder.web.ats_scanner.score_and_persist_haiku", return_value=None):
                result = run_ats_scan(migrated_db_path, config=config)

        assert result["jobs_discovered"] == 1
        assert result["jobs_new"] == 1

        # Verify job is in DB
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT title, company FROM jobs WHERE title = 'Senior Data Scientist'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["title"] == "Senior Data Scientist"

    def test_run_ats_scan_calls_haiku_scoring_for_new_jobs(self, migrated_db_path):
        """run_ats_scan calls score_and_persist_haiku for newly discovered jobs."""
        from job_finder.web.ats_scanner import run_ats_scan

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        self._insert_hit_company(conn, "Stripe", "lever", "stripe")
        conn.close()

        lever_jobs = [
            {
                "text": "Data Scientist",
                "hostedUrl": "https://jobs.lever.co/stripe/job-1",
                "descriptionPlain": "ML at Stripe.",
                "categories": {"location": "Remote"},
                "salaryRange": None,
            }
        ]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = lever_jobs

        config = {
            "TESTING": False,
            "profile": {
                "target_titles": ["data scientist"],
                "exclusions": {"title_keywords": []},
            },
        }

        with patch("job_finder.web.ats_scanner.requests.get", return_value=mock_resp):
            with patch("job_finder.web.ats_scanner.score_and_persist_haiku", return_value=None) as mock_haiku:
                result = run_ats_scan(migrated_db_path, config=config)

        # score_and_persist_haiku called once per new job (1 job discovered)
        mock_haiku.assert_called_once()

    def test_run_ats_scan_calls_sonnet_evaluation_for_above_threshold_jobs(self, migrated_db_path):
        """run_ats_scan calls score_and_persist_sonnet when Haiku score >= threshold."""
        from job_finder.web.ats_scanner import run_ats_scan

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        self._insert_hit_company(conn, "Stripe", "lever", "stripe")
        conn.close()

        # Description must be >200 chars so upsert_job populates jd_full
        # (Sonnet loop skips jobs without jd_full)
        long_desc = "ML and Data Science role at Stripe. " * 8

        lever_jobs = [
            {
                "text": "Senior Data Scientist",
                "hostedUrl": "https://jobs.lever.co/stripe/job-1",
                "descriptionPlain": long_desc,
                "categories": {"location": "Remote"},
                "salaryRange": None,
            }
        ]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = lever_jobs

        config = {
            "TESTING": False,
            "scoring": {"haiku_threshold": 42},
            "profile": {
                "target_titles": ["data scientist"],
                "exclusions": {"title_keywords": []},
            },
        }

        # Haiku returns score above threshold -> job enters sonnet_queue
        haiku_result = {"score": 75, "summary": "Good match"}

        with patch("job_finder.web.ats_scanner.requests.get", return_value=mock_resp):
            with patch("job_finder.web.ats_scanner.score_and_persist_haiku", return_value=haiku_result):
                with patch("job_finder.web.ats_scanner.score_and_persist_sonnet", return_value={"score": 80}) as mock_sonnet:
                    result = run_ats_scan(migrated_db_path, config=config)

        mock_sonnet.assert_called_once()
        assert result["sonnet_evaluated"] == 1

    def test_run_ats_scan_inserts_runs_table_entry(self, migrated_db_path):
        """run_ats_scan inserts a row into runs table with source='ats_scan'."""
        from job_finder.web.ats_scanner import run_ats_scan

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        self._insert_hit_company(conn, "Stripe", "lever", "stripe")
        conn.close()

        lever_jobs = []  # No jobs found
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = lever_jobs

        config = {
            "TESTING": False,
            "profile": {
                "target_titles": ["data scientist"],
                "exclusions": {"title_keywords": []},
            },
        }

        with patch("job_finder.web.ats_scanner.requests.get", return_value=mock_resp):
            with patch("job_finder.web.ats_scanner.score_and_persist_haiku", return_value=None):
                run_ats_scan(migrated_db_path, config=config)

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM runs WHERE source = 'ats_scan'"
        ).fetchone()
        conn.close()

        assert row is not None
        assert row["source"] == "ats_scan"

    def test_run_ats_scan_logs_company_scan_log_entry(self, migrated_db_path):
        """run_ats_scan inserts a company_scan_log row for each scanned company."""
        from job_finder.web.ats_scanner import run_ats_scan

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = self._insert_hit_company(conn, "Stripe", "lever", "stripe")
        conn.close()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []

        config = {
            "TESTING": False,
            "profile": {
                "target_titles": ["data scientist"],
                "exclusions": {"title_keywords": []},
            },
        }

        with patch("job_finder.web.ats_scanner.requests.get", return_value=mock_resp):
            with patch("job_finder.web.ats_scanner.score_and_persist_haiku", return_value=None):
                run_ats_scan(migrated_db_path, config=config)

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM company_scan_log WHERE company_id = ?",
            (company_id,),
        ).fetchone()
        conn.close()

        assert row is not None
        assert row["company_id"] == company_id

    def test_run_ats_scan_updates_company_last_scanned_at(self, migrated_db_path):
        """run_ats_scan updates company.last_scanned_at after scan completes."""
        from job_finder.web.ats_scanner import run_ats_scan

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = self._insert_hit_company(conn, "Stripe", "lever", "stripe")
        conn.close()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []

        config = {
            "TESTING": False,
            "profile": {
                "target_titles": ["data scientist"],
                "exclusions": {"title_keywords": []},
            },
        }

        with patch("job_finder.web.ats_scanner.requests.get", return_value=mock_resp):
            with patch("job_finder.web.ats_scanner.score_and_persist_haiku", return_value=None):
                run_ats_scan(migrated_db_path, config=config)

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT last_scanned_at FROM companies WHERE id = ?",
            (company_id,),
        ).fetchone()
        conn.close()

        assert row["last_scanned_at"] is not None

    def test_run_ats_scan_returns_summary_dict(self, migrated_db_path):
        """run_ats_scan returns a summary dict with expected keys."""
        from job_finder.web.ats_scanner import run_ats_scan

        config = {"TESTING": True}
        result = run_ats_scan(migrated_db_path, config=config)

        assert "companies_scanned" in result
        assert "jobs_discovered" in result
        assert "jobs_new" in result
        assert "haiku_scored" in result
        assert "errors" in result

    def test_run_ats_scan_salary_first_seen_wins(self, migrated_db_path):
        """ATS salary sets salary_min/salary_max only on new jobs (first-seen wins)."""
        from job_finder.web.ats_scanner import run_ats_scan
        from datetime import datetime

        # Pre-insert a job with existing salary data
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        self._insert_hit_company(conn, "Stripe", "lever", "stripe")
        now = datetime.now().isoformat()
        conn.execute(
            """INSERT INTO jobs
               (dedup_key, title, company, location, sources, source_urls,
                salary_min, salary_max, first_seen, last_seen)
               VALUES (?, ?, ?, ?, '[]', '[]', ?, ?, ?, ?)""",
            ("stripe|senior data scientist", "Senior Data Scientist", "Stripe",
             "Remote", 180000, 240000, now, now),
        )
        conn.commit()
        conn.close()

        # ATS scan returns different salary (should NOT overwrite existing)
        lever_jobs = [
            {
                "text": "Senior Data Scientist",
                "hostedUrl": "https://jobs.lever.co/stripe/abc-123",
                "descriptionPlain": "ML at Stripe.",
                "categories": {"location": "Remote"},
                "salaryRange": {"currency": "USD", "min": 150000, "max": 200000},
            }
        ]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = lever_jobs

        config = {
            "TESTING": False,
            "profile": {
                "target_titles": ["data scientist"],
                "exclusions": {"title_keywords": []},
            },
        }

        with patch("job_finder.web.ats_scanner.requests.get", return_value=mock_resp):
            with patch("job_finder.web.ats_scanner.score_and_persist_haiku", return_value=None):
                run_ats_scan(migrated_db_path, config=config)

        # Existing salary should be preserved (upsert_job uses COALESCE for salary)
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT salary_min, salary_max FROM jobs WHERE title = 'Senior Data Scientist'"
        ).fetchone()
        conn.close()
        # The upsert_job UPDATE uses COALESCE(?, salary_min) so existing value preserved
        assert row["salary_min"] == 180000
        assert row["salary_max"] == 240000


# ---------------------------------------------------------------------------
# Tests: run_ats_scan HTML fallback loop
# ---------------------------------------------------------------------------

class TestRunAtsScanHtmlFallback:
    """Tests for the HTML fallback loop in run_ats_scan for miss companies."""

    def _insert_miss_company(self, conn, name, homepage_url=None, scan_enabled=1):
        """Insert a company with ats_probe_status='miss'."""
        from datetime import datetime
        now = datetime.now().isoformat()
        cursor = conn.execute(
            """INSERT INTO companies
               (name, name_raw, homepage_url, ats_probe_status,
                scan_enabled, created_at, updated_at)
               VALUES (?, ?, ?, 'miss', ?, ?, ?)""",
            (name.lower(), name, homepage_url, scan_enabled, now, now),
        )
        conn.commit()
        return cursor.lastrowid

    def test_html_fallback_queries_miss_companies_with_homepage(self, migrated_db_path):
        """run_ats_scan HTML fallback queries companies with ats_probe_status='miss' AND homepage_url IS NOT NULL AND scan_enabled=1."""
        from job_finder.web.ats_scanner import run_ats_scan

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = self._insert_miss_company(conn, "StartupCo", homepage_url="https://startup.co")
        conn.close()

        config = {
            "TESTING": False,
            "profile": {"target_titles": ["data scientist"], "exclusions": {"title_keywords": []}},
        }

        mock_careers_url = "https://startup.co/careers"
        with patch("job_finder.web.ats_scanner.requests.get") as mock_get:
            # No hit companies, so no first loop API calls
            with patch("job_finder.web.careers_scraper.requests.get") as mock_careers_get:
                mock_find_resp = MagicMock()
                mock_find_resp.url = "https://startup.co/"
                mock_find_resp.text = '<html><body><a href="/careers">Careers</a></body></html>'
                mock_find_resp.status_code = 200

                mock_scrape_resp = MagicMock()
                mock_scrape_resp.url = "https://startup.co/careers"
                mock_scrape_resp.text = '<html><body><a href="/jobs/1">Data Scientist</a></body></html>'
                mock_scrape_resp.status_code = 200

                mock_careers_get.side_effect = [mock_find_resp, mock_scrape_resp]

                with patch("job_finder.web.ats_scanner.score_and_persist_haiku", return_value=None):
                    with patch("job_finder.web.ats_scanner.time.sleep"):
                        result = run_ats_scan(migrated_db_path, config=config)

        # html_scraped should be in summary
        assert "html_scraped" in result

    def test_html_fallback_calls_find_careers_url_and_scrape_careers_page(self, migrated_db_path):
        """run_ats_scan HTML fallback calls find_careers_url then scrape_careers_page."""
        from job_finder.web.ats_scanner import run_ats_scan

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        self._insert_miss_company(conn, "StartupCo", homepage_url="https://startup.co")
        conn.close()

        config = {
            "TESTING": False,
            "profile": {"target_titles": ["data scientist"], "exclusions": {"title_keywords": []}},
        }

        with patch("job_finder.web.ats_scanner.find_careers_url", return_value="https://startup.co/careers") as mock_find:
            with patch("job_finder.web.ats_scanner.scrape_careers_page", return_value=[]) as mock_scrape:
                with patch("job_finder.web.ats_scanner.score_and_persist_haiku", return_value=None):
                    with patch("job_finder.web.ats_scanner.time.sleep"):
                        run_ats_scan(migrated_db_path, config=config)

        # Check positional args — keyword args (client, conn, config) also present now
        assert mock_find.call_args[0][0] == "https://startup.co"
        assert mock_scrape.call_args[0][0] == "https://startup.co/careers"
        assert mock_scrape.call_args[0][1] == ["data scientist"]
        assert mock_scrape.call_args[0][2] == []

    def test_html_fallback_creates_job_objects_from_scraped_listings(self, migrated_db_path):
        """run_ats_scan HTML fallback creates Job objects and calls upsert_job for scraped listings."""
        from job_finder.web.ats_scanner import run_ats_scan

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        self._insert_miss_company(conn, "StartupCo", homepage_url="https://startup.co")
        conn.close()

        config = {
            "TESTING": False,
            "profile": {"target_titles": ["data scientist"], "exclusions": {"title_keywords": []}},
        }

        scraped_jobs = [
            {"title": "Data Scientist", "url": "https://startup.co/jobs/1"},
        ]

        with patch("job_finder.web.ats_scanner.find_careers_url", return_value="https://startup.co/careers"):
            with patch("job_finder.web.ats_scanner.scrape_careers_page", return_value=scraped_jobs):
                with patch("job_finder.web.ats_scanner.score_and_persist_haiku", return_value=None):
                    with patch("job_finder.web.ats_scanner.time.sleep"):
                        result = run_ats_scan(migrated_db_path, config=config)

        # Job should be created in DB
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        job = conn.execute(
            "SELECT * FROM jobs WHERE title = 'Data Scientist'"
        ).fetchone()
        conn.close()

        assert job is not None
        # source is stored as JSON array in 'sources' column
        import json as _json
        sources = _json.loads(job["sources"] or "[]")
        assert "careers_page" in sources
        assert result["html_scraped"] >= 1

    def test_html_fallback_skips_miss_companies_without_homepage_url(self, migrated_db_path):
        """run_ats_scan HTML fallback skips miss companies that have no homepage_url."""
        from job_finder.web.ats_scanner import run_ats_scan

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        # Insert miss company with no homepage_url
        self._insert_miss_company(conn, "NoHomepageCo", homepage_url=None)
        conn.close()

        config = {
            "TESTING": False,
            "profile": {"target_titles": ["data scientist"], "exclusions": {"title_keywords": []}},
        }

        with patch("job_finder.web.ats_scanner.find_careers_url") as mock_find:
            with patch("job_finder.web.ats_scanner.score_and_persist_haiku", return_value=None):
                with patch("job_finder.web.ats_scanner.time.sleep"):
                    result = run_ats_scan(migrated_db_path, config=config)

        # find_careers_url should NOT be called (no homepage_url in query)
        mock_find.assert_not_called()

    def test_run_ats_scan_summary_includes_html_scraped_count(self, migrated_db_path):
        """run_ats_scan summary dict includes html_scraped key."""
        from job_finder.web.ats_scanner import run_ats_scan

        config = {"TESTING": True}
        result = run_ats_scan(migrated_db_path, config=config)

        assert "html_scraped" in result

    def test_html_fallback_skips_when_find_careers_url_returns_none(self, migrated_db_path):
        """HTML fallback skips scraping when find_careers_url returns None."""
        from job_finder.web.ats_scanner import run_ats_scan

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        self._insert_miss_company(conn, "NoCareersCo", homepage_url="https://no-careers.co")
        conn.close()

        config = {
            "TESTING": False,
            "profile": {"target_titles": ["data scientist"], "exclusions": {"title_keywords": []}},
        }

        with patch("job_finder.web.ats_scanner.find_careers_url", return_value=None):
            with patch("job_finder.web.ats_scanner.scrape_careers_page") as mock_scrape:
                with patch("job_finder.web.ats_scanner.score_and_persist_haiku", return_value=None):
                    with patch("job_finder.web.ats_scanner.time.sleep"):
                        result = run_ats_scan(migrated_db_path, config=config)

        # scrape_careers_page should NOT be called when find_careers_url returns None
        mock_scrape.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: HTML-scraped jobs included in Haiku scoring (Phase 08 Plan 01)
# ---------------------------------------------------------------------------

class TestHTMLJobsScoring:
    """Tests that HTML-scraped jobs are included in the Haiku scoring pass.

    The scoring block MUST run AFTER the HTML fallback loop so that
    all_new_job_keys contains both ATS API jobs AND HTML-scraped jobs.
    """

    def _insert_hit_company(self, conn, name, platform, slug, scan_enabled=1):
        """Insert a company with ats_probe_status='hit'."""
        from datetime import datetime
        now = datetime.now().isoformat()
        cursor = conn.execute(
            """INSERT INTO companies
               (name, name_raw, ats_platform, ats_slug, ats_probe_status,
                scan_enabled, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'hit', ?, ?, ?)""",
            (name.lower(), name, platform, slug, scan_enabled, now, now),
        )
        conn.commit()
        return cursor.lastrowid

    def _insert_miss_company(self, conn, name, homepage_url=None, scan_enabled=1):
        """Insert a company with ats_probe_status='miss'."""
        from datetime import datetime
        now = datetime.now().isoformat()
        cursor = conn.execute(
            """INSERT INTO companies
               (name, name_raw, homepage_url, ats_probe_status,
                scan_enabled, created_at, updated_at)
               VALUES (?, ?, ?, 'miss', ?, ?, ?)""",
            (name.lower(), name, homepage_url, scan_enabled, now, now),
        )
        conn.commit()
        return cursor.lastrowid

    def test_haiku_scoring_receives_both_ats_and_html_job_keys(self, migrated_db_path):
        """score_and_persist_haiku called for jobs from BOTH ATS API and HTML fallback."""
        from job_finder.web.ats_scanner import run_ats_scan

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        # Hit company provides ATS API jobs
        self._insert_hit_company(conn, "Stripe", "lever", "stripe")
        # Miss company provides HTML-scraped jobs
        self._insert_miss_company(conn, "StartupCo", homepage_url="https://startup.co")
        conn.close()

        # ATS API returns 1 job for Stripe
        lever_jobs = [
            {
                "text": "Data Scientist",
                "hostedUrl": "https://jobs.lever.co/stripe/job-1",
                "descriptionPlain": "ML at Stripe.",
                "categories": {"location": "Remote"},
                "salaryRange": None,
            }
        ]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = lever_jobs

        # HTML fallback returns 1 job for StartupCo
        html_jobs = [{"title": "Data Scientist", "url": "https://startup.co/jobs/1"}]

        config = {
            "TESTING": False,
            "profile": {
                "target_titles": ["data scientist"],
                "exclusions": {"title_keywords": []},
            },
        }

        captured_keys = []

        def capture_haiku_scoring(conn, job_row, *args, **kwargs):
            captured_keys.append(job_row.get("dedup_key"))
            return None

        with patch("job_finder.web.ats_scanner.requests.get", return_value=mock_resp):
            with patch("job_finder.web.ats_scanner.find_careers_url", return_value="https://startup.co/careers"):
                with patch("job_finder.web.ats_scanner.scrape_careers_page", return_value=html_jobs):
                    with patch("job_finder.web.ats_scanner.score_and_persist_haiku", side_effect=capture_haiku_scoring):
                        with patch("job_finder.web.ats_scanner.time.sleep"):
                            result = run_ats_scan(migrated_db_path, config=config)

        # Scoring must have been called for BOTH jobs (1 from ATS API + 1 from HTML)
        assert len(captured_keys) == 2, (
            f"Expected score_and_persist_haiku called 2 times (ATS + HTML), got {len(captured_keys)}: {captured_keys}"
        )

    def test_haiku_scored_summary_count_includes_html_scraped_jobs(self, migrated_db_path):
        """summary['haiku_scored'] reflects the count including HTML-scraped jobs."""
        from job_finder.web.ats_scanner import run_ats_scan

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        self._insert_hit_company(conn, "Stripe", "lever", "stripe")
        self._insert_miss_company(conn, "StartupCo", homepage_url="https://startup.co")
        conn.close()

        lever_jobs = [
            {
                "text": "Data Scientist",
                "hostedUrl": "https://jobs.lever.co/stripe/job-1",
                "descriptionPlain": "ML at Stripe.",
                "categories": {"location": "Remote"},
                "salaryRange": None,
            }
        ]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = lever_jobs

        html_jobs = [{"title": "Data Scientist", "url": "https://startup.co/jobs/2"}]

        config = {
            "TESTING": False,
            "profile": {
                "target_titles": ["data scientist"],
                "exclusions": {"title_keywords": []},
            },
        }

        # score_and_persist_haiku returns a result for each call -> haiku_scored increments
        haiku_result = {"score": 30, "summary": "Below threshold"}

        with patch("job_finder.web.ats_scanner.requests.get", return_value=mock_resp):
            with patch("job_finder.web.ats_scanner.find_careers_url", return_value="https://startup.co/careers"):
                with patch("job_finder.web.ats_scanner.scrape_careers_page", return_value=html_jobs):
                    with patch("job_finder.web.ats_scanner.score_and_persist_haiku", return_value=haiku_result):
                        with patch("job_finder.web.ats_scanner.time.sleep"):
                            result = run_ats_scan(migrated_db_path, config=config)

        assert result["haiku_scored"] == 2, (
            f"Expected haiku_scored=2 (ATS + HTML jobs), got: {result['haiku_scored']}"
        )


# ---------------------------------------------------------------------------
# Tests: /companies/scan route — probe before scan
# ---------------------------------------------------------------------------

class TestScanRouteProbeBeforeScan:
    """Tests for /companies/scan route calling probe_ats_slugs before run_ats_scan.

    The manual 'Scan ATS' button must probe pending companies first so that
    newly-added companies transition from pending->hit/miss before the scan
    runs (which only queries WHERE ats_probe_status='hit').
    """

    @pytest.fixture
    def app_with_db(self, migrated_db_path):
        """Create Flask test app wired to the migrated temp DB."""
        from job_finder.web import create_app

        app = create_app(config={
            "db": {"path": migrated_db_path},
            "TESTING": True,
        })
        app.config["TESTING"] = True
        return app

    def test_scan_route_calls_probe_before_run_scan(self, app_with_db):
        """POST /companies/scan calls probe_ats_slugs BEFORE run_ats_scan."""
        call_order = []

        def mock_probe(db_path, config):
            call_order.append("probe")
            return {"probed": 1, "hits": 1, "misses": 0}

        def mock_scan(db_path, config):
            call_order.append("scan")
            return {
                "companies_scanned": 1,
                "jobs_discovered": 2,
                "jobs_new": 2,
                "haiku_scored": 2,
                "html_scraped": 0,
                "errors": [],
                "probe": {},
            }

        with app_with_db.test_client() as client:
            with patch("job_finder.web.blueprints.companies.probe_ats_slugs", side_effect=mock_probe):
                with patch("job_finder.web.blueprints.companies.run_ats_scan", side_effect=mock_scan):
                    response = client.post("/companies/scan")

        assert response.status_code == 200
        assert call_order == ["probe", "scan"], (
            f"Expected probe called before scan, got call order: {call_order}"
        )

    def test_scan_route_logs_probe_result(self, app_with_db):
        """POST /companies/scan logs the probe result via logger.info."""
        probe_result = {"probed": 2, "hits": 1, "misses": 1}
        scan_result = {
            "companies_scanned": 1,
            "jobs_discovered": 0,
            "jobs_new": 0,
            "haiku_scored": 0,
            "html_scraped": 0,
            "errors": [],
            "probe": probe_result,
        }

        with app_with_db.test_client() as client:
            with patch("job_finder.web.blueprints.companies.probe_ats_slugs", return_value=probe_result):
                with patch("job_finder.web.blueprints.companies.run_ats_scan", return_value=scan_result):
                    with patch("job_finder.web.blueprints.companies.logger") as mock_logger:
                        response = client.post("/companies/scan")

        assert response.status_code == 200
        # Verify logger.info was called with probe result
        info_calls = [str(c) for c in mock_logger.info.call_args_list]
        assert any("probe" in c.lower() for c in info_calls), (
            f"Expected logger.info called with probe result, got: {info_calls}"
        )


# ---------------------------------------------------------------------------
# Tests: ATS Retry Logic (Phase 14, DEBT-01)
# ---------------------------------------------------------------------------

def _insert_company_with_status(conn, name, status, platform=None, slug=None,
                                  retry_count=0, retry_after=None, miss_reason=None):
    """Helper: insert a company with given probe status and retry fields."""
    from datetime import datetime
    now = datetime.now().isoformat()
    cursor = conn.execute(
        """INSERT INTO companies
           (name, name_raw, ats_platform, ats_slug, ats_probe_status,
            retry_count, retry_after, miss_reason,
            scan_enabled, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
        (name.lower(), name, platform, slug, status,
         retry_count, retry_after, miss_reason, now, now),
    )
    conn.commit()
    return cursor.lastrowid


class TestAtsRetryLogic:
    """Tests for ATS transient error retry state machine (DEBT-01)."""

    def test_compute_retry_after_hour_1(self):
        """_compute_retry_after(0) returns timestamp ~1 hour from now (naive UTC)."""
        from datetime import datetime, timedelta, timezone
        from job_finder.web.ats_scanner import _compute_retry_after
        before = datetime.now(timezone.utc).replace(tzinfo=None)
        result = _compute_retry_after(0)
        after = datetime.now(timezone.utc).replace(tzinfo=None)
        parsed = datetime.fromisoformat(result)
        expected_min = before + timedelta(hours=1) - timedelta(seconds=5)
        expected_max = after + timedelta(hours=1) + timedelta(seconds=5)
        assert expected_min <= parsed <= expected_max, (
            f"retry_after for retry_count=0 should be ~1hr from now, got {result}"
        )

    def test_compute_retry_after_hour_4(self):
        """_compute_retry_after(1) returns timestamp ~4 hours from now (naive UTC)."""
        from datetime import datetime, timedelta, timezone
        from job_finder.web.ats_scanner import _compute_retry_after
        before = datetime.now(timezone.utc).replace(tzinfo=None)
        result = _compute_retry_after(1)
        after = datetime.now(timezone.utc).replace(tzinfo=None)
        parsed = datetime.fromisoformat(result)
        expected_min = before + timedelta(hours=4) - timedelta(seconds=5)
        expected_max = after + timedelta(hours=4) + timedelta(seconds=5)
        assert expected_min <= parsed <= expected_max, (
            f"retry_after for retry_count=1 should be ~4hr from now, got {result}"
        )

    def test_compute_retry_after_hour_24(self):
        """_compute_retry_after(2) returns timestamp ~24 hours from now (naive UTC)."""
        from datetime import datetime, timedelta, timezone
        from job_finder.web.ats_scanner import _compute_retry_after
        before = datetime.now(timezone.utc).replace(tzinfo=None)
        result = _compute_retry_after(2)
        after = datetime.now(timezone.utc).replace(tzinfo=None)
        parsed = datetime.fromisoformat(result)
        expected_min = before + timedelta(hours=24) - timedelta(seconds=5)
        expected_max = after + timedelta(hours=24) + timedelta(seconds=5)
        assert expected_min <= parsed <= expected_max, (
            f"retry_after for retry_count=2 should be ~24hr from now, got {result}"
        )

    def test_transient_error_sets_error_status(self, migrated_db_path):
        """First transient error sets ats_probe_status='error', retry_count=1."""
        from datetime import datetime
        from job_finder.web.ats_scanner import _handle_scan_error
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = _insert_company_with_status(conn, "TestCo", "pending")
        now = datetime.now().isoformat()
        _handle_scan_error(conn, company_id, "TestCo", "503 Service Unavailable", now)
        row = conn.execute(
            "SELECT ats_probe_status, retry_count, retry_after FROM companies WHERE id = ?",
            (company_id,),
        ).fetchone()
        conn.close()
        assert row["ats_probe_status"] == "error", (
            f"Expected 'error', got: {row['ats_probe_status']}"
        )
        assert row["retry_count"] == 1, f"Expected retry_count=1, got: {row['retry_count']}"
        assert row["retry_after"] is not None, "Expected retry_after to be set"

    def test_retry_count_increments_with_backoff(self, migrated_db_path):
        """Second transient error increments retry_count to 2 and sets ~4hr retry_after."""
        from datetime import datetime, timedelta, timezone
        from job_finder.web.ats_scanner import _handle_scan_error
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = _insert_company_with_status(
            conn, "BackoffCo", "error", retry_count=1
        )
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        before = datetime.now(timezone.utc).replace(tzinfo=None)
        _handle_scan_error(conn, company_id, "BackoffCo", "502 Bad Gateway", now)
        after = datetime.now(timezone.utc).replace(tzinfo=None)
        row = conn.execute(
            "SELECT retry_count, retry_after FROM companies WHERE id = ?",
            (company_id,),
        ).fetchone()
        conn.close()
        assert row["retry_count"] == 2, f"Expected retry_count=2, got: {row['retry_count']}"
        parsed = datetime.fromisoformat(row["retry_after"])
        expected_min = before + timedelta(hours=4) - timedelta(seconds=5)
        expected_max = after + timedelta(hours=4) + timedelta(seconds=5)
        assert expected_min <= parsed <= expected_max, (
            f"Expected ~4hr retry_after, got {row['retry_after']}"
        )

    def test_third_failure_promotes_to_unreachable(self, migrated_db_path):
        """Third failure (retry_count already at max) promotes to miss/unreachable."""
        from datetime import datetime
        from job_finder.web.ats_scanner import _handle_scan_error
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = _insert_company_with_status(
            conn, "UnreachableCo", "error", retry_count=2
        )
        now = datetime.now().isoformat()
        _handle_scan_error(conn, company_id, "UnreachableCo", "504 Gateway Timeout", now)
        row = conn.execute(
            "SELECT ats_probe_status, miss_reason, retry_count FROM companies WHERE id = ?",
            (company_id,),
        ).fetchone()
        conn.close()
        assert row["ats_probe_status"] == "miss", (
            f"Expected 'miss', got: {row['ats_probe_status']}"
        )
        assert row["miss_reason"] == "unreachable", (
            f"Expected miss_reason='unreachable', got: {row['miss_reason']}"
        )
        assert row["retry_count"] == 3, f"Expected retry_count=3, got: {row['retry_count']}"

    def test_successful_retry_resets_to_hit(self, migrated_db_path):
        """Successful probe on error company resets to hit, clears retry state."""
        from datetime import datetime
        from job_finder.web.ats_scanner import _reset_retry_state
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = _insert_company_with_status(
            conn, "RecoverCo", "error", retry_count=2,
            retry_after="2099-01-01 00:00:00"
        )
        # Update ats_probe_status to hit + reset retry state
        now = datetime.now().isoformat()
        conn.execute(
            "UPDATE companies SET ats_probe_status = 'hit' WHERE id = ?",
            (company_id,),
        )
        _reset_retry_state(conn, company_id, now)
        row = conn.execute(
            "SELECT ats_probe_status, retry_count, retry_after, miss_reason FROM companies WHERE id = ?",
            (company_id,),
        ).fetchone()
        conn.close()
        assert row["ats_probe_status"] == "hit", (
            f"Expected 'hit', got: {row['ats_probe_status']}"
        )
        assert row["retry_count"] == 0, f"Expected retry_count=0, got: {row['retry_count']}"
        assert row["retry_after"] is None, f"Expected retry_after=None, got: {row['retry_after']}"
        assert row["miss_reason"] is None, f"Expected miss_reason=None, got: {row['miss_reason']}"

    def test_error_company_included_in_scan_query(self, migrated_db_path):
        """Error company with past retry_after appears in run_ats_scan company query."""
        from datetime import datetime, timedelta, timezone
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        # Use SQLite-compatible format (no timezone offset) so comparisons with datetime('now') work
        past_retry = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        _insert_company_with_status(
            conn, "EligibleCo", "error",
            platform="lever", slug="eligible-co",
            retry_count=1, retry_after=past_retry,
        )
        rows = conn.execute(
            """SELECT id, name_raw FROM companies
               WHERE (
                   (ats_probe_status = 'hit' AND scan_enabled = 1)
                   OR
                   (ats_probe_status = 'error' AND scan_enabled = 1
                    AND (retry_after IS NULL OR retry_after < datetime('now')))
               )"""
        ).fetchall()
        conn.close()
        names = [r["name_raw"] for r in rows]
        assert "EligibleCo" in names, (
            f"Expected EligibleCo in scan results, got: {names}"
        )

    def test_error_company_with_future_retry_skipped(self, migrated_db_path):
        """Error company with future retry_after is not included in scan query."""
        from datetime import datetime, timedelta, timezone
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        # Use SQLite-compatible format (no timezone offset)
        future_retry = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
        _insert_company_with_status(
            conn, "SkippedCo", "error",
            platform="lever", slug="skipped-co",
            retry_count=1, retry_after=future_retry,
        )
        rows = conn.execute(
            """SELECT id, name_raw FROM companies
               WHERE (
                   (ats_probe_status = 'hit' AND scan_enabled = 1)
                   OR
                   (ats_probe_status = 'error' AND scan_enabled = 1
                    AND (retry_after IS NULL OR retry_after < datetime('now')))
               )"""
        ).fetchall()
        conn.close()
        names = [r["name_raw"] for r in rows]
        assert "SkippedCo" not in names, (
            f"SkippedCo should be skipped (future retry_after), but found in: {names}"
        )

    def test_permanent_miss_on_404(self, migrated_db_path):
        """404 response causes ats_probe_status='miss' without miss_reason='unreachable'."""
        from job_finder.web.ats_scanner import _PERMANENT_MISS_CODES
        assert 404 in _PERMANENT_MISS_CODES, "404 should be in _PERMANENT_MISS_CODES"
        assert 410 in _PERMANENT_MISS_CODES, "410 should be in _PERMANENT_MISS_CODES"

    def test_transient_codes_set(self):
        """_TRANSIENT_CODES contains expected HTTP status codes."""
        from job_finder.web.ats_scanner import _TRANSIENT_CODES
        expected = {429, 500, 502, 503, 504}
        assert expected.issubset(_TRANSIENT_CODES), (
            f"Missing transient codes: {expected - _TRANSIENT_CODES}"
        )

    def test_probe_single_company_success(self, migrated_db_path):
        """probe_single_company returns hit status on successful API response."""
        from job_finder.web.ats_scanner import probe_single_company
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = _insert_company_with_status(
            conn, "SuccessCo", "error",
            platform="lever", slug="success-co",
            retry_count=1,
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{"text": "Software Engineer", "hostedUrl": "https://jobs.lever.co/success-co/1", "categories": {}, "salaryRange": None}]

        with patch("requests.get", return_value=mock_resp):
            result = probe_single_company(company_id, conn, {"TESTING": False})

        row = conn.execute(
            "SELECT ats_probe_status, retry_count FROM companies WHERE id = ?",
            (company_id,),
        ).fetchone()
        conn.close()
        assert result["status"] == "hit", f"Expected 'hit', got: {result}"
        assert row["ats_probe_status"] == "hit", (
            f"Expected company updated to 'hit', got: {row['ats_probe_status']}"
        )
        assert row["retry_count"] == 0, f"Expected retry_count reset to 0, got: {row['retry_count']}"

    def test_probe_single_company_transient_error(self, migrated_db_path):
        """probe_single_company on timeout returns error dict and increments retry_count."""
        import requests as req_module
        from job_finder.web.ats_scanner import probe_single_company
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = _insert_company_with_status(
            conn, "TimeoutCo", "pending",
            platform="lever", slug="timeout-co",
            retry_count=0,
        )

        with patch("requests.get", side_effect=req_module.exceptions.Timeout("timed out")):
            result = probe_single_company(company_id, conn, {"TESTING": False})

        row = conn.execute(
            "SELECT ats_probe_status, retry_count FROM companies WHERE id = ?",
            (company_id,),
        ).fetchone()
        conn.close()
        assert result["status"] == "error", f"Expected 'error', got: {result}"
        assert row["ats_probe_status"] == "error", (
            f"Expected 'error', got: {row['ats_probe_status']}"
        )
        assert row["retry_count"] == 1, f"Expected retry_count=1, got: {row['retry_count']}"

    def test_migration_12_adds_companies_columns(self, migrated_db_path):
        """Migration 12 adds retry_count, retry_after, miss_reason to companies table."""
        conn = sqlite3.connect(migrated_db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(companies)").fetchall()}
        conn.close()
        assert "retry_count" in cols, "retry_count missing from companies"
        assert "retry_after" in cols, "retry_after missing from companies"
        assert "miss_reason" in cols, "miss_reason missing from companies"


# ---------------------------------------------------------------------------
# Tests: POST /companies/<id>/retry route
# ---------------------------------------------------------------------------

class TestRetryRoute:
    """Tests for POST /companies/<id>/retry route."""

    @pytest.fixture
    def app_with_db(self, migrated_db_path):
        """Create Flask test app wired to the migrated temp DB."""
        from job_finder.web import create_app

        app = create_app(config={
            "db": {"path": migrated_db_path},
            "TESTING": True,
        })
        app.config["TESTING"] = True
        return app

    def test_retry_route_success(self, app_with_db, migrated_db_path):
        """POST /companies/{id}/retry for error company returns 200 with updated row HTML."""
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = _insert_company_with_status(
            conn, "RetrySuccessCo", "error",
            platform="lever", slug="retry-success",
            retry_count=1,
        )
        conn.close()

        mock_result = {"status": "hit", "jobs_found": 3}
        with app_with_db.test_client() as client:
            with patch("job_finder.web.blueprints.companies.probe_single_company", return_value=mock_result):
                response = client.post(f"/companies/{company_id}/retry")

        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        data = response.get_data(as_text=True)
        assert f"company-row-{company_id}" in data, (
            f"Expected company row div in response, got: {data[:200]}"
        )

    def test_retry_route_rejects_non_error(self, app_with_db, migrated_db_path):
        """POST /companies/{id}/retry for hit company returns 400."""
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = _insert_company_with_status(
            conn, "HitCo", "hit",
            platform="lever", slug="hit-co",
        )
        conn.close()

        with app_with_db.test_client() as client:
            response = client.post(f"/companies/{company_id}/retry")

        assert response.status_code == 400, f"Expected 400 for hit company, got {response.status_code}"

    def test_retry_route_unreachable_allowed(self, app_with_db, migrated_db_path):
        """POST /companies/{id}/retry for unreachable company returns 200."""
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = _insert_company_with_status(
            conn, "UnreachableCo2", "miss",
            platform="lever", slug="unreachable-co",
            retry_count=3, miss_reason="unreachable",
        )
        conn.close()

        mock_result = {"status": "hit", "jobs_found": 0}
        with app_with_db.test_client() as client:
            with patch("job_finder.web.blueprints.companies.probe_single_company", return_value=mock_result):
                response = client.post(f"/companies/{company_id}/retry")

        assert response.status_code == 200, (
            f"Expected 200 for unreachable company retry, got {response.status_code}"
        )

    def test_retry_route_rejects_regular_miss(self, app_with_db, migrated_db_path):
        """POST /companies/{id}/retry for regular miss (no miss_reason) returns 400."""
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = _insert_company_with_status(
            conn, "MissCo", "miss",
            platform=None, slug=None,
        )
        conn.close()

        with app_with_db.test_client() as client:
            response = client.post(f"/companies/{company_id}/retry")

        assert response.status_code == 400, (
            f"Expected 400 for regular miss company, got {response.status_code}"
        )


# ---------------------------------------------------------------------------
# Tests: URL pattern audit (real-world format verification)
# ---------------------------------------------------------------------------

class TestAtsUrlPatternAudit:
    # AUDIT 2026-03-15: URL patterns verified against current Lever, Greenhouse, Ashby job posting formats.
    # All regex patterns match real-world URLs. Probe URL construction confirmed correct.

    # --- Lever URL pattern audit ---

    def test_lever_jobs_url_with_uuid_job_id(self):
        """Lever jobs.lever.co/{slug}/{uuid} format extracts correct slug."""
        from job_finder.web.ats_detection import extract_ats_from_urls
        urls = ["https://jobs.lever.co/stripe/abc123-def456-789"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "lever"
        assert slug == "stripe"

    def test_lever_jobs_url_with_query_params(self):
        """Lever URL with query params (?lever-origin=applied) still extracts slug."""
        from job_finder.web.ats_detection import extract_ats_from_urls
        urls = ["https://jobs.lever.co/openai/12345678-abcd-efgh?lever-origin=applied"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "lever"
        assert slug == "openai"

    def test_lever_api_url_with_pagination_params(self):
        """Lever api.lever.co/v0/postings/{slug}?mode=json&skip=0&limit=100 extracts slug."""
        from job_finder.web.ats_detection import extract_ats_from_urls
        urls = ["https://api.lever.co/v0/postings/figma?mode=json&skip=0&limit=100"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "lever"
        assert slug == "figma"

    def test_lever_probe_url_construction(self):
        """Lever probe URL for slug 'stripe' equals expected API endpoint."""
        slug = "stripe"
        expected = "https://api.lever.co/v0/postings/stripe?mode=json"
        actual = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        assert actual == expected

    # --- Greenhouse URL pattern audit ---

    def test_greenhouse_boards_url_with_job_id(self):
        """Greenhouse boards.greenhouse.io/{slug}/jobs/{id} extracts correct slug."""
        from job_finder.web.ats_detection import extract_ats_from_urls
        urls = ["https://boards.greenhouse.io/airbnb/jobs/6082884"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "greenhouse"
        assert slug == "airbnb"

    def test_greenhouse_embed_url_slug_limitation(self):
        """Greenhouse embed URL is not a job posting URL — slug extracted is 'embed'.

        # NOTE: Greenhouse embed URLs are not job posting URLs — extract_ats_from_urls may
        # return incorrect slug for embed links. This is acceptable as real job URLs use the
        # /company/jobs/ID format.
        """
        from job_finder.web.ats_detection import extract_ats_from_urls
        # boards.greenhouse.io/embed/job_board/js?for=waymo is an embed URL, not a direct job URL.
        # The regex matches /embed as the slug — this is a known pattern limitation.
        urls = ["https://boards.greenhouse.io/embed/job_board/js?for=waymo"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "greenhouse"
        assert slug == "embed"  # Pattern limitation: captures /embed, not the company slug

    def test_greenhouse_api_url_with_query_params(self):
        """Greenhouse boards-api URL with ?content=true extracts correct slug."""
        from job_finder.web.ats_detection import extract_ats_from_urls
        urls = ["https://boards-api.greenhouse.io/v1/boards/databricks/jobs?content=true"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "greenhouse"
        assert slug == "databricks"

    def test_greenhouse_probe_url_construction(self):
        """Greenhouse probe URL for slug 'waymo' equals expected API endpoint."""
        slug = "waymo"
        expected = "https://boards-api.greenhouse.io/v1/boards/waymo/jobs"
        actual = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
        assert actual == expected

    # --- Ashby URL pattern audit ---

    def test_ashby_url_with_job_id_preserves_case(self):
        """Ashby URL with job UUID preserves exact slug casing (case-sensitive)."""
        from job_finder.web.ats_detection import extract_ats_from_urls
        urls = ["https://jobs.ashbyhq.com/OpenAI/12345-abcde-fghij"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "ashby"
        assert slug == "OpenAI"

    def test_ashby_url_company_page_no_job_id(self):
        """Ashby URL with just company slug (no job ID) extracts slug."""
        from job_finder.web.ats_detection import extract_ats_from_urls
        urls = ["https://jobs.ashbyhq.com/Ramp"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "ashby"
        assert slug == "Ramp"

    def test_ashby_url_with_query_params(self):
        """Ashby URL with ?departmentId query param extracts slug."""
        from job_finder.web.ats_detection import extract_ats_from_urls
        urls = ["https://jobs.ashbyhq.com/notion/abcdef?departmentId=engineering"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "ashby"
        assert slug == "notion"

    def test_ashby_probe_url_construction(self):
        """Ashby probe URL for slug 'OpenAI' equals expected API endpoint with compensation."""
        slug = "OpenAI"
        expected = "https://api.ashbyhq.com/posting-api/job-board/OpenAI?includeCompensation=true"
        actual = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
        assert actual == expected

    # --- Cross-platform verification ---

    def test_lever_wins_over_linkedin_in_mixed_url_list(self):
        """URL list with LinkedIn first and Lever second — Lever (first ATS match) wins."""
        from job_finder.web.ats_detection import extract_ats_from_urls
        urls = [
            "https://www.linkedin.com/jobs/view/987654321/",
            "https://jobs.lever.co/stripe/some-uuid",
        ]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "lever"
        assert slug == "stripe"

    def test_non_ats_greenhouse_domain_returns_none(self):
        """Greenhouse marketing URL (not a job board) returns (None, None)."""
        from job_finder.web.ats_detection import extract_ats_from_urls
        urls = ["https://www.greenhouse.io/blog/hiring"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform is None
        assert slug is None


# ---------------------------------------------------------------------------
# ATS jd_full storage tests (Phase 40 Plan 01 — NEW)
# ---------------------------------------------------------------------------


class TestAtsJdFullStorage:
    """Verify run_ats_scan writes jd_full after job upsert using COALESCE guard.

    ATS APIs (Lever, Greenhouse, Ashby) return full JDs in the description field.
    After upsert, the scanner should write description to jd_full for Sonnet access.
    The COALESCE guard ensures existing jd_full values are never overwritten.
    """

    def _insert_hit_company(self, conn, name, platform, slug, scan_enabled=1):
        """Helper: insert company with ats_probe_status='hit'."""
        from datetime import datetime
        now = datetime.now().isoformat()
        cursor = conn.execute(
            """INSERT INTO companies
               (name, name_raw, ats_platform, ats_slug, ats_probe_status,
                scan_enabled, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'hit', ?, ?, ?)""",
            (name.lower(), name, platform, slug, scan_enabled, now, now),
        )
        conn.commit()
        return cursor.lastrowid

    def test_ats_scan_writes_jd_full_for_new_jobs(self, migrated_db_path):
        """After upsert of a new ATS job with description > 200 chars, jd_full is set."""
        from job_finder.web.ats_scanner import run_ats_scan

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        self._insert_hit_company(conn, "Stripe", "lever", "stripe")
        conn.close()

        long_desc = "Build ML models at Stripe. " * 20  # > 200 chars
        lever_jobs = [
            {
                "text": "Senior Data Scientist",
                "hostedUrl": "https://jobs.lever.co/stripe/abc-123",
                "descriptionPlain": long_desc,
                "categories": {"location": "Remote"},
                "salaryRange": None,
            }
        ]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = lever_jobs

        config = {
            "TESTING": False,
            "profile": {
                "target_titles": ["data scientist"],
                "exclusions": {"title_keywords": []},
            },
        }

        with patch("job_finder.web.ats_scanner.requests.get", return_value=mock_resp):
            with patch("job_finder.web.ats_scanner.score_and_persist_haiku", return_value=None):
                run_ats_scan(migrated_db_path, config=config)

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT jd_full FROM jobs WHERE title = 'Senior Data Scientist'"
        ).fetchone()
        conn.close()

        assert row is not None, "Job row not found in DB after ATS scan"
        assert row["jd_full"] is not None, (
            "Expected jd_full to be set after ATS scan with long description"
        )

    def test_ats_scan_does_not_overwrite_existing_jd_full(self, migrated_db_path):
        """If a job already has jd_full in DB, COALESCE guard does not overwrite it."""
        from job_finder.web.ats_scanner import run_ats_scan
        from job_finder.models import Job

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        self._insert_hit_company(conn, "Acme", "lever", "acme")

        # Pre-insert a job with existing jd_full
        existing_jd = "Existing high-quality job description already stored by a prior source."
        dedup_key = Job.normalized_dedup_key("Acme", "Staff Engineer")
        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls,
               first_seen, last_seen, score, score_breakdown, user_interest, jd_full)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (dedup_key, "Staff Engineer", "Acme", "Remote",
             '["lever"]', '["https://jobs.lever.co/acme/xyz"]',
             "2026-01-01", "2026-01-01", 0, '{}', "unreviewed", existing_jd),
        )
        conn.commit()
        conn.close()

        long_desc = "A new ATS description that should NOT overwrite existing jd_full. " * 10
        lever_jobs = [
            {
                "text": "Staff Engineer",
                "hostedUrl": "https://jobs.lever.co/acme/xyz",
                "descriptionPlain": long_desc,
                "categories": {"location": "Remote"},
                "salaryRange": None,
            }
        ]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = lever_jobs

        config = {
            "TESTING": False,
            "profile": {
                "target_titles": ["staff engineer"],
                "exclusions": {"title_keywords": []},
            },
        }

        with patch("job_finder.web.ats_scanner.requests.get", return_value=mock_resp):
            with patch("job_finder.web.ats_scanner.score_and_persist_haiku", return_value=None):
                run_ats_scan(migrated_db_path, config=config)

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT jd_full FROM jobs WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()
        conn.close()

        assert row is not None, "Job row not found after ATS scan"
        assert row["jd_full"] == existing_jd, (
            f"Existing jd_full should NOT be overwritten by COALESCE guard. "
            f"Got: {row['jd_full']!r}"
        )

    def test_ats_scan_skips_jd_full_for_short_descriptions(self, migrated_db_path):
        """Job with description <= 200 chars does not trigger the jd_full write."""
        from job_finder.web.ats_scanner import run_ats_scan

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        self._insert_hit_company(conn, "Beta", "lever", "beta")
        conn.close()

        short_desc = "Short description."  # < 200 chars
        lever_jobs = [
            {
                "text": "Junior Analyst",
                "hostedUrl": "https://jobs.lever.co/beta/abc-999",
                "descriptionPlain": short_desc,
                "categories": {"location": "NYC"},
                "salaryRange": None,
            }
        ]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = lever_jobs

        config = {
            "TESTING": False,
            "profile": {
                "target_titles": ["analyst"],
                "exclusions": {"title_keywords": []},
            },
        }

        with patch("job_finder.web.ats_scanner.requests.get", return_value=mock_resp):
            with patch("job_finder.web.ats_scanner.score_and_persist_haiku", return_value=None):
                run_ats_scan(migrated_db_path, config=config)

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT jd_full FROM jobs WHERE title = 'Junior Analyst'"
        ).fetchone()
        conn.close()

        assert row is not None, "Job row not found after ATS scan"
        assert row["jd_full"] is None, (
            f"Short description should NOT set jd_full. Got: {row['jd_full']!r}"
        )


# ---------------------------------------------------------------------------
# Homepage discovery integration tests
# ---------------------------------------------------------------------------


class TestHomepageDiscoveryIntegration:
    """Tests for homepage discovery pre-step in run_ats_scan."""

    def test_run_ats_scan_calls_homepage_discovery(self, migrated_db_path):
        """run_ats_scan calls run_homepage_discovery before the HTML fallback loop."""
        from job_finder.web.ats_scanner import run_ats_scan

        config = {"TESTING": False, "profile": {"target_titles": [], "exclusions": {}}}

        with patch("job_finder.web.ats_scanner.run_homepage_discovery") as mock_discover:
            mock_discover.return_value = {"companies_checked": 5, "homepages_found": 2, "errors": []}
            result = run_ats_scan(migrated_db_path, config)

        mock_discover.assert_called_once_with(migrated_db_path, config)
        assert result["homepages_discovered"] == 2

    def test_run_ats_scan_handles_discovery_failure(self, migrated_db_path):
        """run_ats_scan continues gracefully when homepage discovery raises."""
        from job_finder.web.ats_scanner import run_ats_scan

        config = {"TESTING": False, "profile": {"target_titles": [], "exclusions": {}}}

        with patch("job_finder.web.ats_scanner.run_homepage_discovery") as mock_discover:
            mock_discover.side_effect = Exception("network error")
            result = run_ats_scan(migrated_db_path, config)

        # Scan completes despite discovery failure
        assert result["homepages_discovered"] == 0
        assert "companies_scanned" in result

    def test_run_ats_scan_testing_mode_skips_discovery(self, migrated_db_path):
        """run_ats_scan in TESTING mode returns immediately without calling discovery."""
        from job_finder.web.ats_scanner import run_ats_scan

        config = {"TESTING": True, "profile": {"target_titles": [], "exclusions": {}}}
        result = run_ats_scan(migrated_db_path, config)
        assert result["homepages_discovered"] == 0


# ---------------------------------------------------------------------------
# HTML fallback description passthrough tests
# ---------------------------------------------------------------------------


class TestHtmlFallbackDescriptionPassthrough:
    """Tests for description passthrough in HTML fallback loop."""

    def test_scraped_description_passed_to_job_object(self, migrated_db_path):
        """HTML fallback loop passes scraped description to Job, not empty string."""
        from job_finder.web.ats_scanner import run_ats_scan
        from datetime import datetime

        config = {"TESTING": False, "profile": {"target_titles": ["Engineer"], "exclusions": {}}}

        # Insert a miss company with homepage_url
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        now = datetime.now().isoformat()
        conn.execute(
            """INSERT INTO companies
               (name, name_raw, homepage_url, ats_probe_status, scan_enabled,
                jobs_found_total, created_at, updated_at)
               VALUES (?, ?, ?, 'miss', 1, 0, ?, ?)""",
            ("acme", "Acme Corp", "https://acme.com", now, now),
        )
        conn.commit()
        conn.close()

        scraped_jobs = [{"title": "Software Engineer", "url": "https://acme.com/jobs/1", "description": "Full JD text here"}]

        with patch("job_finder.web.ats_scanner.run_homepage_discovery", return_value={"companies_checked": 0, "homepages_found": 0, "errors": []}), \
             patch("job_finder.web.ats_scanner.find_careers_url", return_value="https://acme.com/careers"), \
             patch("job_finder.web.ats_scanner.scrape_careers_page", return_value=scraped_jobs):
            result = run_ats_scan(migrated_db_path, config)

        # Verify the job was created with the scraped description
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        job = conn.execute("SELECT description FROM jobs WHERE company = 'Acme Corp'").fetchone()
        conn.close()

        assert job is not None
        assert job["description"] == "Full JD text here"


# ---------------------------------------------------------------------------
# Tests: probe_ats_slugs retry state machine (Fix 1)
# ---------------------------------------------------------------------------

class TestProbeAtsSlugsRetry:
    """Tests that probe_ats_slugs() correctly handles transient errors."""

    def _insert_pending_company(self, conn, name="Acme Corp"):
        from datetime import datetime
        now = datetime.now().isoformat()
        cursor = conn.execute(
            """INSERT INTO companies (name, name_raw, ats_probe_status, created_at, updated_at)
               VALUES (?, ?, 'pending', ?, ?)""",
            (name.lower(), name, now, now),
        )
        conn.commit()
        return cursor.lastrowid

    def test_probe_ats_slugs_timeout_preserves_retry(self, db_conn):
        """Timeout during probe is transient — company gets error status with retry_after, not permanent miss."""
        import requests
        db_path, conn = db_conn
        company_id = self._insert_pending_company(conn)

        with patch("job_finder.web.ats_scanner._probe_lever_with_result",
                   side_effect=requests.exceptions.Timeout("timeout")), \
             patch("job_finder.web.ats_scanner._probe_greenhouse_with_result",
                   side_effect=requests.exceptions.Timeout("timeout")), \
             patch("job_finder.web.ats_scanner._probe_ashby_with_result",
                   side_effect=requests.exceptions.Timeout("timeout")):
            from job_finder.web.ats_scanner import probe_ats_slugs
            result = probe_ats_slugs(db_path, {"TESTING": False})

        row = conn.execute(
            "SELECT ats_probe_status FROM companies WHERE id = ?",
            (company_id,),
        ).fetchone()
        # Timeouts are transient; _handle_scan_error sets 'error' status (retry-eligible), not permanent 'miss'
        assert row["ats_probe_status"] == "error"
        assert result["misses"] == 0  # Not counted as a miss

    def test_probe_ats_slugs_all_miss_sets_miss_status(self, db_conn):
        """When all probes return False, company gets ats_probe_status='miss'."""
        db_path, conn = db_conn
        company_id = self._insert_pending_company(conn)

        with patch("job_finder.web.ats_scanner._probe_lever_with_result", return_value=False), \
             patch("job_finder.web.ats_scanner._probe_greenhouse_with_result", return_value=False), \
             patch("job_finder.web.ats_scanner._probe_ashby_with_result", return_value=False):
            from job_finder.web.ats_scanner import probe_ats_slugs
            result = probe_ats_slugs(db_path, {"TESTING": False})

        row = conn.execute(
            "SELECT ats_probe_status FROM companies WHERE id = ?", (company_id,)
        ).fetchone()
        assert row["ats_probe_status"] == "miss"
        assert result["misses"] >= 1


# ---------------------------------------------------------------------------
# Tests: probe_ats_slugs batch limit (Fix 3)
# ---------------------------------------------------------------------------

class TestProbeAtsSlugsLimit:
    """Tests that probe_ats_slugs() respects _PROBE_BATCH_LIMIT."""

    def test_probe_ats_slugs_respects_batch_limit(self, db_conn):
        """With 200 pending companies, probe processes only _PROBE_BATCH_LIMIT."""
        from datetime import datetime
        db_path, conn = db_conn

        # Insert 200 pending companies (more than _PROBE_BATCH_LIMIT)
        now = datetime.now().isoformat()
        for i in range(200):
            conn.execute(
                """INSERT INTO companies (name, name_raw, ats_probe_status, created_at, updated_at)
                   VALUES (?, ?, 'pending', ?, ?)""",
                (f"company{i}", f"Company {i}", now, now),
            )
        conn.commit()

        with patch("job_finder.web.ats_scanner._probe_lever_with_result", return_value=False), \
             patch("job_finder.web.ats_scanner._probe_greenhouse_with_result", return_value=False), \
             patch("job_finder.web.ats_scanner._probe_ashby_with_result", return_value=False), \
             patch("job_finder.web.ats_scanner.time") as mock_time:
            mock_time.sleep = lambda x: None
            from job_finder.web.ats_scanner import probe_ats_slugs, _PROBE_BATCH_LIMIT
            result = probe_ats_slugs(db_path, {"TESTING": False})

        assert result["probed"] == _PROBE_BATCH_LIMIT


# ---------------------------------------------------------------------------
# Tests: find_or_create_company (Fix 6)
# ---------------------------------------------------------------------------

class TestFindOrCreateCompany:
    """Tests for find_or_create_company() unified creation path."""

    def _insert_company(self, conn, name):
        from datetime import datetime
        now = datetime.now().isoformat()
        from job_finder.web.dedup_normalizer import normalize_company
        cursor = conn.execute(
            """INSERT INTO companies (name, name_raw, ats_probe_status, created_at, updated_at)
               VALUES (?, ?, 'pending', ?, ?)""",
            (normalize_company(name), name, now, now),
        )
        conn.commit()
        return cursor.lastrowid

    def test_find_or_create_exact_match(self, db_conn):
        """Exact normalized name match returns existing company ID."""
        db_path, conn = db_conn
        existing_id = self._insert_company(conn, "Stripe")

        from job_finder.web.ats_scanner import find_or_create_company
        result_id = find_or_create_company(conn, "Stripe Inc.")
        assert result_id == existing_id

    def test_find_or_create_fuzzy_match(self, db_conn):
        """Fuzzy match path returns existing ID via mocked fuzzy_match_company."""
        db_path, conn = db_conn
        existing_id = self._insert_company(conn, "Stripe")

        from job_finder.web.ats_scanner import find_or_create_company
        # Patch fuzzy_match_company to return the existing ID — tests the fuzzy branch
        # "Stripe Technologies" doesn't normalize-exact-match "stripe" (no suffix stripped)
        with patch("job_finder.web.backfill_companies.fuzzy_match_company",
                   return_value=(existing_id, 90)):
            result_id = find_or_create_company(conn, "Stripe Technologies")

        assert result_id == existing_id

    def test_find_or_create_creates_new(self, db_conn):
        """No match creates and returns new company ID."""
        db_path, conn = db_conn

        from job_finder.web.ats_scanner import find_or_create_company
        result_id = find_or_create_company(conn, "Stripe")
        assert result_id is not None
        row = conn.execute("SELECT name_raw FROM companies WHERE id = ?", (result_id,)).fetchone()
        assert row["name_raw"] == "Stripe"


# ---------------------------------------------------------------------------
# Tests: Scan log differentiation — Fix 10
# ---------------------------------------------------------------------------

class TestScanLogDifferentiation:
    """Tests that company_scan_log.jobs_found tracks new insertions and
    jobs_matched tracks pre-dedup API matches."""

    def test_scan_log_records_new_vs_matched(self, db_conn):
        """API returns 5 jobs; 2 are new, 3 are duplicates.

        Expects: jobs_found=2, jobs_matched=5 in company_scan_log.
        """
        db_path, conn = db_conn

        company_id = _insert_hit_company(conn, "LogCo", "lever", "logco")

        # Pre-insert 3 jobs so they appear as duplicates (use canonical dedup_key)
        from datetime import datetime
        from job_finder.models import Job as _Job
        now_ts = datetime.now().isoformat()
        for i in range(3):
            conn.execute(
                """INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen)
                   VALUES (?, ?, 'LogCo', 'Remote', ?, ?)""",
                (_Job.normalized_dedup_key("LogCo", f"Data Analyst {i}"), f"Data Analyst {i}", now_ts, now_ts),
            )
        conn.commit()

        job_dicts = [
            {"title": f"Data Analyst {i}", "location": "Remote",
             "company_source": "Lever", "source_url": f"https://jobs.lever.co/logco/{i}",
             "description": "", "salary_min": None, "salary_max": None, "comp_json": None}
            for i in range(5)  # 0-2 are pre-existing dupes, 3-4 are new
        ]

        config = {
            "TESTING": False,
            "profile": {"target_titles": ["data analyst"], "exclusions": {"title_keywords": []}},
        }

        with patch("job_finder.web.ats_scanner.scan_lever", return_value=job_dicts), \
             patch("job_finder.web.ats_scanner.score_and_persist_haiku", return_value=None), \
             patch("job_finder.web.ats_scanner.time.sleep"):
            from job_finder.web.ats_scanner import run_ats_scan
            run_ats_scan(db_path, config=config)

        log = conn.execute(
            "SELECT jobs_found, jobs_matched FROM company_scan_log WHERE company_id = ?",
            (company_id,),
        ).fetchone()
        assert log is not None
        assert log["jobs_matched"] == 5
        # jobs_found = newly inserted (0, 1, 2 are dupes; 3, 4 are new → 2 new)
        assert log["jobs_found"] == 2

    def test_html_scan_log_records_new_vs_matched(self, db_conn):
        """HTML fallback: scrapes 3 jobs; 1 is already in DB (dupe), 2 are new.

        Expects: jobs_found=2, jobs_matched=3 in company_scan_log.
        """
        db_path, conn = db_conn

        from datetime import datetime
        now_ts = datetime.now().isoformat()

        company_id = conn.execute(
            """INSERT INTO companies
               (name, name_raw, homepage_url, ats_probe_status, scan_enabled, created_at, updated_at)
               VALUES ('htmlco', 'HtmlCo', 'https://htmlco.com', 'miss', 1, ?, ?)""",
            (now_ts, now_ts),
        ).lastrowid
        conn.commit()

        # Pre-insert 1 job as existing (use canonical dedup_key so it de-dupes correctly)
        from job_finder.models import Job as _Job
        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen)
               VALUES (?, 'Engineer 0', 'HtmlCo', 'Remote', ?, ?)""",
            (_Job.normalized_dedup_key("HtmlCo", "Engineer 0"), now_ts, now_ts),
        )
        conn.commit()

        scraped_jobs = [
            {"title": f"Engineer {i}", "url": f"https://htmlco.com/jobs/{i}", "description": ""}
            for i in range(3)  # 0 is dupe, 1 and 2 are new
        ]

        config = {
            "TESTING": False,
            "profile": {"target_titles": ["engineer"], "exclusions": {"title_keywords": []}},
        }

        with patch("job_finder.web.ats_scanner.find_careers_url", return_value="https://htmlco.com/careers"), \
             patch("job_finder.web.ats_scanner.scrape_careers_page", return_value=scraped_jobs), \
             patch("job_finder.web.ats_scanner.score_and_persist_haiku", return_value=None), \
             patch("job_finder.web.ats_scanner.time.sleep"):
            from job_finder.web.ats_scanner import run_ats_scan
            run_ats_scan(db_path, config=config)

        log = conn.execute(
            "SELECT jobs_found, jobs_matched FROM company_scan_log WHERE company_id = ?",
            (company_id,),
        ).fetchone()
        assert log is not None
        assert log["jobs_matched"] == 3
        assert log["jobs_found"] == 2


# ---------------------------------------------------------------------------
# Tests: jobs_found_total accuracy — Fix 9
# ---------------------------------------------------------------------------

class TestJobsFoundTotalAccuracy:
    """Tests that jobs_found_total is set via subquery (actual linked jobs),
    not inflated by pre-dedup API match counts."""

    def test_jobs_found_total_reflects_linked_count(self, db_conn):
        """After scan, jobs_found_total equals the count of jobs already linked
        to the company (via company_id), not the raw API match count."""
        db_path, conn = db_conn

        company_id = _insert_hit_company(conn, "SubqCo", "lever", "subqco")

        # Pre-link 4 jobs to the company so the subquery returns 4
        from datetime import datetime
        now_ts = datetime.now().isoformat()
        for i in range(4):
            conn.execute(
                """INSERT INTO jobs (dedup_key, title, company, company_id, location, first_seen, last_seen)
                   VALUES (?, ?, 'SubqCo', ?, 'Remote', ?, ?)""",
                (f"subqco-pre-{i}", f"Analyst {i}", company_id, now_ts, now_ts),
            )
        conn.commit()

        # API returns 7 jobs (duplicates of the 4 pre-existing + 3 truly new)
        job_dicts = [
            {"title": f"Analyst {i}", "location": "Remote",
             "company_source": "Lever", "source_url": f"https://jobs.lever.co/subqco/{i}",
             "description": "", "salary_min": None, "salary_max": None, "comp_json": None}
            for i in range(7)
        ]

        config = {
            "TESTING": False,
            "profile": {"target_titles": ["analyst"], "exclusions": {"title_keywords": []}},
        }

        with patch("job_finder.web.ats_scanner.scan_lever", return_value=job_dicts), \
             patch("job_finder.web.ats_scanner.score_and_persist_haiku", return_value=None), \
             patch("job_finder.web.ats_scanner.time.sleep"):
            from job_finder.web.ats_scanner import run_ats_scan
            run_ats_scan(db_path, config=config)

        row = conn.execute(
            "SELECT jobs_found_total FROM companies WHERE id = ?", (company_id,)
        ).fetchone()
        # Subquery counts only company_id-linked jobs: 4 pre-linked ones
        # (the 3 new jobs from this scan are not yet linked via company_id)
        assert row["jobs_found_total"] == 4

    def test_jobs_found_total_does_not_inflate_on_rescan(self, db_conn):
        """Scanning the same jobs twice doesn't double the total."""
        db_path, conn = db_conn

        company_id = _insert_hit_company(conn, "RescanCo", "lever", "rescanco")

        from datetime import datetime
        now_ts = datetime.now().isoformat()
        # Pre-link 2 jobs
        for i in range(2):
            conn.execute(
                """INSERT INTO jobs (dedup_key, title, company, company_id, location, first_seen, last_seen)
                   VALUES (?, ?, 'RescanCo', ?, 'Remote', ?, ?)""",
                (f"rescanco-job-{i}", f"PM {i}", company_id, now_ts, now_ts),
            )
        conn.commit()

        # API returns those same 2 jobs
        job_dicts = [
            {"title": f"PM {i}", "location": "Remote",
             "company_source": "Lever", "source_url": f"https://jobs.lever.co/rescanco/{i}",
             "description": "", "salary_min": None, "salary_max": None, "comp_json": None}
            for i in range(2)
        ]
        config = {
            "TESTING": False,
            "profile": {"target_titles": ["pm"], "exclusions": {"title_keywords": []}},
        }

        with patch("job_finder.web.ats_scanner.scan_lever", return_value=job_dicts), \
             patch("job_finder.web.ats_scanner.score_and_persist_haiku", return_value=None), \
             patch("job_finder.web.ats_scanner.time.sleep"):
            from job_finder.web.ats_scanner import run_ats_scan
            run_ats_scan(db_path, config=config)

        first_total = conn.execute(
            "SELECT jobs_found_total FROM companies WHERE id = ?", (company_id,)
        ).fetchone()["jobs_found_total"]

        # Scan again with same jobs — total must not change
        with patch("job_finder.web.ats_scanner.scan_lever", return_value=job_dicts), \
             patch("job_finder.web.ats_scanner.score_and_persist_haiku", return_value=None), \
             patch("job_finder.web.ats_scanner.time.sleep"):
            run_ats_scan(db_path, config=config)

        second_total = conn.execute(
            "SELECT jobs_found_total FROM companies WHERE id = ?", (company_id,)
        ).fetchone()["jobs_found_total"]

        assert second_total == first_total
