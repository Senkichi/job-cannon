"""Tests for the no-provider banner + budget-skip eventing (WP3, release polish).

Banner contract: shown iff ``not scoring_available and unscored_count > 0``,
on BOTH the dashboard quick-actions partial (full page + HTMX fragment route)
and the job board. Budget-skip contract: exactly one
``scoring_skipped_budget`` activity row per user-initiated attempt / batch
run, never per cascade-provider skip.
"""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

BANNER_TEXT = "no AI provider is configured"


def _insert_scorable_job(conn, dedup_key="acme|data scientist"):
    """Insert a job that count_scorable counts: classification NULL, jd_full set."""
    conn.execute(
        """INSERT INTO jobs
               (dedup_key, title, company, location, sources, source_urls,
                description, jd_full, first_seen, last_seen, pipeline_status)
           VALUES (?, 'Data Scientist', 'Acme', 'Remote', '["linkedin"]', '[]',
                   'short', ?, '2026-06-01T00:00:00', '2026-06-01T00:00:00',
                   'discovered')""",
        (dedup_key, "x" * 250),
    )
    conn.commit()


@pytest.fixture
def app_with_unscored(app):
    """App whose DB contains one scorable (unscored, jd_full-bearing) job."""
    import sqlite3

    conn = sqlite3.connect(app.config["DB_PATH"])
    _insert_scorable_job(conn)
    conn.close()
    return app


class TestDashboardBanner:
    def test_banner_shown_when_no_provider_and_unscored(self, app_with_unscored):
        with patch(
            "job_finder.web.blueprints.dashboard.cached_tier_available",
            return_value=False,
        ):
            resp = app_with_unscored.test_client().get("/dashboard/", follow_redirects=True)
        assert resp.status_code == 200
        assert BANNER_TEXT in resp.get_data(as_text=True)

    def test_banner_survives_fragment_refresh(self, app_with_unscored):
        """The quick-actions HTMX fragment route must carry the same context —
        otherwise the banner vanishes on the first auto-refresh."""
        with patch(
            "job_finder.web.blueprints.dashboard.cached_tier_available",
            return_value=False,
        ):
            resp = app_with_unscored.test_client().get(
                "/dashboard/quick-actions", headers={"HX-Request": "true"}
            )
        assert resp.status_code == 200
        assert BANNER_TEXT in resp.get_data(as_text=True)

    def test_banner_hidden_when_provider_available(self, app_with_unscored):
        with patch(
            "job_finder.web.blueprints.dashboard.cached_tier_available",
            return_value=True,
        ):
            resp = app_with_unscored.test_client().get("/dashboard/", follow_redirects=True)
        assert BANNER_TEXT not in resp.get_data(as_text=True)

    def test_banner_hidden_when_nothing_unscored(self, app):
        with patch(
            "job_finder.web.blueprints.dashboard.cached_tier_available",
            return_value=False,
        ):
            resp = app.test_client().get("/dashboard/", follow_redirects=True)
        assert BANNER_TEXT not in resp.get_data(as_text=True)


class TestJobBoardBanner:
    def test_banner_shown_on_board(self, app_with_unscored):
        # jobs.index imports cached_tier_available inside the function, so the
        # patch target is the provider_status module itself.
        with patch(
            "job_finder.web.provider_status.cached_tier_available",
            return_value=False,
        ):
            resp = app_with_unscored.test_client().get("/jobs/", follow_redirects=True)
        assert resp.status_code == 200
        assert BANNER_TEXT in resp.get_data(as_text=True)

    def test_banner_hidden_on_board_when_available(self, app_with_unscored):
        with patch(
            "job_finder.web.provider_status.cached_tier_available",
            return_value=True,
        ):
            resp = app_with_unscored.test_client().get("/jobs/", follow_redirects=True)
        assert BANNER_TEXT not in resp.get_data(as_text=True)


def _budget_skip_rows(db_path):
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT entity_id, metadata FROM user_activity WHERE action = 'scoring_skipped_budget'"
        ).fetchall()
    finally:
        conn.close()


class TestBudgetSkipEventing:
    def test_rescore_blocked_by_budget_logs_one_event(self, app_with_unscored):
        import sqlite3

        conn = sqlite3.connect(app_with_unscored.config["DB_PATH"])
        conn.execute(
            "UPDATE jobs SET jd_full = ? WHERE dedup_key = ?", ("y" * 300, "acme|data scientist")
        )
        conn.commit()
        conn.close()

        with patch("job_finder.web.claude_client.cost_gate", return_value=False):
            resp = app_with_unscored.test_client().post(
                "/jobs/acme%7Cdata%20scientist/rescore",
                headers={"HX-Request": "true"},
            )
        assert resp.status_code == 200
        rows = _budget_skip_rows(app_with_unscored.config["DB_PATH"])
        assert len(rows) == 1
        assert rows[0][0] == "acme|data scientist"
        assert json.loads(rows[0][1])["path"] == "rescore"

    def test_batch_run_blocked_by_budget_logs_exactly_one_event(self, app_with_unscored):
        """Three skipped jobs + closed gate → ONE activity row, not three."""
        import sqlite3

        db_path = app_with_unscored.config["DB_PATH"]
        conn = sqlite3.connect(db_path)
        _insert_scorable_job(conn, "acme|ml engineer")
        _insert_scorable_job(conn, "acme|data engineer")
        conn.execute(
            "INSERT INTO batch_score_sessions (session_type, status, total, scored, skipped, started_at) "
            "VALUES ('scoring', 'running', 3, 0, 0, '2026-06-01T00:00:00')"
        )
        conn.commit()
        session_id = conn.execute("SELECT MAX(id) FROM batch_score_sessions").fetchone()[0]
        conn.close()

        from job_finder.web.blueprints.batch_scoring import _run_batch_bg

        config = app_with_unscored.config.get("JF_CONFIG", {})
        with (
            patch(
                "job_finder.web.scoring_orchestrator.score_and_persist_job",
                return_value=SimpleNamespace(status="error", error="cascade exhausted"),
            ),
            patch("job_finder.web.claude_client.cost_gate", return_value=False),
        ):
            _run_batch_bg(db_path, session_id, config)

        rows = _budget_skip_rows(db_path)
        assert len(rows) == 1
        assert json.loads(rows[0][1]) == {"path": "batch", "skipped_count": 3}

    def test_batch_run_with_scores_logs_no_event(self, app_with_unscored):
        """A run that scored something is not budget-terminal — no event."""
        import sqlite3

        db_path = app_with_unscored.config["DB_PATH"]
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO batch_score_sessions (session_type, status, total, scored, skipped, started_at) "
            "VALUES ('scoring', 'running', 1, 0, 0, '2026-06-01T00:00:00')"
        )
        conn.commit()
        session_id = conn.execute("SELECT MAX(id) FROM batch_score_sessions").fetchone()[0]
        conn.close()

        from job_finder.web.blueprints.batch_scoring import _run_batch_bg

        config = app_with_unscored.config.get("JF_CONFIG", {})
        with patch(
            "job_finder.web.scoring_orchestrator.score_and_persist_job",
            return_value=SimpleNamespace(status="ok", error=None),
        ):
            _run_batch_bg(db_path, session_id, config)

        assert _budget_skip_rows(db_path) == []
