"""Tests for job visibility rules: hidden statuses, show_hidden, auto-dismiss."""

import sqlite3

import pytest

from job_finder.db import get_filtered_jobs

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_job(
    conn: sqlite3.Connection,
    dedup_key: str,
    status: str = "discovered",
    first_seen: str = "2026-04-01T10:00:00",
) -> None:
    conn.execute(
        """INSERT INTO jobs
            (dedup_key, title, company, location, sources, source_urls,
             first_seen, last_seen, score, score_breakdown, pipeline_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            dedup_key,
            "Data Scientist",
            "ACME Corp",
            "Remote",
            '["test"]',
            '["https://example.com"]',
            first_seen,
            first_seen,
            75.0,
            "{}",
            status,
        ),
    )
    conn.commit()


@pytest.fixture
def migrated_conn(tmp_db_path):
    from job_finder.web.db_migrate import run_migrations

    run_migrations(tmp_db_path)
    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Hidden status exclusion
# ---------------------------------------------------------------------------


class TestHiddenStatusExclusion:
    def test_dismissed_hidden_by_default(self, migrated_conn):
        """Dismissed jobs are excluded when no status filter and show_hidden=False."""
        _insert_job(migrated_conn, "job|dismissed", status="dismissed")
        _insert_job(migrated_conn, "job|discovered", status="discovered")

        jobs = get_filtered_jobs(migrated_conn)
        keys = {j["dedup_key"] for j in jobs}
        assert "job|discovered" in keys
        assert "job|dismissed" not in keys

    def test_rejected_hidden_by_default(self, migrated_conn):
        """Rejected jobs are excluded from the default job list."""
        _insert_job(migrated_conn, "job|rejected", status="rejected")
        _insert_job(migrated_conn, "job|reviewing", status="reviewing")

        jobs = get_filtered_jobs(migrated_conn)
        keys = {j["dedup_key"] for j in jobs}
        assert "job|reviewing" in keys
        assert "job|rejected" not in keys

    def test_archived_hidden_by_default(self, migrated_conn):
        """Archived jobs are excluded from the default job list."""
        _insert_job(migrated_conn, "job|archived", status="archived")
        _insert_job(migrated_conn, "job|applied", status="applied")

        jobs = get_filtered_jobs(migrated_conn)
        keys = {j["dedup_key"] for j in jobs}
        assert "job|applied" in keys
        assert "job|archived" not in keys

    def test_withdrawn_hidden_by_default(self, migrated_conn):
        """Withdrawn jobs are excluded from the default job list."""
        _insert_job(migrated_conn, "job|withdrawn", status="withdrawn")

        jobs = get_filtered_jobs(migrated_conn)
        keys = {j["dedup_key"] for j in jobs}
        assert "job|withdrawn" not in keys

    def test_active_statuses_visible_by_default(self, migrated_conn):
        """Discovered, reviewing, applied, and other active statuses are visible."""
        for status in ("discovered", "reviewing", "applied", "phone_screen"):
            _insert_job(migrated_conn, f"job|{status}", status=status)

        jobs = get_filtered_jobs(migrated_conn)
        keys = {j["dedup_key"] for j in jobs}
        assert "job|discovered" in keys
        assert "job|reviewing" in keys
        assert "job|applied" in keys
        assert "job|phone_screen" in keys


class TestShowHidden:
    def test_dismissed_visible_with_show_hidden(self, migrated_conn):
        """Dismissed jobs appear when show_hidden=True."""
        _insert_job(migrated_conn, "job|dismissed", status="dismissed")

        jobs = get_filtered_jobs(migrated_conn, show_hidden=True)
        keys = {j["dedup_key"] for j in jobs}
        assert "job|dismissed" in keys

    def test_rejected_visible_with_show_hidden(self, migrated_conn):
        """Rejected jobs appear when show_hidden=True."""
        _insert_job(migrated_conn, "job|rejected", status="rejected")

        jobs = get_filtered_jobs(migrated_conn, show_hidden=True)
        keys = {j["dedup_key"] for j in jobs}
        assert "job|rejected" in keys

    def test_show_hidden_includes_all_status_types(self, migrated_conn):
        """show_hidden=True shows both active and hidden status jobs together."""
        _insert_job(migrated_conn, "job|dismissed", status="dismissed")
        _insert_job(migrated_conn, "job|discovered", status="discovered")

        jobs = get_filtered_jobs(migrated_conn, show_hidden=True)
        keys = {j["dedup_key"] for j in jobs}
        assert "job|dismissed" in keys
        assert "job|discovered" in keys

    def test_explicit_status_filter_overrides_hidden_exclusion(self, migrated_conn):
        """Filtering by status='dismissed' returns dismissed jobs regardless of show_hidden."""
        _insert_job(migrated_conn, "job|dismissed", status="dismissed")
        _insert_job(migrated_conn, "job|discovered", status="discovered")

        jobs = get_filtered_jobs(migrated_conn, status="dismissed")
        keys = {j["dedup_key"] for j in jobs}
        assert "job|dismissed" in keys
        assert "job|discovered" not in keys


# ---------------------------------------------------------------------------
# Auto-dismiss via exclusion filter
# ---------------------------------------------------------------------------


class TestAutoDismiss:
    def test_auto_dismiss_excluded_job_sets_dismissed_status(self, tmp_db_path):
        """Jobs excluded by keyword filter are auto-set to dismissed status."""
        from unittest.mock import patch

        from job_finder.web.db_migrate import run_migrations
        from job_finder.web.scoring_runner import run_scoring

        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        conn.execute(
            """INSERT INTO jobs
                (dedup_key, title, company, location, sources, source_urls,
                 first_seen, last_seen, score, score_breakdown, pipeline_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "intern|job|remote",
                "Data Science Intern",
                "ACME",
                "Remote",
                '["test"]',
                '["https://example.com"]',
                "2026-04-01",
                "2026-04-01",
                0,
                "{}",
                "discovered",
            ),
        )
        conn.commit()
        conn.close()

        config = {
            "db": {"path": tmp_db_path},
            "scoring": {"daily_budget_usd": 25.0},
            "profile": {
                "target_titles": ["Data Scientist"],
                "target_locations": ["Remote"],
                "min_salary": 100000,
                "industries": [],
                "exclusions": {"title_keywords": ["intern"], "companies": []},
                "skills": [],
            },
            "sources": {},
        }

        # Liveness gate is mocked so no HTTP requests fire. The exclusion
        # check fires BEFORE the liveness gate now, so the exclusion path
        # never reaches it -- mock kept for safety in case of refactor.
        with patch(
            "job_finder.web.scoring_runner.check_job_liveness", return_value="inconclusive"
        ):
            run_scoring(["intern|job|remote"], config, tmp_db_path)

        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT pipeline_status FROM jobs WHERE dedup_key = 'intern|job|remote'"
        ).fetchone()
        conn.close()
        assert row["pipeline_status"] == "dismissed"

    def test_auto_dismiss_does_not_override_reviewing(self, tmp_db_path):
        """Auto-dismiss only affects discovered jobs, not reviewing/applied/etc."""
        from unittest.mock import patch

        from job_finder.web.db_migrate import run_migrations
        from job_finder.web.scoring_runner import run_scoring

        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        conn.execute(
            """INSERT INTO jobs
                (dedup_key, title, company, location, sources, source_urls,
                 first_seen, last_seen, score, score_breakdown, pipeline_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "intern|job|remote",
                "Data Science Intern",
                "ACME",
                "Remote",
                '["test"]',
                '["https://example.com"]',
                "2026-04-01",
                "2026-04-01",
                0,
                "{}",
                "reviewing",
            ),
        )
        conn.commit()
        conn.close()

        config = {
            "db": {"path": tmp_db_path},
            "scoring": {"daily_budget_usd": 25.0},
            "profile": {
                "target_titles": ["Data Scientist"],
                "target_locations": ["Remote"],
                "min_salary": 100000,
                "industries": [],
                "exclusions": {"title_keywords": ["intern"], "companies": []},
                "skills": [],
            },
            "sources": {},
        }

        with patch(
            "job_finder.web.scoring_runner.check_job_liveness", return_value="inconclusive"
        ):
            run_scoring(["intern|job|remote"], config, tmp_db_path)

        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT pipeline_status FROM jobs WHERE dedup_key = 'intern|job|remote'"
        ).fetchone()
        conn.close()
        # Should remain "reviewing" — auto-dismiss must not override active states
        assert row["pipeline_status"] == "reviewing"
