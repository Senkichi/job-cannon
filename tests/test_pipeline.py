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
                salary_min, salary_max, first_seen, last_seen, pipeline_status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'), ?)""",
        (
            "test-job-key-01",
            "Staff Data Scientist",
            "Acme Corp",
            "Remote",
            '["linkedin"]',
            '["https://linkedin.com/jobs/1"]',
            180000,
            240000,
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


# ---------------------------------------------------------------------------
# Issue #214 — vestigial jobs.score column is not consulted by the Kanban path
# ---------------------------------------------------------------------------


class TestKanbanReadsCompositeNotLegacyScore:
    """Regression coverage for issue #214 (vestigial `jobs.score` column).

    Under v3.0 scoring the legacy `jobs.score` column carried no usable signal
    (rows scored via enrichment-backfill / re-sighting kept score=0.0 even with
    a full assessment in `sub_scores_json`). The Kanban board renders and sorts
    by the live 6-30 composite derived from `sub_scores_json`. m113 has since
    dropped the column outright; these tests keep the composite-only contract
    honest.
    """

    def test_kanban_card_shows_composite_when_score_is_zero(self, client, tmp_db_with_job):
        """A v3.0-scored row surfaces its non-zero composite badge.

        Seed shape mirrors the production "enrichment-backfilled" cohort: a
        populated sub_scores_json that sums to a non-zero composite (the legacy
        ``score`` column was dropped in m113). The card MUST display the
        composite badge.
        """
        conn = sqlite3.connect(tmp_db_with_job)
        conn.execute(
            """INSERT INTO jobs
                   (dedup_key, title, company, location, sources, source_urls,
                    salary_min, salary_max, first_seen, last_seen,
                    classification, sub_scores_json, pipeline_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?,
                       datetime('now'), datetime('now'), ?, ?, ?)""",
            (
                "v3only|ml-engineer|nyc",
                "ML Engineer",
                "V3Only Inc",
                "New York, NY",
                '["greenhouse"]',
                '["https://boards.greenhouse.io/v3only/jobs/9"]',
                190000,
                250000,
                "apply",
                # composite = 5+5+4+5+5+4 = 28 (great bucket >=24)
                '{"title_fit": 5, "location_fit": 5, "comp_fit": 4, '
                '"domain_match": 5, "seniority_match": 5, "skills_match": 4}',
                "applied",
            ),
        )
        conn.commit()
        conn.close()

        response = client.get("/pipeline")
        assert response.status_code == 200
        html = response.data.decode()

        # The card must render (data-job-id present)
        assert 'data-job-id="v3only|ml-engineer|nyc"' in html

        # Slice the HTML around the V3Only card's data-job-id and confirm the
        # 6-30 composite (28) renders inside the score badge. The macro emits
        # whitespace around the composite number, so match the span with a
        # regex tolerant of newlines/indentation.
        import re

        anchor = 'data-job-id="v3only|ml-engineer|nyc"'
        anchor_idx = html.index(anchor)
        # The score row + badge sit within ~2000 chars after the anchor.
        card_slice = html[anchor_idx : anchor_idx + 2000]
        badge_match = re.search(
            r'<span[^>]*class="[^"]*rounded[^"]*"[^>]*>\s*(\S+)\s*</span>',
            card_slice,
        )
        assert badge_match, (
            "Could not locate the score badge span in the V3Only kanban card. "
            "Slice: " + card_slice[:800]
        )
        badge_value = badge_match.group(1).strip()
        assert badge_value == "28", (
            f"Kanban card badge shows {badge_value!r}; expected '28' (the 6-30 "
            "composite). The vestigial jobs.score column appears to be leaking "
            "back into the template."
        )
        # And must NOT show '0.0' anywhere in the card — would indicate the
        # template fell back to the dead score column.
        assert "0.0" not in card_slice, (
            "Kanban card shows '0.0' — the vestigial jobs.score column has "
            "leaked back into the template. Card slice: " + card_slice[:500]
        )

    def test_get_jobs_by_status_does_not_select_score_column(self, tmp_db_with_job):
        """get_jobs_by_status must not depend on the vestigial jobs.score column.

        Direct query-level regression: returned job dicts should not carry a
        'score' key, must carry 'sub_scores_json' and 'classification' for the
        Kanban template to derive the composite + classification bucket.
        """
        from job_finder.db._dashboard_queries import get_jobs_by_status

        conn = sqlite3.connect(tmp_db_with_job)
        conn.row_factory = sqlite3.Row
        try:
            result = get_jobs_by_status(conn)
        finally:
            conn.close()

        # Seed job is in 'discovered'
        assert "discovered" in result
        job = result["discovered"][0]
        assert "sub_scores_json" in job
        assert "classification" in job
        assert "score" not in job, (
            "get_jobs_by_status leaked the legacy `jobs.score` column into the "
            "Kanban result — the Kanban path must derive its score signal from "
            "sub_scores_json instead."
        )

    def test_get_jobs_by_status_orders_by_composite_not_score(self, tmp_db_with_job):
        """Sort order on the Kanban must reflect the composite.

        Two rows in 'reviewing': row A has no sub_scores_json (composite=0);
        row B has a high composite (28). With composite ordering, B leads.
        (This guarded against the legacy `jobs.score` column ordering, which
        was dropped in m113.)
        """
        from job_finder.db._dashboard_queries import get_jobs_by_status

        conn = sqlite3.connect(tmp_db_with_job)
        conn.row_factory = sqlite3.Row
        conn.execute(
            """INSERT INTO jobs
                   (dedup_key, title, company, location, sources, source_urls,
                    salary_min, salary_max, first_seen, last_seen,
                    classification, sub_scores_json, pipeline_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?,
                       datetime('now'), datetime('now'), ?, ?, ?)""",
            (
                "legacy-high-score|x|y",
                "Legacy High Score",
                "Heuristic Co",
                "Remote",
                "[]",
                "[]",
                100000,
                150000,
                "reject",
                None,  # no v3.0 assessment -> composite=0
                "reviewing",
            ),
        )
        conn.execute(
            """INSERT INTO jobs
                   (dedup_key, title, company, location, sources, source_urls,
                    salary_min, salary_max, first_seen, last_seen,
                    classification, sub_scores_json, pipeline_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?,
                       datetime('now'), datetime('now'), ?, ?, ?)""",
            (
                "v3-high-composite|x|y",
                "V3 High Composite",
                "Composite Co",
                "Remote",
                "[]",
                "[]",
                100000,
                150000,
                "apply",
                '{"title_fit": 5, "location_fit": 5, "comp_fit": 4, '
                '"domain_match": 5, "seniority_match": 5, "skills_match": 4}',
                "reviewing",
            ),
        )
        conn.commit()
        try:
            result = get_jobs_by_status(conn)
        finally:
            conn.close()

        reviewing = result.get("reviewing", [])
        keys = [j["dedup_key"] for j in reviewing]
        # The v3-only row (composite=28) MUST sort before the no-assessment row
        # (composite=0). If it doesn't, ORDER BY is not using the composite.
        v3_idx = keys.index("v3-high-composite|x|y")
        legacy_idx = keys.index("legacy-high-score|x|y")
        assert v3_idx < legacy_idx, (
            f"Kanban sort does not favor v3 composite=28 over composite=0. keys order: {keys}"
        )
