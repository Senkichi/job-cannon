"""Tests for the job-board empty state (WP5, release polish).

Contract: ``jobs/_table.html`` distinguishes a truly empty DB ("No jobs yet"
+ dashboard CTA, optional profile tip) from filters that excluded everything
(the pre-existing "No jobs match the current filters" message). The archived
fragment never shows the onboarding CTA.
"""

import sqlite3

import pytest

EMPTY_DB_TEXT = "No jobs yet"
FILTERS_TEXT = "No jobs match the current filters"
CTA_TEXT = "press Sync Now"
TIP_TEXT = "set them in"


def _insert_job(db_path, dedup_key="acme|data scientist", status="discovered"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO jobs
               (dedup_key, title, company, location, sources, source_urls,
                description, jd_full, first_seen, last_seen, pipeline_status)
           VALUES (?, 'Data Scientist', 'Acme', 'Remote', '["linkedin"]', '[]',
                   'short', ?, '2026-06-01T00:00:00', '2026-06-01T00:00:00', ?)""",
        (dedup_key, "x" * 250, status),
    )
    conn.commit()
    conn.close()


class TestEmptyDb:
    def test_empty_db_shows_onboarding_state(self, client):
        resp = client.get("/jobs/", follow_redirects=True)
        body = resp.get_data(as_text=True)
        assert resp.status_code == 200
        assert EMPTY_DB_TEXT in body
        assert CTA_TEXT in body
        assert FILTERS_TEXT not in body

    def test_empty_db_fragment_refresh_keeps_state(self, client):
        """The live-refresh fragment route must carry total_jobs too —
        otherwise the first SSE refresh swaps the CTA away."""
        resp = client.get("/jobs/table", headers={"HX-Request": "true"})
        body = resp.get_data(as_text=True)
        assert resp.status_code == 200
        assert EMPTY_DB_TEXT in body
        assert FILTERS_TEXT not in body

    def test_tip_hidden_when_titles_configured(self, client):
        """Standard fixture has profile.target_titles populated → no tip."""
        body = client.get("/jobs/", follow_redirects=True).get_data(as_text=True)
        assert TIP_TEXT not in body


class TestProfileTip:
    @pytest.fixture
    def app_no_titles(self, tmp_db_path):
        """App whose profile has no target_titles and no portal keywords —
        the configuration under which portal ingestion fetches nothing."""
        from job_finder.web import create_app

        application = create_app(
            config={
                "db": {"path": tmp_db_path},
                "scoring": {"min_score_threshold": 40, "daily_budget_usd": 25.0},
                "profile": {
                    "target_titles": [],
                    "target_locations": ["Remote"],
                    "exclusions": {"title_keywords": [], "companies": []},
                },
                "sources": {},
                "output": {"default_format": "cli", "max_results": 50},
            }
        )
        application.config["TESTING"] = True
        return application

    def test_tip_shown_when_no_titles(self, app_no_titles):
        body = (
            app_no_titles.test_client().get("/jobs/", follow_redirects=True).get_data(as_text=True)
        )
        assert EMPTY_DB_TEXT in body
        assert TIP_TEXT in body
        assert "/profile" in body


class TestFiltersExcludedEverything:
    def test_excluding_filters_keep_existing_message(self, app, client):
        _insert_job(app.config["DB_PATH"])
        # status=offer matches nothing — the one job is 'discovered'.
        resp = client.get("/jobs/?status=offer", follow_redirects=True)
        body = resp.get_data(as_text=True)
        assert resp.status_code == 200
        assert FILTERS_TEXT in body
        assert EMPTY_DB_TEXT not in body

    def test_excluding_filters_fragment(self, app, client):
        _insert_job(app.config["DB_PATH"])
        resp = client.get("/jobs/table?status=offer", headers={"HX-Request": "true"})
        body = resp.get_data(as_text=True)
        assert FILTERS_TEXT in body
        assert EMPTY_DB_TEXT not in body


class TestArchivedFragment:
    def test_archived_empty_never_shows_onboarding_cta(self, app, client):
        """Archived fragment with zero archived rows: filters message, never
        the 'No jobs yet' CTA — even though the DB has jobs."""
        _insert_job(app.config["DB_PATH"])
        resp = client.get("/jobs/archived-table", headers={"HX-Request": "true"})
        body = resp.get_data(as_text=True)
        assert resp.status_code == 200
        assert EMPTY_DB_TEXT not in body
