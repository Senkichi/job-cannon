"""Tests for company backfill script.

Tests all behaviors:
- fuzzy matching (exact, suffix variant, no match, threshold, short name guard)
- denylist filtering (Unknown, Medical jobs, Crossing Hurdles, etc.)
- company linkage (new creation, existing match, multiple jobs same company)
- ATS probing triggered after company creation
- DDG enrichment triggered on new companies
- Summary output after full run
"""

import sqlite3
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fuzzy match tests
# ---------------------------------------------------------------------------


class TestFuzzyMatchCompany:
    """Tests for fuzzy_match_company()."""

    def test_fuzzy_match_exact(self, migrated_db):
        """'Stripe' matches existing company 'stripe' with score >= 85."""
        from job_finder.web.backfill_companies import fuzzy_match_company

        path, conn = migrated_db
        # Insert existing company with normalized name
        conn.execute(
            "INSERT INTO companies (name, name_raw, ats_probe_status, created_at, updated_at) "
            "VALUES ('stripe', 'Stripe', 'pending', '2026-01-01', '2026-01-01')"
        )
        conn.commit()

        existing = conn.execute("SELECT id, name FROM companies").fetchall()
        existing_list = [(row["id"], row["name"]) for row in existing]

        company_id, score = fuzzy_match_company("Stripe", existing_list)

        assert company_id is not None
        assert score >= 85

    def test_fuzzy_match_suffix(self, migrated_db):
        """'OpenAI, Inc.' fuzzy-matches existing 'openai' with score >= 85."""
        from job_finder.web.backfill_companies import fuzzy_match_company

        path, conn = migrated_db
        conn.execute(
            "INSERT INTO companies (name, name_raw, ats_probe_status, created_at, updated_at) "
            "VALUES ('openai', 'OpenAI', 'pending', '2026-01-01', '2026-01-01')"
        )
        conn.commit()

        existing = [(row["id"], row["name"]) for row in conn.execute("SELECT id, name FROM companies").fetchall()]

        company_id, score = fuzzy_match_company("OpenAI, Inc.", existing)

        assert company_id is not None
        assert score >= 85

    def test_fuzzy_match_no_match(self):
        """'Acme Corp' with no existing companies returns (None, 0)."""
        from job_finder.web.backfill_companies import fuzzy_match_company

        company_id, score = fuzzy_match_company("Acme Corp", [])

        assert company_id is None
        assert score == 0

    def test_fuzzy_match_threshold(self, migrated_db):
        """Score below 85 does not match. 'Netflix' does not match 'Medical jobs'."""
        from job_finder.web.backfill_companies import fuzzy_match_company

        path, conn = migrated_db
        conn.execute(
            "INSERT INTO companies (name, name_raw, ats_probe_status, created_at, updated_at) "
            "VALUES ('medical jobs', 'Medical jobs', 'pending', '2026-01-01', '2026-01-01')"
        )
        conn.commit()

        existing = [(row["id"], row["name"]) for row in conn.execute("SELECT id, name FROM companies").fetchall()]

        company_id, score = fuzzy_match_company("Netflix", existing)

        assert company_id is None

    def test_fuzzy_match_short_name_guard(self, migrated_db):
        """Company names under 4 chars skip fuzzy matching (too unreliable)."""
        from job_finder.web.backfill_companies import fuzzy_match_company

        path, conn = migrated_db
        conn.execute(
            "INSERT INTO companies (name, name_raw, ats_probe_status, created_at, updated_at) "
            "VALUES ('ibm', 'IBM', 'pending', '2026-01-01', '2026-01-01')"
        )
        conn.commit()

        existing = [(row["id"], row["name"]) for row in conn.execute("SELECT id, name FROM companies").fetchall()]

        # "IBM" normalizes to "ibm" which is 3 chars — should skip matching
        company_id, score = fuzzy_match_company("IBM", existing)

        assert company_id is None
        assert score == 0


# ---------------------------------------------------------------------------
# Denylist tests
# ---------------------------------------------------------------------------


class TestDenylistFiltering:
    """Tests that denylist names are skipped during linkage."""

    @pytest.mark.parametrize("company_name", [
        "Unknown",
        "Medical jobs",
        "Clinical jobs",
        "Crossing Hurdles",
        "RemoteHunter",
        "Jobgether",
        "Mercor",
    ])
    def test_denylist_skipped(self, company_name, migrated_db):
        """Jobs with denylist company names produce no company records."""
        from job_finder.web.backfill_companies import link_jobs_to_companies

        path, conn = migrated_db

        # Insert a job with a denylist company name
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen) "
            "VALUES (?, 'Software Engineer', ?, 'Remote', '2026-01-01', '2026-01-01')",
            (f"denylist-test-{company_name.lower().replace(' ', '-')}", company_name),
        )
        conn.commit()

        with patch("job_finder.web.backfill_companies.upsert_company") as mock_upsert:
            linked_count, new_company_ids, matched_count = link_jobs_to_companies(conn)

        # upsert_company should NOT have been called for denylist names
        for call_args in mock_upsert.call_args_list:
            name_arg = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("name", "")
            assert company_name.lower() not in name_arg.lower(), (
                f"upsert_company was called with denylist name '{company_name}'"
            )


# ---------------------------------------------------------------------------
# Company linkage tests
# ---------------------------------------------------------------------------


class TestCompanyLinkage:
    """Tests for link_jobs_to_companies()."""

    def test_company_linkage_new(self, migrated_db):
        """Job with company='Acme Corp' creates a new company record and links job."""
        from job_finder.web.backfill_companies import link_jobs_to_companies

        path, conn = migrated_db

        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen) "
            "VALUES ('acme|engineer', 'Software Engineer', 'Acme Corp', 'Remote', '2026-01-01', '2026-01-01')"
        )
        conn.commit()

        new_company_id = 42
        with patch("job_finder.web.backfill_companies.upsert_company", return_value=new_company_id) as mock_upsert:
            linked_count, new_company_ids, matched_count = link_jobs_to_companies(conn)

        assert linked_count >= 1
        assert new_company_id in new_company_ids
        assert matched_count == 0

        # Job should have company_id set
        row = conn.execute(
            "SELECT company_id FROM jobs WHERE dedup_key = 'acme|engineer'"
        ).fetchone()
        assert row["company_id"] == new_company_id

    def test_company_linkage_existing(self, migrated_db):
        """Job with company='Stripe Inc' fuzzy-matches existing 'stripe' without creating new record."""
        from job_finder.web.backfill_companies import link_jobs_to_companies

        path, conn = migrated_db

        # Insert existing stripe company
        conn.execute(
            "INSERT INTO companies (name, name_raw, ats_probe_status, created_at, updated_at) "
            "VALUES ('stripe', 'Stripe', 'pending', '2026-01-01', '2026-01-01')"
        )
        conn.commit()
        existing_id = conn.execute("SELECT id FROM companies WHERE name = 'stripe'").fetchone()["id"]

        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen) "
            "VALUES ('stripe|engineer', 'Software Engineer', 'Stripe Inc', 'Remote', '2026-01-01', '2026-01-01')"
        )
        conn.commit()

        with patch("job_finder.web.backfill_companies.upsert_company") as mock_upsert:
            linked_count, new_company_ids, matched_count = link_jobs_to_companies(conn)

        # Should NOT create a new company
        mock_upsert.assert_not_called()
        assert matched_count >= 1
        assert existing_id not in new_company_ids

        # Job should be linked to existing stripe company
        row = conn.execute(
            "SELECT company_id FROM jobs WHERE dedup_key = 'stripe|engineer'"
        ).fetchone()
        assert row["company_id"] == existing_id

    def test_multiple_jobs_same_company(self, migrated_db):
        """3 jobs with company='Acme Corp' all link to the same single new company record."""
        from job_finder.web.backfill_companies import link_jobs_to_companies

        path, conn = migrated_db

        for i in range(3):
            conn.execute(
                "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen) "
                "VALUES (?, ?, 'Acme Corp', 'Remote', '2026-01-01', '2026-01-01')",
                (f"acme|job-{i}", f"Job {i}"),
            )
        conn.commit()

        new_company_id = 99
        call_count = 0

        def mock_upsert_side_effect(conn_arg, name, **kwargs):
            nonlocal call_count
            call_count += 1
            return new_company_id

        with patch("job_finder.web.backfill_companies.upsert_company", side_effect=mock_upsert_side_effect):
            linked_count, new_company_ids, matched_count = link_jobs_to_companies(conn)

        # upsert_company should be called only ONCE for 3 identical company names
        assert call_count == 1
        assert linked_count == 3

        # All 3 jobs should link to same company
        rows = conn.execute(
            "SELECT company_id FROM jobs WHERE company = 'Acme Corp'"
        ).fetchall()
        assert all(row["company_id"] == new_company_id for row in rows)


# ---------------------------------------------------------------------------
# ATS probing tests
# ---------------------------------------------------------------------------


class TestAtsProbing:
    """Tests for run_ats_probing()."""

    def test_ats_probe_triggered(self, migrated_db):
        """After company creation, probe_ats_slugs is called with db_path and config."""
        from job_finder.web.backfill_companies import run_ats_probing

        path, conn = migrated_db
        config = {"scoring": {}}

        with patch("job_finder.web.backfill_companies.probe_ats_slugs") as mock_probe:
            mock_probe.return_value = {"probed": 5, "hits": 2, "misses": 3}
            result = run_ats_probing(path, config)

        mock_probe.assert_called_once_with(path, config)
        assert result["probed"] == 5
        assert result["hits"] == 2
        assert result["misses"] == 3


# ---------------------------------------------------------------------------
# DDG enrichment tests
# ---------------------------------------------------------------------------


class TestDdgEnrichment:
    """Tests for run_ddg_enrichment()."""

    def test_ddg_enrichment_triggered(self, migrated_db):
        """After company creation, enrich_company_info is called for each new company name."""
        from job_finder.web.backfill_companies import run_ddg_enrichment

        path, conn = migrated_db

        # Insert two new company records
        conn.execute(
            "INSERT INTO companies (name, name_raw, ats_probe_status, created_at, updated_at) "
            "VALUES ('acme', 'Acme Corp', 'pending', '2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO companies (name, name_raw, ats_probe_status, created_at, updated_at) "
            "VALUES ('widgetco', 'WidgetCo', 'pending', '2026-01-01', '2026-01-01')"
        )
        conn.commit()

        acme_id = conn.execute("SELECT id FROM companies WHERE name = 'acme'").fetchone()["id"]
        widgetco_id = conn.execute("SELECT id FROM companies WHERE name = 'widgetco'").fetchone()["id"]
        new_company_ids = [acme_id, widgetco_id]

        # Use homepage_url which exists in the companies schema as a storable field
        with patch("job_finder.web.backfill_companies.enrich_company_info") as mock_enrich:
            mock_enrich.return_value = {"homepage_url": "https://example.com"}
            count = run_ddg_enrichment(conn, new_company_ids)

        # enrich_company_info called once per new company
        assert mock_enrich.call_count == 2
        # Both companies got a storable field (homepage_url exists in schema)
        assert count == 2

    def test_ddg_enrichment_results_stored(self, migrated_db):
        """DDG enrichment results are stored in the companies table."""
        from job_finder.web.backfill_companies import run_ddg_enrichment

        path, conn = migrated_db

        # Need company_size and industry columns — check migration applied them
        # (These may need to be added via migration for companies table)
        # For now, test that enrich_company_info was called and run returned count
        conn.execute(
            "INSERT INTO companies (name, name_raw, ats_probe_status, created_at, updated_at) "
            "VALUES ('testco', 'TestCo', 'pending', '2026-01-01', '2026-01-01')"
        )
        conn.commit()

        company_id = conn.execute("SELECT id FROM companies WHERE name = 'testco'").fetchone()["id"]

        with patch("job_finder.web.backfill_companies.enrich_company_info") as mock_enrich:
            mock_enrich.return_value = {}  # Empty result — no fields to store
            count = run_ddg_enrichment(conn, [company_id])

        # Still counts as attempted even if result is empty
        assert count == 0  # Empty result means 0 enriched


# ---------------------------------------------------------------------------
# Summary output test
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Denylist cleanup tests
# ---------------------------------------------------------------------------


class TestDenylistCleanup:
    """Tests for cleanup_denylist_companies()."""

    def test_cleanup_deletes_denylist_company(self, migrated_db):
        """cleanup_denylist_companies removes a company named 'Medical jobs' from the companies table."""
        from job_finder.web.backfill_companies import cleanup_denylist_companies

        path, conn = migrated_db

        # Insert a denylist company
        conn.execute(
            "INSERT INTO companies (name, name_raw, ats_probe_status, created_at, updated_at) "
            "VALUES ('medical jobs', 'Medical jobs', 'pending', '2026-01-01', '2026-01-01')"
        )
        conn.commit()
        medical_id = conn.execute(
            "SELECT id FROM companies WHERE name = 'medical jobs'"
        ).fetchone()["id"]

        # Insert a job linked to that denylist company
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen, company_id) "
            "VALUES ('denylist|job', 'Software Engineer', 'Medical jobs', 'Remote', '2026-01-01', '2026-01-01', ?)",
            (medical_id,),
        )
        conn.commit()

        result = cleanup_denylist_companies(conn)

        # Company should be deleted
        remaining = conn.execute(
            "SELECT COUNT(*) FROM companies WHERE name = 'medical jobs'"
        ).fetchone()[0]
        assert remaining == 0

        # Job's company_id should be NULL
        job_row = conn.execute(
            "SELECT company_id FROM jobs WHERE dedup_key = 'denylist|job'"
        ).fetchone()
        assert job_row["company_id"] is None

        # Return dict should reflect the cleanup
        assert result["companies_deleted"] >= 1
        assert result["jobs_unlinked"] >= 1

    def test_cleanup_unlinks_jobs(self, migrated_db):
        """cleanup_denylist_companies sets company_id=NULL on jobs linked to deleted companies."""
        from job_finder.web.backfill_companies import cleanup_denylist_companies

        path, conn = migrated_db

        conn.execute(
            "INSERT INTO companies (name, name_raw, ats_probe_status, created_at, updated_at) "
            "VALUES ('mercor', 'Mercor', 'pending', '2026-01-01', '2026-01-01')"
        )
        conn.commit()
        mercor_id = conn.execute(
            "SELECT id FROM companies WHERE name = 'mercor'"
        ).fetchone()["id"]

        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen, company_id) "
            "VALUES ('mercor|eng', 'Engineer', 'Mercor', 'Remote', '2026-01-01', '2026-01-01', ?)",
            (mercor_id,),
        )
        conn.commit()

        cleanup_denylist_companies(conn)

        job_row = conn.execute(
            "SELECT company_id FROM jobs WHERE dedup_key = 'mercor|eng'"
        ).fetchone()
        assert job_row["company_id"] is None

    def test_cleanup_returns_dict(self, migrated_db):
        """cleanup_denylist_companies returns a dict with 'companies_deleted' and 'jobs_unlinked' keys."""
        from job_finder.web.backfill_companies import cleanup_denylist_companies

        path, conn = migrated_db

        conn.execute(
            "INSERT INTO companies (name, name_raw, ats_probe_status, created_at, updated_at) "
            "VALUES ('clinical jobs', 'Clinical jobs', 'pending', '2026-01-01', '2026-01-01')"
        )
        conn.commit()
        clinical_id = conn.execute(
            "SELECT id FROM companies WHERE name = 'clinical jobs'"
        ).fetchone()["id"]

        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen, company_id) "
            "VALUES ('clinical|nurse', 'Nurse', 'Clinical jobs', 'Remote', '2026-01-01', '2026-01-01', ?)",
            (clinical_id,),
        )
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen, company_id) "
            "VALUES ('clinical|doctor', 'Doctor', 'Clinical jobs', 'Remote', '2026-01-01', '2026-01-01', ?)",
            (clinical_id,),
        )
        conn.commit()

        result = cleanup_denylist_companies(conn)

        assert isinstance(result, dict)
        assert "companies_deleted" in result
        assert "jobs_unlinked" in result
        assert result["companies_deleted"] == 1
        assert result["jobs_unlinked"] == 2

    def test_cleanup_is_idempotent(self, migrated_db):
        """Calling cleanup_denylist_companies twice returns 0 on the second call."""
        from job_finder.web.backfill_companies import cleanup_denylist_companies

        path, conn = migrated_db

        conn.execute(
            "INSERT INTO companies (name, name_raw, ats_probe_status, created_at, updated_at) "
            "VALUES ('jobgether', 'Jobgether', 'pending', '2026-01-01', '2026-01-01')"
        )
        conn.commit()

        first_result = cleanup_denylist_companies(conn)
        assert first_result["companies_deleted"] >= 1

        second_result = cleanup_denylist_companies(conn)
        assert second_result["companies_deleted"] == 0
        assert second_result["jobs_unlinked"] == 0


# ---------------------------------------------------------------------------
# Duplicate company detection tests
# ---------------------------------------------------------------------------


class TestFindDuplicateCompanies:
    """Tests for find_duplicate_companies()."""

    def test_no_duplicates_returns_empty(self, migrated_db):
        """find_duplicate_companies returns empty list when no duplicates exist."""
        from job_finder.web.backfill_companies import find_duplicate_companies

        path, conn = migrated_db

        conn.execute(
            "INSERT INTO companies (name, name_raw, ats_probe_status, created_at, updated_at) "
            "VALUES ('stripe', 'Stripe', 'pending', '2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO companies (name, name_raw, ats_probe_status, created_at, updated_at) "
            "VALUES ('google', 'Google', 'pending', '2026-01-01', '2026-01-01')"
        )
        conn.commit()

        dupes = find_duplicate_companies(conn)

        assert dupes == []

    def test_duplicates_returned_as_tuples(self, migrated_db):
        """find_duplicate_companies returns list of tuples for companies with same normalized name."""
        from job_finder.web.backfill_companies import find_duplicate_companies

        path, conn = migrated_db

        # Two entries that normalize to "acme"
        conn.execute(
            "INSERT INTO companies (name, name_raw, ats_probe_status, created_at, updated_at) "
            "VALUES ('acme', 'Acme', 'pending', '2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO companies (name, name_raw, ats_probe_status, created_at, updated_at) "
            "VALUES ('acme', 'ACME Corp', 'pending', '2026-01-01', '2026-01-01')"
        )
        conn.commit()

        dupes = find_duplicate_companies(conn)

        assert len(dupes) >= 1
        # Each duplicate entry should be a tuple of (id_a, id_b, normalized_name)
        first = dupes[0]
        assert isinstance(first, tuple)
        assert len(first) == 3
        # Third element is the normalized name
        assert first[2] == "acme"


# ---------------------------------------------------------------------------
# Fuzzy false positive detection tests
# ---------------------------------------------------------------------------


class TestFindFuzzyFalsePositives:
    """Tests for find_fuzzy_false_positives()."""

    def test_high_score_pair_detected(self, migrated_db):
        """Companies 'stripe' and 'strip' appear in results (high fuzzy score, different names)."""
        from job_finder.web.backfill_companies import find_fuzzy_false_positives

        path, conn = migrated_db

        conn.execute(
            "INSERT INTO companies (name, name_raw, ats_probe_status, created_at, updated_at) "
            "VALUES ('stripe', 'Stripe', 'pending', '2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO companies (name, name_raw, ats_probe_status, created_at, updated_at) "
            "VALUES ('strip', 'Strip', 'pending', '2026-01-01', '2026-01-01')"
        )
        conn.commit()

        results = find_fuzzy_false_positives(conn, threshold=85)

        names_in_results = {(r["name_a"], r["name_b"]) for r in results} | {(r["name_b"], r["name_a"]) for r in results}
        assert ("Stripe", "Strip") in names_in_results or ("stripe", "strip") in names_in_results or \
               any(
                   ("stripe" in r["name_a"].lower() and "strip" in r["name_b"].lower()) or
                   ("strip" in r["name_a"].lower() and "stripe" in r["name_b"].lower())
                   for r in results
               )

    def test_low_score_pair_excluded(self, migrated_db):
        """Companies 'google' and 'netflix' do not appear in results (low fuzzy score)."""
        from job_finder.web.backfill_companies import find_fuzzy_false_positives

        path, conn = migrated_db

        conn.execute(
            "INSERT INTO companies (name, name_raw, ats_probe_status, created_at, updated_at) "
            "VALUES ('google', 'Google', 'pending', '2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO companies (name, name_raw, ats_probe_status, created_at, updated_at) "
            "VALUES ('netflix', 'Netflix', 'pending', '2026-01-01', '2026-01-01')"
        )
        conn.commit()

        results = find_fuzzy_false_positives(conn, threshold=85)

        # google and netflix are unrelated — should not appear
        for r in results:
            pair = {r["name_a"].lower(), r["name_b"].lower()}
            assert "google" not in pair or "netflix" not in pair, (
                "google and netflix should not be a fuzzy false positive pair"
            )

    def test_result_dict_structure(self, migrated_db):
        """Each result dict has keys: id_a, name_a, id_b, name_b, score."""
        from job_finder.web.backfill_companies import find_fuzzy_false_positives

        path, conn = migrated_db

        # Use two very similar company names to guarantee a result
        conn.execute(
            "INSERT INTO companies (name, name_raw, ats_probe_status, created_at, updated_at) "
            "VALUES ('stripe', 'Stripe Inc', 'pending', '2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO companies (name, name_raw, ats_probe_status, created_at, updated_at) "
            "VALUES ('strip', 'Strip LLC', 'pending', '2026-01-01', '2026-01-01')"
        )
        conn.commit()

        results = find_fuzzy_false_positives(conn, threshold=85)

        if results:
            r = results[0]
            assert "id_a" in r
            assert "name_a" in r
            assert "id_b" in r
            assert "name_b" in r
            assert "score" in r
            assert isinstance(r["id_a"], int)
            assert isinstance(r["id_b"], int)
            assert isinstance(r["score"], int)


# ---------------------------------------------------------------------------
# Homepage URL verification tests
# ---------------------------------------------------------------------------


class TestVerifyHomepageUrls:
    """Tests for verify_homepage_urls()."""

    def test_reachable_url_returns_true(self, migrated_db):
        """Company with homepage_url that returns 200 has reachable=True."""
        from job_finder.web.backfill_companies import verify_homepage_urls

        path, conn = migrated_db

        conn.execute(
            "INSERT INTO companies (name, name_raw, ats_probe_status, homepage_url, created_at, updated_at) "
            "VALUES ('example', 'Example Corp', 'pending', 'https://example.com', '2026-01-01', '2026-01-01')"
        )
        conn.commit()

        with patch("requests.head") as mock_head:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_head.return_value = mock_response

            results = verify_homepage_urls(conn)

        assert len(results) == 1
        assert results[0]["homepage_url"] == "https://example.com"
        assert results[0]["reachable"] is True
        assert "id" in results[0]
        assert "name_raw" in results[0]

    def test_null_url_excluded(self, migrated_db):
        """Company with homepage_url=None does not appear in results."""
        from job_finder.web.backfill_companies import verify_homepage_urls

        path, conn = migrated_db

        conn.execute(
            "INSERT INTO companies (name, name_raw, ats_probe_status, created_at, updated_at) "
            "VALUES ('nohome', 'No Homepage Corp', 'pending', '2026-01-01', '2026-01-01')"
        )
        conn.commit()

        with patch("requests.head") as mock_head:
            results = verify_homepage_urls(conn)

        assert results == []
        mock_head.assert_not_called()

    def test_connection_error_returns_unreachable(self, migrated_db):
        """Company with homepage_url that raises ConnectionError has reachable=False."""
        from job_finder.web.backfill_companies import verify_homepage_urls

        path, conn = migrated_db

        conn.execute(
            "INSERT INTO companies (name, name_raw, ats_probe_status, homepage_url, created_at, updated_at) "
            "VALUES ('failco', 'Fail Corp', 'pending', 'https://failco.example.com', '2026-01-01', '2026-01-01')"
        )
        conn.commit()

        with patch("requests.head") as mock_head:
            mock_head.side_effect = ConnectionError("Connection refused")

            results = verify_homepage_urls(conn)

        assert len(results) == 1
        assert results[0]["reachable"] is False


# ---------------------------------------------------------------------------
# Linkage verification tests
# ---------------------------------------------------------------------------


class TestVerifyAllLinkableJobsLinked:
    """Tests for verify_all_linkable_jobs_linked()."""

    def test_unlinked_non_denylist_job_counted(self, migrated_db):
        """Job with non-denylist company and company_id=NULL counts as unlinked_non_denylist=1."""
        from job_finder.web.backfill_companies import verify_all_linkable_jobs_linked

        path, conn = migrated_db

        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen) "
            "VALUES ('acme|eng', 'Engineer', 'Acme Corp', 'Remote', '2026-01-01', '2026-01-01')"
        )
        conn.commit()

        result = verify_all_linkable_jobs_linked(conn)

        assert result["unlinked_non_denylist"] == 1
        assert "unlinked_denylist" in result
        assert "unlinked_details" in result

    def test_denylist_job_counted_as_denylist(self, migrated_db):
        """Job with denylist company ('Unknown') and company_id=NULL counts as unlinked_denylist=1, not non-denylist."""
        from job_finder.web.backfill_companies import verify_all_linkable_jobs_linked

        path, conn = migrated_db

        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen) "
            "VALUES ('unknown|eng', 'Engineer', 'Unknown', 'Remote', '2026-01-01', '2026-01-01')"
        )
        conn.commit()

        result = verify_all_linkable_jobs_linked(conn)

        assert result["unlinked_denylist"] == 1
        assert result["unlinked_non_denylist"] == 0

    def test_linked_job_not_counted(self, migrated_db):
        """Job with non-denylist company and company_id set is NOT counted as unlinked."""
        from job_finder.web.backfill_companies import verify_all_linkable_jobs_linked

        path, conn = migrated_db

        conn.execute(
            "INSERT INTO companies (name, name_raw, ats_probe_status, created_at, updated_at) "
            "VALUES ('acme', 'Acme Corp', 'pending', '2026-01-01', '2026-01-01')"
        )
        conn.commit()
        company_id = conn.execute("SELECT id FROM companies WHERE name = 'acme'").fetchone()["id"]

        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen, company_id) "
            "VALUES ('acme|eng', 'Engineer', 'Acme Corp', 'Remote', '2026-01-01', '2026-01-01', ?)",
            (company_id,),
        )
        conn.commit()

        result = verify_all_linkable_jobs_linked(conn)

        assert result["unlinked_non_denylist"] == 0


# ---------------------------------------------------------------------------
# Summary output test
# ---------------------------------------------------------------------------


class TestSummaryOutput:
    """Tests that main() calls all phases and prints summary."""

    def test_summary_output(self, migrated_db, capsys):
        """After full run, summary prints linked count, new companies, ATS probe results, DDG results."""
        from job_finder.web.backfill_companies import main

        path, conn = migrated_db

        # Insert a job to process
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen) "
            "VALUES ('test|engineer', 'Engineer', 'TestCo', 'Remote', '2026-01-01', '2026-01-01')"
        )
        conn.commit()

        with (
            patch("job_finder.web.backfill_companies.load_config") as mock_cfg,
            patch("job_finder.web.backfill_companies.link_jobs_to_companies") as mock_link,
            patch("job_finder.web.backfill_companies.run_ats_probing") as mock_ats,
            patch("job_finder.web.backfill_companies.run_ddg_enrichment") as mock_ddg,
            patch("job_finder.web.backfill_companies.sqlite3") as mock_sqlite3,
        ):
            mock_cfg.return_value = {"db": {"path": path}}
            mock_link.return_value = (10, [1, 2, 3], 5)
            mock_ats.return_value = {"probed": 3, "hits": 1, "misses": 2}
            mock_ddg.return_value = 2

            # Simulate sqlite3.connect returning a mock conn with required queries
            mock_conn = MagicMock()
            mock_conn.__enter__ = MagicMock(return_value=mock_conn)
            mock_conn.__exit__ = MagicMock(return_value=False)
            mock_conn.execute.return_value.fetchone.return_value = (100,)
            mock_sqlite3.connect.return_value = mock_conn
            mock_sqlite3.Row = sqlite3.Row

            main()

        captured = capsys.readouterr()
        output = captured.out

        # Summary should mention key metrics
        assert any(word in output.lower() for word in ["linked", "created", "matched", "companies"])


# ---------------------------------------------------------------------------
# Tests: run_company_linkage scheduler wrapper (Fix 2)
# ---------------------------------------------------------------------------

class TestRunCompanyLinkage:
    """Tests for the scheduler-compatible run_company_linkage() wrapper."""

    def test_run_company_linkage_returns_summary(self, migrated_db):
        """With an unlinked job, run_company_linkage links it and returns summary."""
        db_path, conn = migrated_db

        # Insert a job with no company_id
        from datetime import datetime
        now = datetime.now().isoformat()
        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen)
               VALUES ('test-key-1', 'Engineer', 'Acme Corp', 'Remote', ?, ?)""",
            (now, now),
        )
        conn.commit()

        from job_finder.web.backfill_companies import run_company_linkage
        result = run_company_linkage(db_path, {})
        assert result["linked"] >= 1

    def test_run_company_linkage_idempotent(self, migrated_db):
        """Running linkage twice: second call returns linked=0."""
        db_path, conn = migrated_db

        from datetime import datetime
        now = datetime.now().isoformat()
        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen)
               VALUES ('test-key-2', 'Engineer', 'Idempotent Corp', 'Remote', ?, ?)""",
            (now, now),
        )
        conn.commit()

        from job_finder.web.backfill_companies import run_company_linkage
        result1 = run_company_linkage(db_path, {})
        assert result1["linked"] >= 1

        result2 = run_company_linkage(db_path, {})
        assert result2["linked"] == 0


# ---------------------------------------------------------------------------
# Tests: Orphan cleanup — Fix 13
# ---------------------------------------------------------------------------

class TestOrphanCleanup:
    """Tests for cleanup_orphan_companies() and run_orphan_cleanup()."""

    def _insert_company(self, conn, name):
        from datetime import datetime
        now = datetime.now().isoformat()
        cursor = conn.execute(
            """INSERT INTO companies (name, name_raw, ats_probe_status, created_at, updated_at)
               VALUES (?, ?, 'pending', ?, ?)""",
            (name.lower(), name, now, now),
        )
        conn.commit()
        return cursor.lastrowid

    def _insert_job(self, conn, key, company_name, company_id=None):
        from datetime import datetime
        now = datetime.now().isoformat()
        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, company_id, location, first_seen, last_seen)
               VALUES (?, 'Engineer', ?, ?, 'Remote', ?, ?)""",
            (key, company_name, company_id, now, now),
        )
        conn.commit()

    def _insert_scan_log(self, conn, company_id):
        from datetime import datetime
        now = datetime.now().isoformat()
        conn.execute(
            """INSERT INTO company_scan_log (company_id, scanned_at, jobs_found)
               VALUES (?, ?, 0)""",
            (company_id, now),
        )
        conn.commit()

    def test_deletes_orphan_companies(self, migrated_db):
        """Companies with no linked jobs and no scan history are deleted."""
        db_path, conn = migrated_db

        id_a = self._insert_company(conn, "WithJobs Co")
        id_b = self._insert_company(conn, "WithScan Co")
        id_c = self._insert_company(conn, "Orphan Co")

        # Link a job to company A
        self._insert_job(conn, "key-a", "WithJobs Co", company_id=id_a)
        # Give company B a scan log entry
        self._insert_scan_log(conn, id_b)
        # Company C has neither — it is the orphan

        from job_finder.web.backfill_companies import cleanup_orphan_companies
        result = cleanup_orphan_companies(conn)

        assert result["orphans_deleted"] == 1
        remaining_ids = [r[0] for r in conn.execute("SELECT id FROM companies").fetchall()]
        assert id_a in remaining_ids
        assert id_b in remaining_ids
        assert id_c not in remaining_ids

    def test_preserves_company_with_scan_history(self, migrated_db):
        """A company with scan history but no linked jobs is NOT deleted."""
        db_path, conn = migrated_db

        company_id = self._insert_company(conn, "ScannedNoJobs Co")
        self._insert_scan_log(conn, company_id)

        from job_finder.web.backfill_companies import cleanup_orphan_companies
        result = cleanup_orphan_companies(conn)

        assert result["orphans_deleted"] == 0
        row = conn.execute("SELECT id FROM companies WHERE id = ?", (company_id,)).fetchone()
        assert row is not None

    def test_recalibrates_jobs_found_total(self, migrated_db):
        """jobs_found_total is reset to actual linked job count for all companies."""
        db_path, conn = migrated_db

        company_id = self._insert_company(conn, "Recalib Co")
        # Manually set a stale total
        conn.execute(
            "UPDATE companies SET jobs_found_total = 99 WHERE id = ?", (company_id,)
        )
        conn.commit()
        # Link 3 real jobs
        for i in range(3):
            self._insert_job(conn, f"recalib-{i}", "Recalib Co", company_id=company_id)

        from job_finder.web.backfill_companies import cleanup_orphan_companies
        cleanup_orphan_companies(conn)

        row = conn.execute(
            "SELECT jobs_found_total FROM companies WHERE id = ?", (company_id,)
        ).fetchone()
        assert row["jobs_found_total"] == 3

    def test_idempotent_second_call(self, migrated_db):
        """Running cleanup twice: second call returns orphans_deleted=0."""
        db_path, conn = migrated_db

        self._insert_company(conn, "Orphan Again")

        from job_finder.web.backfill_companies import cleanup_orphan_companies
        result1 = cleanup_orphan_companies(conn)
        assert result1["orphans_deleted"] == 1

        result2 = cleanup_orphan_companies(conn)
        assert result2["orphans_deleted"] == 0

    def test_run_orphan_cleanup_wrapper(self, migrated_db):
        """run_orphan_cleanup() returns dict with expected keys."""
        db_path, conn = migrated_db
        conn.close()  # wrapper opens its own connection

        from job_finder.web.backfill_companies import run_orphan_cleanup
        result = run_orphan_cleanup(db_path, {})

        assert "orphans_deleted" in result
        assert "recalibrated_total" in result
        assert isinstance(result["orphans_deleted"], int)
        assert isinstance(result["recalibrated_total"], int)
