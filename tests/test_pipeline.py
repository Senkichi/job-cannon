"""Tests for the Kanban pipeline view and /pipeline/move endpoint.

Tests cover:
- GET /pipeline returns 200 with Kanban structure
- POST /pipeline/move with valid data returns 200
- POST /pipeline/move creates a pipeline_events record with correct from/to status
- POST /pipeline/move with missing fields returns 400
- POST /pipeline/move with invalid status returns 400
"""

import os
import sqlite3
import tempfile

import pytest

from job_finder.web.db_migrate import run_migrations

# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_db_with_job():
    """Temp DB with migrations applied and one seed job in 'discovered' status."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    run_migrations(path)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """INSERT INTO jobs
               (dedup_key, title, company, location, sources, source_urls,
                salary_min, salary_max, first_seen, last_seen, score, pipeline_status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'), ?, ?)""",
        (
            "test-job-key-01",
            "Staff Data Scientist",
            "Acme Corp",
            "Remote",
            '["linkedin"]',
            '["https://linkedin.com/jobs/1"]',
            180000,
            240000,
            8.5,
            "discovered",
        ),
    )
    conn.commit()
    conn.close()

    yield path

    if os.path.exists(path):
        os.remove(path)

# intentional — local app/client fixtures use tmp_db_with_job (pre-seeded DB),
# not the conftest app fixture which uses an empty tmp_db_path.
@pytest.fixture
def app(tmp_db_with_job):
    """Flask test app wired to the temp DB with a seed job."""
    from job_finder.web import create_app

    test_config = {
        "db": {"path": tmp_db_with_job},
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
    return application

@pytest.fixture
def client(app):
    """Flask test client."""
    return app.test_client()

@pytest.fixture
def db_conn(tmp_db_with_job):
    """Direct sqlite3 connection to the temp DB for post-action assertions."""
    conn = sqlite3.connect(tmp_db_with_job)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()

# ── Tests ───────────────────────────────────────────────────────────────────

class TestPipelineView:
    def test_pipeline_returns_200(self, client):
        """GET /pipeline returns 200."""
        response = client.get("/pipeline")
        assert response.status_code == 200

    def test_pipeline_has_kanban_columns(self, client):
        """GET /pipeline response contains Kanban column structure markers."""
        response = client.get("/pipeline")
        assert b"kanban-column-body" in response.data

    def test_pipeline_shows_core_columns(self, client):
        """Core columns (Discovered, Reviewing, Applied) always present."""
        response = client.get("/pipeline")
        assert b"Discovered" in response.data
        assert b"Reviewing" in response.data
        assert b"Applied" in response.data

    def test_pipeline_shows_rejected_collapsed(self, client):
        """Rejected column header is present and marked as collapsed."""
        import re

        response = client.get("/pipeline")
        assert b"Rejected" in response.data
        html = response.data.decode()
        # Verify rejected-body div specifically has "hidden" class
        match = re.search(r'id="rejected-body"[^>]*class="([^"]*)"', html)
        assert match, "rejected-body div must exist with a class attribute"
        assert "hidden" in match.group(1), "rejected-body must have 'hidden' class"

    def test_pipeline_shows_seed_job_card(self, client):
        """Seed job card appears in the Discovered column."""
        response = client.get("/pipeline")
        assert b"Staff Data Scientist" in response.data

    def test_pipeline_card_has_sortable_data_attribute(self, client):
        """Job cards have data-job-id attribute for SortableJS."""
        response = client.get("/pipeline")
        assert b'data-job-id="test-job-key-01"' in response.data

    def test_pipeline_has_sortable_script(self, client):
        """Pipeline page includes SortableJS initialization script."""
        response = client.get("/pipeline")
        assert b"Sortable" in response.data
        assert b"kanban-column-body" in response.data

class TestPipelineMove:
    def test_move_valid_returns_200(self, client):
        """POST /pipeline/move with valid job_id and new_status returns 200."""
        response = client.post(
            "/pipeline/move",
            data={"job_id": "test-job-key-01", "new_status": "reviewing"},
        )
        assert response.status_code == 200

    def test_move_creates_pipeline_event(self, client, tmp_db_with_job):
        """POST /pipeline/move logs a pipeline_events row with correct from/to."""
        client.post(
            "/pipeline/move",
            data={"job_id": "test-job-key-01", "new_status": "reviewing"},
        )
        # Check the pipeline_events table directly
        conn = sqlite3.connect(tmp_db_with_job)
        conn.row_factory = sqlite3.Row
        event = conn.execute(
            "SELECT * FROM pipeline_events WHERE job_id = ? ORDER BY id DESC LIMIT 1",
            ("test-job-key-01",),
        ).fetchone()
        conn.close()

        assert event is not None, "No pipeline_events row was created"
        assert event["from_status"] == "discovered"
        assert event["to_status"] == "reviewing"
        assert event["source"] == "manual"

    def test_move_updates_job_status(self, client, tmp_db_with_job):
        """POST /pipeline/move updates the job's pipeline_status column."""
        client.post(
            "/pipeline/move",
            data={"job_id": "test-job-key-01", "new_status": "applied"},
        )
        conn = sqlite3.connect(tmp_db_with_job)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT pipeline_status FROM jobs WHERE dedup_key = ?",
            ("test-job-key-01",),
        ).fetchone()
        conn.close()

        assert row is not None
        assert row["pipeline_status"] == "applied"

    def test_move_missing_job_id_returns_400(self, client):
        """POST /pipeline/move without job_id returns 400."""
        response = client.post(
            "/pipeline/move",
            data={"new_status": "reviewing"},
        )
        assert response.status_code == 400

    def test_move_missing_status_returns_400(self, client):
        """POST /pipeline/move without new_status returns 400."""
        response = client.post(
            "/pipeline/move",
            data={"job_id": "test-job-key-01"},
        )
        assert response.status_code == 400

    def test_move_invalid_status_returns_400(self, client):
        """POST /pipeline/move with a made-up status returns 400."""
        response = client.post(
            "/pipeline/move",
            data={"job_id": "test-job-key-01", "new_status": "flying_pigs"},
        )
        assert response.status_code == 400
