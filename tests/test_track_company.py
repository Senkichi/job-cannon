"""Tests for company tracking + suggestions (WP6, release polish).

Track contract: POST /companies/track upserts a pending companies row
(idempotent; re-enables scan_enabled=0 rows) and returns the ✓ fragment.
Suggestions contract: untracked feed-frequent companies ranked by good-fit
count then volume; tracked companies excluded via normalized-name match.
Job-row contract: expanded row shows Track button (by name, not company_id);
already-tracked companies render the ✓ state up front.
"""

import sqlite3

from job_finder.web.company_suggestions import get_suggested_companies


def _conn(app):
    conn = sqlite3.connect(app.config["DB_PATH"])
    conn.row_factory = sqlite3.Row
    return conn


def _insert_job(conn, dedup_key, company, classification=None, company_id=None):
    conn.execute(
        """INSERT INTO jobs
               (dedup_key, title, company, company_id, location, sources, source_urls,
                description, classification, first_seen, last_seen, pipeline_status)
           VALUES (?, 'Engineer', ?, ?, 'Remote', '["linkedin"]', '[]',
                   'short', ?, '2026-06-01T00:00:00', '2026-06-01T00:00:00', 'discovered')""",
        (dedup_key, company, company_id, classification),
    )
    conn.commit()


class TestTrackRoute:
    def test_track_new_company_creates_pending_row(self, app, client):
        resp = client.post("/companies/track", data={"company_name": "Northwind Trading"})
        assert resp.status_code == 200
        assert "Tracking" in resp.get_data(as_text=True)

        conn = _conn(app)
        row = conn.execute(
            "SELECT * FROM companies WHERE name_raw = 'Northwind Trading'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["ats_probe_status"] == "pending"
        assert row["scan_enabled"] == 1

    def test_track_existing_is_idempotent_and_keeps_hit(self, app, client):
        conn = _conn(app)
        from job_finder.web.ats_scanner import upsert_company

        upsert_company(
            conn,
            name="Acme",
            ats_platform="greenhouse",
            ats_slug="acme",
            ats_probe_status="hit",
        )
        conn.close()

        resp = client.post("/companies/track", data={"company_name": "Acme"})
        assert resp.status_code == 200
        assert "Tracking" in resp.get_data(as_text=True)

        conn = _conn(app)
        rows = conn.execute("SELECT * FROM companies WHERE name_raw = 'Acme'").fetchall()
        conn.close()
        assert len(rows) == 1
        # hit must never downgrade to pending (probe-status precedence).
        assert rows[0]["ats_probe_status"] == "hit"

    def test_track_reenables_disabled_company(self, app, client):
        conn = _conn(app)
        from job_finder.web.ats_scanner import upsert_company

        cid = upsert_company(conn, name="Disabled Co", ats_probe_status="hit")
        conn.execute("UPDATE companies SET scan_enabled = 0 WHERE id = ?", (cid,))
        conn.commit()
        conn.close()

        resp = client.post("/companies/track", data={"company_name": "Disabled Co"})
        assert resp.status_code == 200

        conn = _conn(app)
        row = conn.execute("SELECT scan_enabled FROM companies WHERE id = ?", (cid,)).fetchone()
        conn.close()
        assert row["scan_enabled"] == 1

    def test_track_missing_name_is_400(self, client):
        assert client.post("/companies/track", data={}).status_code == 400
        assert client.post("/companies/track", data={"company_name": "  "}).status_code == 400


class TestIsCompanyTracked:
    def test_tracked_by_normalized_and_raw_name(self, app):
        from job_finder.web.ats_scanner import is_company_tracked, upsert_company

        conn = _conn(app)
        upsert_company(conn, name="Stripe Inc.", ats_probe_status="pending")
        assert is_company_tracked(conn, "Stripe Inc.")
        assert not is_company_tracked(conn, "Totally Unknown Co")
        assert not is_company_tracked(conn, "")
        conn.close()

    def test_disabled_company_reads_as_untracked(self, app):
        from job_finder.web.ats_scanner import is_company_tracked, upsert_company

        conn = _conn(app)
        cid = upsert_company(conn, name="Paused Co", ats_probe_status="hit")
        conn.execute("UPDATE companies SET scan_enabled = 0 WHERE id = ?", (cid,))
        conn.commit()
        assert not is_company_tracked(conn, "Paused Co")
        conn.close()


class TestSuggestions:
    def test_ranked_by_good_fit_then_volume(self, app):
        conn = _conn(app)
        # BigCo: 3 jobs, 0 good. GoodCo: 2 jobs, 2 good. → GoodCo first.
        for i in range(3):
            _insert_job(conn, f"bigco|{i}", "BigCo")
        for i in range(2):
            _insert_job(conn, f"goodco|{i}", "GoodCo", classification="apply")
        suggestions = get_suggested_companies(conn)
        conn.close()
        names = [s["company"] for s in suggestions]
        assert names == ["GoodCo", "BigCo"]
        assert suggestions[0]["good_cnt"] == 2
        assert suggestions[1]["job_cnt"] == 3

    def test_tracked_companies_excluded_via_normalization(self, app):
        from job_finder.web.ats_scanner import upsert_company

        conn = _conn(app)
        # Tracked under a raw name that normalizes differently than the
        # job's raw company string — Python-side normalization must match.
        upsert_company(conn, name="Stripe, Inc.", ats_probe_status="hit")
        _insert_job(conn, "stripe|1", "Stripe Inc.")
        _insert_job(conn, "other|1", "Other Co")
        suggestions = get_suggested_companies(conn)
        conn.close()
        names = [s["company"] for s in suggestions]
        assert "Other Co" in names
        assert "Stripe Inc." not in names

    def test_linked_jobs_excluded(self, app):
        from job_finder.web.ats_scanner import upsert_company

        conn = _conn(app)
        cid = upsert_company(conn, name="Linked Co", ats_probe_status="hit")
        _insert_job(conn, "linked|1", "Linked Co", company_id=cid)
        suggestions = get_suggested_companies(conn)
        conn.close()
        assert suggestions == []

    def test_limit_respected(self, app):
        conn = _conn(app)
        for i in range(12):
            _insert_job(conn, f"co{i}|1", f"Company {i}")
        suggestions = get_suggested_companies(conn, limit=8)
        conn.close()
        assert len(suggestions) == 8


class TestCompaniesPageCard:
    def test_card_shown_with_suggestions(self, app, client):
        conn = _conn(app)
        _insert_job(conn, "feedco|1", "FeedCo", classification="apply")
        conn.close()
        body = client.get("/companies/").get_data(as_text=True)
        assert "Companies in your feed" in body
        assert "FeedCo" in body

    def test_card_omitted_when_no_suggestions(self, client):
        body = client.get("/companies/").get_data(as_text=True)
        assert "Companies in your feed" not in body


class TestJobRowButton:
    def _expand(self, app, client, company):
        conn = _conn(app)
        _insert_job(conn, "row|1", company)
        conn.close()
        return client.get("/jobs/row%7C1/expand", headers={"HX-Request": "true"})

    def test_untracked_company_shows_track_button(self, app, client):
        resp = self._expand(app, client, "Untracked Co")
        body = resp.get_data(as_text=True)
        assert resp.status_code == 200
        assert "Track careers page" in body

    def test_tracked_company_shows_check_state(self, app, client):
        conn = _conn(app)
        from job_finder.web.ats_scanner import upsert_company

        upsert_company(conn, name="Tracked Co", ats_probe_status="hit")
        conn.close()
        resp = self._expand(app, client, "Tracked Co")
        body = resp.get_data(as_text=True)
        assert "Tracking careers page" in body
        assert "Track careers page" not in body
