"""Tests for Flask web views and app factory.

Tests cover:
- App factory creates a valid Flask app
- Root route redirects to /jobs
- All 5 blueprint routes return 200
- Base template includes required CDN scripts
- Base template has dark mode class
"""

from datetime import UTC

import pytest


class TestRootRedirect:
    def test_root_redirects_to_jobs(self, client):
        """GET / should redirect to /jobs (Job Board is default landing)."""
        response = client.get("/")
        assert response.status_code == 302
        assert "/jobs" in response.headers["Location"]


class TestBlueprintRoutes:
    def test_jobs_returns_200(self, client):
        """GET /jobs returns 200 with Job Board content."""
        response = client.get("/jobs")
        assert response.status_code == 200
        assert b"Job Board" in response.data

    def test_dashboard_returns_200(self, client):
        """GET /dashboard returns 200."""
        response = client.get("/dashboard")
        assert response.status_code == 200
        assert b"manual-job-dialog" in response.data
        assert b"add-from-listing" in response.data

    def test_pipeline_returns_200(self, client):
        """GET /pipeline returns 200."""
        response = client.get("/pipeline")
        assert response.status_code == 200

    def test_profile_returns_200(self, client):
        """GET /profile returns 200."""
        response = client.get("/profile")
        assert response.status_code == 200

    def test_settings_returns_200(self, client):
        """GET /settings returns 200."""
        response = client.get("/settings")
        assert response.status_code == 200


class TestBaseTemplate:
    def test_htmx_script_loaded(self, client):
        """Base template includes HTMX CDN script tag."""
        response = client.get("/jobs")
        assert b"htmx.org" in response.data

    def test_tailwind_v4_script_loaded(self, client):
        """Base template includes Tailwind v4 CDN script tag."""
        response = client.get("/jobs")
        assert b"@tailwindcss/browser@4" in response.data

    def test_sortablejs_script_loaded_on_pipeline(self, client):
        """SortableJS is loaded on the pipeline page (not globally)."""
        response = client.get("/pipeline")
        assert b"Sortable" in response.data

    def test_dark_mode_class_on_html(self, client):
        """HTML element has class='dark' for dark mode."""
        response = client.get("/jobs")
        assert b'class="dark"' in response.data


# ---------------------------------------------------------------------------
# Fixtures for Job Board tests
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_jobs(tmp_db_path):
    """Flask app with a temp DB that has 2 sample jobs inserted via migration."""
    import sqlite3

    from job_finder.web import create_app

    test_config = {
        "db": {"path": tmp_db_path},
        "scoring": {"min_score_threshold": 40},
        "profile": {
            "target_titles": ["Staff Data Scientist"],
            "target_locations": ["Remote"],
            "min_salary": 150000,
            "industries": [],
            "exclusions": {"title_keywords": [], "companies": []},
            "skills": [],
        },
        "sources": {},
        "output": {"default_format": "cli", "max_results": 50},
    }
    application = create_app(config=test_config)
    application.config["TESTING"] = True

    # Insert 2 sample jobs after create_app() has run migrations
    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    conn.executemany(
        """INSERT INTO jobs
            (dedup_key, title, company, location, sources, source_urls,
             source_id, salary_min, salary_max, description,
             first_seen, last_seen, score, score_breakdown, pipeline_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                "acme|data-scientist|remote",
                "Data Scientist",
                "Acme Corp",
                "Remote",
                '["linkedin"]',
                '["https://linkedin.com/jobs/1"]',
                "job-1",
                150000,
                200000,
                "Build ML models | Deploy data pipelines | Monitor model performance for Acme Corp",
                "2026-03-01T10:00:00",
                "2026-03-09T10:00:00",
                8.5,
                '{"skills": 0.9}',
                "discovered",
            ),
            (
                "beta|staff-ds|san-francisco",
                "Staff Data Scientist",
                "Beta Inc",
                "San Francisco, CA",
                '["greenhouse"]',
                '["https://boards.greenhouse.io/beta/jobs/2"]',
                "job-2",
                200000,
                280000,
                "Lead data science for the entire platform.",
                "2026-03-03T12:00:00",
                "2026-03-09T12:00:00",
                9.1,
                '{"skills": 0.95}',
                "reviewing",
            ),
        ],
    )
    conn.commit()
    conn.close()

    return application


@pytest.fixture
def jobs_client(app_with_jobs):
    """Test client for the app that has sample jobs."""
    return app_with_jobs.test_client()


# ---------------------------------------------------------------------------
# Job Board route tests
# ---------------------------------------------------------------------------


class TestJobBoardRoutes:
    def test_jobs_index_returns_200_with_table(self, jobs_client):
        """GET /jobs returns 200 and includes table structure."""
        response = jobs_client.get("/jobs")
        assert response.status_code == 200
        data = response.data.decode()
        assert "Job Board" in data
        # Should contain the table
        assert "<table" in data or "<tbody" in data or "job-table-body" in data

    def test_jobs_table_partial_returns_rows_only(self, jobs_client):
        """GET /jobs/table returns partial HTML without full page structure."""
        response = jobs_client.get("/jobs/table", headers={"HX-Request": "true"})
        assert response.status_code == 200
        data = response.data.decode()
        # Should NOT contain base layout elements (no <html> or sidebar)
        assert "<html" not in data
        # Should contain job row data
        assert "Acme Corp" in data or "Beta Inc" in data

    def test_jobs_table_filter_by_status(self, jobs_client):
        """GET /jobs/table?status=reviewing returns only reviewing jobs."""
        response = jobs_client.get("/jobs/table?status=reviewing", headers={"HX-Request": "true"})
        assert response.status_code == 200
        data = response.data.decode()
        assert "Beta Inc" in data
        # The discovered job should not appear
        assert "Acme Corp" not in data

    def test_jobs_table_filter_by_min_score(self, jobs_client):
        """GET /jobs/table?min_score=9 returns only high-score jobs."""
        response = jobs_client.get("/jobs/table?min_score=9", headers={"HX-Request": "true"})
        assert response.status_code == 200
        data = response.data.decode()
        assert "Beta Inc" in data  # score 9.1 >= 9
        assert "Acme Corp" not in data  # score 8.5 < 9

    def test_jobs_expand_returns_accordion(self, jobs_client):
        """GET /jobs/<key>/expand returns accordion row HTML."""
        response = jobs_client.get(
            "/jobs/acme%7Cdata-scientist%7Cremote/expand", headers={"HX-Request": "true"}
        )
        assert response.status_code == 200
        data = response.data.decode()
        # Accordion should show description and links
        assert "ML models" in data or "Acme Corp" in data

    def test_jobs_expand_returns_404_for_unknown_key(self, jobs_client):
        """GET /jobs/<unknown>/expand returns 404."""
        response = jobs_client.get("/jobs/nonexistent-key/expand", headers={"HX-Request": "true"})
        assert response.status_code == 404

    def test_jobs_status_update_changes_status_and_creates_event(self, jobs_client, tmp_db_path):
        """POST /jobs/<key>/status updates pipeline_status and creates pipeline_event."""
        import sqlite3

        response = jobs_client.post(
            "/jobs/acme%7Cdata-scientist%7Cremote/status",
            data={"pipeline_status": "reviewing"},
        )
        assert response.status_code == 200

        # Verify DB state
        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        job = conn.execute(
            "SELECT pipeline_status FROM jobs WHERE dedup_key = ?",
            ("acme|data-scientist|remote",),
        ).fetchone()
        assert job["pipeline_status"] == "reviewing"

        event = conn.execute(
            "SELECT * FROM pipeline_events WHERE job_id = ? ORDER BY timestamp DESC LIMIT 1",
            ("acme|data-scientist|remote",),
        ).fetchone()
        assert event is not None
        assert event["to_status"] == "reviewing"
        assert event["from_status"] == "discovered"
        conn.close()

    def test_jobs_status_update_rejects_invalid_status(self, jobs_client):
        """POST /jobs/<key>/status with invalid status returns 400."""
        response = jobs_client.post(
            "/jobs/acme%7Cdata-scientist%7Cremote/status",
            data={"pipeline_status": "invalid_status"},
        )
        assert response.status_code == 400

    def test_jobs_detail_returns_200_with_full_job(self, jobs_client):
        """GET /jobs/<key> returns 200 with full job detail."""
        response = jobs_client.get("/jobs/acme%7Cdata-scientist%7Cremote")
        assert response.status_code == 200
        data = response.data.decode()
        assert "Data Scientist" in data
        assert "Acme Corp" in data

    def test_jobs_detail_returns_404_for_unknown_key(self, jobs_client):
        """GET /jobs/<unknown> returns 404."""
        response = jobs_client.get("/jobs/not-a-real-key")
        assert response.status_code == 404

    def test_add_from_listing_non_htmx_redirects_to_dashboard(self, jobs_client):
        """POST without HX-Request is rejected with redirect (modal-only contract)."""
        response = jobs_client.post(
            "/jobs/add-from-listing",
            data={"listing_url": "https://example.com/job/1"},
        )
        assert response.status_code == 302
        assert "/dashboard" in response.headers.get("Location", "")

    def test_add_from_listing_htmx_creates_job_and_calls_enrich(self, jobs_client, tmp_db_path):
        """HTMX POST saves row, runs enrich_job, returns fragment."""
        import sqlite3
        from unittest.mock import patch

        import job_finder.web.blueprints.jobs as jb

        with patch.object(jb, "enrich_job", return_value={"jd_full": "x" * 250}) as mock_enrich:
            with patch("job_finder.web.claude_client.cost_gate", return_value=False):
                response = jobs_client.post(
                    "/jobs/add-from-listing",
                    data={
                        "listing_url": "https://example.com/job/42",
                        "job_title": "Widget Engineer",
                        "company": "Contoso LLC",
                        "location": "Remote",
                    },
                    headers={"HX-Request": "true"},
                )
        assert response.status_code == 200
        body = response.data.decode()
        assert "Job saved" in body
        assert "Open job" in body
        mock_enrich.assert_called_once()

        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT title, company, sources, source_urls FROM jobs WHERE title = ?",
            ("Widget Engineer",),
        ).fetchone()
        conn.close()
        assert row is not None
        assert "Contoso" in row["company"]
        assert "manual" in row["sources"]
        assert "example.com" in row["source_urls"]

    def test_add_from_listing_htmx_validation_error_fragment(self, jobs_client):
        response = jobs_client.post(
            "/jobs/add-from-listing",
            data={"listing_url": "javascript:void(0)"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert b"http://" in response.data or b"https://" in response.data

    def test_infer_title_company_splits_html_title(self):
        from unittest.mock import MagicMock, patch

        from job_finder.web.blueprints.jobs import infer_title_company_from_listing_url

        mock_resp = MagicMock()
        mock_resp.raise_for_status = lambda: None
        mock_resp.text = (
            "<html><head><title>Sr PM | Contoso</title></head><body></body></html>"
        )
        with patch(
            "job_finder.web.blueprints.jobs.requests.get", return_value=mock_resp
        ):
            title, company = infer_title_company_from_listing_url("https://x.com/j/1")
        assert title == "Sr PM"
        assert company == "Contoso"

    def test_jobs_collapse_returns_hidden_placeholder(self, jobs_client):
        """GET /jobs/<key>/collapse returns hidden placeholder <tr>, not a full row."""
        response = jobs_client.get(
            "/jobs/acme%7Cdata-scientist%7Cremote/collapse", headers={"HX-Request": "true"}
        )
        assert response.status_code == 200
        data = response.data.decode()
        # Must contain the data-expand-slot placeholder attribute
        assert "data-expand-slot" in data
        # Must have the hidden class (no visual space taken)
        assert 'class="hidden"' in data
        # Must NOT contain a full compact row (no <table> column cells)
        assert "Acme Corp" not in data
        assert "Data Scientist" not in data

    def test_jobs_collapse_returns_404_for_unknown_key(self, jobs_client):
        """GET /jobs/<unknown>/collapse returns 404."""
        response = jobs_client.get(
            "/jobs/nonexistent-key/collapse", headers={"HX-Request": "true"}
        )
        assert response.status_code == 404

    def test_jobs_table_contains_expand_slot_placeholders(self, jobs_client):
        """GET /jobs/table renders a hidden placeholder <tr> after each compact row."""
        response = jobs_client.get("/jobs/table", headers={"HX-Request": "true"})
        assert response.status_code == 200
        data = response.data.decode()
        # Should have data-expand-slot placeholders (one per job)
        assert "data-expand-slot" in data
        assert 'class="hidden"' in data

    def test_jobs_expand_contains_collapse_button_with_correct_htmx(self, jobs_client):
        """GET /jobs/<key>/expand returns accordion with correctly wired collapse button."""
        response = jobs_client.get(
            "/jobs/acme%7Cdata-scientist%7Cremote/expand", headers={"HX-Request": "true"}
        )
        assert response.status_code == 200
        data = response.data.decode()
        # Collapse button must have hx-target="closest tr" and hx-swap="outerHTML"
        assert 'hx-target="closest tr"' in data
        assert 'hx-swap="outerHTML"' in data

    def test_jobs_detail_inline_returns_200_with_full_content(self, jobs_client):
        """GET /jobs/<key>/detail-inline returns 200 with full job content as inline partial."""
        response = jobs_client.get(
            "/jobs/acme%7Cdata-scientist%7Cremote/detail-inline", headers={"HX-Request": "true"}
        )
        assert response.status_code == 200
        data = response.data.decode()
        # Must contain full description content
        assert "ML models" in data or "Build ML models" in data
        # Must contain company name
        assert "Acme Corp" in data
        # Must be a <tr> partial (no <html> wrapper)
        assert "<html" not in data
        assert "<tr" in data

    def test_jobs_detail_inline_returns_404_for_unknown_key(self, jobs_client):
        """GET /jobs/<unknown>/detail-inline returns 404."""
        response = jobs_client.get(
            "/jobs/nonexistent-key/detail-inline", headers={"HX-Request": "true"}
        )
        assert response.status_code == 404

    def test_jobs_detail_inline_contains_collapse_full_detail_button(self, jobs_client):
        """GET /jobs/<key>/detail-inline returns partial with Collapse Full Detail HTMX button."""
        response = jobs_client.get(
            "/jobs/acme%7Cdata-scientist%7Cremote/detail-inline", headers={"HX-Request": "true"}
        )
        assert response.status_code == 200
        data = response.data.decode()
        # Must contain the Collapse Full Detail button wired to /expand route
        assert "Collapse Full Detail" in data
        assert "detail-inline" not in data.replace(
            "/detail-inline", "ROUTE"
        )  # button points to /expand not /detail-inline
        assert "/expand" in data
        # HTMX wiring on collapse button
        assert 'hx-target="closest tr"' in data
        assert 'hx-swap="outerHTML"' in data

    def test_jobs_detail_inline_contains_pipeline_timeline(self, jobs_client):
        """GET /jobs/<key>/detail-inline returns partial with pipeline timeline section."""
        response = jobs_client.get(
            "/jobs/acme%7Cdata-scientist%7Cremote/detail-inline", headers={"HX-Request": "true"}
        )
        assert response.status_code == 200
        data = response.data.decode()
        # Must contain pipeline timeline section
        assert "Pipeline Timeline" in data

    def test_jobs_expand_row_has_toggle_data_attributes(self, jobs_client):
        """GET /jobs/table renders compact rows with data-expand-url and data-collapse-url for toggle logic."""
        response = jobs_client.get("/jobs/table", headers={"HX-Request": "true"})
        assert response.status_code == 200
        data = response.data.decode()
        # Compact rows must have both toggle URL data attributes
        assert "data-expand-url" in data
        assert "data-collapse-url" in data

    def test_jobs_expand_contains_prominent_collapse_button(self, jobs_client):
        """GET /jobs/<key>/expand returns accordion with a prominent Collapse button (not a subtle link)."""
        response = jobs_client.get(
            "/jobs/acme%7Cdata-scientist%7Cremote/expand", headers={"HX-Request": "true"}
        )
        assert response.status_code == 200
        data = response.data.decode()
        # Collapse button must use bg-slate-700 prominent styling (not text-slate-500 subtle link)
        assert "bg-slate-700" in data
        assert "Collapse" in data
        # Must NOT use the invisible subtle link styling
        assert "text-slate-500 hover:text-slate-300 text-xs underline" not in data

    def test_jobs_detail_inline_formats_description(self, jobs_client):
        """GET /jobs/<key>/detail-inline renders description as HTML list, not raw pipe-separated text."""
        response = jobs_client.get(
            "/jobs/acme%7Cdata-scientist%7Cremote/detail-inline", headers={"HX-Request": "true"}
        )
        assert response.status_code == 200
        data = response.data.decode()
        # Should contain HTML list elements (from format_description filter)
        assert "<ul" in data or "<li" in data
        # Should NOT contain raw pipe separators in the description
        assert "Build ML models | Deploy" not in data

    def test_jobs_detail_page_formats_description(self, jobs_client):
        """GET /jobs/<key> (standalone detail page) renders description as HTML list."""
        response = jobs_client.get("/jobs/acme%7Cdata-scientist%7Cremote")
        assert response.status_code == 200
        data = response.data.decode()
        assert "<ul" in data or "<li" in data

    def test_jobs_expand_contains_status_dropdown(self, jobs_client):
        """GET /jobs/<key>/expand returns accordion with a status dropdown."""
        response = jobs_client.get(
            "/jobs/acme%7Cdata-scientist%7Cremote/expand", headers={"HX-Request": "true"}
        )
        assert response.status_code == 200
        data = response.data.decode()
        # Expanded accordion must include a status dropdown
        assert "<select" in data
        assert 'name="pipeline_status"' in data

    def test_jobs_status_cell_uses_htmx_event_stopping(self, jobs_client):
        """GET /jobs/table renders compact rows with hx-on:click (not onclick) for event stopping."""
        response = jobs_client.get("/jobs/table", headers={"HX-Request": "true"})
        assert response.status_code == 200
        data = response.data.decode()
        # Must use HTMX-native event stopping attribute
        assert "hx-on:click" in data
        # Must NOT rely on plain inline onclick for event stopping
        assert 'onclick="event.stopPropagation()"' not in data

    def test_jobs_status_cell_uses_high_contrast_background(self, jobs_client):
        """GET /jobs/table renders status dropdowns with bg-slate-700 (not bg-slate-800) for contrast."""
        response = jobs_client.get("/jobs/table", headers={"HX-Request": "true"})
        assert response.status_code == 200
        data = response.data.decode()
        # Status dropdown must use visible bg-slate-700 (not near-invisible bg-slate-800)
        assert "bg-slate-700" in data


# ---------------------------------------------------------------------------
# HTMX Reactivity tests (Phase 35)
# ---------------------------------------------------------------------------


class TestJobBoardReactivity:
    """Tests for HTMX reactivity: HX-Trigger headers on scoring/archive routes."""

    def test_update_status_archived_suppresses_hx_trigger(self, jobs_client):
        """POST /jobs/{key}/status with archived status does NOT set HX-Trigger.

        The jobs-updated trigger is suppressed when archiving to prevent the main
        tbody from refetching, which would kill the in-flight archive fadeout animation.
        """
        response = jobs_client.post(
            "/jobs/acme%7Cdata-scientist%7Cremote/status",
            data={"pipeline_status": "archived"},
        )
        assert response.status_code == 200
        assert "HX-Trigger" not in response.headers

    def test_archive_response_includes_oob_counter(self, jobs_client):
        """POST /jobs/{key}/status with archived includes OOB archived-count span."""
        response = jobs_client.post(
            "/jobs/acme%7Cdata-scientist%7Cremote/status",
            data={"pipeline_status": "archived"},
        )
        assert response.status_code == 200
        html = response.data.decode()
        assert 'id="archived-count"' in html
        assert 'hx-swap-oob="innerHTML"' in html

    def test_non_archive_status_no_oob_counter(self, jobs_client):
        """POST /jobs/{key}/status with non-archived status has no OOB counter."""
        response = jobs_client.post(
            "/jobs/acme%7Cdata-scientist%7Cremote/status",
            data={"pipeline_status": "reviewing"},
        )
        assert response.status_code == 200
        html = response.data.decode()
        assert "archived-count" not in html

    def test_update_status_non_archive_no_hx_trigger(self, jobs_client):
        """POST /jobs/{key}/status with non-archived status has no HX-Trigger header."""
        response = jobs_client.post(
            "/jobs/acme%7Cdata-scientist%7Cremote/status",
            data={"pipeline_status": "reviewing"},
        )
        assert response.status_code == 200
        assert "HX-Trigger" not in response.headers

    def test_table_body_has_jobs_updated_trigger(self, jobs_client):
        """GET /jobs page has tbody with hx-trigger listening for jobs-updated."""
        response = jobs_client.get("/jobs")
        assert response.status_code == 200
        html = response.data.decode()
        assert "jobs-updated from:body" in html
        assert 'hx-include="#filter-form"' in html

    def test_archive_button_has_hx_target(self, jobs_client):
        """Expanded row archive button targets the status cell element."""
        response = jobs_client.get(
            "/jobs/acme%7Cdata-scientist%7Cremote/expand",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        html = response.data.decode()
        assert 'hx-target="#status-cell-' in html
        assert 'hx-disable-elt="this"' in html


# ---------------------------------------------------------------------------
# Archived Jobs Section tests (Phase 41)
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_archived_job(tmp_db_path):
    """Flask app with a temp DB that has 1 active + 1 archived job."""
    import sqlite3

    from job_finder.web import create_app
    from job_finder.web.db_migrate import run_migrations

    run_migrations(tmp_db_path)
    conn = sqlite3.connect(tmp_db_path)
    conn.execute(
        "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen, pipeline_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "acme|data-scientist|remote",
            "Data Scientist",
            "Acme",
            "Remote",
            "2026-01-01",
            "2026-01-01",
            "discovered",
        ),
    )
    conn.execute(
        "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen, pipeline_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("old|engineer|ny", "Engineer", "OldCo", "NY", "2025-01-01", "2025-01-01", "archived"),
    )
    conn.commit()
    conn.close()

    test_config = {
        "db": {"path": tmp_db_path},
        "scoring": {"min_score_threshold": 40},
        "profile": {
            "target_titles": [],
            "target_locations": [],
            "min_salary": 0,
            "industries": [],
            "exclusions": {"title_keywords": [], "companies": []},
            "skills": [],
        },
        "sources": {},
        "output": {"default_format": "cli", "max_results": 50},
    }
    return create_app(config=test_config)


class TestArchivedSection:
    """Tests for collapsible archived jobs section and /jobs/archived-table route."""

    def test_archived_table_returns_200(self, app_with_archived_job):
        """GET /jobs/archived-table returns 200."""
        client = app_with_archived_job.test_client()
        response = client.get("/jobs/archived-table", headers={"HX-Request": "true"})
        assert response.status_code == 200

    def test_archived_table_contains_archived_job(self, app_with_archived_job):
        """GET /jobs/archived-table response HTML contains the archived job's title."""
        client = app_with_archived_job.test_client()
        response = client.get("/jobs/archived-table", headers={"HX-Request": "true"})
        assert response.status_code == 200
        html = response.data.decode()
        assert "Engineer" in html

    def test_archived_table_excludes_active_jobs(self, app_with_archived_job):
        """GET /jobs/archived-table response HTML does NOT contain the active job's title."""
        client = app_with_archived_job.test_client()
        response = client.get("/jobs/archived-table", headers={"HX-Request": "true"})
        assert response.status_code == 200
        html = response.data.decode()
        assert "Data Scientist" not in html

    def test_index_shows_archived_section_when_archived_exist(self, app_with_archived_job):
        """GET /jobs contains archived-section, archived-count, and the count value."""
        client = app_with_archived_job.test_client()
        response = client.get("/jobs")
        assert response.status_code == 200
        html = response.data.decode()
        assert "archived-section" in html
        assert "archived-count" in html
        assert ">1<" in html  # count badge value

    def test_index_hides_archived_section_when_none_archived(self, app_with_jobs):
        """GET /jobs does NOT contain archived-section when 0 archived jobs."""
        # app_with_jobs fixture has 2 active jobs, no archived jobs
        client = app_with_jobs.test_client()
        response = client.get("/jobs")
        assert response.status_code == 200
        html = response.data.decode()
        assert "archived-section" not in html

    def test_archived_section_has_lazy_load_js(self, app_with_archived_job):
        """GET /jobs contains toggleArchived function and dataset.loaded lazy-load guard."""
        client = app_with_archived_job.test_client()
        response = client.get("/jobs")
        assert response.status_code == 200
        html = response.data.decode()
        assert "toggleArchived" in html
        assert "dataset.loaded" in html


# ---------------------------------------------------------------------------
# Fixtures for batch score tests
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_migrations(tmp_db_path):
    """Flask app with a fully migrated temp database (all migrations run)."""
    from job_finder.web import create_app

    test_config = {
        "db": {"path": tmp_db_path},
        "scoring": {"min_score_threshold": 40, "candidate_score_threshold": 55},
        "profile": {
            "target_titles": ["Staff Data Scientist"],
            "target_locations": ["Remote"],
            "min_salary": 150000,
            "industries": [],
            "exclusions": {"title_keywords": [], "companies": []},
            "skills": [],
        },
        "sources": {},
        "output": {"default_format": "cli", "max_results": 50},
    }
    application = create_app(config=test_config)
    application.config["TESTING"] = True
    return application


@pytest.fixture
def migrated_client(app_with_migrations):
    """Test client for the migrated app."""
    return app_with_migrations.test_client()


@pytest.fixture
def app_with_unscored_jobs(tmp_db_path):
    """Flask app with migrated DB containing unscored jobs."""
    import sqlite3

    from job_finder.web import create_app

    test_config = {
        "db": {"path": tmp_db_path},
        "scoring": {"min_score_threshold": 40, "candidate_score_threshold": 55},
        "profile": {
            "target_titles": ["Staff Data Scientist"],
            "target_locations": ["Remote"],
            "min_salary": 150000,
            "industries": [],
            "exclusions": {"title_keywords": [], "companies": []},
            "skills": [],
        },
        "sources": {},
        "output": {"default_format": "cli", "max_results": 50},
    }
    application = create_app(config=test_config)
    application.config["TESTING"] = True

    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    conn.executemany(
        """INSERT INTO jobs
            (dedup_key, title, company, location, sources, source_urls,
             source_id, salary_min, salary_max, description,
             first_seen, last_seen, score, score_breakdown, pipeline_status,
             classification, sub_scores_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                "acme|data-scientist|remote",
                "Data Scientist",
                "Acme Corp",
                "Remote",
                '["linkedin"]',
                '["https://linkedin.com/jobs/1"]',
                "job-1",
                150000,
                200000,
                "Build ML models",
                "2026-03-01T10:00:00",
                "2026-03-09T10:00:00",
                8.5,
                '{"skills": 0.9}',
                "discovered",
                None,
                None,  # unscored (classification IS NULL)
            ),
            (
                "beta|staff-ds|san-francisco",
                "Staff Data Scientist",
                "Beta Inc",
                "San Francisco, CA",
                '["greenhouse"]',
                '["https://boards.greenhouse.io/beta/jobs/2"]',
                "job-2",
                200000,
                280000,
                "Lead data science.",
                "2026-03-03T12:00:00",
                "2026-03-09T12:00:00",
                9.1,
                '{"skills": 0.95}',
                "reviewing",
                # v3 classification='apply' with sub_scores_json, replacing
                # legacy (haiku_score=75, sonnet_score=None) pair
                "apply",
                '{"title_fit": 4, "location_fit": 4, "comp_fit": 4, "domain_match": 4, "seniority_match": 4, "skills_match": 4}',
            ),
        ],
    )
    conn.commit()
    conn.close()

    return application


@pytest.fixture
def unscored_client(app_with_unscored_jobs):
    """Test client for the app with unscored jobs."""
    return app_with_unscored_jobs.test_client()


# ---------------------------------------------------------------------------
# Batch Score Route tests
# ---------------------------------------------------------------------------


class TestBatchScoreStart:
    def test_batch_score_start_returns_progress_fragment_when_unscored_exist(self, unscored_client):
        """POST /dashboard/batch-score/start returns progress fragment with session_id."""
        response = unscored_client.post("/dashboard/batch-score/start")
        assert response.status_code == 200
        data = response.data.decode()
        assert "batch-score-status" in data
        assert "hx-trigger" in data  # polling continues

    def test_batch_score_start_returns_done_when_no_unscored_jobs(self, migrated_client):
        """POST /dashboard/batch-score/start returns done fragment when 0 unscored jobs."""
        response = migrated_client.post("/dashboard/batch-score/start")
        assert response.status_code == 200
        data = response.data.decode()
        assert "hx-trigger" not in data
        assert "batch-score-status" in data

    def test_batch_score_start_progress_shows_scoring_label(self, unscored_client):
        """Progress fragment shows Scoring progress text."""
        response = unscored_client.post("/dashboard/batch-score/start")
        assert response.status_code == 200
        data = response.data.decode()
        assert "Scoring:" in data

    def test_batch_score_start_creates_session_in_db(self, app_with_unscored_jobs):
        """POST /dashboard/batch-score/start inserts a batch_score_sessions row."""
        import sqlite3

        client = app_with_unscored_jobs.test_client()
        client.post("/dashboard/batch-score/start")

        db_path = app_with_unscored_jobs.config["DB_PATH"]
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        session = conn.execute(
            "SELECT * FROM batch_score_sessions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()

        assert session is not None
        assert session["session_type"] == "scoring"
        assert session["status"] in ("running", "done", "cancelled")


class TestBatchScoreStatus:
    def test_status_returns_progress_when_running(self, app_with_unscored_jobs):
        """GET /dashboard/batch-score/status/<id> returns progress fragment (hx-trigger) when running."""
        import sqlite3
        from datetime import datetime

        db_path = app_with_unscored_jobs.config["DB_PATH"]
        conn = sqlite3.connect(db_path)
        # Use current time so the 30-min timeout does not trigger
        now_iso = datetime.now(UTC).replace(tzinfo=None).isoformat()
        conn.execute(
            "INSERT INTO batch_score_sessions (session_type, status, total, scored, started_at) "
            "VALUES ('haiku', 'running', 10, 3, ?)",
            (now_iso,),
        )
        conn.commit()
        session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()

        client = app_with_unscored_jobs.test_client()
        response = client.get(f"/dashboard/batch-score/status/{session_id}")
        assert response.status_code == 200
        data = response.data.decode()
        # Running fragment must have hx-trigger to continue polling
        assert "hx-trigger" in data

    def test_status_returns_done_fragment_when_done(self, app_with_unscored_jobs):
        """GET /dashboard/batch-score/status/<id> returns done fragment (no hx-trigger) when done."""
        import sqlite3

        db_path = app_with_unscored_jobs.config["DB_PATH"]
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO batch_score_sessions (session_type, status, total, scored, finished_at, started_at) "
            "VALUES ('haiku', 'done', 10, 8, '2026-03-12T00:01:00', '2026-03-12T00:00:00')"
        )
        conn.commit()
        session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()

        client = app_with_unscored_jobs.test_client()
        response = client.get(f"/dashboard/batch-score/status/{session_id}")
        assert response.status_code == 200
        data = response.data.decode()
        # Done fragment must NOT have hx-trigger (stops polling)
        assert "hx-trigger" not in data

    def test_status_returns_done_fragment_when_cancelled(self, app_with_unscored_jobs):
        """GET /dashboard/batch-score/status/<id> returns done fragment when cancelled."""
        import sqlite3

        db_path = app_with_unscored_jobs.config["DB_PATH"]
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO batch_score_sessions (session_type, status, total, scored, started_at) "
            "VALUES ('haiku', 'cancelled', 10, 5, '2026-03-12T00:00:00')"
        )
        conn.commit()
        session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()

        client = app_with_unscored_jobs.test_client()
        response = client.get(f"/dashboard/batch-score/status/{session_id}")
        assert response.status_code == 200
        data = response.data.decode()
        assert "hx-trigger" not in data
        assert "batch-score-status" in data  # correct fragment container


class TestBatchScoreCancel:
    def test_cancel_sets_status_to_cancelling(self, app_with_unscored_jobs):
        """POST /dashboard/batch-score/cancel/<id> sets status='cancelling' in DB."""
        import sqlite3

        db_path = app_with_unscored_jobs.config["DB_PATH"]
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO batch_score_sessions (session_type, status, total, scored, started_at) "
            "VALUES ('haiku', 'running', 10, 3, '2026-03-12T00:00:00')"
        )
        conn.commit()
        session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()

        client = app_with_unscored_jobs.test_client()
        response = client.post(f"/dashboard/batch-score/cancel/{session_id}")
        assert response.status_code == 200

        # Verify DB state changed to 'cancelling'
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        session = conn.execute(
            "SELECT status FROM batch_score_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        conn.close()
        assert session["status"] == "cancelling"

    def test_cancel_returns_progress_fragment(self, app_with_unscored_jobs):
        """POST /dashboard/batch-score/cancel/<id> returns fragment (polling continues until cancelled)."""
        import sqlite3

        db_path = app_with_unscored_jobs.config["DB_PATH"]
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO batch_score_sessions (session_type, status, total, scored, started_at) "
            "VALUES ('haiku', 'running', 10, 3, '2026-03-12T00:00:00')"
        )
        conn.commit()
        session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()

        client = app_with_unscored_jobs.test_client()
        response = client.post(f"/dashboard/batch-score/cancel/{session_id}")
        assert response.status_code == 200
        data = response.data.decode()
        # Cancel response keeps polling until background thread sets status='cancelled'
        assert "batch-score-status" in data


# ---------------------------------------------------------------------------
# Fixtures and tests for source count badge in accordion expand
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_multi_source_job(tmp_db_path):
    """Flask app with a temp DB containing a multi-source merged job and a single-source job."""
    import sqlite3

    from job_finder.web import create_app

    test_config = {
        "db": {"path": tmp_db_path},
        "scoring": {"min_score_threshold": 40},
        "profile": {
            "target_titles": ["Staff Data Scientist"],
            "target_locations": ["Remote"],
            "min_salary": 150000,
            "industries": [],
            "exclusions": {"title_keywords": [], "companies": []},
            "skills": [],
        },
        "sources": {},
        "output": {"default_format": "cli", "max_results": 50},
    }
    application = create_app(config=test_config)
    application.config["TESTING"] = True

    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    conn.executemany(
        """INSERT INTO jobs
            (dedup_key, title, company, location, sources, source_urls,
             source_id, salary_min, salary_max, description,
             first_seen, last_seen, score, score_breakdown, pipeline_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                # Multi-source job: merged from linkedin + glassdoor
                "acme|data-scientist|remote",
                "Data Scientist",
                "Acme Corp",
                "Remote",
                '["linkedin", "glassdoor"]',
                '["https://linkedin.com/jobs/1", "https://glassdoor.com/jobs/2"]',
                "job-1",
                150000,
                200000,
                "Build ML models at Acme Corp.",
                "2026-03-01T10:00:00",
                "2026-03-09T10:00:00",
                8.5,
                '{"skills": 0.9}',
                "discovered",
            ),
            (
                # Single-source job: only from linkedin
                "beta|staff-ds|sf",
                "Staff Data Scientist",
                "Beta Inc",
                "San Francisco, CA",
                '["greenhouse"]',
                '["https://boards.greenhouse.io/beta/jobs/99"]',
                "job-2",
                200000,
                280000,
                "Lead data science at Beta Inc.",
                "2026-03-03T12:00:00",
                "2026-03-09T12:00:00",
                9.1,
                '{"skills": 0.95}',
                "reviewing",
            ),
        ],
    )
    conn.commit()
    conn.close()

    return application


@pytest.fixture
def multi_source_client(app_with_multi_source_job):
    """Test client for the app with a multi-source job."""
    return app_with_multi_source_job.test_client()


class TestSourceCountBadge:
    def test_multi_source_job_shows_source_count_badge_in_accordion(self, multi_source_client):
        """GET /jobs/<key>/expand shows '2 sources' badge for merged multi-source jobs."""
        response = multi_source_client.get(
            "/jobs/acme%7Cdata-scientist%7Cremote/expand", headers={"HX-Request": "true"}
        )
        assert response.status_code == 200
        data = response.data.decode()
        # Multi-source badge should appear in expanded view
        assert "2 sources" in data

    def test_single_source_job_does_not_show_source_count_badge(self, multi_source_client):
        """GET /jobs/<key>/expand does NOT show source count badge for single-source jobs."""
        response = multi_source_client.get(
            "/jobs/beta%7Cstaff-ds%7Csf/expand", headers={"HX-Request": "true"}
        )
        assert response.status_code == 200
        data = response.data.decode()
        # Single-source badge should NOT appear
        assert "1 sources" not in data
        assert "sources" not in data or "greenhouse" in data  # sources list present but no badge

    def test_multi_source_job_shows_all_source_links(self, multi_source_client):
        """GET /jobs/<key>/expand shows all source URLs as clickable links for merged jobs."""
        response = multi_source_client.get(
            "/jobs/acme%7Cdata-scientist%7Cremote/expand", headers={"HX-Request": "true"}
        )
        assert response.status_code == 200
        data = response.data.decode()
        # Both source links should appear
        assert "linkedin" in data
        assert "glassdoor" in data

    def test_multi_source_job_shows_enrichment_indicator(self, multi_source_client):
        """GET /jobs/<key>/expand shows subtle enrichment indicator for multi-source jobs."""
        response = multi_source_client.get(
            "/jobs/acme%7Cdata-scientist%7Cremote/expand", headers={"HX-Request": "true"}
        )
        assert response.status_code == 200
        data = response.data.decode()
        # Enrichment sparkle indicator should appear for multi-source jobs
        assert "&#10024;" in data or "sparkle" in data.lower() or "sources" in data


# ---------------------------------------------------------------------------
# Fixtures and tests for Companies page
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_companies(tmp_db_path):
    """Flask app with migrated DB containing sample companies."""
    import sqlite3
    from datetime import datetime

    from job_finder.web import create_app

    test_config = {
        "db": {"path": tmp_db_path},
        "scoring": {"min_score_threshold": 40},
        "profile": {
            "target_titles": ["Staff Data Scientist"],
            "target_locations": ["Remote"],
            "min_salary": 150000,
            "industries": [],
            "exclusions": {"title_keywords": [], "companies": []},
            "skills": [],
        },
        "sources": {},
        "output": {"default_format": "cli", "max_results": 50},
    }
    application = create_app(config=test_config)
    application.config["TESTING"] = True

    now = datetime.now().isoformat()
    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    conn.executemany(
        """INSERT INTO companies
            (name, name_raw, homepage_url, ats_platform, ats_slug, ats_probe_status,
             scan_enabled, jobs_found_total, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            ("stripe", "Stripe", "https://stripe.com", "lever", "stripe", "hit", 1, 5, now, now),
            ("openai", "OpenAI", "https://openai.com", "ashby", "OpenAI", "hit", 1, 12, now, now),
            ("nohome", "NoHomeCo", None, None, None, "miss", 1, 0, now, now),
        ],
    )
    conn.commit()
    conn.close()

    return application


@pytest.fixture
def companies_client(app_with_companies):
    """Test client for the app with sample companies."""
    return app_with_companies.test_client()


class TestCompaniesPage:
    def test_companies_index_returns_200(self, companies_client):
        """GET /companies returns 200 with Companies page."""
        response = companies_client.get("/companies")
        assert response.status_code == 200
        data = response.data.decode()
        assert "Companies" in data

    def test_companies_index_shows_company_names(self, companies_client):
        """GET /companies shows inserted company names in the page."""
        response = companies_client.get("/companies")
        assert response.status_code == 200
        data = response.data.decode()
        assert "Stripe" in data
        assert "OpenAI" in data

    def test_companies_search_filters_by_name(self, companies_client):
        """GET /companies?search=stripe filters companies by name."""
        response = companies_client.get("/companies?search=stripe")
        assert response.status_code == 200
        data = response.data.decode()
        assert "Stripe" in data
        # OpenAI should not appear
        assert "OpenAI" not in data

    def test_companies_ats_platform_filter(self, companies_client):
        """GET /companies?ats_platform=lever filters by ATS platform."""
        response = companies_client.get("/companies?ats_platform=lever")
        assert response.status_code == 200
        data = response.data.decode()
        # Only Lever companies should appear (Stripe)
        assert "Stripe" in data

    def test_companies_expand_returns_expanded_view(self, app_with_companies):
        """GET /companies/<id>/expand returns expanded view with jobs and scan history."""
        import sqlite3

        db_path = app_with_companies.config["DB_PATH"]
        conn = sqlite3.connect(db_path)
        company_id = conn.execute("SELECT id FROM companies WHERE name = 'stripe'").fetchone()[0]
        conn.close()

        client = app_with_companies.test_client()
        response = client.get(f"/companies/{company_id}/expand")
        assert response.status_code == 200
        data = response.data.decode()
        assert "Stripe" in data
        assert "Scan History" in data

    def test_companies_add_creates_company(self, app_with_companies):
        """POST /companies/add creates company and redirects."""
        import sqlite3

        client = app_with_companies.test_client()
        response = client.post(
            "/companies/add",
            data={"company_name": "Ramp", "homepage_url": "https://ramp.com"},
        )
        assert response.status_code == 302

        # Verify company was created
        db_path = app_with_companies.config["DB_PATH"]
        conn = sqlite3.connect(db_path)
        company = conn.execute("SELECT * FROM companies WHERE name_raw = 'Ramp'").fetchone()
        conn.close()
        assert company is not None

    def test_companies_toggle_switches_scan_enabled(self, app_with_companies):
        """POST /companies/<id>/toggle toggles scan_enabled and returns updated row."""
        import sqlite3

        db_path = app_with_companies.config["DB_PATH"]
        conn = sqlite3.connect(db_path)
        company_id = conn.execute("SELECT id FROM companies WHERE name = 'stripe'").fetchone()[0]
        conn.close()

        client = app_with_companies.test_client()
        response = client.post(f"/companies/{company_id}/toggle")
        assert response.status_code == 200

        # Verify scan_enabled was toggled (was 1, should now be 0)
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT scan_enabled FROM companies WHERE id = ?", (company_id,)
        ).fetchone()
        conn.close()
        assert row[0] == 0  # toggled from 1 to 0

    def test_companies_sidebar_nav_visible(self, companies_client):
        """Companies nav entry appears in sidebar."""
        response = companies_client.get("/companies")
        assert response.status_code == 200
        data = response.data.decode()
        assert "/companies" in data
        assert "Companies" in data


# ---------------------------------------------------------------------------
# Tests for Settings ATS section
# ---------------------------------------------------------------------------


@pytest.fixture
def ats_app(tmp_db_path):
    """Hermetic Flask app for ATS settings tests with explicit ats config.

    The shared `app` fixture omits the `ats` section, so the previous version
    of these tests silently depended on the hardcoded defaults inside
    blueprints/settings.py (scan_enabled=True, scan_days="mon,wed",
    scan_hour=7). With config supplied here, the rendering tests actually
    exercise the wiring from JF_CONFIG → template.
    """
    import sqlite3

    from job_finder.web import create_app

    test_config = {
        "db": {"path": tmp_db_path},
        "scoring": {"min_score_threshold": 40, "daily_budget_usd": 25.0},
        "profile": {
            "target_titles": ["Staff Data Scientist"],
            "target_locations": ["Remote"],
            "min_salary": 150000,
            "industries": [],
            "exclusions": {"title_keywords": [], "companies": []},
            "skills": [],
        },
        "sources": {},
        "output": {"default_format": "cli", "max_results": 50},
        "ats": {
            "scan_enabled": True,
            "scan_days": "mon,wed",
            "scan_hour": 7,
        },
    }
    application = create_app(config=test_config)
    # Seed onboarding_complete so the @before_request gate doesn't redirect /settings.
    conn = sqlite3.connect(tmp_db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO onboarding_state (id, onboarding_complete) VALUES (1, 1)"
        )
        conn.commit()
    finally:
        conn.close()
    application.config["TESTING"] = True
    return application


@pytest.fixture
def ats_client(ats_app):
    return ats_app.test_client()


class TestSettingsAtsScanSection:
    def test_settings_page_renders_ats_scanning_section(self, ats_client):
        """GET /settings renders ATS Scanning section with toggle."""
        response = ats_client.get("/settings")
        assert response.status_code == 200
        data = response.data.decode()
        assert "ATS Scanning" in data
        assert "ats_scan_enabled" in data

    def test_settings_page_renders_ats_scan_days(self, ats_client):
        """GET /settings renders scan_days and scan_hour fields."""
        response = ats_client.get("/settings")
        assert response.status_code == 200
        data = response.data.decode()
        assert "ats_scan_days" in data
        assert "ats_scan_hour" in data

    def test_settings_save_includes_ats_config(self, ats_app, tmp_path, monkeypatch):
        """POST /settings/save with ats_scan_enabled saves ats config.

        Uses monkeypatch to redirect _CONFIG_PATH so the auto-revert teardown
        restores it even if the assertion raises mid-test — replaces the
        try/finally pattern that could leak global state on exception.
        """
        import yaml

        import job_finder.web.blueprints.settings as settings_mod

        # Redirect writes to a temp config file so tests don't clobber the real config.yaml
        config_path = str(tmp_path / "config.yaml")
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(ats_app.config["JF_CONFIG"], f, default_flow_style=False)
        monkeypatch.setattr(settings_mod, "_CONFIG_PATH", config_path)

        client = ats_app.test_client()
        # POST with ats_scan_enabled on
        response = client.post(
            "/settings/save",
            data={
                "ats_scan_enabled": "on",
                "ats_scan_days": "mon,fri",
                "ats_scan_hour": "8",
                # Include required fields to prevent KeyError
                "target_titles": "Data Scientist",
                "target_locations": "Remote",
                "min_salary": "150000",
                "industries": "",
                "exclusion_title_keywords": "",
                "exclusion_companies": "",
                "profile_skills": "Python\nSQL",
                "gmail_enabled": "",
                "gmail_lookback_days": "7",
                "serpapi_enabled": "",
                "jsearch_enabled": "",
                "weight_title_match": "0.30",
                "weight_seniority_alignment": "0.20",
                "weight_location_fit": "0.15",
                "weight_salary_range": "0.15",
                "weight_industry_relevance": "0.10",
                "weight_company_signals": "0.05",
                "weight_recency": "0.05",
                "min_score_threshold": "40",
                "daily_budget_usd": "25.0",
                "candidate_score_threshold": "42",
                "model_haiku": "claude-haiku-4-5",
                "model_sonnet": "claude-sonnet-4-6",
                "output_default_format": "cli",
                "output_markdown_path": "reports/",
                "output_max_results": "50",
                "db_path": "jobs.db",
            },
        )
        # Should redirect to settings page
        assert response.status_code == 302

        # Verify config was updated in app context
        ats_cfg = ats_app.config.get("JF_CONFIG", {}).get("ats", {})
        assert ats_cfg.get("scan_enabled") is True
        assert ats_cfg.get("scan_days") == "mon,fri"
        assert ats_cfg.get("scan_hour") == 8


# ---------------------------------------------------------------------------
# Tests for Dashboard ATS stat card
# ---------------------------------------------------------------------------


class TestDashboardAtsStat:
    def test_dashboard_shows_ats_stat_card(self, client):
        """GET /dashboard shows ATS Discovery stat card."""
        response = client.get("/dashboard")
        assert response.status_code == 200
        data = response.data.decode()
        assert "ATS Discovery" in data

    def test_dashboard_shows_company_count(self, companies_client):
        """GET /dashboard shows company count in ATS stat card."""
        response = companies_client.get("/dashboard")
        assert response.status_code == 200
        data = response.data.decode()
        assert "companies tracked" in data

    def test_dashboard_shows_no_scans_yet_when_empty(self, client):
        """GET /dashboard shows 'No ATS scans yet' when no scans exist."""
        response = client.get("/dashboard")
        assert response.status_code == 200
        data = response.data.decode()
        assert "No ATS scans yet" in data

    def test_dashboard_handles_missing_companies_table_gracefully(self, app):
        """GET /dashboard returns 200 even when companies table query fails."""
        # The _get_ats_context wraps queries in try/except — should not crash
        response = app.test_client().get("/dashboard")
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Tests for /dashboard/stats and /dashboard/quick-actions HTMX fragment routes
# wired by dashboard-refresh auto-refresh in dashboard/index.html
# ---------------------------------------------------------------------------


class TestDashboardRefreshFragments:
    def test_stats_fragment_returns_200_and_renders_stat_cards(self, client):
        """GET /dashboard/stats returns the stats partial body."""
        response = client.get("/dashboard/stats")
        assert response.status_code == 200
        data = response.data.decode()
        # Stat-card labels rendered by _stats_cards.html
        assert "Total Jobs" in data
        assert "New Today" in data
        assert "Pending Review" in data
        assert "ATS Discovery" in data

    def test_quick_actions_fragment_returns_200_and_renders_action_buttons(self, client):
        """GET /dashboard/quick-actions returns the quick-actions partial body."""
        response = client.get("/dashboard/quick-actions")
        assert response.status_code == 200
        data = response.data.decode()
        # Either Sync Now button or sync-progress markup is rendered
        assert "Sync Now" in data or "sync-progress" in data
        # Either a Score button or "No jobs to score" placeholder
        assert "Score" in data or "No jobs to score" in data

    def test_dashboard_index_includes_refresh_listening_wrappers(self, client):
        """Dashboard page must wrap stat cards and quick actions in containers
        that subscribe to the 'dashboard-refresh' event from the body — this is
        what makes the post-batch-score HX-Trigger payload actually do something.
        """
        response = client.get("/dashboard")
        assert response.status_code == 200
        data = response.data.decode()
        assert 'id="dashboard-stats"' in data
        assert 'id="dashboard-quick-actions"' in data
        assert 'hx-trigger="dashboard-refresh from:body"' in data
        # Quick actions wrapper uses a 5s delay so backend session state settles
        assert 'hx-trigger="dashboard-refresh from:body delay:5s"' in data


# ---------------------------------------------------------------------------
# Tests for /dashboard/cost-detail HTMX route (SCORE-07)
# ---------------------------------------------------------------------------


class TestCostDetailRoute:
    def test_cost_detail_returns_200(self, client):
        """GET /dashboard/cost-detail returns 200."""
        response = client.get("/dashboard/cost-detail", headers={"HX-Request": "true"})
        assert response.status_code == 200

    def test_cost_detail_contains_cost_breakdown_elements(self, client):
        """GET /dashboard/cost-detail renders cost breakdown partial with today/week/projected stats."""
        response = client.get("/dashboard/cost-detail", headers={"HX-Request": "true"})
        assert response.status_code == 200
        data = response.data.decode()
        assert "Cost Breakdown" in data
        assert "Today" in data
        assert "This Week" in data
        assert "Projected/mo" in data


class TestDashboardUserActivity:
    """Tests for get_recent_activity() DB function and dashboard User Activity section."""

    @pytest.fixture
    def migrated_app(self, tmp_db_path):
        """Create a Flask app with a fully migrated temp database."""
        from job_finder.web import create_app
        from job_finder.web.db_migrate import run_migrations

        run_migrations(tmp_db_path)

        test_config = {
            "db": {"path": tmp_db_path},
            "scoring": {"min_score_threshold": 40},
            "profile": {
                "target_titles": ["Staff Data Scientist"],
                "target_locations": ["Remote"],
                "min_salary": 150000,
                "industries": [],
                "exclusions": {"title_keywords": [], "companies": []},
                "skills": [],
            },
            "sources": {},
            "output": {"default_format": "cli", "max_results": 50},
        }
        application = create_app(config=test_config)
        application.config["TESTING"] = True
        return application, tmp_db_path

    @pytest.fixture
    def migrated_client(self, migrated_app):
        """Flask test client backed by migrated DB."""
        application, _ = migrated_app
        return application.test_client()

    def test_get_recent_activity_returns_rows(self, tmp_db_path):
        """get_recent_activity() returns dicts from user_activity table ordered DESC."""
        import sqlite3

        from job_finder.db import get_recent_activity
        from job_finder.web.db_migrate import run_migrations

        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row

        conn.execute(
            "INSERT INTO user_activity (action, entity_id, metadata, occurred_at) "
            "VALUES (?, ?, ?, ?)",
            ("upload_resume_pdf", "resume.pdf", '{"status": "ok"}', "2026-03-10T10:00:00"),
        )
        conn.execute(
            "INSERT INTO user_activity (action, entity_id, metadata, occurred_at) "
            "VALUES (?, ?, ?, ?)",
            ("sync", None, '{"jobs_new": 5}', "2026-03-10T09:00:00"),
        )
        conn.commit()

        rows = get_recent_activity(conn)
        conn.close()

        assert len(rows) == 2
        assert rows[0]["action"] == "upload_resume_pdf"
        assert rows[1]["action"] == "sync"

    def test_get_recent_activity_empty_table(self, tmp_db_path):
        """get_recent_activity() returns empty list when user_activity has no rows."""
        import sqlite3

        from job_finder.db import get_recent_activity
        from job_finder.web.db_migrate import run_migrations

        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row

        rows = get_recent_activity(conn)
        conn.close()

        assert rows == []

    def test_get_recent_activity_respects_limit(self, tmp_db_path):
        """get_recent_activity() respects the limit parameter."""
        import sqlite3

        from job_finder.db import get_recent_activity
        from job_finder.web.db_migrate import run_migrations

        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row

        for i in range(5):
            conn.execute(
                "INSERT INTO user_activity (action, entity_id, metadata, occurred_at) "
                "VALUES (?, ?, ?, ?)",
                ("sync", None, "{}", f"2026-03-10T0{i}:00:00"),
            )
        conn.commit()

        rows = get_recent_activity(conn, limit=3)
        conn.close()

        assert len(rows) == 3

    def test_dashboard_contains_user_activity_heading(self, migrated_client):
        """GET /dashboard returns 200 and response contains 'User Activity' heading text."""
        response = migrated_client.get("/dashboard")
        assert response.status_code == 200
        assert b"User Activity" in response.data


# ---------------------------------------------------------------------------
# Fixtures for Data Quality tests (Phase 34)
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_entity_job(tmp_db_path):
    """Flask app with a job that has HTML entities in description and jd_full."""
    import sqlite3

    from job_finder.web import create_app

    test_config = {
        "db": {"path": tmp_db_path},
        "scoring": {"min_score_threshold": 40},
        "profile": {
            "target_titles": ["ML Engineer"],
            "target_locations": ["Remote"],
            "min_salary": 150000,
            "industries": [],
            "exclusions": {"title_keywords": [], "companies": []},
            "skills": [],
        },
        "sources": {},
        "output": {"default_format": "cli", "max_results": 50},
    }
    app = create_app(config=test_config)
    app.config["TESTING"] = True

    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """INSERT INTO jobs
            (dedup_key, title, company, location, sources, source_urls,
             source_id, salary_min, salary_max, description, jd_full,
             first_seen, last_seen, score, score_breakdown, pipeline_status,
             classification)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "test|entity-job|remote",
            "Data Engineer",
            "AT&T",
            "Remote",
            '["linkedin"]',
            '["https://linkedin.com/jobs/100"]',
            "job-100",
            160000,
            220000,
            "Work at AT&amp;T on data systems.\nRequirements:\n- Python &amp; SQL\n- It&#39;s a great role",
            "Full JD for AT&amp;T role.\nWe&#39;re looking for someone who can:\n- Build data pipelines\n- Work with cross-functional teams",
            "2026-03-01T10:00:00",
            "2026-03-09T10:00:00",
            8.0,
            '{"skills": 0.8}',
            "discovered",
            "consider",  # v3 classification replacing legacy haiku_score=72
        ),
    )
    conn.commit()
    conn.close()
    return app


@pytest.fixture
def entity_client(app_with_entity_job):
    """Test client for the app with HTML entity job."""
    return app_with_entity_job.test_client()


@pytest.fixture
def app_with_html_tag_job(tmp_db_path):
    """Flask app with a job whose description contains entity-encoded HTML tags.

    Description uses &lt;p&gt;, &lt;ul&gt;, &lt;li&gt; to simulate the GAP 1
    failure mode: descriptions stored with entity-encoded HTML markup.
    jd_full is None so the description section renders.
    """
    import sqlite3

    from job_finder.web import create_app

    test_config = {
        "db": {"path": tmp_db_path},
        "scoring": {"min_score_threshold": 40},
        "profile": {
            "target_titles": ["Software Engineer"],
            "target_locations": ["Remote"],
            "min_salary": 100000,
            "industries": [],
            "exclusions": {"title_keywords": [], "companies": []},
            "skills": [],
        },
        "sources": {},
        "output": {"default_format": "cli", "max_results": 50},
    }
    app = create_app(config=test_config)
    app.config["TESTING"] = True

    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """INSERT INTO jobs
            (dedup_key, title, company, location, sources, source_urls,
             source_id, description, jd_full,
             first_seen, last_seen, score, score_breakdown, pipeline_status,
             classification)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "test|html-tag-job|remote",
            "Software Engineer",
            "Acme Corp",
            "Remote",
            '["glassdoor"]',
            '["https://glassdoor.com/jobs/200"]',
            "job-200",
            "&lt;p&gt;Job overview&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Build systems&lt;/li&gt;&lt;li&gt;Lead teams&lt;/li&gt;&lt;/ul&gt;",
            None,
            "2026-03-01T10:00:00",
            "2026-03-09T10:00:00",
            7.5,
            '{"skills": 0.7}',
            "discovered",
            "consider",  # v3 classification replacing legacy haiku_score=65
        ),
    )
    conn.commit()
    conn.close()
    return app


@pytest.fixture
def html_tag_client(app_with_html_tag_job):
    """Test client for the app with entity-encoded HTML tag job."""
    return app_with_html_tag_job.test_client()


# ---------------------------------------------------------------------------
# Data Quality tests (Phase 34)
# ---------------------------------------------------------------------------


class TestDataQuality:
    """Tests for Phase 34 data quality: HTML entity handling and jd_full display."""

    def test_expand_description_decodes_entities(self, entity_client):
        """Expanded row description decodes HTML entities like &amp; and &#39;."""
        response = entity_client.get(
            "/jobs/test%7Centity-job%7Cremote/expand", headers={"HX-Request": "true"}
        )
        assert response.status_code == 200
        data = response.data.decode()
        # &amp; should be decoded to & (then re-escaped by Jinja2 as &amp; in HTML)
        # The key check: no double-encoded &amp;amp;
        assert "&amp;amp;" not in data
        # &#39; should be decoded to apostrophe, then re-escaped by Jinja2 as &#39;
        # The key check: no double-encoded &amp;#39; (that would show literal &#39; in browser)
        assert "&amp;#39;" not in data

    def test_expand_description_uses_format_filter(self, entity_client):
        """Expanded row renders description through format_description (has HTML structure)."""
        response = entity_client.get(
            "/jobs/test%7Centity-job%7Cremote/expand", headers={"HX-Request": "true"}
        )
        assert response.status_code == 200
        data = response.data.decode()
        # format_description produces structured HTML (not raw text)
        # Check for list items from bullet points or paragraph tags
        assert "<li" in data or "<p" in data or "<h4" in data

    def test_expand_jd_full_renders_formatted(self, entity_client):
        """Expanded row jd_full uses format_description, not whitespace-pre-wrap."""
        response = entity_client.get(
            "/jobs/test%7Centity-job%7Cremote/expand", headers={"HX-Request": "true"}
        )
        assert response.status_code == 200
        data = response.data.decode()
        # jd_full section should exist
        assert "Job Description (full)" in data
        # Should NOT have double-encoded &amp;#39; — that would show as literal &#39; in browser
        # (format_description decodes entities before re-escaping)
        assert "&amp;#39;" not in data
        # jd_full container should NOT have whitespace-pre-wrap (format_description produces structured HTML)
        assert "whitespace-pre-wrap" not in data

    def test_detail_inline_jd_full_renders_formatted(self, entity_client):
        """Detail-inline view jd_full uses format_description, not whitespace-pre-wrap."""
        response = entity_client.get(
            "/jobs/test%7Centity-job%7Cremote/detail-inline", headers={"HX-Request": "true"}
        )
        assert response.status_code == 200
        data = response.data.decode()
        assert "Full Job Description" in data
        # No double-encoded entities (e.g., &amp;#39; would display as literal &#39; in browser)
        assert "&amp;#39;" not in data
        assert "&amp;amp;" not in data

    def test_expand_shows_full_jd_content(self, entity_client):
        """Expanded row jd_full shows the actual content, not truncated or empty."""
        response = entity_client.get(
            "/jobs/test%7Centity-job%7Cremote/expand", headers={"HX-Request": "true"}
        )
        assert response.status_code == 200
        data = response.data.decode()
        assert "Build data pipelines" in data
        assert "cross-functional teams" in data

    def test_expand_strips_html_tags_from_entity_encoded_descriptions(self, html_tag_client):
        """GAP 1: Entity-encoded HTML tags stripped to plain text, not shown as &lt;p&gt; literals.

        Descriptions stored with &lt;p&gt;, &lt;ul&gt;, &lt;li&gt; should have the text
        extracted cleanly. The tag strings themselves must not appear in the rendered output.
        """
        response = html_tag_client.get(
            "/jobs/test%7Chtml-tag-job%7Cremote/expand", headers={"HX-Request": "true"}
        )
        assert response.status_code == 200
        data = response.data.decode()
        # Text content is preserved
        assert "Job overview" in data
        assert "Build systems" in data
        # Entity-encoded tag artifacts must NOT appear in the rendered HTML
        assert "&lt;p&gt;" not in data
        assert "&lt;ul&gt;" not in data
        assert "&lt;li&gt;" not in data

    def test_expand_hides_description_when_jd_full_exists(self, entity_client):
        """GAP 2 (expand): Description container absent when jd_full is present.

        When a job has both description and jd_full, the description section (max-w-4xl div)
        must be suppressed in the expanded row so users see only the full JD.
        """
        response = entity_client.get(
            "/jobs/test%7Centity-job%7Cremote/expand", headers={"HX-Request": "true"}
        )
        assert response.status_code == 200
        data = response.data.decode()
        # Description container class must not appear (jd_full is a superset)
        assert "max-w-4xl" not in data
        # Full JD section must be present
        assert "Job Description (full)" in data

    def test_detail_hides_description_when_jd_full_exists(self, entity_client):
        """GAP 2 (detail-inline): Description card absent when jd_full is present.

        The detail-inline view must suppress the Description card when jd_full exists
        and show Full Job Description instead.
        """
        response = entity_client.get(
            "/jobs/test%7Centity-job%7Cremote/detail-inline", headers={"HX-Request": "true"}
        )
        assert response.status_code == 200
        data = response.data.decode()
        # Full JD section must be present
        assert "Full Job Description" in data
        # The Description card h3 must not appear as a standalone section
        # (the word "Description" only appears inside "Full Job Description")
        # Count occurrences: "Full Job Description" contains "Description", but
        # a standalone "Description" h3 header must not exist
        assert "<h3" not in data or "Full Job Description" in data

    def test_expand_shows_description_when_no_jd_full(self, html_tag_client):
        """GAP 2 (description-only): Description renders when jd_full is absent.

        Jobs without jd_full must show the description section. The full JD section
        must NOT appear since there is no jd_full to show.
        """
        response = html_tag_client.get(
            "/jobs/test%7Chtml-tag-job%7Cremote/expand", headers={"HX-Request": "true"}
        )
        assert response.status_code == 200
        data = response.data.decode()
        # Description text content is rendered
        assert "Job overview" in data
        # Full JD section must NOT appear (no jd_full)
        assert "Job Description (full)" not in data


# ---------------------------------------------------------------------------
# Fixtures for button disable/spinner tests (Phase 35, Plan 02)
# ---------------------------------------------------------------------------


@pytest.fixture
def client_with_scored_job(tmp_db_path):
    """Flask app + client with a Sonnet-scored job that shows all action buttons."""
    import sqlite3

    from job_finder.web import create_app

    test_config = {
        "db": {"path": tmp_db_path},
        "scoring": {"min_score_threshold": 40},
        "profile": {
            "target_titles": ["Staff Data Scientist"],
            "target_locations": ["Remote"],
            "min_salary": 150000,
            "industries": [],
            "exclusions": {"title_keywords": [], "companies": []},
            "skills": [],
        },
        "sources": {},
        "output": {"default_format": "cli", "max_results": 50},
    }
    application = create_app(config=test_config)
    application.config["TESTING"] = True

    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """INSERT INTO jobs
            (dedup_key, title, company, location, sources, source_urls,
             source_id, salary_min, salary_max, description,
             first_seen, last_seen, pipeline_status,
             classification, sub_scores_json,
             jd_full, fit_analysis)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "acme|data-scientist|remote",
            "Data Scientist",
            "Acme Corp",
            "Remote",
            '["linkedin"]',
            '["https://linkedin.com/jobs/1"]',
            "job-1",
            150000,
            200000,
            "Build ML models for Acme Corp",
            "2026-03-01T10:00:00",
            "2026-03-09T10:00:00",
            "reviewing",
            "apply",  # v3.0 classification
            '{"title_fit": 4, "location_fit": 5, "comp_fit": 4, "domain_match": 4, "seniority_match": 4, "skills_match": 4}',
            "Full job description text here for testing",
            '{"strengths": ["ML experience"], "gaps": [], "resume_priority_skills": ["Python"]}',
        ),
    )
    conn.commit()
    conn.close()

    return application.test_client()


# ---------------------------------------------------------------------------
# Button disable and spinner tests (Phase 35, Plan 02)
# ---------------------------------------------------------------------------


class TestButtonDisableAndSpinner:
    """Every HTMX action button must have hx-disable-elt and spinner indicator."""

    def test_expanded_row_buttons_have_disable(self, client_with_scored_job):
        """All action buttons in expanded row have hx-disable-elt='this'."""
        response = client_with_scored_job.get(
            "/jobs/acme%7Cdata-scientist%7Cremote/expand", headers={"HX-Request": "true"}
        )
        assert response.status_code == 200
        html = response.data.decode()

        # Count hx-disable-elt occurrences — expect at least 4
        # (Collapse, View Full Detail, Re-score, Archive)
        import re

        disable_count = len(re.findall(r'hx-disable-elt="this"', html))
        assert disable_count >= 4, f"Expected >= 4 hx-disable-elt, found {disable_count}"

    def test_expanded_row_buttons_have_spinners(self, client_with_scored_job):
        """All action buttons in expanded row have htmx-indicator spinners."""
        response = client_with_scored_job.get(
            "/jobs/acme%7Cdata-scientist%7Cremote/expand", headers={"HX-Request": "true"}
        )
        assert response.status_code == 200
        html = response.data.decode()

        # Count spinner indicators — expect at least 4
        import re

        spinner_count = len(re.findall(r"htmx-indicator", html))
        assert spinner_count >= 4, f"Expected >= 4 htmx-indicator, found {spinner_count}"

    def test_collapse_button_has_disable(self, client_with_scored_job):
        """Collapse button specifically has hx-disable-elt."""
        response = client_with_scored_job.get(
            "/jobs/acme%7Cdata-scientist%7Cremote/expand", headers={"HX-Request": "true"}
        )
        html = response.data.decode()
        # Find the collapse button section
        collapse_idx = html.find("Collapse")
        assert collapse_idx > 0, "Collapse button not found"
        # Check backwards for hx-disable-elt within the button tag
        button_start = html.rfind("<button", 0, collapse_idx)
        button_section = html[button_start : collapse_idx + 50]
        assert 'hx-disable-elt="this"' in button_section, "Collapse button missing hx-disable-elt"

    def test_view_full_detail_has_disable(self, client_with_scored_job):
        """View Full Detail button has hx-disable-elt."""
        response = client_with_scored_job.get(
            "/jobs/acme%7Cdata-scientist%7Cremote/expand", headers={"HX-Request": "true"}
        )
        html = response.data.decode()
        detail_idx = html.find("View Full Detail")
        assert detail_idx > 0, "View Full Detail button not found"
        button_start = html.rfind("<button", 0, detail_idx)
        button_section = html[button_start : detail_idx + 50]
        assert 'hx-disable-elt="this"' in button_section, "View Full Detail missing hx-disable-elt"


class TestSmoothScroll:
    """Smoke tests for smooth scroll JS on job board (UI-02)."""

    def test_row_onclick_contains_scroll(self, jobs_client):
        """Compact row onclick includes scrollIntoView after expand."""
        response = jobs_client.get("/jobs")
        data = response.data.decode()
        assert "scrollIntoView" in data

    def test_scroll_uses_delay(self, jobs_client):
        """Scroll uses setTimeout delay to avoid jank."""
        response = jobs_client.get("/jobs")
        data = response.data.decode()
        assert "setTimeout" in data

    def test_scroll_on_expand_and_collapse(self, jobs_client):
        """Scroll fires on both expand and collapse paths."""
        response = jobs_client.get("/jobs")
        data = response.data.decode()
        assert "isExpanding" in data
        # Collapse branch also has scrollIntoView
        assert data.count("scrollIntoView") >= 2


# ---------------------------------------------------------------------------
# Save JD tests (Phase 36, Plan 01)
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_jd_full_job(tmp_db_path):
    """Flask app with a job that has jd_full and haiku_score set."""
    import sqlite3

    from job_finder.web import create_app

    test_config = {
        "db": {"path": tmp_db_path},
        "scoring": {"min_score_threshold": 40},
        "profile": {
            "target_titles": ["ML Engineer"],
            "target_locations": ["Remote"],
            "min_salary": 150000,
            "industries": [],
            "exclusions": {"title_keywords": [], "companies": []},
            "skills": [],
        },
        "sources": {},
        "output": {"default_format": "cli", "max_results": 50},
    }
    app = create_app(config=test_config)
    app.config["TESTING"] = True

    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """INSERT INTO jobs
            (dedup_key, title, company, location, sources, source_urls,
             source_id, salary_min, salary_max, description, jd_full,
             first_seen, last_seen, score, score_breakdown, pipeline_status,
             classification)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "test|jd-full-job|remote",
            "ML Engineer",
            "TestCo",
            "Remote",
            '["linkedin"]',
            '["https://linkedin.com/jobs/99"]',
            "job-99",
            160000,
            220000,
            "Build ML infrastructure",
            "This is a full job description for testing.\nLine 2 of description.\nRequirements: Python, SQL",
            "2026-03-01T10:00:00",
            "2026-03-09T10:00:00",
            8.0,
            '{"skills": 0.8}',
            "discovered",
            "consider",  # v3 classification replacing legacy haiku_score=72
        ),
    )
    conn.commit()
    conn.close()

    return app


@pytest.fixture
def jd_full_client(app_with_jd_full_job):
    """Test client for the app with a jd_full job."""
    return app_with_jd_full_job.test_client()


@pytest.fixture
def app_with_scored_jobs(tmp_db_path):
    """App with one job per scoring state for score-cell rendering tests.

    Each row exercises a distinct (classification, sub_scores) combination so
    tests can assert color/value/tooltip rendering per state without ambiguity.
    """
    import sqlite3

    from job_finder.web import create_app

    test_config = {
        "db": {"path": tmp_db_path},
        "scoring": {"min_score_threshold": 40},
        "profile": {
            "target_titles": ["ML Engineer"],
            "target_locations": ["Remote"],
            "min_salary": 150000,
            "industries": [],
            "exclusions": {"title_keywords": [], "companies": []},
            "skills": [],
        },
        "sources": {},
        "output": {"default_format": "cli", "max_results": 50},
    }
    app = create_app(config=test_config)
    app.config["TESTING"] = True

    rows = [
        # (dedup_key, title, classification, sub_scores_json, fit_analysis)
        # Apply, max sum 30 (all 5s)
        (
            "ax|max-apply|remote",
            "Max Apply Role",
            "apply",
            '{"title_fit": 5, "location_fit": 5, "comp_fit": 5, "domain_match": 5, "seniority_match": 5, "skills_match": 5}',
            '{"strengths": ["Deep platform experience aligns with infra-heavy stack"], "gaps": ["No published Kubernetes operator work"], "talking_points": [], "resume_priority_skills": []}',
        ),
        # Apply, min sum 18 (all 3s)
        (
            "ax|min-apply|remote",
            "Min Apply Role",
            "apply",
            '{"title_fit": 3, "location_fit": 3, "comp_fit": 3, "domain_match": 3, "seniority_match": 3, "skills_match": 3}',
            '{"strengths": ["Adequate match"], "gaps": ["Borderline fit"], "talking_points": [], "resume_priority_skills": []}',
        ),
        # Consider, sum 22
        (
            "ax|consider-22|remote",
            "Consider Role",
            "consider",
            '{"title_fit": 4, "location_fit": 3, "comp_fit": 4, "domain_match": 4, "seniority_match": 4, "skills_match": 3}',
            '{"strengths": ["Strong technical match"], "gaps": ["Comp below target"], "talking_points": [], "resume_priority_skills": []}',
        ),
        # Skip, sum 22 (one axis at 2)
        (
            "ax|skip-22|remote",
            "Skip Role",
            "skip",
            '{"title_fit": 5, "location_fit": 5, "comp_fit": 2, "domain_match": 5, "seniority_match": 5, "skills_match": 0}',
            '{"strengths": [], "gaps": ["Compensation well below market"], "talking_points": [], "resume_priority_skills": []}',
        ),
        # Reject, sum 6 (all 1s would auto-reject; legitimacy_note also rejects)
        (
            "ax|reject-6|remote",
            "Reject Role",
            "reject",
            '{"title_fit": 1, "location_fit": 1, "comp_fit": 1, "domain_match": 1, "seniority_match": 1, "skills_match": 1}',
            '{"strengths": [], "gaps": ["Multiple critical mismatches"], "talking_points": [], "resume_priority_skills": []}',
        ),
        # Low signal: enrichment exhausted + jd_full short. Sub-scores irrelevant
        # for color/rank rendering; the template branches solely on classification.
        (
            "ax|low-signal-18|remote",
            "Low Signal Role",
            "low_signal",
            '{"title_fit": 3, "location_fit": 3, "comp_fit": 3, "domain_match": 3, "seniority_match": 3, "skills_match": 3}',
            '{"strengths": ["Insufficient JD signal"], "gaps": ["No description available"], "talking_points": [], "resume_priority_skills": []}',
        ),
        # Unscored: classification + sub_scores_json + fit_analysis all NULL
        ("ax|unscored|remote", "Unscored Role", None, None, None),
        # Apply with no fit_analysis (rationale-missing edge case)
        (
            "ax|apply-no-rationale|remote",
            "Apply No Rationale",
            "apply",
            '{"title_fit": 5, "location_fit": 4, "comp_fit": 5, "domain_match": 4, "seniority_match": 5, "skills_match": 4}',
            None,
        ),
        # Apply with empty strengths/gaps lists
        (
            "ax|apply-empty-lists|remote",
            "Apply Empty Lists",
            "apply",
            '{"title_fit": 4, "location_fit": 4, "comp_fit": 4, "domain_match": 4, "seniority_match": 4, "skills_match": 4}',
            '{"strengths": [], "gaps": [], "talking_points": [], "resume_priority_skills": []}',
        ),
        # Apply with very long strength (truncation test)
        (
            "ax|apply-long-strength|remote",
            "Apply Long Strength",
            "apply",
            '{"title_fit": 4, "location_fit": 4, "comp_fit": 4, "domain_match": 4, "seniority_match": 4, "skills_match": 4}',
            '{"strengths": ["'
            + ("A" * 200)
            + '"], "gaps": ["'
            + ("B" * 200)
            + '"], "talking_points": [], "resume_priority_skills": []}',
        ),
    ]

    conn = sqlite3.connect(tmp_db_path)
    for dk, title, cls, subs, fit in rows:
        conn.execute(
            """INSERT INTO jobs
                (dedup_key, title, company, location, sources, source_urls,
                 source_id, salary_min, salary_max, description,
                 first_seen, last_seen, score, score_breakdown, pipeline_status,
                 classification, sub_scores_json, fit_analysis)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                dk,
                title,
                "TestCo",
                "Remote",
                '["test"]',
                '["https://test/jobs/1"]',
                dk,
                100000,
                200000,
                "desc",
                "2026-04-27T10:00:00",
                "2026-04-27T10:00:00",
                0.0,
                "{}",
                "discovered",
                cls,
                subs,
                fit,
            ),
        )
    conn.commit()
    conn.close()

    return app


@pytest.fixture
def scored_client(app_with_scored_jobs):
    return app_with_scored_jobs.test_client()


class TestUXPolish:
    """Tests for Phase 15 UX polish: spinners, OOB score, jd_full display."""

    # --- UX-03: jd_full collapsible display ---

    def test_expand_with_jd_full_shows_details(self, jd_full_client):
        """Expanded row shows <details> section when job has jd_full."""
        response = jd_full_client.get(
            "/jobs/test%7Cjd-full-job%7Cremote/expand", headers={"HX-Request": "true"}
        )
        assert response.status_code == 200
        data = response.data.decode()
        assert "<details" in data
        assert "Job Description (full)" in data
        assert "full job description for testing" in data

    def test_expand_without_jd_full_no_details(self, jobs_client):
        """Expanded row has no <details> JD section when jd_full is absent."""
        response = jobs_client.get(
            "/jobs/acme%7Cdata-scientist%7Cremote/expand", headers={"HX-Request": "true"}
        )
        assert response.status_code == 200
        data = response.data.decode()
        assert "Job Description (full)" not in data

    def test_detail_inline_with_jd_full_shows_details(self, jd_full_client):
        """Detail-inline view shows <details> section when job has jd_full."""
        response = jd_full_client.get(
            "/jobs/test%7Cjd-full-job%7Cremote/detail-inline", headers={"HX-Request": "true"}
        )
        assert response.status_code == 200
        data = response.data.decode()
        assert "<details" in data
        assert "Full Job Description" in data

    # --- UX-01: Spinner loading indicators ---

    def test_rescore_button_has_indicator(self, jd_full_client):
        """Re-score button has spinner with htmx-indicator class and hx-disable-elt."""
        response = jd_full_client.get(
            "/jobs/test%7Cjd-full-job%7Cremote/expand", headers={"HX-Request": "true"}
        )
        data = response.data.decode()
        assert 'hx-disable-elt="this"' in data
        assert "htmx-indicator" in data
        assert "animate-spin" in data

    def test_paste_jd_button_has_indicator(self, jobs_client):
        """Paste-JD submit button has spinner with htmx-indicator class."""
        # Use a job WITHOUT jd_full so paste-JD form is visible
        response = jobs_client.get(
            "/jobs/acme%7Cdata-scientist%7Cremote/expand", headers={"HX-Request": "true"}
        )
        data = response.data.decode()
        assert "Score with JD" in data
        assert "htmx-indicator" in data

    # --- UX-02: OOB score cell ---

    def test_compact_row_score_has_id(self, jd_full_client):
        """Compact row score cell has id='score-...' for OOB targeting."""
        response = jd_full_client.get("/jobs")
        data = response.data.decode()
        assert 'id="score-test%7Cjd-full-job%7Cremote"' in data

    def test_rescore_response_has_oob_score_cell(self, jd_full_client):
        """POST rescore response includes OOB score cell in <template> wrapper."""
        response = jd_full_client.post("/jobs/test%7Cjd-full-job%7Cremote/rescore")
        assert response.status_code == 200
        data = response.data.decode()
        assert "<template>" in data
        assert 'hx-swap-oob="outerHTML"' in data
        assert 'id="score-test%7Cjd-full-job%7Cremote"' in data

    def test_rescore_response_no_hx_trigger_header(self, jd_full_client):
        """POST rescore response does NOT set HX-Trigger (avoids full table reload)."""
        response = jd_full_client.post("/jobs/test%7Cjd-full-job%7Cremote/rescore")
        assert response.status_code == 200
        assert response.headers.get("HX-Trigger") is None

    def test_paste_jd_response_has_oob_score_cell(self, jobs_client):
        """POST paste-jd response includes OOB score cell in <template> wrapper."""
        response = jobs_client.post(
            "/jobs/acme%7Cdata-scientist%7Cremote/paste-jd",
            data={"jd_text": "Full job description text for testing purposes."},
        )
        assert response.status_code == 200
        data = response.data.decode()
        assert "<template>" in data
        assert 'hx-swap-oob="outerHTML"' in data

    def test_score_cell_route_returns_td(self, jd_full_client):
        """GET /jobs/{dedup_key}/score-cell returns 200 with score <td> element."""
        response = jd_full_client.get("/jobs/test%7Cjd-full-job%7Cremote/score-cell")
        assert response.status_code == 200
        data = response.data.decode()
        assert '<td id="score-' in data

    def test_score_cell_route_404(self, jd_full_client):
        """GET /jobs/nonexistent/score-cell returns 404."""
        response = jd_full_client.get("/jobs/no%7Csuch%7Cjob/score-cell")
        assert response.status_code == 404

    def test_expand_no_load_trigger(self, jd_full_client):
        """Regular expand does NOT include hx-trigger=load (no spurious score refresh)."""
        response = jd_full_client.get(
            "/jobs/test%7Cjd-full-job%7Cremote/expand", headers={"HX-Request": "true"}
        )
        assert response.status_code == 200
        data = response.data.decode()


class TestSaveJD:
    """Tests for POST /jobs/<key>/save-jd route (UI-01)."""

    def test_save_jd_returns_200(self, jobs_client):
        response = jobs_client.post(
            "/jobs/acme%7Cdata-scientist%7Cremote/save-jd",
            data={"jd_text": "Full job description for testing."},
        )
        assert response.status_code == 200

    def test_save_jd_writes_jd_full_to_db(self, jobs_client, tmp_db_path):
        import sqlite3

        jobs_client.post(
            "/jobs/acme%7Cdata-scientist%7Cremote/save-jd",
            data={"jd_text": "Saved JD content here."},
        )
        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT jd_full FROM jobs WHERE dedup_key = ?",
            ("acme|data-scientist|remote",),
        ).fetchone()
        conn.close()
        assert row["jd_full"] == "Saved JD content here."

    def test_save_jd_empty_text_returns_error(self, jobs_client):
        response = jobs_client.post(
            "/jobs/acme%7Cdata-scientist%7Cremote/save-jd",
            data={"jd_text": ""},
        )
        assert response.status_code == 200
        assert b"Please provide a job description" in response.data

    def test_save_jd_no_scoring_triggered(self, jobs_client, tmp_db_path):
        """save-jd only persists jd_full; it does not trigger scoring.

        Plan 5: checks that v3 scoring surface (`classification`,
        `sub_scores_json`) is NOT populated as a side-effect of saving the
        JD text. The previous assertion relied on `sonnet_score IS NULL`
        against a column that has since been dropped by Migration 41.
        """
        import sqlite3

        jobs_client.post(
            "/jobs/acme%7Cdata-scientist%7Cremote/save-jd",
            data={"jd_text": "Some JD text."},
        )
        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT classification, sub_scores_json, jd_full FROM jobs WHERE dedup_key = ?",
            ("acme|data-scientist|remote",),
        ).fetchone()
        conn.close()
        # JD text is persisted...
        assert row["jd_full"] == "Some JD text."
        # ...but no rescore was triggered, so v3 scoring columns remain NULL.
        assert row["classification"] is None
        assert row["sub_scores_json"] is None

    def test_save_jd_no_hx_trigger_header(self, jobs_client):
        """save-jd response should NOT have HX-Trigger header (no table re-sort)."""
        response = jobs_client.post(
            "/jobs/acme%7Cdata-scientist%7Cremote/save-jd",
            data={"jd_text": "Some JD text."},
        )
        assert "HX-Trigger" not in response.headers

    def test_save_jd_logs_activity(self, jobs_client, tmp_db_path):
        import sqlite3

        jobs_client.post(
            "/jobs/acme%7Cdata-scientist%7Cremote/save-jd",
            data={"jd_text": "JD text for activity test."},
        )
        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT action FROM user_activity WHERE entity_id = ? ORDER BY id DESC LIMIT 1",
            ("acme|data-scientist|remote",),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["action"] == "save_jd"

    def test_save_jd_nonexistent_key_returns_404(self, jobs_client):
        response = jobs_client.post(
            "/jobs/nonexistent-key/save-jd",
            data={"jd_text": "Some text."},
        )
        assert response.status_code == 404

    def test_save_jd_response_contains_jd_saved_toast(self, jobs_client):
        response = jobs_client.post(
            "/jobs/acme%7Cdata-scientist%7Cremote/save-jd",
            data={"jd_text": "Full JD for toast test."},
        )
        assert b"JD saved" in response.data

    def test_jd_edit_form_returns_200(self, jd_full_client):
        response = jd_full_client.get("/jobs/test%7Cjd-full-job%7Cremote/jd-edit-form")
        assert response.status_code == 200

    def test_jd_edit_form_contains_prefilled_textarea(self, jd_full_client):
        response = jd_full_client.get("/jobs/test%7Cjd-full-job%7Cremote/jd-edit-form")
        data = response.data.decode()
        assert "<textarea" in data
        assert "full job description for testing" in data

    def test_jd_edit_form_nonexistent_key_returns_404(self, jd_full_client):
        response = jd_full_client.get("/jobs/nonexistent-key/jd-edit-form")
        assert response.status_code == 404

    def test_save_jd_button_is_type_button(self, jobs_client):
        """Save JD button must be type=button to prevent native form submission."""
        response = jobs_client.get(
            "/jobs/acme%7Cdata-scientist%7Cremote/expand", headers={"HX-Request": "true"}
        )
        data = response.data.decode()
        # Find the save-jd button and verify it's type=button
        assert 'type="button"' in data
        assert 'hx-include="closest form"' in data

    def test_edit_button_has_prevent_default(self, jd_full_client):
        """Edit button inside summary must preventDefault to avoid details toggle."""
        response = jd_full_client.get(
            "/jobs/test%7Cjd-full-job%7Cremote/expand", headers={"HX-Request": "true"}
        )
        data = response.data.decode()
        assert "preventDefault" in data


# ---------------------------------------------------------------------------
# Gap closure tests (Phase 35, Plan 03)
# ---------------------------------------------------------------------------


class TestGapClosureFixes:
    """Tests for UAT gap fixes: event bubbling, CSS-safe selectors, resume wrapper."""

    def test_rescore_button_no_after_request_dispatch(self, client_with_scored_job):
        """Re-score button does NOT have hx-on::after-request (OOB score cell used instead)."""
        response = client_with_scored_job.get(
            "/jobs/acme%7Cdata-scientist%7Cremote/expand", headers={"HX-Request": "true"}
        )
        assert response.status_code == 200
        html = response.data.decode()
        # Re-score button must NOT dispatch jobs-updated (causes full table reload)
        assert "Re-score" in html
        # The hx-on::after-request for jobs-updated should NOT be on any scoring button

        rescore_idx = html.find("Re-score")
        button_start = html.rfind("<button", 0, rescore_idx)
        button_end = html.find("</button>", rescore_idx)
        rescore_button = html[button_start:button_end]
        assert "hx-on::after-request" not in rescore_button

    def test_archive_button_target_is_css_safe(self, client_with_scored_job):
        """Archive button hx-target does not contain % characters (CSS-safe selector)."""
        response = client_with_scored_job.get(
            "/jobs/acme%7Cdata-scientist%7Cremote/expand", headers={"HX-Request": "true"}
        )
        assert response.status_code == 200
        html = response.data.decode()
        # Find the archive button target value and verify it has no % chars
        import re

        # Match hx-target="#status-cell-..." on the Archive button
        matches = re.findall(r'hx-target="#status-cell-([^"]*)"', html)
        assert len(matches) > 0, "Archive button hx-target not found"
        for target_val in matches:
            assert "%" not in target_val, (
                f"Archive hx-target contains invalid CSS selector char '%': {target_val}"
            )

    def test_compact_row_status_cell_id_is_css_safe(self, client_with_scored_job):
        """Compact row status cell <td> id does not contain % characters."""

        # The client_with_scored_job uses dedup_key "acme|data-scientist|remote"
        # which encodes to %7C -- verify the rendered id has no % chars
        response = client_with_scored_job.get("/jobs/table", headers={"HX-Request": "true"})
        assert response.status_code == 200
        html = response.data.decode()
        import re

        matches = re.findall(r'id="status-cell-([^"]*)"', html)
        assert len(matches) > 0, "status-cell id not found in /jobs/table"
        for cell_id in matches:
            assert "%" not in cell_id, f"status-cell id contains invalid CSS char '%': {cell_id}"

    def test_score_with_jd_button_no_after_request_dispatch(self, jobs_client):
        """Score with JD button does NOT have hx-on::after-request (OOB score cell used instead)."""
        # Use a job without jd_full so the paste-JD form renders
        response = jobs_client.get(
            "/jobs/acme%7Cdata-scientist%7Cremote/expand", headers={"HX-Request": "true"}
        )
        assert response.status_code == 200
        html = response.data.decode()
        assert "Score with JD" in html
        # The Score with JD button must NOT dispatch jobs-updated

        score_idx = html.find("Score with JD")
        button_start = html.rfind("<button", 0, score_idx)
        button_end = html.find("</button>", score_idx)
        score_button = html[button_start:button_end]
        assert "hx-on::after-request" not in score_button

    # --- REACT-01: data-sort-score in OOB score cell (Gap 1) ---

    def test_rescore_oob_score_cell_has_data_sort_score(self, jd_full_client):
        """POST rescore OOB score cell in <template> contains data-sort-score attribute."""
        response = jd_full_client.post("/jobs/test%7Cjd-full-job%7Cremote/rescore")
        assert response.status_code == 200
        html = response.data.decode()
        # Extract the <template>...</template> OOB section
        template_start = html.find("<template>")
        template_end = html.find("</template>")
        assert template_start != -1, "<template> block not found in rescore response"
        template_section = html[template_start:template_end]
        assert "data-sort-score=" in template_section, (
            "data-sort-score attribute missing from OOB score cell in <template> block"
        )

    def test_paste_jd_oob_score_cell_has_data_sort_score(self, jobs_client):
        """POST paste-jd OOB score cell in <template> contains data-sort-score attribute."""
        response = jobs_client.post(
            "/jobs/acme%7Cdata-scientist%7Cremote/paste-jd",
            data={"jd_text": "Full job description text for Nyquist gap coverage."},
        )
        assert response.status_code == 200
        html = response.data.decode()
        template_start = html.find("<template>")
        template_end = html.find("</template>")
        assert template_start != -1, "<template> block not found in paste-jd response"
        template_section = html[template_start:template_end]
        assert "data-sort-score=" in template_section, (
            "data-sort-score attribute missing from OOB score cell in <template> block"
        )

    # --- REACT-01: paste_jd has no HX-Trigger header (Gap 2) ---

    def test_paste_jd_response_no_hx_trigger_header(self, jobs_client):
        """POST paste-jd does NOT return HX-Trigger header (OOB score cell used instead)."""
        response = jobs_client.post(
            "/jobs/acme%7Cdata-scientist%7Cremote/paste-jd",
            data={"jd_text": "Full job description text for Nyquist gap coverage."},
        )
        assert response.status_code == 200
        assert response.headers.get("HX-Trigger") is None, (
            "paste-jd must NOT emit HX-Trigger (would cause full table reload, "
            "conflicting with OOB score cell approach)"
        )


# ---------------------------------------------------------------------------
# Tests for ATS scan exception handler separation (QUAL-01)
# ---------------------------------------------------------------------------


class TestScanExceptionSeparation:
    """Verify that template rendering errors are distinct from scan logic errors."""

    def test_scan_logic_error_returns_scan_failure_message(self, app_with_companies):
        """POST /companies/scan where run_ats_scan raises RuntimeError returns 200 with error."""
        from unittest.mock import patch

        client = app_with_companies.test_client()
        with (
            patch(
                "job_finder.web.blueprints.companies.run_ats_scan",
                side_effect=RuntimeError("connection timeout"),
            ),
            patch(
                "job_finder.web.blueprints.companies.probe_ats_slugs",
                return_value={"probed": 0},
            ),
        ):
            response = client.post("/companies/scan")

        assert response.status_code == 200
        data = response.data.decode()
        assert "connection timeout" in data

    def test_scan_template_error_not_reported_as_scan_failure(self, app_with_companies):
        """POST /companies/scan where run_ats_scan succeeds but render_template raises propagates.

        In Flask TESTING mode, unhandled exceptions propagate to the test instead of
        returning 500. We verify the exception is a TemplateSyntaxError (not swallowed
        as a generic scan failure), which confirms render_template is outside the try block.
        """
        from unittest.mock import patch

        import jinja2

        client = app_with_companies.test_client()
        with (
            patch(
                "job_finder.web.blueprints.companies.run_ats_scan",
                return_value={
                    "jobs_found": 5,
                    "companies_scanned": 2,
                    "html_scraped": 0,
                    "errors": [],
                },
            ),
            patch(
                "job_finder.web.blueprints.companies.probe_ats_slugs",
                return_value={"probed": 2},
            ),
            patch(
                "job_finder.web.blueprints.companies.render_template",
                side_effect=jinja2.TemplateSyntaxError("unexpected char", 1, filename="test.html"),
            ),
        ):
            # Template error must propagate (not be swallowed as "ATS scan failed")
            with pytest.raises(jinja2.TemplateSyntaxError):
                client.post("/companies/scan")


# ---------------------------------------------------------------------------
# Tests for date filter HTMX trigger (UI-01)
# ---------------------------------------------------------------------------


class TestDateFilterHtmxTrigger:
    """Verify date filter inputs have input event triggers for clearing."""

    def test_date_filter_has_input_trigger_for_date_from(self, client):
        """GET /jobs renders filter form with input trigger on date-from input."""
        response = client.get("/jobs")
        assert response.status_code == 200
        data = response.data.decode()
        assert "input from:#filter-date-from" in data, (
            "filter form hx-trigger must include 'input from:#filter-date-from' "
            "so clearing the date input fires an HTMX request"
        )

    def test_date_filter_has_input_trigger_for_date_to(self, client):
        """GET /jobs renders filter form with input trigger on date-to input."""
        response = client.get("/jobs")
        assert response.status_code == 200
        data = response.data.decode()
        assert "input from:#filter-date-to" in data, (
            "filter form hx-trigger must include 'input from:#filter-date-to' "
            "so clearing the date input fires an HTMX request"
        )


# ---------------------------------------------------------------------------
# Composite Score Cell tests (Phase 34 — v3.0 numeric badge)
# ---------------------------------------------------------------------------


class TestCompositeScoreCell:
    """v3.0 composite-score display: number + color + tooltip + packed sort key.

    Replaces the classification-badge rendering with a colored composite number
    (sum of 6 sub-scores, range 6-30).
    """

    def test_apply_row_renders_composite_30(self, scored_client):
        """Apply with all 5s renders composite '30'."""
        response = scored_client.get("/jobs")
        assert response.status_code == 200
        html = response.data.decode()
        idx = html.find('id="score-ax%7Cmax-apply%7Cremote"')
        assert idx != -1, "score cell for max-apply not rendered"
        cell = html[idx : idx + 800]
        assert ">30<" in cell, f"composite '30' not rendered in cell: {cell[:300]}"

    def test_apply_row_uses_green_color(self, scored_client):
        """Apply classification renders with text-green-400."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Cmax-apply%7Cremote"')
        cell = html[idx : idx + 800]
        assert "text-green-400" in cell

    def test_consider_row_uses_amber_color(self, scored_client):
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Cconsider-22%7Cremote"')
        cell = html[idx : idx + 800]
        assert "text-amber-400" in cell
        assert ">22<" in cell

    def test_skip_row_uses_amber_color_for_composite_22(self, scored_client):
        """skip-22 has composite 22 — color is now keyed off the number, so
        it's amber regardless of classification (≥18 amber threshold)."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Cskip-22%7Cremote"')
        cell = html[idx : idx + 800]
        assert "text-amber-400" in cell

    def test_reject_row_uses_red_color(self, scored_client):
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Creject-6%7Cremote"')
        cell = html[idx : idx + 800]
        assert "text-red-400" in cell
        assert ">6<" in cell

    def test_low_signal_row_uses_amber_color_for_composite_18(self, scored_client):
        """low_signal-18 has composite 18 — under threshold-based coloring it's
        amber (≥18 amber). Classification no longer drives the color."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Clow-signal-18%7Cremote"')
        assert idx != -1, "score cell for low_signal row not rendered"
        cell = html[idx : idx + 800]
        assert "text-amber-400" in cell
        assert ">18<" in cell

    def test_low_signal_sort_score_is_composite(self, scored_client):
        """sort_score is now the raw composite (no rank prefix): 18."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Clow-signal-18%7Cremote"')
        cell = html[idx : idx + 800]
        assert 'data-sort-score="18"' in cell

    def test_apply_min_renders_composite_18(self, scored_client):
        """Apply with all 3s (boundary) renders '18'."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Cmin-apply%7Cremote"')
        cell = html[idx : idx + 800]
        assert ">18<" in cell

    def test_badge_text_no_longer_present(self, scored_client):
        """Old badge background classes are removed from compact-row score cells."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        for old_badge in ('"bg-green-900', '"bg-amber-900', '"bg-red-900', '"bg-slate-800'):
            for dk in (
                "ax%7Cmax-apply%7Cremote",
                "ax%7Cconsider-22%7Cremote",
                "ax%7Cskip-22%7Cremote",
                "ax%7Creject-6%7Cremote",
            ):
                idx = html.find(f'id="score-{dk}"')
                cell = html[idx : idx + 800]
                assert old_badge not in cell, (
                    f"Old badge class {old_badge} still present in score cell {dk}"
                )

    # --- data-sort-score equals the raw composite (sum of 6 sub-scores) ---

    def test_apply_max_sort_score_is_30(self, scored_client):
        """Composite 30 -> data-sort-score='30' (no classification prefix)."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Cmax-apply%7Cremote"')
        cell = html[idx : idx + 800]
        assert 'data-sort-score="30"' in cell

    def test_apply_min_sort_score_is_18(self, scored_client):
        """Composite 18 -> data-sort-score='18'."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Cmin-apply%7Cremote"')
        cell = html[idx : idx + 800]
        assert 'data-sort-score="18"' in cell

    def test_consider_sort_score_is_22(self, scored_client):
        """Composite 22 -> data-sort-score='22'."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Cconsider-22%7Cremote"')
        cell = html[idx : idx + 800]
        assert 'data-sort-score="22"' in cell

    def test_skip_sort_score_is_22(self, scored_client):
        """skip-22 has composite 22 — same sort key as consider-22 now that
        classification no longer prefixes the sort. Pure-numeric ordering
        is what the user wants: a 22 is a 22, regardless of bucket."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Cskip-22%7Cremote"')
        cell = html[idx : idx + 800]
        assert 'data-sort-score="22"' in cell

    def test_reject_sort_score_is_6(self, scored_client):
        """Composite 6 -> data-sort-score='6'."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Creject-6%7Cremote"')
        cell = html[idx : idx + 800]
        assert 'data-sort-score="6"' in cell

    def test_unscored_sort_score_is_0(self, scored_client):
        """No sub_scores_json -> data-sort-score='0'."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Cunscored%7Cremote"')
        cell = html[idx : idx + 800]
        assert 'data-sort-score="0"' in cell

    # --- Tooltip content ---

    def test_tooltip_contains_all_six_axis_labels(self, scored_client):
        """Tooltip lists all 6 sub-score axes for an Apply row."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Cmax-apply%7Cremote"')
        cell = html[idx : idx + 1500]
        for label in ("Title", "Location", "Comp", "Domain", "Seniority", "Skills"):
            assert label in cell, f"Axis label '{label}' missing from tooltip in {cell[:300]}"

    def test_tooltip_contains_axis_values(self, scored_client):
        """Tooltip shows numeric values for each axis."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Cconsider-22%7Cremote"')
        cell = html[idx : idx + 1500]
        # consider-22: title 4, location 3, comp 4, domain 4, seniority 4, skills 3
        assert "4" in cell and "3" in cell

    def test_tooltip_includes_top_strength(self, scored_client):
        """Tooltip surfaces fit_analysis.strengths[0]."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Cmax-apply%7Cremote"')
        cell = html[idx : idx + 2500]
        assert "Deep platform experience aligns with infra-heavy stack" in cell

    def test_tooltip_includes_top_gap(self, scored_client):
        """Tooltip surfaces fit_analysis.gaps[0]."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Cmax-apply%7Cremote"')
        cell = html[idx : idx + 2500]
        assert "No published Kubernetes operator work" in cell

    def test_tooltip_omits_strength_when_empty(self, scored_client):
        """fit_analysis.strengths == [] -> no Strength: line."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Capply-empty-lists%7Cremote"')
        cell = html[idx : idx + 1500]
        assert "Strength:" not in cell
        assert "Gap:" not in cell

    def test_tooltip_works_when_fit_analysis_missing(self, scored_client):
        """fit_analysis NULL -> tooltip still renders sub-scores; no Strength/Gap lines."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Capply-no-rationale%7Cremote"')
        cell = html[idx : idx + 1500]
        assert "Title" in cell and "Skills" in cell
        assert "Strength:" not in cell
        assert "Gap:" not in cell

    def test_tooltip_truncates_long_strength(self, scored_client):
        """Strength text > 120 chars is truncated with ellipsis."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Capply-long-strength%7Cremote"')
        cell = html[idx : idx + 2000]
        long_a = "A" * 200
        truncated = "A" * 120 + "…"
        assert long_a not in cell, "Untruncated long strength leaked into tooltip"
        assert truncated in cell, "Expected truncated strength with ellipsis"

    def test_tooltip_uses_group_hover_pattern(self, scored_client):
        """Tooltip markup uses Tailwind group-hover (CSS-only, no JS)."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Cmax-apply%7Cremote"')
        cell = html[idx : idx + 1500]
        assert "group-hover" in cell

    def test_unscored_no_tooltip(self, scored_client):
        """NULL classification -> no tooltip markup."""
        response = scored_client.get("/jobs")
        html = response.data.decode()
        idx = html.find('id="score-ax%7Cunscored%7Cremote"')
        cell = html[idx : idx + 1500]
        assert "Strength:" not in cell
        assert "Gap:" not in cell
