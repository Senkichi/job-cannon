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
        assert row["ats_evidence_extractor_version"] == ATS_EXTRACTOR_VERSION  # m050-v1 after PR-4

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

    @patch("job_finder.web.ats_identity_reconcile._verify_live")
    def test_refuses_promotion_on_slug_collision(self, mock_verify, seeded_pending_company):
        """A second company can't be promoted onto a slug already owned by another.

        Without this guard, an aggregator name like "Experimentation Jobs"
        could silently take over a real company's ATS slug — the ats_scan
        would then attribute the real company's jobs to the aggregator
        (see the 2026-05-28 Headway / Experimentation Jobs investigation).
        """
        from job_finder.web.ats_identity_reconcile import reconcile_company_ats

        mock_verify.return_value = True
        path, cid = seeded_pending_company
        now = datetime.now().isoformat()
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        # Pre-existing owner of (greenhouse, acmecorp)
        conn.execute(
            """INSERT INTO companies
                  (name, name_raw, ats_platform, ats_slug,
                   ats_probe_status, scan_enabled, created_at, updated_at)
                VALUES ('Acme Corp Inc', 'Acme Corp Inc', 'greenhouse', 'acmecorp',
                        'hit', 1, ?, ?)""",
            (now, now),
        )
        owner_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()

        res = reconcile_company_ats(
            conn,
            cid,
            reason="test",
            config={"ats": {"identity_reconcile": {"enabled": True, "shadow": False}}},
        )

        # Aggregator-style row refused; loser company still untouched.
        assert res["outcome"] == "slug_collision"
        assert res["existing_owner_id"] == owner_id
        row = dict(conn.execute("SELECT * FROM companies WHERE id = ?", (cid,)).fetchone())
        assert row["ats_platform"] is None
        assert row["ats_slug"] is None
        # The legit owner still owns the slug
        owner_row = dict(
            conn.execute("SELECT * FROM companies WHERE id = ?", (owner_id,)).fetchone()
        )
        assert owner_row["ats_slug"] == "acmecorp"
        conn.close()

    @patch("job_finder.web.ats_identity_reconcile._verify_live")
    def test_race_window_collision_returns_slug_collision_with_race_flag(
        self, mock_verify, seeded_pending_company
    ):
        """m076 defense-in-depth: between the SELECT-guard and the UPDATE,
        another transaction can promote (platform, slug). The SELECT-guard
        reports "no owner" so reconcile proceeds, then the UPDATE raises
        sqlite3.IntegrityError on the partial UNIQUE index.

        The handler must catch this, re-query the racing owner, and return
        the same slug_collision outcome shape — with race_detected=True.

        Simulated by wrapping the real sqlite3.Connection in a proxy that
        returns an empty cursor for the SELECT-guard query (so reconcile
        sees "no owner") while letting the UPDATE pass through to the real
        connection (where the partial UNIQUE index does fire).
        """
        from job_finder.web.ats_identity_reconcile import reconcile_company_ats

        mock_verify.return_value = True
        path, cid = seeded_pending_company
        now = datetime.now().isoformat()

        real_conn = sqlite3.connect(path)
        real_conn.row_factory = sqlite3.Row
        # Pre-existing owner that the partial UNIQUE index will defend.
        real_conn.execute(
            """INSERT INTO companies
                  (name, name_raw, ats_platform, ats_slug,
                   ats_probe_status, scan_enabled, created_at, updated_at)
                VALUES ('Real Owner', 'Real Owner', 'greenhouse', 'acmecorp',
                        'hit', 1, ?, ?)""",
            (now, now),
        )
        racer_id = real_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        real_conn.commit()

        class _EmptyCursor:
            def fetchone(self):
                return None

            def fetchall(self):
                return []

        class _ProxyConn:
            """Forwards everything to real_conn but masks the FIRST
            SELECT-guard call.

            The pre-UPDATE guard's signature: a SELECT on companies
            filtered by both `ats_platform = ?` and `ats_slug = ?` AND
            `id != ?` with the current company_id. Returning an empty
            cursor models a racing-but-not-yet-visible owner. Subsequent
            matching SELECTs (the post-IntegrityError racer re-query in
            the recovery branch) must pass through so the handler can
            find the real owner.
            """

            def __init__(self, real):
                self._real = real
                self.row_factory = real.row_factory
                self._guard_masked = False

            def execute(self, sql, *args, **kwargs):
                if (
                    not self._guard_masked
                    and "ats_platform = ?" in sql
                    and "ats_slug = ?" in sql
                    and "id != ?" in sql
                    and args
                    and len(args[0]) == 3
                    and args[0][2] == cid
                ):
                    self._guard_masked = True
                    return _EmptyCursor()
                return self._real.execute(sql, *args, **kwargs)

            def commit(self):
                return self._real.commit()

        proxy = _ProxyConn(real_conn)

        res = reconcile_company_ats(
            proxy,  # type: ignore[arg-type]
            cid,
            reason="test",
            config={"ats": {"identity_reconcile": {"enabled": True, "shadow": False}}},
        )

        # Race-detected outcome shape matches the SELECT-guard outcome,
        # plus the new race_detected sentinel.
        assert res["outcome"] == "slug_collision"
        assert res["existing_owner_id"] == racer_id
        assert res["existing_owner_name"] == "Real Owner"
        assert res["race_detected"] is True

        # Loser left in pre-reconcile state (no platform/slug written).
        loser_row = dict(
            real_conn.execute("SELECT * FROM companies WHERE id = ?", (cid,)).fetchone()
        )
        assert loser_row["ats_platform"] is None
        assert loser_row["ats_slug"] is None
        # Real owner still owns the pair.
        racer_row = dict(
            real_conn.execute(
                "SELECT ats_platform, ats_slug FROM companies WHERE id = ?",
                (racer_id,),
            ).fetchone()
        )
        assert racer_row["ats_platform"] == "greenhouse"
        assert racer_row["ats_slug"] == "acmecorp"
        real_conn.close()


def _seed_scan_disabled_miss(db_path: str) -> int:
    """Insert a frozen custom-miss company (scan_enabled=0) with a careers_url."""
    from job_finder.web.db_migrate import run_migrations

    run_migrations(db_path)
    now = datetime.now().isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO companies
              (name, name_raw, careers_url, ats_probe_status, miss_reason,
               scan_enabled, created_at, updated_at)
           VALUES ('frozenco', 'FrozenCo', 'https://frozenco.com/careers',
                   'miss', 'speculative_exhausted', 0, ?, ?)""",
        (now, now),
    )
    cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    return int(cid)


class TestPromoteFromCareersLinkReenableScan:
    """The batch reprobe re-promotes frozen (scan_enabled=0) custom-miss companies
    when their careers page now embeds a live ATS board. ``reenable_scan`` lets a
    live-verified promotion override the earlier give-up — atomically, and only on
    verify success."""

    @patch("job_finder.web.ats_identity_reconcile._verify_live", return_value=True)
    def test_reenable_scan_promotes_and_reenables(self, _verify, tmp_db_path):
        from job_finder.web.ats_identity_reconcile import promote_from_careers_link

        cid = _seed_scan_disabled_miss(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        res = promote_from_careers_link(
            conn,
            cid,
            "greenhouse",
            "frozenco",
            page_url="https://frozenco.com/careers",
            config={"ats": {"identity_reconcile": {"enabled": True, "shadow": False}}},
            reenable_scan=True,
        )
        assert res["outcome"] == "promoted"
        row = dict(conn.execute("SELECT * FROM companies WHERE id = ?", (cid,)).fetchone())
        conn.close()
        assert row["ats_probe_status"] == "hit"
        assert row["ats_platform"] == "greenhouse"
        assert row["ats_slug"] == "frozenco"
        assert row["scan_enabled"] == 1  # re-enabled atomically with the promotion

    def test_default_still_skips_scan_disabled(self, tmp_db_path):
        from job_finder.web.ats_identity_reconcile import promote_from_careers_link

        cid = _seed_scan_disabled_miss(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        res = promote_from_careers_link(
            conn,
            cid,
            "greenhouse",
            "frozenco",
            page_url="https://frozenco.com/careers",
            config={"ats": {"identity_reconcile": {"enabled": True, "shadow": False}}},
        )
        row = dict(conn.execute("SELECT * FROM companies WHERE id = ?", (cid,)).fetchone())
        conn.close()
        # Without reenable_scan the existing contract is unchanged: frozen → skip.
        assert res["outcome"] == "skipped_scan_disabled"
        assert row["ats_probe_status"] == "miss"
        assert row["scan_enabled"] == 0

    @patch("job_finder.web.ats_identity_reconcile._verify_live", return_value=False)
    def test_reenable_scan_no_write_when_verify_fails(self, _verify, tmp_db_path):
        from job_finder.web.ats_identity_reconcile import promote_from_careers_link

        cid = _seed_scan_disabled_miss(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        res = promote_from_careers_link(
            conn,
            cid,
            "greenhouse",
            "frozenco",
            page_url="https://frozenco.com/careers",
            config={"ats": {"identity_reconcile": {"enabled": True, "shadow": False}}},
            reenable_scan=True,
        )
        row = dict(conn.execute("SELECT * FROM companies WHERE id = ?", (cid,)).fetchone())
        conn.close()
        # A dead board must not flip status OR re-enable scan.
        assert res["outcome"] == "verify_failed"
        assert row["ats_probe_status"] == "miss"
        assert row["scan_enabled"] == 0
