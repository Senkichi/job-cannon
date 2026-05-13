"""Unit and integration tests for ATS URL→verify identity reconciliation (Phase A+B)."""

import sqlite3
from datetime import datetime
from unittest.mock import patch

import pytest

from job_finder.web.ats_detection import (
    ATS_EXTRACTOR_VERSION,
    aggregate_ats_candidates_from_job_bundles,
    extract_ats_from_url_best,
)


class TestExtractAtsFromUrlBest:
    def test_api_greenhouse_outranks_boards_for_same_slug(self):
        api = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
        board = "https://boards.greenhouse.io/acme/jobs/1"
        assert extract_ats_from_url_best(api)[2] > extract_ats_from_url_best(board)[2]

    def test_lever_api_pattern(self):
        hit = extract_ats_from_url_best("https://api.lever.co/v0/postings/acme")
        assert hit == ("lever", "acme", 10)


class TestAggregateAtsCandidates:
    def test_majority_picks_greenhouse(self):
        bundles = [
            {
                "dedup_key": "a",
                "last_seen": "2026-05-01T00:00:00",
                "urls": ["https://boards.greenhouse.io/winner/jobs/1"],
            },
            {
                "dedup_key": "b",
                "last_seen": "2026-05-02T00:00:00",
                "urls": ["https://boards.greenhouse.io/winner/jobs/2"],
            },
            {
                "dedup_key": "c",
                "last_seen": "2026-05-01T00:00:00",
                "urls": ["https://jobs.lever.co/loser/x"],
            },
        ]
        winner, abstain = aggregate_ats_candidates_from_job_bundles(bundles)
        assert abstain is None
        assert winner == ("greenhouse", "winner")

    def test_abstains_on_perfect_two_way_tie(self):
        bundles = [
            {
                "dedup_key": "a",
                "last_seen": "2026-05-01T12:00:00",
                "urls": ["https://jobs.lever.co/foo/x"],
            },
            {
                "dedup_key": "b",
                "last_seen": "2026-05-01T12:00:00",
                "urls": ["https://boards.greenhouse.io/bar/x"],
            },
        ]
        winner, abstain = aggregate_ats_candidates_from_job_bundles(bundles)
        assert winner is None
        assert abstain == "ambiguous_tie"


@pytest.fixture()
def seeded_pending_company(tmp_db_path):
    from job_finder.web.db_migrate import run_migrations

    run_migrations(tmp_db_path)
    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    now = datetime.now().isoformat()
    conn.execute(
        """INSERT INTO companies (name, name_raw, ats_probe_status, scan_enabled, created_at, updated_at)
           VALUES ('acme', 'Acme', 'pending', 1, ?, ?)""",
        (now, now),
    )
    cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls,
           first_seen, last_seen, company_id, pipeline_status)
           VALUES ('k1', 'T', 'Acme', 'Remote', '[]',
           ?, ?, ?, ?, 'discovered')""",
        (
            '["https://boards.greenhouse.io/acmecorp/jobs/1"]',
            now,
            now,
            cid,
        ),
    )
    conn.commit()
    conn.close()
    return tmp_db_path, int(cid)


class TestReconcileCompanyAts:
    @patch("job_finder.web.ats_identity_reconcile._verify_live")
    def test_promotes_after_verify(self, mock_verify, seeded_pending_company):
        from job_finder.web.ats_identity_reconcile import reconcile_company_ats

        mock_verify.return_value = True
        path, cid = seeded_pending_company
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        res = reconcile_company_ats(
            conn,
            cid,
            reason="test",
            config={"ats": {"identity_reconcile": {"enabled": True, "shadow": False}}},
        )
        conn.close()
        assert res["outcome"] == "promoted"
        assert res["platform"] == "greenhouse"
        assert res["slug"] == "acmecorp"

        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        row = dict(conn.execute("SELECT * FROM companies WHERE id = ?", (cid,)).fetchone())
        conn.close()
        assert row["ats_probe_status"] == "hit"
        assert row["ats_evidence_trigger"] == "test"
        assert row["ats_evidence_extractor_version"] == ATS_EXTRACTOR_VERSION

    def test_skips_existing_hit(self, seeded_pending_company):
        from job_finder.web.ats_identity_reconcile import reconcile_company_ats

        path, cid = seeded_pending_company
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "UPDATE companies SET ats_probe_status = 'hit', ats_platform='lever', ats_slug='x' WHERE id = ?",
            (cid,),
        )
        conn.commit()
        res = reconcile_company_ats(conn, cid, reason="test", config={})
        conn.close()
        assert res["outcome"] == "skipped_already_hit"
