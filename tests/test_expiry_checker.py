"""Tests for job expiry detection signal cascade."""

import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import requests


class TestQuickLivenessCheck:
    """quick_liveness_check: lightweight per-job URL check for scoring preflight."""

    @patch("job_finder.web.expiry_checker.requests.get")
    def test_404_returns_expired(self, mock_get):
        from job_finder.web.expiry_checker import EXPIRED, quick_liveness_check

        mock_get.return_value = MagicMock(spec=requests.Response, status_code=404)
        assert quick_liveness_check("https://example.com/job/123") == EXPIRED

    @patch("job_finder.web.expiry_checker.requests.get")
    def test_410_returns_expired(self, mock_get):
        from job_finder.web.expiry_checker import EXPIRED, quick_liveness_check

        mock_get.return_value = MagicMock(spec=requests.Response, status_code=410)
        assert quick_liveness_check("https://example.com/job/123") == EXPIRED

    @patch("job_finder.web.expiry_checker.requests.get")
    def test_200_returns_live(self, mock_get):
        from job_finder.web.expiry_checker import LIVE, quick_liveness_check

        mock_get.return_value = MagicMock(
            spec=requests.Response, status_code=200, text="Job description here"
        )
        assert quick_liveness_check("https://example.com/job/123") == LIVE

    @patch("job_finder.web.expiry_checker.requests.get")
    def test_timeout_returns_inconclusive(self, mock_get):
        from job_finder.web.expiry_checker import INCONCLUSIVE, quick_liveness_check

        mock_get.side_effect = requests.exceptions.Timeout("timed out")
        assert quick_liveness_check("https://example.com/job/123") == INCONCLUSIVE

    @patch("job_finder.web.expiry_checker.requests.get")
    def test_connection_error_returns_inconclusive(self, mock_get):
        from job_finder.web.expiry_checker import INCONCLUSIVE, quick_liveness_check

        mock_get.side_effect = requests.exceptions.ConnectionError("refused")
        assert quick_liveness_check("https://example.com/job/123") == INCONCLUSIVE

    @patch("job_finder.web.expiry_checker.requests.get")
    def test_body_marker_position_filled_returns_expired(self, mock_get):
        from job_finder.web.expiry_checker import EXPIRED, quick_liveness_check

        mock_get.return_value = MagicMock(
            spec=requests.Response,
            status_code=200,
            text="Sorry, this position has been filled. Please check other openings.",
        )
        assert quick_liveness_check("https://example.com/job/123") == EXPIRED

    @patch("job_finder.web.expiry_checker.requests.get")
    def test_body_marker_no_longer_available_returns_expired(self, mock_get):
        from job_finder.web.expiry_checker import EXPIRED, quick_liveness_check

        mock_get.return_value = MagicMock(
            spec=requests.Response, status_code=200, text="This job is no longer available."
        )
        assert quick_liveness_check("https://example.com/job/123") == EXPIRED

    @patch("job_finder.web.expiry_checker.requests.get")
    def test_new_marker_this_job_has_expired_returns_expired(self, mock_get):
        from job_finder.web.expiry_checker import EXPIRED, quick_liveness_check

        mock_get.return_value = MagicMock(
            spec=requests.Response,
            status_code=200,
            text="We're sorry, this job has expired. Please browse other openings.",
        )
        assert quick_liveness_check("https://example.com/job/123") == EXPIRED

    @patch("job_finder.web.expiry_checker.requests.get")
    def test_new_marker_this_position_is_no_longer_available_returns_expired(self, mock_get):
        from job_finder.web.expiry_checker import EXPIRED, quick_liveness_check

        mock_get.return_value = MagicMock(
            spec=requests.Response,
            status_code=200,
            text="This position is no longer available. Check our careers page.",
        )
        assert quick_liveness_check("https://example.com/job/123") == EXPIRED

    @patch("job_finder.web.expiry_checker.requests.get")
    def test_regex_glassdoor_date_interpolated_returns_expired(self, mock_get):
        from job_finder.web.expiry_checker import EXPIRED, quick_liveness_check

        mock_get.return_value = MagicMock(
            spec=requests.Response,
            status_code=200,
            text="This job from Jul 9, 2025 is no longer available for applications.",
        )
        assert quick_liveness_check("https://www.glassdoor.com/job/123") == EXPIRED

    @patch("job_finder.web.expiry_checker.requests.get")
    def test_regex_no_false_positive_on_benign_text(self, mock_get):
        from job_finder.web.expiry_checker import LIVE, quick_liveness_check

        # "no longer" in benign context should not trigger EXPIRED
        mock_get.return_value = MagicMock(
            spec=requests.Response,
            status_code=200,
            text=(
                "We are no longer just a startup — join our mission. "
                "This job requires experience with systems that are no longer maintained."
            ),
        )
        assert quick_liveness_check("https://example.com/job/123") == LIVE

    @patch("job_finder.web.expiry_checker.requests.get")
    def test_regex_no_longer_active_returns_expired(self, mock_get):
        from job_finder.web.expiry_checker import EXPIRED, quick_liveness_check

        mock_get.return_value = MagicMock(
            spec=requests.Response, status_code=200, text="This posting is no longer active."
        )
        assert quick_liveness_check("https://example.com/job/123") == EXPIRED


class TestCheckJobLiveness:
    """check_job_liveness: extract URLs and call quick_liveness_check."""

    @patch("job_finder.web.expiry_checker.quick_liveness_check")
    def test_calls_first_url(self, mock_check):
        from job_finder.web.expiry_checker import LIVE, check_job_liveness

        mock_check.return_value = LIVE
        job = {"source_urls": '["https://a.com/1", "https://b.com/2"]'}
        assert check_job_liveness(job) == LIVE
        mock_check.assert_called_once_with("https://a.com/1")

    def test_no_urls_returns_inconclusive(self):
        from job_finder.web.expiry_checker import INCONCLUSIVE, check_job_liveness

        assert check_job_liveness({"source_urls": "[]"}) == INCONCLUSIVE
        assert check_job_liveness({}) == INCONCLUSIVE

    @patch("job_finder.web.expiry_checker.quick_liveness_check")
    def test_handles_list_type_source_urls(self, mock_check):
        from job_finder.web.expiry_checker import EXPIRED, check_job_liveness

        mock_check.return_value = EXPIRED
        job = {"source_urls": ["https://example.com/job/1"]}
        assert check_job_liveness(job) == EXPIRED


class TestExtractPostingId:
    """_extract_posting_id extracts individual posting IDs from ATS URLs."""

    def test_lever_uuid(self):
        from job_finder.web.expiry_checker import _extract_posting_id

        url = "https://jobs.lever.co/acme-corp/abc12345-def6-7890-abcd-ef1234567890"
        assert _extract_posting_id(url, "lever") == "abc12345-def6-7890-abcd-ef1234567890"

    def test_greenhouse_numeric_id(self):
        from job_finder.web.expiry_checker import _extract_posting_id

        url = "https://boards.greenhouse.io/acme/jobs/4567890"
        assert _extract_posting_id(url, "greenhouse") == "4567890"

    def test_ashby_uuid(self):
        from job_finder.web.expiry_checker import _extract_posting_id

        url = "https://jobs.ashbyhq.com/AcmeCorp/abc12345-def6-7890-abcd-ef1234567890"
        assert _extract_posting_id(url, "ashby") == "abc12345-def6-7890-abcd-ef1234567890"

    def test_returns_none_for_non_matching_url(self):
        from job_finder.web.expiry_checker import _extract_posting_id

        url = "https://www.linkedin.com/jobs/view/12345/"
        assert _extract_posting_id(url, "lever") is None

    def test_returns_none_for_unknown_platform(self):
        from job_finder.web.expiry_checker import _extract_posting_id

        url = "https://jobs.lever.co/acme/abc123"
        assert _extract_posting_id(url, "unknown") is None


class TestCheckAtsApi:
    """Signal 1: ATS API liveness check."""

    @patch("job_finder.web.expiry_checker.requests.get")
    def test_lever_404_returns_expired(self, mock_get):
        from job_finder.web.expiry_checker import EXPIRED, _check_ats_api

        mock_get.return_value = MagicMock(status_code=404)
        result = _check_ats_api("acme", "abc-123", "lever")
        assert result == EXPIRED
        mock_get.assert_called_once()
        assert "api.lever.co" in mock_get.call_args[0][0]

    @patch("job_finder.web.expiry_checker.requests.get")
    def test_lever_200_returns_live(self, mock_get):
        from job_finder.web.expiry_checker import LIVE, _check_ats_api

        mock_get.return_value = MagicMock(status_code=200)
        result = _check_ats_api("acme", "abc-123", "lever")
        assert result == LIVE

    @patch("job_finder.web.expiry_checker.requests.get")
    def test_greenhouse_404_returns_expired(self, mock_get):
        from job_finder.web.expiry_checker import EXPIRED, _check_ats_api

        mock_get.return_value = MagicMock(status_code=404)
        result = _check_ats_api("acme", "12345", "greenhouse")
        assert result == EXPIRED
        assert "boards-api.greenhouse.io" in mock_get.call_args[0][0]

    @patch("job_finder.web.expiry_checker.requests.get")
    def test_network_error_returns_inconclusive(self, mock_get):
        from job_finder.web.expiry_checker import INCONCLUSIVE, _check_ats_api

        mock_get.side_effect = requests.exceptions.ConnectionError("timeout")
        result = _check_ats_api("acme", "abc-123", "lever")
        assert result == INCONCLUSIVE

    def test_unknown_platform_returns_inconclusive(self):
        from job_finder.web.expiry_checker import INCONCLUSIVE, _check_ats_api

        result = _check_ats_api("acme", "abc-123", "unknown")
        assert result == INCONCLUSIVE


class TestCheckCareersPage:
    """Signal 2: Company careers page title search."""

    @patch("job_finder.web.expiry_checker.scrape_careers_page")
    @patch("job_finder.web.expiry_checker.find_careers_url")
    def test_title_found_returns_live(self, mock_find, mock_scrape):
        from job_finder.web.expiry_checker import LIVE, _check_careers_page

        mock_find.return_value = "https://acme.com/careers"
        # scrape_careers_page returns list of dicts with 'title' and 'url' keys
        mock_scrape.return_value = [
            {"title": "Senior Data Scientist", "url": "https://acme.com/careers/123"}
        ]
        result = _check_careers_page(
            "https://acme.com", "Senior Data Scientist", ["data scientist"], []
        )
        assert result == LIVE

    @patch("job_finder.web.expiry_checker.scrape_careers_page")
    @patch("job_finder.web.expiry_checker.find_careers_url")
    def test_title_not_found_returns_inconclusive(self, mock_find, mock_scrape):
        from job_finder.web.expiry_checker import INCONCLUSIVE, _check_careers_page

        mock_find.return_value = "https://acme.com/careers"
        mock_scrape.return_value = [
            {"title": "Backend Engineer", "url": "https://acme.com/careers/456"}
        ]
        result = _check_careers_page(
            "https://acme.com", "Senior Data Scientist", ["data scientist"], []
        )
        assert result == INCONCLUSIVE

    @patch("job_finder.web.expiry_checker.find_careers_url")
    def test_no_careers_url_returns_inconclusive(self, mock_find):
        from job_finder.web.expiry_checker import INCONCLUSIVE, _check_careers_page

        mock_find.return_value = None
        result = _check_careers_page(
            "https://acme.com", "Senior Data Scientist", ["data scientist"], []
        )
        assert result == INCONCLUSIVE

    def test_no_homepage_returns_inconclusive(self):
        from job_finder.web.expiry_checker import INCONCLUSIVE, _check_careers_page

        result = _check_careers_page(None, "Senior Data Scientist", ["data scientist"], [])
        assert result == INCONCLUSIVE


class TestSignalCascade:
    """_check_job_expiry runs signals in order and short-circuits.

    SerpAPI (Signal 3) was removed from the cascade — absence from its index
    is a weak signal that caused false positives, and per-job 30s timeouts
    dominated wall-clock runtime. Signal 2 (careers page) is now the final
    fallback; INCONCLUSIVE from it means we can't tell.
    """

    @patch("job_finder.web.expiry_checker._check_careers_page")
    @patch("job_finder.web.expiry_checker._check_ats_api")
    @patch("job_finder.web.expiry_checker.quick_liveness_check")
    def test_ats_expired_short_circuits(self, mock_url, mock_ats, mock_careers):
        from job_finder.web.expiry_checker import EXPIRED, INCONCLUSIVE, _check_job_expiry

        mock_url.return_value = INCONCLUSIVE  # Signal 0 passes through
        mock_ats.return_value = EXPIRED
        job = {
            "dedup_key": "test",
            "title": "DS",
            "company": "Acme",
            "source_urls": '["https://jobs.lever.co/acme/abc-123"]',
        }
        company = {"ats_platform": "lever", "ats_slug": "acme", "homepage_url": "https://acme.com"}
        config = {}
        result, evidence = _check_job_expiry(job, company, config)
        assert result == EXPIRED
        assert "lever" in evidence.lower() or "ats" in evidence.lower()
        mock_careers.assert_not_called()

    @patch("job_finder.web.expiry_checker._check_careers_page")
    @patch("job_finder.web.expiry_checker._check_ats_api")
    @patch("job_finder.web.expiry_checker.quick_liveness_check")
    def test_ats_inconclusive_falls_through_to_careers(self, mock_url, mock_ats, mock_careers):
        from job_finder.web.expiry_checker import INCONCLUSIVE, LIVE, _check_job_expiry

        mock_url.return_value = INCONCLUSIVE  # Signal 0 passes through
        mock_ats.return_value = INCONCLUSIVE
        mock_careers.return_value = LIVE
        job = {
            "dedup_key": "test",
            "title": "DS",
            "company": "Acme",
            "source_urls": '["https://jobs.lever.co/acme/abc-123"]',
        }
        company = {"ats_platform": "lever", "ats_slug": "acme", "homepage_url": "https://acme.com"}
        config = {"profile": {"target_titles": [], "exclusions": {"title_keywords": []}}}
        result, evidence = _check_job_expiry(job, company, config)
        assert result == LIVE
        mock_careers.assert_called_once()

    @patch("job_finder.web.expiry_checker._check_careers_page")
    @patch("job_finder.web.expiry_checker._check_ats_api")
    @patch("job_finder.web.expiry_checker.quick_liveness_check")
    def test_all_inconclusive_returns_inconclusive(self, mock_url, mock_ats, mock_careers):
        from job_finder.web.expiry_checker import INCONCLUSIVE, _check_job_expiry

        mock_url.return_value = INCONCLUSIVE
        mock_ats.return_value = INCONCLUSIVE
        mock_careers.return_value = INCONCLUSIVE
        job = {
            "dedup_key": "test",
            "title": "DS",
            "company": "Acme",
            "source_urls": '["https://jobs.lever.co/acme/abc-123"]',
        }
        company = {"ats_platform": "lever", "ats_slug": "acme", "homepage_url": "https://acme.com"}
        config = {"profile": {"target_titles": [], "exclusions": {"title_keywords": []}}}
        result, evidence = _check_job_expiry(job, company, config)
        assert result == INCONCLUSIVE

    # --- Signal 0 tests ---

    @patch("job_finder.web.expiry_checker._check_careers_page")
    @patch("job_finder.web.expiry_checker._check_ats_api")
    @patch("job_finder.web.expiry_checker.quick_liveness_check")
    def test_signal_0_expired_short_circuits_before_ats(self, mock_url, mock_ats, mock_careers):
        from job_finder.web.expiry_checker import EXPIRED, _check_job_expiry

        mock_url.return_value = EXPIRED
        job = {
            "dedup_key": "test",
            "title": "DS",
            "company": "Acme",
            "source_urls": '["https://example.com/job/1"]',
        }
        company = {"ats_platform": "lever", "ats_slug": "acme", "homepage_url": "https://acme.com"}
        config = {}
        result, evidence = _check_job_expiry(job, company, config)
        assert result == EXPIRED
        assert "url_check" in evidence
        mock_ats.assert_not_called()
        mock_careers.assert_not_called()

    @patch("job_finder.web.expiry_checker._check_careers_page")
    @patch("job_finder.web.expiry_checker._check_ats_api")
    @patch("job_finder.web.expiry_checker.quick_liveness_check")
    def test_signal_0_live_short_circuits_before_ats(self, mock_url, mock_ats, mock_careers):
        from job_finder.web.expiry_checker import LIVE, _check_job_expiry

        mock_url.return_value = LIVE
        job = {
            "dedup_key": "test",
            "title": "DS",
            "company": "Acme",
            "source_urls": '["https://example.com/job/1"]',
        }
        company = {"ats_platform": "lever", "ats_slug": "acme", "homepage_url": "https://acme.com"}
        config = {}
        result, evidence = _check_job_expiry(job, company, config)
        assert result == LIVE
        assert "url_check" in evidence
        mock_ats.assert_not_called()
        mock_careers.assert_not_called()

    @patch("job_finder.web.expiry_checker._check_careers_page")
    @patch("job_finder.web.expiry_checker._check_ats_api")
    @patch("job_finder.web.expiry_checker.quick_liveness_check")
    def test_signal_0_inconclusive_falls_through_to_signal_1(
        self, mock_url, mock_ats, mock_careers
    ):
        from job_finder.web.expiry_checker import EXPIRED, INCONCLUSIVE, _check_job_expiry

        mock_url.return_value = INCONCLUSIVE
        mock_ats.return_value = EXPIRED
        job = {
            "dedup_key": "test",
            "title": "DS",
            "company": "Acme",
            "source_urls": '["https://jobs.lever.co/acme/abc-123"]',
        }
        company = {"ats_platform": "lever", "ats_slug": "acme", "homepage_url": "https://acme.com"}
        config = {}
        result, evidence = _check_job_expiry(job, company, config)
        assert result == EXPIRED
        mock_ats.assert_called_once()

    @patch("job_finder.web.expiry_checker._check_careers_page")
    @patch("job_finder.web.expiry_checker._check_ats_api")
    def test_signal_0_skipped_when_no_source_urls(self, mock_ats, mock_careers):
        from job_finder.web.expiry_checker import INCONCLUSIVE, _check_job_expiry

        mock_ats.return_value = INCONCLUSIVE
        mock_careers.return_value = INCONCLUSIVE
        job = {"dedup_key": "test", "title": "DS", "company": "Acme", "source_urls": "[]"}
        company = {"ats_platform": None, "ats_slug": None, "homepage_url": None}
        config = {"profile": {"target_titles": [], "exclusions": {"title_keywords": []}}}
        result, _ = _check_job_expiry(job, company, config)
        assert result == INCONCLUSIVE


class TestRunStalenessCheck:
    """run_staleness_check orchestrates the three phases (B → A → C).

    These tests disable Phase B (batch ATS) via config so they don't hit
    real HTTP. Phase A runs naturally on fresh last_seen timestamps (no
    time-based archives fire). Phase C is the focus — _check_job_expiry
    is mocked per-test.
    """

    def _setup_db(self, path):
        """Create a migrated DB with test jobs and companies."""
        from job_finder.web.db_migrate import run_migrations

        run_migrations(path)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row

        # Fresh last_seen so Phase A (time-based stale/archive) is a no-op.
        now_iso = datetime.now(UTC).isoformat()

        conn.execute(
            "INSERT INTO companies (name, name_raw, homepage_url, ats_platform, ats_slug, "
            "ats_probe_status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "acme",
                "Acme Corp",
                "https://acme.com",
                "lever",
                "acme-corp",
                "hit",
                now_iso,
                now_iso,
            ),
        )
        company_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen, "
            "pipeline_status, company_id, source_urls) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "acme|ds|remote",
                "Data Scientist",
                "Acme Corp",
                "Remote",
                now_iso,
                now_iso,
                "discovered",
                company_id,
                '["https://jobs.lever.co/acme-corp/abc-123-def"]',
            ),
        )

        # Applied job — must NEVER be touched by expiry check.
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen, "
            "pipeline_status, company_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "acme|sde|remote",
                "Software Engineer",
                "Acme Corp",
                "Remote",
                now_iso,
                now_iso,
                "applied",
                company_id,
            ),
        )
        conn.commit()
        conn.close()
        return path

    def _base_config(self):
        return {
            "profile": {"target_titles": [], "exclusions": {"title_keywords": []}},
            "staleness": {"batch_ats_enabled": False, "cascade_parallel_workers": 2},
        }

    @patch("job_finder.web.expiry_checker._check_job_expiry")
    def test_archives_expired_job(self, mock_check, tmp_db_path):
        from job_finder.web.expiry_checker import run_staleness_check

        self._setup_db(tmp_db_path)
        mock_check.return_value = ("expired", "lever_api 404")

        result = run_staleness_check(tmp_db_path, self._base_config())

        assert result["phase_c"]["archived"] >= 1

        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT pipeline_status FROM jobs WHERE dedup_key = ?",
            ("acme|ds|remote",),
        ).fetchone()
        assert row["pipeline_status"] == "archived"
        conn.close()

    @patch("job_finder.web.expiry_checker._check_job_expiry")
    def test_does_not_touch_applied_jobs(self, mock_check, tmp_db_path):
        from job_finder.web.expiry_checker import run_staleness_check

        self._setup_db(tmp_db_path)
        mock_check.return_value = ("expired", "lever_api 404")

        run_staleness_check(tmp_db_path, self._base_config())

        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT pipeline_status FROM jobs WHERE dedup_key = ?",
            ("acme|sde|remote",),
        ).fetchone()
        assert row["pipeline_status"] == "applied"
        conn.close()

    @patch("job_finder.web.expiry_checker._check_job_expiry")
    def test_updates_expiry_checked_at_on_live(self, mock_check, tmp_db_path):
        from job_finder.web.expiry_checker import run_staleness_check

        self._setup_db(tmp_db_path)
        mock_check.return_value = ("live", "lever_api 200")

        run_staleness_check(tmp_db_path, self._base_config())

        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT expiry_checked_at FROM jobs WHERE dedup_key = ?",
            ("acme|ds|remote",),
        ).fetchone()
        assert row["expiry_checked_at"] is not None
        conn.close()

    @patch("job_finder.web.expiry_checker._check_job_expiry")
    def test_skips_recently_checked_jobs(self, mock_check, tmp_db_path):
        from job_finder.web.expiry_checker import run_staleness_check

        self._setup_db(tmp_db_path)

        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            "UPDATE jobs SET expiry_checked_at = ? WHERE dedup_key = ?",
            (datetime.now(UTC).isoformat(), "acme|ds|remote"),
        )
        conn.commit()
        conn.close()

        config = {
            **self._base_config(),
            "staleness": {
                "batch_ats_enabled": False,
                "cascade_parallel_workers": 2,
                "cascade_recheck_days": 3,
            },
        }
        run_staleness_check(tmp_db_path, config)

        mock_check.assert_not_called()

    def test_run_expiry_check_is_deprecated_alias(self, tmp_db_path):
        """Legacy entry point emits DeprecationWarning and returns the
        nested phase summary from run_staleness_check."""
        import warnings

        from job_finder.web.expiry_checker import run_expiry_check

        self._setup_db(tmp_db_path)

        with patch("job_finder.web.expiry_checker._check_job_expiry") as mock_check:
            mock_check.return_value = ("inconclusive", "")
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                result = run_expiry_check(tmp_db_path, self._base_config())

        assert any(issubclass(w.category, DeprecationWarning) for w in caught)
        assert "phase_a" in result
        assert "phase_b" in result
        assert "phase_c" in result

    def test_phase_order_is_b_then_c_then_a(self, tmp_db_path):
        """Phase A (clock-based) must run LAST so it judges against the
        liveness evidence Phases B and C just refreshed."""
        from job_finder.web.expiry_checker import run_staleness_check

        self._setup_db(tmp_db_path)

        call_order: list[str] = []
        with (
            patch("job_finder.web.ats_reconciler.reconcile_all_companies") as mock_b,
            patch("job_finder.web.expiry_checker._run_phase_c_cascade") as mock_c,
            patch("job_finder.web.stale_detector.run_stale_detection") as mock_a,
        ):
            mock_b.side_effect = lambda *a, **k: (call_order.append("b"), {})[1]
            mock_c.side_effect = lambda *a, **k: (call_order.append("c"), {})[1]
            mock_a.side_effect = lambda *a, **k: (call_order.append("a"), {})[1]
            config = {
                **self._base_config(),
                "staleness": {"batch_ats_enabled": True, "cascade_parallel_workers": 2},
            }
            run_staleness_check(tmp_db_path, config)

        assert call_order == ["b", "c", "a"]


class TestCareersBackoff:
    """_record_careers_outcome tracks failures and sets skip-until timestamps."""

    def setup_method(self):
        """Reset module-level backoff state before each test."""
        from job_finder.web import expiry_checker

        expiry_checker._careers_failure_counts.clear()
        expiry_checker._careers_skip_until.clear()

    def test_three_consecutive_failures_trigger_skip(self):
        from job_finder.web.expiry_checker import _careers_skip_until, _record_careers_outcome

        _record_careers_outcome(42, success=False)
        _record_careers_outcome(42, success=False)
        assert 42 not in _careers_skip_until  # Not yet at threshold
        _record_careers_outcome(42, success=False)
        assert 42 in _careers_skip_until  # Now at threshold
        skip_time = _careers_skip_until[42]
        # Should be ~7 days from now
        now = datetime.now(UTC)
        assert skip_time > now + timedelta(days=6)
        assert skip_time < now + timedelta(days=8)

    def test_success_resets_failure_count(self):
        from job_finder.web.expiry_checker import (
            _careers_failure_counts,
            _careers_skip_until,
            _record_careers_outcome,
        )

        _record_careers_outcome(42, success=False)
        _record_careers_outcome(42, success=False)
        assert _careers_failure_counts.get(42, 0) == 2
        _record_careers_outcome(42, success=True)
        assert 42 not in _careers_failure_counts
        assert 42 not in _careers_skip_until


class TestAutoReopen:
    """Archived jobs re-appearing during ingestion are auto-reopened."""

    def test_archived_job_reopened_on_upsert(self, tmp_db_path):
        """upsert_job for an existing archived job sets pipeline_status to discovered."""
        import sqlite3

        from job_finder.db import upsert_job
        from job_finder.models import Job
        from job_finder.parsed_job import ParsedJob
        from job_finder.web.db_migrate import run_migrations

        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row

        # Insert a job and set it to archived
        job = Job(
            title="Data Scientist",
            company="Acme Corp",
            location="Remote",
            source="linkedin",
            source_url="https://linkedin.com/jobs/1",
        )
        upsert_job(conn, ParsedJob.from_job(job))
        conn.execute(
            "UPDATE jobs SET pipeline_status = 'archived' WHERE dedup_key = ?",
            (job.dedup_key,),
        )
        conn.commit()

        # Re-ingest the same job (simulates re-appearance in Gmail/SerpAPI)
        result = upsert_job(conn, ParsedJob.from_job(job))
        assert result.kind != "inserted"  # existing job, not new

        row = conn.execute(
            "SELECT pipeline_status FROM jobs WHERE dedup_key = ?",
            (job.dedup_key,),
        ).fetchone()
        assert row["pipeline_status"] == "discovered"

        # Verify evidence was recorded in pipeline_events
        event = conn.execute(
            "SELECT evidence, source FROM pipeline_events WHERE job_id = ? ORDER BY timestamp DESC LIMIT 1",
            (job.dedup_key,),
        ).fetchone()
        assert event["evidence"] == "re_appeared"
        assert event["source"] == "ingestion"

        conn.close()

    def test_reopen_clears_frozen_expiry_state(self, tmp_db_path):
        """Reopened jobs shed their expiry verdict so Phase B/C re-verify them.

        Both staleness phases exclude expiry_status='expired' rows; without
        the clear, a reopened job would never be liveness-checked again.
        """
        import sqlite3

        from job_finder.db import upsert_job
        from job_finder.models import Job
        from job_finder.parsed_job import ParsedJob
        from job_finder.web.db_migrate import run_migrations

        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row

        job = Job(
            title="Data Scientist",
            company="Acme Corp",
            location="Remote",
            source="linkedin",
            source_url="https://linkedin.com/jobs/1",
        )
        upsert_job(conn, ParsedJob.from_job(job))
        conn.execute(
            "UPDATE jobs SET pipeline_status = 'archived', expiry_status = 'expired', "
            "expiry_checked_at = '2026-05-01T00:00:00', is_stale = 1 WHERE dedup_key = ?",
            (job.dedup_key,),
        )
        conn.commit()

        upsert_job(conn, ParsedJob.from_job(job))

        row = conn.execute(
            "SELECT pipeline_status, expiry_status, expiry_checked_at, is_stale "
            "FROM jobs WHERE dedup_key = ?",
            (job.dedup_key,),
        ).fetchone()
        assert row["pipeline_status"] == "discovered"
        assert row["expiry_status"] is None
        assert row["expiry_checked_at"] is None
        assert row["is_stale"] == 0

        conn.close()

    def test_non_archived_job_not_reopened(self, tmp_db_path):
        """upsert_job for an existing reviewing job does NOT change pipeline_status."""
        import sqlite3

        from job_finder.db import upsert_job
        from job_finder.models import Job
        from job_finder.parsed_job import ParsedJob
        from job_finder.web.db_migrate import run_migrations

        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row

        job = Job(
            title="Data Scientist",
            company="Acme Corp",
            location="Remote",
            source="linkedin",
            source_url="https://linkedin.com/jobs/1",
        )
        upsert_job(conn, ParsedJob.from_job(job))
        conn.execute(
            "UPDATE jobs SET pipeline_status = 'reviewing' WHERE dedup_key = ?",
            (job.dedup_key,),
        )
        conn.commit()

        upsert_job(conn, ParsedJob.from_job(job))

        row = conn.execute(
            "SELECT pipeline_status FROM jobs WHERE dedup_key = ?",
            (job.dedup_key,),
        ).fetchone()
        assert row["pipeline_status"] == "reviewing"  # unchanged

        conn.close()
