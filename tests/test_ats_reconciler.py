"""Tests for batch ATS reconciliation (Phase B of the staleness orchestrator).

Covers:
- Posting-ID extraction for all 5 supported platforms.
- Set-diff happy path: tracked job's URL matches live board → LIVE + last_seen refresh.
- Set-diff expired path: tracked job's ID missing from live board → EXPIRED + archive.
- Safety guards: scan-empty, scan-exception, unsupported-platform,
  Workday completeness gate (incomplete board → skip, no writes),
  scan returns postings but none parseable.
- reconcile_all_companies orchestrator aggregates per-company results.
"""

import logging
import sqlite3
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# _extract_posting_id
# ---------------------------------------------------------------------------


class TestExtractPostingId:
    def test_lever_url(self):
        from job_finder.web.ats_reconciler import _extract_posting_id

        assert (
            _extract_posting_id("https://jobs.lever.co/acme/abc-123-def", "lever") == "abc-123-def"
        )

    def test_greenhouse_url(self):
        from job_finder.web.ats_reconciler import _extract_posting_id

        assert (
            _extract_posting_id("https://boards.greenhouse.io/airbnb/jobs/12345", "greenhouse")
            == "12345"
        )

    def test_ashby_url_case_sensitive(self):
        from job_finder.web.ats_reconciler import _extract_posting_id

        # Ashby slugs are case-sensitive — pattern preserves case
        assert (
            _extract_posting_id("https://jobs.ashbyhq.com/OpenAI/abc-123-def", "ashby")
            == "abc-123-def"
        )

    def test_workday_url_extracts_tail(self):
        from job_finder.web.ats_reconciler import _extract_posting_id

        url = "https://walmart.wd5.myworkdayjobs.com/en-US/WalmartExternal/job/Software-Engineer_R-123456"
        assert _extract_posting_id(url, "workday") == "Software-Engineer_R-123456"

    def test_workday_url_handles_location_prefix(self):
        """Real stored URLs often include a location segment like
        '.../job/Remote-United-States/Senior-Data-Scientist_JR101664'.
        The regex must capture only the final (role-slug) segment."""
        from job_finder.web.ats_reconciler import _extract_posting_id

        url = "https://stord.wd503.myworkdayjobs.com/Stord_External_Career/job/Remote-United-States/Senior-Data-Scientist_JR101664"
        assert _extract_posting_id(url, "workday") == "Senior-Data-Scientist_JR101664"

    def test_workday_url_survives_scan_doubled_job_path(self):
        """scan_workday has a quirk that produces '.../job//job/<location>/<slug>'
        because Workday's externalPath already starts with /job/. The regex must
        still land on the final (role-slug) segment so scan and stored URLs
        normalize to the same ID."""
        from job_finder.web.ats_reconciler import _extract_posting_id

        url = "https://stord.wd503.myworkdayjobs.com/en-US/Stord_External_Career/job//job/Remote-United-States/Data-Analyst_JR101972"
        assert _extract_posting_id(url, "workday") == "Data-Analyst_JR101972"

    def test_workday_regex_rejects_non_workday_domains(self):
        """Guard against the 'unanchored /job/' bug: Google search and
        ZipRecruiter URLs contain '/job/' substrings but are not Workday
        postings. They must return None so they can't pollute live_id_set
        or a tracked job's job_ids set."""
        from job_finder.web.ats_reconciler import _extract_posting_id

        assert (
            _extract_posting_id("https://www.google.com/search?source=sh/x/job/li/m1/1", "workday")
            is None
        )
        assert (
            _extract_posting_id(
                "https://www.ziprecruiter.com/c/Walmart/Job/Manager,-Advanced-Analytics/-in-Oakland,CA",
                "workday",
            )
            is None
        )
        assert (
            _extract_posting_id("https://www.linkedin.com/jobs/view/4379178105/", "workday")
            is None
        )

    def test_smartrecruiters_url(self):
        from job_finder.web.ats_reconciler import _extract_posting_id

        assert (
            _extract_posting_id(
                "https://jobs.smartrecruiters.com/AbbVie/744000123456789", "smartrecruiters"
            )
            == "744000123456789"
        )

    def test_smartrecruiters_url_strips_slug_suffix(self):
        """Stored URLs often keep the '-<slug-text>' SEO suffix that
        scan_smartrecruiters' constructed URLs omit. The regex must stop
        at the first dash so both forms normalize to the same ID."""
        from job_finder.web.ats_reconciler import _extract_posting_id

        assert (
            _extract_posting_id(
                "https://jobs.smartrecruiters.com/WNSGlobalServices144/744000118077858-assistant-manager-analytics",
                "smartrecruiters",
            )
            == "744000118077858"
        )

    def test_unknown_platform_returns_none(self):
        from job_finder.web.ats_reconciler import _extract_posting_id

        assert _extract_posting_id("https://jobs.lever.co/acme/abc", "icims") is None

    def test_non_matching_url_returns_none(self):
        from job_finder.web.ats_reconciler import _extract_posting_id

        assert _extract_posting_id("https://random.com/jobs/x", "lever") is None


# ---------------------------------------------------------------------------
# Test DB helper
# ---------------------------------------------------------------------------


def _setup_company_and_jobs(
    path,
    *,
    platform="lever",
    slug="acme",
    job_urls=None,
    old_last_seen=False,
):
    """Seed a migrated DB with one company and N discovered jobs.

    Args:
        path: tmp_db_path
        platform: ats_platform value
        slug: ats_slug value
        job_urls: list of source URLs (one job per URL)
        old_last_seen: if True, use a timestamp long in the past (to test refresh)

    Returns: (company_id, [dedup_keys])
    """
    from job_finder.web.db_migrate import run_migrations

    run_migrations(path)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    now_iso = datetime.now(UTC).isoformat()
    old_iso = "2026-01-01T00:00:00" if old_last_seen else now_iso

    conn.execute(
        "INSERT INTO companies (name, name_raw, homepage_url, ats_platform, ats_slug, "
        "ats_probe_status, scan_enabled, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
        ("acme", "Acme Corp", "https://acme.com", platform, slug, "hit", now_iso, now_iso),
    )
    company_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    dedup_keys = []
    for i, url in enumerate(job_urls or []):
        dk = f"acme|job{i}"
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, first_seen, "
            "last_seen, pipeline_status, company_id, source_urls) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                dk,
                f"Job {i}",
                "Acme Corp",
                "Remote",
                old_iso,
                old_iso,
                "discovered",
                company_id,
                f'["{url}"]',
            ),
        )
        dedup_keys.append(dk)

    conn.commit()
    conn.close()
    return company_id, dedup_keys


# ---------------------------------------------------------------------------
# reconcile_company
# ---------------------------------------------------------------------------


class TestReconcileCompany:
    @patch("job_finder.web.ats_reconciler.run_platform_scan")
    def test_live_job_refreshes_last_seen_and_clears_is_stale(
        self,
        mock_scan,
        tmp_db_path,
    ):
        """A tracked job whose URL posting-ID matches the live board is
        marked live, last_seen is refreshed to ~now, and is_stale is cleared."""
        from job_finder.web.ats_reconciler import reconcile_company
        from job_finder.web.db_helpers import standalone_connection

        url = "https://jobs.lever.co/acme/abc-123-def"
        _, dedup_keys = _setup_company_and_jobs(
            tmp_db_path,
            job_urls=[url],
            old_last_seen=True,
        )
        # Artificially mark job as stale to verify Phase B clears it
        conn = sqlite3.connect(tmp_db_path)
        conn.execute("UPDATE jobs SET is_stale = 1 WHERE dedup_key = ?", (dedup_keys[0],))
        conn.commit()
        conn.close()

        mock_scan.return_value = [
            {"source_url": url, "title": "Job 0", "company_source": "Lever"},
        ]

        with standalone_connection(tmp_db_path) as conn:
            company_row = dict(
                conn.execute("SELECT id, ats_platform, ats_slug FROM companies").fetchone()
            )
            result = reconcile_company(conn, company_row)

        assert result["live"] == 1
        assert result["expired"] == 0
        assert result["skipped"] is False

        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT expiry_status, is_stale, last_seen FROM jobs WHERE dedup_key = ?",
            (dedup_keys[0],),
        ).fetchone()
        conn.close()
        assert row["expiry_status"] == "live"
        assert row["is_stale"] == 0
        # last_seen must be a fresh timestamp (not the old one we seeded)
        assert not row["last_seen"].startswith("2026-01-01")

    @patch("job_finder.web.ats_reconciler.run_platform_scan")
    def test_missing_job_is_archived(self, mock_scan, tmp_db_path):
        """A tracked job whose posting-ID is NOT in the live board is
        marked expired and archived via update_pipeline_status."""
        from job_finder.web.ats_reconciler import reconcile_company
        from job_finder.web.db_helpers import standalone_connection

        _, dedup_keys = _setup_company_and_jobs(
            tmp_db_path,
            job_urls=["https://jobs.lever.co/acme/111-222-aaa"],
        )
        mock_scan.return_value = [
            # Different hex-only ID — tracked job is NOT on the live board
            {"source_url": "https://jobs.lever.co/acme/333-444-bbb"},
        ]

        with standalone_connection(tmp_db_path) as conn:
            company_row = dict(
                conn.execute("SELECT id, ats_platform, ats_slug FROM companies").fetchone()
            )
            result = reconcile_company(conn, company_row)

        assert result["expired"] == 1
        assert result["live"] == 0

        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT expiry_status, pipeline_status FROM jobs WHERE dedup_key = ?",
            (dedup_keys[0],),
        ).fetchone()
        conn.close()
        assert row["expiry_status"] == "expired"
        assert row["pipeline_status"] == "archived"

    @patch("job_finder.web.ats_reconciler.run_platform_scan")
    def test_expired_job_clears_direct_url_and_resets_attempts(self, mock_scan, tmp_db_path):
        """Phase 5: when a job expires, a resolved primary-source link is dead
        (its posting dropped off the board). NULL direct_url/direct_url_confidence
        and reset direct_url_attempts so a future repost re-resolves."""
        from job_finder.web.ats_reconciler import reconcile_company
        from job_finder.web.db_helpers import standalone_connection

        _, dedup_keys = _setup_company_and_jobs(
            tmp_db_path,
            job_urls=["https://jobs.lever.co/acme/111-222-aaa"],
        )
        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            "UPDATE jobs SET direct_url = ?, direct_url_confidence = 'strict', "
            "direct_url_attempts = 2 WHERE dedup_key = ?",
            ("https://jobs.lever.co/acme/111-222-aaa", dedup_keys[0]),
        )
        conn.commit()
        conn.close()

        mock_scan.return_value = [
            {"source_url": "https://jobs.lever.co/acme/333-444-bbb"},  # different ID → gone
        ]

        with standalone_connection(tmp_db_path) as conn:
            company_row = dict(
                conn.execute("SELECT id, ats_platform, ats_slug FROM companies").fetchone()
            )
            result = reconcile_company(conn, company_row)

        assert result["expired"] == 1
        assert result["direct_url_cleared"] == 1

        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT direct_url, direct_url_confidence, direct_url_attempts, expiry_status "
            "FROM jobs WHERE dedup_key = ?",
            (dedup_keys[0],),
        ).fetchone()
        conn.close()
        assert row["expiry_status"] == "expired"
        assert row["direct_url"] is None
        assert row["direct_url_confidence"] is None
        assert row["direct_url_attempts"] == 0

    @patch("job_finder.web.ats_reconciler.run_platform_scan")
    def test_live_job_preserves_direct_url(self, mock_scan, tmp_db_path):
        """A still-live job keeps its resolved primary link untouched."""
        from job_finder.web.ats_reconciler import reconcile_company
        from job_finder.web.db_helpers import standalone_connection

        url = "https://jobs.lever.co/acme/abc-123-def"
        _, dedup_keys = _setup_company_and_jobs(tmp_db_path, job_urls=[url])
        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            "UPDATE jobs SET direct_url = ?, direct_url_confidence = 'strict' WHERE dedup_key = ?",
            (url, dedup_keys[0]),
        )
        conn.commit()
        conn.close()

        mock_scan.return_value = [{"source_url": url}]  # still on the board

        with standalone_connection(tmp_db_path) as conn:
            company_row = dict(
                conn.execute("SELECT id, ats_platform, ats_slug FROM companies").fetchone()
            )
            result = reconcile_company(conn, company_row)

        assert result["live"] == 1
        assert result["direct_url_cleared"] == 0

        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT direct_url, direct_url_confidence FROM jobs WHERE dedup_key = ?",
            (dedup_keys[0],),
        ).fetchone()
        conn.close()
        assert row["direct_url"] == url
        assert row["direct_url_confidence"] == "strict"

    @patch("job_finder.web.ats_reconciler.run_platform_scan")
    def test_expired_job_without_direct_url_leaves_attempts_untouched(
        self, mock_scan, tmp_db_path
    ):
        """The clear/reset is surgical: a job that never carried a direct_url is
        not counted as cleared and its direct_url_attempts are left alone (only
        rows with a now-dead primary link are reset)."""
        from job_finder.web.ats_reconciler import reconcile_company
        from job_finder.web.db_helpers import standalone_connection

        _, dedup_keys = _setup_company_and_jobs(
            tmp_db_path,
            job_urls=["https://jobs.lever.co/acme/111-222-aaa"],
        )
        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            "UPDATE jobs SET direct_url_attempts = 2 WHERE dedup_key = ?",
            (dedup_keys[0],),
        )
        conn.commit()
        conn.close()

        mock_scan.return_value = [{"source_url": "https://jobs.lever.co/acme/333-444-bbb"}]

        with standalone_connection(tmp_db_path) as conn:
            company_row = dict(
                conn.execute("SELECT id, ats_platform, ats_slug FROM companies").fetchone()
            )
            result = reconcile_company(conn, company_row)

        assert result["expired"] == 1
        assert result["direct_url_cleared"] == 0

        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT direct_url_attempts FROM jobs WHERE dedup_key = ?",
            (dedup_keys[0],),
        ).fetchone()
        conn.close()
        assert row["direct_url_attempts"] == 2  # untouched — no direct_url to clear

    @patch("job_finder.web.ats_reconciler.run_platform_scan")
    def test_scan_empty_skips_company(self, mock_scan, tmp_db_path):
        """Safety guard: scan returning [] must NOT mass-expire tracked jobs."""
        from job_finder.web.ats_reconciler import reconcile_company
        from job_finder.web.db_helpers import standalone_connection

        _setup_company_and_jobs(
            tmp_db_path,
            job_urls=["https://jobs.lever.co/acme/abc-123"],
        )
        mock_scan.return_value = []

        with standalone_connection(tmp_db_path) as conn:
            company_row = dict(
                conn.execute("SELECT id, ats_platform, ats_slug FROM companies").fetchone()
            )
            result = reconcile_company(conn, company_row)

        assert result["skipped"] is True
        assert result["skip_reason"] == "scan_empty"
        assert result["expired"] == 0  # no false-expire

    @patch("job_finder.web.ats_reconciler.run_platform_scan")
    def test_scan_exception_skips_company(self, mock_scan, tmp_db_path):
        """Safety guard: scan raising must NOT mass-expire tracked jobs."""
        from job_finder.web.ats_reconciler import reconcile_company
        from job_finder.web.db_helpers import standalone_connection

        _setup_company_and_jobs(
            tmp_db_path,
            job_urls=["https://jobs.lever.co/acme/abc-123"],
        )
        mock_scan.side_effect = RuntimeError("network blew up")

        with standalone_connection(tmp_db_path) as conn:
            company_row = dict(
                conn.execute("SELECT id, ats_platform, ats_slug FROM companies").fetchone()
            )
            result = reconcile_company(conn, company_row)

        assert result["skipped"] is True
        assert "scan_exception" in result["skip_reason"]
        assert result["expired"] == 0

    def test_unsupported_platform_skips_silently(self, tmp_db_path):
        """iCIMS/Phenom/UKG/custom: no scan_* exists; skip without network call."""
        from job_finder.web.ats_reconciler import reconcile_company
        from job_finder.web.db_helpers import standalone_connection

        _setup_company_and_jobs(
            tmp_db_path,
            platform="icims",
            slug="acme_icims",
            job_urls=["https://acme.icims.com/jobs/1234"],
        )

        with standalone_connection(tmp_db_path) as conn:
            company_row = dict(
                conn.execute("SELECT id, ats_platform, ats_slug FROM companies").fetchone()
            )
            result = reconcile_company(conn, company_row)

        assert result["skipped"] is True
        assert result["skip_reason"] == "unsupported_platform"

    @patch("job_finder.web.ats_reconciler._workday_live_id_set")
    def test_workday_incomplete_board_skips_no_writes(self, mock_wday, tmp_db_path):
        """Workday board that can't be fully paginated (total > cap) skips to avoid
        false-expire.  Zero expiry writes must be made for any tracked job."""
        from job_finder.web.ats_reconciler import reconcile_company
        from job_finder.web.db_helpers import standalone_connection

        url = (
            "https://walmart.wd5.myworkdayjobs.com/en-US/WalmartExternal/job/Software-Engineer_R-1"
        )
        _, dedup_keys = _setup_company_and_jobs(
            tmp_db_path,
            platform="workday",
            slug="walmart.wd5/WalmartExternal",
            job_urls=[url],
        )
        mock_wday.return_value = (set(), False)  # board incomplete

        with standalone_connection(tmp_db_path) as conn:
            company_row = dict(
                conn.execute("SELECT id, ats_platform, ats_slug FROM companies").fetchone()
            )
            result = reconcile_company(conn, company_row)

        assert result["skipped"] is True
        assert result["skip_reason"] == "workday_incomplete"
        assert result["expired"] == 0  # false-expire guard held

        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT expiry_status FROM jobs WHERE dedup_key = ?",
            (dedup_keys[0],),
        ).fetchone()
        conn.close()
        assert row["expiry_status"] is None  # no write happened

    @patch("job_finder.web.ats_platforms._platforms_workday.requests.post")
    def test_workday_over_cap_total_skips_via_completeness_gate(self, mock_post, tmp_db_path):
        """End-to-end: CXS returns total=500 (> 200 cap) → complete=False →
        reconcile skips with 'workday_incomplete', no DB writes."""
        from job_finder.web.ats_reconciler import reconcile_company
        from job_finder.web.db_helpers import standalone_connection

        url = "https://walmart.wd5.myworkdayjobs.com/en-US/WalmartExternal/job/R-1"
        _, dedup_keys = _setup_company_and_jobs(
            tmp_db_path,
            platform="workday",
            slug="walmart.wd5/WalmartExternal",
            job_urls=[url],
        )
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "total": 500,
            "jobPostings": [
                {"title": f"Job {i}", "externalPath": f"/job/Job-{i}_R-{i}"} for i in range(20)
            ],
        }
        mock_post.return_value = mock_resp

        with standalone_connection(tmp_db_path) as conn:
            company_row = dict(
                conn.execute("SELECT id, ats_platform, ats_slug FROM companies").fetchone()
            )
            result = reconcile_company(conn, company_row)

        assert result["skipped"] is True
        assert result["skip_reason"] == "workday_incomplete"
        assert result["expired"] == 0

        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT expiry_status FROM jobs WHERE dedup_key = ?",
            (dedup_keys[0],),
        ).fetchone()
        conn.close()
        assert row["expiry_status"] is None

    @patch("job_finder.web.ats_reconciler._workday_live_id_set")
    def test_workday_complete_board_expires_absentees_and_keeps_live(self, mock_wday, tmp_db_path):
        """Complete Workday board: job present on board stays live; absentee expires."""
        from job_finder.web.ats_reconciler import reconcile_company
        from job_finder.web.db_helpers import standalone_connection

        present_url = (
            "https://walmart.wd5.myworkdayjobs.com/en-US/WalmartExternal/job/Present-Job_R-1"
        )
        absent_url = (
            "https://walmart.wd5.myworkdayjobs.com/en-US/WalmartExternal/job/Absent-Job_R-2"
        )
        _, dedup_keys = _setup_company_and_jobs(
            tmp_db_path,
            platform="workday",
            slug="walmart.wd5/WalmartExternal",
            job_urls=[present_url, absent_url],
        )
        # Live board only includes Present-Job_R-1
        mock_wday.return_value = ({"Present-Job_R-1"}, True)

        with standalone_connection(tmp_db_path) as conn:
            company_row = dict(
                conn.execute("SELECT id, ats_platform, ats_slug FROM companies").fetchone()
            )
            result = reconcile_company(conn, company_row)

        assert result["skipped"] is False
        assert result["live"] == 1
        assert result["expired"] == 1

        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        rows = {
            r["dedup_key"]: dict(r)
            for r in conn.execute(
                "SELECT dedup_key, expiry_status, pipeline_status FROM jobs"
            ).fetchall()
        }
        conn.close()
        assert rows[dedup_keys[0]]["expiry_status"] == "live"  # present → live
        assert rows[dedup_keys[1]]["expiry_status"] == "expired"  # absent → expired
        assert rows[dedup_keys[1]]["pipeline_status"] == "archived"

    @patch("job_finder.web.ats_platforms._fetch_workday_description")
    @patch("job_finder.web.ats_platforms._platforms_workday.requests.post")
    def test_workday_reconcile_does_not_fetch_descriptions(
        self, mock_post, mock_desc_fetch, tmp_db_path
    ):
        """Workday reconciliation uses the CXS list endpoint only —
        per-job description GETs must NOT be issued during reconciliation."""
        from job_finder.web.ats_reconciler import reconcile_company
        from job_finder.web.db_helpers import standalone_connection

        url = "https://walmart.wd5.myworkdayjobs.com/en-US/WalmartExternal/job/Present-Job_R-1"
        _setup_company_and_jobs(
            tmp_db_path,
            platform="workday",
            slug="walmart.wd5/WalmartExternal",
            job_urls=[url],
        )
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "total": 1,
            "jobPostings": [{"title": "Present Job", "externalPath": "/job/Present-Job_R-1"}],
        }
        mock_post.return_value = mock_resp

        with standalone_connection(tmp_db_path) as conn:
            company_row = dict(
                conn.execute("SELECT id, ats_platform, ats_slug FROM companies").fetchone()
            )
            reconcile_company(conn, company_row)

        mock_desc_fetch.assert_not_called()

    @patch("job_finder.web.ats_reconciler.run_platform_scan")
    def test_scan_returns_postings_but_no_parseable_ids_skips(
        self,
        mock_scan,
        tmp_db_path,
    ):
        """If scan output has no source_urls matching the platform's ID
        pattern (URL format drift), skip rather than falsely expire."""
        from job_finder.web.ats_reconciler import reconcile_company
        from job_finder.web.db_helpers import standalone_connection

        _setup_company_and_jobs(
            tmp_db_path,
            job_urls=["https://jobs.lever.co/acme/abc-123"],
        )
        mock_scan.return_value = [
            # Malformed source_url that doesn't match _LEVER_POSTING_RE
            {"source_url": "https://careers.acme.com/role?id=789"},
        ]

        with standalone_connection(tmp_db_path) as conn:
            company_row = dict(
                conn.execute("SELECT id, ats_platform, ats_slug FROM companies").fetchone()
            )
            result = reconcile_company(conn, company_row)

        assert result["skipped"] is True
        assert result["skip_reason"] == "no_parseable_live_ids"

    @patch("job_finder.web.ats_reconciler.run_platform_scan")
    def test_unparseable_stored_url_defers_to_phase_c(self, mock_scan, tmp_db_path):
        """If a tracked job's own source_urls don't yield a parseable posting-ID
        (e.g. Gmail-mangled URL), the job is marked 'unparseable' and NOT
        archived — Phase C (cascade) handles it later."""
        from job_finder.web.ats_reconciler import reconcile_company
        from job_finder.web.db_helpers import standalone_connection

        _, dedup_keys = _setup_company_and_jobs(
            tmp_db_path,
            job_urls=["https://careers.acme.com/role?id=789"],  # can't parse as Lever
        )
        mock_scan.return_value = [
            {"source_url": "https://jobs.lever.co/acme/abc-def-123"},
        ]

        with standalone_connection(tmp_db_path) as conn:
            company_row = dict(
                conn.execute("SELECT id, ats_platform, ats_slug FROM companies").fetchone()
            )
            result = reconcile_company(conn, company_row)

        assert result["unparseable"] == 1
        assert result["expired"] == 0  # no false-expire

        # Job must remain discovered
        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT pipeline_status, expiry_status FROM jobs WHERE dedup_key = ?",
            (dedup_keys[0],),
        ).fetchone()
        conn.close()
        assert row["pipeline_status"] == "discovered"
        assert row["expiry_status"] is None

    @patch("job_finder.web.ats_reconciler.run_platform_scan")
    def test_source_id_rescues_unparseable_greenhouse_job(self, mock_scan, tmp_db_path):
        """Issue #218: a greenhouse-tracked job whose stored URLs are aggregator-only
        (no greenhouse posting-id segment) but whose `source_id` column carries the
        live-board id must be rescued from `unparseable` and classified live/expired.

        Cohort: tracked jobs ingested via Gmail aggregator alerts whose ATS scanner
        later populated `source_id`. Without the fallback, the set-diff sees the
        aggregator URL → None, lands in `unparseable`, gets no liveness signal.
        """
        from job_finder.web.ats_reconciler import reconcile_company
        from job_finder.web.db_helpers import standalone_connection

        # Job's only URL is an aggregator (LinkedIn) — _extract_posting_id → None.
        _, dedup_keys = _setup_company_and_jobs(
            tmp_db_path,
            platform="greenhouse",
            slug="airbnb",
            job_urls=["https://www.linkedin.com/jobs/view/4384362665/"],
        )
        # But the ATS scanner persisted the canonical greenhouse id earlier.
        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            "UPDATE jobs SET source_id = ? WHERE dedup_key = ?",
            ("12345", dedup_keys[0]),
        )
        conn.commit()
        conn.close()

        # Live greenhouse board includes that id.
        mock_scan.return_value = [
            {"source_url": "https://boards.greenhouse.io/airbnb/jobs/12345"},
        ]

        with standalone_connection(tmp_db_path) as conn:
            company_row = dict(
                conn.execute("SELECT id, ats_platform, ats_slug FROM companies").fetchone()
            )
            result = reconcile_company(conn, company_row)

        assert result["unparseable"] == 0
        assert result["live"] == 1
        assert result["expired"] == 0

        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT expiry_status FROM jobs WHERE dedup_key = ?",
            (dedup_keys[0],),
        ).fetchone()
        conn.close()
        assert row["expiry_status"] == "live"

    @patch("job_finder.web.ats_reconciler.run_platform_scan")
    def test_source_id_rescue_expires_absent_greenhouse_job(self, mock_scan, tmp_db_path):
        """source_id rescue path lands a job in `expired` when the id is NOT in the
        live set — symmetric to the live case above, proves the diff actually runs."""
        from job_finder.web.ats_reconciler import reconcile_company
        from job_finder.web.db_helpers import standalone_connection

        _, dedup_keys = _setup_company_and_jobs(
            tmp_db_path,
            platform="greenhouse",
            slug="airbnb",
            job_urls=["https://www.linkedin.com/jobs/view/4384362665/"],
        )
        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            "UPDATE jobs SET source_id = ? WHERE dedup_key = ?",
            ("99999", dedup_keys[0]),  # not on live board
        )
        conn.commit()
        conn.close()

        mock_scan.return_value = [
            {"source_url": "https://boards.greenhouse.io/airbnb/jobs/12345"},
        ]

        with standalone_connection(tmp_db_path) as conn:
            company_row = dict(
                conn.execute("SELECT id, ats_platform, ats_slug FROM companies").fetchone()
            )
            result = reconcile_company(conn, company_row)

        assert result["unparseable"] == 0
        assert result["live"] == 0
        assert result["expired"] == 1

    @patch("job_finder.web.ats_reconciler.run_platform_scan")
    def test_source_id_fallback_gated_off_for_lever(self, mock_scan, tmp_db_path):
        """Namespace guard: lever's source_id (when present) is NOT in the
        live-board UUID namespace, so blindly union-ing it would risk cross-namespace
        false-`live`/false-`expired`. The fallback must be gated to
        greenhouse/smartrecruiters only — a lever job with an unparseable URL and a
        populated source_id stays `unparseable`."""
        from job_finder.web.ats_reconciler import reconcile_company
        from job_finder.web.db_helpers import standalone_connection

        _, dedup_keys = _setup_company_and_jobs(
            tmp_db_path,
            platform="lever",
            slug="acme",
            job_urls=["https://www.linkedin.com/jobs/view/4384362665/"],  # not lever-parseable
        )
        # Populate source_id with a string that COINCIDENTALLY matches a live
        # lever UUID — if the fallback were not platform-gated, this would
        # produce a false-LIVE.
        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            "UPDATE jobs SET source_id = ? WHERE dedup_key = ?",
            ("abc-def-123", dedup_keys[0]),
        )
        conn.commit()
        conn.close()

        mock_scan.return_value = [
            {"source_url": "https://jobs.lever.co/acme/abc-def-123"},
        ]

        with standalone_connection(tmp_db_path) as conn:
            company_row = dict(
                conn.execute("SELECT id, ats_platform, ats_slug FROM companies").fetchone()
            )
            result = reconcile_company(conn, company_row)

        # Lever stays in the deferred-to-Phase-C `unparseable` bucket; no
        # false liveness signal manufactured from a cross-namespace coincidence.
        assert result["unparseable"] == 1
        assert result["live"] == 0
        assert result["expired"] == 0

    @patch("job_finder.web.ats_reconciler.run_platform_scan")
    def test_warning_when_all_jobs_unparseable(self, mock_scan, tmp_db_path, caplog):
        """IA-13 visibility: when every tracked job for a company is unparseable,
        Phase B produced no real liveness signal. A `WARNING` line must be emitted
        so the silent blind spot is greppable (mirrors no_parseable_live_ids)."""
        from job_finder.web.ats_reconciler import reconcile_company
        from job_finder.web.db_helpers import standalone_connection

        _setup_company_and_jobs(
            tmp_db_path,
            platform="greenhouse",
            slug="airbnb",
            job_urls=[
                "https://www.linkedin.com/jobs/view/1/",
                "https://www.glassdoor.com/job-listing/x/2",
            ],
        )
        mock_scan.return_value = [
            {"source_url": "https://boards.greenhouse.io/airbnb/jobs/12345"},
        ]

        with (
            caplog.at_level(logging.WARNING, logger="job_finder.web.ats_reconciler"),
            standalone_connection(tmp_db_path) as conn,
        ):
            company_row = dict(
                conn.execute("SELECT id, ats_platform, ats_slug FROM companies").fetchone()
            )
            result = reconcile_company(conn, company_row)

        assert result["checked"] == 2
        assert result["unparseable"] == 2
        warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "unparseable" in r.getMessage()
        ]
        assert warnings, "expected a WARNING when unparseable == checked"

    @patch("job_finder.web.ats_reconciler.run_platform_scan")
    def test_no_warning_when_mixed_unparseable(self, mock_scan, tmp_db_path, caplog):
        """The 100%-unparseable warn must NOT fire when at least one job parses
        — partial coverage is not a silent blind spot."""
        from job_finder.web.ats_reconciler import reconcile_company
        from job_finder.web.db_helpers import standalone_connection

        _setup_company_and_jobs(
            tmp_db_path,
            platform="greenhouse",
            slug="airbnb",
            job_urls=[
                "https://boards.greenhouse.io/airbnb/jobs/12345",  # parseable → live
                "https://www.linkedin.com/jobs/view/1/",  # unparseable
            ],
        )
        mock_scan.return_value = [
            {"source_url": "https://boards.greenhouse.io/airbnb/jobs/12345"},
        ]

        with (
            caplog.at_level(logging.WARNING, logger="job_finder.web.ats_reconciler"),
            standalone_connection(tmp_db_path) as conn,
        ):
            company_row = dict(
                conn.execute("SELECT id, ats_platform, ats_slug FROM companies").fetchone()
            )
            result = reconcile_company(conn, company_row)

        assert result["live"] == 1
        assert result["unparseable"] == 1
        unparseable_warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "all unparseable" in r.getMessage()
        ]
        assert not unparseable_warnings, (
            "100%-unparseable warn must only fire when every checked job is unparseable"
        )


# ---------------------------------------------------------------------------
# reconcile_all_companies
# ---------------------------------------------------------------------------


class TestReconcileAllCompanies:
    @patch("job_finder.web.ats_reconciler.run_platform_scan")
    def test_aggregates_per_company(self, mock_scan, tmp_db_path):
        """Multiple companies are each reconciled; summary aggregates counts."""
        from job_finder.web.ats_reconciler import reconcile_all_companies
        from job_finder.web.db_migrate import run_migrations

        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        now_iso = datetime.now(UTC).isoformat()

        # Two Lever companies, each with one discovered job.
        # Hex-only posting IDs (Lever UUID format).
        for _i, (slug, url) in enumerate(
            [
                ("acme", "https://jobs.lever.co/acme/aaa-bbb-111"),
                ("beta", "https://jobs.lever.co/beta/ccc-ddd-222"),
            ]
        ):
            conn.execute(
                "INSERT INTO companies (name, name_raw, homepage_url, ats_platform, "
                "ats_slug, ats_probe_status, scan_enabled, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (
                    slug,
                    slug.title(),
                    f"https://{slug}.com",
                    "lever",
                    slug,
                    "hit",
                    now_iso,
                    now_iso,
                ),
            )
            company_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO jobs (dedup_key, title, company, location, first_seen, "
                "last_seen, pipeline_status, company_id, source_urls) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"{slug}|job",
                    f"Job at {slug}",
                    slug.title(),
                    "Remote",
                    now_iso,
                    now_iso,
                    "discovered",
                    company_id,
                    f'["{url}"]',
                ),
            )
        conn.commit()
        conn.close()

        # scan_lever returns the URL only for the first company, so company 2's
        # job is missing from its live board → should be archived.
        def _fake_scan(scanner, slug, targets, excl):
            if slug == "acme":
                return [{"source_url": "https://jobs.lever.co/acme/aaa-bbb-111"}]
            # beta's tracked job ID (ccc-ddd-222) is absent → expired
            return [{"source_url": "https://jobs.lever.co/beta/eee-fff-999"}]

        mock_scan.side_effect = _fake_scan

        summary = reconcile_all_companies(tmp_db_path, config={})

        assert summary["companies_checked"] == 2
        assert summary["live"] == 1
        assert summary["expired"] == 1

    def test_skips_companies_without_ats_slug(self, tmp_db_path):
        """Companies with NULL ats_slug must not be queried at all."""
        from job_finder.web.ats_reconciler import reconcile_all_companies
        from job_finder.web.db_migrate import run_migrations

        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        now_iso = datetime.now(UTC).isoformat()
        conn.execute(
            "INSERT INTO companies (name, name_raw, homepage_url, ats_platform, "
            "ats_slug, ats_probe_status, scan_enabled, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
            ("no_ats", "No ATS", "https://no-ats.com", None, None, "miss", now_iso, now_iso),
        )
        conn.commit()
        conn.close()

        summary = reconcile_all_companies(tmp_db_path, config={})
        # Zero companies queried (filtered by WHERE ats_platform/slug NOT NULL)
        assert summary["companies_checked"] == 0
        assert summary["companies_skipped"] == 0
