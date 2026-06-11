"""Tests for demo-mode seeding (``job-cannon --demo``, WP1 release polish).

These run against the fully-migrated schema on purpose: if a future migration
adds a NOT NULL column or changes a constraint the seeder violates, the break
surfaces at PR time, not when a user runs ``--demo``. Never mark these
slow/skip.
"""

import json
import sqlite3
from datetime import datetime

import pytest

from job_finder.db._classification import derive_classification
from job_finder.demo_data import DEMO_COMPANIES, DEMO_JOBS
from job_finder.demo_seed import build_demo_config, seed_demo_db
from job_finder.web.db_migrate import run_migrations


@pytest.fixture
def demo_conn(tmp_path):
    """Migrated + seeded demo DB, opened with Row factory."""
    db_path = str(tmp_path / "demo.db")
    run_migrations(db_path)
    seed_demo_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


class TestSeedContents:
    def test_row_counts(self, demo_conn):
        jobs = demo_conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        companies = demo_conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        assert jobs == len(DEMO_JOBS) == 30
        assert companies == len(DEMO_COMPANIES) == 4

    def test_classification_distribution(self, demo_conn):
        rows = demo_conn.execute(
            "SELECT classification, COUNT(*) AS n FROM jobs GROUP BY classification"
        ).fetchall()
        by_class = {r["classification"]: r["n"] for r in rows}
        assert by_class[None] == 8  # unscored
        assert by_class["apply"] == 6
        assert by_class["consider"] == 9
        assert by_class["reject"] == 5
        assert by_class["low_signal"] == 2
        # `skip` is unreachable for integer 1-5 sub-scores — the seed must
        # never pretend otherwise.
        assert "skip" not in by_class

    def test_every_classification_is_derivable(self, demo_conn):
        """Seeded classifications must equal what derive_classification recomputes.

        Guards against hand-set classifications drifting from the rubric —
        the demo must show real rubric behavior, not invented labels.
        """
        rows = demo_conn.execute(
            "SELECT dedup_key, classification, sub_scores_json, legitimacy_note,"
            "       enrichment_tier, COALESCE(LENGTH(jd_full), 0) AS jd_len"
            "  FROM jobs WHERE classification IS NOT NULL"
        ).fetchall()
        assert len(rows) == 22
        for r in rows:
            recomputed = derive_classification(
                json.loads(r["sub_scores_json"]),
                r["legitimacy_note"],
                enrichment_tier=r["enrichment_tier"],
                jd_full_length=r["jd_len"],
            )
            assert recomputed == r["classification"], r["dedup_key"]

    def test_scored_rows_carry_demo_attribution_and_rationale(self, demo_conn):
        rows = demo_conn.execute(
            "SELECT scoring_provider, scoring_model, fit_analysis FROM jobs "
            "WHERE classification IS NOT NULL"
        ).fetchall()
        for r in rows:
            assert r["scoring_provider"] == "demo"
            assert r["scoring_model"] == "synthetic"
            rationale = json.loads(r["fit_analysis"])
            assert set(rationale) == {
                "strengths",
                "gaps",
                "talking_points",
                "resume_priority_skills",
            }

    def test_unscored_rows_are_scorable(self, demo_conn):
        """Unscored seeds must count as scorable (jd_full set, not dismissed) —
        they exist so the no-provider state (WP3) is demo-able."""
        from job_finder.web.exclusion_filter import count_scorable

        n = count_scorable(demo_conn, build_demo_config("unused"))
        assert n == 8

    def test_pipeline_events_exist_for_moved_jobs(self, demo_conn):
        moved = demo_conn.execute(
            "SELECT dedup_key, pipeline_status FROM jobs WHERE pipeline_status != 'discovered'"
        ).fetchall()
        assert moved, "expected seeded jobs beyond 'discovered'"
        for r in moved:
            events = demo_conn.execute(
                "SELECT COUNT(*) FROM pipeline_events WHERE job_id = ?",
                (r["dedup_key"],),
            ).fetchone()[0]
            assert events >= 1, r["dedup_key"]

    def test_kanban_columns_populated(self, demo_conn):
        statuses = {
            r["pipeline_status"]
            for r in demo_conn.execute("SELECT DISTINCT pipeline_status FROM jobs")
        }
        assert {"applied", "phone_screen", "technical", "offer", "rejected"} <= statuses

    def test_onboarding_marked_complete(self, demo_conn):
        row = demo_conn.execute(
            "SELECT onboarding_complete FROM onboarding_state WHERE id = 1"
        ).fetchone()
        assert row is not None and row[0] == 1

    def test_timestamps_are_naive_utc_iso(self, demo_conn):
        rows = demo_conn.execute("SELECT first_seen, last_seen, posted_date FROM jobs").fetchall()
        for r in rows:
            for value in (r["first_seen"], r["last_seen"], r["posted_date"]):
                assert value, "timestamp column unexpectedly empty"
                parsed = datetime.fromisoformat(value)
                assert parsed.tzinfo is None, value

    def test_companies_are_scan_ready(self, demo_conn):
        rows = demo_conn.execute(
            "SELECT name_raw, ats_platform, ats_probe_status, scan_enabled, careers_url "
            "FROM companies"
        ).fetchall()
        platforms = {r["ats_platform"] for r in rows}
        assert platforms == {"greenhouse", "lever", "ashby"}
        for r in rows:
            assert r["ats_probe_status"] == "hit"
            assert r["scan_enabled"] == 1
            assert r["careers_url"]

    def test_reseeding_same_db_is_idempotent(self, demo_conn, tmp_path):
        events_before = demo_conn.execute("SELECT COUNT(*) FROM pipeline_events").fetchone()[0]
        seed_demo_db(str(tmp_path / "demo.db"))
        jobs = demo_conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        events_after = demo_conn.execute("SELECT COUNT(*) FROM pipeline_events").fetchone()[0]
        assert jobs == 30
        # Re-seeding must not replay pipeline chains (duplicate events /
        # backward status moves).
        assert events_after == events_before


class TestDemoApp:
    @pytest.fixture
    def demo_client(self, tmp_path):
        """App built exactly the way __main__ --demo builds it."""
        from job_finder.web import create_app

        demo_dir = str(tmp_path / "demo-userdata")
        cfg = build_demo_config(demo_dir)
        db_path = cfg["db"]["path"]
        import os

        os.makedirs(demo_dir, exist_ok=True)
        run_migrations(db_path)
        seed_demo_db(db_path)
        app = create_app(config=cfg)
        app.config["TESTING"] = True
        return app.test_client()

    def test_job_board_renders_with_demo_banner(self, demo_client):
        resp = demo_client.get("/jobs/", follow_redirects=True)
        assert resp.status_code == 200
        text = resp.get_data(as_text=True)
        assert "Demo mode — sample data only" in text
        # A seeded job and its classification chip surface on the board.
        assert "Northbeam Analytics" in text

    def test_root_redirects_to_board(self, demo_client):
        resp = demo_client.get("/")
        assert resp.status_code == 302

    def test_companies_page_shows_seeded_ats_rows(self, demo_client):
        resp = demo_client.get("/companies/", follow_redirects=True)
        assert resp.status_code == 200
        text = resp.get_data(as_text=True)
        assert "Helio Systems" in text

    def test_scheduler_skipped(self, demo_client):
        # build_demo_config carries SKIP_SCHEDULER; create_app propagates it.
        cfg = build_demo_config("unused")
        assert cfg["SKIP_SCHEDULER"] is True
        assert cfg["DEMO_MODE"] is True

    def test_normal_app_has_no_demo_banner(self, client):
        resp = client.get("/jobs/", follow_redirects=True)
        assert resp.status_code == 200
        assert "Demo mode — sample data only" not in resp.get_data(as_text=True)


class TestCli:
    def test_parser_accepts_demo_flag(self):
        from job_finder.__main__ import _build_parser

        args = _build_parser().parse_args(["--demo"])
        assert args.demo is True

    def test_demo_defaults_off(self):
        from job_finder.__main__ import _build_parser

        args = _build_parser().parse_args([])
        assert args.demo is False
